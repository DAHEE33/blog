"""네이버 플레이스 화면을 전용 Chrome으로 읽어 매장 기본정보를 수집한다.

직접 HTTP 요청은 429 차단과 robots 금지가 실측 확인되어 사용하지 않는다(2026-10-22).
보안 확인(캡차)·로그인은 우회하지 않는다. 보안 확인이 작업을 막으면 그 탭을 닫지 않고
열어 두어 사용자가 직접 완료한 뒤 재조회할 수 있게 한다.
수집 항목은 화면에 실제로 보이는 값만 쓰며, 누락 사유를 보안 확인/로딩 실패/항목 없음/
추출 실패로 구분해 기록한다.
"""
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

from browser import connect, ensure_browser
from nsupporter import LOCK  # 전용 브라우저를 엔서포터 수집과 동시에 쓰지 않도록 잠금을 공유한다.

LABELS = ['매장명', '주소', '영업 상태·라스트오더', '영업시간', '전화', '주차', '메뉴·가격', '지도 링크']
WEEK = ['월', '화', '수', '목', '금', '토', '일']
DAY_LINE = re.compile(r'^(매일|[월화수목금토일])(요일)?$')
# 요일 라벨: '월', '월요일', '매일', '공휴일', '월~금', '월·수·금' (네이버 화면은 보통 요일 하나씩 표시한다)
DAY_LABEL = re.compile(r'^(매일|공휴일|[월화수목금토일](요일)?(\s*[~\-–·,]\s*[월화수목금토일](요일)?)*)$')
TIME_TEXT = re.compile(r'\d{1,2}:\d{2}')
TIME_RANGE = re.compile(r'(다음\s*날\s*)?(\d{1,2}:\d{2})\s*[-~–]\s*(다음\s*날\s*)?(\d{1,2}:\d{2})')
NEXT_DAY_TIME = re.compile(r'(다음\s*날\s*)?(\d{1,2}:\d{2})')
# 조회 시점의 현재 상태 문구. '영업 전'도 포함한다(2026-10-23 실화면: '영업 전16:00에 영업 시작').
STATUS_WORDS = ('곧 영업 종료', '곧 영업 시작', '영업 전', '영업 중', '영업 종료', '브레이크타임', '임시휴무', '휴무')
STATUS_LINE = re.compile(r'^(' + '|'.join(re.escape(w) for w in STATUS_WORDS) + r')(.*)$')
BLIND_TIME = re.compile(r'^\d{1,2}시\s*\d{1,2}분에')  # 스크린리더용 중복 문구('16시 0분에 영업 시작')
TOGGLE_WORDS = ('펼쳐보기', '접기', '더보기')

# 누락 사유 판별: 라벨의 흔적이 화면 텍스트에 있는지 검사한다.
ANCHOR_TESTS = {
    '주소': lambda text: '주소' in text,
    '영업 상태·라스트오더': lambda text: any(k in text for k in STATUS_WORDS + ('라스트오더',)),
    '영업시간': lambda text: bool(DAY_LINE.search(text)) and bool(TIME_TEXT.search(text)),
    '주차': lambda text: '주차' in text,
    '메뉴·가격': lambda text: bool(re.search(r'\d{1,3}(,\d{3})+원?', text)),
}


def now():
    return datetime.now(timezone.utc).isoformat()


def item(value, source):
    return {'value': value, 'source': source, 'checked_at': now(), 'edited': False}


def parse_candidates(links):
    """검색 화면의 entry/search 매장 링크를 ID별로 합친다. 라벨은 보이는 첫 줄만 쓴다."""
    seen = {}
    addresses = {}
    for link in links:
        href, text = link[:2]
        address = str(link[2] or "").strip().splitlines()[0] if len(link) > 2 and link[2] else ""
        url = urlsplit(href or '')
        m = re.fullmatch(r'/p/(?:entry|search/[^?#]+)/place/(\d+)/?', url.path) if url.hostname == 'map.naver.com' else None
        if m:
            place_id = m.group(1)
            if address:
                addresses[place_id] = address
            label = (text or '').strip().splitlines()[0].strip() if (text or '').strip() else ''
            if place_id not in seen or (label and not seen[place_id]):
                seen[place_id] = label
    return [{'place_id': k, 'label': v, **({'address': addresses[k]} if k in addresses else {})} for k, v in seen.items()]


