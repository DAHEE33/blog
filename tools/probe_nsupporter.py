"""엔서포터 화면 연동 가능성 검증. 앱의 자체 분석과 분리한다.

python tools/probe_nsupporter.py setup
  별도 시험용 Chrome을 열어 엔서포터 공식 설치 페이지를 표시한다.
python tools/probe_nsupporter.py probe
  시험용 Chrome의 현재 열린 페이지에서 보이는 분석 버튼·결과 텍스트를 확인한다.
로그인·CAPTCHA·권한 동의는 자동 처리하지 않는다.
"""
import argparse
import json
import sys
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from browser import PROFILE, ADDRESS, connect, ensure_browser, port_open  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'probe'])
    args = parser.parse_args()
    if args.action == 'setup':
        if port_open():
            print('전용 Chrome이 이미 열려 있어 재사용합니다. 엔서포터가 설치돼 있다면 추가 작업이 필요 없습니다.')
            print('엔서포터를 아직 설치하지 않았다면 그 창에서 직접 설치 페이지를 여세요: https://www.reviewerns.com/portal/lab/nsupporter/')
            return
        ensure_browser(print)
        driver = connect()
        if not any('reviewerns.com' in (driver.current_url or '') for _ in [0]):
            driver.switch_to.new_window('tab')
            driver.get('https://www.reviewerns.com/portal/lab/nsupporter/')
        print('전용 Chrome을 열었습니다. 엔서포터가 설치돼 있지 않다면 이 창에서 설치해 주세요. 이미 설치돼 있으면 바로 사용하면 됩니다.')
        driver.service.stop()
        return
    driver = connect()
    pages = []
    for handle in driver.window_handles:
        driver.switch_to.window(handle)
        url = driver.current_url
        if not url.startswith(('https://search.naver.com/', 'https://www.reviewerns.com/')):
            continue
        articles = []
        if url.startswith('https://search.naver.com/'):
            for panel in driver.find_elements(By.CSS_SELECTOR, '.nspt-post-box'):
                result = panel.find_elements(By.CSS_SELECTOR, '.nspt-js-result-blog')
                if not result or not result[0].is_displayed():
                    continue
                for button in panel.find_elements(By.CSS_SELECTOR, '.nspt-js-do-show-more'):
                    if button.is_displayed():
                        button.click()
                sections = []
                for section in panel.find_elements(By.CSS_SELECTOR, '.nspt-js-databox'):
                    heading = section.find_element(By.CSS_SELECTOR, 'h4').text
                    fields = []
                    for field in section.find_elements(By.CSS_SELECTOR, '._c-field'):
                        if not field.is_displayed():
                            continue
                        fields.append({'label': field.find_element(By.CSS_SELECTOR, '._c-field__label').text,
                                       'value': field.find_element(By.CSS_SELECTOR, '._c-field__value').text,
                                       'unit': field.find_element(By.CSS_SELECTOR, '._c-field__unit').text})
                    sections.append({'heading': heading, 'fields': fields})
                articles.append({'source': 'nsupporter_extension_visible_ui', 'sections': sections})
        visible = driver.find_element(By.TAG_NAME, 'body').text
        lines = visible.splitlines()
        selected = set()
        for index, line in enumerate(lines):
            if any(word in line.lower() for word in ('엔서포터', 'nsupporter', '단어 수 분석', '검색 키워드 수', '글자 수 분석')):
                selected.update(range(max(0, index - 1), min(len(lines), index + 25)))
        pages.append({'url': url, 'articles': articles, 'visible_analysis_text': '\n'.join(lines[i] for i in sorted(selected))[:15000]})
    out = ROOT / 'tmp' / 'nsupporter_probe.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'확인 페이지 {len(pages)}개. 결과: {out}')
    driver.service.stop()


if __name__ == '__main__':
    main()
