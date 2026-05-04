"""기술적 지표 계산 모듈 (RSI, MACD)"""


def _ema(data: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    ema = [sum(data[:period]) / period]
    for price in data[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def rsi(closes: list[float], period: int = 14) -> float:
    """RSI 계산 (마지막 값 반환)"""
    if len(closes) < period + 1:
        return 50.0

    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """
    MACD 계산
    반환: {"macd": [...], "signal": [...], "hist": [...]}
    마지막 두 개 값으로 골든크로스 여부 판단
    """
    if len(closes) < slow + signal:
        return {"macd": [], "signal": [], "hist": [], "golden_cross": False}

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)

    # 길이 맞추기 (slow - fast 만큼 앞부분 제거)
    offset = slow - fast
    ema_fast = ema_fast[offset:]

    macd_line   = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = _ema(macd_line, signal)

    # signal_line 길이에 맞게 macd_line 끝 정렬
    macd_aligned = macd_line[len(macd_line) - len(signal_line):]
    hist = [m - s for m, s in zip(macd_aligned, signal_line)]

    # 골든크로스: 전일 MACD < Signal → 당일 MACD > Signal
    golden_cross = (
        len(macd_aligned) >= 2
        and macd_aligned[-2] <= signal_line[-2]
        and macd_aligned[-1] > signal_line[-1]
    )

    return {
        "macd":         macd_aligned,
        "signal":       signal_line,
        "hist":         hist,
        "golden_cross": golden_cross,
    }
