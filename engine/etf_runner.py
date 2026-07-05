"""ETF 모멘텀 전략 — 매주 월요일 09:01 실행

흐름:
  - etf_momentum_signal_{MODE}.json 로드 (토요일 스크리닝 결과)
  - 현재 포지션 확인
  - action=buy  : 선정 ETF 매수 (기존 ETF 다르면 매도 후 매수)
  - action=cash : 보유 ETF 전량 매도
  - action=hold : 유지 (변동 없음)
  - 15:30 결산 알림

사용: python engine/etf_runner.py
워치독 또는 Task Scheduler(월요일)에서 기동
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
    add_position, load_positions, remove_position,
    record_trade, load_daily_pnl,
)
from engine.notifier import notify_buy, notify_sell, notify_error, send
from engine.order_utils import limit_buy

_BASE    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE    = os.getenv("KIS_MODE", "live")
_SID     = "etf_momentum"

log = logging.getLogger(_SID)


def _setup_logger():
    log_file = os.path.join(_BASE, f"trading_{_SID}_{_MODE}.log")
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


def _load_strategy() -> dict:
    path = os.path.join(_BASE, "strategies", "etf_momentum.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_signal() -> dict | None:
    path = os.path.join(_BASE, f"etf_momentum_signal_{_MODE}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("시그널 파일 없음 — 실행 불가")
        return None


def _save_signal(signal: dict):
    path = os.path.join(_BASE, f"etf_momentum_signal_{_MODE}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(signal, f, ensure_ascii=False, indent=2)


def _get_my_position() -> dict | None:
    """현재 ETF 포지션 반환 (없으면 None)"""
    positions = load_positions()
    for pos in positions.values():
        if pos.get("strategy_id") == _SID and not pos.get("_reserved"):
            return pos
    return None


def _sell_etf(api: KISApi, code: str, name: str, qty: int, buy_price: int, reason: str):
    """ETF 전량 매도 (bid_price_1 지정가 → 미체결 시 정정 → 시장가 fallback)"""
    log.info(f"[매도] {name}({code}) {qty}주 사유={reason}")
    try:
        # bid_price_1 지정가
        bid1 = int(api.get_price(code).get("stck_prpr", buy_price))
        try:
            ob = api.get_orderbook(code)
            b  = int(ob.get("bidp1", 0))
            if b > 0:
                bid1 = b
        except Exception:
            pass

        result   = api.sell(code, qty, price=bid1)
        order_no = result.get("odno", "")
        log.info(f"  주문번호={order_no} @ {bid1:,}원 (지정가)")

        # 미체결 확인 (30초)
        time.sleep(30)
        try:
            pending = api.get_pending_orders()
            if any(p.get("odno") == order_no for p in pending):
                ob2  = api.get_orderbook(code)
                bid2 = int(ob2.get("bidp1", bid1))
                log.info(f"  미체결 → 정정 {bid1:,}→{bid2:,}원")
                try:
                    r2       = api.modify_order(order_no, code, qty, bid2)
                    order_no = r2.get("odno", order_no)
                except Exception:
                    pass
                time.sleep(30)
                pending2 = api.get_pending_orders()
                if any(p.get("odno") == order_no for p in pending2):
                    log.info(f"  재정정 후도 미체결 → 시장가 강제 매도")
                    try:
                        api.cancel_order(order_no, code, qty)
                        time.sleep(0.5)
                    except Exception:
                        pass
                    r3       = api.sell(code, qty, price=0)
                    order_no = r3.get("odno", "")
                    time.sleep(3.0)
        except Exception as pe:
            log.warning(f"  미체결 확인 오류: {pe}")

        # 체결가 조회
        sell_price = 0
        for _ in range(5):
            time.sleep(1.0)
            sell_price = api.get_sell_execution_price(order_no, code)
            if sell_price > 0:
                break
        if sell_price <= 0:
            sell_price = int(api.get_price(code).get("stck_prpr", buy_price))
            log.warning(f"  체결가 조회 실패 → 현재가 {sell_price:,}원 사용")

        gross = (sell_price - buy_price) * qty
        cost  = round(buy_price * qty * 0.000018 + sell_price * qty * (0.000018 + 0.002))
        pnl   = gross - cost

        record_trade(name, code, qty, buy_price, sell_price, reason, _SID)
        remove_position(f"{code}_{_SID}")
        notify_sell(name, code, qty, buy_price, sell_price, reason, _SID)
        log.info(f"  매도 완료: {buy_price:,}→{sell_price:,} | 순손익 {pnl:+,}원")
        return True
    except Exception as e:
        log.error(f"  매도 오류: {e}")
        notify_error(f"ETF 매도 실패 {name}({code}): {e}")
        return False


def _buy_etf(api: KISApi, code: str, name: str, amount: int, order_type: str = "limit") -> bool:
    """ETF 매수 — order_type='market'이면 시장가, 그 외 ask1 지정가. 성공 시 True 반환"""
    try:
        price_info = api.get_price(code)
        current    = int(price_info.get("stck_prpr", 0))
        if current <= 0:
            log.error(f"  {name}({code}) 현재가 조회 실패")
            return False

        qty = max(1, amount // current)
        cash = api.get_cash()
        if cash < qty * current:
            msg = f"[{name}] 잔고 부족 (필요 {qty*current:,}원 / 보유 {cash:,}원)"
            log.warning(msg)
            notify_error(msg)
            return False

        log.info(f"[매수] {name}({code}) {qty}주 ≈ {qty*current:,}원 ({order_type})")

        if order_type == "market":
            result   = api.buy(code, qty, price=0)
            order_no = result.get("odno", "") or result.get("ODNO", "")
            time.sleep(3.0)
            actual_price = 0
            if order_no:
                for _ in range(5):
                    actual_price = api.get_buy_execution_price(order_no, code)
                    if actual_price > 0:
                        break
                    time.sleep(1.0)
            if actual_price <= 0:
                for _ in range(3):
                    actual_price = api.get_avg_buy_price(code)
                    if actual_price > 0:
                        break
                    time.sleep(1.0)
            if actual_price <= 0:
                actual_price = current
        else:
            actual_price = limit_buy(api, code, name, qty, wait_sec=30)
            if actual_price <= 0:
                log.error(f"  {name}({code}) 지정가 매수 미체결 → 포기")
                notify_error(f"ETF 매수 취소(미체결) {name}({code})")
                return False

        add_position(code, name, actual_price, qty, _SID)
        notify_buy(name, code, qty, actual_price, "ETF 모멘텀 전략")
        log.info(f"  매수 완료: {actual_price:,}원 × {qty}주")
        return True

    except Exception as e:
        log.error(f"  매수 오류: {e}")
        notify_error(f"ETF 매수 실패 {name}({code}): {e}")
        return False


def _send_summary(api: KISApi, today: str):
    try:
        pnl_data = load_daily_pnl()
        trades   = [t for t in pnl_data.get("trades", []) if t.get("strategy_id") == _SID]
        total    = sum(t["pnl"] for t in trades)

        lines = [f"[ETF 모멘텀] 결산 ({today})\n"]
        if trades:
            for t in trades:
                sign = "+" if t["pnl"] >= 0 else "-"
                lines.append(
                    f"{sign} {t['name']} [{t['reason']}]  "
                    f"{t.get('gross_pnl', t['pnl']):+,}원 / 비용 -{t.get('cost',0):,}원 / 순 {t['pnl']:+,}원"
                )
        else:
            lines.append("당일 거래 없음")

        pos = _get_my_position()
        if pos:
            cur_price = 0
            try:
                cur_price = int(api.get_price(pos["code"]).get("stck_prpr", 0))
            except Exception:
                pass
            unr = (cur_price - pos["buy_price"]) * pos["qty"] if cur_price > 0 else 0
            lines.append(
                f"\n보유: {pos['name']} {pos['qty']}주 @ {pos['buy_price']:,}원"
                + (f"  미실현 {unr:+,}원" if cur_price > 0 else "")
            )

        lines.append(f"\n당일 순손익: {total:+,}원")
        send("\n".join(lines))
    except Exception as e:
        log.error(f"결산 전송 실패: {e}")


def run():
    _setup_logger()
    strategy = _load_strategy()

    if not strategy.get("enabled", True):
        log.info(f"[{_SID}] 비활성화 상태 — 종료")
        return

    entry_time = strategy["schedule"].get("entry_time", "09:01")
    eh, em     = map(int, entry_time.split(":"))
    entry_t    = eh * 100 + em

    signal = _load_signal()
    if signal is None:
        send(f"⚠️ [{_SID}] 시그널 파일 없음 — 실행 불가")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    log.info(f"[{_SID}] 시작 | 진입시각={entry_time} | action={signal['action']}")

    # 이미 실행된 시그널이면 매매 스킵 (워치독 재시작 시 중복 매수 방지)
    already_executed = signal.get("executed", False)
    if already_executed:
        log.info(f"[{_SID}] 시그널 이미 실행됨 ({signal.get('executed_at','')}) — 매매 생략, 모니터링만 진행")

    api = KISApi()

    trade_done     = already_executed  # 이미 실행됐으면 재진입 차단
    summary_sent   = False
    order_type     = strategy["entry"].get("order_type", "limit")

    while True:
        now = datetime.now()
        t   = now.hour * 100 + now.minute

        # 진입 실행 (1회)
        if not trade_done and t >= entry_t:
            action       = signal.get("action", "cash")
            selected_etf = signal.get("selected_etf")
            current_pos  = _get_my_position()

            log.info(f"=== [{_SID}] 매매 실행 action={action} ===")

            trade_ok = False

            if action == "buy" and selected_etf:
                new_code = selected_etf["code"]
                new_name = selected_etf["name"]

                if current_pos:
                    cur_code = current_pos.get("code") or current_pos.get("pos_key","").split("_")[0]
                    if cur_code == new_code:
                        log.info(f"  보유 유지: {new_name}({new_code}) — 동일 ETF")
                        send(f"🔄 [ETF 모멘텀] {new_name} 보유 유지 (변동 없음)")
                        trade_ok = True
                    else:
                        log.info(f"  교체: {current_pos['name']} → {new_name}")
                        sold = _sell_etf(
                            api,
                            cur_code,
                            current_pos["name"],
                            current_pos["qty"],
                            current_pos["buy_price"],
                            "ETF교체",
                        )
                        time.sleep(2.0)
                        if sold:
                            trade_ok = _buy_etf(api, new_code, new_name, strategy["entry"]["amount"], order_type)
                else:
                    log.info(f"  신규 매수: {new_name}({new_code})")
                    trade_ok = _buy_etf(api, new_code, new_name, strategy["entry"]["amount"], order_type)

            elif action == "cash":
                if current_pos:
                    cur_code = current_pos.get("code") or current_pos.get("pos_key","").split("_")[0]
                    log.info(f"  현금 전환: {current_pos['name']} 전량 매도")
                    trade_ok = _sell_etf(
                        api,
                        cur_code,
                        current_pos["name"],
                        current_pos["qty"],
                        current_pos["buy_price"],
                        "현금전환",
                    )
                else:
                    log.info("  현금 보유 (기존 포지션 없음)")
                    send("💵 [ETF 모멘텀] 조건 미충족 — 현금 보유 유지")
                    trade_ok = True

            # 매매 성공 시에만 executed 처리 (실패 시 다음 주기에 재시도 가능하도록)
            if trade_ok:
                signal["executed"] = True
                signal["executed_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                _save_signal(signal)
            else:
                log.warning(f"  매매 실패 — executed 미설정, 다음 실행 시 재시도 가능")
                notify_error(f"[ETF 모멘텀] 매매 실패 — executed 미설정")
            trade_done = True

        # 15:30 결산
        if t >= 1530 and not summary_sent:
            _send_summary(api, today_str)
            summary_sent = True
            log.info(f"[{_SID}] 결산 완료 — 종료")
            return

        time.sleep(10)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info(f"[{_SID}] 수동 종료")
    except Exception as e:
        log.error(f"[{_SID}] 오류:\n{traceback.format_exc()}")
        notify_error(f"[{_SID}] 오류 종료: {e}")
