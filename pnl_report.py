"""일자별 전략별 손익 리포트"""

import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
from collections import defaultdict
from datetime import date

BASE         = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE, "strategy_history.json")
PNL_FILE     = os.path.join(BASE, "daily_pnl.json")


def _compute_entry(data: dict) -> dict:
    groups = defaultdict(lambda: {
        "pnl": 0, "cost": 0, "gross_pnl": 0,
        "invested": 0, "trades": 0, "wins": 0, "losses": 0,
    })
    for t in data.get("trades", []):
        sid = t.get("strategy_id") or "기타"
        g = groups[sid]
        g["pnl"]      += t["pnl"]
        g["cost"]      += t.get("cost", 0)
        g["gross_pnl"] += t.get("gross_pnl", t["pnl"])
        g["invested"]  += t["buy_price"] * t["qty"]
        g["trades"]    += 1
        if t["pnl"] >= 0:
            g["wins"] += 1
        else:
            g["losses"] += 1
    return {
        "date":       data["date"],
        "total_pnl":  data["total_pnl"],
        "total_cost": data.get("total_cost", 0),
        "strategies": dict(groups),
    }


def load_history() -> list:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []

    # 오늘 데이터(daily_pnl.json)도 포함
    today = str(date.today())
    if not any(h["date"] == today for h in history):
        try:
            with open(PNL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today and data.get("trades"):
                history.append(_compute_entry(data))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    return sorted(history, key=lambda x: x["date"])


def print_report(history: list):
    if not history:
        print("기록 없음")
        return

    all_sids = sorted({
        sid
        for day in history
        for sid in day["strategies"]
    })

    W = 82
    print("=" * W)
    print(" 일자별 전략별 손익 리포트")
    print("=" * W)

    cumul = 0
    for day in history:
        d          = day["date"]
        total_pnl  = day["total_pnl"]
        total_cost = day.get("total_cost", 0)
        cumul     += total_pnl
        sign       = "+" if total_pnl >= 0 else ""

        print(f"\n[{d}]  순손익: {sign}{total_pnl:,}원   비용: -{total_cost:,}원")
        print(f"  {'전략':<28} {'투자금':>12} {'순손익':>10} {'수익률':>7}  {'승/패':>5}")
        print("  " + "-" * (W - 2))

        for sid in all_sids:
            s = day["strategies"].get(sid)
            if not s or s["trades"] == 0:
                continue
            invested = s["invested"]
            pnl      = s["pnl"]
            rate     = pnl / invested * 100 if invested else 0
            wins     = s["wins"]
            losses   = s["losses"]
            mark = "+" if pnl >= 0 else "-"
            print(
                f"  {mark} {sid:<26} {invested:>12,} {pnl:>+10,} {rate:>+6.2f}%  {wins}승{losses}패"
            )

    print("\n" + "=" * W)
    cumul_sign = "+" if cumul >= 0 else ""
    print(f" 누적 순손익: {cumul_sign}{cumul:,}원")
    print("=" * W)


if __name__ == "__main__":
    history = load_history()
    print_report(history)
