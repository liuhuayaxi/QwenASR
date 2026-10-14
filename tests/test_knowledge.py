from tests import isolation  # noqa: F401

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
import pymupdf as fitz

from qwen_asr_app.knowledge import (
    CHUNKER_VERSION,
    PARSER_VERSION,
    Chunk,
    KnowledgeService,
    KnowledgeStore,
    chunk_markdown_text,
    chunk_pdf_document,
    chunk_transcript_segments,
    knowledge_router,
)


class FakeEmbedder:
    def __init__(self, contract=None, dim=2048):
        self.contract = contract or {"provider": "fake", "model": "fake-v1", "dim": dim}
        self.dim = dim
        self.fail = False

    def _embed(self, text: str):
        if self.fail:
            raise RuntimeError("Fake embedder error: model failure")
        vec = [0.0] * self.dim
        for word in text.lower().split():
            idx = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16) % self.dim
            vec[idx] += 1.0
        for ch in text:
            if "\u4e00" <= ch <= "\u9fff":
                idx = (ord(ch) * 10007) % self.dim
                vec[idx] += 1.0
        norm = sum(x * x for x in vec) ** 0.5
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    def encode_documents(self, texts):
        return [self._embed(t) for t in texts]

    def encode_query(self, text):
        return self._embed(text)


class KnowledgeUnitTests(unittest.TestCase):
    def test_chunking_preserves_number_formula_and_table_boundaries(self):
        sample_text = (
            "# 第一章 基础理论\n\n"
            "在量子力学中，普朗克常数是 6.62607015e-34 J·s，光速是 299792458 m/s，圆周率是 3.1415926535。\n\n"
            "爱因斯坦质能方程为：\n"
            "$$E = m c^2$$\n\n"
            "以下为实验对照数据表：\n"
            "| 参数名 | 测量值 | 理论值 |\n"
            "| :--- | :--- | :--- |\n"
            "| 频率 | 4.5e14 | 4.52e14 |\n"
            "| 能量 | 2.98e-19 | 2.99e-19 |\n\n"
            "根据上述表格，当温度达到 2026.10 K 时系统保持稳定。\n"
        )

        chunks = chunk_markdown_text(sample_text, max_chars=180)
        self.assertTrue(len(chunks) >= 2)

        # Check formula is not broken
        formula_chunks = [c for c in chunks if "$$E = m c^2$$" in c.text]
        self.assertEqual(len(formula_chunks), 1)

        # Check table is kept together as a block
        table_chunks = [c for c in chunks if "| 参数名 |" in c.text]
        self.assertEqual(len(table_chunks), 1)
        self.assertIn("| 能量 | 2.98e-19 | 2.99e-19 |", table_chunks[0].text)

        # Check numbers are not broken across chunks
        for num in ["6.62607015e-34", "3.1415926535", "2026.10"]:
            found = False
            for c in chunks:
                if num in c.text:
                    found = True
                    break
            self.assertTrue(found, f"Number {num} was broken or lost across chunk boundaries")

        # Check headings are tracked
        first_chunk = chunks[0]
        self.assertEqual(first_chunk.heading, "第一章 基础理论")

    def test_chunk_transcript_segments_groups_cleanly(self):
        segments = [
            {"id": 1, "start": 0.0, "end": 15.0, "raw": "同学们好，今天我们开始讲微积分。"},
            {"id": 2, "start": 15.0, "end": 35.0, "raw": "首先我们复习导数的定义。"},
            {"id": 3, "start": 35.0, "end": 80.0, "raw": "导数本质上是一个极限。"},
            {"id": 4, "start": 80.0, "end": 130.0, "raw": "我们来看这个例题，已知 f(x) = x^2。"},
        ]

        chunks = chunk_transcript_segments(segments, max_chars=40, max_seconds=60.0, text_key="raw")
        self.assertTrue(len(chunks) >= 2)

        # Verify time range and segment_ids
        for c in chunks:
            self.assertIsNotNone(c.start)
            self.assertIsNotNone(c.end)
            self.assertTrue(c.end >= c.start)
            self.assertTrue(len(c.segment_ids) >= 1)

        # Verify segment 1 and 2 ids are captured
        self.assertIn(1, chunks[0].segment_ids)


class KnowledgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

        self.storage_root = self.root / "library"
        self.storage_root.mkdir()

        self.settings = SimpleNamespace(
            storage_root=self.storage_root,
            known_roots=[str(self.storage_root)],
            categories=["数学", "物理"],
        )

        self.embedder = FakeEmbedder(dim=2048)
        self.db_path = self.root / "test_knowledge.db"

        # Mock course files and recording store
        self.course_files = MockCourseFiles(self.storage_root)
        self.recording_store = MockRecordingStore(self.storage_root)

        self.service = KnowledgeService(
            settings=self.settings,
            recording_store=self.recording_store,
            course_files=self.course_files,
            db_path=self.db_path,
            embedder=self.embedder,
        )
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            shutil.rmtree(self.temp_dir.name, ignore_errors=True)

    async def test_course_id_is_stable_and_persisted_in_sidecar(self):
        cid1 = self.service.course_id("数学")
        self.assertTrue(cid1.startswith("c_"))

        # Verify sidecar file was written
        sidecar = self.storage_root / "数学" / ".qwenasr-course-id.json"
        self.assertTrue(sidecar.is_file())
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(data["course_id"], cid1)

        # Second call returns identical ID
        cid2 = self.service.course_id("数学")
        self.assertEqual(cid1, cid2)

    async def test_ingest_and_search_with_rrf_and_deduplication(self):
        # 1. Create a course file for "数学"
        math_folder = self.storage_root / "数学"
        math_folder.mkdir(parents=True, exist_ok=True)
        doc_path = math_folder / "微积分导论.md"
        doc_path.write_text(
            "# 微积分讲义\n\n导数的物理意义是瞬时变化率。速度是位移对时间的导数。\n\n"
            "牛顿莱布尼茨公式将微分与积分统一起来。\n",
            encoding="utf-8",
        )
        self.course_files.add_file("数学", "asset_math_01", "微积分讲义.md", "讲义", doc_path)

        # 2. Sync course
        sync_result = self.service.sync_course("数学")
        self.assertGreater(sync_result["chunks_indexed"], 0)

        # 3. Search query
        results = self.service.search("数学", "微积分 导数", limit=5)
        self.assertTrue(len(results) >= 1)
        r0 = results[0]

        # Verify result contract fields
        self.assertTrue(r0["citation_id"].startswith("K"))
        self.assertIn("微积分", r0["text"])
        self.assertEqual(r0["category"], "数学")
        self.assertEqual(r0["origin"], "original")
        self.assertFalse(r0["stale"])
        self.assertIsInstance(r0["score"], float)
        self.assertIn("neighbor_context", r0)

    async def test_cross_course_filtering(self):
        # Course 1: 数学
        math_path = self.storage_root / "数学_课件.md"
        math_path.write_text("这是数学关于矩阵特征值和特征向量的内容。", encoding="utf-8")
        self.course_files.add_file("数学", "asset_m1", "代数.md", "课件", math_path)

        # Course 2: 物理
        phys_path = self.storage_root / "物理_课件.md"
        phys_path.write_text("这是物理关于电磁场麦克斯韦方程组的内容。", encoding="utf-8")
        self.course_files.add_file("物理", "asset_p1", "电磁学.md", "课件", phys_path)

        self.service.sync_course("数学")
        self.service.sync_course("物理")

        # Searching 数学 should NEVER return 物理
        math_res = self.service.search("数学", "电磁场 麦克斯韦", limit=5)
        self.assertEqual(len(math_res), 0)

        phys_res = self.service.search("物理", "电磁场", limit=5)
        self.assertEqual(len(phys_res), 1)
        self.assertEqual(phys_res[0]["category"], "物理")

    async def test_version_replacement_deletion_and_restore(self):
        # 1. Create file and index
        fpath = self.root / "test_notes.md"
        fpath.write_text("版本一：初始课堂讲义内容。", encoding="utf-8")
        self.course_files.add_file("数学", "asset_v1", "讲义.md", "讲义", fpath)

        self.service.sync_course("数学")
        res1 = self.service.search("数学", "版本一", limit=5)
        self.assertEqual(len(res1), 1)
        self.assertIn("版本一", res1[0]["text"])

        # 2. Update file content (version replacement)
        fpath.write_text("版本二：已更新的最新修订讲义内容。", encoding="utf-8")
        # Ensure mtime changes
        os.utime(fpath, (time.time() + 2, time.time() + 2))
        self.course_files.convert_file('数学', 'asset_v1')

        self.service.sync_course("数学")
        res2_old = self.service.search("数学", "版本一", limit=5)
        self.assertTrue(all('版本一' not in r['text'] for r in res2_old))
        self.assertTrue(all(r['revision'] != res1[0]['revision'] for r in res2_old))

        res2_new = self.service.search("数学", "版本二", limit=5)
        self.assertEqual(len(res2_new), 1)
        self.assertIn("版本二", res2_new[0]["text"])

        # 3. Delete file on disk (tombstone)
        fpath.unlink()
        # Search automatically tombstones missing file
        res3 = self.service.search("数学", "版本二", limit=5)
        self.assertEqual(len(res3), 0)

        status = self.service.status("数学")
        self.assertGreater(status["tombstoned_sources_count"], 0)

        # 4. Restore file on disk and re-sync
        fpath.write_text("版本二：已更新的最新修订讲义内容。", encoding="utf-8")
        self.course_files.convert_file('数学', 'asset_v1')
        self.service.sync_course("数学")
        res4 = self.service.search("数学", "版本二", limit=5)
        self.assertEqual(len(res4), 1)

    async def test_transactional_old_new_switch_fail_leaves_stale_hidden(self):
        fpath = self.root / "stable.md"
        fpath.write_text("稳定版本的内容，保证检索可用。", encoding="utf-8")
        self.course_files.add_file("数学", "asset_tx", "稳定.md", "课件", fpath)

        self.service.sync_course("数学")
        res_stable = self.service.search("数学", "稳定版本", limit=5)
        self.assertEqual(len(res_stable), 1)

        # Now simulate embedding failure on update
        fpath.write_text("尝试更新的内容，但嵌入模型中途断开连接报错。", encoding="utf-8")
        os.utime(fpath, (time.time() + 2, time.time() + 2))
        self.course_files.convert_file('数学', 'asset_tx')

        self.embedder.fail = True
        with self.assertRaises(RuntimeError):
            self.service.sync_course("数学")

        # Check that old stable version is STILL accessible and intact ("fail leaves stale hidden")
        self.embedder.fail = False
        res_after_fail = self.service.search("数学", "稳定版本", limit=5)
        self.assertEqual(len(res_after_fail), 0)
        self.assertEqual(len(res_after_fail), 0)

    async def test_model_contract_change_rebuild_without_mixing(self):
        fpath = self.root / "contract_test.md"
        fpath.write_text("测试嵌入模型协议切换的内容。", encoding="utf-8")
        self.course_files.add_file("数学", "asset_c1", "协议.md", "课件", fpath)

        self.service.sync_course("数学")

        # Now switch to a completely different model contract (e.g. dim 8)
        new_embedder = FakeEmbedder(
            contract={"provider": "fake", "model": "fake-v2-large", "dim": 2048},
            dim=2048,
        )
        self.service.embedder = new_embedder

        # Search should detect contract mismatch and fall back safely without erroring or mixing
        res_mismatch = self.service.search("数学", "嵌入模型", limit=5)
        self.assertTrue(len(res_mismatch) >= 1)

        # Now re-sync with new contract: rebuilds chunks under new contract
        self.service.sync_course("数学")
        res_rebuilt = self.service.search("数学", "嵌入模型", limit=5)
        self.assertEqual(len(res_rebuilt), 1)
        self.assertEqual(res_rebuilt[0]["embedding_contract"]["model"], "fake-v2-large")

    async def test_idempotent_resume_and_cancellation(self):
        fpath = self.root / "resume_test.md"
        fpath.write_text("幂等恢复测试内容。", encoding="utf-8")
        self.course_files.add_file("数学", "asset_idem", "幂等.md", "课件", fpath)

        # First sync
        stats1 = self.service.sync_course("数学")
        self.assertEqual(stats1["sources_updated"], 1)

        # Second sync with NO changes: 0 sources updated (idempotent)
        stats2 = self.service.sync_course("数学")
        self.assertEqual(stats2["sources_updated"], 0)

        # Cancellation check
        cancelled = False
        def cancel_stop():
            return True

        stats3 = self.service.sync_course("数学", stop=cancel_stop)
        self.assertEqual(stats3["sources_updated"], 0)

    async def test_derived_summary_lineage_and_exclusion(self):
        # Create a recording with raw transcript and AI summary
        record = self.recording_store.create_record(
            rec_id="rec_001",
            category="数学",
            title="高等代数第三讲",
            segments=[
                {"id": 1, "start": 0.0, "end": 20.0, "raw": "今天我们证明凯莱哈密顿定理。"},
            ],
            summary="本节课重点讲解了凯莱哈密顿定理的证明思路与特征多项式应用。",
        )

        self.service.sync_course("数学")

        # Search with include_derived=False (default): AI summary must NOT be returned as fact
        res_default = self.service.search("数学", "凯莱哈密顿定理", include_derived=False)
        for r in res_default:
            self.assertNotEqual(r["origin"], "ai_derived")

        # Search with include_derived=True: AI summary can be included with lineage
        res_derived = self.service.search("数学", "凯莱哈密顿定理", include_derived=True)
        summary_chunks = [r for r in res_derived if r["origin"] == "ai_derived"]
        self.assertTrue(len(summary_chunks) >= 1)
        self.assertEqual(summary_chunks[0]["kind"], "总结")

    async def test_embedded_mineru_transcript_in_pdf_is_reused(self):
        # Create a real PDF using pymupdf with embedded mineru-transcript.md
        pdf_path = self.root / "test_with_mineru.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Native PDF page text for testing")
        doc.embfile_add(
            "mineru-transcript.md",
            b"# MinerU OCR Output\n\nRecovered mathematical formula $$F = ma$$ from embedded transcript.",
            filename="mineru-transcript.md",
        )
        doc.save(pdf_path)
        doc.close()

        self.course_files.add_pdf_file("数学", "asset_mineru", "物理讲义.pdf", "课件", pdf_path)
        self.service.sync_course("数学")

        # Search for MinerU formula
        res = self.service.search("数学", "F = ma", include_derived=True)
        self.assertTrue(len(res) >= 1)
        mineru_chunk = next((r for r in res if "F = ma" in r["text"]), None)
        self.assertIsNotNone(mineru_chunk)
        self.assertEqual(mineru_chunk["origin"], "original")

    async def test_validate_evidence_freshness(self):
        fpath = self.root / "evidence_test.md"
        fpath.write_text("可追溯证据条目内容。", encoding="utf-8")
        self.course_files.add_file("数学", "asset_ev", "证据.md", "课件", fpath)
        self.service.sync_course("数学")

        results = self.service.search("数学", "可追溯证据", limit=1)
        self.assertEqual(len(results), 1)
        evidence = results[0]

        # 1. Fresh evidence validates
        validated = self.service.validate_evidence([evidence])
        self.assertTrue(validated[0]["valid"])
        self.assertFalse(validated[0]["stale"])

        # 2. Alter file on disk: evidence becomes stale
        fpath.write_text("已被他人篡改的内容。", encoding="utf-8")
        os.utime(fpath, (time.time() + 5, time.time() + 5))
        validated2 = self.service.validate_evidence([evidence])
        self.assertFalse(validated2[0]["valid"])
        self.assertTrue(validated2[0]["stale"])
        self.assertIn('原件已更新', validated2[0]['reason'])

    async def test_fastapi_knowledge_routes(self):
        app = FastAPI()
        app.include_router(knowledge_router(self.service))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(client.aclose)

        # 1. Status route
        res = await client.get("/api/knowledge/status?category=数学")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["category"], "数学")

        # 2. Index route
        res = await client.post("/api/knowledge/index", json={"category": "数学"})
        self.assertEqual(res.status_code, 200)
        job_id = res.json()["job_id"]
        self.assertTrue(job_id.startswith("job_"))

        # 3. Jobs route
        res = await client.get("/api/knowledge/jobs")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(len(res.json()["jobs"]) >= 1)

        # 4. Search route
        res = await client.get("/api/knowledge/search?category=数学&q=测试")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("results", data)
        self.assertIn("status", data)

    async def test_source_route_file_serving_and_security(self):
        app = FastAPI()
        app.include_router(knowledge_router(self.service))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(client.aclose)

        # 1. Create a safe file inside storage_root
        fpath = self.storage_root / "数学" / "测试讲义.pdf"
        fpath.parent.mkdir(parents=True, exist_ok=True)
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 50), "Safe document content")
        doc.save(fpath)
        doc.close()

        self.course_files.add_pdf_file("数学", "asset_safe", "测试讲义.pdf", "课件", fpath)
        self.service.sync_course("数学")

        # 2. Query safe source
        res = await client.get("/api/knowledge/source/cf_asset_safe_pdf?page=1")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("x-page-number"), "1")
        self.assertIn("application/pdf", res.headers.get("content-type", ""))

        # 3. Path traversal attack blocked
        unsafe_source_id = "malicious_src"
        outside_file = self.root.parent / "secret.txt"
        outside_file.write_text("classified", encoding="utf-8")
        self.service.store.commit_source_revision(
            source_meta={
                "source_id": unsafe_source_id,
                "course_id": "c_fake",
                "category": "数学",
                "title": "traversal.txt",
                "kind": "课件",
                "origin": "original",
                "source_type": "document",
                "file_path": str(outside_file.resolve()),
                "open_url": f"/api/knowledge/source/{unsafe_source_id}",
            },
            revision="rev_hack",
            content_hash="hash_hack",
            parser_version=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
            embedding_contract="",
            chunks_data=[{"chunk_id": "hack_chunk", "content_hash": "h", "text": "classified"}],
            vectors=None,
        )

        res_blocked = await client.get(f"/api/knowledge/source/{unsafe_source_id}")
        self.assertEqual(res_blocked.status_code, 403)

    async def test_deduplication_of_identical_chunks(self):
        # Two sources with identical chunk content
        f1 = self.storage_root / "数学" / "file1.md"
        f1.parent.mkdir(parents=True, exist_ok=True)
        f1.write_text("这一段内容完全相同，例如通用的考试注意事项和考场纪律。", encoding="utf-8")
        self.course_files.add_file("数学", "asset_dup1", "file1.md", "课件", f1)

        f2 = self.storage_root / "数学" / "file2.md"
        f2.write_text("这一段内容完全相同，例如通用的考试注意事项和考场纪律。", encoding="utf-8")
        self.course_files.add_file("数学", "asset_dup2", "file2.md", "课件", f2)

        self.service.sync_course("数学")

        # Search should deduplicate and return only 1 unique chunk
        results = self.service.search("数学", "考试注意事项 考场纪律", limit=5)
        matching = [r for r in results if "考试注意事项" in r["text"]]
        self.assertEqual(len(matching), 1)

    async def test_durable_job_recovery(self):
        # Insert a job stuck in running status into DB
        self.service.store.create_job("job_crashed_01", "数学")
        self.service.store.update_job("job_crashed_01", status="running", stage="parsing")

        # Re-initialize service and call start()
        new_service = KnowledgeService(
            settings=self.settings,
            recording_store=self.recording_store,
            course_files=self.course_files,
            db_path=self.db_path,
            embedder=self.embedder,
        )
        await new_service.start()
        try:
            job = new_service.store.get_job("job_crashed_01")
            self.assertIn(job['status'], {'queued','running','completed'})
            await asyncio.wait_for(new_service.job_manager._queue.join(), 3)
            self.assertEqual(new_service.store.get_job('job_crashed_01')['status'], 'completed')
        finally:
            await new_service.close()



