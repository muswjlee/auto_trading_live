"""외국인·기관 순매수 야간 스크리너 — 21:00 장 마감 후 실행
거래대금 상위 100종목 중 당일 외국인+기관 모두 순매수한 종목 상위 3개 선정
결과: fi_buying_candidates_{MODE}.json
"""

import json
import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kis_api import KISApi

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE = os.getenv("KIS_MODE", "live")
_SID  = "fi_buying_1430"

log = logging.getLogger(_SID + "_screener")


def _setup_logger():
    log_file = os.path.join(_BASE, f"fi_buying_screener_{_MODE}.log")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(fh)
        root.addHandler(sh)


def _load_strategy() -> dict:
    with open(os.path.join(_BASE, "strategies", f"{_SID}.json"), encoding="utf-8") as f:
        return json.load(f)


def _save_candidates(candidates: list):
    path = os.path.join(_BASE, f"fi_buying_candidates_{_MODE}.json")
    data = {
        "screened_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "screen_date": datetime.now().strftime("%Y-%m-%d"),
        "candidates": candidates,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"후보 저장 완료: {len(candidates)}개 → fi_buying_candidates_{_MODE}.json")


def run():
    _setup_logger()
    strategy = _load_strategy()

    if not strategy.get("enabled", False):
        log.info(f"[{_SID}] 비활성화 — 스크리닝 생략")
        return

    api = KISApi()

    uni_cfg        = strategy.get("universe", {})
    top_n_turnover = uni_cfg.get("top_n_turnover", 100)
    min_price      = uni_cfg.get("min_price", 1000)
    max_price      = uni_cfg.get("max_price", 500000)
    top_n          = strategy.get("filter", {}).get("top_n", 3)

    log.info(f"유니버스 조회: 거래대금 상위 {top_n_turnover}종목")
    try:
        universe = api.get_volume_rank(
            top_n=top_n_turnover,
            by_turnover=True,
            min_price=min_price,
            max_price=max_price,
        )
    except Exception as e:
        log.error(f"유니버스 조회 실패: {e}")
        return

    log.info(f"유니버스 {len(universe)}종목 → 투자자 데이터 조회")

    candidates = []
    for item in universe:
        code = item["code"]
        name = item["name"]
        try:
            trend = api.get_investor_trend(code, days=3)
            if not trend:
                time.sleep(0.1)
                continue

            # 유효한 데이터가 있는 첫 번째 행 사용
            row = None
            for r in trend:
                if r.get("frgn_ntby_qty", "") != "" and r.get("orgn_ntby_qty", "") != "":
                    row = r
                    break
            if row is None:
                time.sleep(0.1)
                continue

            frgn_qty = int(row.get("frgn_ntby_qty", 0))
            orgn_qty = int(row.get("orgn_ntby_qty", 0))

            if frgn_qty > 0 and orgn_qty > 0:
                score = frgn_qty + orgn_qty
                candidates.append({
                    "code":     code,
                    "name":     name,
                    "frgn_qty": frgn_qty,
                    "orgn_qty": orgn_qty,
                    "score":    score,
                    "date":     row.get("stck_bsop_date", ""),
                })
                log.info(
                    f"  통과: {name}({code}) "
                    f"외국인={frgn_qty:+,} 기관={orgn_qty:+,} 합계={score:,}"
                )
        except Exception as e:
            log.warning(f"  [{code}] 오류: {e}")
        time.sleep(0.12)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = candidates[:top_n]

    log.info(f"최종 선정: {len(top)}개 / 후보 {len(candidates)}개")
    for i, c in enumerate(top, 1):
        log.info(
            f"  {i}. {c['name']}({c['code']}) "
            f"외국인={c['frgn_qty']:+,} 기관={c['orgn_qty']:+,}"
        )

    _save_candidates(top)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.error(f"스크리닝 오류: {e}", exc_info=True)
