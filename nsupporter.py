"""설치된 엔서포터의 화면만 조작·읽는다. 비공개 API를 호출하지 않는다."""
import re
import socket
import threading
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlencode

LOCK = threading.Lock()


def summarize(references):
    counts, documents = Counter(), Counter()
    for ref in references:
        words = Counter()
        for section in ref.get('sections', []):
            if section['heading'].startswith('단어 수 분석'):
                for field in section['fields']:
                    value = field['value'].replace(',', '')
                    if value.isdigit():
                        words[field['label']] += int(value)
        counts.update(words)
        documents.update(words.keys())
    return {'keywords': [{'word': word, 'count': count, 'documents': documents[word]}
                         for word, count in counts.most_common(50)],
            'structures': [],
            'note': '엔서포터 실제 화면의 단어 사용 횟수 합산. 검색량·권장 반복 횟수 아님. 글 구성·소제목은 이 분석에서 제공하지 않음. 사용자 사진·메모와 맞는 단어만 반영.'}


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


def collect(query, progress=None):
    progress = progress or (lambda message: None)
    report = {'query': query, 'source': 'nsupporter_browser', 'fetched_at': datetime.now(timezone.utc).isoformat(),
              'references': [], 'error': ''}
    driver = None
    acquired = LOCK.acquire(blocking=False)
    try:
        if not acquired:
            raise ValueError('엔서포터 브라우저가 다른 작업을 처리 중입니다.')
        try:
            with socket.create_connection(('127.0.0.1', 9223), timeout=2):
                pass
        except OSError:
            raise ValueError('엔서포터용 Chrome을 먼저 열어 주세요: python tools/probe_nsupporter.py setup') from None
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from tools.probe_nsupporter import connect
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
        progress('Chrome에서 네이버 자동 검색 중')
        driver.get('https://search.naver.com/search.naver?' + urlencode({'query': query}))
        wait = WebDriverWait(driver, 25)
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, '.nspt-post-box'))
        seen = set()
        for panel in driver.find_elements(By.CSS_SELECTOR, '.nspt-post-box'):
            if len(report['references']) >= 3:
                break
            parent = panel.find_element(By.XPATH, '..')
            candidates = parent.find_elements(By.CSS_SELECTOR, 'a[href]')
            link = next((a for a in candidates if re.fullmatch(r'https://blog\.naver\.com/[\w-]+/\d+', a.get_attribute('href') or '') and a.text.strip()), None)
            if link is None:
                continue
            url = link.get_attribute('href')
            if url in seen:
                continue
            seen.add(url)
            ref = {'url': url, 'title': link.text.replace('새 창 열림', '').strip(), 'summary': '',
                   'search_source': 'nsupporter_browser', 'body_available': False,
                   'analysis_available': False, 'fetched_at': datetime.now(timezone.utc).isoformat(),
                   'sections': [], 'error': ''}
            report['references'].append(ref)
            progress(f"엔서포터 글 분석 {len(report['references'])}/최대 3 실행 중")
            try:
                panel.find_element(By.CSS_SELECTOR, '.nspt-js-run').click()
                wait.until(lambda d: any(e.is_displayed() and '단어 수 분석' in e.text
                                        for e in panel.find_elements(By.CSS_SELECTOR, '.nspt-js-result-post')))
                ref['sections'] = read_sections(panel)
                ref['analysis_available'] = any(s['heading'].startswith('단어 수 분석') for s in ref['sections'])
                progress('엔서포터 단어 분석 결과 확보')
            except Exception:
                ref['error'] = '엔서포터 글 분석 시간 초과 또는 화면 변경. 이 글은 건너뜁니다.'
                progress(ref['error'])
        if not report['references']:
            report['error'] = '엔서포터로 분석할 블로그 글을 찾지 못했습니다.'
    except Exception as exc:
        report['error'] = str(exc) if isinstance(exc, ValueError) else '엔서포터 연결·검색 실패. 확장 설치·브라우저 상태·접근 제한을 확인하세요.'
        progress(report['error'])
    finally:
        if driver is not None:
            driver.service.stop()
        if acquired:
            LOCK.release()
    report['analysis'] = summarize(report['references'])
    return report
