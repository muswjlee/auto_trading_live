"""오전 거래량 모멘텀 전략 (09:03)

흐름:
  [매수] 09:03 ~ 09:30
    1. 거래량 상위 100종목 중 등락률 5~10%, 주가 1만~10만원 필터
    2. 거래량 순 상위 3종목 시장가 1주씩 매수

  [매도]
    - 현재가 >= 매수가 × 1.01 (+1%) → bid1 지정가 매도
    - 14:30 미청산 포지션 시장가 강제 청산
"""

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kis_api import KISApi
from engine.monitor import (
    add_position, remove_position, record_trade, load_positions,
    reserve_or_skip, cancel_reservation, _sell_with_verify,
)
from engine.notifier import send, notify_buy, notify_sell, notify_error
from engine.screener import is_etf, is_etn, is_inverse

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE = os.getenv("KIS_MODE", "live")
_SID  = "open_momentum_0903"

log = logging.getLogger(_SID)


def _setup_logger():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        fh = logging.FileHandler(
            os.path.join(_BASE, f"trading_{_SID}_{_MODE}.log"), encoding="utf-8"
        )
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(fh)
        root.addHandler(sh)


def _load_strategy() -> dict:
    with open(os.path.join(_BASE, "strategies", f"{_SID}.json"), encoding="utf-8") as f:
        return json.load(f)


def _is_trading_day() -> bool:
    import holidays as _holidays
    d = date.today()
    if d.weekday() >= 5:
        return False
    FIXED = {(1,1),(3,1),(5,1),(5,5),(6,6),(8,15),(10,3),(10,9),(12,25)}
    if (d.month, d.day) in FIXED:
        return False
    try:
        return d not in _holidays.KR(years=d.year)
    except Exception:
        return True


# ── 스크리닝 ──────────────────────────────────────────────────────────────────

def _screen(api: KISApi, strategy: dict, exclude_codes: set) -> list[dict]:
    """거래량 상위 종목 중 조건 충족 종목 반환 (거래량 내림차순 상위 max_stocks개)"""
    cfg         = strategy["universe"]
    top_n       = cfg.get("top_n", 100)
    min_ret     = cfg["min_change_rate"]
    max_ret     = cfg["max_change_rate"]
    min_p       = cfg["min_price"]
    max_p       = cfg["max_price"]
    min_turnover = cfg.get("min_turnover_억", 10) * 100_000_000
    max_stk     = strategy["entry"]["max_stocks"]

    try:
        raw = api.get_volume_rank(
            top_n=top_n, market="0",
            min_price=min_p, max_price=max_p,
            by_turnover=False,
        )
    except Exception as e:
        log.error(f"거래량 조회 실패: {e}")
        return []

    passed = []
    for s in raw:
        if len(passed) >= max_stk:
            break

        code = s["code"]
        name = s["name"]

        if code in exclude_codes:
            continue
        if is_etf(name) or is_etn(name) or is_inverse(name):
            continue

        change_rate = s.get("change_rate", 0)
        if not (min_ret <= change_rate <= max_ret):
            continue

        turnover = s.get("turnover", 0)
        if turnover < min_turnover:
            log.info(f"  [{name}({code})] 거래대금 {turnover//100_000_000:.0f}억 < {min_turnover//100_000_000:.0f}억 → 제외")
            continue

        log.info(f"  ✅ {name}({code}) 등락률={change_rate:.1f}% 가격={s.get('price',0):,}원 거래대금={turnover//100_000_000:.0f}억")
        passed.append(s)

    log.info(f"스크리닝: {len(passed)}종목 통과")
    return passed


# ── 매수 ──────────────────────────────────────────────────────────────────────

def _buy_market(api: KISApi, code: str, name: str, qty: int) -> int:
    """시장가 매수 → 당일 체결 가중평균 반환 (실패 시 0)"""
    try:
        result   = api.buy(code, qty, price=0)
        order_no = result.get("odno", "") or result.get("ODNO", "")
        time.sleep(2.0)
        if order_no:
            for _ in range(5):
                p = api.get_buy_execution_price(order_no, code)
                if p > 0:
                    return p
                time.sleep(1.0)
        for _ in range(15):
            p = api.get_avg_buy_price(code)
            if p > 0:
                return p
            time.sleep(3.0)
        # 잔고 반영 지연 시 현재가로 대체 (주문 접수된 경우에 한함)
        if order_no:
            try:
                p = int(api.get_price(code).get("stck_prpr", 0))
                if p > 0:
                    log.warning(f"  [{name}] 체결가 조회 실패 — 현재가 {p:,}원으로 등록")
                    return p
            except Exception:
                pass
            log.error(f"  [{name}] 주문 접수됐으나 체결가 확인 불가 — 수동 확인 필요 (주문번호: {order_no})")
    except Exception as e:
        log.error(f"  [{name}] 매수 오류: {e}")
    return 0


# ── 매도 ──────────────────────────────────────────────────────────────────────



