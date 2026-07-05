"""
상한가 전략 백테스트 (swing_limitup)

전략 로직:
  - 스크리닝: 당일 종가가 전일 대비 +29% 이상 (상한가)
  - 진입: 다음날 시가 > 전일(상한가) 종가 → 시가에 매수
  - 청산: +1% 익절 / -1% 손절 / 당일 미달성 시 종가 청산

제약/가정:
  - 유니버스: 현재 거래대금 상위 N 종목 (서바이버십 바이어스 존재)
  - require_above_open(09:03 현재가>시가): 일봉 시뮬레이션 불가 → 생략
  - 동일일 TP+SL 동시 터치: SL 우선 (보수적)
  - 기간: 최근 252 거래일 (약 1년)

사용: python backtest_limitup.py [--universe N]
"""

import os, sys, json, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime

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
BACKTEST_DAYS = 252  # 1년

_ETF_KW = [
    "ETF","레버리지","인버스","KODEX","TIGER","KBSTAR","HANARO",
    "SOL","ACE","RISE","PLUS","KoAct","TIME","KOSEF","ARIRANG",
    "파생","스팩","리츠","선물","옵션","WON","1Q",
]


def _is_etf(name: str) -> bool:
    return any(k in name for k in _ETF_KW)


def _collect(api, code, name):
    try:
        ohlcv = api.get_ohlcv(code, period="D", count=BACKTEST_DAYS + 5)
        if len(ohlcv) < 5:
            return None
        return {"code": code, "name": name, "ohlcv": ohlcv}
    except Exception:
        return None


