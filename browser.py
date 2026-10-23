"""전용 Chrome(tmp/nsupporter-chrome 프로필) 실행·연결 공통 코드.

매장 정보 조사와 엔서포터 분석이 같은 브라우저를 쓴다. 닫혀 있으면 기존 프로필로
자동 실행해 설치된 엔서포터·로그인 상태를 재사용하고, 설치 페이지는 열지 않는다.
이미 열려 있으면 그대로 재사용하며 중복 실행하지 않는다.
"""
import socket
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / 'tmp' / 'nsupporter-chrome'
HOST, PORT = '127.0.0.1', 9223
ADDRESS = f'{HOST}:{PORT}'
START_TIMEOUT = 30


def port_open(timeout=1.5):
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout):
            return True
    except OSError:
        return False


def connect():
    from selenium import webdriver
    options = webdriver.ChromeOptions()
    options.debugger_address = ADDRESS
    return webdriver.Chrome(options=options)


def launch():
    """기존 프로필로 전용 Chrome을 새로 실행한다. 브라우저는 detach로 계속 떠 있는다."""
    from selenium import webdriver
    PROFILE.mkdir(parents=True, exist_ok=True)
    options = webdriver.ChromeOptions()
    options.add_argument(f'--user-data-dir={PROFILE}')
    options.add_argument(f'--remote-debugging-address={HOST}')
    options.add_argument(f'--remote-debugging-port={PORT}')
    options.add_experimental_option('detach', True)
    driver = webdriver.Chrome(options=options)
    driver.service.stop()


def ensure_browser(progress=None):
    """열려 있으면 재사용, 닫혀 있으면 자동 실행 후 연결 가능해질 때까지 기다린다."""
    progress = progress or (lambda message: None)
    if port_open():
        return
    progress('전용 Chrome이 닫혀 있어 기존 프로필로 자동 실행 중 (엔서포터 설치 상태 유지)')
    try:
        launch()
    except Exception:
        raise ValueError('전용 Chrome 자동 실행에 실패했습니다. Chrome 설치와 Selenium을 확인하거나, '
                         'python tools/probe_nsupporter.py setup 으로 직접 열어 주세요.') from None
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if port_open():
            progress('전용 Chrome 연결 준비 완료')
            return
        time.sleep(0.5)
    raise ValueError(f'전용 Chrome이 {START_TIMEOUT}초 안에 연결되지 않았습니다. '
                     '같은 프로필을 쓰는 다른 Chrome 창이 열려 있으면 닫고 다시 시도해 주세요.')
