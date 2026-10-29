"""Short-lived native PDF operations; the web process keeps no MuPDF runtime."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import threading

PDF_LOCK=threading.RLock()


def pdf_operation(operation,**arguments):
    request={'operation':operation,**arguments}
    with PDF_LOCK:
        result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker'],
            input=json.dumps(request,ensure_ascii=False),capture_output=True,text=True,timeout=120)
    if result.returncode:
        try:message=json.loads(result.stdout).get('error')
        except (ValueError,AttributeError):message=None
        raise ValueError(message or 'PDF 工作进程未完成，原件和已保存成果保留')
    return json.loads(result.stdout)['result']


def _text_pdf(source, destination, title=None):
    import pymupdf as fitz
    import html

    raw = source.read_bytes()
    encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            continue
    else:
        raise ValueError("文本编码无法识别，请先保存为 UTF-8")
    if "\x00" in text:
        raise ValueError("文本含二进制内容，请先保存为 UTF-8")
    # Preserve literal Markdown / LaTeX and readable text rather than fetching links.
    # Bound long lines (including URLs / formulas) so they cannot overflow a page.
    import textwrap
    lines = [part for line in text.splitlines() for part in (textwrap.wrap(line.expandtabs(4), width=70, replace_whitespace=False, drop_whitespace=False) or [""])]
    body = "<h1>" + html.escape(title or source.name) + "</h1>" + "".join(
        "<p>" + html.escape(line) + "</p>" for line in lines
    )
    story = fitz.Story(html=body, user_css="body{font-size:11pt;line-height:1.5;}p{margin:0 0 5pt;overflow-wrap:break-word;}h1{font-size:18pt;}")
    writer = fitz.DocumentWriter(str(destination))
    try:
        more = True
        while more:
            device = writer.begin_page(fitz.paper_rect("a4"))
            more, _ = story.place(fitz.Rect(42, 42, 553, 800))
            story.draw(device)
            writer.end_page()
    finally:
        writer.close()


def _image_pdf(source, destination):
    import pymupdf as fitz

    from PIL import Image, ImageOps, ImageSequence
    doc = fitz.open()
    try:
        with Image.open(source) as image:
            for frame in ImageSequence.Iterator(image):
                frame = ImageOps.exif_transpose(frame).convert("RGB")
                import io
                stream = io.BytesIO()
                frame.save(stream, format="PNG")
                width, height = frame.size
                scale = min(1, 842 / max(width, height))
                page = doc.new_page(width=width * scale, height=height * scale)
                page.insert_image(page.rect, stream=stream.getvalue())
        doc.save(destination, deflate=True)
    finally:
        doc.close()


def _execute(request):
    import pymupdf as fitz
    operation=request['operation']
    if operation in {'text_pdf', 'image_pdf'}:
        source, output = Path(request['path']), Path(request['output'])
        if operation == 'text_pdf':
            _text_pdf(source, output, request.get('title'))
        else:
            _image_pdf(source, output)
        return None
    if operation=='image_locations':
        digests={};locations={row['image_id']:[] for row in request['figures']}
        for row in request['figures']:
            pixmap=fitz.Pixmap(row['path']);digests.setdefault(pixmap.digest,[]).append(row['image_id']);del pixmap
        with fitz.open(request['path']) as doc:
            for number,page in enumerate(doc,1):
                for image in page.get_image_info(hashes=True):
                    for ident in digests.get(image['digest'],[]):
                        if number not in locations[ident]:locations[ident].append(number)
        return locations
    with fitz.open(request['path']) as doc:
        if operation == 'page_count':
            return len(doc)
        if operation == 'validate':
            if doc.needs_pass:
                raise ValueError('PDF 已加密，请先解密再转换')
            if not len(doc):
                raise ValueError('文件中没有可转换的页面')
            return len(doc)
        if operation == 'package':
            for name, path in request['attachments'].items():
                doc.embfile_add(name, Path(path).read_bytes())
            doc.save(request['output'], deflate=True)
            return len(doc)
        if operation == 'text_stats':
            total_chars, uncertain = 0, []
            for index, page in enumerate(doc, 1):
                text = page.get_text().strip()
                total_chars += len(text)
                if '\ufffd' in text:
                    uncertain.append(index)
            return {'pages': len(doc), 'text_chars': total_chars, 'uncertain_pages': uncertain}
        if operation=='inspect':
            return {'pages':len(doc),'blank_pages':[i+1 for i,page in enumerate(doc) if not page.get_text().strip() and not page.get_images()]}
        if operation=='text':
            from qwen_asr_app.yuketang.pdf_layers import inspect_page
            return [{'page':i+1,'text':page.get_text(sort=bool(request.get('sort'))),
                'plain_text':page.get_text(),
                **({'yuketang': inspect_page(page)} if request.get('inspect_yuketang') else {})}
                for i,page in enumerate(doc)]
        if operation=='yuketang_layers':
            from qwen_asr_app.yuketang.pdf_layers import inspect_page
            return [{'page':i+1, **inspect_page(page)} for i,page in enumerate(doc)]
        if operation=='embedded_markdown':
            return doc.embfile_get('mineru-transcript.md').decode('utf-8',errors='replace') if 'mineru-transcript.md' in doc.embfile_names() else None
        if operation=='render':
            selections=request['pages']
            if not 1<=len(selections)<=4:raise ValueError('一次最多渲染四页')
            result=[]
            for row in selections:
                number=row['page']
                if type(number) is not int or not 1<=number<=len(doc):raise ValueError('PDF 页码无效')
                page=doc[number-1];output=Path(row['output']);bbox=row.get('bbox')
                if request.get('expected_pages') is not None and len(doc)!=request['expected_pages']:
                    raise ValueError('原件页码与渲染画面页码不一致，未抽图')
                clip=None
                if bbox is not None:
                    if (len(bbox)!=4 or not all(type(x) in {int,float} and 0<=x<=1 for x in bbox)
                            or not (bbox[0]<bbox[2] and bbox[1]<bbox[3])):raise ValueError('裁切区域无效')
                    r=page.rect;clip=fitz.Rect(r.width*bbox[0],r.height*bbox[1],r.width*bbox[2],r.height*bbox[3])
                    if clip.width<60 or clip.height<40:raise ValueError('裁切区域过小，无法保留可读标签')
                scale=float(row.get('scale',1.25))
                if not 0<scale<=2:raise ValueError('PDF 渲染倍率无效')
                if not output.exists():
                    output.parent.mkdir(parents=True,exist_ok=True)
                    temporary=output.with_name('.'+output.stem+'.part'+output.suffix)
                    clean=None
                    try:
                        rendered=page
                        if request.get('neutralize_yuketang'):
                            from qwen_asr_app.yuketang.pdf_layers import inspect_page, clean_page
                            info=inspect_page(page)
                            if info.get('missing_slide') or info.get('slides'):
                                clean,rendered=clean_page(doc,page,info)
                        rendered.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=clip,alpha=False).save(temporary)
                        temporary.replace(output)
                    finally:
                        if clean is not None:clean.close()
                        temporary.unlink(missing_ok=True)
                result.append({'page':number,'text':page.get_text()})
            return result
    raise ValueError('未知 PDF 操作')


if __name__=='__main__':
    try:
        if sys.argv[1:]!=['--worker']:raise ValueError('无效工作进程参数')
        sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
        value=_execute(json.loads(sys.stdin.read()))
        print(json.dumps({'result':value},ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error':str(error)},ensure_ascii=False));sys.exit(1)
