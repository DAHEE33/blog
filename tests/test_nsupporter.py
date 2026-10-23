import unittest
from unittest.mock import MagicMock, patch

import nsupporter


def make_sections(query="신림 올바른 숙성회", hits=(10, 8, 35), phrase=0, words=None, chars='1,805'):
    tokens = query.split()
    fields = [{'label': '띄어쓰기 일치', 'value': str(phrase), 'unit': '개'},
              {'label': '띄어쓰기 불일치', 'value': '0', 'unit': '개'}]
    fields += [{'label': f"'{t}'", 'value': str(n), 'unit': '개'} for t, n in zip(tokens, hits)]
    return [
        {'heading': '글자 수 분석', 'fields': [{'label': '전체', 'value': chars, 'unit': '자'}]},
        {'heading': f"검색 키워드 수 분석  |  '{query}'", 'fields': fields},
        {'heading': '단어 수 분석  |  단어 2개', 'fields': [
            {'label': '숙성회', 'value': '35', 'unit': '개'}, {'label': '메뉴', 'value': '19', 'unit': '개'}]},
        {'heading': '외부링크 분석', 'fields': [{'label': '도메인 전체', 'value': '0', 'unit': '개'}]},
    ]


class KeywordParsingTests(unittest.TestCase):
    def test_comma_newline_split_dedupe_and_cap(self):
        keywords, skipped = nsupporter.parse_keywords('신림 혼술, 신림 데이트\n신림 혼술\n신림 회포장, 신림 맛집')
        self.assertEqual(keywords, ['신림 혼술', '신림 데이트', '신림 회포장'])
        self.assertEqual(skipped, ['신림 맛집'])
        self.assertEqual(nsupporter.parse_keywords(''), ([], []))


class RelevanceTests(unittest.TestCase):
    def test_all_tokens_found_is_match(self):
        relevance, reason = nsupporter.classify('신림 올바른 숙성회', '숙성회 후기', make_sections())
        self.assertEqual(relevance, 'match')

    def test_exact_phrase_is_match_even_without_token_fields(self):
        sections = make_sections(hits=(0, 0, 0), phrase=2)
        self.assertEqual(nsupporter.classify('신림 올바른 숙성회', '후기', sections)[0], 'match')

    def test_partial_tokens_marked_uncertain(self):
        sections = make_sections(hits=(10, 0, 0))
        relevance, reason = nsupporter.classify('신림 올바른 숙성회', '다른 집 후기', sections)
        self.assertEqual(relevance, 'uncertain')
        self.assertIn('불확실', reason)

    def test_no_tokens_found_is_excluded(self):
        sections = make_sections(query='강남 멘츠루', hits=(0, 0))
        relevance, reason = nsupporter.classify('강남 멘츠루', '부천 칼국수 후기', sections)
        self.assertEqual(relevance, 'excluded')

    def test_title_match_counts_as_presence(self):
        sections = make_sections(query='강남 멘츠루', hits=(0, 0))
        relevance, _ = nsupporter.classify('강남 멘츠루', '강남 멘츠루 다녀온 날', sections)
        self.assertEqual(relevance, 'match')

    def test_reused_analysis_ignores_other_query_hits(self):
        # 재사용 분석의 검색 키워드 수는 다른 검색어 기준이므로 제목·단어 목록으로만 판정한다.
        sections = make_sections(query='신림 올바른', hits=(10, 8))
        self.assertEqual(nsupporter.classify('신림 올바른', '간 날', sections)[0], 'match')
        relevance, reason = nsupporter.classify('신림 올바른', '간 날', sections, reused=True)
        self.assertEqual(relevance, 'excluded')
        self.assertIn('재사용', reason)
        # 단어 목록에 있는 토큰은 재사용이어도 인정한다.
        relevance, _ = nsupporter.classify('숙성회 메뉴', '간 날', sections, reused=True)
        self.assertEqual(relevance, 'match')