# Search results use both /p/entry/place/ID and /p/search/QUERY/place/ID.
# Read visible text in one browser round trip; image/roadview links have no name.
_CANDIDATES_JS = r"""
return [...document.querySelectorAll('a[href*="map.naver.com/p/"]')]
  .filter(a => a.getClientRects().length && /\/place\/\d+(?:[/?#]|$)/.test(a.href))
  .map(a => {
    const card = a.closest('li');
    const address = card?.querySelector('[data-nlog-area$=".address"]');
    return [a.href, a.innerText || '', address?.innerText?.split('\n')[0] || ''];
  });
"""


def read_candidates(driver):
    """Wait for named place results, rather than unrelated page navigation links."""
    return [c for c in parse_candidates(driver.execute_script(_CANDIDATES_JS) or []) if c['label']]


def pick_candidate(query, candidates):
    """질의 토큰과 상호 라벨 일치로 매장을 고른다. 최고 일치가 하나뿐일 때만 자동 선택하고,
    구분할 수 없으면 (None, 상위 후보)를 돌려 사용자 선택을 받는다."""
    tokens = [t for t in re.sub(r'\s+', ' ', str(query)).split() if t]
    def score(candidate):
        compact = re.sub(r'\s+', '', candidate['label'])
        return sum(1 for t in tokens if t in compact)
    scored = sorted(((score(c), c) for c in candidates), key=lambda x: -x[0])
    if not scored:
        return None, []
    best = scored[0][0]
    top = [c for s, c in scored if s == best and s > 0]
    shortlist = [c for s, c in scored[:5] if c['label']] or [c for _, c in scored[:5]]
    if len(top) == 1 and best == len(tokens):
        return top[0], shortlist
    return None, shortlist


def parse_home(lines, title, tel_numbers, url, rows=None):
    """플레이스 홈 화면의 보이는 줄에서 항목을 추출한다. 못 찾은 항목은 넣지 않는다.
    rows가 있으면(펼친 영업시간 DOM에서 읽은 요일별 행) 영업시간은 그 행으로 정리한다."""
    items = {}
    name = (title or '').split(' : ')[0].strip()
    if name and '네이버' not in name:
        items['매장명'] = item(name, url)
    detail = parse_week_hours(lines, rows=rows)
    consumed = set(detail.pop('_consumed', ()))
    for index, line in enumerate(lines):
        if line == '주소' and index + 1 < len(lines):
            address = re.sub(r'(지도|내비게이션|거리뷰|복사)+$', '', lines[index + 1]).strip()
            if address:
                items['주소'] = item(address, url)
        if index in consumed or '영업 상태·라스트오더' in items:
            continue
        status = parse_status(line)
        if status:
            items['영업 상태·라스트오더'] = item(status + ' (조회 시점의 현재 상태)', url)
    if detail['days'] or detail['notes'] or detail['exceptions']:
        items['영업시간'] = hours_item(detail, url)
    parking = parse_parking(lines)
    if parking:
        items['주차'] = item(parking, url)
    if tel_numbers:
        items['전화'] = item(tel_numbers[0], url)
    return items


def parse_status(line):
    """'영업 전16:00에 영업 시작' 같은 현재 상태 줄을 '영업 전 · 16:00에 영업 시작'으로 정리한다.
    요일 줄·요일별 시간 줄(정규 영업시간)은 현재 상태로 취급하지 않는다."""
    text = line.strip()
    if DAY_LABEL.match(text) or BLIND_TIME.match(text) or text in TOGGLE_WORDS:
        return ''
    m = STATUS_LINE.match(text)
    if not m:
        if '라스트오더' in text and TIME_TEXT.search(text) and not text.startswith(('라스트오더', '다음')):
            return text
        return ''
    if m.group(1) == '휴무' and ('정기' in text or '매주' in text):
        return ''
    rest = m.group(2).strip(' ·,')
    return f"{m.group(1)} · {rest}" if rest else m.group(1)


