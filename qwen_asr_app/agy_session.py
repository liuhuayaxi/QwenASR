"""Verify the exact local AGY turn when its print-mode result loses text.

AGY 1.4 can emit SUCCESS with empty response/usage although the completed
planner response is already durable. Never substitute a different conversation,
thoughts, tool output, an incomplete response, or a failed tool invocation.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


class SessionEvidenceError(ValueError):
    def __init__(self, message, *, kind='agy_cli_error'):
        super().__init__(message)
        self.kind = kind


READ_PATH_KEYS = {'view_file': 'AbsolutePath', 'read_file': 'Path'}


def native_read_range(path, arguments, output, *, media=None):
    """Verify the observed AGY 1.4.3 read_file receipt without widening paths."""
    if set(arguments) - {'Path', 'toolAction', 'toolSummary'}:
        raise SessionEvidenceError('新版读取工具的参数尚未核验', kind='material_read_path')
    if Path(path).suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
        if not isinstance(media, list) or not media:
            raise SessionEvidenceError('新版读取工具缺少实际图片回执', kind='material_read_path')
        return 1, 1
    match = re.search(r'^Showing lines (\d+)-(\d+) of (\d+)\s*$', output, re.M)
    if not match:
        raise SessionEvidenceError('新版读取工具缺少完整行范围回执', kind='material_read_path')
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError) as exc:
        raise SessionEvidenceError('新版读取工具的文字原件不可核验', kind='material_read_path') from exc
    start, end, total = map(int, match.groups())
    shown = [(int(m[1]), m[2]) for m in re.finditer(r'^(\d+): ?(.*)$', output[match.end():], re.M)]
    expected = list(enumerate(lines[start - 1:end], start))
    if not 1 <= start <= end <= len(lines) or total != len(lines) or shown != expected:
        raise SessionEvidenceError('新版读取工具返回内容与指定文件不一致或不完整', kind='material_read_path')
    return start, end


FINISH_REMINDER = "You did not call any tools. You should continue calling tools until you've completed your task. If you have completed your task call the finish tool."


def _request_text(entry):
    text=str(entry.get('content') or '')
    match=re.search(r'<USER_REQUEST>\s*(.*?)\s*(?:</USER_REQUEST>|\Z)',text,re.S)
    return match[1].strip() if match else text.strip()


def last_turn(entries, prior_prompts):
    """Entries of the final turn of a continued lesson conversation.

    Earlier turns must be exactly the prompts this lesson session sent, in
    order; otherwise the conversation is not ours and nothing is accepted.
    """
    if not prior_prompts:
        return entries
    starts=[i for i,e in enumerate(entries) if e.get('type')=='USER_INPUT' and _request_text(e)!=FINISH_REMINDER]
    if len(starts)!=len(prior_prompts)+1:
        raise SessionEvidenceError('AGY 续接会话的轮次与本节会话记录不一致')
    for index,prompt in zip(starts,prior_prompts):
        if _request_text(entries[index])!=str(prompt).strip():
            raise SessionEvidenceError('AGY 续接会话的历史轮次与本节会话记录不一致')
    return entries[starts[-1]:]


def output_limit_before_failure(session_id, prompt, *, root=None, prior_prompts=()):
    """Classify a cut-off generation even if its continuation later disconnects."""
    if not re.fullmatch(r'[0-9a-fA-F-]{36}',str(session_id or '')):return False
    root=Path(root) if root is not None else Path.home()/'.gemini/antigravity-cli/brain'
    try:
        entries=[json.loads(line) for line in (root/session_id/'.system_generated/logs/transcript_full.jsonl').read_text().splitlines() if line.strip()]
        entries=last_turn(entries,prior_prompts)
    except (OSError,ValueError):return False
    users=[e for e in entries if e.get('type')=='USER_INPUT']
    if len(users)!=1 or users[0].get('status')!='DONE':return False
    user_text=str(users[0].get('content',''))
    match=re.search(r'<USER_REQUEST>\s*(.*?)\s*(?:</USER_REQUEST>|\Z)',user_text,re.S)
    if (match[1].strip() if match else user_text.strip())!=prompt.strip():return False
    return any(e.get('type')=='ERROR_MESSAGE' and 'exceeded the output token limit' in str(e.get('content','')) for e in entries)


def _continued_response(entries, responses, limits):
    """Recover verbatim JSON suffixes, or a complete replacement answer.

    Never repair syntax, guess overlaps, or join unrelated assistant messages.
    Each fragment must be separated by the CLI's explicit output-limit event.
    Business-level completeness remains the caller's responsibility.
    """
    final=str(responses[-1]['content']).strip()
    if not limits:
        return final, 1

    def complete_json(value):
        value=re.sub(r'^```(?:json)?\s*|\s*```$', '', value.strip())
        def unique_object(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError('duplicate JSON key')
                result[key]=value
            return result
        try:
            result=json.loads(value,object_pairs_hook=unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
            return isinstance(result,(dict,list))
        except ValueError:
            return False

    if complete_json(final):
        return final, 1
    # A JSON suffix needs its preceding knowledge and source bindings.
    positions={id(entry):index for index,entry in enumerate(entries)}
    first_limit=limits[0]
    before=[e for e in responses if positions[id(e)]<first_limit]
    if not before:
        raise SessionEvidenceError('AGY 超限续写缺少起始正文',kind='output_truncated')
    first=before[-1]
    fragments=[e for e in responses if positions[id(e)]>=positions[id(first)]]
    if not str(first['content']).lstrip().startswith(('{','[','```json')):
        # No unambiguous way to distinguish a prose suffix from a replacement.
        raise SessionEvidenceError('AGY 超限续写不能确认正文完整，需要拆批重试',kind='output_truncated')
    for left,right in zip(fragments,fragments[1:]):
        between=entries[positions[id(left)]+1:positions[id(right)]]
        if not between or any(e.get('type')!='ERROR_MESSAGE' or
                'exceeded the output token limit' not in str(e.get('content','')) for e in between):
            raise SessionEvidenceError('AGY 超限续写片段不连续',kind='output_truncated')
    combined=''.join(str(e['content']) for e in fragments).strip()
    if not complete_json(combined):
        raise SessionEvidenceError('AGY 超限续写未构成完整 JSON，需要拆批重试',kind='output_truncated')
    return combined,len(fragments)


def _arguments(call):
    result = {}
    for key, value in (call.get('args') or {}).items():
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        result[key] = value
    return result


def read_session(session_id, prompt, *, root=None, allowed_files=None, required_files=None, schema=None,
                 require_output_continuation=False, prior_prompts=()):
    """Return only a verified completed turn and its successful read receipts."""
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', str(session_id or '')):
        raise SessionEvidenceError('AGY 没有返回有效会话编号')
    root = Path(root) if root is not None else Path.home()/'.gemini/antigravity-cli/brain'
    path = root/session_id/'.system_generated/logs/transcript_full.jsonl'
    try:
        entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise SessionEvidenceError('AGY 会话完成记录尚不可用') from exc
    entries = last_turn(entries, prior_prompts)
    users = [e for e in entries if e.get('type') == 'USER_INPUT']
    # Schema print mode can insert this fixed internal finish reminder after
    # the answer. It is not another source-data request. No arbitrary later
    # user turn is accepted, and ordinary text calls still require one turn.
    finish_reminder = "You did not call any tools. You should continue calling tools until you've completed your task. If you have completed your task call the finish tool."
    extra_users_valid = schema is not None and all(
        e.get('status') == 'DONE' and re.search(r'<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>', str(e.get('content','')), re.S)
        and re.search(r'<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>', str(e.get('content','')), re.S)[1] == finish_reminder
        for e in users[1:])
    if not users or users[0].get('status') != 'DONE' or (len(users) != 1 and not extra_users_valid):
        raise SessionEvidenceError('AGY 会话不是本次独立请求')
    user_text = str(users[0].get('content') or '')
    if 'NOTE: The output was truncated because it was too long.' in user_text:
        raise SessionEvidenceError('AGY 内部截断了请求资料，未采纳不完整输入的结果')
    request_match=re.search(r'<USER_REQUEST>\s*(.*?)\s*(?:</USER_REQUEST>|\Z)',user_text,re.S)
    if (request_match[1].strip() if request_match else user_text.strip()) != prompt.strip():
        raise SessionEvidenceError('AGY 会话输入与本次请求不一致')
    limits = [i for i,e in enumerate(entries) if e.get('type') == 'ERROR_MESSAGE'
              and e.get('status') == 'DONE' and 'exceeded the output token limit' in str(e.get('content',''))]
    other_errors=[e for i,e in enumerate(entries) if e.get('type')=='ERROR_MESSAGE' and i not in limits]
    if other_errors:
        from .agy_cli import _error_kind
        kinds=[_error_kind(str(e.get('content','')))[0] for e in other_errors]
        kind=next((k for k in kinds if k in {'provider_auth','provider_region','agy_cli_config'}),'agy_cli_error')
        if limits and all(k in {'network_transport','network_read_timeout','network_connect_timeout','provider_unavailable'} for k in kinds):
            kind='output_truncated'
        raise SessionEvidenceError('AGY 续写包含其他失败，未采纳结果',kind=kind)
    if require_output_continuation:
        if not limits:
            raise SessionEvidenceError('AGY 缺少本次输出超限及完整资料证据')
        # Only this known internal continuation is recoverable. An auth/network
        # error or another user turn must not be hidden by a later answer.
    allowed = {str(Path(p).resolve()): spec for p, spec in (allowed_files or {}).items()}
    required = set(allowed) if required_files is None else {str(Path(p).resolve()) for p in required_files}
    intervals = {p: [] for p in allowed}
    pending = []
    responses = []
    response_index = -1
    calls = []
    structured = None
    structured_index = -1
    complete_reads=set();redundant_read_errors=0
    events = [{'event': 'init', 'conversation_id': session_id}]
    for entry_index,entry in enumerate(entries):
        status = entry.get('status')
        if status in {'ERROR', 'FAILED', 'CANCELLED', 'CANCELED'}:
            # Some agents ask for an extra offset after the tool explicitly
            # reported the complete text file. An exhausted offset adds no
            # missing evidence; keep the successful read instead of throwing
            # away an otherwise complete review. Never forgive an unknown
            # path, first/partial read, cancellation or other tool failure.
            if status=='ERROR' and entry.get('type')=='GENERIC' and pending:
                name,file,args=pending[0]
                offset=args.get('ContentOffset')
                exhausted=re.search(r'ContentOffset (\d+) exceeds line range size (\d+)',str(entry.get('content','')))
                if (name=='view_file' and file in complete_reads and type(offset) is int
                        and exhausted and offset==int(exhausted[1]) and offset>int(exhausted[2])
                        and args.get('StartLine',1)==1 and args.get('EndLine',allowed[file]['lines'])>=allowed[file]['lines']):
                    pending.pop(0);redundant_read_errors+=1
                    continue
            # The native finish validator can reject a proposal and accept a
            # corrected one in this same turn. Preserve that error in replay.
            if not (schema is not None and entry.get('type') == 'GENERIC' and pending
                    and pending[0][0] == 'finish' and re.search(r'invalid arguments\s*:', str(entry.get('content','')), re.I)):
                raise SessionEvidenceError('AGY 工具或生成步骤失败；未采纳该会话结果',
                    kind='material_read_path' if pending and pending[0][0] in READ_PATH_KEYS else 'agy_cli_error')
        if entry.get('type') == 'PLANNER_RESPONSE':
            planned = entry.get('tool_calls') or []
            if planned:
                if pending:
                    raise SessionEvidenceError('AGY 工具读取回执不完整')
                for call in planned:
                    name = call.get('name')
                    if name == 'finish' and schema is not None:
                        pending.append((name, None, _arguments(call)))
                        calls.append(name)
                        continue
                    if name not in READ_PATH_KEYS:
                        raise SessionEvidenceError(
                            'AGY 文本会话尝试调用未授权工具：' + str(name),
                            kind='material_tool_violation',
                        )
                    args = _arguments(call)
                    value = args.get(READ_PATH_KEYS[name])
                    if name=='read_file' and (not isinstance(value,str) or not Path(value).is_absolute()):
                        raise SessionEvidenceError('新版读取工具必须使用清单内的绝对路径',kind='material_read_path')
                    if not isinstance(value, str) or str(Path(value).resolve()) not in allowed:
                        raise SessionEvidenceError('AGY 文本会话尝试读取未授权文件',kind='material_read_path')
                    pending.append((name, str(Path(value).resolve()), args))
                    calls.append(name)
            elif entry.get('content'):
                if status != 'DONE' or pending:
                    raise SessionEvidenceError('AGY 最终正文尚未完整结束')
                responses.append(entry)
                response_index=entry_index
        elif entry.get('type') == 'GENERIC':
            if not pending:
                raise SessionEvidenceError('AGY 工具读取回执不完整')
            name, file, args = pending.pop(0)
            output = str(entry.get('content') or '')
            if name == 'finish':
                events.append({'event':'step_update','step_update':{'step_type':'tool',
                    'state':status,'tool_info':{'name':'finish','parameters':args,
                    **({'error':output} if status != 'DONE' else {})}}})
                structured = {k:v for k,v in args.items() if k not in {'toolAction','toolSummary'}} if status == 'DONE' else None
                structured_index=entry_index if status=='DONE' else -1
                continue
            if status != 'DONE':
                raise SessionEvidenceError('AGY 工具读取回执不完整')
            if re.search(r'permission denied|operation not permitted|file not found|no such file|error (?:reading|opening)|<truncated \d+ bytes>', output, re.I):
                raise SessionEvidenceError('AGY 资料读取失败或输出被截断',kind='material_read_path')
            start = args.get('StartLine', 1)
            end = args.get('EndLine', allowed[file]['lines'])
            if name == 'read_file':
                start, end = native_read_range(file, args, output, media=entry.get('media'))
            if not isinstance(start, int) or not isinstance(end, int):
                raise SessionEvidenceError('AGY 资料读取范围无效')
            intervals[file].append((start, end))
            if (start==1 and end>=allowed[file]['lines'] and not args.get('ContentOffset')
                    and 'The above content shows the entire, complete file contents of the requested file.' in output):
                complete_reads.add(file)
            events.append({'event':'step_update','step_update':{'step_type':'tool',
                'state':'DONE','tool_info':{'name':name,'parameters':args,'output':output,
                    **({'media':entry['media']} if entry.get('media') else {})}}})
    if pending or (structured is None and (not responses or entries[-1] is not responses[-1])):
        raise SessionEvidenceError('AGY 未保存完整最终正文')
    if limits and ((structured is None and (not responses or response_index <= limits[-1]))
                   or (structured is not None and structured_index<=limits[-1])):
        raise SessionEvidenceError('AGY 超限后尚未完成续写',kind='output_truncated')
    for file, spec in allowed.items():
        try:
            data = Path(file).read_bytes()
        except OSError as exc:
            raise SessionEvidenceError('AGY 请求资料已改变') from exc
        if hashlib.sha256(data).hexdigest() != spec['sha256']:
            raise SessionEvidenceError('AGY 请求资料已改变')
        covered = set()
        for start, end in intervals[file]:
            covered.update(range(max(1, start), min(spec['lines'], end)+1))
        if file in required and len(covered) != spec['lines']:
            raise SessionEvidenceError('AGY 没有完整读取全部请求资料')
    if structured is not None:
        response,fragments=json.dumps(structured,ensure_ascii=False),1
    else:
        response,fragments=_continued_response(entries,responses,limits)
    events.append({'event':'result','result':{'status':'SUCCESS','conversation_id':session_id,
                   'response':response, **({'structured_output':structured} if structured is not None else {})}})
    return {'response': response, 'session_id': session_id, 'events': events,
            'viewed_files': sorted(p for p, values in intervals.items() if values),
            'redundant_read_errors':redundant_read_errors,
            'output_continuation_fragments':fragments,
            'tools': calls, 'recovered_from_completed_turn': True}
