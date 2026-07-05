"""스윙 돌파 전략 실행 프로세스

흐름:
  - 기동 시: swing_candidates_{MODE}.json (전날 21시 스크리닝 결과) 로드
  - 09:03: 후보 중 현재가 > 시가인 종목 매수
  - 모니터링: +10% 익절 / -5% 손절 (30초 간격)
  - 15:30: 결산 알림 후 종료 (강제청산 없음 — 미청산 포지션 익일 이월)
  - 기동 시 유령 포지션 검증 (positions 파일 vs 실제 잔고 대조)

실행: python engine/swing_runner.py <strategy_id>
워치독이 swing 타입 전략에 대해 자동으로 기동
"""

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kis_api import KISApi
from engine.monitor import (
    add_position, check_and_exit, load_positions,
    remove_position, record_trade, load_daily_pnl,
    reserve_or_skip, cancel_reservation, is_sl_blacklisted,
)
from engine.notifier import notify_buy, notify_error, notify_sell, send
from engine.order_utils import limit_buy

_BASE    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE    = os.getenv("KIS_MODE", "live")
_PERSONAL_HOLDINGS = {"005935", "491700", "0162Z0"}


def _setup_logger(strategy_id: str) -> logging.Logger:
    log_file = os.path.join(_BASE, f"trading_{strategy_id}_{_MODE}.log")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(fh)
        root.addHandler(sh)
    return logging.getLogger(strategy_id)


