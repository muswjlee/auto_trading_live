"""장기 이동평균 돌파 전략 (14:30)

흐름:
  1. 14:30 전 종목 스크리닝
     - 당일 거래대금 1000억 이상 1차 필터 (상위 500종목)
     - 현재가 > 시가, 현재가 >= 당일고가 × 95%
     - 240일선 또는 480일선 상향 돌파 (전일종가 < MA <= 현재가)
     - 당일 누적 거래량 >= 60일 평균 거래량 × 5배
  2. 거래대금 상위 3종목 → 최우선 매수호가(bid1) 지정가 매수 (14:30~14:40)
  3. 익절 +30% 전량 매도
  4. 30 거래일 경과 후 미도달 시 강제 전량 매도
  5. 손절 없음
"""

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kis_api import KISApi
from engine.monitor import (
    add_position, remove_position, record_trade, load_positions,
    reserve_or_skip, cancel_reservation, update_position_extra,
)
from engine.notifier import send, notify_buy, notify_sell, notify_error
from engine.screener import is_etf, is_etn

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE = os.getenv("KIS_MODE", "live")
_SID  = "ma_breakout_1430"

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


def _count_trading_days(start: date, end: date) -> int:
    """start 다음 날부터 end까지 실제 거래일 수"""
    try:
        import holidays as _holidays
        kr_hols = _holidays.KR(years=range(start.year, end.year + 1))
    except Exception:
        kr_hols = set()
    count = 0
    d = start + timedelta(days=1)
    while d <= end:
        if d.weekday() < 5 and d not in kr_hols:
            count += 1
        d += timedelta(days=1)
    return count


# ── 스크리닝 ──────────────────────────────────────────────────────────────────

def _screen(api: KISApi, strategy: dict) -> list[dict]:
    """14:30 전 종목 스크리닝 — 조건 충족 종목 거래대금 내림차순 반환"""
    cfg_uni   = strategy["universe"]
    cfg_scr   = strategy["screen"]
    min_trn   = cfg_uni["min_turnover_억"] * 1e8   # 1000억
    top_n     = cfg_uni.get("top_candidates", 500)
    ma_days   = cfg_scr["ma_days"]                 # [240, 480]
    vol_ratio = cfg_scr["volume_ratio_60d"]        # 5.0
    near_high = cfg_scr["near_high_pct"] / 100     # 0.95
    ohlcv_cnt = max(ma_days) + 10                  # 여유분 포함

    passed = []

    # 거래대금 상위 종목 조회 (1000억 이상이 포함될 만큼 넉넉하게)
    try:
        raw = api.get_volume_rank(top_n=top_n, market="0", by_turnover=True)
    except Exception as e:
        log.error(f"유니버스 조회 실패: {e}")
        return []

    log.info(f"유니버스 {len(raw)}종목 → 스크리닝 시작")

    for s in raw:
        code = s["code"]
        name = s["name"]

        if is_etf(name) or is_etn(name):
            continue

        try:
            info = api.get_price(code)

            # 관리·경고 종목 제외
            if cfg_uni.get("exclude_managed") and info.get("mrkt_warn_cls_code", "00") != "00":
                continue

            cur      = int(info.get("stck_prpr", 0))
            open_p   = int(info.get("stck_oprc", 0))
            high     = int(info.get("stck_hgpr", 0))
            trn      = float(info.get("acml_tr_pbmn", 0))
            acml_vol = int(info.get("acml_vol", 0))

            if cur <= 0 or open_p <= 0 or high <= 0:
                continue

            # 거래대금 1000억 이상
            if trn < min_trn:
                continue

            # 현재가 > 시가
            if cur <= open_p:
                log.info(f"  {name} 현재가 {cur:,} ≤ 시가 {open_p:,} → 제외")
                continue

            # 현재가 >= 당일고가 × 95%
            if cur < high * near_high:
                log.info(f"  {name} 현재가 {cur:,} < 고가 {high:,}×{near_high:.0%} → 제외")
                continue

            # OHLCV 조회 (MA + 거래량 비율)
            ohlcv = api.get_ohlcv(code, period="D", count=ohlcv_cnt)
            # 실제 거래일(거래량 > 0)만 사용
            ohlcv = [o for o in ohlcv if int(o.get("acml_vol", 0)) > 0]

            if len(ohlcv) < max(ma_days):
                log.info(f"  {name} 데이터 부족 ({len(ohlcv)}일) → 제외")
                continue

            closes  = [float(o["stck_clpr"]) for o in ohlcv]
            volumes = [int(o.get("acml_vol", 0)) for o in ohlcv]

            # ohlcv[0] = 가장 최근 거래일(전일) 종가
            prev_close = closes[0]

            # 240일선 또는 480일선 상향 돌파 체크
            breakout_ma = None
            for ma_d in ma_days:
                if len(closes) < ma_d:
                    continue
                ma_val = sum(closes[:ma_d]) / ma_d
                if cur >= ma_val and prev_close < ma_val:
                    breakout_ma = ma_d
                    log.info(
                        f"  {name} MA{ma_d} 돌파 "
                        f"(MA={ma_val:,.0f} 전일종가={prev_close:,.0f} 현재가={cur:,})"
                    )
                    break

            if breakout_ma is None:
                continue

            # 당일 누적 거래량 >= 60일 평균 × 5배
            avg60_vol = sum(volumes[:60]) / 60 if len(volumes) >= 60 else 0
            if avg60_vol <= 0:
                continue
            ratio = acml_vol / avg60_vol
            if ratio < vol_ratio:
                log.info(f"  {name} 거래량비율 {ratio:.1f}배 < {vol_ratio}배 → 제외")
                continue

            log.info(
                f"  ✅ {name}({code}) 거래대금={trn/1e8:.0f}억 "
                f"거래량비율={ratio:.1f}배 MA{breakout_ma}돌파 현재가={cur:,}"
            )
            passed.append({
                "code":       code,
                "name":       name,
                "cur":        cur,
                "turnover":   trn,
                "vol_ratio":  ratio,
                "breakout_ma": breakout_ma,
            })
            time.sleep(0.15)

        except Exception as e:
            log.warning(f"  {name}({code}) 스크리닝 오류: {e}")
            time.sleep(0.1)

    passed.sort(key=lambda x: -x["turnover"])
    log.info(f"스크리닝 완료: {len(passed)}종목 통과")
    return passed


