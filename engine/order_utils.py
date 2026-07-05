"""지정가 주문 공통 유틸리티

limit_buy : ask_price_1 지정가 매수 → wait_sec 후 미체결 시 정정 1회 → 취소
            성공: 실제 체결가(int) / 실패·취소: 0
"""

import time
import logging

log = logging.getLogger(__name__)


def _is_pending(api, order_no: str) -> bool:
    if not order_no:
        return False
    try:
        return any(p.get("odno") == order_no for p in api.get_pending_orders())
    except Exception:
        return False


def _try_cancel(api, order_no: str, code: str, qty: int):
    try:
        api.cancel_order(order_no, code, qty)
    except Exception as e:
        log.warning(f"  주문 취소 오류 ({order_no}): {e}")


def _wait_buy_price(api, code: str) -> int:
    for _ in range(5):
        p = api.get_avg_buy_price(code)
        if p > 0:
            return p
        time.sleep(1.0)
    try:
        return int(api.get_price(code).get("stck_prpr", 0))
    except Exception:
        return 0


def limit_buy(api, code: str, name: str, qty: int, wait_sec: int = 30) -> int:
    """
    ask_price_1 지정가 매수
    - wait_sec 후 미체결 → 정정 1회 → 재대기 → 취소
    - orderbook 조회 실패 → 시장가 fallback
    - 반환: 실제 매입가 (0 = 체결 안 됨)
    """
    order_no = ""
    try:
        ob   = api.get_orderbook(code)
        ask1 = int(ob.get("askp1", 0))
        if ask1 <= 0:
            log.warning(f"[{name}] 매도호가1 조회 실패 → 시장가 fallback")
            result   = api.buy(code, qty, price=0)
            order_no = result.get("odno", "")
            return _wait_buy_price(api, code)

        log.info(f"[매수주문] {name}({code}) {qty}주 @ {ask1:,}원 (지정가)")
        result   = api.buy(code, qty, price=ask1)
        order_no = result.get("odno", "")
        log.info(f"  주문번호={order_no}")

        time.sleep(wait_sec)

        if not _is_pending(api, order_no):
            price = _wait_buy_price(api, code)
            log.info(f"  체결 완료: {price:,}원")
            return price

        # 미체결: 1회 정정
        ob2  = api.get_orderbook(code)
        ask2 = int(ob2.get("askp1", ask1))
        log.info(f"  미체결 → 정정 {ask1:,} → {ask2:,}원")
        try:
            r2       = api.modify_order(order_no, code, qty, ask2)
            order_no = r2.get("odno", order_no)
        except Exception as e:
            log.warning(f"  정정 실패: {e} → 취소")
            _try_cancel(api, order_no, code, qty)
            return 0

        time.sleep(wait_sec)

        if not _is_pending(api, order_no):
            price = _wait_buy_price(api, code)
            log.info(f"  체결 완료(정정 후): {price:,}원")
            return price

        log.info(f"  재정정 후도 미체결 → 취소")
        _try_cancel(api, order_no, code, qty)
        return 0

    except Exception as e:
        log.error(f"[매수 오류] {name}({code}): {e}")
        if order_no:
            _try_cancel(api, order_no, code, qty)
        return 0