def expand_day_label(label):
    """요일 라벨을 요일 목록으로 푼다. '매일'→월~일, '월~금'→월..금, '월·수·금'→[월,수,금].
    '공휴일'은 요일이 아니므로 None(별도 안내로 보존)."""
    text = re.sub(r'요일', '', label.strip())
    if text == '매일':
        return list(WEEK)
    if text == '공휴일':
        return None
    days = []
    for part in re.split(r'\s*[·,]\s*', text):
        span = re.match(r'^([월화수목금토일])\s*[~\-–]\s*([월화수목금토일])$', part)
        if span:
            a, b = WEEK.index(span.group(1)), WEEK.index(span.group(2))
            days.extend(WEEK[a:b + 1] if a <= b else WEEK[a:] + WEEK[:b + 1])
        elif part in WEEK:
            days.append(part)
    return days or None


def _norm_time(m):
    return ('다음 날 ' if m.group(1) else '') + m.group(2)


def _norm_ranges(text):
    """'16:00 - 다음 날 02:00' → '16:00~다음 날 02:00'. 복수 구간은 각각 보존한다."""
    return [f"{'다음 날 ' if a else ''}{s}~{'다음 날 ' if b else ''}{e}" for a, s, b, e in TIME_RANGE.findall(text)]


def parse_day_entry(texts):
    """한 요일의 줄들을 영업 구간·라스트오더·브레이크타임·휴무·별도 안내로 구분한다.
    화면에 없는 값은 만들지 않는다. '다음 날'은 그대로 보존한다."""
    entry = {'hours': [], 'last_order': [], 'break': [], 'closed': False, 'closed_text': '', 'notes': [], 'raw': []}
    for raw in texts:
        for line in (part.strip() for part in str(raw).split('\n')):
            if not line or line in TOGGLE_WORDS or BLIND_TIME.match(line):
                continue
            entry['raw'].append(line)
            ranges = _norm_ranges(line)
            if '브레이크' in line:
                # 구간이 하나면 그것이 브레이크타임, 여럿이면 마지막 구간을 브레이크타임으로 본다(영업 구간이 앞에 온다)
                entry['hours'].extend(ranges[:-1])
                entry['break'].extend(ranges[-1:] or [line])
            elif '라스트오더' in line:
                entry['hours'].extend(ranges)
                remainder = TIME_RANGE.sub(' ', line)
                times = [_norm_time(m) for m in NEXT_DAY_TIME.finditer(remainder)]
                entry['last_order'].extend(times or [line])
            elif '휴무' in line:
                entry['closed'] = True
                entry['closed_text'] = line if not entry['closed_text'] else entry['closed_text'] + ' · ' + line
            elif TIME_RANGE.search(line):
                entry['hours'].extend(_norm_ranges(line))
            elif '24시간' in line:
                entry['hours'].append('24시간 영업')
            else:
                entry['notes'].append(line)
    return entry


def _raw_line(day, entry):
    return f"{day}: {' · '.join(entry['raw'])}"