# ── 매수 (bid1 지정가) ────────────────────────────────────────────────────────

def _buy_bid1(api: KISApi, code: str, name: str, qty: int, wait_sec: int) -> int:
    """최우선 매수호가(bid1) 지정가 매수 → 미체결 시 1회 정정 → 취소
    Returns: 체결가 (int), 실패/취소 시 0"""
    try:
        ob   = api.get_orderbook(code)
        bid1 = int(ob.get("bidp1", 0))
        if bid1 <= 0:
            log.warning(f"  [{name}] 매수호가1 조회 실패 → 매수 포기")
            return 0

        log.info(f"[매수주문] {name}({code}) {qty}주 @ {bid1:,}원 (bid1 지정가)")
        result   = api.buy(code, qty, price=bid1)
        order_no = result.get("odno", "")

        time.sleep(wait_sec)

        pending = api.get_pending_orders()
        if not any(p.get("odno") == order_no for p in pending):
            return _get_fill_price(api, code)

        # 1회 정정
        ob2  = api.get_orderbook(code)
        bid2 = int(ob2.get("bidp1", bid1))
        log.info(f"  미체결 → 정정 {bid1:,} → {bid2:,}원")
        try:
            r2       = api.modify_order(order_no, code, qty, bid2)
            order_no = r2.get("odno", order_no)
        except Exception as e:
            log.warning(f"  정정 실패: {e}")

        time.sleep(wait_sec)

        pending = api.get_pending_orders()
        if not any(p.get("odno") == order_no for p in pending):
            return _get_fill_price(api, code)

        # 취소
        log.info(f"  재정정 후도 미체결 → 취소")
        try:
            api.cancel_order(order_no, code, qty)
        except Exception:
            pass
        return 0

    except Exception as e:
        log.error(f"  [{name}] 매수 오류: {e}")
        return 0


def _get_fill_price(api: KISApi, code: str) -> int:
    for _ in range(5):
        p = api.get_avg_buy_price(code)
        if p > 0:
            return p
        time.sleep(1.0)
    return 0


