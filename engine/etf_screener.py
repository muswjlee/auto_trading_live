"""ETF 모멘텀 전략 — 매주 토요일 21:00 스크리닝

흐름:
  1. 후보 ETF별 최근 80거래일 일봉 조회
  2. 20일/60일 수익률 계산 → 모멘텀 스코어 산출
  3. 필터(60일 MA 상회, 60일 수익률 > 0) 적용
  4. 최고 스코어 ETF 선정 → etf_momentum_signal_{MODE}.json 저장
  5. 텔레그램 알림

사용: python engine/etf_screener.py
"""

import json
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kis_api import KISApi
from engine.notifier import send

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE = os.getenv("KIS_MODE", "live")

log = logging.getLogger("etf_screener")


def _setup_logger():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.FileHandler(
            os.path.join(_BASE, f"etf_screener_{_MODE}.log"), encoding="utf-8"
        )
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)


def _load_strategy() -> dict:
    path = os.path.join(_BASE, "strategies", "etf_momentum.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run():
    _setup_logger()
    strategy = _load_strategy()
    mom_cfg  = strategy["momentum"]
    flt_cfg  = strategy["filter"]

    short_days  = mom_cfg["short_days"]   # 20
    long_days   = mom_cfg["long_days"]    # 60
    sw          = mom_cfg["short_weight"] # 0.5
    lw          = mom_cfg["long_weight"]  # 0.5
    ohlcv_count = mom_cfg["ohlcv_count"]  # 80

    today_str = datetime.now().strftime("%Y-%m-%d")
    log.info(f"=== ETF 모멘텀 스크리닝 시작 [{today_str}] ===")

    api = KISApi()
    etf_list = strategy["etfs"]
    scores = []

    log.info(f"{'ETF명':<28} {'20일수익':>8} {'60일수익':>8} {'스코어':>8} {'MA60':>8} {'현재가':>8} 판정")
    log.info("-" * 85)

    for etf in etf_list:
        code = etf["code"]
        name = etf["name"]

        try:
            ohlcv = api.get_ohlcv(code, period="D", count=ohlcv_count)
            if len(ohlcv) < long_days + 1:
                log.warning(f"  {name}({code}) 데이터 부족 ({len(ohlcv)}일) → 스킵")
                continue

            # ohlcv[0]=최신, ohlcv[-1]=가장 오래된 (역순)
            close_today  = float(ohlcv[0].get("stck_clpr", 0))
            close_20d    = float(ohlcv[short_days].get("stck_clpr", 0))
            close_60d    = float(ohlcv[long_days].get("stck_clpr", 0))

            if close_today <= 0 or close_20d <= 0 or close_60d <= 0:
                log.warning(f"  {name}({code}) 종가 0 → 스킵")
                continue

            ret_20d = close_today / close_20d - 1
            ret_60d = close_today / close_60d - 1
            score   = sw * ret_20d + lw * ret_60d

            # 60일 이동평균
            closes_60 = [float(ohlcv[i].get("stck_clpr", 0)) for i in range(long_days)]
            ma60 = sum(closes_60) / long_days if all(c > 0 for c in closes_60) else 0

            # 필터 조건
            pass_ma60   = close_today > ma60 if flt_cfg.get("require_above_ma60", True) else True
            pass_60d    = ret_60d > 0        if flt_cfg.get("require_positive_60d_return", True) else True
            passed      = pass_ma60 and pass_60d

            flag_ma  = "O" if pass_ma60 else "XMA"
            flag_60d = "O" if pass_60d  else "X60d"
            verdict  = "통과" if passed else f"제외({flag_ma},{flag_60d})"

            log.info(
                f"  {name:<26} {ret_20d:>+7.2%} {ret_60d:>+7.2%} {score:>+7.4f} "
                f"{ma60:>8,.0f} {close_today:>8,.0f} {verdict}"
            )

            scores.append({
                "code":      code,
                "name":      name,
                "close":     close_today,
                "ret_20d":   round(ret_20d, 6),
                "ret_60d":   round(ret_60d, 6),
                "score":     round(score, 6),
                "ma60":      round(ma60, 2),
                "pass_ma60": pass_ma60,
                "pass_60d":  pass_60d,
                "passed":    passed,
            })

        except Exception as e:
            log.error(f"  {name}({code}) 오류: {e}")

    log.info("-" * 85)

    # 스코어 내림차순 정렬
    scores.sort(key=lambda x: -x["score"])

    # 필터 통과한 것 중 최고 스코어 선정
    passed_list = [s for s in scores if s["passed"]]
    if passed_list:
        selected = passed_list[0]
        action   = "buy"
        log.info(f"[선정] {selected['name']}({selected['code']})  스코어={selected['score']:+.4f}")
    else:
        selected = None
        action   = "cash"
        log.info("[결과] 필터 통과 ETF 없음 -> 현금 보유")

    # 시그널 저장
    signal_path = os.path.join(_BASE, f"etf_momentum_signal_{_MODE}.json")
    signal = {
        "screened_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
        "screen_date":  today_str,
        "action":       action,
        "selected_etf": {"code": selected["code"], "name": selected["name"]} if selected else None,
        "executed":     False,
        "scores":       scores,
    }
    with open(signal_path, "w", encoding="utf-8") as f:
        json.dump(signal, f, ensure_ascii=False, indent=2)
    log.info(f"시그널 저장: {signal_path}")

    # 텔레그램 알림
    lines = [f"📊 <b>[ETF 모멘텀] 주간 스크리닝 {today_str}</b>\n"]
    lines.append(f"{'ETF':<22} {'20일':>7} {'60일':>7} {'스코어':>8}  판정")
    lines.append("─" * 52)
    for s in scores:
        mark = "[O]" if s["passed"] else "[X]"
        sel  = " ◀ 선정" if selected and s["code"] == selected["code"] else ""
        lines.append(
            f"{mark} {s['name']:<20} {s['ret_20d']:>+6.1%} {s['ret_60d']:>+6.1%} "
            f"{s['score']:>+7.4f}{sel}"
        )
    lines.append("")
    if selected:
        lines.append(f"<b>선정: {selected['name']} → 다음 거래일 시초가 매수</b>")
    else:
        lines.append("<b>선정 없음 → 현금 보유</b>")

    send("\n".join(lines))
    log.info("=== ETF 모멘텀 스크리닝 완료 ===")

    return signal


if __name__ == "__main__":
    run()
