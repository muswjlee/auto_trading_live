"""
쌍봉 패턴 스캐너 (조회용)

거래대금 상위 300 종목의 일봉 75일치를 분석해
최근 3개월 내 쌍봉을 찍은 종목을 출력합니다.

사용: python scan_double_top.py
"""

import io
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kis_api import KISApi

# ── 파라미터 ──────────────────────────────────────────────────────────────────
TOP_N             = 300   # 유니버스 크기
OHLCV_DAYS        = 75    # 수집 일수 (≈3개월)
PEAK_WINDOW       = 5     # 로컬 고점 탐지 윈도우 (전후 N일)
PEAK_SIMILARITY   = 0.03  # 두 고점 가격 허용 오차 (3%)
MIN_TROUGH_DROP   = 0.05  # 두 고점 사이 최소 조정폭 (5%)
MIN_PEAK_GAP      = 10    # 두 고점 최소 간격 (거래일)
MAX_PEAK_GAP      = 45    # 두 고점 최대 간격 (거래일)
RECENT_DAYS       = 25    # 두 번째 고점이 이 기간 내여야 함
MAX_WORKERS       = 8

ETF_KW = [
    "ETF", "레버리지", "인버스", "KODEX", "TIGER", "KBSTAR", "HANARO",
    "SOL", "ACE", "RISE", "PLUS", "KoAct", "TIME", "KOSEF", "ARIRANG",
    "파생", "스팩", "리츠", "ETN",
]

# ─────────────────────────────────────────────────────────────────────────────

def _is_etf(name: str) -> bool:
    return any(k in name for k in ETF_KW)


def _find_peaks(prices: list[float], window: int) -> list[int]:
    """로컬 고점 인덱스 목록 반환 (전후 window일 대비 최고)"""
    peaks = []
    n = len(prices)
    for i in range(window, n - window):
        lo, hi = i - window, i + window
        if prices[i] == max(prices[lo:hi + 1]):
            peaks.append(i)
    return peaks


def _detect_double_top(ohlcv: list[dict]) -> dict | None:
    """
    ohlcv: 최신순 정렬 (index 0 = 오늘)
    반환: 쌍봉 정보 dict 또는 None
    """
    n = len(ohlcv)
    if n < OHLCV_DAYS // 2:
        return None

    # 시계열 순으로 뒤집기 (오래된 것 → 최신)
    ohlcv_asc = list(reversed(ohlcv))
    highs  = [int(d.get("stck_hgpr", 0)) for d in ohlcv_asc]
    closes = [int(d.get("stck_clpr", 0)) for d in ohlcv_asc]
    dates  = [d.get("stck_bsop_date", "") for d in ohlcv_asc]

    if any(h == 0 for h in highs):
        return None

    peaks = _find_peaks(highs, PEAK_WINDOW)
    if len(peaks) < 2:
        return None

    best = None
    for i in range(len(peaks) - 1):
        for j in range(i + 1, len(peaks)):
            p1, p2 = peaks[i], peaks[j]
            gap = p2 - p1
            if gap < MIN_PEAK_GAP or gap > MAX_PEAK_GAP:
                continue

            # 두 번째 고점이 최근 RECENT_DAYS 이내
            if (n - 1 - p2) > RECENT_DAYS:
                continue

            h1, h2 = highs[p1], highs[p2]

            # 두 고점 가격 유사도
            if abs(h1 - h2) / max(h1, h2) > PEAK_SIMILARITY:
                continue

            # 두 고점 사이 최저 종가
            trough = min(closes[p1:p2 + 1])
            lower_peak = min(h1, h2)
            if (lower_peak - trough) / lower_peak < MIN_TROUGH_DROP:
                continue

            # 가장 최근 쌍봉 우선
            if best is None or p2 > best["p2_idx"]:
                best = {
                    "p1_idx": p1, "p2_idx": p2,
                    "p1_date": dates[p1], "p2_date": dates[p2],
                    "p1_high": h1, "p2_high": h2,
                    "trough": trough,
                    "trough_drop_pct": round((lower_peak - trough) / lower_peak * 100, 1),
                    "peak_gap_days": gap,
                    "current_close": closes[-1],
                    "current_date": dates[-1],
                }

    return best


def _check(api: KISApi, code: str, name: str) -> dict | None:
    try:
        ohlcv = api.get_ohlcv(code, period="D", count=OHLCV_DAYS)
        if len(ohlcv) < 30:
            return None
        result = _detect_double_top(ohlcv)
        if result:
            result["code"] = code
            result["name"] = name
            # 현재가 대비 두 번째 고점 대비 위치
            result["from_p2_pct"] = round(
                (result["current_close"] - result["p2_high"]) / result["p2_high"] * 100, 1
            )
        return result
    except Exception:
        return None


def main():
    api = KISApi()

    print("거래대금 상위 300 종목 수집 중...")
    raw = api.get_volume_rank(top_n=TOP_N, market="0", min_price=3000, max_price=0, by_turnover=True)
    stocks = [
        {"code": s["code"], "name": s["name"]}
        for s in raw
        if s["code"] and not _is_etf(s["name"])
    ]
    print(f"ETF 제외 후 {len(stocks)}개 → 쌍봉 분석 시작\n")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_check, api, s["code"], s["name"]): s for s in stocks}
        done = 0
        for f in as_completed(futs):
            done += 1
            r = f.result()
            if r:
                results.append(r)
            if done % 50 == 0:
                print(f"  진행 {done}/{len(stocks)} - 쌍봉 {len(results)}개")

    # 두 번째 고점이 최근일수록 상단 정렬
    results.sort(key=lambda x: x["p2_date"], reverse=True)

    print(f"\n{'='*72}")
    print(f"  쌍봉 패턴 감지 결과 - {len(results)}개  "
          f"(파라미터: 유사도+-{int(PEAK_SIMILARITY*100)}%, 조정>={int(MIN_TROUGH_DROP*100)}%, 간격 {MIN_PEAK_GAP}~{MAX_PEAK_GAP}일)")
    print(f"{'='*72}")

    if not results:
        print("  해당 종목 없음")
        return

    fmt = "{:<6} {:<16} {:>8} {:>8} {:>8} {:>6} {:>6} {:>8}"
    print(fmt.format("코드", "종목명", "1차고점", "2차고점", "조정저점", "조정폭", "간격", "현재/2고점"))
    print("-" * 72)
    for r in results:
        print(fmt.format(
            r["code"],
            r["name"][:14],
            f"{r['p1_high']:,}",
            f"{r['p2_high']:,}",
            f"{r['trough']:,}",
            f"-{r['trough_drop_pct']}%",
            f"{r['peak_gap_days']}일",
            f"{r['from_p2_pct']:+.1f}%",
        ))
        print(f"       1차:{r['p1_date']}  2차:{r['p2_date']}  현재({r['current_date']}):{r['current_close']:,}원")
        print()


if __name__ == "__main__":
    main()
