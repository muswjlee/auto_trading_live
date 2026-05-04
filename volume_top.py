"""거래량 상위 100 종목 현재가 / 거래량 조회 예시"""

from kis_api import KISApi


def main():
    api = KISApi()

    print("거래량 상위 100 종목 조회 중...")
    stocks = api.get_volume_rank(
        top_n=100,
        market="0",       # 0: 전체, 1: 코스피, 2: 코스닥
        min_price=1000,   # 1,000원 이상만
    )

    print(f"\n{'순위':>4}  {'코드':>8}  {'종목명':<16}  {'현재가':>10}  {'거래량':>14}  {'등락률':>7}")
    print("-" * 70)
    for s in stocks:
        sign = "+" if s["change"] >= 0 else ""
        print(
            f"{s['rank']:>4}  "
            f"{s['code']:>8}  "
            f"{s['name']:<16}  "
            f"{s['price']:>10,}  "
            f"{s['volume']:>14,}  "
            f"{sign}{s['change_rate']:>6.2f}%"
        )


if __name__ == "__main__":
    main()
