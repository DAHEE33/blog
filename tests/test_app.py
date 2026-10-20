import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

PHOTO = {'name': 'one.png', 'url': 'data:image/png;base64,iVBORw0KGgo='}
RESULT = {'titles': ['후기', '방문 기록', '식사'], 'body': '방문 메모 기반 본문\n[사진:1]', 'tags': ['후기'], 'photos': [{'index': 1, 'caption': '사진', 'reason': '대표 사진'}], 'checks': ['가격 확인'], 'style_summary': '짧은 해요체'}

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data = app.DATA
        app.DATA = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key', 'ANTHROPIC_MODEL': 'test-model'})
        self.env.start()
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.env.stop()
        app.DATA = self.old_data
        self.tmp.cleanup()

    def request(self, path, payload=None, token=True):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Studio-Token'] = app.TOKEN
        req = urllib.request.Request(self.base+path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        with urllib.request.urlopen(req) as res:
            return json.load(res)

    def test_settings_and_manual_draft_survive_reopen(self):
        self.request('/api/settings', {'engine': 'api', 'style': 'mine', 'examples': '내 글의 말투', 'rules': '과장 없이'})
        self.assertEqual(self.request('/api/state')['settings']['examples'], '내 글의 말투')
        draft = self.request('/api/save', {'place': '식당', 'photos': [PHOTO], 'notes': '방문함', 'result': RESULT})
        loaded = self.request('/api/drafts/'+draft['id'])
        self.assertEqual(loaded['photos'], [PHOTO])
        self.assertEqual(loaded['result']['body'], RESULT['body'])
        draft['result']['body'] = '직접 수정'
        self.request('/api/save', draft)
        self.assertEqual(len(self.request('/api/state')['drafts']), 1)
        self.assertEqual(self.request('/api/drafts/'+draft['id'])['result']['body'], '직접 수정')

    def test_real_generation_flow_with_mock_transport(self):
        self.request('/api/settings', {'engine': 'api', 'style': 'mine', 'examples': '기존 말투', 'rules': '이모지 없이'})
        calls = []
        original = urllib.request.urlopen
        def transport(req, *args, **kwargs):
            if req.full_url.startswith(self.base):
                return original(req, *args, **kwargs)
            calls.append(json.loads(req.data))
            return io.BytesIO(json.dumps({'content': [{'type': 'text', 'text': json.dumps(RESULT)}], 'stop_reason': 'end_turn'}).encode())
        with patch.object(app.urllib.request, 'urlopen', side_effect=transport):
            draft = self.request('/api/generate', {'place': '식당', 'notes': '간이 셌다', 'photos': [PHOTO]})
        self.assertEqual(draft['result']['body'], RESULT['body'])
        self.assertEqual(calls[0]['messages'][0]['content'][1]['source']['media_type'], 'image/png')
        self.assertIn('기존 말투', calls[0]['messages'][0]['content'][-1]['text'])
        self.assertEqual(len(self.request('/api/state')['drafts']), 1)

    def test_missing_credentials_and_token_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/save', {'place': '식당'}, token=False)
        self.assertEqual(caught.exception.code, 403)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': ''}):
            with self.assertRaisesRegex(ValueError, 'AI 연결 전'):
                app.generate({'place': '식당'}, {'engine': 'api'})

    def test_provider_failure_does_not_save_fake_draft(self):
        with patch.object(app.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError('https://api.anthropic.com', 401, 'bad', {}, None)):
            with self.assertRaisesRegex(ValueError, 'API 키'):
                app.generate({'place': '식당', 'photos': [PHOTO]}, {'engine': 'api'})
        with app.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM drafts').fetchone()[0], 0)

    def test_invalid_photo_rejected_and_invalid_reference_removed(self):
        with self.assertRaises(ValueError):
            app.validate_input({'place': '식당', 'photos': [{'url': 'data:text/html;base64,YQ=='}]})
        wrong = dict(RESULT, body='실제 사진 [사진:1] 잘못된 번호 [사진:999]', checks=[])
        result = app.parse_result(json.dumps(wrong), 1)
        self.assertNotIn('[사진:999]', result['body'])
        self.assertIn('[사진:1]', result['body'])
        self.assertTrue(result['checks'])
        with self.assertRaises(ValueError):
            app.parse_result('{"body": "test", "titles": []}', 0)

    def test_codex_adapter_images_stdin_and_no_shell(self):
        calls = []
        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            output = Path(command[command.index('--output-last-message')+1])
            output.write_text(json.dumps(RESULT), encoding='utf-8')
            image = Path(command[command.index('--image')+1])
            self.assertTrue(image.is_file())
            self.assertIn('資料; $(example)', kwargs['input'])
            self.assertNotIn('ANTHROPIC_API_KEY', kwargs['env'])
            self.assertFalse(kwargs['shell'])
            return SimpleNamespace(returncode=0)
        with patch.object(app.shutil, 'which', return_value='/mock/codex'), patch.object(app.subprocess, 'run', side_effect=fake_run):
            r = app.generate({'place': '식당', 'photos': [PHOTO], 'sources': '資料; $(example)'}, {'engine': 'codex'})
        self.assertEqual(r['body'], RESULT['body'])
        self.assertFalse(Path(calls[0][1]['cwd']).exists())
        self.assertIn('read-only', calls[0][0])

    def test_claude_adapter_restricts_tools_and_reads_images(self):
        def fake_run(command, **kwargs):
            self.assertIn('--restricted', command)
            self.assertEqual(command[command.index('--tools')+1], 'Read')
            self.assertIn('photo-1.png', kwargs['input'])
            self.assertNotIn('ANTHROPIC_API_KEY', kwargs['env'])
            return SimpleNamespace(returncode=0, stdout=json.dumps({'result': json.dumps(RESULT), 'is_error': False}))
        with patch.object(app.shutil, 'which', return_value='/mock/claude'), patch.object(app.subprocess, 'run', side_effect=fake_run):
            r = app.generate({'place': '식당', 'photos': [PHOTO]}, {'engine': 'claude'})
        self.assertEqual(r['titles'], RESULT['titles'])

    def test_missing_cli_no_fallback_to_paid_api(self):
        with patch.object(app.shutil, 'which', return_value=None), patch.object(app.urllib.request, 'urlopen') as upstream:
            with self.assertRaisesRegex(ValueError, 'CLI를 찾지'):
                app.generate({'place': '식당', 'photos': [PHOTO]}, {'engine': 'codex'})
            upstream.assert_not_called()

if __name__ == '__main__':
    unittest.main()
