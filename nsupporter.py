"""설치된 엔서포터의 화면만 조작·읽는다. 비공개 API를 호출하지 않는다.

엔서포터 글 분석이 실제 제공하는 항목(2026-10-22 실측): 블로그 정보(수집 안 함),
글자 수 분석(전체/한글/영어/숫자), 검색 키워드 수 분석(띄어쓰기 일치·불일치와
검색어 단어별 본문 등장 횟수), 단어 수 분석(단어별 횟수), 외부링크 분석(도메인 수).
사진 수·제목 분석·소제목·문단 구성은 제공되지 않으므로 수집·표시하지 않는다.
"""
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlencode

from browser import connect, ensure_browser

LOCK = threading.Lock()
MAX_KEYWORDS = 3   # 목표 키워드 검색 상한. 초과분은 건너뛰고 안내한다.
MAX_POSTS = 3      # 검색당 채택하는 참고 글 수
MAX_ATTEMPTS = 5   # 검색당 엔서포터 분석을 시도하는 글 수 상한(무관 글 제외 대비)

NOTE = ('단어 횟수는 엔서포터 화면의 관측값 합산이며 검색량·경쟁도·권장 반복 횟수가 아님. '
        '검색 결과 전체나 네이버 최상단 순위를 대표하지 않음. 글 구성·소제목은 제공되지 않음.')


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_keywords(raw):
    """쉼표·줄바꿈 구분, 중복 제거. (검색할 키워드, 상한 초과로 건너뛴 키워드)를 반환."""
    items = [k.strip() for k in re.split(r'[,\n]', str(raw or '')) if k.strip()]
    unique = list(dict.fromkeys(items))
    return unique[:MAX_KEYWORDS], unique[MAX_KEYWORDS:]


def keyword_hits(sections):
    """검색 키워드 수 분석 섹션에서 (완전 문구 횟수, 단어별 본문 등장 횟수)를 읽는다."""
    for section in sections:
        if section['heading'].startswith('검색 키워드 수 분석'):
            phrase, hits = 0, {}
            for field in section['fields']:
                value = field['value'].replace(',', '')
                if not value.isdigit():
                    continue
                label = field['label']
                if label in ('띄어쓰기 일치', '띄어쓰기 불일치'):
                    phrase += int(value)
                elif len(label) >= 3 and label[0] == label[-1] == "'":
                    hits[label[1:-1]] = int(value)
            return phrase, hits
    return 0, {}


def word_labels(sections):
    for section in sections:
        if section['heading'].startswith('단어 수 분석'):
            return {field['label'] for field in section['fields']}
    return set()


def char_count(sections):
    for section in sections:
        if section['heading'].startswith('글자 수 분석'):
            for field in section['fields']:
                value = field['value'].replace(',', '')
                if field['label'] == '전체' and value.isdigit():
                    return int(value)
    return None


def classify(query, title, sections, reused=False):
    """관련도 판정: ('match'|'uncertain'|'excluded', 이유).
    제목과 엔서포터가 보여준 등장 횟수만 근거로 쓴다. 재사용 분석의 검색 키워드 수는
    다른 검색어 기준이므로 제목·단어 목록으로만 판정한다."""
    tokens = [t for t in str(query).split() if t]
    compact = re.sub(r'\s+', '', title or '')
    phrase, hits = (0, {}) if reused else keyword_hits(sections)
    words = word_labels(sections) if reused else set()
    found = [t for t in tokens if t in compact or hits.get(t, 0) > 0 or t in words]
    basis = '재사용 분석 · 제목과 단어 목록 기준' if reused else '제목과 본문 등장 횟수 기준'
    if tokens and (phrase > 0 or len(found) == len(tokens)):
        return 'match', f'검색어 단어가 모두 확인됨 ({basis})'
    if found:
        return 'uncertain', f"'{', '.join(found)}'만 확인됨 · 같은 매장(주제)인지 불확실 ({basis})"
    return 'excluded', f'검색어 단어가 제목·분석에서 확인되지 않아 제외 ({basis})'