class SelectReferencesTests(unittest.TestCase):
    def test_cached_url_is_reused_without_new_analysis(self):
        cache = {'https://blog.naver.com/a/1': {'sections': make_sections(), 'analysis_available': True, 'error': ''}}
        analyze = MagicMock(side_effect=AssertionError('재사용 시 분석을 다시 실행하면 안 됨'))
        refs = nsupporter.select_references(
            [{'url': 'https://blog.naver.com/a/1', 'title': '신림 올바른 숙성회 후기', 'analyze': analyze}],
            '신림 올바른 숙성회', cache)
        self.assertTrue(refs[0]['reused'])
        self.assertTrue(refs[0]['analysis_available'])
        analyze.assert_not_called()

    def test_excluded_posts_do_not_fill_quota(self):
        unrelated = make_sections(query='신림 올바른 숙성회', hits=(0, 0, 0))
        related = make_sections()
        candidates = [
            {'url': f'https://blog.naver.com/x/{i}', 'title': '무관한 글', 'analyze': lambda: unrelated}
            for i in range(2)
        ] + [
            {'url': f'https://blog.naver.com/y/{i}', 'title': '신림 올바른 숙성회', 'analyze': lambda: related}
            for i in range(3)
        ]
        refs = nsupporter.select_references(candidates, '신림 올바른 숙성회', {})
        included = [r for r in refs if r['relevance'] != 'excluded']
        self.assertEqual(len(included), 3)
        self.assertEqual(len([r for r in refs if r['relevance'] == 'excluded']), 2)

    def test_analysis_failure_recorded_and_next_candidate_used(self):
        def boom():
            raise TimeoutError()
        ok = make_sections()
        refs = nsupporter.select_references([
            {'url': 'https://blog.naver.com/a/1', 'title': '신림 올바른 숙성회', 'analyze': boom},
            {'url': 'https://blog.naver.com/a/2', 'title': '신림 올바른 숙성회', 'analyze': lambda: ok},
        ], '신림 올바른 숙성회', {})
        self.assertIn('건너뜁니다', refs[0]['error'])
        self.assertEqual(refs[0]['relevance'], 'excluded')
        self.assertTrue(refs[1]['analysis_available'])


class SummarizeTests(unittest.TestCase):
    def test_counts_come_only_from_included_word_analysis(self):
        refs = [
            {'relevance': 'match', 'analysis_available': True, 'sections': make_sections()},
            {'relevance': 'excluded', 'analysis_available': True, 'sections': make_sections()},
            {'relevance': 'match', 'analysis_available': False, 'sections': []},
        ]
        result = nsupporter.summarize(refs)
        self.assertEqual(result['keywords'][0], {'word': '숙성회', 'count': 35, 'documents': 1})
        self.assertEqual(result['char_counts'], [1805])
        self.assertEqual(result['structures'], [])


class CollectAllTests(unittest.TestCase):
    def test_browser_start_failure_returns_actionable_error_without_api(self):
        failure = ValueError('전용 Chrome 자동 실행에 실패했습니다. python tools/probe_nsupporter.py setup 으로 직접 열어 주세요.')
        with patch.object(nsupporter, 'ensure_browser', side_effect=failure):
            result = nsupporter.collect_all('신림 올바른 숙성회', '신림 혼술')
        self.assertIn('setup', result['error'])
        self.assertEqual(result['store']['references'], [])
        self.assertEqual(result['keywords'][0]['keyword'], '신림 혼술')
        self.assertEqual(result['store']['analysis']['keywords'], [])

    def test_concurrent_browser_work_is_rejected(self):
        nsupporter.LOCK.acquire()
        try:
            result = nsupporter.collect_all('식당')
            self.assertIn('다른 작업', result['error'])
            self.assertTrue(nsupporter.LOCK.locked())
        finally:
            nsupporter.LOCK.release()

    def test_store_failure_does_not_block_keyword_groups(self):
        driver = MagicMock(window_handles=[])
        related = make_sections(query='신림 혼술', hits=(3, 2))

        def fake_candidates(_driver, query):
            if query == '신림 올바른 숙성회':
                raise RuntimeError('검색 화면 변경')
            return iter([{'url': 'https://blog.naver.com/k/1', 'title': '신림 혼술 후기', 'analyze': lambda: related}])
        with patch.object(nsupporter, 'ensure_browser'), \
             patch.object(nsupporter, 'connect', return_value=driver), \
             patch.object(nsupporter, '_candidates_from_search', side_effect=fake_candidates):
            result = nsupporter.collect_all('신림 올바른 숙성회', '신림 혼술')
        self.assertIn('건너뜁니다', result['store']['error'])
        self.assertEqual(result['store']['references'], [])
        keyword_group = result['keywords'][0]
        self.assertEqual(keyword_group['references'][0]['relevance'], 'match')
        self.assertTrue(keyword_group['analysis']['keywords'])
        self.assertEqual(result['error'], '')

    def test_same_post_analysis_reused_across_groups(self):
        driver = MagicMock(window_handles=[])
        sections = make_sections()
        calls = []

        def analyze():
            calls.append(1)
            return sections

        def fake_candidates(_driver, query):
            return iter([{'url': 'https://blog.naver.com/same/1', 'title': '신림 올바른 숙성회 혼술', 'analyze': analyze}])
        with patch.object(nsupporter, 'ensure_browser'), \
             patch.object(nsupporter, 'connect', return_value=driver), \
             patch.object(nsupporter, '_candidates_from_search', side_effect=fake_candidates):
            result = nsupporter.collect_all('신림 올바른 숙성회', '신림 혼술')
        self.assertEqual(len(calls), 1)
        self.assertFalse(result['store']['references'][0]['reused'])
        self.assertTrue(result['keywords'][0]['references'][0]['reused'])