def parse_week_hours(lines, rows=None):
    """영업시간을 요일별로 읽어 상세 구조를 돌려준다.
    rows: 펼친 DOM에서 읽은 [{'day': '월', 'text': '16:00 - 다음 날 02:00\\n01:00 라스트오더'}, ...].
    rows가 없으면 화면 줄에서 읽는다 — 요일 다음 줄에 시간이 오는 여러 줄 화면, '월 11:00 - 21:00' 한 줄 화면,
    과거 저장 형식 '월: 11:00 - 21:00 · 라스트오더 20:30' 모두 처리한다.
    반환: {'days': {요일: entry}, 'raw': [요일별 원자료 줄], 'notes': [요일에 속하지 않는 별도 안내], 'exceptions': [공휴일 등]}"""
    detail = {'days': {}, 'raw': [], 'notes': [], 'exceptions': [], '_consumed': []}
    blocks = []
    if rows:
        for row in rows:
            label = str(row.get('day', '')).strip()
            text = str(row.get('text', '')).strip()
            if DAY_LABEL.match(label):
                blocks.append((label, [text]))
            elif label or text:
                detail['notes'].append(f"{label} {text}".strip())
    else:
        current = None
        for index, raw in enumerate(lines):
            line = raw.strip()
            one_line = re.match(r'^(매일|공휴일|[월화수목금토일](요일)?)\s*[:：]?\s+(.+)$', line)
            if DAY_LABEL.match(line):
                current = [line, []]
                blocks.append(current)
                detail['_consumed'].append(index)
            elif one_line and (TIME_TEXT.search(line) or '휴무' in line):
                current = [one_line.group(1), [p.strip() for p in one_line.group(3).split(' · ') if p.strip()]]
                blocks.append(current)  # 뒤따르는 라스트오더·브레이크타임 줄은 이 요일에 붙인다
                detail['_consumed'].append(index)
            elif current is not None and (TIME_TEXT.search(line) or any(k in line for k in ('라스트오더', '휴무', '브레이크'))):
                current[1].append(line)
                detail['_consumed'].append(index)
            elif current is not None and current[1]:
                current = None  # 요일 블록이 끝났다
        blocks = [(b[0], b[1]) for b in blocks if b[1]]
    for label, texts in blocks:
        entry = parse_day_entry(texts)
        if not entry['raw']:
            continue
        days = expand_day_label(label)
        if days is None:
            detail['exceptions'].append(_raw_line(label, entry))
            continue
        detail['raw'].append(_raw_line(label, entry))
        for day in days:
            detail['days'][day] = {k: (list(v) if isinstance(v, list) else v) for k, v in entry.items()}
    return detail


def _signature(entry):
    return (tuple(entry['hours']), tuple(entry['last_order']), tuple(entry['break']), entry['closed'],
            entry['closed_text'] if entry['closed'] else '', tuple(entry['notes']))


def _format_days(days):
    """요일 집합 → '매일' / '월~목' / '금~토' / '월·수·금' / '일요일'. 연속 구간만 '~'로 묶는다."""
    idx = sorted(WEEK.index(d) for d in days)
    if len(idx) == 7:
        return '매일'
    runs, start = [], None
    for i, d in enumerate(idx):
        if start is None:
            start = d
        if i + 1 == len(idx) or idx[i + 1] != d + 1:
            runs.append((start, d))
            start = None
    if len(runs) == 1 and runs[0][0] == runs[0][1]:
        return WEEK[runs[0][0]] + '요일'
    return '·'.join(WEEK[a] if a == b else f"{WEEK[a]}~{WEEK[b]}" for a, b in runs)


def _format_entry(entry):
    if entry['closed']:
        text = entry['closed_text'] if '정기' in entry['closed_text'] or '휴무' not in entry['closed_text'] else '휴무'
        if entry['hours']:
            text = ', '.join(entry['hours']) + ' / ' + text
    else:
        text = ', '.join(entry['hours']) or (' · '.join(entry['notes']) if entry['notes'] else '시간 미표시')
    if entry['last_order']:
        text += ' / 라스트오더 ' + ', '.join(entry['last_order'])
    if entry['break']:
        text += ' / 브레이크타임 ' + ', '.join(entry['break'])
    if entry['notes'] and (entry['hours'] or entry['closed']):
        text += ' / ' + ' · '.join(entry['notes'])
    return text