def summarize(references):
    """excluded가 아닌, 분석을 확보한 글의 단어 수 분석만 집계한다."""
    counts, documents, chars = Counter(), Counter(), []
    for ref in references:
        if ref.get('relevance') == 'excluded' or not ref.get('analysis_available'):
            continue
        words = Counter()
        for section in ref.get('sections', []):
            if section['heading'].startswith('단어 수 분석'):
                for field in section['fields']:
                    value = field['value'].replace(',', '')
                    if value.isdigit():
                        words[field['label']] += int(value)
        counts.update(words)
        documents.update(words.keys())
        total = char_count(ref.get('sections', []))
        if total is not None:
            chars.append(total)
    return {'keywords': [{'word': word, 'count': count, 'documents': documents[word]}
                         for word, count in counts.most_common(50)],
            'char_counts': chars, 'structures': [], 'note': NOTE}


def read_sections(panel):
    from selenium.webdriver.common.by import By
    for button in panel.find_elements(By.CSS_SELECTOR, '.nspt-js-do-show-more'):
        if button.is_displayed():
            button.click()
    sections = []
    for box in panel.find_elements(By.CSS_SELECTOR, '.nspt-js-databox'):
        if not box.is_displayed():
            continue
        heading = box.find_element(By.CSS_SELECTOR, 'h4').text
        # 방문자 등 작성에 불필요한 개인 프로필 정보는 저장하지 않는다.
        if heading == '블로그 정보':
            continue
        fields = []
        for field in box.find_elements(By.CSS_SELECTOR, '._c-field'):
            if field.is_displayed():
                fields.append({key: field.find_element(By.CSS_SELECTOR, selector).text for key, selector in (
                    ('label', '._c-field__label'), ('value', '._c-field__value'), ('unit', '._c-field__unit'))})
        sections.append({'heading': heading, 'fields': fields})
    return sections


def select_references(candidates, query, cache, progress=None, label='검색'):
    """후보 글을 순서대로 분석·판정한다. 이미 분석한 URL은 재사용하고,
    excluded 글은 목록에 남기되 채택 수(MAX_POSTS)에 넣지 않는다."""
    progress = progress or (lambda message: None)
    refs, seen, attempts, included = [], set(), 0, 0
    for cand in candidates:
        if included >= MAX_POSTS or attempts >= MAX_ATTEMPTS:
            break
        url = cand['url']
        if url in seen:
            continue
        seen.add(url)
        ref = {'url': url, 'title': cand['title'], 'summary': '', 'search_source': 'nsupporter_browser',
               'query': query, 'analysis_available': False, 'reused': False,
               'fetched_at': now(), 'sections': [], 'relevance': '', 'reason': '', 'error': ''}
        cached = cache.get(url)
        if cached is not None:
            ref.update(sections=cached['sections'], analysis_available=cached['analysis_available'],
                       reused=True, error=cached['error'])
            relevance, reason = classify(query, ref['title'], ref['sections'], reused=True)
        else:
            attempts += 1
            progress(f'{label} · 엔서포터 글 분석 {attempts}번째 실행 중')
            try:
                ref['sections'] = cand['analyze']()
                ref['analysis_available'] = any(s['heading'].startswith('단어 수 분석') for s in ref['sections'])
            except Exception:
                ref['error'] = '엔서포터 글 분석 시간 초과 또는 화면 변경. 이 글은 건너뜁니다.'
            cache[url] = {'sections': ref['sections'], 'analysis_available': ref['analysis_available'], 'error': ref['error']}
            if ref['error']:
                ref['relevance'], ref['reason'] = 'excluded', '분석 실패로 제외'
                refs.append(ref)
                progress(ref['error'])
                continue
            relevance, reason = classify(query, ref['title'], ref['sections'])
        ref['relevance'], ref['reason'] = relevance, reason
        refs.append(ref)
        if relevance != 'excluded' and ref['analysis_available']:
            included += 1
    return refs