# ── 매도 (bid1 지정가 → 시장가 fallback) ──────────────────────────────────────

def _sell_position(api: KISApi, pos_key: str, pos: dict, reason: str):
    """bid1 지정가 매도 → 미체결 시 1회 정정 → 시장가 fallback"""
    code      = pos["code"]
    name      = pos["name"]
    qty       = pos["qty"]
    buy_price = pos["buy_price"]
    wait_sec  = 30

    try:
        info = api.get_price(code)
        cur  = int(info.get("stck_prpr", buy_price))
    except Exception:
        cur = buy_price

    try:
        ob   = api.get_orderbook(code)
        bid1 = int(ob.get("bidp1", cur))
        if bid1 <= 0:
            bid1 = cur
    except Exception:
        bid1 = cur

    log.info(f"[매도주문] {name}({code}) {qty}주 @ {bid1:,}원 사유={reason}")
    order_no = ""
    try:
        result   = api.sell(code, qty, price=bid1)
        order_no = result.get("odno", "") or result.get("ODNO", "")
    except Exception as e:
        log.error(f"  매도 주문 실패: {e}")
        return

    time.sleep(wait_sec)

    pending = api.get_pending_orders()
    if any(p.get("odno") == order_no for p in pending):
        try:
            ob2  = api.get_orderbook(code)
            bid2 = int(ob2.get("bidp1", bid1))
            api.modify_order(order_no, code, qty, bid2)
        except Exception:
            pass
        time.sleep(wait_sec)

        pending = api.get_pending_orders()
        if any(p.get("odno") == order_no for p in pending):
            log.info(f"  미체결 지속 → 시장가 강제 매도")
            try:
                api.cancel_order(order_no, code, qty)
                time.sleep(0.5)
            except Exception:
                pass
            try:
                r3       = api.sell(code, qty, price=0)
                order_no = r3.get("odno", "") or r3.get("ODNO", "")
                time.sleep(3.0)
            except Exception as e:
                log.error(f"  시장가 매도 실패: {e}")

    # 체결가 조회
    sell_price = cur
    if order_no:
        time.sleep(0.5)
        fetched = api.get_sell_execution_price(order_no, code)
        if fetched > 0:
            sell_price = fetched

    pnl_pct = (sell_price / buy_price - 1) * 100 if buy_price > 0 else 0
    record_trade(name, code, qty, buy_price, sell_price, reason, _SID)
    remove_position(pos_key)
    notify_sell(name, code, qty, buy_price, sell_price, reason, _SID)
    log.info(
        f"  [{name}] 매도완료 {buy_price:,}→{sell_price:,}원 "
        f"수익={pnl_pct:+.2f}% 사유={reason}"
    )


# ── 포지션 모니터링 ───────────────────────────────────────────────────────────

def _check_exits(api: KISApi, strategy: dict):
    """보유 포지션 TP(+30%) 및 30거래일 시간 청산 체크"""
    positions = load_positions()
    if not positions:
        return

    tp_ratio = strategy["exit"]["take_profit"] / 100        # 0.30
    max_td   = strategy["exit"]["max_hold_trading_days"]    # 30
    today    = date.today()

    for pos_key, pos in list(positions.items()):
        if pos.get("strategy_id") != _SID:
            continue
        if pos.get("_reserved"):
            continue

        code      = pos["code"]
        name      = pos["name"]
        buy_price = pos["buy_price"]

        try:
            info = api.get_price(code)
            cur  = int(info.get("stck_prpr", 0))
            if cur <= 0:
                continue

            pnl_pct = (cur - buy_price) / buy_price

            # 경과 거래일 계산
            td_str = pos.get("bought_date", "")
            td_count = None
            if td_str:
                try:
                    bought_date = date.fromisoformat(td_str)
                    td_count = _count_trading_days(bought_date, today)
                except Exception:
                    pass

            log.info(
                f"[모니터] {name}({code}) 매입 {buy_price:,} → 현재 {cur:,} "
                f"({pnl_pct*100:+.2f}%)"
                + (f" 경과{td_count}거래일" if td_count is not None else "")
            )

            # 익절 +30%
            if pnl_pct >= tp_ratio:
                log.info(f"[익절] {name}({code}) {pnl_pct*100:+.2f}% → 매도")
                _sell_position(api, pos_key, pos, "익절")
                continue

            # 30거래일 시간 청산
            if td_count is not None and td_count >= max_td:
                log.info(f"[시간청산] {name}({code}) {td_count}거래일 경과 → 매도")
                _sell_position(api, pos_key, pos, "시간청산")

        except Exception as e:
            log.warning(f"  {name}({code}) 모니터 오류: {e}")


