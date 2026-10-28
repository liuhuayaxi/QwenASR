"""Resume complete Markdown knowledge groups after an upstream output limit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .ai import request_ai_text, AIRequestError
from .agy_cli import last_session_id
from .durable import atomic_bytes, atomic_json


def _sha(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


async def write_batches(instructions, payload, config, folder):
    """Keep all evidence, partition output ownership, attest every saved group."""
    from .note_latex_author import _figure_batches
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    knowledge=payload['coverage']['knowledge']
    ids=[str(k['id']) for k in knowledge]
    if not ids:raise ValueError('图文写作缺少完整知识清单')
    root=folder/'markdown-batches';root.mkdir(exist_ok=True)
    fingerprint=_sha([instructions,payload])
    plan_path=root/'plan.json'
    if plan_path.exists():
        plan=json.loads(plan_path.read_text())
        if plan.get('source_sha256')!=fingerprint or [i for group in plan['groups'] for i in group]!=ids:
            raise ValueError('已保存图文写作批次与当前完整证据不同，旧稿已保留')
    else:
        middle=max(1,len(ids)//2)
        plan={'source_sha256':fingerprint,'groups':[ids[:middle],ids[middle:]] if len(ids)>1 else [ids]}
        atomic_json(plan_path,plan)
    by_id={str(k['id']):k for k in knowledge}
    all_figures,_=_figure_batches(knowledge,payload.get('images',[]),[knowledge])
    figures=all_figures[0]
    outline=[{key:k[key] for key in ('id','topic','kind','target_section') if key in k} for k in knowledge]
    session_ids=[]

    async def group_body(group,path):
        path.mkdir(parents=True,exist_ok=True)
        split_path=path/'split.json'
        if split_path.exists():
            groups=json.loads(split_path.read_text())['groups']
            if [i for part in groups for i in part]!=group or any(not part for part in groups):
                raise ValueError('写作输出拆分计划与完整知识清单不一致')
            parts=[await group_body(part,path/str(n)) for n,part in enumerate(groups)]
            return '\n\n'.join(parts)
        accepted_path=path/'accepted.json';body_path=path/'body.md'
        if accepted_path.exists():
            accepted=json.loads(accepted_path.read_text())
            if accepted.get('knowledge_ids')!=group or not body_path.exists() or accepted.get('body_sha256')!=hashlib.sha256(body_path.read_bytes()).hexdigest():
                raise ValueError('已完成图文写作批次内容改变，旧稿和回执已保留')
            if not accepted.get('writer_session_id'):raise ValueError('图文写作批次缺少写作者回执')
            session_ids.append(accepted['writer_session_id'])
            return body_path.read_text()
        selected=[f for f in figures if f['assigned_knowledge_id'] in group]
        local={**payload,'coverage':{**payload['coverage'],'knowledge':[by_id[i] for i in group],
                                     'figures':selected},'images':selected,
               'required_knowledge_ids':group,'complete_coverage_context':outline}
        # The final fast-note postprocessor appends the complete Yuketang bank;
        # never repeat that large payload for every fallback batch.
        local.pop('yuketang_questions', None)
        extra=('\n本批次不生成课上习题小节；完整题目由程序在最终稿统一补全。\n本次只编写 required_knowledge_ids 列出的连续知识批次。完整来源资料保留用于上下文；'
            '只输出本批知识，不重复其他批次。每个ID恰好出现一次，使用 '
            '<!-- knowledge:真实ID -->正文<!-- /knowledge:真实ID -->，不得合并或漏掉ID。'
            '只嵌入本批 images 清单中的图，每图一次，放到 assigned_knowledge_id 指定知识块。'
            '不输出独立总标题、总导航、结语或预告，章节标题放到对应知识块内。保留原公式、推导、例题步骤与来源，'
            '不要因为分批而写成摘要。只返回完整 Markdown。')
        existing=sorted(path.glob('proposal-*.md'),key=lambda p:int(p.stem.rsplit('-',1)[1]))
        for attempt in range(len(existing),3):
            try:
                response=await request_ai_text(instructions=instructions+extra,payload=local,config=config,
                                               mode='summary',max_output_tokens=32768)
            except AIRequestError as exc:
                if exc.kind!='output_truncated' or len(group)<2:raise
                middle=len(group)//2
                atomic_json(split_path,{'groups':[group[:middle],group[middle:]],'error':str(exc)})
                return await group_body(group,path)
            session=last_session_id.get()
            atomic_bytes(path/f'proposal-{attempt}.md',response.encode())
            atomic_json(path/f'proposal-{attempt}.receipt.json',{'writer_session_id':session,
                         'body_sha256':hashlib.sha256(response.encode()).hexdigest()})
            starts=re.findall(r'<!--\s*knowledge:([^\s>]+)\s*-->',response)
            ends=re.findall(r'<!--\s*/knowledge:([^\s>]+)\s*-->',response)
            errors=[]
            if starts!=group or ends!=group:errors.append('知识块开始与结束ID必须分别严格等于 required_knowledge_ids 的顺序，各恰好一次')
            for image in selected:
                if response.count(']('+image['path']+')')!=1:errors.append('必须恰好嵌入一次指定图片 '+image['path'])
            if not session:errors.append('缺少写作者会话回执')
            if not errors:
                atomic_bytes(body_path,response.encode())
                atomic_json(accepted_path,{'writer_session_id':session,'knowledge_ids':group,
                            'body_sha256':hashlib.sha256(response.encode()).hexdigest()})
                session_ids.append(session)
                return response
            atomic_json(path/f'proposal-{attempt}.validation.json',{'errors':errors})
            local={**local,'previous_proposal':response,'validation_errors':errors}
        raise ValueError('图文写作批次三次完整提案仍未通过，原稿及诊断已保留')

    bodies=[await group_body(group,root/str(index)) for index,group in enumerate(plan['groups'])]
    return '\n\n'.join(bodies),list(dict.fromkeys(session_ids))
