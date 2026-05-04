"""전략 실행 엔진"""

import json
import os
import sys
import time
import logging
import traceback
from datetime import datetime
from glob import glob

from kis_api import KISApi
from engine.screener import screen
from engine.monitor import add_position, check_and_exit, load_positions, remove_position, record_trade, load_daily_pnl
from engine.notifier import notify_buy, notify_no_signal, notify_error, notify_sell, send

LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "trading.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── 전략 로드 ──────────────────────────────────────────────────────────────

def load_strategy(strategy_id: str) -> dict:
    path = os.path.join(os.path.dirname(__file__), "..", "strategies", f"{strategy_id}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_all_enabled_strategies() -> list[dict]:
    pattern = os.path.join(os.path.dirname(__file__), "..", "strategies", "*.json")
    strategies = []
    for path in glob(pattern):
        with open(path, "r", encoding="utf-8") as f:
            s = json.load(f)
        if s.get("enabled", False):
            strategies.append(s)
    return strategies


# ── 매수 실행 ──────────────────────────────────────────────────────────────

def execute_entry(api: KISApi, strategy: dict):
    """스크리닝 → 종목별 매수 (실패 시 최대 3회 재시도)"""
    selected = screen(api, strategy)
    if not selected:
        log.info("매수 대상 종목 없음")
        notify_no_signal()
        return

    amount = strategy["entry"]["amount_per_stock"]
    for stock in selected:
        code  = stock["code"]
        name  = stock["name"]
        price = stock["price"]
        qty   = max(1, amount // price)

        try:
            cash = api.get_cash()
            if cash < qty * price:
                msg = f"[{name}] 잔고 부족 (필요 {qty*price:,}원 / 보유 {cash:,}원)"
                log.warning(msg)
                notify_error(msg)
                continue

            last_err = None
            for attempt in range(3):
                try:
                    result = api.buy(code, qty, price=0)
                    log.info(f"[매수] {name}({code}) {qty}주 시장가  주문번호={result.get('odno')}")
                    notify_buy(name, code, qty, price)
                    add_position(code, name, price, qty, strategy["id"])
                    break
                except Exception as e:
                    last_err = e
                    log.warning(f"[매수 재시도 {attempt+1}/3] {name}({code}): {e}")
                    if attempt < 2:
                        time.sleep(1.0 * (attempt + 1))
            else:
                log.error(f"[매수 오류] {name}({code}): {last_err}")
                notify_error(f"매수 실패 {name}({code}): {last_err}")
            time.sleep(0.3)
        except Exception as e:
            log.error(f"[매수 오류] {name}({code}): {e}")
            notify_error(f"매수 실패 {name}({code}): {e}")


# ── 15:20 강제매도 ─────────────────────────────────────────────────────────

def force_sell_all(api: KISApi):
    """미청산 포지션 전량 시장가 강제매도"""
    positions = load_positions()
    if not positions:
        log.info("15:20 — 미청산 포지션 없음")
        return

    log.info("=== 15:20 장마감 강제매도 시작 ===")
    for pos_key, pos in list(positions.items()):
        code = pos.get("code") or pos_key.split("_")[0]
        try:
            price_info = api.get_price(code)
            current    = int(price_info["stck_prpr"])
            api.sell(code, pos["qty"])
            notify_sell(pos["name"], code, pos["qty"], pos["buy_price"], current, "장마감 강제매도")
            record_trade(pos["name"], code, pos["qty"], pos["buy_price"], current, "장마감 강제매도", pos.get("strategy_id", ""))
            remove_position(pos_key)
            log.info(f"[장마감 강제매도] {pos['name']}({code}) [{pos['strategy_id']}] 완료")
        except Exception as e:
            log.error(f"[장마감 강제매도 오류] {pos_key}: {e}")
            notify_error(f"장마감 강제매도 실패 {pos['name']}({code}): {e}")


# ── 15:30 일일 결산 ────────────────────────────────────────────────────────

def send_daily_summary(api: KISApi, today):
    """당일 손익 결산 텔레그램 발송"""
    try:
        pnl_data  = load_daily_pnl()
        bal       = api.get_balance()
        s         = bal["summary"][0] if isinstance(bal["summary"], list) else bal["summary"]
        cash      = int(s.get("dnca_tot_amt", 0))
        total     = int(s.get("tot_evlu_amt", 0))
        trades    = pnl_data.get("trades", [])
        total_pnl = pnl_data.get("total_pnl", 0)

        total_cost = pnl_data.get("total_cost", 0)
        lines = [f"📊 <b>일일 결산 ({today})</b>\n"]

        if trades:
            # 전략별 그룹핑
            from collections import defaultdict
            strategy_groups = defaultdict(list)
            for tr in trades:
                strategy_groups[tr.get("strategy_id", "기타")].append(tr)

            for sid, group in strategy_groups.items():
                group_pnl = sum(tr["pnl"] for tr in group)
                sign = "✅" if group_pnl >= 0 else "🔴"
                lines.append(f"{sign} <b>[{sid}]</b>  소계: {group_pnl:+,}원")
                for tr in group:
                    t_sign = "↑" if tr["pnl"] >= 0 else "↓"
                    gross  = tr.get("gross_pnl", tr["pnl"])
                    cost   = tr.get("cost", 0)
                    lines.append(
                        f"  {t_sign} {tr['name']} [{tr['reason']}]  "
                        f"{gross:+,}원 / 비용 -{cost:,}원 / 순 {tr['pnl']:+,}원"
                        f"  ({tr['buy_price']:,}→{tr['sell_price']:,})"
                    )
        else:
            lines.append("거래 없음")

        pnl_sign = "+" if total_pnl >= 0 else ""
        lines.append(f"\n💰 당일 순실현손익: {pnl_sign}{total_pnl:,}원")
        lines.append(f"📋 당일 부대비용  : -{total_cost:,}원 (수수료+세금)")
        lines.append(f"💼 총평가금액     : {total:,}원")
        lines.append(f"🏦 예수금         : {cash:,}원")

        send("\n".join(lines))
        log.info("일일 결산 요약 전송 완료")
        return True
    except Exception as e:
        log.error(f"일일 결산 요약 실패: {e}")
        notify_error(f"일일 결산 요약 실패: {e}")
        return False


# ── 메인 루프 ──────────────────────────────────────────────────────────────

def run(strategy_id: str = None):
    api = KISApi()

    strategies = [load_strategy(strategy_id)] if strategy_id else load_all_enabled_strategies()

    if not strategies:
        msg = "실행할 전략이 없습니다."
        log.error(msg)
        send(f"⚠️ {msg}")
        return

    strategy_names = "\n".join(f"  • {s['name']} ({s['schedule']['entry_time']})" for s in strategies)
    log.info(f"자동매매 시작 | 전략 {len(strategies)}개: {[s['id'] for s in strategies]}")
    send(
        f"🚀 <b>자동매매 시작</b>  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"활성 전략 {len(strategies)}개:\n{strategy_names}"
    )

    now = datetime.now()
    if not api.is_trading_day(now):
        msg = f"📵 오늘({now.date()})은 비거래일입니다. 자동매매를 종료합니다."
        log.info(msg)
        send(msg)
        return

    entry_done       = set()
    force_sold_day   = None
    summary_sent_day = None
    interval         = strategies[0]["schedule"]["monitor_interval_sec"]

    while True:
        now   = datetime.now()
        today = now.date()
        t     = now.hour * 100 + now.minute

        # 자정 초기화
        if t < 900 and force_sold_day != today:
            entry_done.clear()

        # 15:20 강제매도
        if t >= 1520 and force_sold_day != today:
            force_sell_all(api)
            force_sold_day = today
            time.sleep(60)
            continue

        # 15:30 일일 결산
        if t >= 1530 and summary_sent_day != today:
            if send_daily_summary(api, today):
                summary_sent_day = today

        # 장 마감 후 대기
        if t >= 1520:
            time.sleep(60)
            continue

        # 전략별 매수
        for strategy in strategies:
            if strategy["id"] in entry_done:
                continue
            h, m    = map(int, strategy["schedule"]["entry_time"].split(":"))
            entry_t = h * 100 + m
            if entry_t <= t < entry_t + 10:
                log.info(f"=== 전략 [{strategy['name']}] 매수 실행 ===")
                execute_entry(api, strategy)
                entry_done.add(strategy["id"])

        # 포지션 모니터링
        if 910 <= t < 1520:
            check_and_exit(api, strategies)

        time.sleep(interval)


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        run(sid)
    except KeyboardInterrupt:
        log.info("자동매매 수동 종료")
        send(f"🛑 자동매매 수동 종료  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    except Exception as e:
        log.error(f"치명적 오류로 종료:\n{traceback.format_exc()}")
        send(f"🆘 <b>자동매매 오류 종료</b>  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{e}")
