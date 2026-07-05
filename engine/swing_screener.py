"""스윙 전략 야간 스크리닝 (매일 21:00 실행 권장)

선별 조건:
  유니버스: 시가총액 3,000억↑, ETF·관리종목 제외, 5일 평균 거래대금 1,000억↑
  신고가:   당일 종가 > 직전 60거래일 최고 종가 (60일 신고가 돌파)
  거래대금: ① 당일/20일평균 > 2  ② 5일평균/20일평균 > 1.5  ③ 당일/전일5일평균 > 1.2
  투자자:  최근 5일 외국인 누적 순매수 > 0  AND  기관 누적 순매수 > 0

결과: swing_candidates_{MODE}.json 저장 + 텔레그램 알림
사용: python engine/swing_screener.py [strategy_id]
      strategy_id 생략 시 swing_breakout 사용
"""

import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests as _req

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kis_api import KISApi
from engine.notifier import send

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE = os.getenv("KIS_MODE", "live")

_ETF_KW = [
    "ETF", "레버리지", "인버스", "KODEX", "TIGER", "KBSTAR", "HANARO",
    "SOL", "ACE", "RISE", "PLUS", "KoAct", "TIME", "KOSEF", "ARIRANG",
    "파생", "스팩", "리츠",
]

log = logging.getLogger("swing_screener")


def _setup_logger():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.FileHandler(
            os.path.join(_BASE, f"swing_screener_{_MODE}.log"),
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)


