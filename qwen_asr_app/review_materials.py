"""Native, course-scoped evidence packages, independent of the vector index."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from datetime import datetime
import zipfile
from xml.etree import ElementTree as ET

from .durable import atomic_json
from .note_content import transcript_blocks
from .storage import RecordingRecord, app_settings
from .course_files import CourseFiles, digest_file
from .paths import PRIVATE_ROOT

from .native_pdf import PDF_LOCK  # Native worker operations remain centrally serialized.
FORMAT='review-materials-v1'


def substantive_page_text(text):
    """A scan's added filename/page footer is not a usable native text layer."""
    value=re.sub(r'-?\s*\d+\s*/\s*\d+\s*页\s*-?', '', text)
    return re.sub(r'《[^》\n]*》', '', value).strip()


def learning_scope_candidates(sources):
    candidates=[]
    for source in sources:
        matches=[]
        for page in source['pages']:
            text=page.get('text','')
            if (re.search(r'(?:\d+(?:\.\d+)*\s*[章节]|本[章节]|学习|教学|考核)\s*(?:基本)?要求|学习目标|学习重点|要掌握|'
                          r'(?i:learning\s+(?:goals?|objectives?|outcomes?)|course\s+objectives?)',text)
                    or re.search(r'(?:^|\n)\s*(?:\d+[.、．)]\s*)?(?:掌握|了解|理解|能够计算)',text)):
                matches.append(page)
        # A chapter may put its requirements in an image at the very front.
        # These bounded discovery pages are inspected once per original hash.
        selected={p['page']:p for p in matches}
        if not matches:
            selected.update({p['page']:p for p in source['pages'][:3]})
        for number,page in sorted(selected.items()):
            candidates.append({'source_id':source['source_id'],'source_sha256':source['sha256'],
                'page':number,'text':page.get('text',''),'image_primary':page.get('image_primary',False),
                'reason':'learning_requirement_signal' if page in matches else 'chapter_front_discovery'})
    return candidates


def native_page_cache(path, digest):
    cache=PRIVATE_ROOT/'review-materials-cache'/'native-v1'/f'{digest}.json'
    with PDF_LOCK:
        if cache.is_file():return json.loads(cache.read_text())
        pages=native_pages(path)
        atomic_json(cache,pages)
        return pages


def original_slide_pdf(path, cache):
    """LibreOffice renders the original slide canvas, never an OCR text layout."""
    path=Path(path);cache=Path(cache);cache.mkdir(parents=True,exist_ok=True)
    target=cache/(path.stem+'.pdf')
    if target.is_file():return target
    binary=shutil.which('soffice') or shutil.which('libreoffice')
    if not binary:raise ValueError('原始幻灯片渲染器不可用，请补选保留原画面的 PDF 原件')
    with PDF_LOCK:
        if target.is_file():return target
        profile=(cache/'office-profile').resolve().as_uri()
        result=subprocess.run([binary,'--headless','-env:UserInstallation='+profile,'--convert-to','pdf','--outdir',str(cache),str(path)],capture_output=True,timeout=180)
        if result.returncode or not target.is_file():raise ValueError('原始幻灯片画面渲染未完成，未使用转写排版 PDF 代替')
    return target


def sha(value):return hashlib.sha256(value).hexdigest()


def lesson_precedes(previous, current):
    try:
        a=datetime.fromisoformat(str(previous).replace('Z','+00:00'))
        b=datetime.fromisoformat(str(current).replace('Z','+00:00'))
        return a.tzinfo is not None and b.tzinfo is not None and a.timestamp()<b.timestamp()
    except (ValueError,TypeError):return False


def prior_lessons(record, settings):
    category=record.metadata.get('category');cutoff=record.metadata.get('started_at')
    for meta in (Path(settings.storage_root)/category).rglob('session.json'):
        try:
            data=json.loads(meta.read_text())
            if data.get('id')==record.id or data.get('category')!=category or not lesson_precedes(data.get('started_at'),cutoff):continue
            yield meta,data
        except (OSError,ValueError):continue


