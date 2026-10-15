"""Conservative high-risk edit gate. This is not a semantic correctness oracle."""
from __future__ import annotations

import collections
import copy
import re


def sensitive_signature(text):
    normalized = re.sub(r'\[\d{1,2}:\d{2}(?::\d{2})?(?:[–—-]\d{1,2}:\d{2}(?::\d{2})?)?\]', '', str(text))
    normalized = normalized.replace('−', '-').replace('－', '-').replace('＋', '+')
    normalized = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', normalized)
    numbers = re.findall(r'(?<![0-9A-Za-z_.])[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?\s*(?:%|[μµu]?[mMkKG]?Hz|[μµu]?[mMkK]?W|[μµu]?[mMc]?A|[μµu]?[mMkK]?V|[μµu]?[mMc]?s|[μµu]?m|dB|℃|°C|nm|毫米|厘米|毫瓦|瓦|伏|秒|赫兹)?', normalized)
    numbers = [re.sub(r'\s+', '', n).replace('µ','μ') for n in numbers]
    negations = re.findall(r'不是|不能|不会|不等于|不成立|不存在|不满足|没有|未测量|无关|非(?=相干|线性|同步|相关|对称|平稳|因果|零|负)|无(?=噪声|损耗|损|效|解)|不(?!仅|断|同|管|论|过|但)|(?:\bnot\b|\bnever\b|\bno\b)', normalized, flags=re.I)
    relations = re.findall(r'<=|>=|!=|≤|≥|≠|(?<![<>])[<>](?![<>])|不小于|不大于|大于等于|小于等于|大于|小于|至少|至多', normalized)
    numeral=r'[零〇一二两三四五六七八九十百千万亿]+'
    chinese_numbers=re.findall(numeral+'分之'+numeral+'|'+numeral+'点'+numeral+'|'+numeral+r'(?:毫瓦|瓦|毫伏|伏|纳米|微米|米|度|秒|赫兹|分贝)',normalized)
    return {'numbers_units': collections.Counter(numbers), 'negations': collections.Counter(negations),
            'relations': collections.Counter(relations), 'chinese_numbers':collections.Counter(chinese_numbers), 'sign_words': collections.Counter(re.findall(r'正号|负号|加号|减号|负值|负数|正数|[正负](?=[零一二三四五六七八九十0-9])|negative|positive', normalized, flags=re.I))}


def correction_risks(before, after):
    left, right = sensitive_signature(before), sensitive_signature(after)
    risks = []
    for key, label in [('numbers_units','数字或单位变化'), ('chinese_numbers','中文数值或单位变化'), ('negations','否定词变化'), ('relations','公式条件或不等号变化'), ('sign_words','正负号语义变化')]:
        if left[key] != right[key]:
            risks.append(label)
    if len(str(after)) > max(80, len(str(before))*1.7):
        risks.append('明显扩写，可能把资料补充混入转录')
    return risks


def protect_correction(segment, proposal, evidence=None):
    """Return safe text separately from the retained, reviewable proposal."""
    before = str(segment.get('corrected') or segment.get('raw') or '')
    after = str(proposal or '')
    risks = correction_risks(before, after)
    if not risks:
        return after, None
    candidate = {'before': before, 'after': after, 'segment_ids': segment.get('segment_ids') or [segment.get('id')],
                 'risks': risks, 'status': '待核对', 'evidence': copy.deepcopy(evidence or [])}
    return before, candidate


def correction_provenance(before, proposal, config, segment_ids=None, model=None):
    """Retain the input and candidate retrieval support, without asserting entailment."""
    import hashlib
    references=[{k:copy.deepcopy(e.get(k)) for k in ('citation_id','source_id','revision','content_hash','chunk_id','title','page','start','end','open_url','origin')}
                for e in config.get('_knowledge_evidence') or []]
    return {'before':before,'proposal':proposal,'before_hash':hashlib.sha256(str(before).encode()).hexdigest(),
            'segment_ids':list(segment_ids or []),'sources':references,'model':model or '',
            'support_status':'检索候选，未逐项确认修改的语义支持'}