def group_hours(detail):
    """같은 일정(영업 구간·라스트오더·브레이크타임·휴무가 모두 같음)끼리 요일을 묶는다.
    라스트오더나 브레이크타임만 달라도 합치지 않는다. 확인된 요일만 묶고 미확인 요일은 따로 둔다."""
    days = detail.get('days') or {}
    groups = []
    for day in WEEK:
        if day not in days:
            continue
        sig = _signature(days[day])
        for group in groups:
            if group['signature'] == sig:
                group['days'].append(day)
                break
        else:
            groups.append({'signature': sig, 'days': [day], 'entry': days[day]})
    return {'groups': [{'days': g['days'], 'label': _format_days(g['days']), 'text': _format_entry(g['entry'])} for g in groups],
            'confirmed': [d for d in WEEK if d in days], 'unconfirmed': [d for d in WEEK if d not in days]}


def format_hours(detail):
    """저장·표시·초안 전달에 같이 쓰는 영업시간 요약 문자열. 일부 요일만 확인됐으면 '매일'로 넓히지 않는다."""
    grouped = group_hours(detail)
    lines = [f"{g['label']} {g['text']}" for g in grouped['groups']]
    if grouped['confirmed'] and grouped['unconfirmed']:
        lines.append('미확인 요일: ' + '·'.join(grouped['unconfirmed']) + ' (화면에서 확인되지 않음)')
    lines.extend('별도 안내: ' + text for text in (detail.get('exceptions') or []) + (detail.get('notes') or []))
    return '\n'.join(lines)


def hours_item(detail, source):
    """영업시간 항목: value는 묶은 요약, detail에 요일별 원자료·묶음·미확인 요일을 보존한다."""
    grouped = group_hours(detail)
    data = item(format_hours(detail), source)
    data['detail'] = {'raw': list(detail.get('raw') or []), 'groups': grouped['groups'], 'confirmed': grouped['confirmed'],
                      'unconfirmed': grouped['unconfirmed'], 'notes': list(detail.get('notes') or []),
                      'exceptions': list(detail.get('exceptions') or []),
                      'days': {d: {k: v for k, v in e.items() if k != 'raw'} for d, e in (detail.get('days') or {}).items()}}
    return data


def enrich_hours(data):
    """과거 저장 형식('월: 16:00 - 02:00 · 라스트오더 01:00' 줄)의 영업시간 항목에 detail·summary를 보충한다.
    value·확인일은 바꾸지 않고, 사용자 수정값은 건드리지 않는다."""
    if not isinstance(data, dict) or data.get('edited') or data.get('detail') or not data.get('value'):
        return data
    detail = parse_week_hours(str(data['value']).splitlines())
    detail.pop('_consumed', None)
    if detail['days']:
        data['detail'] = hours_item(detail, data.get('source', ''))['detail']
        data['detail']['summary'] = format_hours(detail)
    return data


def parse_parking(lines):
    """주차 안내를 원문 그대로 보존한다. '주차 불가'와 '주변 공영주차장 안내'가 함께 있으면
    함께 저장하고, 가능 여부를 해석하거나 바꾸지 않는다."""
    collected = []
    for index, line in enumerate(lines):
        if '주차' in line and len(line) < 80:
            chunk = [line]
            for follow in lines[index + 1:index + 4]:
                if ('주차' in follow or '공영' in follow or '발렛' in follow or '유료' in follow or '무료' in follow) and len(follow) < 120:
                    chunk.append(follow)
                else:
                    break
            collected.extend(c for c in chunk if c not in collected)
    return '\n'.join(collected)


def parse_menu(lines, url):
    """메뉴 화면에서 '이름 다음 줄 가격(원)' 쌍을 최대 6개 수집한다."""
    entries = []
    for index, line in enumerate(lines[1:], 1):
        if re.fullmatch(r'\d{1,3}(,\d{3})+원?', line.strip()) and len(lines[index - 1]) < 40:
            entries.append(f"{lines[index - 1].strip()} {line.strip()}")
        if len(entries) >= 6:
            break
    return item('\n'.join(entries) + '\n(공개 메뉴판 기준, 실제 결제 금액 아님)', url) if entries else None


