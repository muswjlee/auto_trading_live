"""단일 전략 실행 프로세스 — launcher.py가 전략별로 별도 실행"""

import json
import os
import sys
import time
import logging
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kis_api import KISApi
from engine.screener import screen
from engine.monitor import (
    add_position, check_and_exit, load_positions,
    remove_position, record_trade, load_daily_pnl, load_prev_cash,
)
from engine.notifier import notify_buy, notify_no_signal, notify_error, notify_sell, send

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _setup_logger(strategy_id: str) -> logging.Logger:
    log_file = os.path.join(LOG_DIR, f"trading_{strategy_id}.log")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # 루트 로거에 핸들러 추가 → screener/monitor/kis_api 로그도 전략 파일에 기록됨
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


def load_strategy(strategy_id: str) -> dict:
    path = os.path.join(LOG_DIR, "strategies", f"{strategy_id}.json")  # LOG_DIR = auto_trading/
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 매수 실행 ──────────────────────────────────────────────────────────────

def execute_entry(api: KISApi, strategy: dict, log, suppress_no_signal: bool = False):
    selected = screen(api, strategy)
    if not selected:
        if suppress_no_signal:
            log.info("매수 대상 종목 없음 (연속진입 — 알림 생략)")
        else:
            log.info("매수 대상 종목 없음")
            notify_no_signal(strategy["name"])
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
            bought = False
            for attempt in range(3):
                try:
                    result = api.buy(code, qty, price=0)
                    log.info(f"[매수] {name}({code}) {qty}주  주문번호={result.get('odno')}")
                    notify_buy(name, code, qty, price, strategy["name"])
                    add_position(code, name, price, qty, strategy["id"],
                                 stock.get("execution_strength", 0.0),
                                 stock.get("change_rate", 0.0),
                                 stock.get("vwap_ratio", 0.0))
                    bought = True
                    break
                except Exception as e:
                    last_err = e
                    log.warning(f"[매수 재시도 {attempt+1}/3] {name}({code}): {e}")
                    if attempt < 2:
                        time.sleep(1.0 * (attempt + 1))
                        # 500 에러라도 실제 체결됐을 수 있으므로 잔고 확인
                        try:
                            bal = api.get_balance()
                            held = {s.get("pdno") for s in bal.get("stocks", [])}
                            if code in held:
                                log.info(f"[{name}] 500 에러지만 실제 체결 확인 → 재시도 취소, 포지션 등록")
                                notify_buy(name, code, qty, price, strategy["name"])
                                add_position(code, name, price, qty, strategy["id"],
                                             stock.get("execution_strength", 0.0),
                                             stock.get("change_rate", 0.0),
                                             stock.get("vwap_ratio", 0.0))
                                bought = True
                                break
                        except Exception:
                            pass
            if not bought:
                log.error(f"[매수 오류] {name}({code}): {last_err}")
                notify_error(f"매수 실패 {name}({code}): {last_err}")
            time.sleep(0.3)
        except Exception as e:
            log.error(f"[매수 오류] {name}({code}): {e}")
            notify_error(f"매수 실패 {name}({code}): {e}")


# ── 강제매도 (자신의 전략 포지션만) ──────────────────────────────────────

def force_sell_strategy(api: KISApi, strategy: dict, log):
    positions = load_positions()
    sid = strategy["id"]
    my_pos = {k: v for k, v in positions.items() if v.get("strategy_id") == sid}

    if not my_pos:
        log.info(f"[{sid}] 15:20 — 미청산 포지션 없음")
        return

    log.info(f"[{sid}] === 15:20 장마감 강제매도 시작 ===")
    for pos_key, pos in list(my_pos.items()):
        code = pos.get("code") or pos_key.split("_")[0]
        try:
            price_info = api.get_price(code)
            current    = int(price_info["stck_prpr"])
            sold = False
            try:
                api.sell(code, pos["qty"])
                sold = True
            except Exception as e:
                err = str(e)
                if "잔고내역이 없습니다" in err:
                    log.warning(f"[장마감 강제매도] {pos['name']}({code}) 이미 매도됨 → 포지션만 제거")
                    sold = True
                else:
                    raise
            if sold:
                notify_sell(pos["name"], code, pos["qty"], pos["buy_price"], current, "장마감 강제매도", strategy["name"])
                try:
                    record_trade(pos["name"], code, pos["qty"], pos["buy_price"], current, "장마감 강제매도", sid,
                                 pos.get("execution_strength", 0.0),
                                 pos.get("change_rate", 0.0),
                                 pos.get("vwap_ratio", 0.0))
                except Exception as e:
                    log.error(f"[손익 기록 실패] {pos['name']}({code}): {e}")
                remove_position(pos_key)
                log.info(f"[장마감 강제매도] {pos['name']}({code}) 완료")
        except Exception as e:
            log.error(f"[장마감 강제매도 오류] {pos_key}: {e}")
            notify_error(f"장마감 강제매도 실패 {pos['name']}({code}): {e}")


# ── 전략별 미니 결산 ──────────────────────────────────────────────────────

