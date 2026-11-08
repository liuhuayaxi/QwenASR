#!/usr/bin/env python3
"""Read-only local preview of the "↗ PPT" links and the PDF page viewer.

Serves the real static files, the real section-page routes and a small reader
page for one note, without starting the full app (no schedulers, no writes).

  .venv/bin/python scripts/preview_section_pages.py --port 8781
  open http://127.0.0.1:8781/preview/<recording id>
"""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'scripts'))
import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from qwen_asr_app.section_page_routes import section_page_router  # noqa: E402
import section_page_links as cli  # noqa: E402

STORAGE = cli.storage_root()


class Store:
    def find(self, ident):
        try: directory, session = cli.find(STORAGE, ident)
        except SystemExit: return None
        return SimpleNamespace(id=ident, directory=directory, metadata=session)


class Files:
    def file(self, category, asset_id, variant):
        folder = STORAGE / category
        item = json.loads((folder / '.qwenasr-files' / 'index.json').read_text(encoding='utf-8')).get(asset_id) or {}
        relative = (item.get('conversion') or {}).get('relative_path')
        if variant != 'pdf' or not relative: raise ValueError('文件版本尚未生成')
        path = (folder / relative).resolve()
        if not path.is_relative_to(folder.resolve()) or not path.is_file(): raise ValueError('文件不存在')
        return item, path


app = FastAPI()
app.include_router(section_page_router(Store(), SimpleNamespace(store=Files())))
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PPT 跳转预览</title><link rel="stylesheet" href="/static/vendor/katex/katex.min.css"><link rel="stylesheet" href="/static/styles.css">
<link rel="stylesheet" href="/static/ppt-jump.css?v=1"><script src="/static/vendor/katex/katex.min.js"></script></head>
<body><main style="max-width:860px;margin:auto;padding:1rem"><p>只读预览：__TITLE__</p><nav id="toc" class="note-toc"></nav>
<textarea id="source" hidden></textarea><div id="reader" class="note-summary-reader"></div></main>
<script type="module">
import { createMarkdownReader } from "/static/js/markdown-reader.js?v=20261108-ppt-1";
const id = __ID__;
const source = document.getElementById("source");
source.value = __MARKDOWN__;
const reader = createMarkdownReader({ container: document.getElementById("reader"), source, toc: document.getElementById("toc"), onChange() {} });
const links = await fetch(`/api/section-page-links/${id}`).then(r => r.json());
reader.setPageLinks({ ...links, recording: id }); reader.render();
window.__links = links;
</script></body></html>"""


@app.get('/preview/{ident}', response_class=HTMLResponse)
def preview(ident: str):
    record = Store().find(ident)
    if record is None: raise HTTPException(404)
    markdown = (record.metadata.get('ai_note_summary') or {}).get('summary') or ''
    dump = lambda v: json.dumps(v, ensure_ascii=False).replace('</', '<\\/')
    return PAGE.replace('__TITLE__', record.metadata.get('title') or ident).replace('__ID__', dump(ident)).replace('__MARKDOWN__', dump(markdown))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--port', type=int, default=8781)
    uvicorn.run(app, host='127.0.0.1', port=parser.parse_args().port, log_level='warning')