def _sell_market_force(api: KISApi, pos_key: str, pos: dict, force_label: str):
    """강제 시장가 매도"""
    code      = pos["code"]
    name      = pos["name"]
    qty       = pos["qty"]
    buy_price = pos["buy_price"]
    reason    = f"{force_label}강제청산"

    log.info(f"[{force_label} 강제매도] {name}({code}) {qty}주 시장가")
    order_no = ""
    try:
        result   = api.sell(code, qty, price=0)
        order_no = result.get("odno", "") or result.get("ODNO", "")
    except Exception as e:
        log.error(f"  강제매도 실패: {e}")
        remove_position(pos_key)
        return

    time.sleep(3.0)

    sell_price = 0
    if order_no:
        sell_price = api.get_sell_execution_price(order_no, code)
    if sell_price <= 0:
        try:
            sell_price = int(api.get_price(code).get("stck_prpr", buy_price))
        except Exception:
            sell_price = buy_price

    pnl_pct = (sell_price / buy_price - 1) * 100 if buy_price > 0 else 0
    record_trade(name, code, qty, buy_price, sell_price, reason, _SID)
    remove_position(pos_key)
    notify_sell(name, code, qty, buy_price, sell_price, reason, _SID)
    log.info(f"  [{name}] 강제매도완료 {buy_price:,}→{sell_price:,}원 수익={pnl_pct:+.2f}%")


# ── 모니터링 ──────────────────────────────────────────────────────────────────

def _check_exits(api: KISApi, cfg_exit: dict):
    """보유 포지션 익절/손절 체크
    - 항시: -sl_pct% 손절
    - 매수 후 1시간 이내: +tp_pct% 익절
    - 매수 후 1시간 이후: +tp_pct_after_1h% 익절
    """
    positions  = load_positions()
    tp_pct     = cfg_exit["tp_pct"]
    tp_pct_1h  = cfg_exit["tp_pct_after_1h"]
    sl_pct     = cfg_exit["sl_pct"]

    for pos_key, pos in list(positions.items()):
        if pos.get("strategy_id") != _SID:
            continue
        if pos.get("_reserved"):
            continue

        code      = pos["code"]
        buy_price = pos["buy_price"]

        try:
            cur = int(api.get_price(code).get("stck_prpr", 0))
            if cur <= 0:
                continue

            pnl_pct = (cur / buy_price - 1) * 100

            hold_min = 0
            bought_at_str = pos.get("bought_at", "")
            if bought_at_str:
                today     = datetime.now().date()
                bought_dt = datetime.strptime(bought_at_str, "%H:%M:%S").replace(
                    year=today.year, month=today.month, day=today.day
                )
                hold_min = (datetime.now() - bought_dt).total_seconds() / 60

            log.info(f"[모니터] {pos['name']}({code}) 매입={buy_price:,} 현재={cur:,} {pnl_pct:+.2f}% 보유{hold_min:.0f}분")

            if pnl_pct <= -sl_pct:
                log.info(f"  [손절] {pnl_pct:+.2f}% → 매도")
                _sell_with_verify(api, pos, pos_key, cur, f"손절({pnl_pct:+.1f}%)", _SID)
            elif hold_min >= 60:
                if pnl_pct >= tp_pct_1h:
                    log.info(f"  [1시간 후 익절] {pnl_pct:+.2f}% → 매도")
                    _sell_with_verify(api, pos, pos_key, cur, f"익절({pnl_pct:+.1f}%)", _SID)
            else:
                if pnl_pct >= tp_pct:
                    log.info(f"  [익절] +{tp_pct}% 도달 → 매도")
                    _sell_with_verify(api, pos, pos_key, cur, f"익절({pnl_pct:+.1f}%)", _SID)

        except Exception as e:
            log.warning(f"  [{pos.get('name','')}({code})] 모니터 오류: {e}")


def _force_exit_all(api: KISApi, force_label: str):
    """미청산 포지션 전량 시장가 매도"""
    positions = load_positions()
    my_pos = {k: v for k, v in positions.items()
              if v.get("strategy_id") == _SID and not v.get("_reserved")}

    if not my_pos:
        log.info(f"[{force_label}] 미청산 포지션 없음")
        return

    log.info(f"[{force_label}] 강제 청산 — {len(my_pos)}종목")
    for pos_key, pos in list(my_pos.items()):
        _sell_market_force(api, pos_key, pos, force_label)


# ── 메인 ──────────────────────────────────────────────────────────────────────