def history_signature(record, settings):
    values=[]
    for meta,data in prior_lessons(record,settings):
        transcript=meta.parent/'transcript.json'
        values.append({'id':data.get('id'),'date':data.get('started_at'),'title':data.get('title'),
            'transcript_sha256':digest_file(transcript) if transcript.is_file() else None,
            'refinement_sha256':sha(json.dumps([data.get('ai_global_rewrite'),data.get('ai_reviews')],sort_keys=True,ensure_ascii=False).encode())})
    return sorted(values,key=lambda x:(x['date'],str(x['id'])))


def history_content_signature(values):
    """Display titles can change after AI naming; they are not source content."""
    return [{key:value for key,value in row.items() if key!='title'} for row in values]


def safe_path(root, relative):
    root=Path(root).resolve()
    if not isinstance(relative,str) or '\\' in relative or ':' in relative or relative.startswith('/'):
        raise ValueError('资料路径无效')
    result=(root/relative).resolve()
    if not result.is_relative_to(root) or '..' in Path(relative).parts:raise ValueError('资料路径越界')
    return result


def tokens(text):
    words=re.findall(r'[a-zA-Z][a-zA-Z0-9_-]+|[\u4e00-\u9fff]+',text.lower())
    result=set()
    for word in words:
        if re.search('[\u4e00-\u9fff]',word):result.update(word[i:i+2] for i in range(len(word)-1))
        else:result.add(word)
    return result


def native_pages(path):
    if path.suffix.lower()=='.pdf':
        from .native_pdf import pdf_operation
        with PDF_LOCK:pages=pdf_operation('text',path=str(path),sort=True)
        return [{'page':p['page'],'text':p['text'],'visual_source':True,
            'image_primary':len(substantive_page_text(p['plain_text']))<40} for p in pages]
    if path.suffix.lower()=='.pptx':
        with zipfile.ZipFile(path) as z:
            names=sorted((n for n in z.namelist() if re.fullmatch(r'ppt/slides/slide\d+.xml',n)),key=lambda n:int(re.search(r'(\d+)\.xml',n)[1]))
            return [{'page':i+1,'text':'\n'.join(e.text or '' for e in ET.fromstring(z.read(n)).iter() if e.tag.endswith('}t')),
                     'visual_source':False,'requires_original_slide_render':True} for i,n in enumerate(names)]
    if path.suffix.lower() in {'.txt','.md'}:
        return [{'page':1,'text':path.read_text(encoding='utf-8'),'visual_source':False}]
    raise ValueError('原件暂不支持原生读取，请补选 PDF 或 PPTX；未使用转写排版 PDF 作为原图')


def _retrieval_by_source(blocks, originals):
    """Each file's summed top-2 page-match score over all transcript blocks."""
    indexed=[(source,page,tokens(page['text'])) for source in originals for page in source['pages']]
    totals={s['source_id']:0.0 for s in originals}
    for block in blocks:
        terms=tokens(block['text'])
        ranked=sorted(((len(terms&v)/max(1,len(v)**.5),s) for s,p,v in indexed),key=lambda x:-x[0])
        for score,source in ranked[:2]:
            if score>0:totals[source['source_id']]+=score
    return totals


