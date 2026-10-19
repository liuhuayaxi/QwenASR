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


def output_limit_before_failure(session_id, prompt, *, root=None):
    """Classify a cut-off generation even if its continuation later disconnects."""
    if not re.fullmatch(r'[0-9a-fA-F-]{36}',str(session_id or '')):return False
    root=Path(root) if root is not None else Path.home()/'.gemini/antigravity-cli/brain'
    try:
        entries=[json.loads(line) for line in (root/session_id/'.system_generated/logs/transcript_full.jsonl').read_text().splitlines() if line.strip()]
    except (OSError,ValueError):return False
    users=[e for e in entries if e.get('type')=='USER_INPUT']
    if len(users)!=1 or prompt not in str(users[0].get('content','')):return False
    return any(e.get('type')=='ERROR_MESSAGE' and 'exceeded the output token limit' in str(e.get('content','')) for e in entries)


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
                 require_output_continuation=False):
    """Return only a verified completed turn and its successful read receipts."""
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', str(session_id or '')):
        raise SessionEvidenceError('AGY 没有返回有效会话编号')
    root = Path(root) if root is not None else Path.home()/'.gemini/antigravity-cli/brain'
    path = root/session_id/'.system_generated/logs/transcript_full.jsonl'
    try:
        entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise SessionEvidenceError('AGY 会话完成记录尚不可用') from exc
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
    if any(e.get('type') == 'ERROR_MESSAGE' and i not in limits for i,e in enumerate(entries)):
        raise SessionEvidenceError('AGY 续写包含其他失败，未采纳结果')
    if require_output_continuation:
        if not allowed_files or not limits:
            raise SessionEvidenceError('AGY 缺少本次输出超限及完整资料证据')
        # Only this known internal continuation is recoverable. An auth/network
        # error or another user turn must not be hidden by a later answer.
    allowed = {str(Path(p).resolve()): spec for p, spec in (allowed_files or {}).items()}
    required = set(allowed) if required_files is None else {str(Path(p).resolve()) for p in required_files}
    intervals = {p: [] for p in allowed}
    pending = []
    responses = []
    calls = []
    structured = None
    complete_reads=set();redundant_read_errors=0
    events = [{'event': 'init', 'conversation_id': session_id}]
    for entry in entries:
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
                    kind='material_read_path' if pending and pending[0][0]=='view_file' else 'agy_cli_error')
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
                    if name != 'view_file':
                        raise SessionEvidenceError('AGY 文本会话尝试调用未授权工具：'+str(name))
                    args = _arguments(call)
                    value = args.get('AbsolutePath')
                    if not isinstance(value, str) or str(Path(value).resolve()) not in allowed:
                        raise SessionEvidenceError('AGY 文本会话尝试读取未授权文件',kind='material_read_path')
                    pending.append((name, str(Path(value).resolve()), args))
                    calls.append(name)
            elif entry.get('content'):
                if status != 'DONE' or pending:
                    raise SessionEvidenceError('AGY 最终正文尚未完整结束')
                responses.append(entry)
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
                continue
            if status != 'DONE':
                raise SessionEvidenceError('AGY 工具读取回执不完整')
            if re.search(r'permission denied|operation not permitted|file not found|no such file|error (?:reading|opening)|<truncated \d+ bytes>', output, re.I):
                raise SessionEvidenceError('AGY 资料读取失败或输出被截断',kind='material_read_path')
            start = args.get('StartLine', 1)
            end = args.get('EndLine', allowed[file]['lines'])
            if not isinstance(start, int) or not isinstance(end, int):
                raise SessionEvidenceError('AGY 资料读取范围无效')
            intervals[file].append((start, end))
            if (start==1 and end>=allowed[file]['lines'] and not args.get('ContentOffset')
                    and 'The above content shows the entire, complete file contents of the requested file.' in output):
                complete_reads.add(file)
            events.append({'event':'step_update','step_update':{'step_type':'tool',
                'state':'DONE','tool_info':{'name':name,'parameters':args,'output':output}}})
    if pending or (structured is None and (not responses or entries[-1] is not responses[-1])):
        raise SessionEvidenceError('AGY 未保存完整最终正文')
    if require_output_continuation and (not responses or entries.index(responses[-1]) <= limits[-1]):
        raise SessionEvidenceError('AGY 超限后尚未完成续写')
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
    response = json.dumps(structured, ensure_ascii=False) if structured is not None else str(responses[-1]['content']).strip()
    events.append({'event':'result','result':{'status':'SUCCESS','conversation_id':session_id,
                   'response':response, **({'structured_output':structured} if structured is not None else {})}})
    return {'response': response, 'session_id': session_id, 'events': events,
            'viewed_files': sorted(p for p, values in intervals.items() if values),
            'redundant_read_errors':redundant_read_errors,
            'tools': calls, 'recovered_from_completed_turn': True}
