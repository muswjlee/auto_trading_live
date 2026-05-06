"""5틱 모멘텀 스캘핑 전략 실행기"""

import os
import sys
import json
import time
import logging
import threading
import traceback
from datetime import datetime, timedelta
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kis_api import KISApi
from engine.notifier import send, notify_error
from engine.screener import is_etf
from engine.runner import _is_trading_day

log = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_tick_size(price: int) -> int:
    if price < 2_000:   return 1
    if price < 5_000:   return 5
    if price < 20_000:  return 10
    if price < 50_000:  return 50
    if price < 200_000: return 100
    if price < 500_000: return 500
    return 1_000


class TickScalper:
    """단일 ETF 종목 틱 스캘핑 모니터"""

    def __init__(self, code: str, name: str, cfg: dict, api: KISApi,
                 state: dict, lock: threading.Lock):
        self.code   = code
        self.name   = name
        self.cfg    = cfg
        self.api    = api
        self.state  = state
        self.lock   = lock

        self.prices  = deque(maxlen=cfg["entry"]["consecutive_up_ticks"] + 2)
        self.prev_vol = 0
        self.position = None
        self.cooldown_until = None

    # ── 공유 상태 ────────────────────────────────────────────────────────────

    def _is_halted(self) -> bool:
        with self.lock:
            return self.state["halted"]

    def _record_trade(self, pnl: int):
        with self.lock:
            self.state["daily_pnl"] += pnl
            if pnl < 0:
                self.state["consecutive_losses"] += 1
            else:
                self.state["consecutive_losses"] = 0

            losses = self.state["consecutive_losses"]
            if losses >= self.cfg["risk"]["max_consecutive_losses"]:
                self.state["halted"] = True
                send(f"[틱스캘핑] 연속 손실 {losses}회 — 오늘 거래 중단")
                return

            max_loss = self.cfg["risk"]["max_daily_loss_pct"] / 100 * self.state["capital"]
            if self.state["daily_pnl"] <= max_loss:
                self.state["halted"] = True
                send(f"[틱스캘핑] 일손실 {self.state['daily_pnl']:,}원 한도 초과 — 오늘 거래 중단")

    # ── 진입 ─────────────────────────────────────────────────────────────────

    def _check_entry(self, info: dict) -> bool:
        if self.position:
            return False
        if self.cooldown_until and datetime.now() < self.cooldown_until:
            return False

        price = int(info.get("stck_prpr", 0))
        ask   = int(info.get("askp",  info.get("askp1",  0)))
        bid   = int(info.get("bidp",  info.get("bidp1",  0)))
        vol   = int(info.get("acml_vol", 0))

        if price <= 0 or ask <= 0 or bid <= 0:
            return False

        self.prices.append(price)
        n = self.cfg["entry"]["consecutive_up_ticks"]
        prices = list(self.prices)

        if len(prices) < n + 1:
            return False

        # 5틱 연속상승
        consecutive_up = all(prices[-i] > prices[-i - 1] for i in range(1, n + 1))

        # 거래량 증가
        vol_ok = vol > self.prev_vol if self.prev_vol > 0 else False
        self.prev_vol = vol

        # 호가 안정: 스프레드 <= max_spread_ticks
        tick   = get_tick_size(price)
        spread = ask - bid
        spread_ok = spread <= self.cfg["entry"]["max_spread_ticks"] * tick

        return consecutive_up and vol_ok and spread_ok

    def _enter(self, info: dict):
        ask  = int(info.get("askp", info.get("askp1", 0)))
        if ask <= 0:
            return

        tick   = get_tick_size(ask)
        amount = self.cfg["entry"]["amount_per_stock"]
        qty    = max(1, amount // ask)

        try:
            result = self.api.buy(self.code, qty, price=ask)
            tp = ask + self.cfg["exit"]["take_profit_ticks"] * tick
            sl = ask - self.cfg["exit"]["stop_loss_ticks"]   * tick

            self.position = {
                "qty":         qty,
                "entry_price": ask,
                "tp_price":    tp,
                "sl_price":    sl,
                "deadline":    datetime.now() + timedelta(seconds=self.cfg["exit"]["time_limit_sec"]),
                "order_no":    result.get("odno", ""),
            }
            log.info(f"[매수] {self.name}({self.code}) {qty}주 @{ask:,} TP={tp:,} SL={sl:,}")
            send(f"[틱스캘핑] 매수 {self.name} {qty}주 @{ask:,}원")
        except Exception as e:
            log.warning(f"[매수 실패] {self.name}: {e}")

    # ── 청산 ─────────────────────────────────────────────────────────────────

    def _check_exit(self, info: dict):
        if not self.position:
            return
        price = int(info.get("stck_prpr", 0))
        pos   = self.position
        now   = datetime.now()

        if   price >= pos["tp_price"]:  reason = "익절"
        elif price <= pos["sl_price"]:  reason = "손절"
        elif now   >= pos["deadline"]:  reason = "시간청산"
        else:                           return

        self._exit(price, reason)

    def _exit(self, current_price: int, reason: str):
        pos = self.position
        try:
            self.api.sell(self.code, pos["qty"])
            pnl = (current_price - pos["entry_price"]) * pos["qty"]
            log.info(f"[매도] {self.name}({self.code}) {reason} @{current_price:,} 손익={pnl:+,}원")
            send(f"[틱스캘핑] {reason} {self.name} @{current_price:,}원 ({pnl:+,}원)")
            self._record_trade(pnl)
        except Exception as e:
            log.error(f"[매도 실패] {self.name}: {e}")
            notify_error(f"[틱스캘핑] 매도 실패 {self.name}: {e}")
        finally:
            self.position = None
            self.cooldown_until = datetime.now() + timedelta(
                seconds=self.cfg["risk"]["reentry_cooldown_sec"]
            )

    # ── 사이클 진입점 ────────────────────────────────────────────────────────

    def update(self, info: dict):
        if self._is_halted():
            return
        if self.position:
            self._check_exit(info)
        elif self._check_entry(info):
            self._enter(info)


# ── ETF 유니버스 ─────────────────────────────────────────────────────────────

def _get_etf_universe(api: KISApi, cfg: dict) -> list[dict]:
    top_n     = cfg["universe"]["top_n"]
    min_price = cfg["universe"].get("min_price", 0)
    max_price = cfg["universe"].get("max_price", 0)

    raw = api._fetch_volume_rank_page("4", min_price, max_price, 0)

    etfs = []
    for item in raw:
        name  = item.get("hts_kor_isnm", "")
        code  = item.get("mksc_shrn_iscd", "")
        price = int(item.get("stck_prpr", 0))
        vol   = int(item.get("acml_vol", 0))
        tr_val = int(item.get("acml_tr_pbmn", vol * price))

        if not is_etf(name):
            continue
        if min_price and price < min_price:
            continue
        if max_price and price > max_price:
            continue

        etfs.append({"code": code, "name": name, "price": price, "tr_val": tr_val})

    etfs.sort(key=lambda x: x["tr_val"], reverse=True)
    result = etfs[:top_n]
    log.info(f"ETF 유니버스 {len(result)}개: {[e['name'] for e in result]}")
    return result


# ── 메인 ─────────────────────────────────────────────────────────────────────

def _setup_logger(strategy_id: str):
    log_file = os.path.join(_BASE, f"trading_{strategy_id}.log")
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


def run(strategy_id: str):
    cfg_path = os.path.join(_BASE, "strategies", f"{strategy_id}.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    _setup_logger(strategy_id)
    sid = cfg["id"]

    if not _is_trading_day():
        log.info(f"[{sid}] 오늘은 휴장일 — 실행 종료")
        send(f"[{sid}] 오늘은 휴장일입니다.")
        return

    schedule = cfg["schedule"]
    start_h, start_m = map(int, schedule["start_time"].split(":"))
    end_h,   end_m   = map(int, schedule["end_time"].split(":"))
    start_t = start_h * 100 + start_m
    end_t   = end_h   * 100 + end_m
    poll    = schedule["poll_interval_sec"]

    api = KISApi()
    log.info(f"[{sid}] 틱스캘핑 시작 | {schedule['start_time']}~{schedule['end_time']}")

    try:
        capital = api.get_cash()
    except Exception:
        capital = 10_000_000

    state = {"daily_pnl": 0, "consecutive_losses": 0, "halted": False, "capital": capital}
    lock  = threading.Lock()

    etf_list = _get_etf_universe(api, cfg)
    if not etf_list:
        log.error(f"[{sid}] ETF 유니버스 조회 실패 — 종료")
        return

    scalpers = [TickScalper(e["code"], e["name"], cfg, api, state, lock) for e in etf_list]
    send(f"[틱스캘핑] 시작 — {len(scalpers)}개 ETF 모니터링 중")

    while True:
        now = datetime.now()
        t   = now.hour * 100 + now.minute

        # 장 시작 전 대기
        if t < start_t:
            time.sleep(30)
            continue

        # 장 마감 — 미청산 포지션 시장가 정리 후 종료
        if t >= end_t:
            for sc in scalpers:
                if sc.position:
                    try:
                        info = api.get_price(sc.code)
                        sc._exit(int(info.get("stck_prpr", 0)), "장마감 청산")
                    except Exception as e:
                        log.error(f"[장마감 청산 실패] {sc.name}: {e}")
            log.info(f"[{sid}] 장마감 종료")
            return

        # 서킷브레이커 발동 시 대기
        if state["halted"]:
            time.sleep(30)
            continue

        # ETF 순환 업데이트 (종목 간 짧은 간격으로 API 레이트리밋 방지)
        for sc in scalpers:
            try:
                info = api.get_price(sc.code)
                sc.update(info)
            except Exception as e:
                log.warning(f"[{sc.name}] 조회 오류: {e}")
            time.sleep(poll / len(scalpers))  # 전체 사이클을 poll_interval 내에 완료


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tick_runner.py <strategy_id>")
        sys.exit(1)
    try:
        run(sys.argv[1])
    except KeyboardInterrupt:
        log.info("수동 종료")
    except Exception as e:
        log.error(f"치명적 오류:\n{traceback.format_exc()}")
        notify_error(f"[틱스캘핑] 오류 종료: {e}")