def _candidates_from_search(driver, query):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    driver.get('https://search.naver.com/search.naver?' + urlencode({'query': query}))
    wait = WebDriverWait(driver, 25)
    wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '.nspt-post-box'))
    for panel in driver.find_elements(By.CSS_SELECTOR, '.nspt-post-box'):
        parent = panel.find_element(By.XPATH, '..')
        link = next((a for a in parent.find_elements(By.CSS_SELECTOR, 'a[href]')
                     if re.fullmatch(r'https://blog\.naver\.com/[\w-]+/\d+', a.get_attribute('href') or '') and a.text.strip()), None)
        if link is None:
            continue

        def analyze(panel=panel, wait=wait):
            panel.find_element(By.CSS_SELECTOR, '.nspt-js-run').click()
            wait.until(lambda d: any(e.is_displayed() and '단어 수 분석' in e.text
                                     for e in panel.find_elements(By.CSS_SELECTOR, '.nspt-js-result-post')))
            return read_sections(panel)
        yield {'url': link.get_attribute('href'), 'title': link.text.replace('새 창 열림', '').strip(), 'analyze': analyze}


def collect_all(place, keywords_raw='', progress=None):
    """같은 매장 글 분석(상호 검색)과 목표 키워드별 글 분석을 분리해서 수집한다.
    일부 검색이 실패해도 나머지 그룹과 초안 작성은 계속 진행할 수 있게 그룹별로 기록한다."""
    progress = progress or (lambda message: None)
    place = str(place or '').strip()
    keywords, skipped = parse_keywords(keywords_raw)
    report = {'source': 'nsupporter_browser', 'fetched_at': now(), 'error': '', 'note': NOTE,
              'store': {'purpose': '같은 매장 글 분석', 'query': place, 'references': [], 'error': '', 'analysis': {}},
              'keywords': [{'purpose': '목표 키워드 글 분석', 'keyword': k, 'query': k,
                            'references': [], 'error': '', 'analysis': {}} for k in keywords],
              'skipped_keywords': skipped}
    if skipped:
        progress(f"목표 키워드는 최대 {MAX_KEYWORDS}개까지 검색합니다. 건너뜀: {', '.join(skipped)}")
    driver = None
    acquired = LOCK.acquire(blocking=False)
    try:
        if not acquired:
            raise ValueError('엔서포터 브라우저가 다른 작업을 처리 중입니다.')
        ensure_browser(progress)
        progress('엔서포터용 Chrome 연결 중 · 블로그 API는 사용하지 않음')
        driver = connect()
        driver.set_page_load_timeout(30)
        # 사용자 일반 탭 대신 전용 프로필의 검색 탭만 재사용한다.
        for handle in driver.window_handles:
            driver.switch_to.window(handle)
            if driver.current_url.startswith('https://search.naver.com/'):
                break
        else:
            driver.switch_to.new_window('tab')
        cache = {}
        groups = [(report['store'], place)] + [(g, g['query']) for g in report['keywords']]
        for group, query in groups:
            if not query:
                group['error'] = '검색어가 비어 있습니다.'
                continue
            label = f"{group['purpose']} '{query}'"
            progress(f'{label} · Chrome에서 네이버 자동 검색 중')
            try:
                group['references'] = select_references(_candidates_from_search(driver, query), query, cache, progress, label)
                if not group['references']:
                    group['error'] = '엔서포터로 분석할 블로그 글을 찾지 못했습니다.'
            except Exception:
                group['error'] = '검색·분석 실패. 확장 설치·브라우저 상태·접근 제한을 확인하세요. 이 검색은 건너뜁니다.'
                progress(f"{label}: {group['error']}")
    except Exception as exc:
        report['error'] = str(exc) if isinstance(exc, ValueError) else '엔서포터 연결·검색 실패. 확장 설치·브라우저 상태·접근 제한을 확인하세요.'
        progress(report['error'])
    finally:
        if driver is not None:
            driver.service.stop()
        if acquired:
            LOCK.release()
    report['store']['analysis'] = summarize(report['store']['references'])
    for group in report['keywords']:
        group['analysis'] = summarize(group['references'])
    return report