def _load_strategy(strategy_id: str) -> dict:
    path = os.path.join(_BASE, "strategies", f"{strategy_id}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _is_etf(name: str) -> bool:
    return any(k in name for k in _ETF_KW)


def _get_all_naver_codes() -> list[dict]:
    """네이버 금융 시가총액 순위에서 KOSPI+KOSDAQ 전종목 코드·이름 수집"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    seen = {}
    for market in ["KOSPI", "KOSDAQ"]:
        url = f"https://finance.naver.com/sise/sise_market_sum.nhn?market={market}&page={{}}"
        for page in range(1, 120):
            try:
                r = _req.get(url.format(page), headers=headers, timeout=10)
                pairs = re.findall(
                    r'<a href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a>',
                    r.text,
                )
                new_found = False
                for code, name in pairs:
                    name = name.strip()
                    if code not in seen and not _is_etf(name):
                        seen[code] = name
                        new_found = True
                if not new_found:
                    break
                time.sleep(0.08)
            except Exception as e:
                log.warning(f"네이버 전종목 수집 오류 (page={page}): {e}")
                time.sleep(0.5)
    return [{"code": code, "name": name} for code, name in seen.items()]


def _get_amount(candle: dict) -> int:
    return int(candle.get("acml_tr_pbmn", 0))


def _check_limitup(api: KISApi, code: str, name: str, strategy: dict) -> dict | None:
    """상한가 전략 전용 체크 — get_price() 기반으로 실제 시장 등락률 사용"""
    uni = strategy.get("universe", {})
    scr = strategy.get("screen", {})
    limitup_pct = scr.get("limitup_pct", 29.0)

    try:
        price_info  = api.get_price(code)
        change_rate = float(price_info.get("prdy_ctrt", 0))
        today_close = int(price_info.get("stck_prpr", 0))
        today_amount = int(price_info.get("acml_tr_pbmn", 0))
        market_cap  = int(price_info.get("hts_avls", 0))

        if today_close <= 0:
            return None

        if change_rate < limitup_pct:
            log.info(f"  {name}({code}) 등락률 {change_rate:.1f}% < {limitup_pct}% → 상한가 아님")
            return None

        min_cap = uni.get("min_market_cap_억", 0)
        if min_cap and market_cap < min_cap:
            log.info(f"  {name}({code}) 시가총액 {market_cap:,}억 < {min_cap:,}억")
            return None

        if uni.get("exclude_managed", True):
            if price_info.get("mrkt_warn_cls_code", "00") != "00":
                log.info(f"  {name}({code}) 관리·경고 종목")
                return None

        log.info(
            f"  ✅ {name}({code}) 상한가 {change_rate:.1f}% 종가={today_close:,} "
            f"시총={market_cap:,}억 거래대금={today_amount//100_000_000:,}억"
        )
        return {
            "code": code,
            "name": name,
            "close": today_close,
            "change_rate": round(change_rate, 2),
            "market_cap_억": market_cap,
            "today_amount_억": today_amount // 100_000_000,
        }

    except Exception as e:
        log.warning(f"  {name}({code}) 오류: {e}")
        return None


def _check_stock(api: KISApi, code: str, name: str, strategy: dict) -> dict | None:
    uni = strategy.get("universe", {})
    scr = strategy.get("screen", {})

    # 상한가 전략 분기
    if "limitup_pct" in scr:
        return _check_limitup(api, code, name, strategy)

    try:
        # OHLCV: 당일(index 0) + 이전 60일 → 최소 61개 필요
        high_days = scr.get("high_days", 60)
        needed = high_days + 1
        ohlcv = api.get_ohlcv(code, period="D", count=needed)
        if len(ohlcv) < max(22, needed):
            return None

        today_d   = ohlcv[0]
        today_close  = int(today_d.get("stck_clpr", 0))
        today_amount = _get_amount(today_d)
        if today_close <= 0 or today_amount <= 0:
            return None

        # ── 60일 고점 조건 (돌파 or 근접 범위) ───────────────────────────
        prior = ohlcv[1:needed]
        max_60d = max(int(d.get("stck_clpr", 0)) for d in prior if int(d.get("stck_clpr", 0)) > 0)

        ratio_min = scr.get("high_ratio_min")
        ratio_max = scr.get("high_ratio_max")

        if ratio_min is not None and ratio_max is not None:
            # 범위 조건: 60일 고점의 ratio_min ~ ratio_max 사이
            ratio = today_close / max_60d if max_60d > 0 else 0
            if not (ratio_min <= ratio <= ratio_max):
                log.info(
                    f"  {name}({code}) 60일 고점 비율 {ratio:.2%} "
                    f"(기준 {ratio_min:.0%}~{ratio_max:.0%}, 60일고점={max_60d:,})"
                )
                return None
        else:
            # 기본: 돌파 조건
            if today_close <= max_60d:
                log.info(f"  {name}({code}) 60일 신고가 미돌파 {today_close:,} <= {max_60d:,}")
                return None

        # ── 거래대금 평균 계산 ─────────────────────────────────────────────
        prior_20  = ohlcv[1:21]   # 이전 20일 (당일 제외)
        prior_5   = ohlcv[1:6]    # 이전 5일 (당일 제외)
        cur_5     = ohlcv[0:5]    # 당일 포함 5일

        avg_20d    = sum(_get_amount(d) for d in prior_20) / len(prior_20) if prior_20 else 0
        avg_5d     = sum(_get_amount(d) for d in cur_5)   / len(cur_5)    if cur_5    else 0
        avg_prev5d = sum(_get_amount(d) for d in prior_5) / len(prior_5)  if prior_5  else 0

        if avg_20d <= 0 or avg_prev5d <= 0:
            return None

        # ── 5일 평균 거래대금 1,000억 이상 (유니버스 필터) ─────────────────
        min_5d_amt = uni.get("min_5d_avg_amount_억", 1000) * 100_000_000
        if avg_5d < min_5d_amt:
            log.info(f"  {name}({code}) 5일 평균 거래대금 {avg_5d/1e8:.0f}억 < {uni.get('min_5d_avg_amount_억',1000)}억")
            return None

        # ── 거래대금 비율 3가지 ────────────────────────────────────────────
        r1 = today_amount / avg_20d
        r2 = avg_5d / avg_20d
        r3 = today_amount / avg_prev5d

        thr1 = scr.get("vol_ratio_today_vs_20d",   2.0)
        thr2 = scr.get("vol_ratio_5d_vs_20d",       1.5)
        thr3 = scr.get("vol_ratio_today_vs_prev5d", 1.2)

        if r1 <= thr1:
            log.info(f"  {name}({code}) 당일/20일 {r1:.2f} <= {thr1}")
            return None
        if r2 <= thr2:
            log.info(f"  {name}({code}) 5일/20일 {r2:.2f} <= {thr2}")
            return None
        if r3 <= thr3:
            log.info(f"  {name}({code}) 당일/전일5일 {r3:.2f} <= {thr3}")
            return None

        # ── 시가총액·관리종목 체크 (get_price 1회 호출) ────────────────────
        price_info = api.get_price(code)
        market_cap = int(price_info.get("hts_avls", 0))
        min_cap    = uni.get("min_market_cap_억", 3000)
        if market_cap < min_cap:
            log.info(f"  {name}({code}) 시가총액 {market_cap:,}억 < {min_cap:,}억")
            return None

        if uni.get("exclude_managed", True):
            mrkt_warn = price_info.get("mrkt_warn_cls_code", "00")
            if mrkt_warn != "00":
                log.info(f"  {name}({code}) 관리·경고 종목 (mrkt_warn={mrkt_warn})")
                return None

        # ── 외국인/기관 순매수 ─────────────────────────────────────────────
        req_frgn = scr.get("require_foreign_net_buy_5d", True)
        req_inst = scr.get("require_inst_net_buy_5d",    True)

        if req_frgn or req_inst:
            investor = api.get_investor_trend(code, days=5)
            if req_frgn:
                frgn_sum = sum(int(d.get("frgn_ntby_qty", 0)) for d in investor)
                if frgn_sum <= 0:
                    log.info(f"  {name}({code}) 외국인 5일 누적 순매수 {frgn_sum:,} <= 0")
                    return None
            if req_inst:
                inst_sum = sum(int(d.get("orgn_ntby_qty", 0)) for d in investor)
                if inst_sum <= 0:
                    log.info(f"  {name}({code}) 기관 5일 누적 순매수 {inst_sum:,} <= 0")
                    return None
        else:
            frgn_sum = inst_sum = 0

        log.info(
            f"  ✅ {name}({code}) 종가={today_close:,} 시총={market_cap:,}억 "
            f"거래대금비율=({r1:.1f}/{r2:.1f}/{r3:.1f}) "
            f"외인={frgn_sum:,} 기관={inst_sum:,}"
        )
        return {
            "code": code,
            "name": name,
            "close": today_close,
            "market_cap_억": market_cap,
            "high_60d": max_60d,
            "high_60d_ratio": round(today_close / max_60d, 4) if max_60d > 0 else None,
            "today_amount_억": today_amount // 100_000_000,
            "avg_5d_amount_억": int(avg_5d)  // 100_000_000,
            "avg_20d_amount_억": int(avg_20d) // 100_000_000,
            "vol_ratio_today_vs_20d":   round(r1, 2),
            "vol_ratio_5d_vs_20d":      round(r2, 2),
            "vol_ratio_today_vs_prev5d": round(r3, 2),
            "frgn_net_5d": frgn_sum,
            "inst_net_5d": inst_sum,
        }

    except Exception as e:
        log.warning(f"  {name}({code}) 오류: {e}")
        return None


def run(strategy_id: str = "swing_breakout"):
    _setup_logger()
    strategy = _load_strategy(strategy_id)
    uni = strategy.get("universe", {})
    log.info(f"=== 스윙 스크리닝 시작 [{strategy['name']}] ===")

    api = KISApi()

    # 유니버스 구성
    if uni.get("scan_all", False):
        log.info("전종목 스캔 모드 — 네이버 금융 전종목 수집 중...")
        candidates = _get_all_naver_codes()
        log.info(f"전종목 수집 완료: {len(candidates)}개 (ETF 제외)")
    else:
        top_n = uni.get("top_turnover", 500)
        log.info(f"거래대금 상위 {top_n} 종목 수집 중...")
        raw = api.get_volume_rank(top_n=top_n, market="0", min_price=1000, max_price=0, by_turnover=True)
        candidates = [
            {"code": s["code"], "name": s["name"]}
            for s in raw
            if s["code"] and not _is_etf(s["name"])
        ]
        log.info(f"ETF 제외 후 {len(candidates)}개 대상 종목")
        if len(candidates) == 0:
            msg = (
                f"⚠️ <b>[스윙 스크리닝 실패] {strategy['name']}</b>\n"
                f"Universe 0개 수집 — API 오류 또는 점검 시간 가능성\n"
                f"종목 수동 확인 필요"
            )
            send(msg)
            log.error("Universe 0개 — 스크리닝 중단 (API 오류 가능성)")
            return []

    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_check_stock, api, s["code"], s["name"], strategy): s for s in candidates}
        done = 0
        for f in as_completed(futs):
            done += 1
            r = f.result()
            if r:
                results.append(r)
            if done % 50 == 0:
                log.info(f"  진행 {done}/{len(candidates)} — 통과 {len(results)}개")

    results.sort(key=lambda x: -x["today_amount_억"])

    # 저장
    candidates_file = os.path.join(_BASE, f"swing_candidates_{strategy_id}_{_MODE}.json")
    data = {
        "strategy_id": strategy_id,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "screened_at": datetime.now().strftime("%H:%M"),
        "count": len(results),
        "stocks": results,
    }
    with open(candidates_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"결과 저장: {candidates_file} ({len(results)}개)")

    # 텔레그램 알림
    if results:
        lines = [f"📊 <b>[스윙 스크리닝] {strategy['name']} {data['date']} — {len(results)}개 선별</b>\n"]
        for r in results[:10]:
            if r.get("change_rate") and not r.get("vol_ratio_today_vs_20d"):
                # 상한가 전략
                lines.append(
                    f"• {r['name']} ({r['code']}): {r['close']:,}원 "
                    f"| 상한가 {r['change_rate']:.1f}% "
                    f"| 시총 {r['market_cap_억']:,}억 "
                    f"| 거래대금 {r['today_amount_억']:,}억"
                )
            else:
                ratio_str = f" | 60일고점比 {r['high_60d_ratio']:.1%}" if r.get("high_60d_ratio") else ""
                lines.append(
                    f"• {r['name']} ({r['code']}): {r['close']:,}원 "
                    f"| 시총 {r['market_cap_억']:,}억 "
                    f"| 거래대금비율 {r['vol_ratio_today_vs_20d']:.1f}x"
                    f"{ratio_str}"
                )
        if len(results) > 10:
            lines.append(f"... 외 {len(results)-10}개")
        lines.append("\n※ 내일 09:03 시가 대비 + 인 경우 매수")
        send("\n".join(lines))
    else:
        send(f"📊 <b>[스윙 스크리닝] {strategy['name']} {data['date']}]</b>\n조건 충족 종목 없음")

    log.info(f"=== 스크리닝 완료: {len(results)}개 선별 ===")
    return results


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "swing_breakout"
    run(sid)
