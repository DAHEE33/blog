"""관련 글의 관측 단어 빈도와 정보 항목 분석. 검색량·순위 점수가 아니다."""
import re
from collections import Counter
from tools.verify_collect import search_naver_api, extract_body, now

STOP = set('그리고 하지만 그래서 저는 제가 오늘 정말 너무 있는 있어요 하는 합니다 입니다 이렇게 여기 저희 이번 바로 가장 다른 같은 위한 대한 이 그 수 것 등 및 안녕하세요 블로그 이웃 댓글 공감'.split())
SECTIONS = {'위치·접근': ('위치', '주소', '역', '출구'), '주차': ('주차',),
            '대기·예약': ('웨이팅', '대기', '예약'), '메뉴·가격': ('메뉴', '가격', '원'),
            '음식': ('음식', '맛', '식감'), '분위기': ('분위기', '인테리어', '좌석'),
            '총평': ('총평', '추천', '재방문')}

def analyze(references):
    total, documents = Counter(), Counter()
    structures = []
    for ref in references:
        text = ref.get('body_text', '')
        words = Counter(w.lower() for w in re.findall(r'[가-힣A-Za-z]{2,20}', text) if w not in STOP)
        total.update(words)
        documents.update(words.keys())
        positions = []
        for label, cues in SECTIONS.items():
            matches = [m.start() for cue in cues for m in re.finditer(re.escape(cue), text)]
            if matches:
                positions.append((min(matches), label))
        structures.append({'url': ref['url'], 'items': [label for _, label in sorted(positions)]})
    return {'keywords': [{'word': w, 'count': n, 'documents': documents[w]} for w, n in total.most_common(40)],
            'structures': structures,
            'note': '단어는 띄어쓰기 기반 관측 빈도(조사·어미 미분리). 구성은 정보 항목의 첫 언급 순서 추정이며 실제 소제목이 아님. 검색량·상위 노출 효과를 뜻하지 않음.'}

def collect(query, progress=None):
    progress = progress or (lambda message: None)
    result = {'query': query, 'fetched_at': now(), 'references': [], 'error': ''}
    try:
        progress('네이버 공식 API로 관련 글 검색 중')
        search = search_naver_api(query)
        progress(f"검색 응답: {len(search.get('items', []))}건" if search['ok'] else '검색 실패 · 사진과 메모로 계속 진행')
        if not search['ok']:
            result['error'] = search.get('error') or '검색 실패'
        for index, item in enumerate(search.get('items', [])[:3], 1):
            progress(f'참고 글 {index}/최대 3 본문 추출 중')
            try:
                body = extract_body(item['url'])
            except Exception:
                body = {'body_available': False, 'error': '본문 요청 처리 실패', 'fetched_at': now()}
            result['references'].append({**item, **body, 'search_source': 'naver_api_hub'})
            progress(f"참고 글 {index}: " + (f"본문 {body.get('body_chars', 0)}자 확보" if body.get('body_available') else '본문 확보 실패 · 건너뜀'))
    except Exception:
        result['error'] = '검색 처리 실패. 사진·메모로 작성을 계속합니다.'
    progress('단어 빈도·등장 글 수·정보 구성 분석 중')
    result['analysis'] = analyze(result['references'])
    return result
