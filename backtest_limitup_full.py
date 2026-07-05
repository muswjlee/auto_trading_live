"""
상한가 전략 백테스트 — 전종목 스캔, 최근 1개월

전략:
  - 스크리닝: 당일 종가 >= 전일 종가 × 1.29 (상한가)
  - ETF·관리종목 제외 (종목명 키워드 기반)
  - 진입: 다음날 시가 > 상한가일 종가 (갭 상승) → 시가 매수
  - 청산: +1% 익절 / -1% 손절 / 당일 미달성 시 종가 청산
  - 동일일 TP+SL 동시 터치: SL 우선 (보수적)

유니버스:
  - 네이버 금융 전종목 리스트 (~2,100개, KOSPI+KOSDAQ)
  - KIS API OHLCV 30일 (약 1개월 + 버퍼)

사용: python backtest_limitup_full.py
"""

import io, os, re, sys, json, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime

import requests as _req

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kis_api import KISApi

_BASE = os.path.dirname(os.path.abspath(__file__))

BUY_FEE  = 0.000018
SELL_FEE = 0.000018
SELL_TAX = 0.002
AMOUNT   = 300_000

LIMITUP_PCT   = 29.0
TP_PCT        = 0.01
SL_PCT        = 0.01
OHLCV_COUNT   = 30   # 30거래일 (1개월 + 버퍼)
BACKTEST_DAYS = 23   # 실제 백테스트 기간 (거래일 기준)

_ETF_KW = [
    "ETF","레버리지","인버스","KODEX","TIGER","KBSTAR","HANARO","SOL","ACE",
    "RISE","PLUS","KoAct","TIME","KOSEF","ARIRANG","파생","스팩","리츠","선물",
    "옵션","WON","1Q","PLUS","HANARO","SMART","마이다스","히어로",
]


def _is_etf(name: str) -> bool:
    return any(k in name for k in _ETF_KW)


# ── 전종목 코드 수집 (네이버 금융) ───────────────────────────────────────────

