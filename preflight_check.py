"""내일 아침 자동매매 정상 실행 가능 여부 사전 점검 (매일 밤 21:00 실행)

점검 항목:
  1. 전략 JSON 파싱
  2. 필수 환경변수 존재 여부
  3. KIS API 연결 (토큰 발급 + 잔고 조회)
  4. 결과를 텔레그램으로 전송 후 종료
"""

import os
import sys
import json
import logging
from glob import glob
from datetime import datetime

_BASE = os.path.dirname(os.path.abspath(__file__))
_MODE = os.getenv("KIS_MODE", "live")

sys.path.insert(0, _BASE)

# .env 먼저 로드 (notifier import 전)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BASE, f".env.{_MODE}"))

LOG_FILE = os.path.join(_BASE, "preflight.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("preflight")

_REQUIRED_ENV = ["KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "KIS_ACCOUNT_CD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


def _send(text: str):
    """notifier 모듈 없이 직접 전송 (점검 실패 시에도 알림 보내야 함)"""
    import requests
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("텔레그램 토큰/채팅ID 없음 — 알림 전송 불가")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"텔레그램 전송 실패: {e}")


def check_env() -> list[str]:
    errors = []
    for key in _REQUIRED_ENV:
        if not os.getenv(key):
            errors.append(f"환경변수 없음: {key}")
    return errors


def check_strategies() -> tuple[list[str], list[str]]:
    ok, errors = [], []
    files = sorted(glob(os.path.join(_BASE, "strategies", "*.json")))
    if not files:
        errors.append("전략 파일이 없습니다 (strategies/*.json)")
        return ok, errors

    for path in files:
        fname = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = json.load(f)
            enabled = s.get("enabled", False)
            ok.append(f"{'✅' if enabled else '⛔'} {s.get('name', fname)} ({'활성' if enabled else '비활성'})")
        except json.JSONDecodeError as e:
            errors.append(f"JSON 오류 [{fname}]: {e}")
        except Exception as e:
            errors.append(f"읽기 오류 [{fname}]: {e}")

    return ok, errors


def check_api() -> tuple[bool, str]:
    try:
        from kis_api import KISApi
        api = KISApi()
        bal = api.get_balance()
        s   = bal["summary"][0] if isinstance(bal["summary"], list) else bal["summary"]
        cash  = int(s.get("dnca_tot_amt", 0))
        total = int(s.get("tot_evlu_amt", 0))
        return True, f"예수금 {cash:,}원 / 총평가 {total:,}원"
    except Exception as e:
        return False, str(e)


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    log.info(f"=== Preflight 점검 시작 ({now}) MODE={_MODE} ===")

    lines  = [f"🔍 <b>내일 아침 사전 점검</b> ({now})\n모드: {_MODE}\n"]
    passed = True

    # 1. 환경변수
    env_errors = check_env()
    if env_errors:
        passed = False
        lines.append("❌ <b>환경변수 이상</b>")
        lines.extend(f"  • {e}" for e in env_errors)
    else:
        lines.append("✅ 환경변수 정상")

    # 2. 전략 JSON
    strat_ok, strat_errors = check_strategies()
    if strat_errors:
        passed = False
        lines.append("\n❌ <b>전략 파일 이상</b>")
        lines.extend(f"  • {e}" for e in strat_errors)
    else:
        active = sum(1 for s in strat_ok if "활성)" in s)
        lines.append(f"\n✅ 전략 파일 정상 ({active}개 활성)")
        lines.extend(f"  {s}" for s in strat_ok)

    # 3. KIS API 연결
    api_ok, api_msg = check_api()
    if api_ok:
        lines.append(f"\n✅ KIS API 연결 정상\n  {api_msg}")
    else:
        passed = False
        lines.append(f"\n❌ <b>KIS API 연결 실패</b>\n  {api_msg}")

    # 4. 최종 결과
    if passed:
        lines.append("\n\n🚀 <b>내일 아침 정상 실행 가능합니다.</b>")
    else:
        lines.append("\n\n⚠️ <b>이상 항목이 있습니다. 확인 후 조치 필요!</b>")

    result = "\n".join(lines)
    log.info(result.replace("<b>", "").replace("</b>", ""))
    _send(result)
    log.info("=== Preflight 점검 완료 ===")


if __name__ == "__main__":
    main()
