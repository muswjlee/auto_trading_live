"""
매매 전략 모음
각 전략은 signal(stock_code) -> 'buy' | 'sell' | None 을 반환
"""

from kis_api import KISApi


def _moving_average(prices: list[float], n: int) -> float:
    return sum(prices[:n]) / n


class GoldenCrossStrategy:
    """
    골든크로스 / 데드크로스 전략
    - 단기 이동평균이 장기 이동평균을 상향 돌파 → 매수
    - 단기 이동평균이 장기 이동평균을 하향 돌파 → 매도
    """

    def __init__(self, api: KISApi, short: int = 5, long: int = 20):
        self.api = api
        self.short = short
        self.long = long

    def signal(self, stock_code: str) -> str | None:
        candles = self.api.get_ohlcv(stock_code, count=self.long + 1)
        if len(candles) < self.long + 1:
            return None

        closes = [float(c["stck_clpr"]) for c in candles]

        ma_short_now = _moving_average(closes, self.short)
        ma_long_now = _moving_average(closes, self.long)
        ma_short_prev = _moving_average(closes[1:], self.short)
        ma_long_prev = _moving_average(closes[1:], self.long)

        if ma_short_prev <= ma_long_prev and ma_short_now > ma_long_now:
            return "buy"
        if ma_short_prev >= ma_long_prev and ma_short_now < ma_long_now:
            return "sell"
        return None


class RSIStrategy:
    """
    RSI 과매수/과매도 전략
    - RSI < 30 → 매수
    - RSI > 70 → 매도
    """

    def __init__(self, api: KISApi, period: int = 14, oversold: int = 30, overbought: int = 70):
        self.api = api
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def _rsi(self, closes: list[float]) -> float:
        gains, losses = [], []
        for i in range(1, self.period + 1):
            diff = closes[i - 1] - closes[i]
            (gains if diff > 0 else losses).append(abs(diff))
        avg_gain = sum(gains) / self.period
        avg_loss = sum(losses) / self.period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def signal(self, stock_code: str) -> str | None:
        candles = self.api.get_ohlcv(stock_code, count=self.period + 2)
        if len(candles) < self.period + 1:
            return None

        closes = [float(c["stck_clpr"]) for c in candles]
        rsi = self._rsi(closes)

        if rsi < self.oversold:
            return "buy"
        if rsi > self.overbought:
            return "sell"
        return None