def collect(record, folder, selected=None, settings=None, lesson_title=None):
    settings=settings or app_settings
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    category=record.metadata.get('category')
    if category not in settings.categories:raise ValueError('课程不存在')
    files=CourseFiles(settings)
    from .knowledge.ingest import ensure_course_id
    from .knowledge.store import KnowledgeStore
    cid=ensure_course_id(Path(settings.storage_root),category,KnowledgeStore(Path(settings.storage_root)/'.knowledge.db'))
    blocks=transcript_blocks(record)
    atomic_json(folder/'transcript.json',{'segments':record.segments,'blocks':blocks,'recording_id':record.id,
        'audio_sha256':digest_file(record.audio_path) if record.audio_path.is_file() else None,
        'audio_path':str(record.audio_path),'time_basis':'recording sample timeline; not slide dwell time'})
    from .classroom_intelligence.integrations import saved_notes_markdown
    notes=saved_notes_markdown(record)
    (folder/'accepted-notes.md').write_text(notes,encoding='utf-8')
    originals=[];seen=set();excluded=[];ai_warnings=[]
    for item in files.list(category):
        if selected is not None and item['id'] not in selected:continue
        if item.get('status') in {'deleted','superseded','excluded'} or item.get('superseded_by'):
            excluded.append({'id':item['id'],'reason':'superseded_or_inactive'});continue
        try:
            _,path=files.file(category,item['id'],'original')
            digest=digest_file(path)
            if digest!=item['sha256']:raise ValueError('课件原件已改变，请先确认资料版本')
            if digest in seen:
                excluded.append({'id':item['id'],'reason':'duplicate_content','duplicate_of':next(s['source_id'] for s in originals if s['sha256']==digest)})
                continue
            seen.add(digest)
            suffix=path.suffix.lower();relative=f'sources/{item["id"]}{suffix}'
            target=safe_path(folder,relative);target.parent.mkdir(exist_ok=True)
            # Only files chosen for this lesson are copied into the package
            # (below); text is read from the verified original in place.
            cache=folder/'sources'/(digest+'.pages.json')
            if cache.exists():pages=json.loads(cache.read_text())
            else:pages=native_page_cache(path,digest);atomic_json(cache,pages)
            if suffix=='.pdf':
                # Reclassify old caches as well; keep their original text and
                # reuse the already converted, source-bound navigation layer.
                for page in pages:
                    if not page.get('text_authority'):
                        page['image_primary']=len(substantive_page_text(page.get('text','')))<40
            conversion=item.get('conversion') or {}
            # Reuse a successful existing OCR layer strictly for navigation.
            # Page markers bind it to the original; scans remain image-primary.
            # Prefer the up-to-date AI友好型 text (headings, formulas, OCR'd
            # scans) for every page, not only scans; the original page image
            # stays primary for figures and page numbers via the page markers.
            ai_friendly=False
            if suffix=='.pdf' and conversion.get('status')=='completed' and conversion.get('source_sha256')==digest:
                try:
                    _,derived=files.file(category,item['id'],'pdf')
                    derived_digest=digest_file(derived)
                    from .native_pdf import pdf_operation
                    with PDF_LOCK:
                        for converted_page in pdf_operation('text',path=str(derived)):
                            text=converted_page['text']
                            marker=re.search(r'\[\[AI_TRANSCRIPT_PAGE:(\d+)\]\]',text)
                            if marker and 1<=int(marker[1])<=len(pages):
                                original_page=pages[int(marker[1])-1]
                                original_page['text']=text
                                original_page['text_authority']='existing_ocr_navigation_only; original image is primary'
                                original_page['text_lineage']={'derived_sha256':derived_digest,'original_page':int(marker[1])}
                                ai_friendly=True
                except (OSError,ValueError):pass
            if suffix=='.pdf' and not ai_friendly:
                state='已过期' if conversion.get('status')=='completed' else '缺少'
                ai_warnings.append(f'{item["name"]}：AI友好型版本{state}，本次使用原件文字层')
                import logging
                logging.getLogger(__name__).warning('AI-friendly PDF %s for %s; using original text',state,item['name'])
            originals.append({'source_id':item['id'],'name':item['name'],'sha256':digest,'path':relative,
                'text_source':'ai_friendly' if ai_friendly else 'original',
                'course_id':cid,'pages':pages,'origin':'original','kind':item.get('kind'),'asset_created_at':item.get('created_at')})
        except (OSError,ValueError,zipfile.BadZipFile) as exc:
            excluded.append({'id':item['id'],'reason':str(exc)})
    if selected is not None and set(selected)-{s['source_id'] for s in originals}:
        raise ValueError('补选课件不属于本课程、已过期或无法读取')
    # Lesson scope: keep only this lesson's files (chapter / 雨课堂 title /
    # transcript retrieval), not every chapter of the course.
    from . import material_scope
    if lesson_title is None:lesson_title=material_scope.lesson_title_for(record)
    try:coverage=material_scope.coverage_scores(blocks,originals,tokens)
    except Exception:coverage=None
    try:prior=material_scope.previous_lesson_files(record,settings)
    except Exception:prior=set()
    chosen_ids,decisions=material_scope.choose(originals,_retrieval_by_source(blocks,originals),lesson_title,selected,coverage,prior)
    for row in decisions:
        if not row['chosen']:excluded.append({'id':row['source_id'],'reason':'not_in_lesson_scope','detail':'；'.join(row['reasons'])})
    originals=[s for s in originals if s['source_id'] in set(chosen_ids)]
    for source in originals:
        target=safe_path(folder,source['path'])
        if not target.exists():
            _,original=files.file(category,source['source_id'],'original');shutil.copy2(original,target)
    material_selection={'version':material_scope.VERSION,'lesson_title':lesson_title,'files':decisions,
        'summary':material_scope.summary(decisions,lesson_title)}
    # Score every real transcript block, including the middle and final blocks.
    associations=[];page_scores={}
    indexed=[(source,page,tokens(page['text'])) for source in originals for page in source['pages']]
    for i,block in enumerate(blocks,1):
        terms=tokens(block['text'])
        ranked=sorted(((len(terms&v)/max(1,len(v)**.5),s,p) for s,p,v in indexed),key=lambda x:-x[0])
        hits=[]
        for score,source,page in ranked[:2]:
            if score<=0:continue
            sid=source['source_id'];number=page['page']
            hits.append({'source_id':sid,'page':number,'score':round(score,4),'confidence':'topic_match_only'})
            for adjacent in range(max(1,number-1),min(len(source['pages']),number+1)+1):
                page_scores[(sid,adjacent)]=page_scores.get((sid,adjacent),0)+score*(1 if adjacent==number else .25)
        associations.append({'source':'S'+str(i),'start':block['start'],'end':block['end'],'pages':hits,
            'page_dwell':'unknown; audio topic duration is only an indirect clue'})
    history=[];cutoff=record.metadata.get('started_at','')
    # Read only earlier lessons in this course, using transcripts as authority.
    for meta,data in prior_lessons(record,settings):
        try:
            previous=RecordingRecord.load(meta.parent)
            history.append({'id':previous.id,'started_at':data['started_at'],'title':data.get('title'),
                'blocks':transcript_blocks(previous),'authority':'transcript','complete':data.get('status')=='completed'})
        except (OSError,ValueError):continue
    history.sort(key=lambda x:x['started_at'])
    atomic_json(folder/'history.json',history)
    relevant=[]
    for (sid,number),score in sorted(page_scores.items(),key=lambda x:-x[1]):
        source=next(s for s in originals if s['source_id']==sid)
        page=source['pages'][number-1]
        relevant.append({'source_id':sid,'page':number,'score':score,'text':page['text'],'visual_source':page['visual_source']})
    package={'format':FORMAT,'recording_id':record.id,'course_id':cid,'category':category,'lesson_date':cutoff,
        'sources':originals,'excluded':excluded,'associations':associations,'relevant_pages':relevant,
        'material_selection':material_selection,
        'history_count':len(history),'history_scope':'仅已收录且早于本节的转写；未收录不能证明此前未讲',
        'warnings':([] if originals else ['没有可读课件，请补选本课程资料'])+([] if history else ['缺少此前课次，首次引入的判断范围有限'])
            +[w for w in ai_warnings if any(w.startswith(s['name']+'：') for s in originals)]}
    package['learning_scope_candidates']=learning_scope_candidates(originals)
    atomic_json(folder/'materials.json',package)
    return package