def _do_entry(api: KISApi, strategy: dict, slot_label: str, budget_per_stock: int = 0):
    """빈 슬롯 채우기: 현재 보유 < max_stocks이면 스크리닝 후 매수"""
    cfg_ent  = strategy["entry"]
    max_stk  = cfg_ent["max_stocks"]
    qty_fallback = cfg_ent.get("qty_per_stock", 1)

    if budget_per_stock <= 0:
        log.info(f"[{slot_label}] 예산 미설정 (budget=0) — 매수 스킵")
        return

    positions = load_positions()
    my_codes  = {v["code"] for v in positions.values()
                 if v.get("strategy_id") == _SID and not v.get("_reserved")}

    empty = max_stk - len(my_codes)
    if empty <= 0:
        log.info(f"[{slot_label}] 만석 ({max_stk}종목) — 스킵")
        return

    log.info(f"=== [{slot_label}] 스크리닝 (보유 {len(my_codes)}/{max_stk}, 빈슬롯 {empty}개, 예산 {budget_per_stock:,}원) ===")
    candidates = _screen(api, strategy, exclude_codes=my_codes)

    for s in candidates:
        positions = load_positions()
        my_codes  = {v["code"] for v in positions.values()
                     if v.get("strategy_id") == _SID and not v.get("_reserved")}
        if len(my_codes) >= max_stk:
            break

        code  = s["code"]
        name  = s["name"]
        price = s.get("price", 0)
        if price <= 0:
            log.warning(f"  [{name}] 가격 정보 없음(price=0) → qty={qty_fallback} fallback")
            qty = qty_fallback
        else:
            qty = max(1, budget_per_stock // price)

        if not reserve_or_skip(code, _SID):
            log.info(f"  [{name}] 이미 보유/예약 → 스킵")
            continue

        buy_price = _buy_market(api, code, name, qty)
        if buy_price > 0:
            add_position(code, name, buy_price, qty, _SID)
            notify_buy(name, code, qty, buy_price, strategy["name"])
            log.info(f"  매수완료: {name}({code}) {qty}주 @ {buy_price:,}원 (예산 {budget_per_stock:,}원)")
        else:
            cancel_reservation(code, _SID)
            log.info(f"  매수실패: {name}({code})")


def run():
    _setup_logger()
    strategy = _load_strategy()

    if not strategy.get("enabled", True):
        log.info(f"[{_SID}] 비활성화 → 종료")
        return

    if not _is_trading_day():
        log.info(f"[{_SID}] 오늘은 휴장일 → 종료")
        return

    cfg_exit = strategy["exit"]
    cfg_ent  = strategy["entry"]
    sch      = strategy["schedule"]

    # 진입 슬롯: ["09:03", "09:10", "09:20", "09:29"] → [903, 910, 920, 929]
    entry_slots = [int(t.replace(":", "")) for t in sch.get("entry_slots", [])]
    end_t       = int(sch["entry_end_time"].replace(":", ""))
    interval    = sch.get("monitor_interval_sec", 10)
    force_t     = int(cfg_exit["force_exit_time"].replace(":", ""))
    force_label = cfg_exit["force_exit_time"]
    reserve_cash = cfg_ent.get("reserve_cash", 0)
    max_stk      = cfg_ent["max_stocks"]

    first_slot = min(entry_slots) if entry_slots else end_t

    log.info(f"=== [{_SID}] 시작 [{date.today()}] ===")
    log.info(f"진입슬롯: {[f'{t//100:02d}:{t%100:02d}' for t in sorted(entry_slots)]}")
    api = KISApi()

    slots_done       = set()
    force_exit_done  = False
    summary_sent     = False
    budget_per_stock = 0
    budget_calc_done = False

    while True:
        now   = datetime.now()
        now_t = now.hour * 100 + now.minute

        # 08:55 예수금 기반 종목당 예산 계산 (1회)
        if not budget_calc_done and now_t >= 855:
            try:
                cash  = api.get_cash()
                avail = max(0, cash - reserve_cash)
                budget_per_stock = avail // max_stk
                log.info(
                    f"[08:55] 예수금 {cash:,}원 − 예비금 {reserve_cash:,}원 "
                    f"= 가용 {avail:,}원 → 종목당 {budget_per_stock:,}원"
                )
                budget_calc_done = True
            except Exception as e:
                log.warning(f"[08:55] 예수금 조회 실패: {e}")

        # 15:35 이후 종료
        if now_t >= 1535:
            if not summary_sent:
                positions = load_positions()
                held = [p for p in positions.values()
                        if p.get("strategy_id") == _SID and not p.get("_reserved")]
                send(f"<b>[{strategy['name']}]</b> 오늘 결산 | 보유중: {len(held)}종목")
                summary_sent = True
            log.info(f"[{_SID}] 15:35 이후 — 종료")
            break

        # 강제 청산 (1회)
        if now_t >= force_t and not force_exit_done:
            _force_exit_all(api, force_label)
            force_exit_done = True

        # 익절/손절 모니터링 (첫 슬롯 이후 ~ 강제청산)
        if first_slot <= now_t < force_t:
            _check_exits(api, cfg_exit)

        # 진입 슬롯 체크 (14:00 이전)
        if now_t < end_t:
            for slot_t in sorted(entry_slots):
                if slot_t <= now_t and slot_t not in slots_done:
                    slots_done.add(slot_t)
                    label = f"{slot_t//100:02d}:{slot_t%100:02d}"
                    _do_entry(api, strategy, label, budget_per_stock)

        time.sleep(interval)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info(f"[{_SID}] 수동 종료")
    except Exception as e:
        log.error(f"[{_SID}] 오류:\n{traceback.format_exc()}")
        notify_error(f"[{_SID}] 오류 종료: {e}")