def send_strategy_summary(strategy: dict, today: str, log):
    try:
        sid        = strategy["id"]
        pnl_data   = load_daily_pnl()
        trades     = [t for t in pnl_data.get("trades", []) if t.get("strategy_id") == sid]
        strat_pnl  = sum(t["pnl"] for t in trades)
        strat_cost = sum(t.get("cost", 0) for t in trades)

        lines = [f"📋 <b>[{strategy['name']}] 결산 ({today})</b>\n"]
        if trades:
            for tr in trades:
                sign  = "✅" if tr["pnl"] >= 0 else "🔴"
                gross = tr.get("gross_pnl", tr["pnl"])
                cost  = tr.get("cost", 0)
                lines.append(
                    f"{sign} {tr['name']} [{tr['reason']}]  "
                    f"{gross:+,}원 / 비용 -{cost:,}원 / 순 {tr['pnl']:+,}원"
                    f"  ({tr['buy_price']:,}→{tr['sell_price']:,})"
                )
        else:
            lines.append("거래 없음")

        pnl_sign = "+" if strat_pnl >= 0 else ""
        lines.append(f"\n💰 순손익: {pnl_sign}{strat_pnl:,}원  |  비용: -{strat_cost:,}원")

        send("\n".join(lines))
        log.info(f"[{sid}] 결산 전송 완료")
        return True
    except Exception as e:
        log.error(f"결산 전송 실패: {e}")
        return False


# ── 메인 루프 ──────────────────────────────────────────────────────────────

def _is_trading_day(d=None) -> bool:
    import holidays as _holidays
    from datetime import date as _date
    if d is None:
        d = _date.today()
    if hasattr(d, "date"):
        d = d.date()
    if d.weekday() >= 5:
        return False
    FIXED_HOLIDAYS = {(1, 1), (3, 1), (5, 1), (5, 5), (6, 6), (8, 15), (10, 3), (10, 9), (12, 25)}
    if (d.month, d.day) in FIXED_HOLIDAYS:
        return False
    try:
        kr = _holidays.KR(years=d.year)
        return d not in kr
    except Exception:
        return False


def run(strategy_id: str):
    strategy = load_strategy(strategy_id)
    log      = _setup_logger(strategy_id)
    sid      = strategy["id"]

    if not _is_trading_day():
        log.info(f"[{sid}] 오늘은 휴장일 — 실행 종료")
        from engine.notifier import send
        send(f"📵 [{sid}] 오늘은 휴장일입니다.")
        return

    h, m     = map(int, strategy["schedule"]["entry_time"].split(":"))
    entry_t  = h * 100 + m
    interval = strategy["schedule"]["monitor_interval_sec"]
    continuous = strategy["schedule"].get("continuous_entry", False)

    entry_end_t = 1500  # 기본값
    if continuous:
        h_end, m_end = map(int, strategy["schedule"].get("entry_end_time", "15:00").split(":"))
        entry_end_t  = h_end * 100 + m_end

    api = KISApi()

    # 전일 예수금 기반 종목당 매수금액 동적 설정 (10%)
    prev_cash = load_prev_cash()
    if prev_cash > 0:
        amount = int(prev_cash * 0.1)
        strategy["entry"]["amount_per_stock"] = amount
        log.info(f"[{sid}] 종목당 매수금액: {amount:,}원 (전일 예수금 {prev_cash:,}원의 10%)")
    else:
        log.info(f"[{sid}] 전일 잔고 없음 — 기본값 사용: {strategy['entry']['amount_per_stock']:,}원")

    log.info(f"[{sid}] 프로세스 시작 | 진입: {strategy['schedule']['entry_time']}"
             + (" (연속진입)" if continuous else ""))

    entry_done       = False
    force_sold_day   = None
    summary_sent_day = None

    while True:
        now   = datetime.now()
        today = now.date()
        t     = now.hour * 100 + now.minute

        # 자정 초기화
        if t < 855 and entry_done:
            entry_done = False

        # 15:20 강제매도
        if t >= 1520 and force_sold_day != today:
            force_sell_strategy(api, strategy, log)
            force_sold_day = today

        # 15:30 결산 후 종료
        if t >= 1530 and summary_sent_day != today:
            send_strategy_summary(strategy, str(today), log)
            summary_sent_day = today
            log.info(f"[{sid}] 오늘 거래 완료. 프로세스 종료.")
            return

        # 장 마감 후 대기
        if t >= 1520:
            time.sleep(30)
            continue

        if continuous:
            # 연속진입: 09:01 ~ entry_end_t 동안 포지션 없을 때마다 매수 시도
            if entry_t <= t < entry_end_t:
                my_pos = {k: v for k, v in load_positions().items()
                          if v.get("strategy_id") == sid}
                if not my_pos:
                    log.info(f"=== [{sid}] 매수 시도 (연속진입) ===")
                    execute_entry(api, strategy, log, suppress_no_signal=True)
        else:
            # 일반: 진입 시간대 1회 매수
            if not entry_done and entry_t <= t < entry_t + 10:
                log.info(f"=== [{sid}] 매수 실행 ===")
                execute_entry(api, strategy, log)
                entry_done = True

        # 모니터링 (자신의 포지션만 체크)
        if 900 <= t < 1520:
            check_and_exit(api, [strategy])

        time.sleep(interval)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python runner.py <strategy_id>")
        sys.exit(1)
    sid = sys.argv[1]
    try:
        run(sid)
    except KeyboardInterrupt:
        logging.getLogger(sid).info(f"[{sid}] 수동 종료")
    except Exception as e:
        logging.getLogger(sid).error(f"[{sid}] 치명적 오류:\n{traceback.format_exc()}")
        notify_error(f"[{sid}] 오류 종료: {e}")