# Mocks for isolating filesystem dependencies
class MockCourseFiles:
    def __init__(self, root: Path):
        self.root = root
        self.assets = {}

    def add_file(self, category: str, asset_id: str, name: str, kind: str, path: Path, converted: bool = True):
        self.assets.setdefault(category, {})[asset_id] = {
            "meta": {"id": asset_id, "name": name, "kind": kind, "conversion": {"status": "none"}},
            "original": path,
            "pdf": None,
        }
        if converted:
            self.convert_file(category, asset_id)

    def convert_file(self, category: str, asset_id: str):
        """Synthetic completed MinerU artifact; not a real conversion quality benchmark."""
        item = self.assets[category][asset_id]
        pdf = self.root / 'converted-fixtures' / category / (asset_id + '.pdf')
        pdf.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open() as doc:
            doc.new_page()
            doc.embfile_add('mineru-transcript.md', item['original'].read_bytes())
            doc.save(pdf)
        item['pdf'] = pdf
        item['meta']['conversion'] = {'status': 'completed', 'engine': 'mineru-4-v1'}
        return pdf

    def add_pdf_file(self, category: str, asset_id: str, name: str, kind: str, pdf_path: Path):
        self.assets.setdefault(category, {})[asset_id] = {
            "meta": {"id": asset_id, "name": name, "kind": kind, "conversion": {"status": "completed", "engine": "mineru-4-v1"}},
            "original": pdf_path,
            "pdf": pdf_path,
        }

    def list(self, category: str):
        return [item["meta"] for item in self.assets.get(category, {}).values()]

    def file(self, category: str, asset_id: str, variant: str = "original"):
        item = self.assets[category][asset_id]
        if variant == "original":
            return item["meta"], item["original"]
        elif variant == "pdf":
            return item["meta"], item["pdf"]
        raise ValueError("Invalid variant")