def miss_reason(label, pages):
    """누락 사유 구분: 로딩 실패 / 추출 실패 / 보안 확인 / 화면에 항목 없음."""
    loaded = [p for p in pages if p.get('loaded')]
    if not loaded:
        return '로딩 실패 · 네트워크·페이지 상태를 확인하고 재조회'
    text = '\n'.join('\n'.join(p.get('lines', [])) for p in loaded)
    test = ANCHOR_TESTS.get(label)
    if test and test(text):
        return '항목 표시는 있으나 추출 실패 · 화면 구조가 바뀌었을 수 있음'
    if label == '영업시간':
        # 펼치기 결과로 사유를 가른다. 보안 확인 글자만으로 보안 탓으로 돌리지 않는다.
        actions = [p.get('interaction') or {} for p in loaded]
        if any(a.get('toggle_found') and a.get('rows') for a in actions):
            return '영업시간 행은 펼쳐졌으나 추출 실패 · 화면 구조가 바뀌었을 수 있음'
        if any(a.get('toggle_found') and not a.get('expanded') for a in actions):
            return '영업시간 펼치기를 눌렀으나 상세 일정이 표시되지 않음 · 재조회'
    if any(p.get('secured') for p in loaded):
        return '보안 확인으로 화면에 표시되지 않음 · 열어 둔 탭에서 직접 완료 후 재조회'
    if label == '영업시간':
        return '화면에 항목 없음 · 영업시간 펼치기 요소를 찾지 못함'
    return '화면에 항목 없음'


# 영업시간 토글 후보. 2026-10-23 모바일 홈 실측: <a role="button" aria-expanded="false" data-nlog-area="plc_btp.bzhour">
# 안에 현재 상태(em '영업 전'/'영업 중' …)와 '펼쳐보기' 숨김 글자가 있고, 펼치면 요일별 행이 같은 요소 안에 생긴다.
# 현재 상태 문구(라스트오더 유무)와 무관하게 '펼쳐보기/접기'를 가진 영업시간 영역의 실제 버튼만 고른다.
_STATUS_COND = ' or '.join(f"contains(., '{w}')" for w in STATUS_WORDS + ('라스트오더', '영업시간'))
TOGGLE_XPATHS = (
    "//*[@data-nlog-area='plc_btp.bzhour']",
    f"//*[(self::a or self::button or @role='button') and @aria-expanded][.//*[contains(text(), '펼쳐보기') or contains(text(), '접기')]][{_STATUS_COND}]",
    f"//*[(self::a or self::button or @role='button')][.//*[contains(text(), '펼쳐보기')]][{_STATUS_COND}]",
)
# 토글 요소 안에서 요일 라벨(잎 요소)과 그 다음 형제(시간 블록, <br> 구분)를 읽는다. 클래스 이름에 의존하지 않는다.
_ROWS_JS = """
const root = arguments[0];
const text = n => { let out = ''; for (const c of n.childNodes) {
  if (c.nodeType === 3) out += c.data; else if (c.nodeName === 'BR') out += '\\n'; else out += text(c); } return out; };
const rows = [];
for (const el of root.querySelectorAll('*')) {
  if (el.children.length) continue;
  const label = (el.textContent || '').trim();
  if (!/^(매일|공휴일|[월화수목금토일](요일)?)$/.test(label)) continue;
  const sib = el.nextElementSibling;
  if (!sib) continue;
  rows.push({day: label, text: text(sib).split('\\n').map(s => s.trim()).filter(Boolean).join('\\n')});
}
return {expanded: root.getAttribute('aria-expanded'), rows: rows, text: text(root)};
"""


def _find_hours_toggle(driver):
    from selenium.webdriver.common.by import By
    for xpath in TOGGLE_XPATHS:
        found = driver.find_elements(By.XPATH, xpath)
        if found:
            return found[0]
    return None


def _hours_rows(driver, toggle):
    try:
        data = driver.execute_script(_ROWS_JS, toggle) or {}
    except Exception:
        return {'expanded': None, 'rows': [], 'text': ''}
    return {'expanded': data.get('expanded'), 'rows': list(data.get('rows') or []), 'text': str(data.get('text') or '')}


