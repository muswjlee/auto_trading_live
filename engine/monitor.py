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
from filelock import FileLock
from kis_api import KISApi
from engine.notifier import notify_sell

log = logging.getLogger(__name__)
POSITION_FILE = os.path.join(os.path.dirname(__file__), "..", "positions.json")
PNL_FILE      = os.path.join(os.path.dirname(__file__), "..", "daily_pnl.json")
HISTORY_FILE  = os.path.join(os.path.dirname(__file__), "..", "strategy_history.json")

_POS_LOCK = FileLock(POSITION_FILE + ".lock")
_PNL_LOCK = FileLock(PNL_FILE + ".lock")


# ── 포지션 파일 관리 ───────────────────────────────────────────────────────

def load_positions() -> dict:
    with _POS_LOCK:
        try:
            with open(POSITION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


def add_position(code: str, name: str, buy_price: int, qty: int, strategy_id: str):
    pos_key = f"{code}_{strategy_id}"
    with _POS_LOCK:
        try:
            with open(POSITION_FILE, "r", encoding="utf-8") as f:
                positions = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            positions = {}
        positions[pos_key] = {
            "code": code, "name": name,
            "buy_price": buy_price, "qty": qty,
            "strategy_id": strategy_id,
        }
        with open(POSITION_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
    log.info(f"[포지션 등록] {name}({code}) {qty}주 @ {buy_price:,}원 [{strategy_id}]")


def remove_position(pos_key: str):
    with _POS_LOCK:
        try:
            with open(POSITION_FILE, "r", encoding="utf-8") as f:
                positions = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            positions = {}
        positions.pop(pos_key, None)
        with open(POSITION_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)


# ── 전략별 히스토리 ────────────────────────────────────────────────────────

def _append_strategy_history(data: dict):
    """일별 전략 손익 요약을 strategy_history.json에 기록"""
    from collections import defaultdict
    groups = defaultdict(lambda: {
        "pnl": 0, "cost": 0, "gross_pnl": 0,
        "invested": 0, "trades": 0, "wins": 0, "losses": 0,
    })
    for t in data.get("trades", []):
        sid = t.get("strategy_id") or "기타"
        g = groups[sid]
        g["pnl"]       += t["pnl"]
        g["cost"]       += t.get("cost", 0)
        g["gross_pnl"]  += t.get("gross_pnl", t["pnl"])
        g["invested"]   += t["buy_price"] * t["qty"]
        g["trades"]     += 1
        if t["pnl"] >= 0:
            g["wins"] += 1
        else:
            g["losses"] += 1

    entry = {
        "date":       data["date"],
        "total_pnl":  data["total_pnl"],
        "total_cost": data.get("total_cost", 0),
        "strategies": dict(groups),
    }

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []

    for i, h in enumerate(history):
        if h["date"] == data["date"]:
            history[i] = entry
            break
    else:
        history.append(entry)

    history.sort(key=lambda x: x["date"])
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ── 일별 손익 기록 ─────────────────────────────────────────────────────────

def record_trade(name: str, code: str, qty: int, buy_price: int, sell_price: int,
                 reason: str, strategy_id: str = ""):
    """매도 완료 시 손익을 daily_pnl.json에 누적 (수수료·세금 차감 후 순손익)"""
    today     = str(date.today())
    gross_pnl = (sell_price - buy_price) * qty
    buy_fee   = round(buy_price  * qty * BUY_FEE_RATE)
    sell_fee  = round(sell_price * qty * SELL_FEE_RATE)
    sell_tax  = round(sell_price * qty * SELL_TAX_RATE)
    total_cost = buy_fee + sell_fee + sell_tax
    net_pnl    = gross_pnl - total_cost

    with _PNL_LOCK:
        try:
            with open(PNL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except UnicodeDecodeError:
            try:
                with open(PNL_FILE, "r", encoding="cp949") as f:
                    data = json.load(f)
                with open(PNL_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                log.warning("daily_pnl.json cp949 → utf-8 변환 완료")
            except Exception:
                data = {}
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

        if data.get("date") != today:
            # 이전 날 데이터 아카이브
            if data.get("date") and data.get("trades"):
                archive_path = PNL_FILE.replace("daily_pnl.json", f"pnl_{data['date']}.json")
                with open(archive_path, "w", encoding="utf-8") as af:
                    json.dump(data, af, ensure_ascii=False, indent=2)
                _append_strategy_history(data)
            data = {"date": today, "total_pnl": 0, "total_cost": 0, "trades": []}

        data["total_pnl"]  += net_pnl
        data["total_cost"]  = data.get("total_cost", 0) + total_cost
        data["trades"].append({
            "name": name, "code": code, "qty": qty,
            "buy_price": buy_price, "sell_price": sell_price,
            "gross_pnl": gross_pnl, "cost": total_cost, "pnl": net_pnl,
            "reason": reason, "strategy_id": strategy_id,
        })

        with open(PNL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def is_code_held(code: str) -> bool:
    """해당 종목이 이미 어떤 전략에서든 보유 중인지 확인 (크로스 전략 중복 매수 방지)"""
    positions = load_positions()
    return any(v["code"] == code for v in positions.values())


def load_daily_pnl() -> dict:
    today = str(date.today())
    with _PNL_LOCK:
        data = None
        try:
            with open(PNL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except UnicodeDecodeError:
            try:
                with open(PNL_FILE, "r", encoding="cp949") as f:
                    data = json.load(f)
                with open(PNL_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if data and data.get("date") == today:
            return data
    return {"date": today, "total_pnl": 0, "total_cost": 0, "trades": []}


# ── 모니터링 루프 ──────────────────────────────────────────────────────────

def _sell_with_verify(api: KISApi, pos: dict, pos_key: str,
                      current: int, reason: str, sid: str) -> bool:
    """매도 실행 — 500에러 시 잔고 확인으로 체결 여부 판단 후 position 정리"""
    code      = pos.get("code") or pos_key.split("_")[0]
    name      = pos["name"]
    buy_price = pos["buy_price"]
    qty       = pos["qty"]

    sold = False
    try:
        api.sell(code, qty)
        sold = True
    except Exception as e:
        err = str(e)
        if "잔고내역이 없습니다" in err:
            # 이미 매도된 포지션 — 기록 없이 포지션만 제거
            log.warning(f"[{name}({code})] 잔고 없음 → 이미 매도된 포지션, 기록 없이 제거")
            remove_position(pos_key)
            return False
        log.warning(f"[매도 500에러] {name}({code}) [{sid}]: {e}")
        try:
            bal  = api.get_balance()
            held = {s.get("pdno") for s in bal.get("stocks", [])}
            if code not in held:
                log.info(f"[{name}] 500에러지만 잔고에 없음 → 체결 확인")
                sold = True
            else:
                log.error(f"[매도 실패] {name}({code}) [{sid}]: 잔고 확인 결과 여전히 보유 중 — 다음 주기 재시도")
        except Exception as e2:
            log.error(f"[매도 실패] {name}({code}): 잔고 확인 오류 {e2}")

    if sold:
        notify_sell(name, code, qty, buy_price, current, reason, sid)
        try:
            record_trade(name, code, qty, buy_price, current, reason, sid)
        except Exception as e:
            log.error(f"[손익 기록 실패] {name}({code}): {e}")
        remove_position(pos_key)

    return sold


def check_and_exit(api: KISApi, strategies: list):
    """보유 포지션 중 자신의 전략 포지션만 체크 후 조건 충족 시 매도"""
    positions = load_positions()
    if not positions:
        return

    strategy_map = {s["id"]: s for s in strategies}

    for pos_key, pos in list(positions.items()):
        code     = pos.get("code") or pos_key.split("_")[0]
        strategy = strategy_map.get(pos.get("strategy_id"))
        if not strategy:
            continue  # 다른 전략 프로세스의 포지션 — 조용히 스킵
        exit_cfg    = strategy["exit"]
        take_profit = exit_cfg["take_profit"]
        stop_loss   = exit_cfg["stop_loss"]
        try:
            price_info = api.get_price(code)
            current    = int(price_info["stck_prpr"])
            buy_price  = pos["buy_price"]
            pnl_pct    = (current - buy_price) / buy_price * 100
            sid        = pos.get("strategy_id", "")

            log.info(
                f"[모니터] {pos['name']}({code}) [{sid}] "
                f"매입 {buy_price:,} → 현재 {current:,} ({pnl_pct:+.2f}%)"
            )

            if pnl_pct >= take_profit:
                log.info(f"[익절] {pos['name']}({code}) [{sid}] {pnl_pct:+.2f}% → 매도")
                _sell_with_verify(api, pos, pos_key, current, "익절", sid)

            elif pnl_pct <= stop_loss:
                log.info(f"[손절] {pos['name']}({code}) [{sid}] {pnl_pct:+.2f}% → 매도")
                _sell_with_verify(api, pos, pos_key, current, "손절", sid)

            time.sleep(0.1)

        except Exception as e:
            log.error(f"[모니터 오류] {pos_key}: {e}")
