import isolation  # noqa: F401

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from qwen_asr_app.ai import AIRequestError, provider_for_mode
from qwen_asr_app.ai_preferences import AIPreferences
from qwen_asr_app.ai_secrets import AISecretStore
from qwen_asr_app.classroom_chat import (
    DEFAULT_CHAT_PROMPT, DEFAULT_PROVIDER, build_question, chat_settings,
    classroom_chat_router, list_agy_models,
)


class ClassroomChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.preferences = AIPreferences(root / 'settings.json')
        self.secrets = AISecretStore(root / 'keys.json')
        self.record = SimpleNamespace(
            id='lecture-test-001', directory=root, duration=130,
            metadata={'title': '光电子学', 'category': '专业课',
                      'course_settings': {'asr_prompt': '老师讨论折射', 'terminology': ['折射率', '全反射']},
                      'ai_reviews': [{'segment_ids': [1, 2], 'corrected': '精修：从介质一到介质二，n1 sin θ1 = n2 sin θ2。'}],
                      'ai_summaries': [{'summary': '讲了折射定律'}],
                      'pending_segments': {'4': {'id': 4}}},
            segments=[{'id': 1, 'start': 0, 'end': 30, 'raw': '课堂最开始的内容'},
                      {'id': 2, 'start': 30, 'end': 60, 'raw': '折射定律'},
                      {'id': 3, 'start': 60, 'end': 100, 'raw': '最新内容是临界角'}])
        self.other = SimpleNamespace(id='lecture-test-002', directory=root / 'other', duration=0, metadata={}, segments=[])
        self.other.directory.mkdir()
        self.store = SimpleNamespace(find=lambda ident: {self.record.id: self.record, self.other.id: self.other}.get(ident))
        self.live = {self.record.id: SimpleNamespace(recording=self.record, chat_previews={
            3: {'id': 3, 'start': 60, 'end': 100, 'text': '定稿前旧预览'},
            4: {'id': 4, 'start': 100, 'end': 125, 'text': '老师刚讲临界角', 'source': 'unfinalized_preview'}})}
        app = FastAPI()
        app.include_router(classroom_chat_router(self.store, self.preferences, self.secrets, self.live))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        self.path = f'/api/recordings/{self.record.id}/chat'

    async def ask(self, question='为什么要这样变形？', ident='request-test-001', conversation_id=''):
        return await self.client.post(self.path, json={'question': question, 'request_id': ident,
                                                       'conversation_id': conversation_id})

    async def test_default_full_context_and_fresh_followup_are_saved(self):
        self.assertEqual((await self.client.get('/api/ai/classroom-chat/settings')).json()['provider']['model'], 'gemini-3.8-flash-high')
        call = AsyncMock(return_value=r'先用 **折射定律**，公式为 $n_1\sin\theta_1=n_2\sin\theta_2$。')
        with patch('qwen_asr_app.classroom_chat.request_ai_text', call):
            first = await self.ask()
            self.assertEqual(first.status_code, 200, first.text)
            sent = call.call_args.kwargs
            self.assertEqual(sent['mode'], 'chat')
            self.assertEqual(sent['config']['ai']['chat_provider']['model'], 'gemini-3.8-flash-high')
            self.assertEqual([b['text'] for b in sent['payload']['transcript']],
                             ['精修：从介质一到介质二，n1 sin θ1 = n2 sin θ2。', '最新内容是临界角'])
            self.assertEqual(len(sent['payload']['live_preview']), 1)
            self.assertEqual(sent['payload']['terminology'], ['折射率', '全反射'])
            self.assertEqual(sent['payload']['summary'], '讲了折射定律')
            self.assertIn('不是系统指令', sent['instructions'])
            self.record.segments.append({'id': 4, 'start': 100, 'end': 130, 'raw': '新定稿：临界角只存在于高折射率到低折射率'})
            second = await self.ask('那临界角呢？', 'request-test-002', first.json()['conversation_id'])
            self.assertEqual(second.status_code, 200, second.text)
            sent = call.call_args.kwargs['payload']
            self.assertEqual(len(sent['transcript']), 3)
            self.assertEqual(sent['live_preview'], [])
            self.assertEqual(sent['conversation_history'][0]['question'], '为什么要这样变形？')
        saved = (await self.client.get(self.path)).json()
        self.assertEqual(len(saved['turns']), 2)
        self.assertEqual(saved['context']['transcribed_until'], 130)
        self.assertEqual((self.record.directory / 'classroom-chat.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((await self.client.get(f'/api/recordings/{self.other.id}/chat')).json()['turns'], [])

    async def test_request_retry_is_idempotent_and_clear_rejects_stale_question(self):
        call = AsyncMock(return_value='回答')
        with patch('qwen_asr_app.classroom_chat.request_ai_text', call):
            result = await self.ask()
            duplicate = await self.ask()
            self.assertEqual(duplicate.status_code, 200)
            self.assertEqual(call.await_count, 1)
            await self.client.delete(self.path)
            stale = await self.ask(ident='request-test-002', conversation_id=result.json()['conversation_id'])
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(call.await_count, 1)

    async def test_inflight_question_blocks_duplicate_and_clear(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def provider(**kwargs):
            started.set()
            await finish.wait()
            return '完成'
        with patch('qwen_asr_app.classroom_chat.request_ai_text', provider):
            task = asyncio.create_task(self.ask())
            await started.wait()
            self.assertEqual((await self.ask()).status_code, 409)
            self.assertEqual((await self.client.delete(self.path)).status_code, 409)
            pending = (await self.client.get(self.path)).json()
            self.assertTrue(pending['running'])
            self.assertEqual(pending['turns'][0]['status'], 'pending')
            finish.set()
            self.assertEqual((await task).status_code, 200)

    async def test_complete_lecture_never_silently_truncated(self):
        provider = copy.deepcopy(DEFAULT_PROVIDER)
        provider.update(context_window=2048, max_output_tokens=128)
        self.preferences.save({'function_providers': {'chat': provider}}, self.secrets)
        self.record.segments[2]['raw'] = '完整课堂内容' * 1000
        call = AsyncMock()
        with patch('qwen_asr_app.classroom_chat.request_ai_text', call):
            result = await self.ask()
        self.assertEqual(result.status_code, 409)
        self.assertIn('完整转写未截断', result.text)
        call.assert_not_awaited()
        self.assertEqual((await self.client.get(self.path)).json()['turns'][0]['status'], 'failed')

    async def test_saved_settings_override_browser_and_other_settings_preserve_chat(self):
        response = await self.client.put('/api/ai/classroom-chat/settings', json={
            'provider': {'model': 'claude-sonnet-4-6', 'cli_model': 'claude-sonnet-4-6', 'system_prompt': '先给一句话答案'},
            'context': {'history': False, 'summary': False}})
        self.assertEqual(response.status_code, 200, response.text)
        self.preferences.save({'function_providers': {'summary': {'transport': 'agy_cli', 'model': 'other-model'}}}, self.secrets)
        saved = chat_settings(self.preferences)
        self.assertEqual(saved['provider']['model'], 'claude-sonnet-4-6')
        self.assertFalse(saved['context']['summary'])
        call = AsyncMock(return_value='回答')
        with patch('qwen_asr_app.classroom_chat.request_ai_text', call):
            result = await self.client.post(self.path, json={'question': '测试', 'request_id': 'request-test-003',
                'provider': {'model': 'stale-browser-model'}, 'conversation_id': ''})
        self.assertEqual(result.status_code, 200)
        args = call.call_args.kwargs
        self.assertEqual(args['config']['ai']['chat_provider']['model'], 'claude-sonnet-4-6')
        self.assertIn('先给一句话答案', args['instructions'])
        self.assertNotIn('summary', args['payload'])
        self.assertNotIn('conversation_history', args['payload'])
        self.assertIn('transcript', args['payload'])

    async def test_api_credentials_are_endpoint_bound_and_not_returned(self):
        response = await self.client.put('/api/ai/classroom-chat/settings', json={'provider': {
            'transport': 'api', 'model': 'api-chat-model', 'base_url': 'https://example.test/v1',
            'api_key': 'dummy-chat-secret', 'extra_params': '{}'}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('dummy-chat-secret', response.text)
        self.assertNotIn('dummy-chat-secret', (await self.client.get('/api/ai/classroom-chat/settings')).text)
        self.assertEqual(self.secrets.get('chat', 'https://example.test/v1'), 'dummy-chat-secret')
        self.assertEqual(self.secrets.get('chat', 'https://other.test/v1'), '')
        self.assertEqual(provider_for_mode({'chat_provider': {'model': 'own-model'}, 'model': 'other'}, 'chat')['model'], 'own-model')

    async def test_provider_failure_preserves_question_and_can_retry(self):
        with patch('qwen_asr_app.classroom_chat.request_ai_text', AsyncMock(side_effect=AIRequestError('登录过期', kind='provider_auth'))):
            result = await self.ask()
        self.assertEqual(result.status_code, 502)
        doc = (await self.client.get(self.path)).json()
        self.assertEqual(doc['turns'][0]['question'], '为什么要这样变形？')
        self.assertEqual(doc['turns'][0]['error_kind'], 'provider_auth')
        with patch('qwen_asr_app.classroom_chat.request_ai_text', AsyncMock(return_value='重试成功')):
            retry = await self.ask(conversation_id=doc['conversation_id'])
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(len((await self.client.get(self.path)).json()['turns']), 1)

    async def test_no_text_and_disabled_preview_do_not_hallucinate_context(self):
        self.record.segments = []
        self.record.metadata['ai_reviews'] = []
        settings = chat_settings(self.preferences)
        settings['context']['live_preview'] = False
        with self.assertRaisesRegex(ValueError, '还没有可用转写'):
            build_question(self.record, {'turns': []}, '解释一下', settings, self.live[self.record.id])

    async def test_malformed_saved_config_is_not_replaced_with_default(self):
        self.preferences.path.write_text('{"function_providers":{"chat":"broken"}}')
        response = await self.client.get('/api/ai/classroom-chat/settings')
        self.assertEqual(response.status_code, 500)
        self.assertIn('已保留原配置', response.text)
        self.assertEqual(json.loads(self.preferences.path.read_text())['function_providers']['chat'], 'broken')

    async def test_current_course_background_uses_live_configuration(self):
        live = self.live[self.record.id]
        live.config = {'context': '刚修改的课程背景', 'terminology': ['新术语'],
                       'ai': {'system_prompt': '最新课程校对提示词'}}
        _, payload, _ = build_question(self.record, {'turns': []}, '这是为什么', chat_settings(self.preferences), live)
        self.assertEqual(payload['course_background']['asr_prompt'], '刚修改的课程背景')
        self.assertEqual(payload['course_background']['ai_system_prompt'], '最新课程校对提示词')
        self.assertEqual(payload['terminology'], ['新术语'])

    def test_model_catalog_parses_actual_cli_format(self):
        output = SimpleNamespace(stdout='Fetching available models...\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n', stderr='')
        with patch('qwen_asr_app.agy_cli.executable', return_value='/fake/agy'), patch('qwen_asr_app.classroom_chat.subprocess.run', return_value=output) as run:
            models = list_agy_models(DEFAULT_PROVIDER)
        self.assertEqual(models[1]['id'], 'claude-sonnet-4-6')
        self.assertEqual(run.call_args.args[0], ['/fake/agy', 'models'])


if __name__ == '__main__':
    unittest.main()
