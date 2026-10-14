"""Course files and recordings ingestion: identity, extraction, and lineage-aware typed sources."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from ..lazy import lazy_module
from .chunking import (
    CHUNKER_VERSION,
    PARSER_VERSION,
    Chunk,
    chunk_markdown_text,
    chunk_pdf_document,
    chunk_transcript_segments,
)
from .store import KnowledgeStore, make_citation_id

SIDECAR_FILENAME = ".qwenasr-course-id.json"


def terminology_text(settings, category):
    profile=(getattr(settings,'category_profiles',{}) or {}).get(category,{})
    terms=profile.get('terminology') or []
    return '\n\n'.join('术语及别名：'+str(term) for term in terms if str(term).strip())


def terminology_stat(settings, category):
    body=terminology_text(settings,category)
    return 0.0,len(body.encode()),compute_content_hash(body)


def ensure_course_id(storage_root: Path, category: str, store: KnowledgeStore) -> str:
    """Ensure a stable root-scoped course ID, persisted in a course folder sidecar."""
    cat = str(category or "").strip()
    if not cat or ".." in cat or "/" in cat or "\\" in cat or "\x00" in cat:
        raise ValueError(f"非法课程类别名称: {category}")

    storage_root = storage_root.resolve()
    course_folder = (storage_root / cat).resolve()
    if not course_folder.is_relative_to(storage_root):
        raise ValueError(f"课程目录越界: {category}")

    sidecar_path = course_folder / SIDECAR_FILENAME

    # 1. Check existing sidecar in folder
    if sidecar_path.is_file():
        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
            cid = data.get("course_id")
            if cid:
                store.upsert_course(cid, cat, str(course_folder))
                return cid
        except (OSError, ValueError):
            pass

    # 2. Check DB
    existing = store.get_course(cat)
    if existing:
        cid = existing["course_id"]
    else:
        cid = f"c_{uuid.uuid4().hex[:12]}"

    # Write sidecar if folder exists
    try:
        if not course_folder.exists():
            course_folder.mkdir(parents=True, exist_ok=True)
        tmp = sidecar_path.with_name(f"{sidecar_path.name}.tmp.{uuid.uuid4().hex[:6]}")
        tmp.write_text(json.dumps({"course_id": cid, "category": cat}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(sidecar_path)
    except OSError:
        pass

    store.upsert_course(cid, cat, str(course_folder))
    return cid


def compute_file_stat_hash(path: Path) -> tuple[float, int, str]:
    """Return (mtime, size, stat_hash) for freshness proof."""
    try:
        stat = path.stat()
        mtime = stat.st_mtime
        size = stat.st_size
        sig = f"{path.resolve()}:{mtime}:{size}".encode("utf-8")
        stat_hash = hashlib.sha256(sig).hexdigest()
        return mtime, size, stat_hash
    except OSError:
        return 0.0, 0, ""


def compute_recording_stat_hash(rec_dir: Path) -> tuple[float, int, str]:
    """Compound stat hash across transcript.json, segments.json, session.json, and AI修正后文本.txt."""
    parts = []
    max_mtime = 0.0
    total_size = 0
    for fname in ("transcript.json", "segments.json", "session.json", "AI修正后文本.txt", "AI总结.md", "classroom-notes.json"):
        p = rec_dir / fname
        if p.is_file():
            try:
                st = p.stat()
                max_mtime = max(max_mtime, st.st_mtime)
                total_size += st.st_size
                parts.append(f"{fname}:{st.st_mtime}:{st.st_size}")
            except OSError:
                pass
    if not parts:
        return compute_file_stat_hash(rec_dir)
    sig = ";".join(parts).encode("utf-8")
    return max_mtime, total_size, hashlib.sha256(sig).hexdigest()


def compute_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CourseIngestor:
    def __init__(
        self,
        settings: Any,
        recording_store: Any,
        course_files: Any,
        store: KnowledgeStore,
    ) -> None:
        self.settings = settings
        self.recording_store = recording_store
        self.course_files = course_files
        self.store = store

    def ingest_course(
        self,
        category: str,
        course_id: str,
        embedder: Any | None,
        stop: Callable[[], bool] | None = None,
        progress_cb: Callable[[str, float], None] | None = None,
    ) -> dict[str, Any]:
        """Ingest all course files and recordings for category incrementally."""
        storage_root = Path(self.settings.storage_root)
        active_contract = embedder.contract if embedder and hasattr(embedder, "contract") else {}
        contract_str = json.dumps(active_contract, sort_keys=True) if active_contract else ""

        stats = {
            "sources_checked": 0,
            "sources_updated": 0,
            "chunks_indexed": 0,
            "tombstoned": 0,
        }

        # 1. Discover Course Files
        discovered_file_sources = self._discover_course_files(category, course_id)
        # 2. Discover Recordings
        discovered_recording_sources = self._discover_recordings(category, course_id)

        all_sources = discovered_file_sources + discovered_recording_sources
        glossary=terminology_text(self.settings,category)
        if glossary:
            all_sources.append({'source_id':f'terms_{course_id}','course_id':course_id,'category':category,
                'lesson_id':None,'title':category+' · 专业术语与别名','kind':'专业术语','origin':'human',
                'source_type':'course_terminology','file_path':None,'lineage':{'type':'course_profile'},
                'open_url':'/?'+urlencode({'category':category,'settings':'terminology'}),
                'stat_calculator':lambda:terminology_stat(self.settings,category),
                'loader':lambda body=glossary:(body,chunk_markdown_text(body))})
        stats["sources_checked"] = len(all_sources)

        # Tombstone missing sources: check existing DB sources for category
        existing_sources = self.store.list_sources(category, active_only=True)
        discovered_ids = {s["source_id"] for s in all_sources}

        for ex in existing_sources:
            if ex["source_id"] not in discovered_ids:
                self.store.tombstone_source(ex["source_id"])
                stats["tombstoned"] += 1

        total = len(all_sources)
        for idx, src_spec in enumerate(all_sources):
            if stop and stop():
                break

            if progress_cb:
                progress_cb(f"正在处理: {src_spec['title']}", idx / max(1, total))

            updated, chunk_count = self._sync_single_source(
                src_spec, active_contract, contract_str, embedder, stop
            )
            if updated:
                stats["sources_updated"] += 1
                stats["chunks_indexed"] += chunk_count

        return stats

    def _discover_course_files(self, category: str, course_id: str) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        if not self.course_files:
            return specs

        try:
            items = self.course_files.list(category)
        except Exception:
            return specs

        for item in items:
            asset_id = item["id"]
            title = item.get("name", "未命名文件")
            kind = item.get("kind", "参考资料")
            found_any = False

            # Check original file
            try:
                _, orig_path = self.course_files.file(category, asset_id, variant="original")
                if orig_path.is_file():
                    found_any = True
                    specs.append({
                        "source_id": f"cf_{asset_id}_orig",
                        "course_id": course_id,
                        "category": category,
                        "asset_id": asset_id,
                        "lesson_id": None,
                        "title": title,
                        "kind": kind,
                        "origin": "original",
                        "source_type": "course_file_original",
                        "file_path": str(orig_path.resolve()),
                        "open_url": f"/api/knowledge/source/cf_{asset_id}_orig",
                        "loader": lambda p=orig_path: self._load_file_content(p),
                    })
            except Exception:
                pass

            # Check converted PDF
            try:
                conversion = item.get("conversion", {})
                if conversion.get("status") == "completed":
                    _, pdf_path = self.course_files.file(category, asset_id, variant="pdf")
                    if pdf_path.is_file():
                        found_any = True
                        specs.append({
                            "source_id": f"cf_{asset_id}_pdf",
                            "course_id": course_id,
                            "category": category,
                            "asset_id": asset_id,
                            "lesson_id": None,
                            "title": f"{title} (PDF)",
                            "kind": kind,
                            "origin": "original",
                            "source_type": "course_file_pdf",
                            "file_path": str(pdf_path.resolve()),
                            "open_url": f"/api/knowledge/source/cf_{asset_id}_pdf",
                            "loader": lambda p=pdf_path: self._load_pdf_content(p),
                        })

                        # Check for embedded mineru-transcript.md
                        # Note: MinerU parser transcript is original material extraction (provenance: parser, NOT AI explanation)
                        mineru_data = self._check_embedded_mineru(pdf_path)
                        if mineru_data:
                            specs.append({
                                "source_id": f"cf_{asset_id}_mineru",
                                "course_id": course_id,
                                "category": category,
                                "asset_id": asset_id,
                                "lesson_id": None,
                                "title": f"{title} (MinerU转写)",
                                "kind": kind,
                                "origin": "original",
                                "source_type": "course_file_transcript",
                                "file_path": str(pdf_path.resolve()),
                                "open_url": f"/api/knowledge/source/cf_{asset_id}_pdf",
                                "lineage": {"asset_id": asset_id, "parent_source_id": f"cf_{asset_id}_pdf", "parser": "mineru-transcript.md"},
                                "loader": lambda md=mineru_data: (md, chunk_markdown_text(md)),
                            })
                elif conversion.get("status") == "failed":
                    self.store.record_source_failure(
                        source_id=f"cf_{asset_id}_failed",
                        course_id=course_id,
                        category=category,
                        title=title,
                        kind=kind,
                        file_path=None,
                        error_msg=conversion.get("error", "文件转换失败"),
                        asset_id=asset_id,
                    )
            except Exception as exc:
                if not found_any:
                    self.store.record_source_failure(
                        source_id=f"cf_{asset_id}_failed",
                        course_id=course_id,
                        category=category,
                        title=title,
                        kind=kind,
                        file_path=None,
                        error_msg=f"文件加载失败: {exc}",
                        asset_id=asset_id,
                    )

        by_asset={spec.get('asset_id'):spec for spec in specs if spec['source_type']=='course_file_original'}
        for spec in specs:
            if spec['source_type'] not in {'course_file_pdf','course_file_transcript'}:
                continue
            original=by_asset.get(spec.get('asset_id'))
            if original:
                original_path=Path(original['file_path'])
                parent_stat=compute_file_stat_hash(original_path)[2]
                previous=self.store.get_source(spec['source_id'])
                old_lineage=json.loads(previous.get('lineage') or '{}') if previous else {}
                converted_stat=compute_file_stat_hash(Path(spec['file_path']))[2]
                # Reindexing must not bless an unchanged old conversion with the new parent's identity.
                if (previous and old_lineage.get('parent_stat_hash') and old_lineage['parent_stat_hash']!=parent_stat
                        and previous.get('stat_hash')==converted_stat):
                    self.store.mark_source_stale_and_hide_chunks(spec['source_id'],'原件已更新，转换文件尚未更新')
                    spec['_skip_stale_conversion']=True
                    continue
                item=next((x for x in items if x['id']==spec.get('asset_id')),{})
                expected=(item.get('conversion') or {}).get('source_sha256')
                if expected:
                    with original_path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
                    if expected!=actual:
                        if previous:self.store.mark_source_stale_and_hide_chunks(spec['source_id'],'转换任务的源文件版本不匹配')
                        spec['_skip_stale_conversion']=True
                        continue
                spec['lineage']={**(spec.get('lineage') or {}),'parent_source_id':original['source_id'],
                                 'parent_file_path':str(original_path),'parent_stat_hash':parent_stat}
        return [spec for spec in specs if not spec.get('_skip_stale_conversion')]

    def _discover_recordings(self, category: str, course_id: str) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        if not self.recording_store:
            return specs

        # Refresh store paths
        try:
            self.recording_store.refresh(mark_interrupted=False)
        except Exception:
            pass

        for rec_id, rec_dir in list(getattr(self.recording_store, "paths", {}).items()):
            try:
                record = self.recording_store.find(rec_id)
                if not record:
                    continue
                if record.metadata.get("category") != category:
                    continue

                rec_title = record.metadata.get("title") or "课堂录音"

                # Check segments from record.segments or transcript.json or segments.json
                segments = list(getattr(record, "segments", []) or [])
                if not segments and hasattr(record, "directory"):
                    for seg_file in ("transcript.json", "segments.json"):
                        sp = record.directory / seg_file
                        if sp.is_file():
                            try:
                                raw = json.loads(sp.read_text(encoding="utf-8"))
                                if isinstance(raw, list) and raw:
                                    segments = raw
                                    break
                            except Exception:
                                pass

                # 1. Raw transcript
                if segments:
                    raw_path = record.directory / 'transcript.json'
                    if not raw_path.exists():
                        raw_path = record.directory / 'session.json'
                    if not raw_path.exists():
                        raw_path = record.directory
                    specs.append({
                        "source_id": f"rec_{rec_id}_raw",
                        "course_id": course_id,
                        "category": category,
                        "lesson_id": rec_id,
                        "title": f"{rec_title} - 原始录音转写",
                        "kind": "录音",
                        "origin": "original",
                        "source_type": "recording_raw",
                        "file_path": str(raw_path.resolve()),
                        "stat_calculator": lambda p=raw_path: compute_file_stat_hash(p),
                        "open_url": f"/?recording={rec_id}",
                        "loader": lambda r=record, s=segments: self._load_recording_raw(r, s),
                    })

                # 2. Refined transcript
                refined_blocks = self._extract_refined_blocks(record)
                if refined_blocks:
                    ref_path = record.directory / "AI修正后文本.txt"
                    if not ref_path.exists():
                        ref_path = record.directory / "session.json" if (record.directory / "session.json").exists() else record.directory
                    specs.append({
                        "source_id": f"rec_{rec_id}_refined",
                        "course_id": course_id,
                        "category": category,
                        "lesson_id": rec_id,
                        "title": f"{rec_title} - 精修文本",
                        "kind": "课堂笔记",
                        "origin": "ai_derived",
                        "source_type": "recording_refined",
                        "file_path": str(ref_path.resolve()),
                        "stat_calculator": lambda d=record.directory: compute_recording_stat_hash(d),
                        "open_url": f"/?recording={rec_id}",
                        "lineage": {"parent_source_id": f"rec_{rec_id}_raw", "type": "ai_refinement"},
                        "loader": lambda b=refined_blocks: self._load_recording_refined(b),
                    })

                # 3. Human scribbles / classroom notes
                notes_path = record.directory / "classroom-notes.json"
                if not notes_path.exists() and record.notes_path.exists():
                    notes_path = record.notes_path
                if notes_path.is_file() and self._load_scribbles(notes_path)[0].strip():
                    specs.append({
                        "source_id": f"rec_{rec_id}_scribbles",
                        "course_id": course_id,
                        "category": category,
                        "lesson_id": rec_id,
                        "title": f"{rec_title} - 课堂速记",
                        "kind": "速记",
                        "origin": "human",
                        "source_type": "recording_scribble",
                        "file_path": str(notes_path.resolve()),
                        "open_url": f"/?recording={rec_id}",
                        "loader": lambda p=notes_path: self._load_scribbles(p),
                    })
                    if notes_path.suffix.lower()=='.json' and self._load_scribbles(notes_path,derived=True)[0].strip():
                        specs.append({'source_id':f'rec_{rec_id}_enhanced_notes','course_id':course_id,'category':category,'lesson_id':rec_id,'title':f'{rec_title} - 已接受的 AI 增强笔记','kind':'增强笔记','origin':'ai_derived','source_type':'recording_enhanced_note','file_path':str(notes_path.resolve()),'open_url':f'/?chat={rec_id}','lineage':{'parent_source_id':f'rec_{rec_id}_raw','type':'accepted_ai_note'},'loader':lambda p=notes_path:self._load_scribbles(p,derived=True)})

                # 4. AI Summary
                summary_text = self._extract_summary_text(record)
                if summary_text:
                    sum_path = record.directory / "AI总结.md"
                    if not sum_path.exists():
                        sum_path = record.directory / "session.json" if (record.directory / "session.json").exists() else record.directory
                    specs.append({
                        "source_id": f"rec_{rec_id}_summary",
                        "course_id": course_id,
                        "category": category,
                        "lesson_id": rec_id,
                        "title": f"{rec_title} - AI总结",
                        "kind": "总结",
                        "origin": "ai_derived",
                        "source_type": "recording_summary",
                        "file_path": str(sum_path.resolve()),
                        "open_url": f"/?recording={rec_id}",
                        "lineage": {"parent_source_id": f"rec_{rec_id}_raw", "type": "ai_summary"},
                        "loader": lambda s=summary_text: (s, chunk_markdown_text(s)),
                    })

            except Exception:
                continue

        return specs

    def _sync_single_source(
        self,
        src_spec: dict[str, Any],
        active_contract: dict[str, Any] | str,
        contract_str: str,
        embedder: Any | None,
        stop: Callable[[], bool] | None,
    ) -> tuple[bool, int]:
        source_id = src_spec["source_id"]
        file_path = Path(src_spec["file_path"]) if src_spec.get("file_path") else None

        if "stat_calculator" in src_spec:
            mtime, size, stat_hash = src_spec["stat_calculator"]()
        elif file_path and file_path.exists():
            mtime, size, stat_hash = compute_file_stat_hash(file_path)
        else:
            mtime, size, stat_hash = 0.0, 0, ""

        # Check existing source
        existing = self.store.get_source(source_id)
        if existing and existing["status"] == "active":
            # If stat_hash is unchanged
            if stat_hash and existing.get("stat_hash") == stat_hash:
                needs_vecs = existing.get("needs_vectors", 0)
                # If needs vectors and we can embed, proceed to embed!
                if not needs_vecs or embedder is None or '超过模型输入预算' in str(existing.get('error') or ''):
                    # Check revision's contract
                    with self.store._connect() as conn:
                        rev_row = conn.execute(
                            "SELECT embedding_contract,parser_version,chunker_version FROM source_revisions WHERE source_id = ? AND revision = ? AND is_active = 1",
                            (source_id, existing["active_revision"]),
                        ).fetchone()
                        if rev_row and rev_row['embedding_contract'] == contract_str and rev_row['parser_version']==PARSER_VERSION and rev_row['chunker_version']==CHUNKER_VERSION:
                            return False, 0

        # Load raw content and chunks
        try:
            full_text, chunks = src_spec["loader"]()
        except Exception as exc:
            self.store.record_source_failure(
                source_id=source_id,
                course_id=src_spec["course_id"],
                category=src_spec["category"],
                title=src_spec["title"],
                kind=src_spec["kind"],
                file_path=src_spec.get("file_path"),
                error_msg=f"解析失败: {exc}",
                asset_id=src_spec.get("asset_id"),
            )
            return False, 0

        if not chunks:
            self.store.record_source_failure(source_id=source_id, course_id=src_spec['course_id'], category=src_spec['category'], title=src_spec['title'], kind=src_spec['kind'], file_path=src_spec.get('file_path'), error_msg='未提取到可用文本，请核对扫描件或既有转换结果', asset_id=src_spec.get('asset_id'))
            return False, 0

        if stop and stop():
            return False, 0

        content_hash = compute_content_hash(full_text)
        file_digest = ''
        if file_path and file_path.is_file():
            with file_path.open('rb') as stream:
                file_digest=hashlib.file_digest(stream,'sha256').hexdigest()
        revision = compute_content_hash(json.dumps([content_hash,file_digest,PARSER_VERSION,CHUNKER_VERSION]))

        # Prepare chunk data with specific open_urls
        chunks_data = []
        texts_to_embed = []
        for c in chunks:
            c_hash = compute_content_hash(c.text)
            c_id = f"{source_id}_{revision}_{c.chunk_index}"
            chunk_open_url = src_spec.get("open_url", "")
            if c.page is not None and "source" in chunk_open_url:
                chunk_open_url = f"{src_spec['open_url']}#page={c.page}"
            elif c.start is not None and "session=" in chunk_open_url:
                chunk_open_url = f"{src_spec['open_url']}&t={int(c.start)}"

            chunk_dict = {
                "chunk_id": c_id,
                "citation_id": make_citation_id(c_id),
                "content_hash": c_hash,
                "text": c.text,
                "heading": c.heading,
                "page": c.page,
                "start": c.start,
                "end": c.end,
                "segment_ids": c.segment_ids,
                "open_url": chunk_open_url,
            }
            chunks_data.append(chunk_dict)
            texts_to_embed.append(c.text)

        # Determine if embedder is prepared/usable
        vectors: list[list[float]] | None = None
        vector_warning = None
        can_embed = False
        if embedder and hasattr(embedder, "encode_documents") and texts_to_embed:
            if hasattr(embedder, "config"):
                from .settings import prepared
                can_embed = prepared(embedder.config)
            else:
                can_embed = True

        if can_embed:
            batch_size = 16
            if hasattr(embedder, "config") and isinstance(embedder.config, dict):
                batch_size = max(1, embedder.config.get("batch_size", 16))
            cached = self.store.cached_vectors([c['content_hash'] for c in chunks_data], active_contract)
            vectors = [cached.get(c['content_hash']) for c in chunks_data]
            missing=[i for i,value in enumerate(vectors) if value is None]
            try:
                for offset in range(0,len(missing),batch_size):
                    if stop and stop():
                        return False, 0
                    indexes=missing[offset:offset+batch_size]
                    try:
                        batch_vecs = embedder.encode_documents([texts_to_embed[i] for i in indexes])
                    except Exception as batch_error:
                        if '超过模型输入预算' not in str(batch_error):
                            raise
                        batch_vecs=[]
                        for i in indexes:
                            try:
                                batch_vecs.append(embedder.encode_documents([texts_to_embed[i]])[0])
                            except Exception as item_error:
                                if '超过模型输入预算' not in str(item_error):
                                    raise
                                batch_vecs.append(None)
                                vector_warning='部分完整结构块超过模型输入预算，保留全文供词法查询；其余分块已向量化'
                    for i,vector in zip(indexes,batch_vecs):
                        vectors[i]=vector
            except Exception as exc:
                if "尚未准备" in str(exc) or "not prepared" in str(exc).lower() or "超过模型输入预算" in str(exc):
                    vectors = None
                else:
                    raise RuntimeError(f"Embedding failure for source {source_id}: {exc}") from exc

        if stop and stop():
            return False, 0

        # Snapshot source hash and recheck after embedding before commit (abort if modified)
        if "stat_calculator" in src_spec:
            cur_mtime, cur_size, cur_stat_hash = src_spec["stat_calculator"]()
        elif file_path and file_path.exists():
            cur_mtime, cur_size, cur_stat_hash = compute_file_stat_hash(file_path)
        else:
            cur_mtime, cur_size, cur_stat_hash = 0.0, 0, ""

        if stat_hash and cur_stat_hash != stat_hash:
            # Underlying source modified during embedding! Abort commit.
            return False, 0

        lineage=src_spec.get('lineage') or {}
        if lineage.get('parent_file_path'):
            if compute_file_stat_hash(Path(lineage['parent_file_path']))[2] != lineage.get('parent_stat_hash'):
                return False,0
        # Transactional commit
        meta_to_save = {
            "source_id": source_id,
            "course_id": src_spec["course_id"],
            "category": src_spec["category"],
            "asset_id": src_spec.get("asset_id"),
            "lesson_id": src_spec.get("lesson_id"),
            "title": src_spec["title"],
            "kind": src_spec["kind"],
            "origin": src_spec["origin"],
            "source_type": src_spec["source_type"],
            "file_path": src_spec.get("file_path"),
            "file_mtime": mtime,
            "file_size": size,
            "stat_hash": stat_hash,
            "open_url": src_spec["open_url"],
            "lineage": src_spec.get("lineage"),
            "error": vector_warning,
        }

        self.store.commit_source_revision(
            source_meta=meta_to_save,
            revision=revision,
            content_hash=content_hash,
            parser_version=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
            embedding_contract=active_contract,
            chunks_data=chunks_data,
            vectors=vectors,
        )

        return True, len(chunks_data)

    def _check_embedded_mineru(self, pdf_path: Path) -> str | None:
        """Extract mineru-transcript.md embedded file from PDF if present."""
        try:
            import pymupdf as fitz
            doc = fitz.open(pdf_path)
            try:
                if "mineru-transcript.md" in doc.embfile_names():
                    raw = doc.embfile_get("mineru-transcript.md")
                    return raw.decode("utf-8", errors="replace")
            finally:
                doc.close()
        except Exception:
            pass
        return None

    def _load_pdf_content(self, pdf_path: Path) -> tuple[str, list[Chunk]]:
        import pymupdf as fitz
        doc = fitz.open(pdf_path)
        pages_text: list[tuple[int, str]] = []
        full_parts = []
        try:
            for page_idx in range(len(doc)):
                txt = doc[page_idx].get_text()
                pages_text.append((page_idx + 1, txt))
                full_parts.append(txt)
        finally:
            doc.close()

        full_text = "\n\n".join(full_parts)
        chunks = chunk_pdf_document(pages_text)
        return full_text, chunks

    def _load_file_content(self, path: Path) -> tuple[str, list[Chunk]]:
        suffix = path.suffix.lower()
        if suffix == '.pdf':
            return self._load_pdf_content(path)
        if suffix in {'.docx','.pptx','.xlsx'}:
            import zipfile
            import xml.etree.ElementTree as ET
            with zipfile.ZipFile(path) as archive:
                if suffix=='.pptx':
                    names=sorted((n for n in archive.namelist() if re.fullmatch(r'ppt/slides/slide[0-9]+\.xml',n)),key=lambda x:int(re.search(r'slide([0-9]+)',x).group(1)))
                    pages=[]
                    for index,name in enumerate(names,1):
                        node=ET.fromstring(archive.read(name))
                        text='\n'.join(' '.join(t.text or '' for t in par.iter() if t.tag.rsplit('}',1)[-1]=='t') for par in node.iter() if par.tag.rsplit('}',1)[-1]=='p')
                        pages.append((index,text))
                    return '\n\n'.join(text for _,text in pages),chunk_pdf_document(pages)
                if suffix=='.docx':
                    node=ET.fromstring(archive.read('word/document.xml'))
                    paragraphs=[]
                    for par in node.iter():
                        if par.tag.rsplit('}',1)[-1]=='p':
                            paragraphs.append(''.join(t.text or '' for t in par.iter() if t.tag.rsplit('}',1)[-1] in {'t','instrText'}))
                    text='\n\n'.join(paragraphs)
                    return text,chunk_markdown_text(text)
                strings=[]
                if 'xl/sharedStrings.xml' in archive.namelist():
                    shared=ET.fromstring(archive.read('xl/sharedStrings.xml'))
                    strings=[''.join(t.text or '' for t in item.iter() if t.tag.rsplit('}',1)[-1]=='t') for item in shared]
                sections=[]
                for name in sorted(n for n in archive.namelist() if re.fullmatch(r'xl/worksheets/sheet[0-9]+\.xml',n)):
                    node=ET.fromstring(archive.read(name));rows=[]
                    for row in node.iter():
                        if row.tag.rsplit('}',1)[-1]!='row':continue
                        cells=[]
                        for cell in row:
                            value=''.join(n.text or '' for n in cell.iter() if n.tag.rsplit('}',1)[-1] in {'v','t'})
                            if cell.get('t')=='s' and value.isdigit():value=strings[int(value)]
                            cells.append(value)
                        rows.append('| '+' | '.join(cells)+' |')
                    sections.append('## '+Path(name).stem+'\n'+'\n'.join(rows))
                text='\n\n'.join(sections)
                return text,chunk_markdown_text(text)
        if suffix not in {'.txt','.md','.csv','.tsv','.html','.htm','.tex','.json','.srt','.vtt'}:
            raise ValueError('此格式尚无文本解析器，请先使用现有资料转换功能生成可检索 PDF')
        raw = path.read_bytes()
        text = ''
        for encoding in ('utf-8-sig','gb18030','utf-16'):
            try:
                text=raw.decode(encoding)
                break
            except UnicodeError:
                continue
        if not text.strip():
            raise ValueError('此资料没有可用文本，需要在资料页确认解析方式')
        return text,chunk_markdown_text(text)

    def _load_recording_raw(self, record: Any, segments: list[dict[str, Any]] | None = None) -> tuple[str, list[Chunk]]:
        segs = segments if segments is not None else getattr(record, "segments", [])
        if not segs and hasattr(record, "directory"):
            for seg_file in ("transcript.json", "segments.json"):
                sp = record.directory / seg_file
                if sp.is_file():
                    try:
                        raw = json.loads(sp.read_text(encoding="utf-8"))
                        if isinstance(raw, list) and raw:
                            segs = raw
                            break
                    except Exception:
                        pass
        chunks = chunk_transcript_segments(segs, text_key="raw")
        full_text = "\n".join(c.text for c in chunks)
        return full_text, chunks

    def _extract_refined_blocks(self, record: Any) -> list[dict[str, Any]]:
        try:
            from ..note_content import transcript_blocks
            blocks = transcript_blocks(record)
            if blocks and any(b.get("refined") for b in blocks):
                return [b for b in blocks if b.get("refined")]
        except Exception:
            pass

        refined_file = record.directory / "AI修正后文本.txt"
        if refined_file.is_file():
            try:
                content = refined_file.read_text(encoding="utf-8").strip()
                if content:
                    return [{"text": content, "refined": True, "start": 0.0, "end": 0.0, "id": 1}]
            except Exception:
                pass
        return []

    def _load_recording_refined(self, blocks: list[dict[str, Any]]) -> tuple[str, list[Chunk]]:
        chunks = chunk_transcript_segments(blocks, text_key="text")
        full_text = "\n".join(c.text for c in chunks)
        return full_text, chunks

    def _load_scribbles(self, path: Path, derived=False) -> tuple[str, list[Chunk]]:
        if path.suffix.lower() != '.json':
            text=path.read_text(encoding='utf-8')
            return text,chunk_markdown_text(text)
        data=json.loads(path.read_text(encoding='utf-8'))
        notes=data if isinstance(data,list) else data.get('notes',[])
        chunks=[];texts=[]
        for note in notes:
            if derived:
                text=str(note.get('content') or '') if note.get('evidence') else ''
            else:
                text=str(note.get('raw') or note.get('text') or '')
                history=note.get('history') or []
                manual=note.get('manual_edit') if 'manual_edit' in note else bool(history and history[-1].get('source')=='manual')
                if manual and not note.get('evidence'):
                    text+='\n'+str(note.get('content') or '')
            if not text.strip():continue
            texts.append(text)
            ids=list(dict.fromkeys(i for e in note.get('evidence',[]) if e.get('type')=='classroom' for i in e.get('segment_ids',[])))
            for chunk in chunk_markdown_text(text):
                chunk.start=note.get('start');chunk.end=note.get('end')
                chunk.segment_ids=ids;chunk.heading='速记 '+str(note.get('id',''));chunk.chunk_index=len(chunks)
                chunks.append(chunk)
        return '\n\n'.join(texts),chunks

    def _extract_summary_text(self, record: Any) -> str | None:
        summary_path = record.directory / "AI总结.md"
        if summary_path.is_file():
            try:
                return summary_path.read_text(encoding="utf-8")
            except OSError:
                pass

        summary_meta = record.metadata.get("ai_note_summary")
        if isinstance(summary_meta, dict):
            return summary_meta.get("summary") or summary_meta.get("content")
        return None
