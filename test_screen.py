"""
신규 모멘텀 전략 스크리닝 조건 테스트 — 주문 없음, live API 조회만 사용
사용법: python test_screen.py [전략ID]
예시:   python test_screen.py momentum_0910
"""

import sys
import json
import logging
import os

os.environ.setdefault("KIS_MODE", "live")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def load_strategy(strategy_id: str) -> dict:
    path = os.path.join("strategies", f"{strategy_id}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_screen(strategy_id: str):
    from kis_api import KISApi
    from engine.screener import screen

    strategy = load_strategy(strategy_id)
    log.info(f"=== 전략: {strategy['name']} ===")
    log.info(f"매수 조건:")
    entry = strategy["entry"]
    log.info(f"  누적 체결강도    > {entry.get('min_accumulated_execution_strength', '-')}")
    log.info(f"  VWAP 상위 조건  : {entry.get('require_above_vwap', False)}")
    log.info(f"  당일고가 근접   : {entry.get('near_day_high_pct', '-')}% 이내")
    log.info(f"  30초 체결강도   > {entry.get('instant_execution_strength_30s', '-')}")
    log.info(f"  1분 거래대금 배율 > {entry.get('min_1min_turnover_ratio', '-')}배")
    log.info("")

    api = KISApi()

    # enabled 상태 무관하게 강제 실행 (테스트용)
    strategy_copy = dict(strategy)
    strategy_copy["enabled"] = True

    log.info("─── 스크리닝 시작 ───")
    result = screen(api, strategy_copy)

    log.info("")
    log.info(f"=== 최종 통과 종목: {len(result)}개 ===")
    for i, s in enumerate(result, 1):
        log.info(
            f"  {i}. {s['name']}({s['code']})"
            f" 체결강도={s['execution_strength']:.1f}"
            f" VWAP비율={s['vwap_ratio']:.1f}%"
            f" 등락률={s.get('change_rate', 0):+.2f}%"
            f" 현재가={s['price']:,}원"
        )

    if not result:
        log.info("  (통과 종목 없음 — 조건이 너무 엄격하거나 장외 시간일 수 있음)")

    log.info("")
    log.info("※ 주문은 발생하지 않았습니다.")


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "momentum_0910"
    test_screen(sid)
