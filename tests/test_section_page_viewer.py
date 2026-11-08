from tests import isolation  # noqa: F401

import base64
import hashlib
import json
import re
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from qwen_asr_app import section_pages as sp
from qwen_asr_app.section_page_routes import section_page_router

ROOT = Path(__file__).resolve().parents[1]
RECORDING = "rec12345"
SOURCE = "a" * 32
SHA = "b" * 64
MARKDOWN = "# 标题\n\n## 端口\n正文\n"


def document():
    return {
        "schema_version": 1, "mapping_version": sp.MAPPING_VERSION, "recording_id": RECORDING,
        "note_body_sha": sp.sha(MARKDOWN), "resources": [{
            "source_id": SOURCE, "name": "课件.pdf", "viewer_name": "ai.pdf", "viewer_sha256": SHA,
        }], "sections": [{
            "index": 1, "heading": "端口", "section_id": "s", "status": "verified", "targets": [{
                "source_id": SOURCE, "source_name": "课件.pdf", "page_start": 2, "page_end": 4,
                "viewer_page_start": 3, "viewer_page_end": 5,
            }],
        }],
    }


class Store:
    def find(self, ident):
        if ident != RECORDING:
            return None
        return SimpleNamespace(id=ident, metadata={"category": "课", "ai_note_summary": {"summary": MARKDOWN}})


def client():
    app = FastAPI()
    app.include_router(section_page_router(Store(), SimpleNamespace()))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class PageImageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sp.set_enabled(True)
        sp.save(document())
        shutil.rmtree(sp.PAGES, ignore_errors=True)

    def tearDown(self):
        if sp.SETTINGS.exists():
            sp.SETTINGS.unlink()

    def publish(self):
        folder = sp.PAGES / SHA
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({
            "pdf_sha256": SHA, "pages": 2, "format": "webp", "thumb_width": 320,
            "sizes": [[1280, 720], [1280, 720]], "source_pages": [1, 2],
        }), encoding="utf-8")
        (folder / "t1.webp").write_bytes(b"thumb")
        (folder / "1.webp").write_bytes(b"full")

    async def test_links_omit_sha_until_page_images_exist(self):
        async with client() as api:
            before = (await api.get(f"/api/section-page-links/{RECORDING}")).json()
            self.assertNotIn("viewer_sha256", before["sections"][0]["targets"][0])
            self.assertNotIn("pages_ready", before["resources"][0])
            self.publish()
            after = (await api.get(f"/api/section-page-links/{RECORDING}")).json()
        target = after["sections"][0]["targets"][0]
        self.assertEqual(target["viewer_sha256"], SHA)
        self.assertEqual(target["viewer_pages"], 2)
        self.assertTrue(after["resources"][0]["pages_ready"])

    async def test_page_images_follow_the_linked_pdf_only(self):
        self.publish()
        async with client() as api:
            thumb = await api.get(f"/api/section-page-links/{RECORDING}/pages/{SOURCE}/t1.webp")
            full = await api.get(f"/api/section-page-links/{RECORDING}/pages/{SOURCE}/1.webp")
            manifest = await api.get(f"/api/section-page-links/{RECORDING}/pages/{SOURCE}/manifest.json")
            missing = await api.get(f"/api/section-page-links/{RECORDING}/pages/{SOURCE}/9.webp")
            escaped = await api.get(f"/api/section-page-links/{RECORDING}/pages/{SOURCE}/t0.webp")
            other = await api.get(f"/api/section-page-links/{RECORDING}/pages/{'c' * 32}/1.webp")
            disabled = sp.SETTINGS
        self.assertEqual(thumb.content, b"thumb")
        self.assertEqual(thumb.headers["content-type"].split(";")[0], "image/webp")
        self.assertEqual(full.content, b"full")
        self.assertEqual(manifest.json()["pdf_sha256"], SHA)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(escaped.status_code, 404)
        self.assertEqual(other.status_code, 404)
        disabled.unlink()
        async with client() as api:
            hidden = await api.get(f"/api/section-page-links/{RECORDING}/pages/{SOURCE}/1.webp")
        self.assertEqual(hidden.status_code, 404)

    def test_manifest_must_describe_this_pdf(self):
        folder = sp.PAGES / SHA
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({
            "pdf_sha256": "c" * 64, "pages": 1, "sizes": [[1, 1]],
        }), encoding="utf-8")
        self.assertIsNone(sp.page_image_manifest(SHA))
        self.assertIsNone(sp.page_image_file(SHA, "manifest.json"))
        self.assertIsNone(sp.page_image_file(SHA, "../secret"))


class ViewerSourceTests(unittest.TestCase):
    def text(self, *parts):
        return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")

    def test_shared_viewer_keeps_local_pdf_backend_and_the_speed_path(self):
        engine = self.text("static", "js", "pdf-page-viewer.js")
        page = self.text("static", "js", "pdf-viewer-page.js")
        html = self.text("static", "pdf-viewer.html")
        css = self.text("static", "pdf-viewer.css")
        reader = self.text("static", "js", "markdown-reader.js")
        self.assertIn("new URL('../vendor/pdfjs/pdf.min.mjs'", engine)
        self.assertNotIn("new URL('./vendor/pdfjs/", engine)
        self.assertIn("root.pdfViewer = Object.freeze(api)", engine)
        self.assertIn("const MAX_WIDTH = 960", engine)
        self.assertIn("pv-in-range", engine)
        self.assertIn("requestThumb", engine)
        self.assertNotIn("IMAGE_LANES", engine)
        self.assertIn("可用“下载 PDF”。", engine)
        self.assertIn("/api/section-page-links/", page)
        self.assertIn("url: images ? undefined : pdfUrl", page)
        self.assertIn("systemUrl: pdfUrl", page)
        self.assertIn("window.__pvEarly", html)
        self.assertIn("/api/section-page-links/", html)
        self.assertIn("下载 PDF", html)
        script = re.search(r"<script>\n(.*?)\n</script>", html, re.S).group(0)
        source = script.removeprefix("<script>").removesuffix("</script>")
        digest = base64.b64encode(hashlib.sha256(source.encode()).digest()).decode()
        self.assertIn(f"'sha256-{digest}'", html)
        self.assertIn(".pv-page:not(.pv-rendered) > img.pv-full{visibility:hidden}", css)
        self.assertIn(".pv-page.pv-in-range{outline:3px solid var(--range)", css)
        self.assertIn("outline:none!important", css)
        self.assertIn('dataset.pptLinks = "pending"', self.text("static", "app.js"))
        self.assertIn("const thumbLimit = 2", reader)
        self.assertIn('container.dataset.pptLinks = aligned ? "ready" : "mismatch"', reader)
        self.assertIn('container.dataset.pptLinks = "off"', reader)


if __name__ == "__main__":
    unittest.main()
