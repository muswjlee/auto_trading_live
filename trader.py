"""자동매매 실행 엔진"""

import time
import logging
from datetime import datetime
from kis_api import KISApi
from strategy import GoldenCrossStrategy, RSIStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("trading.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


class Trader:
    def __init__(
        self,
        watchlist: list[str],
        order_amount: int = 100_000,   # 1회 주문 금액 (원)
        interval_sec: int = 60,        # 체크 주기 (초)
    ):
        self.api = KISApi()
        self.watchlist = watchlist
        self.order_amount = order_amount
        self.interval_sec = interval_sec
        self.strategy = GoldenCrossStrategy(self.api)   # 전략 교체 가능

    def _is_market_open(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5:  # 토/일
            return False
        t = now.hour * 100 + now.minute
        return 900 <= t <= 1530

    def _qty_to_buy(self, price: int) -> int:
        return max(1, self.order_amount // price)

    def _holding_qty(self, stock_code: str) -> int:
        balance = self.api.get_balance()
        for s in balance["stocks"]:
            if s.get("pdno") == stock_code:
                return int(s.get("hldg_qty", 0))
        return 0

    def run_once(self):
        for code in self.watchlist:
            try:
                sig = self.strategy.signal(code)
                if sig is None:
                    continue

                price_info = self.api.get_price(code)
                current_price = int(price_info["stck_prpr"])
                name = price_info.get("hts_kor_isnm", code)

                if sig == "buy":
                    cash = self.api.get_cash()
                    qty = self._qty_to_buy(current_price)
                    cost = qty * current_price
                    if cost > cash:
                        log.warning(f"[{name}] 잔고 부족 (필요: {cost:,}원 / 보유: {cash:,}원)")
                        continue
                    result = self.api.buy(code, qty)
                    log.info(f"[매수] {name}({code}) {qty}주 @ {current_price:,}원  주문번호={result.get('odno')}")

                elif sig == "sell":
                    qty = self._holding_qty(code)
                    if qty == 0:
                        log.info(f"[{name}] 매도 신호이나 보유 수량 없음")
                        continue
                    result = self.api.sell(code, qty)
                    log.info(f"[매도] {name}({code}) {qty}주 @ {current_price:,}원  주문번호={result.get('odno')}")

            except Exception as e:
                log.error(f"[{code}] 오류: {e}")

    def run(self):
        log.info(f"자동매매 시작 | 종목: {self.watchlist}")
        while True:
            if self._is_market_open():
                self.run_once()
            else:
                log.debug("장 외 시간 — 대기 중")
            time.sleep(self.interval_sec)


if __name__ == "__main__":
    # ── 매매할 종목 코드 목록 ──────────────────────────────────
    WATCHLIST = [
        "005930",   # 삼성전자
        "000660",   # SK하이닉스
        "035420",   # NAVER
    ]

    trader = Trader(
        watchlist=WATCHLIST,
        order_amount=100_000,   # 1회 최대 주문 금액
        interval_sec=60,        # 60초마다 신호 체크
    )
    trader.run()
