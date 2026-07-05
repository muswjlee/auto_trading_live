"""스윙 돌파 전략 백테스트

데이터 범위: 최근 90거래일 (KIS API 다중 호출)
백테스트 기간: 약 20~25거래일 (60일 신고가 기준 확보 후)
외국인/기관: 최근 30거래일 데이터 반영

제약사항:
  - 진입가: 다음날 시가 (현재가>시가 조건 → 당일 종가>시가로 단순화)
  - 익절/손절: 일봉 고/저가 기준 (분봉 사용 불가)
  - 동일일 TP·SL 동시 충족: 고가 먼저 도달 가정 → TP 우선
  - 개장 갭다운으로 즉시 SL: 시가 <= 진입가 × 0.95이면 시가에 손절
  - 미청산 포지션: 데이터 기간 초과 시 마지막 종가로 평가

사용: python backtest_swing.py [strategy_id] [--universe N]
      strategy_id 생략 시 swing_breakout
      --universe N: 유니버스 종목 수 (기본 200)
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kis_api import KISApi

_BASE = os.path.dirname(os.path.abspath(__file__))
_MODE = os.getenv("KIS_MODE", "live")

_ETF_KW = [
    "ETF","레버리지","인버스","KODEX","TIGER","KBSTAR","HANARO",
    "SOL","ACE","RISE","PLUS","KoAct","TIME","KOSEF","ARIRANG",
    "파생","스팩","리츠",
]

BUY_FEE   = 0.000018
SELL_FEE  = 0.000018
SELL_TAX  = 0.002


# ── 데이터 수집 ──────────────────────────────────────────────────────────────

def _collect(api: KISApi, code: str, name: str) -> dict | None:
    try:
        ohlcv    = api.get_ohlcv(code, period="D", count=92)
        investor = api.get_investor_trend(code, days=30)
        if len(ohlcv) < 65:
            return None
        inv_by_date = {d["stck_bsop_date"]: d for d in investor}
        return {"code": code, "name": name, "ohlcv": ohlcv, "inv": inv_by_date}
    except Exception:
        return None


def _get_amount(d: dict) -> int:
    return int(d.get("acml_vol", 0)) * int(d.get("stck_clpr", 0))


# ── 스크리닝 조건 ────────────────────────────────────────────────────────────

def _screen(stock: dict, i: int, strategy: dict) -> bool:
    """ohlcv[i]가 스크리닝 기준일일 때 조건 충족 여부"""
    ohlcv = stock["ohlcv"]
    uni   = strategy.get("universe", {})
    scr   = strategy.get("screen", {})

    if i + 61 >= len(ohlcv):
        return False

    today_d      = ohlcv[i]
    today_close  = int(today_d.get("stck_clpr", 0))
    today_amount = _get_amount(today_d)
    if today_close <= 0 or today_amount <= 0:
        return False

    # 60일 신고가
    high_days  = scr.get("high_days", 60)
    prior_high = [int(d.get("stck_clpr", 0)) for d in ohlcv[i+1:i+high_days+1]
                  if int(d.get("stck_clpr","0")) > 0]
    if not prior_high or today_close <= max(prior_high):
        return False

    # 거래대금 평균
    prior_20  = ohlcv[i+1:i+21]
    prior_5   = ohlcv[i+1:i+6]
    cur_5     = ohlcv[i:i+5]

    avg_20d    = sum(_get_amount(d) for d in prior_20) / len(prior_20) if prior_20 else 0
    avg_5d     = sum(_get_amount(d) for d in cur_5)   / len(cur_5)    if cur_5    else 0
    avg_prev5d = sum(_get_amount(d) for d in prior_5) / len(prior_5)  if prior_5  else 0

    if avg_20d <= 0 or avg_prev5d <= 0:
        return False

    # 5일 평균 거래대금 1,000억 이상
    min_5d = uni.get("min_5d_avg_amount_억", 1000) * 100_000_000
    if avg_5d < min_5d:
        return False

    # 거래대금 비율 3종
    if today_amount / avg_20d   <= scr.get("vol_ratio_today_vs_20d",   2.0): return False
    if avg_5d       / avg_20d   <= scr.get("vol_ratio_5d_vs_20d",       1.5): return False
    if today_amount / avg_prev5d <= scr.get("vol_ratio_today_vs_prev5d", 1.2): return False

    # 외국인/기관 5일 누적 순매수
    inv = stock["inv"]
    if scr.get("require_foreign_net_buy_5d", True) or scr.get("require_inst_net_buy_5d", True):
        dates_5 = [ohlcv[i+k].get("stck_bsop_date","") for k in range(5) if i+k < len(ohlcv)]
        if all(d in inv for d in dates_5):
            frgn = sum(int(inv[d].get("frgn_ntby_qty", 0)) for d in dates_5)
            inst = sum(int(inv[d].get("orgn_ntby_qty", 0)) for d in dates_5)
            if scr.get("require_foreign_net_buy_5d", True) and frgn <= 0:
                return False
            if scr.get("require_inst_net_buy_5d", True) and inst <= 0:
                return False
        # 투자자 데이터 없는 경우: 조건 생략 (오래된 날짜)

    return True


# ── 진입·청산 시뮬레이션 ─────────────────────────────────────────────────────

def _simulate_trade(stock: dict, entry_i: int, strategy: dict) -> dict | None:
    """ohlcv[entry_i]가 진입일.  entry_i = screening_i - 1"""
    ohlcv = stock["ohlcv"]
    exit_cfg  = strategy["exit"]
    tp_pct    = exit_cfg.get("take_profit", 10.0) / 100
    sl_pct    = abs(exit_cfg.get("stop_loss", -5.0)) / 100

    entry_day  = ohlcv[entry_i]
    entry_open = int(entry_day.get("stck_oprc", 0))
    entry_close= int(entry_day.get("stck_clpr", 0))

    if entry_open <= 0:
        return None

    # 진입 조건: 당일 종가 > 시가 (9:03 현재가 > 시가 단순화)
    if strategy["entry"].get("require_above_open", True) and entry_close <= entry_open:
        return None

    buy_price = entry_open  # 시가에 매수
    tp_price  = buy_price * (1 + tp_pct)
    sl_price  = buy_price * (1 - sl_pct)

    # 진입 당일 즉시 갭다운 SL 체크
    if entry_open <= sl_price:
        sell_price = entry_open
        return _make_trade(stock, entry_day, entry_day, buy_price, sell_price, "손절(갭)")

    # 진입 당일 고/저가로 TP/SL 체크
    entry_high = int(entry_day.get("stck_hgpr", 0))
    entry_low  = int(entry_day.get("stck_lwpr", 0))
    if entry_high >= tp_price:
        return _make_trade(stock, entry_day, entry_day, buy_price, tp_price, "익절")
    if entry_low <= sl_price:
        return _make_trade(stock, entry_day, entry_day, buy_price, sl_price, "손절")

    # 이후 날짜 추적 (entry_i-1 ~ 0)
    for k in range(entry_i - 1, -1, -1):
        d    = ohlcv[k]
        high = int(d.get("stck_hgpr", 0))
        low  = int(d.get("stck_lwpr", 0))
        opn  = int(d.get("stck_oprc", 0))
        if opn <= sl_price:
            return _make_trade(stock, entry_day, d, buy_price, opn, "손절(갭)")
        if high >= tp_price:
            return _make_trade(stock, entry_day, d, buy_price, tp_price, "익절")
        if low <= sl_price:
            return _make_trade(stock, entry_day, d, buy_price, sl_price, "손절")

    # 기간 내 미청산 - 마지막 종가로 평가
    last_d = ohlcv[0]
    sell_price = int(last_d.get("stck_clpr", 0))
    return _make_trade(stock, entry_day, last_d, buy_price, sell_price, "미청산(평가)")


def _make_trade(stock, entry_d, exit_d, buy_price, sell_price, reason):
    qty   = max(1, 300_000 // buy_price)
    gross = (sell_price - buy_price) * qty
    cost  = round(buy_price * qty * BUY_FEE + sell_price * qty * (SELL_FEE + SELL_TAX))
    net   = gross - cost
    pnl_pct = (sell_price - buy_price) / buy_price * 100
    return {
        "code":        stock["code"],
        "name":        stock["name"],
        "screen_date": "",
        "entry_date":  entry_d.get("stck_bsop_date", ""),
        "exit_date":   exit_d.get("stck_bsop_date",  ""),
        "buy_price":   buy_price,
        "sell_price":  sell_price,
        "qty":         qty,
        "pnl_pct":     round(pnl_pct, 2),
        "gross_pnl":   gross,
        "cost":        cost,
        "net_pnl":     net,
        "reason":      reason,
    }


# ── 백테스트 메인 ─────────────────────────────────────────────────────────────

def run(strategy_id: str = "swing_breakout", universe_n: int = 200):
    path = os.path.join(_BASE, "strategies", f"{strategy_id}.json")
    with open(path, encoding="utf-8") as f:
        strategy = json.load(f)

    api = KISApi()

    # ── 유니버스 수집 ──────────────────────────────────────────────────────
    print(f"거래대금 상위 {universe_n} 종목 수집 중...", flush=True)
    raw = api.get_volume_rank(top_n=universe_n, market="0", min_price=1000, max_price=0, by_turnover=True)
    candidates = [{"code": s["code"], "name": s["name"]}
                  for s in raw if s["code"] and not any(k in s["name"] for k in _ETF_KW)]
    print(f"ETF 제외 후 {len(candidates)}개 대상  OHLCV+투자자 데이터 수집 중...", flush=True)

    # ── 데이터 수집 (병렬) ─────────────────────────────────────────────────
    stocks = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_collect, api, s["code"], s["name"]): s for s in candidates}
        done = 0
        for f in as_completed(futs):
            done += 1
            r = f.result()
            if r:
                stocks.append(r)
            if done % 30 == 0:
                print(f"  수집 {done}/{len(candidates)} 완료...", flush=True)

    print(f"데이터 수집 완료: {len(stocks)}개\n", flush=True)

    # ── 백테스트 실행 ─────────────────────────────────────────────────────
    # 백테스트 가능 범위: 60일 신고가 확보(i+60 < 90) + 진입일 확보(i >= 2)
    # → i from 2 to 27 (약 25거래일 ≈ 5주)
    trades = []
    screened_days = set()

    for stock in stocks:
        ohlcv = stock["ohlcv"]
        for i in range(2, min(28, len(ohlcv) - 62)):
            screen_date = ohlcv[i].get("stck_bsop_date", "")
            entry_i     = i - 1

            if not _screen(stock, i, strategy):
                continue

            trade = _simulate_trade(stock, entry_i, strategy)
            if trade:
                trade["screen_date"] = screen_date
                trades.append(trade)
                screened_days.add(screen_date)

    # ── 결과 리포트 ───────────────────────────────────────────────────────
    trades.sort(key=lambda x: (x["entry_date"], x["code"]))

    total     = len(trades)
    wins      = [t for t in trades if t["net_pnl"] > 0]
    losses    = [t for t in trades if t["net_pnl"] <= 0]
    open_pos  = [t for t in trades if t["reason"] == "미청산(평가)"]
    tp_trades = [t for t in trades if t["reason"] == "익절"]
    sl_trades = [t for t in trades if "손절" in t["reason"]]

    total_net   = sum(t["net_pnl"]   for t in trades)
    total_gross = sum(t["gross_pnl"] for t in trades)
    total_cost  = sum(t["cost"]      for t in trades)
    avg_pnl_pct = sum(t["pnl_pct"]  for t in trades) / total if total else 0

    print("=" * 65)
    print(f"  {strategy['name']} - 백테스트 결과")
    print(f"  기간: 최근 약 25거래일 (오늘 기준 ~5주 전 ~ 2일 전)")
    print(f"  유니버스: {len(stocks)}개 종목")
    print("=" * 65)
    print(f"  총 거래: {total}건  |  익절: {len(tp_trades)}건  |  손절: {len(sl_trades)}건  |  미청산: {len(open_pos)}건")
    if total:
        print(f"  승률: {len(wins)/total*100:.1f}%  ({len(wins)}승 {len(losses)}패)")
        print(f"  평균 손익률: {avg_pnl_pct:+.2f}%")
        print(f"  총 순손익: {total_net:+,}원  (총수익: {total_gross:+,}원 / 비용: -{total_cost:,}원)")
        if tp_trades:
            avg_tp = sum(t["pnl_pct"] for t in tp_trades) / len(tp_trades)
            print(f"  평균 익절 수익률: +{avg_tp:.2f}%")
        if sl_trades:
            avg_sl = sum(t["pnl_pct"] for t in sl_trades) / len(sl_trades)
            print(f"  평균 손절 손실률: {avg_sl:.2f}%")
    print("=" * 65)

    if trades:
        print("\n  [ 거래 내역 ]")
        print(f"  {'스크리닝일':<10} {'종목':<14} {'진입일':<10} {'청산일':<10} {'매입':<8} {'청산':<8} {'수익률':>6} {'순손익':>9} {'사유'}")
        print("  " + "-" * 90)
        by_day = defaultdict(list)
        for t in trades:
            by_day[t["screen_date"]].append(t)

        for day in sorted(by_day.keys()):
            for t in by_day[day]:
                sign = "[+]" if t["net_pnl"] > 0 else "[-]" if t["net_pnl"] < 0 else "[ ]"
                print(
                    f"  {t['screen_date']:<10} {t['name']:<14} {t['entry_date']:<10} "
                    f"{t['exit_date']:<10} {t['buy_price']:>7,} {t['sell_price']:>7,} "
                    f"{t['pnl_pct']:>+6.2f}% {t['net_pnl']:>+9,}  {sign}{t['reason']}"
                )

    # 날짜별 요약
    if by_day if trades else False:
        print("\n  [ 날짜별 요약 ]")
        print(f"  {'스크리닝일':<10} {'거래수':>4} {'익절':>4} {'손절':>4} {'순손익':>10}")
        print("  " + "-" * 42)
        for day in sorted(by_day.keys()):
            ts = by_day[day]
            day_net = sum(t["net_pnl"] for t in ts)
            day_tp  = sum(1 for t in ts if t["reason"] == "익절")
            day_sl  = sum(1 for t in ts if "손절" in t["reason"])
            sign = "[+]" if day_net > 0 else "[-]" if day_net < 0 else "[ ]"
            print(f"  {day:<10} {len(ts):>4}  {day_tp:>4}  {day_sl:>4}  {day_net:>+10,} {sign}")

    # 저장
    out_file = os.path.join(_BASE, f"backtest_{strategy_id}.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({
            "strategy_id": strategy_id,
            "run_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "universe_count": len(stocks),
            "total_trades": total,
            "win_rate_pct": round(len(wins)/total*100, 1) if total else 0,
            "avg_pnl_pct": round(avg_pnl_pct, 2),
            "total_net_pnl": total_net,
            "trades": trades,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  상세 결과 저장: {out_file}")
    print("=" * 65)

    return trades


if __name__ == "__main__":
    sid  = "swing_breakout"
    n    = 200
    args = sys.argv[1:]
    for a in args:
        if a.startswith("--universe"):
            n = int(a.split("=")[-1]) if "=" in a else int(args[args.index(a)+1])
        elif not a.startswith("--"):
            sid = a
    run(sid, n)