def get_all_codes() -> list[tuple[str, str]]:
    """네이버 금융 시가총액 순위에서 KOSPI+KOSDAQ 전종목 코드+이름 수집"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    base = "https://finance.naver.com/sise/sise_market_sum.nhn?market=KOSPI&page={}"
    seen = {}

    for page in range(1, 100):
        try:
            r = _req.get(base.format(page), headers=headers, timeout=10)
            # 종목코드 + 이름 패턴 파싱
            pairs = re.findall(r'<a href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>', r.text)
            new_found = False
            for code, name in pairs:
                if code not in seen:
                    seen[code] = name.strip()
                    new_found = True
            if not new_found:
                break
            time.sleep(0.08)
        except Exception:
            time.sleep(0.5)

    # KOSDAQ도 수집 (누락 없이)
    base2 = "https://finance.naver.com/sise/sise_market_sum.nhn?market=KOSDAQ&page={}"
    for page in range(1, 100):
        try:
            r = _req.get(base2.format(page), headers=headers, timeout=10)
            pairs = re.findall(r'<a href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>', r.text)
            new_found = False
            for code, name in pairs:
                if code not in seen:
                    seen[code] = name.strip()
                    new_found = True
            if not new_found:
                break
            time.sleep(0.08)
        except Exception:
            time.sleep(0.5)

    result = [(code, name) for code, name in seen.items() if not _is_etf(name)]
    return result


# ── OHLCV 수집 ───────────────────────────────────────────────────────────────

def _collect(api: KISApi, code: str, name: str) -> dict | None:
    try:
        ohlcv = api.get_ohlcv(code, period="D", count=OHLCV_COUNT)
        if len(ohlcv) < 5:
            return None
        return {"code": code, "name": name, "ohlcv": ohlcv}
    except Exception:
        return None


# ── 시뮬레이션 ───────────────────────────────────────────────────────────────

def _make_trade(stock, screen_d, entry_d, buy_price, sell_price, reason, change_rate):
    qty   = max(1, AMOUNT // buy_price)
    gross = (sell_price - buy_price) * qty
    cost  = round(buy_price * qty * BUY_FEE + sell_price * qty * (SELL_FEE + SELL_TAX))
    net   = gross - cost
    pct   = (sell_price - buy_price) / buy_price * 100
    return {
        "code":         stock["code"],
        "name":         stock["name"],
        "screen_date":  screen_d.get("stck_bsop_date", ""),
        "entry_date":   entry_d.get("stck_bsop_date", ""),
        "limitup_rate": round(change_rate, 1),
        "buy_price":    buy_price,
        "sell_price":   round(sell_price),
        "qty":          qty,
        "pnl_pct":      round(pct, 2),
        "gross_pnl":    gross,
        "cost":         cost,
        "net_pnl":      net,
        "reason":       reason,
    }


def _simulate(stock: dict) -> list[dict]:
    ohlcv = stock["ohlcv"]  # ohlcv[0]=최신, ohlcv[-1]=가장 오래된
    n     = len(ohlcv)
    trades = []

    # i=스크리닝일, i-1=진입일(다음 거래일), i+1=전날
    for i in range(1, min(n - 1, BACKTEST_DAYS + 2)):
        screen_d = ohlcv[i]
        entry_d  = ohlcv[i - 1]
        prev_d   = ohlcv[i + 1]

        # 거래량 0 → 거래정지/관리 → 스킵
        if int(screen_d.get("acml_vol", "0") or "0") == 0:
            continue

        sc = int(screen_d.get("stck_clpr", 0))
        pc = int(prev_d.get("stck_clpr", 0))
        if sc <= 0 or pc <= 0:
            continue

        change_rate = (sc - pc) / pc * 100
        if change_rate < LIMITUP_PCT:
            continue

        # 다음날 시가 > 상한가일 종가 (갭 상승)
        entry_open  = int(entry_d.get("stck_oprc", 0))
        entry_high  = int(entry_d.get("stck_hgpr", 0))
        entry_low   = int(entry_d.get("stck_lwpr", 0))
        entry_close = int(entry_d.get("stck_clpr", 0))
        if entry_open <= 0:
            continue
        if entry_open <= sc:
            continue  # 갭 상승 없음

        buy_price = entry_open
        tp_price  = buy_price * (1 + TP_PCT)
        sl_price  = buy_price * (1 - SL_PCT)

        # SL 우선 (보수적)
        if entry_low <= sl_price:
            sell_price, reason = sl_price, "손절"
        elif entry_high >= tp_price:
            sell_price, reason = tp_price, "익절"
        else:
            sell_price, reason = entry_close, "종가청산"

        trades.append(_make_trade(stock, screen_d, entry_d, buy_price, sell_price, reason, change_rate))

    return trades


# ── 메인 ─────────────────────────────────────────────────────────────────────

def run():
    t0 = time.time()

    # 1. 전종목 코드 수집
    print("전종목 코드 수집 중 (네이버 금융)...", flush=True)
    all_stocks = get_all_codes()
    print(f"ETF 제외 후 {len(all_stocks)}개  ({time.time()-t0:.0f}초)", flush=True)

    # 2. OHLCV 수집 (병렬)
    api = KISApi()
    print(f"OHLCV {OHLCV_COUNT}일 수집 중...", flush=True)
    stocks = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_collect, api, code, name): (code, name)
                for code, name in all_stocks}
        done = 0
        for f in as_completed(futs):
            done += 1
            r = f.result()
            if r:
                stocks.append(r)
            if done % 200 == 0:
                elapsed = time.time() - t0
                remaining = elapsed / done * (len(all_stocks) - done)
                print(f"  {done}/{len(all_stocks)} 완료 (남은 시간 약 {remaining:.0f}초)...", flush=True)

    print(f"데이터 수집 완료: {len(stocks)}개  ({time.time()-t0:.0f}초)\n", flush=True)

    # 3. 백테스트
    all_trades = []
    for stock in stocks:
        all_trades.extend(_simulate(stock))
    all_trades.sort(key=lambda x: (x["entry_date"], x["name"]))

    # 4. 집계
    total = len(all_trades)
    if total == 0:
        print("거래 없음")
        return

    wins      = [t for t in all_trades if t["net_pnl"] > 0]
    tp_trades = [t for t in all_trades if t["reason"] == "익절"]
    sl_trades = [t for t in all_trades if t["reason"] == "손절"]
    ev_trades = [t for t in all_trades if t["reason"] == "종가청산"]

    total_net   = sum(t["net_pnl"]   for t in all_trades)
    total_gross = sum(t["gross_pnl"] for t in all_trades)
    total_cost  = sum(t["cost"]      for t in all_trades)
    avg_pct     = sum(t["pnl_pct"]   for t in all_trades) / total

    avg_tp = sum(t["pnl_pct"] for t in tp_trades) / len(tp_trades) if tp_trades else 0
    avg_sl = sum(t["pnl_pct"] for t in sl_trades) / len(sl_trades) if sl_trades else 0
    avg_ev = sum(t["pnl_pct"] for t in ev_trades) / len(ev_trades) if ev_trades else 0

    sep = "=" * 70
    print(sep)
    print(f"  스윙 상한가 전략 백테스트  (최근 1개월, 전종목 스캔)")
    print(f"  유니버스: {len(stocks)}개  |  상한가 기준: +{LIMITUP_PCT}% 이상")
    print(f"  진입조건: 다음날 갭상승 시가 매수  |  TP/SL: +{TP_PCT*100:.0f}%/{-SL_PCT*100:.0f}%")
    print(sep)
    print(f"  총 거래: {total}건  (일 평균: {total/BACKTEST_DAYS:.1f}건)")
    print(f"    익절: {len(tp_trades):>4}건 ({len(tp_trades)/total*100:>5.1f}%)  평균 {avg_tp:+.2f}%")
    print(f"    손절: {len(sl_trades):>4}건 ({len(sl_trades)/total*100:>5.1f}%)  평균 {avg_sl:+.2f}%")
    print(f"  종가청산: {len(ev_trades):>4}건 ({len(ev_trades)/total*100:>5.1f}%)  평균 {avg_ev:+.2f}%")
    print(f"  승률(순손익>0): {len(wins)}/{total} = {len(wins)/total*100:.1f}%")
    print(f"  평균 손익률: {avg_pct:+.3f}%")
    print(sep)
    print(f"  총 순손익:  {total_net:>+12,.0f}원")
    print(f"  총 수익금:  {total_gross:>+12,.0f}원")
    print(f"  총 비용:   -{total_cost:>11,.0f}원")
    print(sep)

    # 날짜별 집계
    by_day = defaultdict(list)
    for t in all_trades:
        by_day[t["entry_date"]].append(t)

    print("\n  [ 날짜별 상한가 거래 요약 ]")
    print(f"  {'진입일':<10}  {'거래':>4}  {'익절':>4}  {'손절':>4}  {'종가':>4}  {'승률':>6}  {'순손익':>13}")
    print("  " + "-" * 62)
    for day in sorted(by_day.keys()):
        ts  = by_day[day]
        mn  = sum(t["net_pnl"] for t in ts)
        mtp = sum(1 for t in ts if t["reason"] == "익절")
        msl = sum(1 for t in ts if t["reason"] == "손절")
        mev = sum(1 for t in ts if t["reason"] == "종가청산")
        mw  = sum(1 for t in ts if t["net_pnl"] > 0)
        wr  = mw / len(ts) * 100
        print(f"  {day:<10}  {len(ts):>4}  {mtp:>4}  {msl:>4}  {mev:>4}  {wr:>5.0f}%  {mn:>+13,.0f}원")

    # 수익/손실 상위
    print("\n  [ 수익 TOP 10 ]")
    print(f"  {'진입일':<10}  {'종목':<14}  {'상한가':>7}  {'매수':>8}  {'청산':>8}  {'손익%':>7}  {'순손익':>10}  사유")
    print("  " + "-" * 78)
    for t in sorted(all_trades, key=lambda x: x["net_pnl"], reverse=True)[:10]:
        print(f"  {t['entry_date']:<10}  {t['name']:<14}  {t['limitup_rate']:>6.1f}%"
              f"  {t['buy_price']:>8,}  {t['sell_price']:>8,}  {t['pnl_pct']:>+6.2f}%  {t['net_pnl']:>+9,}원  {t['reason']}")

    print("\n  [ 손실 TOP 10 ]")
    for t in sorted(all_trades, key=lambda x: x["net_pnl"])[:10]:
        print(f"  {t['entry_date']:<10}  {t['name']:<14}  {t['limitup_rate']:>6.1f}%"
              f"  {t['buy_price']:>8,}  {t['sell_price']:>8,}  {t['pnl_pct']:>+6.2f}%  {t['net_pnl']:>+9,}원  {t['reason']}")

    print(sep)
    print(f"  총 소요시간: {time.time()-t0:.0f}초")
    print(sep)

    # 저장
    out = os.path.join(_BASE, "backtest_swing_limitup_full.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "run_at":        datetime.now().strftime("%Y-%m-%d %H:%M"),
            "universe":      len(stocks),
            "backtest_days": BACKTEST_DAYS,
            "total_trades":  total,
            "tp_rate_pct":   round(len(tp_trades)/total*100, 1),
            "sl_rate_pct":   round(len(sl_trades)/total*100, 1),
            "win_rate_pct":  round(len(wins)/total*100, 1),
            "avg_pnl_pct":   round(avg_pct, 3),
            "total_net_pnl": round(total_net),
            "daily": {
                day: {
                    "trades": len(ts),
                    "tp": sum(1 for t in ts if t["reason"]=="익절"),
                    "sl": sum(1 for t in ts if t["reason"]=="손절"),
                    "net_pnl": round(sum(t["net_pnl"] for t in ts)),
                }
                for day, ts in sorted(by_day.items())
            },
            "trades": all_trades,
        }, f, ensure_ascii=False, indent=2)
    print(f"  저장: {out}")


if __name__ == "__main__":
    run()
