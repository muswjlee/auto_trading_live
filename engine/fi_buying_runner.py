"""외국인·기관 순매수 전략 — 09:05 진입
전날 21:00 스크리닝으로 선정된 상위 3종목을
현재가 > 전일 종가인 경우 1주씩 시장가 매수
익절/손절 ±1%, 15:20 강제청산
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
from engine.monitor import add_position, load_positions, remove_position, record_trade
from engine.notifier import notify_buy, notify_sell, notify_error, send

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE = os.getenv("KIS_MODE", "live")
_SID  = "fi_buying_1430"

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


def _load_candidates() -> list:
    path = os.path.join(_BASE, f"fi_buying_candidates_{_MODE}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        screen_date = data.get("screen_date", "")
        log.info(f"후보 파일 로드: screen_date={screen_date} ({len(data.get('candidates', []))}개)")
        return data.get("candidates", [])
    except FileNotFoundError:
        log.warning("후보 파일 없음 (fi_buying_candidates_*.json) — 매수 없음")
        return []


def _my_positions() -> dict:
    return {k: v for k, v in load_positions().items() if v.get("strategy_id") == _SID}


def _sell(api: KISApi, pos: dict, pos_key: str, cur_price: int, reason: str):
    code      = pos["code"]
    name      = pos["name"]
    buy_price = pos["buy_price"]
    qty       = pos["qty"]
    try:
        result   = api.sell(code, qty, price=0)
        order_no = result.get("odno", "")
        sell_price = 0
        for _ in range(5):
            time.sleep(1.0)
            sell_price = api.get_sell_execution_price(order_no, code)
            if sell_price > 0:
                break
        if sell_price <= 0:
            sell_price = cur_price if cur_price > 0 else buy_price

        pnl_pct = (sell_price - buy_price) / buy_price * 100
        record_trade(name, code, qty, buy_price, sell_price, reason, _SID)
        remove_position(pos_key)
        notify_sell(name, code, qty, buy_price, sell_price, reason, _SID)
        log.info(f"  [{reason}] {name} {buy_price:,}→{sell_price:,}원 ({pnl_pct:+.2f}%)")
    except Exception as e:
        log.error(f"  매도 오류 [{name}]: {e}")
        notify_error(f"[{_SID}] 매도 실패 {name}({code}): {e}")


def run():
    _setup_logger()
    strategy = _load_strategy()

    if not strategy.get("enabled", False):
        log.info(f"[{_SID}] 비활성화 — 종료")
        return

    entry_time_str = strategy["schedule"].get("entry_time", "09:05")
    force_exit_str = strategy["exit"].get("force_exit_time", "15:20")
    tp_pct         = strategy["exit"].get("tp_pct", 1.0)
    sl_pct         = strategy["exit"].get("sl_pct", 1.0)
    qty_each       = strategy["entry"].get("qty", 1)

    eh, em = map(int, entry_time_str.split(":"))
    fh, fm = map(int, force_exit_str.split(":"))
    entry_t     = eh * 100 + em
    force_exit_t = fh * 100 + fm

    candidates = _load_candidates()
    api = KISApi()

    if not candidates:
        send(f"⚠️ [{_SID}] 후보 종목 없음 — 오늘 매수 없음")

    entry_done   = False
    summary_sent = False

    log.info(
        f"[{_SID}] 시작 | 진입={entry_time_str} | 후보={len(candidates)}개 "
        f"| TP={tp_pct}% SL={sl_pct}%"
    )

    while True:
        now = datetime.now()
        t   = now.hour * 100 + now.minute

        # ── 09:05 매수 ────────────────────────────────────────────────────
        if not entry_done and t >= entry_t:
            entry_done = True
            bought = []
            for c in candidates:
                code = c["code"]
                name = c["name"]

                # 이미 보유 중이면 스킵 (워치독 재시작으로 인한 중복 방지)
                if f"{code}_{_SID}" in load_positions():
                    log.info(f"  [{name}] 이미 보유 중 — 스킵")
                    continue

                try:
                    price_info = api.get_price(code)
                    cur  = int(price_info.get("stck_prpr", 0))
                    prev = int(price_info.get("stck_prdy_clpr", 0))  # 전일 종가

                    if cur <= 0 or prev <= 0:
                        log.warning(f"  [{name}] 가격 조회 실패 — 스킵")
                        continue

                    if cur <= prev:
                        log.info(
                            f"  [{name}] 현재가({cur:,}) ≤ 전일종가({prev:,}) "
                            f"({(cur/prev-1)*100:+.2f}%) — 조건 미충족, 스킵"
                        )
                        continue

                    log.info(
                        f"  [{name}({code})] 현재가={cur:,} > 전일종가={prev:,} "
                        f"(+{(cur/prev-1)*100:.2f}%) → {qty_each}주 매수"
                    )
                    result   = api.buy(code, qty_each, price=0)
                    order_no = result.get("odno", "")
                    time.sleep(2.0)

                    actual_price = 0
                    if order_no:
                        for _ in range(5):
                            actual_price = api.get_buy_execution_price(order_no, code)
                            if actual_price > 0:
                                break
                            time.sleep(1.0)
                    if actual_price <= 0:
                        actual_price = api.get_avg_buy_price(code)
                    if actual_price <= 0:
                        actual_price = cur

                    add_position(code, name, actual_price, qty_each, _SID)
                    notify_buy(
                        name, code, qty_each, actual_price,
                        f"외국인·기관 순매수 | 전일比 +{(cur/prev-1)*100:.1f}%"
                    )
                    bought.append(name)
                    log.info(f"  매수 완료: {actual_price:,}원 × {qty_each}주")

                except Exception as e:
                    log.error(f"  [{name}] 매수 오류: {e}")
                    notify_error(f"[{_SID}] 매수 실패 {name}({code}): {e}")
                time.sleep(0.5)

            if not bought:
                log.info("진입 조건 충족 종목 없음 — 오늘 매수 없음")
                send(f"⚠️ [{_SID}] 진입 조건 미충족 — 전일종가 이상 종목 없음")

        # ── 익절/손절 모니터링 ────────────────────────────────────────────
        for pos_key, pos in list(_my_positions().items()):
            code      = pos["code"]
            name      = pos["name"]
            buy_price = pos["buy_price"]
            try:
                cur = int(api.get_price(code).get("stck_prpr", 0))
                if cur <= 0:
                    continue
                pnl_pct = (cur - buy_price) / buy_price * 100

                if pnl_pct >= tp_pct:
                    log.info(f"  [익절] {name} {pnl_pct:+.2f}%")
                    _sell(api, pos, pos_key, cur, f"익절({pnl_pct:+.1f}%)")
                elif pnl_pct <= -sl_pct:
                    log.info(f"  [손절] {name} {pnl_pct:+.2f}%")
                    _sell(api, pos, pos_key, cur, f"손절({pnl_pct:+.1f}%)")
            except Exception as e:
                log.warning(f"  [{name}] 모니터링 오류: {e}")

        # ── 15:20 강제청산 ────────────────────────────────────────────────
        if t >= force_exit_t:
            for pos_key, pos in list(_my_positions().items()):
                try:
                    cur = int(api.get_price(pos["code"]).get("stck_prpr", 0))
                    _sell(api, pos, pos_key, cur, "강제청산")
                except Exception as e:
                    log.error(f"  강제청산 오류 [{pos['name']}]: {e}")

            if not summary_sent:
                summary_sent = True
                log.info(f"[{_SID}] 강제청산 완료 — 종료")
                return

        # ── 15:35 결산 후 종료 ────────────────────────────────────────────
        if t >= 1535 and not summary_sent:
            summary_sent = True
            log.info(f"[{_SID}] 종료")
            return

        time.sleep(5)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info(f"[{_SID}] 수동 종료")
    except Exception as e:
        log.error(f"[{_SID}] 오류:\n{traceback.format_exc()}")
        notify_error(f"[{_SID}] 오류 종료: {e}")