def _expand_home(driver):
    """영업시간 토글을 찾아 펼치고(이미 펼쳐져 있으면 그대로 둠) 요일별 행을 DOM에서 읽는다.
    '찾아가는길' 내용 더보기도 시도한다. 실패해도 예외를 내지 않고 결과에 기록한다."""
    from selenium.webdriver.common.by import By
    result = {'toggle_found': False, 'already_expanded': False, 'clicked': False, 'expanded': False, 'rows': [], 'section_text': ''}
    toggle = _find_hours_toggle(driver)
    if toggle is not None:
        result['toggle_found'] = True
        state = _hours_rows(driver, toggle)
        if str(state['expanded']).lower() == 'true' or state['rows']:
            result['already_expanded'] = True  # 다시 눌러 접지 않는다
        else:
            try:
                driver.execute_script('arguments[0].click()', toggle)
                result['clicked'] = True
            except Exception:
                pass
            deadline = time.time() + 4
            while time.time() < deadline:  # 상세 일정이 실제로 표시됐는지 확인
                time.sleep(0.4)
                state = _hours_rows(driver, toggle)
                if state['rows']:
                    break
        result['expanded'] = bool(state['rows'])
        result['rows'] = state['rows']
        result['section_text'] = state['text']
    for el in driver.find_elements(By.XPATH, "//a[normalize-space()='내용 더보기'] | //button[normalize-space()='내용 더보기'] | //a[normalize-space()='더보기 펼쳐보기']")[:3]:
        try:
            driver.execute_script('arguments[0].click()', el)
            time.sleep(0.6)
        except Exception:
            continue
    return result