# ── 메인 ──────────────────────────────────────────────────────────────────────

def run():
    _setup_logger()
    strategy = _load_strategy()

    if not strategy.get("enabled", True):
        log.info(f"[{_SID}] 비활성화 → 종료")
        return

    if not _is_trading_day():
        log.info(f"[{_SID}] 오늘은 휴장일 → 종료")
        return

    cfg_ent  = strategy["entry"]
    entry_t  = int(strategy["schedule"]["entry_time"].replace(":", ""))      # 1430
    end_t    = int(strategy["schedule"]["entry_end_time"].replace(":", ""))  # 1440
    interval = strategy["schedule"].get("monitor_interval_sec", 60)
    wait_sec = cfg_ent["unfilled_wait_sec"]
    max_stk  = cfg_ent["max_stocks"]
    amount   = cfg_ent["amount_per_stock"]
    today    = date.today().isoformat()

    log.info(f"=== [{_SID}] 시작 [{today}] ===")
    api = KISApi()

    entry_done   = False
    summary_sent = False

    while True:
        now_t = datetime.now().hour * 100 + datetime.now().minute

        # 15:35 이후 종료
        if now_t >= 1535:
            if not summary_sent:
                positions = load_positions()
                held = [
                    p for p in positions.values()
                    if p.get("strategy_id") == _SID and not p.get("_reserved")
                ]
                send(
                    f"<b>[{strategy['name']}]</b> 오늘 결산\n"
                    f"보유중: {len(held)}종목"
                )
                summary_sent = True
            log.info(f"[{_SID}] 15:35 이후 — 종료")
            break

        # 보유 포지션 TP / 시간청산 체크
        _check_exits(api, strategy)

        # 14:30 ~ 14:40 매수 (1일 1회)
        if not entry_done and entry_t <= now_t < end_t:
            log.info("=== 14:30 스크리닝 시작 ===")
            candidates = _screen(api, strategy)
            selected   = candidates[:max_stk]

            if not selected:
                log.info("스크리닝 통과 종목 없음")
                send(f"<b>[{strategy['name']}]</b> 스크리닝 통과 종목 없음")
            else:
                send(
                    f"<b>[{strategy['name']}]</b> {len(selected)}종목 선정\n"
                    + "\n".join(
                        f"  {s['name']} MA{s['breakout_ma']}돌파 "
                        f"거래대금={s['turnover']/1e8:.0f}억 "
                        f"거래량{s['vol_ratio']:.1f}배"
                        for s in selected
                    )
                )

                for s in selected:
                    code = s["code"]
                    name = s["name"]
                    cur  = s["cur"]

                    if not reserve_or_skip(code, _SID):
                        log.info(f"  [{name}] 이미 보유/예약 중 → 스킵")
                        continue

                    qty = max(1, amount // cur)
                    buy_price = _buy_bid1(api, code, name, qty, wait_sec)

                    if buy_price > 0:
                        add_position(code, name, buy_price, qty, _SID)
                        update_position_extra(f"{code}_{_SID}", {"bought_date": today})
                        notify_buy(name, code, qty, buy_price, strategy["name"])
                        log.info(f"  매수 완료: {name}({code}) {qty}주 @ {buy_price:,}원")
                    else:
                        cancel_reservation(code, _SID)
                        log.info(f"  매수 실패/취소: {name}({code})")

            entry_done = True

        time.sleep(interval)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info(f"[{_SID}] 수동 종료")
    except Exception as e:
        log.error(f"[{_SID}] 오류:\n{traceback.format_exc()}")
        notify_error(f"[{_SID}] 오류 종료: {e}")