def _make_trade(stock, screen_d, entry_d, buy_price, sell_price, reason, change_rate):
    qty   = max(1, AMOUNT // buy_price)
    gross = (sell_price - buy_price) * qty
    cost  = round(buy_price * qty * BUY_FEE + sell_price * qty * (SELL_FEE + SELL_TAX))
    net   = gross - cost
    pct   = (sell_price - buy_price) / buy_price * 100
    return {
        "code":        stock["code"],
        "name":        stock["name"],
        "screen_date": screen_d.get("stck_bsop_date", ""),
        "entry_date":  entry_d.get("stck_bsop_date", ""),
        "limitup_rate": round(change_rate, 1),
        "buy_price":   buy_price,
        "sell_price":  round(sell_price),
        "qty":         qty,
        "pnl_pct":     round(pct, 2),
        "gross_pnl":   gross,
        "cost":        cost,
        "net_pnl":     net,
        "reason":      reason,
    }


def _simulate(stock: dict) -> list[dict]:
    """종목 한 개에 대한 1년 시뮬레이션"""
    ohlcv = stock["ohlcv"]  # ohlcv[0]=최신, ohlcv[-1]=가장 오래된
    n = len(ohlcv)
    trades = []

    # i = 상한가 스크리닝일 인덱스
    # ohlcv[i-1] = 다음 거래일(진입일), ohlcv[i+1] = 전날
    for i in range(1, min(n - 1, BACKTEST_DAYS + 1)):
        screen_d = ohlcv[i]
        entry_d  = ohlcv[i - 1]
        prev_d   = ohlcv[i + 1]

        # 거래량 0 (거래정지/관리종목) → 스킵
        if int(screen_d.get("acml_vol", "0") or "0") == 0:
            continue

        # 상한가 조건
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
            continue  # 갭 상승 없음 → 매수 안함

        buy_price = entry_open
        tp_price  = buy_price * (1 + TP_PCT)
        sl_price  = buy_price * (1 - SL_PCT)

        # TP/SL 판단 (SL 우선 — 보수적)
        if entry_low <= sl_price:
            sell_price = sl_price
            reason = "손절"
        elif entry_high >= tp_price:
            sell_price = tp_price
            reason = "익절"
        else:
            sell_price = entry_close
            reason = "종가청산"

        trades.append(_make_trade(stock, screen_d, entry_d, buy_price, sell_price, reason, change_rate))

    return trades


def run(universe_n: int = 300):
    api = KISApi()

    print(f"거래대금 상위 {universe_n} 종목 수집 중...", flush=True)
    raw = api.get_volume_rank(top_n=universe_n, market="0", min_price=1000, max_price=0, by_turnover=True)
    candidates = [s for s in raw if s.get("code") and not _is_etf(s.get("name", ""))]
    print(f"ETF 제외 후 {len(candidates)}개  |  OHLCV {BACKTEST_DAYS}일 수집 중...", flush=True)

    stocks = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_collect, api, s["code"], s["name"]): s for s in candidates}
        done = 0
        for f in as_completed(futs):
            done += 1
            r = f.result()
            if r:
                stocks.append(r)
            if done % 50 == 0:
                print(f"  수집 {done}/{len(candidates)} ...", flush=True)

    print(f"데이터 수집 완료: {len(stocks)}개\n", flush=True)

    # 백테스트 실행
    all_trades = []
    for stock in stocks:
        all_trades.extend(_simulate(stock))

    all_trades.sort(key=lambda x: (x["entry_date"], x["code"]))

    # ── 집계 ──────────────────────────────────────────────────────────────────
    total = len(all_trades)
    if total == 0:
        print("거래 없음")
        return

    wins      = [t for t in all_trades if t["net_pnl"] > 0]
    losses    = [t for t in all_trades if t["net_pnl"] <= 0]
    tp_trades = [t for t in all_trades if t["reason"] == "익절"]
    sl_trades = [t for t in all_trades if t["reason"] == "손절"]
    ev_trades = [t for t in all_trades if t["reason"] == "종가청산"]

    total_net   = sum(t["net_pnl"]   for t in all_trades)
    total_gross = sum(t["gross_pnl"] for t in all_trades)
    total_cost  = sum(t["cost"]      for t in all_trades)
    avg_pct     = sum(t["pnl_pct"]   for t in all_trades) / total

    avg_tp_pct = sum(t["pnl_pct"] for t in tp_trades) / len(tp_trades) if tp_trades else 0
    avg_sl_pct = sum(t["pnl_pct"] for t in sl_trades) / len(sl_trades) if sl_trades else 0
    avg_ev_pct = sum(t["pnl_pct"] for t in ev_trades) / len(ev_trades) if ev_trades else 0

    sep = "=" * 68
    print(sep)
    print(f"  스윙 상한가 전략 백테스트  (최근 약 1년)")
    print(f"  유니버스: {len(stocks)}개 종목  |  기준: 상한가(+{LIMITUP_PCT}%) 다음날 갭 상승 진입")
    print(sep)
    print(f"  총 거래: {total}건")
    print(f"    익절: {len(tp_trades)}건 ({len(tp_trades)/total*100:.1f}%)  평균 {avg_tp_pct:+.2f}%")
    print(f"    손절: {len(sl_trades)}건 ({len(sl_trades)/total*100:.1f}%)  평균 {avg_sl_pct:+.2f}%")
    print(f"  종가청산: {len(ev_trades)}건 ({len(ev_trades)/total*100:.1f}%)  평균 {avg_ev_pct:+.2f}%")
    print(f"  승률(익절+종가청산양수): {len(wins)}/{total} = {len(wins)/total*100:.1f}%")
    print(f"  평균 손익률: {avg_pct:+.2f}%")
    print(sep)
    print(f"  총 순손익: {total_net:+,}원")
    print(f"  총 수익금: {total_gross:+,}원  /  총 비용: -{total_cost:,}원")
    print(sep)

    # 월별 집계
    by_month = defaultdict(list)
    for t in all_trades:
        ym = t["entry_date"][:6]
        by_month[ym].append(t)

    print("\n  [ 월별 요약 ]")
    print(f"  {'월':>7}  {'거래':>4}  {'익절':>4}  {'손절':>4}  {'종가':>4}  {'승률':>6}  {'순손익':>12}")
    print("  " + "-" * 60)
    for ym in sorted(by_month.keys()):
        ts = by_month[ym]
        mn = sum(t["net_pnl"] for t in ts)
        mtp = sum(1 for t in ts if t["reason"] == "익절")
        msl = sum(1 for t in ts if t["reason"] == "손절")
        mev = sum(1 for t in ts if t["reason"] == "종가청산")
        mw  = sum(1 for t in ts if t["net_pnl"] > 0)
        wr  = mw / len(ts) * 100
        sign = "+" if mn >= 0 else ""
        print(f"  {ym[:4]}-{ym[4:]:>2}  {len(ts):>4}  {mtp:>4}  {msl:>4}  {mev:>4}  {wr:>5.0f}%  {sign}{mn:>11,}원")

    # 상위 손익 거래
    print("\n  [ 수익 TOP 10 ]")
    top_wins = sorted(all_trades, key=lambda x: x["net_pnl"], reverse=True)[:10]
    print(f"  {'진입일':<10} {'종목':<14} {'상한가%':>7} {'매수':>8} {'청산':>8} {'손익%':>7} {'순손익':>10} 사유")
    print("  " + "-" * 75)
    for t in top_wins:
        print(f"  {t['entry_date']:<10} {t['name']:<14} {t['limitup_rate']:>6.1f}%"
              f"  {t['buy_price']:>7,}  {t['sell_price']:>7,}  {t['pnl_pct']:>+6.2f}%  {t['net_pnl']:>+9,}원  {t['reason']}")

    print("\n  [ 손실 TOP 10 ]")
    top_losses = sorted(all_trades, key=lambda x: x["net_pnl"])[:10]
    for t in top_losses:
        print(f"  {t['entry_date']:<10} {t['name']:<14} {t['limitup_rate']:>6.1f}%"
              f"  {t['buy_price']:>7,}  {t['sell_price']:>7,}  {t['pnl_pct']:>+6.2f}%  {t['net_pnl']:>+9,}원  {t['reason']}")

    print(sep)

    # JSON 저장
    out = os.path.join(_BASE, "backtest_swing_limitup.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "universe_count": len(stocks),
            "backtest_days": BACKTEST_DAYS,
            "total_trades": total,
            "win_rate_pct": round(len(wins) / total * 100, 1),
            "tp_rate_pct":  round(len(tp_trades) / total * 100, 1),
            "sl_rate_pct":  round(len(sl_trades) / total * 100, 1),
            "avg_pnl_pct": round(avg_pct, 2),
            "total_net_pnl": total_net,
            "monthly": {
                ym: {
                    "trades": len(ts),
                    "net_pnl": sum(t["net_pnl"] for t in ts),
                }
                for ym, ts in sorted(by_month.items())
            },
            "trades": all_trades,
        }, f, ensure_ascii=False, indent=2)
    print(f"  상세 저장: {out}")
    print(sep)


if __name__ == "__main__":
    n = 300
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--universe" and i + 1 < len(args):
            n = int(args[i + 1])
        elif a.startswith("--universe="):
            n = int(a.split("=")[1])
    run(n)
