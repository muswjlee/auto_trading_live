"""
보유 포지션 모니터링 및 자동 매도
+2% 익절 / -2% 손절
"""

# 거래 부대비용 (KOSPI/KOSDAQ 공통)
BUY_FEE_RATE  = 0.000018  # 매수 수수료 0.0018%
SELL_FEE_RATE = 0.000018  # 매도 수수료 0.0018%
SELL_TAX_RATE = 0.002     # 매도 세금  0.2%

import json
import os
import time
import logging
from datetime import date
from filelock import FileLock
from kis_api import KISApi
from engine.notifier import notify_sell

log = logging.getLogger(__name__)
_MODE         = os.getenv("KIS_MODE", "live")
POSITION_FILE = os.path.join(os.path.dirname(__file__), "..", f"positions_{_MODE}.json")
PNL_FILE      = os.path.join(os.path.dirname(__file__), "..", f"daily_pnl_{_MODE}.json")
HISTORY_FILE  = os.path.join(os.path.dirname(__file__), "..", f"strategy_history_{_MODE}.json")
BALANCE_FILE  = os.path.join(os.path.dirname(__file__), "..", f"balance_history_{_MODE}.json")

SL_BLACKLIST_FILE = os.path.join(os.path.dirname(__file__), "..", f"sl_blacklist_{_MODE}.json")

_POS_LOCK = FileLock(POSITION_FILE + ".lock")
_PNL_LOCK = FileLock(PNL_FILE + ".lock")
_SL_LOCK  = FileLock(SL_BLACKLIST_FILE + ".lock")


# ── 손절 블랙리스트 ────────────────────────────────────────────────────────