def _load_strategy(strategy_id: str) -> dict:
    path = os.path.join(_BASE, "strategies", f"{strategy_id}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_candidates(strategy_id: str, log) -> list[dict]:
    """전날 스크리닝 결과 로드. 날짜가 2거래일 이상 지났으면 경고 후 빈 리스트."""
    path = os.path.join(_BASE, f"swing_candidates_{strategy_id}_{_MODE}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log.warning(f"[{strategy_id}] swing_candidates 파일 없음 — 매수 없이 모니터링만 진행")
        return []

    screen_date_str = data.get("date", "")
    try:
        screen_date = datetime.strptime(screen_date_str, "%Y-%m-%d").date()
    except ValueError:
        log.warning(f"[{strategy_id}] candidates 날짜 파싱 실패 ({screen_date_str})")
        return []

    today = datetime.now().date()
    delta = (today - screen_date).days
    if delta > 3:
        log.warning(
            f"[{strategy_id}] candidates가 {delta}일 전 것 ({screen_date_str}) — "
            "스크리닝이 최근에 실행되지 않아 매수 생략"
        )
        return []

    stocks = data.get("stocks", [])
    log.info(
        f"[{strategy_id}] 후보 {len(stocks)}개 로드 "
        f"(스크리닝: {screen_date_str} {data.get('screened_at','')})"
    )
    return stocks


def _validate_positions(api: KISApi, sid: str, log):
    """시작 시 positions 파일과 실제 잔고 대조해 유령 포지션 제거"""
    positions = load_positions()
    my_pos = {k: v for k, v in positions.items()
              if v.get("strategy_id") == sid and not v.get("_reserved")}
    if not my_pos:
        return
    try:
        bal = api.get_balance()
        held = {s.get("pdno") for s in bal.get("stocks", [])}
    except Exception as e:
        log.warning(f"[포지션 검증] 잔고 조회 실패 ({e}) — 검증 생략")
        return
    for pos_key, pos in my_pos.items():
        code = pos.get("code") or pos_key.split("_")[0]
        if code in _PERSONAL_HOLDINGS:
            continue
        if code not in held:
            log.warning(f"[포지션 검증] {pos.get('name',code)}({code}) 실제 잔고 없음 → 유령 포지션 제거")
            remove_position(pos_key)


def _execute_entry(api: KISApi, strategy: dict, candidates: list[dict], log, entry_done_codes: set):
    """후보 종목 중 시가 대비 현재가 + 인 경우 매수"""
    sid        = strategy["id"]
    entry      = strategy["entry"]
    max_stocks = entry["max_stocks"]
    # 현재 보유 수 확인
    positions = load_positions()
    my_pos = {k: v for k, v in positions.items()
              if v.get("strategy_id") == sid and not v.get("_reserved")}
    available = max_stocks - len(my_pos)
    if available <= 0:
        log.info(f"[{sid}] 최대 보유 종목({max_stocks}개) 도달 — 신규 매수 없음")
        return

    if "total_amount" in entry:
        n = min(len(candidates), available)
        amount = entry["total_amount"] // n if n > 0 else entry["total_amount"]
        log.info(f"[{sid}] 종목당 투자금: {entry['total_amount']:,}원 ÷ {n}개 = {amount:,}원")
    else:
        amount = entry.get("amount_per_stock", 300000)
        log.info(f"[{sid}] 종목당 투자금: {amount:,}원")

    bought = 0
    for stock in candidates:
        if bought >= available:
            break

        code = stock["code"]
        name = stock["name"]

        if code in entry_done_codes:
            continue

        if is_sl_blacklisted(code):
            log.info(f"[손절 블랙리스트] {name}({code}) 당일 손절 이력 → 재매수 금지")
            entry_done_codes.add(code)
            continue

        if not reserve_or_skip(code, sid):
            log.info(f"[중복 방지] {name}({code}) 타 전략 보유/예약 중 → 스킵")
            entry_done_codes.add(code)
            continue

        try:
            price_info = api.get_price(code)
            current    = int(price_info.get("stck_prpr", 0))
            open_price = int(price_info.get("stck_oprc", 0))
            prev_close = int(price_info.get("stck_prdy_clpr", 0))

            if open_price <= 0 or current <= 0:
                log.info(f"[{name}({code})] 가격 정보 없음 → 스킵")
                cancel_reservation(code, sid)
                entry_done_codes.add(code)
                continue

            # 시가 > 전일 종가 (갭 상승) 조건
            if entry.get("require_positive_open") and prev_close > 0 and open_price <= prev_close:
                log.info(
                    f"  {name}({code}) 시가 {open_price:,} <= 전일종가 {prev_close:,} → 매수 취소"
                )
                cancel_reservation(code, sid)
                entry_done_codes.add(code)
                continue

            # 시가 대비 현재가 + 조건
            if entry.get("require_above_open", True) and current <= open_price:
                log.info(
                    f"  {name}({code}) 현재가 {current:,} <= 시가 {open_price:,} → 매수 보류"
                )
                cancel_reservation(code, sid)
                continue  # 이번 주기에는 보류 (다음 주기 재시도 안 함 — 9:03 1회만)

            qty = max(1, amount // current)
            cash = api.get_cash()
            if cash < qty * current:
                msg = f"[{name}] 잔고 부족 (필요 {qty*current:,}원 / 보유 {cash:,}원)"
                log.warning(msg)
                notify_error(msg)
                cancel_reservation(code, sid)
                entry_done_codes.add(code)
                continue

            # 지정가 매수
            wait_sec     = strategy["entry"].get("unfilled_wait_sec", 30)
            actual_price = limit_buy(api, code, name, qty, wait_sec)
            if actual_price > 0:
                log.info(f"[체결가] {name}({code}) {actual_price:,}원")
                notify_buy(name, code, qty, actual_price, strategy["name"])
                add_position(code, name, actual_price, qty, sid)
                entry_done_codes.add(code)
                bought += 1
            else:
                log.info(f"[매수 취소] {name}({code}) 지정가 미체결 → 포기")
                notify_error(f"매수 취소(미체결) {name}({code})")
                cancel_reservation(code, sid)
                entry_done_codes.add(code)

            time.sleep(0.3)

        except Exception as e:
            log.error(f"[매수 오류] {name}({code}): {e}")
            notify_error(f"매수 실패 {name}({code}): {e}")
            cancel_reservation(code, sid)
            entry_done_codes.add(code)


def _send_summary(strategy: dict, today: str, log):
    """당일 결산 (미청산 포지션 안내 포함)"""
    sid = strategy["id"]
    try:
        pnl_data = load_daily_pnl()
        trades   = [t for t in pnl_data.get("trades", []) if t.get("strategy_id") == sid]
        strat_pnl  = sum(t["pnl"] for t in trades)
        strat_cost = sum(t.get("cost", 0) for t in trades)

        lines = [f"📋 <b>[{strategy['name']}] 결산 ({today})</b>\n"]
        if trades:
            for tr in trades:
                sign = "✅" if tr["pnl"] >= 0 else "🔴"
                lines.append(
                    f"{sign} {tr['name']} [{tr['reason']}]  "
                    f"{tr.get('gross_pnl', tr['pnl']):+,}원 / 비용 -{tr.get('cost',0):,}원 / 순 {tr['pnl']:+,}원"
                    f"  ({tr['buy_price']:,}→{tr['sell_price']:,})"
                )
        else:
            lines.append("거래 없음")

        pnl_sign = "+" if strat_pnl >= 0 else ""
        lines.append(f"\n💰 순손익: {pnl_sign}{strat_pnl:,}원  |  비용: -{strat_cost:,}원")

        # 미청산 포지션 안내
        positions = load_positions()
        my_open = {k: v for k, v in positions.items()
                   if v.get("strategy_id") == sid and not v.get("_reserved")}
        if my_open:
            lines.append(f"\n⏳ 미청산 {len(my_open)}개 → 내일 이월")
            for pos in my_open.values():
                lines.append(
                    f"  • {pos['name']} {pos['qty']}주 @ {pos['buy_price']:,}원"
                )

        send("\n".join(lines))
        log.info(f"[{sid}] 결산 전송 완료")
    except Exception as e:
        log.error(f"결산 전송 실패: {e}")


def _is_trading_day() -> bool:
    import holidays as _holidays
    from datetime import date as _date
    d = _date.today()
    if d.weekday() >= 5:
        return False
    FIXED = {(1,1),(3,1),(5,1),(5,5),(6,6),(8,15),(10,3),(10,9),(12,25)}
    if (d.month, d.day) in FIXED:
        return False
    try:
        return d not in _holidays.KR(years=d.year)
    except Exception:
        return True


def run(strategy_id: str):
    strategy = _load_strategy(strategy_id)
    log      = _setup_logger(strategy_id)
    sid      = strategy["id"]

    if not _is_trading_day():
        log.info(f"[{sid}] 오늘은 휴장일 — 실행 종료")
        send(f"📵 [{sid}] 오늘은 휴장일입니다.")
        return

    h, m      = map(int, strategy["schedule"]["entry_time"].split(":"))
    entry_t   = h * 100 + m
    interval  = strategy["schedule"].get("monitor_interval_sec", 30)

    api = KISApi()
    _validate_positions(api, sid, log)

    candidates = _load_candidates(sid, log)
    log.info(f"[{sid}] 스윙 러너 시작 | 진입 {strategy['schedule']['entry_time']} | 후보 {len(candidates)}개")

    entry_executed   = False    # 당일 9:03 진입 1회 플래그
    entry_done_codes: set = set()
    summary_sent_day = None

    while True:
        now   = datetime.now()
        today = now.date()
        t     = now.hour * 100 + now.minute

        # 자정 초기화 (다음날 새벽 다시 시작될 경우 대비)
        if t < 855 and entry_executed:
            entry_executed = False
            entry_done_codes.clear()
            candidates = _load_candidates(sid, log)
            log.info(f"[{sid}] 날짜 전환 — 새 candidates {len(candidates)}개 로드")

        # 09:03 매수 (1회) — 재시작 대비 5분 윈도우
        if not entry_executed and entry_t <= t < entry_t + 5:
            log.info(f"=== [{sid}] 09:03 진입 실행 ===")
            _execute_entry(api, strategy, candidates, log, entry_done_codes)
            entry_executed = True

        # 모니터링 (9:00~15:30)
        if 900 <= t < 1530:
            check_and_exit(api, [strategy])

        # 15:30 결산 후 종료 (강제청산 없음)
        if t >= 1530 and summary_sent_day != today:
            _send_summary(strategy, str(today), log)
            summary_sent_day = today
            log.info(f"[{sid}] 오늘 완료. 미청산 포지션 이월. 프로세스 종료.")
            return

        time.sleep(interval)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python engine/swing_runner.py <strategy_id>")
        sys.exit(1)
    sid = sys.argv[1]
    try:
        run(sid)
    except KeyboardInterrupt:
        logging.getLogger(sid).info(f"[{sid}] 수동 종료")
    except Exception as e:
        logging.getLogger(sid).error(f"[{sid}] 치명적 오류:\n{traceback.format_exc()}")
        notify_error(f"[{sid}] 오류 종료: {e}")