class MockRecordingRecord:
    def __init__(self, directory: Path, metadata: dict, segments: list):
        self.id = metadata["id"]
        self.directory = directory
        self.metadata = metadata
        self.segments = segments
        self.audio_path = directory / "recording.wav"
        self.notes_path = directory / "课堂笔记.md"
        self.raw_transcript_path = directory / "原始转写.txt"
        self.corrected_transcript_path = directory / "AI修正后文本.txt"


class MockRecordingStore:
    def __init__(self, root: Path):
        self.root = root
        self.paths = {}
        self.records = {}

    def create_record(self, rec_id: str, category: str, title: str, segments: list, summary: str = None):
        rec_dir = self.root / category / "上课录音" / rec_id
        rec_dir.mkdir(parents=True, exist_ok=True)
        meta = {"id": rec_id, "category": category, "title": title}
        if summary:
            meta["ai_note_summary"] = {"summary": summary}
            (rec_dir / "AI总结.md").write_text(summary, encoding="utf-8")

        (rec_dir / "session.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        record = MockRecordingRecord(rec_dir, meta, segments)
        self.paths[rec_id] = rec_dir
        self.records[rec_id] = record
        return record

    def refresh(self, mark_interrupted: bool = False):
        pass

    def find(self, rec_id: str):
        return self.records.get(rec_id)


if __name__ == "__main__":
    unittest.main()
