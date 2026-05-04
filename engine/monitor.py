"""
보유 포지션 모니터링 및 자동 매도
+2% 익절 / -2% 손절
"""

# 거래 부대비용 (한국투자증권 KOSPI 기준)
BUY_FEE_RATE  = 0.00015   # 매수 수수료 0.015%
SELL_FEE_RATE = 0.00015   # 매도 수수료 0.015%
SELL_TAX_RATE = 0.0015    # 매도 세금  0.15% (농어촌특별세)

import json
import os
import time
import logging
from datetime import date
from kis_api import KISApi
from engine.notifier import notify_sell

log = logging.getLogger(__name__)
POSITION_FILE = os.path.join(os.path.dirname(__file__), "..", "positions.json")
PNL_FILE      = os.path.join(os.path.dirname(__file__), "..", "daily_pnl.json")


# ── 포지션 파일 관리 ───────────────────────────────────────────────────────

def load_positions() -> dict:
    try:
        with open(POSITION_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_positions(positions: dict):
    with open(POSITION_FILE, "w") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


def add_position(code: str, name: str, buy_price: int, qty: int, strategy_id: str):
    positions = load_positions()
    pos_key = f"{code}_{strategy_id}"
    positions[pos_key] = {
        "code": code,
        "name": name,
        "buy_price": buy_price,
        "qty": qty,
        "strategy_id": strategy_id,
    }
    save_positions(positions)
    log.info(f"[포지션 등록] {name}({code}) {qty}주 @ {buy_price:,}원 [{strategy_id}]")


def remove_position(pos_key: str):
    positions = load_positions()
    positions.pop(pos_key, None)
    save_positions(positions)


# ── 일별 손익 기록 ─────────────────────────────────────────────────────────

def record_trade(name: str, code: str, qty: int, buy_price: int, sell_price: int, reason: str, strategy_id: str = ""):
    """매도 완료 시 손익을 daily_pnl.json에 누적 (수수료·세금 차감 후 순손익)"""
    today = str(date.today())
    gross_pnl = (sell_price - buy_price) * qty
    buy_fee   = round(buy_price  * qty * BUY_FEE_RATE)
    sell_fee  = round(sell_price * qty * SELL_FEE_RATE)
    sell_tax  = round(sell_price * qty * SELL_TAX_RATE)
    total_cost = buy_fee + sell_fee + sell_tax
    net_pnl    = gross_pnl - total_cost

    try:
        with open(PNL_FILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    if data.get("date") != today:
        data = {"date": today, "total_pnl": 0, "total_cost": 0, "trades": []}

    data["total_pnl"]  += net_pnl
    data["total_cost"] = data.get("total_cost", 0) + total_cost
    data["trades"].append({
        "name": name, "code": code, "qty": qty,
        "buy_price": buy_price, "sell_price": sell_price,
        "gross_pnl": gross_pnl, "cost": total_cost, "pnl": net_pnl,
        "reason": reason, "strategy_id": strategy_id,
    })

    with open(PNL_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_daily_pnl() -> dict:
    today = str(date.today())
    try:
        with open(PNL_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") == today:
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"date": today, "total_pnl": 0, "trades": []}


# ── 모니터링 루프 ──────────────────────────────────────────────────────────

def check_and_exit(api: KISApi, strategies: list):
    """보유 포지션 전체 체크 후 조건 충족 시 매도"""
    positions = load_positions()
    if not positions:
        return

    strategy_map = {s["id"]: s for s in strategies}

    for pos_key, pos in list(positions.items()):
        code     = pos.get("code") or pos_key.split("_")[0]
        strategy = strategy_map.get(pos.get("strategy_id"))
        if not strategy:
            log.warning(f"[모니터] {pos_key} — 전략({pos.get('strategy_id')}) 없음, 스킵")
            continue
        exit_cfg    = strategy["exit"]
        take_profit = exit_cfg["take_profit"]
        stop_loss   = exit_cfg["stop_loss"]
        try:
            price_info = api.get_price(code)
            current    = int(price_info["stck_prpr"])
            buy_price  = pos["buy_price"]
            pnl_pct    = (current - buy_price) / buy_price * 100

            log.info(
                f"[모니터] {pos['name']}({code}) [{pos['strategy_id']}] "
                f"매입 {buy_price:,} → 현재 {current:,} ({pnl_pct:+.2f}%)"
            )

            sid = pos.get("strategy_id", "")
            if pnl_pct >= take_profit:
                log.info(f"[익절] {pos['name']}({code}) [{sid}] {pnl_pct:+.2f}% → 매도")
                api.sell(code, pos["qty"])
                notify_sell(pos["name"], code, pos["qty"], buy_price, current, "익절")
                record_trade(pos["name"], code, pos["qty"], buy_price, current, "익절", sid)
                remove_position(pos_key)

            elif pnl_pct <= stop_loss:
                log.info(f"[손절] {pos['name']}({code}) [{sid}] {pnl_pct:+.2f}% → 매도")
                api.sell(code, pos["qty"])
                notify_sell(pos["name"], code, pos["qty"], buy_price, current, "손절")
                record_trade(pos["name"], code, pos["qty"], buy_price, current, "손절", sid)
                remove_position(pos_key)

            time.sleep(0.1)

        except Exception as e:
            log.error(f"[모니터 오류] {pos_key}: {e}")
