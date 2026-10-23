import unittest
from unittest.mock import patch

import place


class FakeToggle:
    """모바일 홈의 영업시간 토글(role=button, aria-expanded)을 흉내 낸다. 클릭 시 펼쳐지고 요일 행이 생긴다."""
    def __init__(self, status_text, rows, expanded=False, nlog=False):
        self.status_text = status_text
        self.rows = rows
        self.expanded = expanded
        self.nlog = nlog
        self.clicks = 0

    @property
    def text(self):
        return self.status_text + ' 펼쳐보기' if not self.expanded else self.status_text + ' 접기'

    def matches(self, xpath):
        if '내용 더보기' in xpath:
            return False  # 찾아가는길 더보기 버튼이 아니다
        if 'bzhour' in xpath:
            return self.nlog
        if '펼쳐보기' in xpath:
            return ('펼쳐보기' in self.text or '접기' in self.text) and any(w in self.text for w in place.STATUS_WORDS + ('라스트오더', '영업시간'))
        return False


class FakeDriver:
    def __init__(self, toggle=None):
        self.toggle = toggle
        self.scripts = []

    def find_elements(self, by, xpath):
        return [self.toggle] if self.toggle is not None and self.toggle.matches(xpath) else []

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if script.startswith('arguments[0].click()'):
            el = args[0]
            el.clicks += 1
            el.expanded = not el.expanded  # 실제 토글처럼 다시 누르면 접힌다
            return None
        el = args[0]
        return {'expanded': 'true' if el.expanded else 'false', 'rows': list(el.rows) if el.expanded else [], 'text': el.text}


