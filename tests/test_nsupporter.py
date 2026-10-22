import unittest
from unittest.mock import patch
import nsupporter


class NSupporterTests(unittest.TestCase):
    def test_counts_come_only_from_extension_word_analysis(self):
        refs = [{'sections': [
            {'heading': '검색 키워드 수 분석', 'fields': [{'label': '라멘', 'value': '999'}]},
            {'heading': '단어 수 분석 | 단어 2개', 'fields': [
                {'label': '라멘', 'value': '5'}, {'label': '혼밥', 'value': '2'}]}]},
            {'sections': [{'heading': '단어 수 분석', 'fields': [{'label': '라멘', 'value': '3'}]}]}]
        result = nsupporter.summarize(refs)
        self.assertEqual(result['keywords'][0], {'word': '라멘', 'count': 8, 'documents': 2})
        self.assertEqual(result['structures'], [])

    def test_closed_browser_returns_actionable_failure_without_api(self):
        with patch.object(nsupporter.socket, 'create_connection', side_effect=OSError()):
            result = nsupporter.collect('강남 멘츠루')
        self.assertIn('setup', result['error'])
        self.assertEqual(result['references'], [])
        self.assertEqual(result['analysis']['keywords'], [])

    def test_concurrent_browser_work_is_rejected(self):
        nsupporter.LOCK.acquire()
        try:
            result = nsupporter.collect('query')
            self.assertIn('다른 작업', result['error'])
            self.assertTrue(nsupporter.LOCK.locked())
        finally:
            nsupporter.LOCK.release()
