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


def _execute(request):
    import pymupdf as fitz
    operation=request['operation']
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
        if operation=='inspect':
            return {'pages':len(doc),'blank_pages':[i+1 for i,page in enumerate(doc) if not page.get_text().strip() and not page.get_images()]}
        if operation=='text':
            return [{'page':i+1,'text':page.get_text(sort=bool(request.get('sort'))),
                'plain_text':page.get_text()} for i,page in enumerate(doc)]
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
                    try:
                        page.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=clip,alpha=False).save(temporary)
                        temporary.replace(output)
                    finally:temporary.unlink(missing_ok=True)
                result.append({'page':number,'text':page.get_text()})
            return result
    raise ValueError('未知 PDF 操作')


if __name__=='__main__':
    try:
        if sys.argv[1:]!=['--worker']:raise ValueError('无效工作进程参数')
        value=_execute(json.loads(sys.stdin.read()))
        print(json.dumps({'result':value},ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error':str(error)},ensure_ascii=False));sys.exit(1)
