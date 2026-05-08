import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
from kis_api import KISApi
api = KISApi()
bal = api.get_balance()
stocks = bal.get("stocks", [])
print(f"실제 보유종목: {len(stocks)}개")
for st in stocks:
    name = st["prdt_name"]
    code = st["pdno"]
    qty = int(st["hldg_qty"])
    avg = int(float(st["pchs_avg_pric"]))
    cur = int(st["prpr"])
    pnl = int(st["evlu_pfls_amt"])
    rate = float(st["evlu_pfls_rt"])
    print(f"  {name}({code}) {qty}주 | 평균 {avg:,}원 | 현재 {cur:,}원 | {pnl:+,}원 ({rate:+.2f}%)")

with open("positions.json", encoding="utf-8") as f:
    pos = json.load(f)
print(f"\npositions.json: {len(pos)}개")
for k, p in pos.items():
    print(f"  {p['name']}({p['code']}) {p['qty']}주 @ {p['buy_price']:,}원 [{p['strategy_id']}]")
if not pos:
    print("  없음")