def add_sl_blacklist(code: str, name: str):
    """당일 손절 종목을 블랙리스트에 추가 — 전략 간 공유"""
    today = str(date.today())
    with _SL_LOCK:
        try:
            with open(SL_BLACKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        if data.get("date") != today:
            data = {"date": today, "codes": {}}
        data["codes"][code] = name
        with open(SL_BLACKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"[손절 블랙리스트] {name}({code}) 당일 재매수 금지 등록")


def is_sl_blacklisted(code: str) -> bool:
    """당일 손절된 종목이면 True"""
    today = str(date.today())
    try:
        with open(SL_BLACKLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("date") == today and code in data.get("codes", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return False


# ── 포지션 파일 관리 ───────────────────────────────────────────────────────

def load_positions() -> dict:
    with _POS_LOCK:
        for _ in range(3):
            try:
                with open(POSITION_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except PermissionError:
                time.sleep(0.2)
            except (FileNotFoundError, json.JSONDecodeError):
                return {}
        return {}


def reserve_or_skip(code: str, strategy_id: str) -> bool:
    """다른 전략이 보유/예약 중이면 False, 아니면 즉시 예약 후 True 반환 (원자적)"""
    pos_key = f"{code}_{strategy_id}"
    with _POS_LOCK:
        try:
            with open(POSITION_FILE, "r", encoding="utf-8") as f:
                positions = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            positions = {}
        held = {v["code"] for v in positions.values()}
        if code in held:
            return False
        positions[pos_key] = {"code": code, "strategy_id": strategy_id, "_reserved": True}
        with open(POSITION_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
    return True


def cancel_reservation(code: str, strategy_id: str):
    """매수 실패 시 예약 항목 제거"""
    remove_position(f"{code}_{strategy_id}")


def add_position(code: str, name: str, buy_price: int, qty: int, strategy_id: str,
                 execution_strength: float = 0.0, change_rate: float = 0.0, vwap_ratio: float = 0.0):
    pos_key = f"{code}_{strategy_id}"
    with _POS_LOCK:
        try:
            with open(POSITION_FILE, "r", encoding="utf-8") as f:
                positions = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            positions = {}
        from datetime import datetime as _dt
        positions[pos_key] = {
            "code": code, "name": name,
            "buy_price": buy_price, "qty": qty,
            "strategy_id": strategy_id,
            "execution_strength": execution_strength,
            "change_rate": change_rate,
            "vwap_ratio": vwap_ratio,
            "bought_at": _dt.now().strftime("%H:%M:%S"),
        }
        with open(POSITION_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False, indent=2)
    log.info(f"[포지션 등록] {name}({code}) {qty}주 @ {buy_price:,}원 [{strategy_id}] 등락률={change_rate}% 체결강도={execution_strength} VWAP비율={vwap_ratio}")


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


# ── 일별 잔고 기록 ─────────────────────────────────────────────────────────

def record_daily_balance(cash: int, total: int, asset_change: int, buy_amt: int, sell_amt: int):
    """장마감 후 계좌 잔고 정보를 balance_history.json에 기록"""
    today = str(date.today())
    entry = {
        "date":           today,
        "dnca_tot_amt":   cash,
        "tot_evlu_amt":   total,
        "asst_icdc_amt":  asset_change,
        "thdt_buy_amt":   buy_amt,
        "thdt_sll_amt":   sell_amt,
    }
    try:
        with open(BALANCE_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []

    for i, h in enumerate(history):
        if h["date"] == today:
            history[i] = entry
            break
    else:
        history.append(entry)

    history.sort(key=lambda x: x["date"])
    with open(BALANCE_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    log.info(f"잔고 기록 완료: 예수금={cash:,} 총평가={total:,} 자산증감={asset_change:+,}")


def load_prev_cash() -> int:
    """전일 예수금 반환 — balance_history.json의 오늘 이전 가장 최근 기록"""
    today = str(date.today())
    try:
        with open(BALANCE_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
        prev = [h for h in history if h["date"] < today]
        if prev:
            return int(prev[-1]["dnca_tot_amt"])
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return 0


# ── 일별 손익 기록 ─────────────────────────────────────────────────────────

def record_trade(name: str, code: str, qty: int, buy_price: int, sell_price: int,
                 reason: str, strategy_id: str = "", execution_strength: float = 0.0,
                 change_rate: float = 0.0, vwap_ratio: float = 0.0):
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
                archive_path = os.path.join(os.path.dirname(PNL_FILE), f"pnl_{data['date']}.json")
                with open(archive_path, "w", encoding="utf-8") as af:
                    json.dump(data, af, ensure_ascii=False, indent=2)
                _append_strategy_history(data)
            data = {"date": today, "total_pnl": 0, "total_cost": 0, "trades": []}

        data["total_pnl"]  += net_pnl
        data["total_cost"]  = data.get("total_cost", 0) + total_cost
        from datetime import datetime as _dt
        # 포지션에서 매수 시각 가져오기
        try:
            with open(POSITION_FILE, "r", encoding="utf-8") as pf:
                pos_data = json.load(pf)
            pos_key = f"{code}_{strategy_id}"
            bought_at = pos_data.get(pos_key, {}).get("bought_at", "")
        except Exception:
            bought_at = ""
        data["trades"].append({
            "name": name, "code": code, "qty": qty,
            "buy_price": buy_price, "sell_price": sell_price,
            "gross_pnl": gross_pnl, "cost": total_cost, "pnl": net_pnl,
            "reason": reason, "strategy_id": strategy_id,
            "execution_strength": execution_strength,
            "change_rate": change_rate,
            "vwap_ratio": vwap_ratio,
            "bought_at": bought_at,
            "sold_at": _dt.now().strftime("%H:%M:%S"),
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

def _cancel_pending_sells(api: KISApi, code: str, name: str):
    """해당 종목의 미체결 주문 전체 취소 (수량 초과 오류 해소용)"""
    try:
        pending = api.get_pending_orders()
        for order in pending:
            if order.get("pdno") != code:
                continue
            order_no = order.get("odno", "")
            qty = int(order.get("ord_qty", 0))
            if order_no and qty:
                try:
                    api.cancel_order(order_no, code, qty)
                    log.info(f"[미체결 취소] {name}({code}) 주문번호={order_no} {qty}주")
                    time.sleep(0.3)
                except Exception as ce:
                    log.warning(f"[미체결 취소 오류] {name}({code}) 주문번호={order_no}: {ce}")
    except Exception as e:
        log.warning(f"[미체결 조회 오류] {name}({code}): {e}")


def _sell_with_verify(api: KISApi, pos: dict, pos_key: str,
                      current: int, reason: str, sid: str) -> bool:
    """매도 실행 — 500에러 시 잔고 확인으로 체결 여부 판단 후 position 정리"""
    code      = pos.get("code") or pos_key.split("_")[0]
    name      = pos["name"]
    buy_price = pos["buy_price"]
    qty       = pos["qty"]

    sold     = False
    order_no = ""
    try:
        result   = api.sell(code, qty)
        order_no = result.get("ODNO") or result.get("odno", "")
        sold     = True
    except Exception as e:
        err = str(e)
        if "주문 가능한 수량을 초과" in err or "주문가능수량을 초과" in err:
            log.warning(f"[매도 수량 초과] {name}({code}) [{sid}] — 미체결 주문 취소 후 재시도")
            _cancel_pending_sells(api, code, name)
            time.sleep(1)
            try:
                result   = api.sell(code, qty)
                order_no = result.get("ODNO") or result.get("odno", "")
                sold     = True
            except Exception as e2:
                err = str(e2)
                log.warning(f"[매도 재시도 오류] {name}({code}) [{sid}]: {e2}")
        if not sold and "잔고내역이 없습니다" in err:
            # 매수 직후 VTS 잔고 미반영일 수 있으므로 재시도
            for retry in range(3):
                time.sleep(5)
                log.info(f"[{name}({code})] 잔고 없음 재시도 {retry+1}/3")
                try:
                    result   = api.sell(code, qty)
                    order_no = result.get("ODNO") or result.get("odno", "")
                    sold     = True
                    break
                except Exception as e2:
                    if "잔고내역이 없습니다" not in str(e2):
                        log.warning(f"[매도 재시도 오류] {name}({code}): {e2}")
                        break
            else:
                # 3회 재시도 후에도 잔고 없음 → 다른 프로세스가 이미 매도
                log.warning(f"[{name}({code})] 잔고 없음 (3회 재시도) → 이미 매도된 포지션, 기록 없이 제거")
                remove_position(pos_key)
                return False
        if not sold:
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
        # 실제 매도 체결가 조회 (주문번호 기반)
        actual_sell_price = current
        if order_no:
            time.sleep(0.5)
            fetched = api.get_sell_execution_price(order_no, code)
            if fetched > 0:
                log.info(f"[매도 체결가] {name}({code}) 실제 체결가 {fetched:,}원 (트리거 시점 {current:,}원)")
                actual_sell_price = fetched
            else:
                log.warning(f"[매도 체결가 조회 실패] {name}({code}) 트리거 시점가 {current:,}원 사용")
        try:
            notify_sell(name, code, qty, buy_price, actual_sell_price, reason, sid)
            try:
                record_trade(name, code, qty, buy_price, actual_sell_price, reason, sid,
                             pos.get("execution_strength", 0.0),
                             pos.get("change_rate", 0.0),
                             pos.get("vwap_ratio", 0.0))
            except Exception as e:
                log.error(f"[손익 기록 실패] {name}({code}): {e}")
            if reason == "손절":
                try:
                    add_sl_blacklist(code, name)
                except Exception as e:
                    log.error(f"[블랙리스트 등록 실패] {name}({code}): {e}")
        finally:
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
        exit_cfg     = strategy["exit"]
        take_profit  = exit_cfg["take_profit"]
        stop_loss    = exit_cfg["stop_loss"]
        min_hold_sec = exit_cfg.get("min_hold_sec", 0)
        try:
            price_info = api.get_price(code)
            current    = int(price_info["stck_prpr"])
            buy_price  = pos["buy_price"]
            pnl_pct    = (current - buy_price) / buy_price * 100
            sid        = pos.get("strategy_id", "")

            # 보유시간 계산
            hold_sec = 0
            bought_at_str = pos.get("bought_at", "")
            if bought_at_str:
                from datetime import datetime as _dt
                today = _dt.now().date()
                bought_dt = _dt.strptime(bought_at_str, "%H:%M:%S").replace(
                    year=today.year, month=today.month, day=today.day
                )
                hold_sec = (_dt.now() - bought_dt).total_seconds()

            log.info(
                f"[모니터] {pos['name']}({code}) [{sid}] "
                f"매입 {buy_price:,} → 현재 {current:,} ({pnl_pct:+.2f}%)"
                + (f" 보유 {hold_sec:.0f}초" if min_hold_sec > 0 else "")
            )

            if pnl_pct >= take_profit:
                log.info(f"[익절] {pos['name']}({code}) [{sid}] {pnl_pct:+.2f}% → 매도")
                _sell_with_verify(api, pos, pos_key, current, "익절", sid)

            elif pnl_pct <= stop_loss:
                if min_hold_sec > 0 and hold_sec < min_hold_sec:
                    log.info(f"[손절 유예] {pos['name']}({code}) [{sid}] {pnl_pct:+.2f}% — 보유 {hold_sec:.0f}초 < {min_hold_sec}초")
                else:
                    log.info(f"[손절] {pos['name']}({code}) [{sid}] {pnl_pct:+.2f}% → 매도")
                    _sell_with_verify(api, pos, pos_key, current, "손절", sid)

            time.sleep(0.1)

        except Exception as e:
            if "500" in str(e):
                log.warning(f"[모니터 일시 오류] {pos['name']}({code}) 현재가 조회 실패 (VTS 500) — 다음 주기 재시도")
            else:
                log.error(f"[모니터 오류] {pos_key}: {e}")