def extract_image(folder, package, request):
    from .native_pdf import pdf_operation
    sid=request['source_id'];page=request['page'];bbox=request.get('bbox',[0,0,1,1])
    source=next((s for s in package['sources'] if s['source_id']==sid),None)
    if not source or source['course_id']!=package['course_id']:raise ValueError('图片来源不属于本课程')
    if type(page) is not int or not 1<=page<=len(source['pages']):raise ValueError('课件页码无效')
    if len(bbox)!=4 or not all(isinstance(x,(int,float)) and 0<=x<=1 for x in bbox) or not (bbox[0]<bbox[2] and bbox[1]<bbox[3]):raise ValueError('裁切区域无效')
    original=safe_path(folder,source['path'])
    if digest_file(original)!=source['sha256']:raise ValueError('图片来源版本改变')
    rendered_original=original
    if original.suffix.lower()=='.pptx':
        rendered_original=original_slide_pdf(original,Path(folder)/'slide-render'/source['sha256'])
    elif original.suffix.lower()!='.pdf':raise ValueError('该原件需要原始幻灯片渲染，不能使用转写排版 PDF 充当原图')
    ident='fig-'+sha(json.dumps([sid,source['sha256'],page,bbox]).encode())[:16]
    relative=f'assets/{ident}.png';target=safe_path(folder,relative);target.parent.mkdir(exist_ok=True)
    cached_image=PRIVATE_ROOT/'review-materials-cache'/'images-v1'/f'{ident}.png'
    with PDF_LOCK:
        if not target.exists() and cached_image.is_file():shutil.copy2(cached_image,target)
        pdf_operation('render',path=str(rendered_original),expected_pages=len(source['pages']),
            pages=[{'page':page,'output':str(target),'bbox':bbox,'scale':1.6}])
        if not cached_image.is_file():
            cached_image.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            temporary=cached_image.with_suffix('.part.png');shutil.copy2(target,temporary);temporary.replace(cached_image)
    return {'image_id':ident,'path':relative,'source_id':sid,'source_name':source['name'],'source_sha256':source['sha256'],
        'page':page,'bbox':bbox,'concept':request.get('concept',''),'reason':request.get('reason',''), 'sha256':digest_file(target)}