class PlaceParsingTests(unittest.TestCase):
    def test_candidates_deduped_and_multiple_kept_for_user_choice(self):
        links = [
            ('https://map.naver.com/p/entry/place/1103805198?lng=1', '올바른숙성회 신림본점'),
            ('https://map.naver.com/p/entry/place/1103805198?lng=2', ''),
            ('https://map.naver.com/p/entry/place/999?x=1', '올바른숙성회 봉천점'),
            ('https://map.naver.com/', '지도'),
        ]
        candidates = place.parse_candidates(links)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0], {'place_id': '1103805198', 'label': '올바른숙성회 신림본점'})

    def test_unique_best_match_is_auto_selected_others_ask_user(self):
        candidates = [
            {'place_id': '1', 'label': '올바른숙성회 신림본점'},
            {'place_id': '2', 'label': '올바른숙성회 봉천점'},
            {'place_id': '3', 'label': '해인어시장 신림본점'},
            {'place_id': '4', 'label': ''},
        ]
        chosen, shortlist = place.pick_candidate('신림 올바른 숙성회', candidates)
        self.assertEqual(chosen['place_id'], '1')  # 신림+올바른+숙성회 모두 일치하는 유일 후보
        self.assertTrue(shortlist)
        # 지점 구분이 안 되는 질의는 자동 선택하지 않는다.
        chosen, shortlist = place.pick_candidate('올바른 숙성회', candidates[:2])
        self.assertIsNone(chosen)
        self.assertEqual(len(shortlist), 2)

    def test_search_layout_names_replace_blank_roadview_links(self):
        links = [
            ('https://map.naver.com/p/entry/place/1199429613?p=roadview', ''),
            ('https://map.naver.com/p/search/강남%20멘츠루/place/1199429613?placePath=photo', ''),
            ('https://map.naver.com/p/search/강남%20멘츠루/place/1199429613?placePath=home', '멘츠루 신사점\n플레이스 플러스\n일본식라면'),
            ('https://map.naver.com/p/search/강남%20멘츠루/place/1285639217?placePath=home', '멘츠루 강남점\n쿠폰'),
            ('https://map.naver.com/p/search/강남%20멘츠루/place/1285639217?placePath=home', '리뷰이벤트 참여시 음료 증정 쿠폰'),
            ('https://example.com/p/entry/place/99', '가짜'),
        ]
        candidates = place.parse_candidates(links)
        self.assertEqual(candidates, [
            {'place_id': '1199429613', 'label': '멘츠루 신사점'},
            {'place_id': '1285639217', 'label': '멘츠루 강남점'}])
        chosen, _ = place.pick_candidate('강남 멘츠루', candidates)
        self.assertEqual(chosen['place_id'], '1285639217')

    def test_candidate_addresses_stay_with_their_own_place_id(self):
        candidates = place.parse_candidates([
            ('https://map.naver.com/p/search/멘츠루/place/1', '멘츠루 강남점', '서울 강남구 역삼동'),
            ('https://map.naver.com/p/entry/place/1', '', ''),
            ('https://map.naver.com/p/search/멘츠루/place/2', '멘츠루 신사점', '서울 강남구 신사동'),
        ])
        self.assertEqual(candidates[0]['address'], '서울 강남구 역삼동')
        self.assertEqual(candidates[1]['address'], '서울 강남구 신사동')

    def test_partial_region_match_does_not_auto_select_other_branch(self):
        candidates = [{'place_id': '1', 'label': '멘츠루 신사점'}]
        self.assertIsNone(place.pick_candidate('강남 멘츠루', candidates)[0])

    def test_read_candidates_waits_for_visible_names(self):
        driver = unittest.mock.Mock()
        driver.execute_script.return_value = [('https://map.naver.com/p/entry/place/1', '')]
        self.assertEqual(place.read_candidates(driver), [])
        driver.execute_script.return_value = [('https://map.naver.com/p/search/멘츠루/place/1', '멘츠루 강남점\n일본식라면')]
        self.assertEqual(place.read_candidates(driver)[0]['label'], '멘츠루 강남점')

    def test_home_parsing_extracts_only_visible_items(self):
        lines = ['저장', '올바른숙성회 신림본점', '영업 중다음 날 01:00에 라스트오더', '주소',
                 '서울 관악구 남부순환로 1597-6 1층지도내비게이션거리뷰', '찾아가는길']
        items = place.parse_home(lines, '올바른숙성회 신림본점 : 네이버', ['02-123-4567'], 'https://m.place.naver.com/restaurant/1/home')
        self.assertEqual(items['주소']['value'], '서울 관악구 남부순환로 1597-6 1층')
        self.assertIn('라스트오더', items['영업 상태·라스트오더']['value'])
        self.assertEqual(items['전화']['value'], '02-123-4567')
        self.assertEqual(items['매장명']['value'], '올바른숙성회 신림본점')
        self.assertNotIn('주차', items)  # 화면에 없으면 만들지 않는다
        self.assertNotIn('메뉴·가격', items)

    def test_menu_pairs_require_price_lines(self):
        menu = place.parse_menu(['메뉴', '모듬숙성회(중)', '59,000원', '매운탕', '12,000', '리뷰 465'], 'url')
        self.assertIn('모듬숙성회(중) 59,000원', menu['value'])
        self.assertIn('실제 결제 금액 아님', menu['value'])
        self.assertIsNone(place.parse_menu(['가격 없는 화면'], 'url'))

    def test_browser_start_failure_reports_error_without_bypass(self):
        failure = ValueError('전용 Chrome 자동 실행에 실패했습니다. python tools/probe_nsupporter.py setup 으로 직접 열어 주세요.')
        with patch.object(place, 'ensure_browser', side_effect=failure):
            report = place.collect_place('신림 올바른 숙성회')
        self.assertIn('setup', report['error'])
        self.assertEqual(report['items'], {})
        self.assertIn('주소', report['missing'])
        self.assertIn('로딩 실패', report['miss_reasons']['주소'])

    def test_week_hours_multiline_and_oneline_formats(self):
        lines = ['영업시간', '월', '16:00 - 02:00', '라스트오더 01:00', '화', '정기휴무 (매주 화요일)',
                 '수 16:00 - 02:00', '찾아가는길']
        detail = place.parse_week_hours(lines)
        self.assertIn('월: 16:00 - 02:00 · 라스트오더 01:00', detail['raw'])
        self.assertIn('화: 정기휴무 (매주 화요일)', detail['raw'])
        self.assertEqual(detail['days']['수']['hours'], ['16:00~02:00'])
        self.assertEqual(detail['days']['월']['last_order'], ['01:00'])
        self.assertTrue(detail['days']['화']['closed'])
        self.assertEqual(place.parse_week_hours(['주소', '서울 어딘가'])['days'], {})

    # ---- 펼치기 ----
    def test_expand_finds_toggle_in_before_open_status_and_verifies_rows(self):
        rows = [{'day': d, 'text': '16:00 - 다음 날 02:00\n01:00 라스트오더'} for d in ['금', '토', '일', '월', '화', '수', '목']]
        toggle = FakeToggle('영업 전16:00에 영업 시작', rows)  # 라스트오더 문구 없음, data-nlog 없음 → 텍스트 조건으로 찾아야 한다
        driver = FakeDriver(toggle)
        with patch.object(place.time, 'sleep', lambda s: None):
            result = place._expand_home(driver)
        self.assertTrue(result['toggle_found'])
        self.assertTrue(result['clicked'])
        self.assertEqual(toggle.clicks, 1)
        self.assertTrue(result['expanded'])
        self.assertEqual(len(result['rows']), 7)
        # 영업 중·영업 종료·휴무 상태에서도 같은 요소를 찾는다
        for status in ('영업 중다음 날 01:00에 라스트오더', '영업 종료', '휴무'):
            with patch.object(place.time, 'sleep', lambda s: None):
                self.assertTrue(place._expand_home(FakeDriver(FakeToggle(status, rows)))['toggle_found'], status)

    def test_expand_does_not_collapse_already_expanded_toggle(self):
        rows = [{'day': '월', 'text': '11:00 - 21:00'}]
        toggle = FakeToggle('영업 중', rows, expanded=True, nlog=True)
        driver = FakeDriver(toggle)
        with patch.object(place.time, 'sleep', lambda s: None):
            result = place._expand_home(driver)
        self.assertTrue(result['already_expanded'])
        self.assertFalse(result['clicked'])
        self.assertEqual(toggle.clicks, 0)
        self.assertTrue(toggle.expanded)
        self.assertEqual(result['rows'], rows)

    def test_expand_reports_missing_toggle_without_exception(self):
        with patch.object(place.time, 'sleep', lambda s: None):
            result = place._expand_home(FakeDriver(None))
        self.assertFalse(result['toggle_found'])
        self.assertFalse(result['expanded'])
        pages = [{'loaded': True, 'secured': False, 'lines': ['주소', '어딘가'], 'interaction': result}]
        self.assertIn('펼치기 요소를 찾지 못함', place.miss_reason('영업시간', pages))
        # 토글은 눌렀는데 상세가 안 나온 경우는 펼치기 실패로 안내하고, 보안 확인 탓으로 돌리지 않는다
        pages = [{'loaded': True, 'secured': True, 'lines': ['보안 확인'], 'interaction': {'toggle_found': True, 'expanded': False, 'rows': []}}]
        self.assertIn('상세 일정이 표시되지 않음', place.miss_reason('영업시간', pages))

    # ---- 정규화·묶기 ----
    def test_same_schedule_all_week_becomes_daily_and_preserves_next_day(self):
        rows = [{'day': d, 'text': '16:00 - 다음 날 02:00\n01:00 라스트오더'} for d in ['금', '토', '일', '월', '화', '수', '목']]
        lines = ['올바른숙성회 신림본점', '영업 전16:00에 영업 시작', '주소', '서울 관악구 남부순환로 1597-6 1층지도']
        items = place.parse_home(lines, '올바른숙성회 신림본점 : 네이버', [], 'home', rows=rows)
        self.assertEqual(items['영업시간']['value'], '매일 16:00~다음 날 02:00 / 라스트오더 01:00')
        self.assertEqual(items['영업 상태·라스트오더']['value'], '영업 전 · 16:00에 영업 시작 (조회 시점의 현재 상태)')
        detail = items['영업시간']['detail']
        self.assertEqual(detail['confirmed'], place.WEEK)
        self.assertEqual(detail['unconfirmed'], [])
        self.assertEqual(len(detail['raw']), 7)  # 요일별 원자료 보존(화면 순서 그대로)
        self.assertEqual(detail['raw'][0], '금: 16:00 - 다음 날 02:00 · 01:00 라스트오더')
        self.assertEqual(detail['days']['월']['hours'], ['16:00~다음 날 02:00'])
        self.assertEqual(items['영업시간']['source'], 'home')

    def test_weekday_weekend_difference_is_split(self):
        rows = [{'day': d, 'text': '11:00 - 21:00\n20:30 라스트오더'} for d in ['월', '화', '수', '목']]
        rows += [{'day': d, 'text': '11:00 - 22:00\n21:30 라스트오더'} for d in ['금', '토']]
        rows += [{'day': '일', 'text': '정기휴무 (매주 일요일)'}]
        value = place.format_hours(place.parse_week_hours([], rows=rows))
        self.assertEqual(value, '월~목 11:00~21:00 / 라스트오더 20:30\n금~토 11:00~22:00 / 라스트오더 21:30\n일요일 정기휴무 (매주 일요일)')

    def test_last_order_only_difference_is_not_merged(self):
        rows = [{'day': d, 'text': '11:00 - 21:00\n20:30 라스트오더'} for d in place.WEEK]
        rows[2] = {'day': '수', 'text': '11:00 - 21:00\n20:00 라스트오더'}
        value = place.format_hours(place.parse_week_hours([], rows=rows))
        self.assertEqual(value, '월~화·목~일 11:00~21:00 / 라스트오더 20:30\n수요일 11:00~21:00 / 라스트오더 20:00')
        self.assertNotIn('매일', value)

    def test_break_time_only_difference_is_not_merged(self):
        rows = [{'day': d, 'text': '11:00 - 21:00'} for d in place.WEEK]
        rows[5] = {'day': '토', 'text': '11:00 - 21:00\n15:00 - 17:00 브레이크타임'}
        value = place.format_hours(place.parse_week_hours([], rows=rows))
        self.assertEqual(value, '월~금·일 11:00~21:00\n토요일 11:00~21:00 / 브레이크타임 15:00~17:00')

    def test_closed_days_exceptions_and_multiple_ranges(self):
        rows = [{'day': d, 'text': '11:00 - 15:00\n17:00 - 21:00\n20:30 라스트오더'} for d in ['월', '화', '수', '목', '금']]
        rows += [{'day': '토', 'text': '11:00 - 21:00'}, {'day': '일', 'text': '정기휴무 (매주 일요일)'},
                 {'day': '공휴일', 'text': '휴무'}, {'day': '임시휴무', 'text': '10.25 (토) 임시휴무'}]
        detail = place.parse_week_hours([], rows=rows)
        value = place.format_hours(detail)
        self.assertEqual(value.split('\n')[0], '월~금 11:00~15:00, 17:00~21:00 / 라스트오더 20:30')
        self.assertIn('토요일 11:00~21:00', value)
        self.assertIn('일요일 정기휴무 (매주 일요일)', value)
        self.assertIn('별도 안내: 공휴일: 휴무', value)
        self.assertIn('별도 안내: 임시휴무 10.25 (토) 임시휴무', value)
        self.assertEqual(detail['days']['월']['hours'], ['11:00~15:00', '17:00~21:00'])
        self.assertTrue(detail['days']['일']['closed'])
        self.assertNotIn('공휴일', detail['days'])  # 특정일 예외는 기본 일정에 덮어쓰지 않는다
        self.assertEqual(detail['days']['토']['last_order'], [])  # 화면에 없는 라스트오더를 추측하지 않는다

    def test_non_consecutive_days_are_listed_exactly(self):
        rows = [{'day': d, 'text': '11:00 - 21:00'} for d in ['월', '수', '금']]
        rows += [{'day': d, 'text': '12:00 - 20:00'} for d in ['화', '목', '토', '일']]
        value = place.format_hours(place.parse_week_hours([], rows=rows))
        self.assertEqual(value, '월·수·금 11:00~21:00\n화·목·토~일 12:00~20:00')
        self.assertEqual(place.expand_day_label('월~금'), ['월', '화', '수', '목', '금'])
        self.assertEqual(place.expand_day_label('월·수·금'), ['월', '수', '금'])
        self.assertEqual(place.expand_day_label('매일'), place.WEEK)
        self.assertIsNone(place.expand_day_label('공휴일'))

    def test_partial_days_are_not_widened_to_daily(self):
        rows = [{'day': d, 'text': '16:00 - 다음 날 02:00\n01:00 라스트오더'} for d in ['월', '화', '수']]
        detail = place.parse_week_hours([], rows=rows)
        value = place.format_hours(detail)
        self.assertEqual(value, '월~수 16:00~다음 날 02:00 / 라스트오더 01:00\n미확인 요일: 목·금·토·일 (화면에서 확인되지 않음)')
        self.assertEqual(detail['days']['월']['last_order'], ['01:00'])
        self.assertNotIn('매일', value)
        # 화면이 '매일'로 표시한 경우만 매일이다
        daily = place.format_hours(place.parse_week_hours(['매일 11:00 - 21:00', '브레이크타임 15:00 - 17:00']))
        self.assertEqual(daily, '매일 11:00~21:00 / 브레이크타임 15:00~17:00')

    def test_status_line_is_separated_from_regular_hours(self):
        lines = ['영업 중다음 날 01:00에 라스트오더', '월', '16:00 - 다음 날 02:00', '01:00 라스트오더', '찾아가는길']
        items = place.parse_home(lines, 't : 네이버', [], 'u')
        self.assertEqual(items['영업 상태·라스트오더']['value'], '영업 중 · 다음 날 01:00에 라스트오더 (조회 시점의 현재 상태)')
        self.assertEqual(items['영업시간']['value'].split('\n')[0], '월요일 16:00~다음 날 02:00 / 라스트오더 01:00')
        # 요일별 '정기휴무' 줄이 현재 상태로 오인되지 않는다
        items = place.parse_home(['화', '정기휴무 (매주 화요일)', '주소', '어딘가'], 't : 네이버', [], 'u')
        self.assertNotIn('영업 상태·라스트오더', items)
        self.assertTrue(place.ANCHOR_TESTS['영업 상태·라스트오더']('영업 전16:00에 영업 시작'))

    def test_legacy_value_enriched_without_changing_value_or_edited(self):
        legacy = {'value': '월: 16:00 - 02:00 · 라스트오더 01:00\n화: 정기휴무 (매주 화요일)', 'source': 'url', 'checked_at': '2026-10-22', 'edited': False}
        enriched = place.enrich_hours(dict(legacy))
        self.assertEqual(enriched['value'], legacy['value'])
        self.assertEqual(enriched['checked_at'], '2026-10-22')
        self.assertEqual(enriched['detail']['summary'].split('\n')[0], '월요일 16:00~02:00 / 라스트오더 01:00')
        self.assertIn('미확인 요일: 수·목·금·토·일', enriched['detail']['summary'])
        edited = {'value': '매일 10시~22시', 'edited': True}
        self.assertNotIn('detail', place.enrich_hours(dict(edited)))

    def test_parking_notice_preserved_verbatim_without_interpretation(self):
        lines = ['찾아가는길', '주차 불가', '주변 공영주차장 안내를 참고해 주세요', '그 외 안내']
        parking = place.parse_parking(lines)
        self.assertIn('주차 불가', parking)
        self.assertIn('주변 공영주차장 안내', parking)
        self.assertNotIn('가능', parking.replace('공영주차장', ''))
        self.assertEqual(place.parse_parking(['주차 설명 없음 화면'.replace('주차 ', '')]), '')

    def test_miss_reasons_distinguish_causes(self):
        self.assertIn('로딩 실패', place.miss_reason('주소', [{'loaded': False}]))
        secured = [{'loaded': True, 'secured': True, 'lines': ['영업 중']}]
        self.assertIn('보안 확인', place.miss_reason('주소', secured))
        visible = [{'loaded': True, 'secured': False, 'lines': ['주소', '어딘가']}]
        self.assertIn('추출 실패', place.miss_reason('주소', visible))
        absent = [{'loaded': True, 'secured': False, 'lines': ['편의시설', '포장']}]
        self.assertIn('항목 없음', place.miss_reason('주차', absent))
        # 보안 확인 글자가 있어도 항목 흔적이 있으면 보안 확인 탓으로 돌리지 않는다.
        both = [{'loaded': True, 'secured': True, 'lines': ['주차 불가 안내가 보임']}]
        self.assertIn('추출 실패', place.miss_reason('주차', both))