def _page_lines(driver, url, interact=None):
    """탭을 열어(이미 같은 주소 탭이 있으면 재사용) 보이는 줄을 읽는다.
    보안 확인이 떠 있으면 탭을 닫지 않고 유지해 사용자가 직접 완료할 수 있게 한다."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    page = {'url': url, 'loaded': False, 'secured': False, 'lines': [], 'title': '', 'tels': [], 'kept': False, 'interaction': {}}
    base = url.split('?')[0]
    home_handle = driver.current_window_handle
    reused = None
    for handle in driver.window_handles:
        driver.switch_to.window(handle)
        if (driver.current_url or '').split('?')[0] == base:
            reused = handle
            break
    if reused is None:
        driver.switch_to.new_window('tab')
    try:
        driver.get(url)
        WebDriverWait(driver, 20).until(lambda d: len(d.find_element(By.TAG_NAME, 'body').text) > 300)
        page['loaded'] = True
        if interact:
            try:
                page['interaction'] = interact(driver) or {}
            except Exception as exc:
                page['interaction'] = {'error': str(exc)[:200]}
        body = driver.find_element(By.TAG_NAME, 'body').text
        page['secured'] = '보안 확인' in body
        page['lines'] = [l.strip() for l in body.splitlines() if l.strip()]
        page['title'] = driver.title
        page['tels'] = [a.get_attribute('href')[4:] for a in driver.find_elements(By.CSS_SELECTOR, 'a[href^="tel:"]') if a.get_attribute('href')]
    except Exception:
        pass
    finally:
        if page['secured']:
            page['kept'] = True  # 보안 확인 탭은 유지한다.
        else:
            try:
                driver.close()
            except Exception:
                pass
        driver.switch_to.window(home_handle if home_handle in driver.window_handles else driver.window_handles[0])
    return page


def collect_place(query, chosen_id=None, progress=None):
    """매장 기본정보 수집. 실패해도 예외 대신 error와 누락 사유를 담은 보고서를 반환한다."""
    progress = progress or (lambda message: None)
    report = {'query': str(query or '').strip(), 'place_id': '', 'fetched_at': now(), 'items': {},
              'missing': [], 'miss_reasons': {}, 'candidates': [], 'error': '', 'notices': [],
              'source_note': '네이버 플레이스 화면에서 보이는 값만 수집. 보안 확인·로그인 제한은 우회하지 않음.'}
    if not report['query']:
        report['error'] = '매장 이름이 비어 있습니다.'
        return report
    driver = None
    pages = []
    acquired = LOCK.acquire(blocking=False)
    try:
        if not acquired:
            raise ValueError('엔서포터 브라우저가 다른 작업을 처리 중입니다.')
        ensure_browser(progress)
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        driver = connect()
        driver.set_page_load_timeout(30)
        for handle in driver.window_handles:
            driver.switch_to.window(handle)
            if driver.current_url.startswith('https://search.naver.com/'):
                break
        else:
            driver.switch_to.new_window('tab')
        chosen_label = ''
        if chosen_id:
            report['place_id'] = str(chosen_id)
        else:
            progress(f"매장 정보 조사 '{report['query']}' · 네이버 검색에서 플레이스 확인 중")
            driver.get('https://search.naver.com/search.naver?' + urlencode({'query': report['query']}))
            candidates = WebDriverWait(driver, 20).until(read_candidates)
            if not candidates:
                report['error'] = '검색 화면에서 플레이스 정보를 찾지 못했습니다. 지역·상호·지점을 더 정확히 입력해 보세요.'
                return report
            chosen, shortlist = pick_candidate(report['query'], candidates)
            if chosen is None:
                report['candidates'] = shortlist
                report['error'] = '매장이 하나로 구분되지 않습니다. 목록에서 매장을 선택해 주세요.'
                return report
            report['place_id'] = chosen['place_id']
            chosen_label = chosen['label']
            if len(shortlist) > 1:
                report['notices'].append(f"상호 일치로 '{chosen['label']}'을(를) 선택했습니다. 다른 지점이면 재조회에서 선택하세요.")
                report['candidates'] = shortlist
        home_url = f"https://m.place.naver.com/restaurant/{report['place_id']}/home"
        progress('매장 정보 조사 · 플레이스 홈 읽는 중 (영업시간 펼치기 포함)')
        home = _page_lines(driver, home_url, interact=_expand_home)
        pages.append(home)
        report['items'] = parse_home(home['lines'], home['title'], home['tels'], home_url, rows=home['interaction'].get('rows'))
        report['items']['지도 링크'] = item(f"https://map.naver.com/p/entry/place/{report['place_id']}", home_url)
        if '매장명' not in report['items'] and chosen_label:
            # 상세 화면에서 못 읽어도 검색 화면에서 확인한 상호는 쓸 수 있다.
            report['items']['매장명'] = item(chosen_label, '네이버 검색 화면(상호 일치 선택)')
        if '주차' not in report['items'] or '영업시간' not in report['items']:
            info_url = f"https://m.place.naver.com/restaurant/{report['place_id']}/information"
            progress('매장 정보 조사 · 정보 탭 읽는 중 (주차·상세 안내)')
            info = _page_lines(driver, info_url)
            pages.append(info)
            if '주차' not in report['items']:
                parking = parse_parking(info['lines'])
                if parking:
                    report['items']['주차'] = item(parking, info_url)
        if '메뉴·가격' not in report['items']:
            menu_url = f"https://m.place.naver.com/restaurant/{report['place_id']}/menu/list"
            menu_page = _page_lines(driver, menu_url)
            pages.append(menu_page)
            menu = parse_menu(menu_page['lines'], menu_url)
            if menu:
                report['items']['메뉴·가격'] = menu
        if any(p['kept'] for p in pages):
            report['notices'].append('보안 확인이 뜬 탭을 닫지 않고 열어 두었습니다. 그 탭에서 직접 완료한 뒤 재조회하세요. 자동 통과는 하지 않습니다.')
    except Exception as exc:
        report['error'] = str(exc) if isinstance(exc, ValueError) else '플레이스 화면 연결·읽기에 실패했습니다. 전용 Chrome 상태를 확인하세요.'
    finally:
        if driver is not None:
            driver.service.stop()
        if acquired:
            LOCK.release()
    report['missing'] = [label for label in LABELS if label not in report['items']]
    report['miss_reasons'] = {label: miss_reason(label, pages) for label in report['missing']}
    return report
