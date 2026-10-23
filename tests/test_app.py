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
import place

PHOTO = {'name': 'one.png', 'url': 'data:image/png;base64,iVBORw0KGgo='}
RESULT = {'titles': ['후기', '방문 기록', '식사'], 'body': '방문 메모 기반 본문\n[사진:1]', 'tags': ['후기'], 'photos': [{'index': 1, 'caption': '사진', 'reason': '대표 사진'}], 'checks': ['가격 확인'], 'style_summary': '짧은 해요체'}

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data = app.DATA
        app.DATA = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key', 'ANTHROPIC_MODEL': 'test-model'})
        self.env.start()
        # 테스트에서 실제 브라우저 매장 조사를 실행하지 않는다.
        self.place_patch = patch.object(app, 'collect_place', return_value={
            'query': '', 'place_id': '', 'items': {}, 'missing': [], 'candidates': [],
            'notices': [], 'error': '테스트: 조사 생략', 'fetched_at': '2026-10-22T00:00:00+00:00'})
        self.place_patch.start()
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.place_patch.stop()
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

    def test_length_goal_prefers_user_target_over_reference_median(self):
        refs = [1805, 596, 1142]
        collection = {'store': {'analysis': {'char_counts': refs}}}
        self.assertEqual(app.length_goal({'target_chars': '1500', 'min_chars': '800'}, collection), (800, 1500, refs, False))
        self.assertEqual(app.length_goal({}, collection), (1000, 1142, refs, True))  # 중앙값, 기본 최소 1000
        self.assertEqual(app.length_goal({'min_chars': '1500'}, collection), (1500, 1500, refs, True))  # 참고값이 최소를 낮추지 않음
        self.assertEqual(app.length_goal({}, {}), (1000, 0, [], False))
        self.assertEqual(app.content_chars('안녕 hello 123! [사진:1] .'), 12)  # 공백 제외, 사진 표시 제외
        self.assertEqual(app.nsupporter_chars('안녕 hello 123! .'), 10)  # 엔서포터 기준: 한글·영문·숫자

    def test_finalize_flags_shortfall_ungrounded_claims_and_drops_duplicate_caption(self):
        def fresh():
            return {'body': '생일날 서비스로 받은 회\n[사진:1]\n기본안주 과자와 백김치가 나와요.', 'checks': [],
                    'photos': [{'index': 1, 'caption': '기본안주 과자와 백김치'}], 'titles': ['제목'], 'tags': []}
        p = {'place': '식당', 'notes': '회를 먹었다'}  # 생일·서비스 근거 없음
        out = app.finalize_result(fresh(), p, {'min_chars': '500'})
        self.assertTrue(out['length']['actual'] < 500)
        joined = ' '.join(out['checks'])
        self.assertIn('미달', joined)
        self.assertIn("'생일'", joined)
        self.assertIn("'서비스'", joined)
        # 중복 캡션은 경고만 남기지 않고 생략 처리한다.
        self.assertEqual(out['photos'][0]['caption'], '')
        self.assertIn('캡션을 생략', joined)
        # 메모에 근거가 있으면 표현 플래그가 없어야 한다.
        grounded = app.finalize_result(fresh(), {'place': '식당', 'notes': '생일날 방문, 후토마끼는 서비스'}, {'min_chars': '1'})
        joined = ' '.join(grounded['checks'])
        self.assertNotIn("'생일'", joined)
        self.assertNotIn("'서비스'", joined)
        self.assertNotIn('미달', joined)
        # 최소를 충족해도 목표의 80% 미만이면 안내한다.
        advisory = app.finalize_result(fresh(), {'place': '식당', 'notes': '생일 서비스'}, {'min_chars': '1', 'target_chars': '2000'})
        self.assertTrue(any('목표 2000자보다 짧습니다' in c for c in advisory['checks']))

    def test_short_draft_triggers_single_retry_with_facts_only_revision(self):
        self.request('/api/settings', {'engine': 'api', 'style': 'friendly'})  # 최소 기본 1000자
        long_body = '확보된 사실을 더 자세히 쓴 본문입니다. ' * 60 + '[사진:1]'
        replies = [dict(RESULT), dict(RESULT, body=long_body)]
        calls = []
        def transport(req, *args, **kwargs):
            calls.append(json.loads(req.data))
            return io.BytesIO(json.dumps({'content': [{'type': 'text', 'text': json.dumps(replies[len(calls) - 1])}], 'stop_reason': 'end_turn'}).encode())
        with patch.object(app.urllib.request, 'urlopen', side_effect=transport):
            result = app.generate({'place': '식당', 'notes': '간이 셌다', 'photos': [PHOTO]}, {'engine': 'api', 'style': 'friendly'})
        self.assertEqual(len(calls), 2)
        self.assertIn('이미 있는 사실만', calls[1]['messages'][0]['content'][-1]['text'])
        self.assertEqual(result['body'], long_body)
        self.assertTrue(any('보완 작성' in c for c in result['checks']))
        self.assertTrue(result['length']['actual'] >= 1000)

    def test_mine_style_without_examples_falls_back_without_error(self):
        payload = app.make_request({'place': '식당', 'notes': '메모', 'photos': []}, {'style': 'mine', 'examples': ''}, 'model')
        self.assertIn('기본 후기체 적용', payload['messages'][0]['content'][-1]['text'])

    def test_place_store_edit_refresh_and_draft_snapshot_preserved(self):
        fetched = {'query': '신림 올바른 숙성회', 'place_id': '1103805198', 'fetched_at': '2026-10-22T00:00:00+00:00',
                   'items': {'주소': {'value': '서울 관악구 남부순환로 1597-6 1층', 'source': 'https://m.place.naver.com/restaurant/1103805198/home', 'checked_at': '2026-10-22T00:00:00+00:00', 'edited': False}},
                   'missing': [], 'candidates': [], 'notices': [], 'error': ''}
        with patch.object(app, 'collect_place', return_value=dict(fetched, items=dict(fetched['items']))):
            info = self.request('/api/place', {'place': '신림 올바른 숙성회'})
        self.assertEqual(info['items']['주소']['value'], '서울 관악구 남부순환로 1597-6 1층')
        # 수정 저장 → edited 표시, 저장본 재사용
        edited = self.request('/api/place', {'place': '신림 올바른 숙성회', 'edit': {'주차': '가게 앞 2대'}})
        self.assertTrue(edited['items']['주차']['edited'])
        again = self.request('/api/place', {'place': '신림 올바른 숙성회'})
        self.assertEqual(again['items']['주차']['value'], '가게 앞 2대')
        # 재조회 시 사용자 수정값 유지 + 새 값과 다르면 충돌 표시
        conflict = dict(fetched, items={'주차': {'value': '불가', 'source': 'url', 'checked_at': 'x', 'edited': False},
                                        '주소': dict(fetched['items']['주소'])})
        with patch.object(app, 'collect_place', return_value=conflict):
            refreshed = self.request('/api/place', {'place': '신림 올바른 숙성회', 'refresh': True})
        self.assertEqual(refreshed['items']['주차']['value'], '가게 앞 2대')
        self.assertEqual(refreshed['items']['주차']['conflict_with']['value'], '불가')
        # 재조회에서 안 보인 항목(전화 등 과거 수집값)은 지우지 않고 유지한다.
        before_phone = self.request('/api/place', {'place': '신림 올바른 숙성회', 'edit': {'전화': ''}})  # 초기화
        self.request('/api/place', {'place': '신림 올바른 숙성회', 'edit': {}})
        with patch.object(app, 'collect_place', return_value=dict(fetched, items={})):
            kept = self.request('/api/place', {'place': '신림 올바른 숙성회', 'refresh': True})
        self.assertEqual(kept['items']['주소']['value'], '서울 관악구 남부순환로 1597-6 1층')
        self.assertTrue(any('과거 값' in n for n in kept['notices']))
        # 초안에는 작성 당시 정보가 보관되고, 이후 매장 정보 수정에 영향받지 않는다.
        self.request('/api/settings', {'engine': 'api', 'style': 'friendly'})
        original = urllib.request.urlopen
        def transport(req, *args, **kwargs):
            if req.full_url.startswith(self.base):
                return original(req, *args, **kwargs)
            return io.BytesIO(json.dumps({'content': [{'type': 'text', 'text': json.dumps(dict(RESULT, body='본문 ' * 600))}], 'stop_reason': 'end_turn'}).encode())
        with patch.object(app.urllib.request, 'urlopen', side_effect=transport):
            draft = self.request('/api/generate', {'place': '신림 올바른 숙성회', 'notes': '방문', 'photos': [], 'auto_place': True})
        self.assertEqual(draft['place_info']['items']['주차']['value'], '가게 앞 2대')
        self.request('/api/place', {'place': '신림 올바른 숙성회', 'edit': {'주차': '발렛만 가능'}})
        reloaded = self.request('/api/drafts/' + draft['id'])
        self.assertEqual(reloaded['place_info']['items']['주차']['value'], '가게 앞 2대')

    def test_hours_legacy_value_kept_and_user_edit_survives_refresh(self):
        legacy_hours = {'value': '월: 16:00 - 02:00 · 라스트오더 01:00\n화: 정기휴무 (매주 화요일)', 'source': 'home', 'checked_at': '2026-10-22T00:00:00+00:00', 'edited': False}
        legacy = {'query': '신림 올바른 숙성회', 'place_id': '1103805198', 'fetched_at': '2026-10-22T00:00:00+00:00',
                  'items': {'영업시간': dict(legacy_hours)}, 'missing': [], 'candidates': [], 'notices': [], 'error': ''}
        app.save_place(legacy)
        loaded = self.request('/api/place', {'place': '신림 올바른 숙성회'})
        hours = loaded['items']['영업시간']
        self.assertEqual(hours['value'], legacy_hours['value'])  # 과거 값·확인일은 그대로
        self.assertEqual(hours['checked_at'], legacy_hours['checked_at'])
        self.assertEqual(hours['detail']['raw'], ['월: 16:00 - 02:00 · 라스트오더 01:00', '화: 정기휴무 (매주 화요일)'])
        self.assertIn('미확인 요일: 수·목·금·토·일', hours['detail']['summary'])
        # 새 조회는 묶은 요약 + 요일별 원자료를 저장하고 AI 요청에도 같은 자료를 전달한다
        rows = [{'day': d, 'text': '16:00 - 다음 날 02:00\n01:00 라스트오더'} for d in ['금', '토', '일', '월', '화', '수', '목']]
        fresh_items = place.parse_home(['영업 전16:00에 영업 시작'], 't : 네이버', [], 'home', rows=rows)
        fresh = dict(legacy, items=fresh_items, fetched_at='2026-10-23T00:00:00+00:00')
        with patch.object(app, 'collect_place', return_value=fresh):
            refreshed = self.request('/api/place', {'place': '신림 올바른 숙성회', 'refresh': True})
        self.assertEqual(refreshed['items']['영업시간']['value'], '매일 16:00~다음 날 02:00 / 라스트오더 01:00')
        self.assertEqual(len(refreshed['items']['영업시간']['detail']['raw']), 7)
        payload = app.make_request({'place': '신림 올바른 숙성회', 'notes': '메모', 'photos': [], 'place_info': refreshed}, {'style': 'friendly'}, 'model')
        sent = payload['messages'][0]['content'][-1]['text']
        self.assertIn('매일 16:00~다음 날 02:00 / 라스트오더 01:00', sent)
        self.assertIn('금: 16:00 - 다음 날 02:00 · 01:00 라스트오더', sent)
        # 사용자가 직접 고친 값은 재조회해도 유지되고 새 값은 충돌로만 표시된다
        self.request('/api/place', {'place': '신림 올바른 숙성회', 'edit': {'영업시간': '매일 17:00~01:00 (전화 확인)'}})
        with patch.object(app, 'collect_place', return_value=dict(fresh, items=dict(fresh_items))):
            again = self.request('/api/place', {'place': '신림 올바른 숙성회', 'refresh': True})
        self.assertEqual(again['items']['영업시간']['value'], '매일 17:00~01:00 (전화 확인)')
        self.assertTrue(again['items']['영업시간']['edited'])
        self.assertNotIn('detail', again['items']['영업시간'])
        self.assertEqual(again['items']['영업시간']['conflict_with']['value'], '매일 16:00~다음 날 02:00 / 라스트오더 01:00')

    def test_settings_store_length_goals(self):
        self.request('/api/settings', {'engine': 'api', 'style': 'friendly', 'min_chars': '800', 'target_chars': '1500'})
        saved = self.request('/api/state')['settings']
        self.assertEqual(saved['min_chars'], 800)
        self.assertEqual(saved['target_chars'], 1500)

    def test_placeholder_info_lines_removed_from_body(self):
        body = ('- 상호: 올바른 숙성회 (신림)\n- 주소: (지도 앱에서 확인 후 입력)\n'
                '- 영업시간: (방문 전 확인 후 입력)\n- 전화번호: (확인 후 입력)\n\n\n숙성 광어가 쫄깃했어요.')
        result = app.parse_result(json.dumps(dict(RESULT, body=body, checks=[])), 1)
        self.assertNotIn('확인 후 입력', result['body'])
        self.assertIn('상호: 올바른 숙성회 (신림)', result['body'])   # 실제 값이 있는 줄은 유지
        self.assertIn('숙성 광어가 쫄깃했어요.', result['body'])
        self.assertNotIn('\n\n\n', result['body'])
        self.assertTrue(any('빈칸 안내' in c for c in result['checks']))

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

    def test_generation_continues_with_failed_collection_and_separates_purposes(self):
        self.request('/api/settings', {'engine': 'api', 'style': 'friendly', 'examples': '', 'rules': ''})
        failed = {'source': 'nsupporter_browser', 'error': '', 'note': '관측값',
                  'store': {'purpose': '같은 매장 글 분석', 'query': '신림 올바른 숙성회', 'references': [], 'error': '검색·분석 실패', 'analysis': {'keywords': [], 'char_counts': []}},
                  'keywords': [{'purpose': '목표 키워드 글 분석', 'keyword': '신림 혼술', 'query': '신림 혼술', 'error': '',
                                'references': [{'title': '혼술 후기', 'url': 'https://blog.naver.com/k/1', 'relevance': 'match', 'reason': '확인', 'analysis_available': True}],
                                'analysis': {'keywords': [{'word': '혼술', 'count': 3, 'documents': 1}], 'char_counts': [900]}}],
                  'skipped_keywords': []}
        calls = []
        original = urllib.request.urlopen
        def transport(req, *args, **kwargs):
            if req.full_url.startswith(self.base):
                return original(req, *args, **kwargs)
            calls.append(json.loads(req.data))
            return io.BytesIO(json.dumps({'content': [{'type': 'text', 'text': json.dumps(RESULT)}], 'stop_reason': 'end_turn'}).encode())
        with patch.object(app, 'collect_all', return_value=failed), patch.object(app.urllib.request, 'urlopen', side_effect=transport):
            draft = self.request('/api/generate', {'place': '신림 올바른 숙성회', 'notes': '쫄깃했다', 'photos': [PHOTO], 'auto_analyze': True, 'keyword': '신림 혼술'})
        self.assertEqual(draft['result']['body'], RESULT['body'])
        self.assertEqual(draft['collection']['store']['error'], '검색·분석 실패')
        sent = calls[0]['messages'][0]['content'][-1]['text']
        self.assertIn('같은 매장 참고 글 분석', sent)
        self.assertIn('목표 키워드별 참고 글 분석', sent)
        self.assertIn('신림 혼술', sent)

    def test_external_analysis_keeps_legacy_shape_and_drops_excluded(self):
        legacy = {'analysis': {'keywords': [{'word': '라멘', 'count': 5, 'documents': 1}]}}
        self.assertEqual(app.external_analysis(legacy), legacy['analysis'])
        split = {'store': {'query': '가게', 'references': [
            {'title': '포함', 'relevance': 'match', 'reason': ''},
            {'title': '빠져야 함', 'relevance': 'excluded', 'reason': ''}], 'analysis': {}, 'error': ''}, 'keywords': []}
        view = app.external_analysis(split)
        titles = [r['제목'] for r in view['같은 매장 참고 글 분석(이 매장 글의 관측 단어·분량 참고 전용)']['참고 글 제목(복제 금지)']]
        self.assertEqual(titles, ['포함'])

    def test_missing_cli_no_fallback_to_paid_api(self):
        with patch.object(app, 'find_cli', return_value=None), patch.object(app.urllib.request, 'urlopen') as upstream:
            with self.assertRaisesRegex(ValueError, 'CLI를 찾지'):
                app.generate({'place': '식당', 'photos': [PHOTO]}, {'engine': 'codex'})
            upstream.assert_not_called()

if __name__ == '__main__':
    unittest.main()