def bind_markdown_images(markdown, folder, images, rejected=()):
    """Resolve model locators against actual inspected assets, never guess files.

    Some writers return source/page locators instead of supplied asset paths.
    Bind those using the validated inventory; deliberately rejected text-only
    slides remain prose, with their exclusion reason in the task ledger.
    """
    by_page={(i['source_id'],i['page']):i for i in images}
    by_path={i['path']:i for i in images}
    excluded={(r['image']['source_id'],r['image']['page']):r for r in rejected}
    bindings=[]
    def replace(match):
        alt,uri=match[1],match[2].removeprefix('./')
        safe_path(folder,uri)
        image=by_path.get(uri)
        locator=re.fullmatch(r'figures/([a-f0-9]{32})_p(\d+)\.png',uri)
        if not image and locator:
            key=(locator[1],int(locator[2]));image=by_page.get(key)
            if key in excluded:
                bindings.append({'requested':uri,'status':'excluded','reason':excluded[key]['reason']})
                return ''
        if not image:
            from .yuketang.note_images import registered
            sha=registered(folder).get(uri)
            actual=safe_path(folder,uri)
            if sha and actual.is_file() and digest_file(actual)==sha:
                bindings.append({'requested':uri,'path':uri,'status':'yuketang'})
                return f'![{alt}]({uri})'
        if not image:raise ValueError('正文引用了未抽取或未看图确认的图片：'+uri)
        actual=safe_path(folder,image['path'])
        if not actual.is_file() or digest_file(actual)!=image['sha256']:raise ValueError('笔记图片缺失或版本改变')
        bindings.append({'requested':uri,'path':image['path'],'image_id':image['image_id'],'status':'embedded'})
        return f'![{alt}]({image["path"]})'
    result=re.sub(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)',replace,markdown)
    atomic_json(Path(folder)/'image-bindings.json',bindings)
    return result
