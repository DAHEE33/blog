import json
import unittest
from unittest.mock import patch
import collect
import app


class CollectionTests(unittest.TestCase):
    def test_frequency_counts_documents_and_estimates_sections(self):
        result = collect.analyze([
            {'url': 'a', 'body_text': '주차 주차 메뉴 음식'},
            {'url': 'b', 'body_text': '메뉴 주차 분위기'},
        ])
        keyword = next(x for x in result['keywords'] if x['word'] == '주차')
        self.assertEqual((keyword['count'], keyword['documents']), (3, 2))
        self.assertEqual(result['structures'][1]['items'], ['메뉴·가격', '주차', '분위기'])

    def test_body_failure_preserves_summary_and_other_results(self):
        with patch.object(collect, 'search_naver_api', return_value={'ok': True, 'items': [
            {'url': 'a', 'summary': 'first'}, {'url': 'b', 'summary': 'second'}]}), patch.object(
                collect, 'extract_body', side_effect=[RuntimeError(), {'body_available': True, 'body_text': '주차 메뉴'}]):
            result = collect.collect('query')
        self.assertFalse(result['references'][0]['body_available'])
        self.assertEqual(result['references'][0]['summary'], 'first')
        self.assertTrue(result['references'][1]['body_available'])

    def test_only_analysis_enters_prompt_and_own_style_stays_separate(self):
        request = app.make_request({'place': '가게', 'collection': {'analysis': {'keywords': ['주차']},
                                   'references': [{'body_text': 'EXTERNAL_RAW_TEXT'}]}},
                                  {'examples': 'MY_STYLE_EXAMPLE'}, 'mock')
        payload = json.dumps(request, ensure_ascii=False)
        self.assertIn('MY_STYLE_EXAMPLE', payload)
        self.assertIn('주차', payload)
        self.assertNotIn('EXTERNAL_RAW_TEXT', payload)

    def test_search_error_returns_empty_analysis(self):
        with patch.object(collect, 'search_naver_api', return_value={'ok': False, 'items': [], 'error': 'HTTP 403'}):
            result = collect.collect('query')
        self.assertEqual(result['error'], 'HTTP 403')
        self.assertEqual(result['analysis']['keywords'], [])
