"""종목 스크리닝 — 거래량 유니버스 또는 리서치 유니버스 지원"""

import time
import logging
from kis_api import KISApi
from engine.indicators import rsi as calc_rsi, macd as calc_macd
from engine.research import get_today_research

log = logging.getLogger(__name__)

_KODEX200 = "069500"  # KOSPI 방향 프록시
_kodex_cache: list[tuple[float, float]] = []   # (epoch_sec, price)
_CACHE_MAX_AGE = 900                            # 15분 초과 항목 제거


def update_kodex_cache(api: KISApi):
    """KODEX200 현재가를 캐시에 추가 — runner 메인 루프에서 60초마다 호출"""
    try:
        info  = api.get_price(_KODEX200)
        price = float(info["stck_prpr"])
        now   = time.time()
        _kodex_cache.append((now, price))
        cutoff = now - _CACHE_MAX_AGE
        while _kodex_cache and _kodex_cache[0][0] < cutoff:
            _kodex_cache.pop(0)
    except Exception:
        pass


def is_market_rising(minutes: int = 3) -> bool:
    """
    KODEX200 가격 캐시 기준 직전 N분 추세 확인
    최신 캐시 가격 >= N분 전 캐시 가격이면 True (상승/보합)
    캐시 부족 또는 N분 전 데이터 없으면 True 반환 (필터 미적용)
    """
    if len(_kodex_cache) < 2:
        log.warning("KODEX200 캐시 부족 → 필터 미적용")
        return True

    now_ts, current = _kodex_cache[-1]
    target    = now_ts - minutes * 60
    tolerance = 90  # ±90초 허용

    candidates = [(abs(t - target), p) for t, p in _kodex_cache[:-1]]
    diff, prev = min(candidates, key=lambda x: x[0])

    if diff > tolerance:
        log.warning(f"KODEX200 {minutes}분 전 캐시 없음 ({diff:.0f}초 차이) → 필터 미적용")
        return True

    rising = current >= prev
    log.info(f"[지수 방향] KODEX200 {minutes}분 전 {prev:,.0f} → 현재 {current:,.0f} ({'상승' if rising else '하락'})")
    return rising


_ETF_PREFIXES = (
    "KODEX", "TIGER", "ARIRANG", "KBSTAR", "HANARO", "KOSEF",
    "KINDEX", "ACE", "SOL", "FOCUS", "SMART", "TIMEFOLIO",
    "TREX", "PLUS", "NEWTON", "RISE", "WOORI ETF", "KIWOOM",
)

def is_etn(name: str) -> bool:
    return "ETN" in name.upper()

def is_etf(name: str) -> bool:
    return any(name.upper().startswith(p) for p in _ETF_PREFIXES)

def is_inverse(name: str) -> bool:
    return "인버스" in name

def is_derivatives_etf(name: str) -> bool:
    """파생ETF 여부 — 단일종목레버리지·선물단일종목 등 파생상품 기반 ETF"""
    n = name
    return "단일종목레버리지" in n or "선물단일종목" in n or "선물레버리지" in n

# 시스템 외 개인 보유 종목 — 매수 대상에서 영구 제외
_PERSONAL_HOLDINGS = {"005935", "491700", "0162Z0"}


def get_execution_strength(api: KISApi, stock_code: str, instant: bool = False) -> float:
    if instant:
        return api.get_instant_execution_strength(stock_code)
    return api.get_execution_strength(stock_code)


def is_upper_limit(price_info: dict) -> bool:
    current = int(price_info.get("stck_prpr", 0))
    upper   = int(price_info.get("stck_mxpr", 0))
    return upper > 0 and current >= upper


# ── 유니버스 빌더 ──────────────────────────────────────────────────────────

_universe_cache: dict[str, tuple[float, list[dict]]] = {}  # key → (ts, stocks)
_UNIVERSE_TTL = 60  # 거래대금 universe는 60초 캐시

def _build_volume_candidates(api: KISApi, univ: dict, entry: dict) -> list[dict]:
    """거래량/거래대금 상위 종목 조회 후 등락률 필터 (universe 60초 캐시)"""
    by_turnover = "top_turnover" in univ
    top_n       = univ.get("top_turnover") or univ.get("top_volume", 100)
    label       = "거래대금" if by_turnover else "거래량"

    cache_key = f"{by_turnover}:{top_n}:{univ.get('market','0')}:{univ.get('min_price',0)}:{univ.get('max_price',0)}"
    now = time.time()
    if cache_key in _universe_cache:
        ts, cached = _universe_cache[cache_key]
        if now - ts < _UNIVERSE_TTL:
            log.info(f"{label} 상위 {top_n} 종목 캐시 사용 ({int(now-ts)}초 전)")
            volume_stocks = cached
        else:
            volume_stocks = None
    else:
        volume_stocks = None

    if volume_stocks is None:
        log.info(f"{label} 상위 {top_n} 종목 조회 중...")
        volume_stocks = api.get_volume_rank(
            top_n=top_n,
            market=univ.get("market", "0"),
            min_price=univ.get("min_price", 0),
            max_price=univ.get("max_price", 0),
            by_turnover=by_turnover,
        )
        _universe_cache[cache_key] = (now, volume_stocks)
    min_change  = entry.get("min_change_rate")
    max_change  = entry.get("max_change_rate")
    exclude_etf = univ.get("exclude_etf", False)

    candidates = []
    for s in volume_stocks:
        if s["code"] in _PERSONAL_HOLDINGS:
            log.info(f"  {s['name']}({s['code']}) 개인 보유 종목 → 제외")
            continue
        if min_change is not None and s["change_rate"] < min_change:
            continue
        if max_change is not None and s["change_rate"] > max_change:
            continue
        if is_etn(s["name"]):
            log.info(f"  {s['name']} ETN → 제외")
            continue
        if is_inverse(s["name"]):
            log.info(f"  {s['name']} 인버스 → 제외")
            continue
        if is_derivatives_etf(s["name"]):
            log.info(f"  {s['name']} 파생ETF → 제외")
            continue
        if exclude_etf and is_etf(s["name"]):
            log.info(f"  {s['name']} ETF → 제외")
            continue
        candidates.append(s)
    log.info(f"등락률 필터 후: {len(candidates)}개")
    return candidates


def _build_research_candidates(api: KISApi, univ: dict, entry: dict) -> list[dict]:
    """네이버 리서치 보고서 종목 조회 후 등락률·가격 필터"""
    reports = get_today_research(max_pages=univ.get("max_pages", 3))
    if not reports:
        return []

    min_price  = univ.get("min_price", 0)
    max_price  = univ.get("max_price", 0)
    min_change = entry["min_change_rate"]
    max_change = entry.get("max_change_rate", 100.0)

    candidates = []
    for r in reports:
        try:
            price_info  = api.get_price(r["code"])
            price       = int(price_info["stck_prpr"])
            change_rate = float(price_info.get("prdy_ctrt", 0))

            if max_price and not (min_price <= price <= max_price):
                log.info(f"  {r['name']} 가격 {price:,}원 범위 벗어남 → 제외")
                continue
            if not (min_change <= change_rate <= max_change):
                log.info(f"  {r['name']} 등락률 {change_rate}% → 제외")
                continue

            candidates.append({
                "code":        r["code"],
                "name":        r["name"],
                "price":       price,
                "change_rate": change_rate,
                "firm":        r["firm"],
            })
            log.info(f"  {r['name']}({r['code']}) {r['firm']} 등락률 {change_rate}%")
            time.sleep(0.1)
        except Exception as e:
            log.warning(f"  {r['code']} 조회 오류: {e}")

    log.info(f"리서치 후보: {len(candidates)}개")
    return candidates


# ── 메인 스크리닝 ─────────────────────────────────────────────────────────

def screen(api: KISApi, strategy: dict) -> list[dict]:
    """
    전략 조건에 따라 종목 스크리닝
    반환: [{"code", "name", "price", "change_rate", "execution_strength", ...}]
    """
    univ  = strategy["universe"]
    entry = strategy["entry"]

    if univ.get("type") == "research":
        candidates = _build_research_candidates(api, univ, entry)
    else:
        candidates = _build_volume_candidates(api, univ, entry)

    if not candidates:
        return []

    min_per          = entry.get("min_per")
    max_per          = entry.get("max_per")
    min_pbr          = entry.get("min_pbr")
    max_pbr          = entry.get("max_pbr")
    use_fundamental  = any(v is not None for v in [min_per, max_per, min_pbr, max_pbr])
    indicator_cfg    = entry.get("indicators")
    use_indicators   = indicator_cfg is not None
    min_volume_ratio = entry.get("min_volume_ratio")

    instant_strength = entry.get("instant_execution_strength", False)

    result = []
    for s in candidates:
        try:
            price_info   = api.get_price(s["code"])
            current_price = int(price_info["stck_prpr"])

            strength     = get_execution_strength(api, s["code"], instant=instant_strength)
            min_strength = entry.get("min_execution_strength")
            if min_strength is not None and strength < min_strength:
                log.info(f"  {s['name']} 체결강도 {strength} < {min_strength} → 제외")
                continue

            # 누적 체결강도 (min_accumulated_execution_strength)
            min_acc_strength = entry.get("min_accumulated_execution_strength")
            if min_acc_strength is not None:
                acc = strength if not instant_strength else api.get_execution_strength(s["code"])
                if acc < min_acc_strength:
                    log.info(f"  {s['name']} 누적 체결강도 {acc} < {min_acc_strength} → 제외")
                    continue

            at_limit = is_upper_limit(price_info)

            vwap = float(price_info.get("wghn_avrg_stck_prc", 0) or 0)
            vwap_ratio = round(current_price / vwap * 100, 2) if vwap > 0 else 0.0

            # 현재가 > VWAP 조건
            if entry.get("require_above_vwap") and vwap > 0:
                if current_price <= vwap:
                    log.info(f"  {s['name']} 현재가 {current_price:,} ≤ VWAP {vwap:,.0f} → 제외")
                    continue

            # 당일 고가 근접 조건
            near_high_pct = entry.get("near_day_high_pct")
            if near_high_pct is not None:
                day_high = int(price_info.get("stck_hgpr", 0))
                if day_high > 0:
                    threshold = day_high * (1 - near_high_pct / 100)
                    if current_price < threshold:
                        log.info(f"  {s['name']} 현재가 {current_price:,} < 당일고가 {day_high:,}의 {100 - near_high_pct:.1f}% → 제외")
                        continue

            # 30초 순간 체결강도
            min_inst_30s = entry.get("instant_execution_strength_30s")
            if min_inst_30s is not None:
                inst_30s = api.get_instant_execution_strength(s["code"], window_sec=30)
                if inst_30s < min_inst_30s:
                    log.info(f"  {s['name']} 30초 체결강도 {inst_30s:.1f} < {min_inst_30s} → 제외")
                    continue
                log.info(f"  {s['name']} 30초 체결강도 {inst_30s:.1f} ✓")

            # 1분 거래대금 / 최근 5분 평균 비율
            min_1min_ratio = entry.get("min_1min_turnover_ratio")
            if min_1min_ratio is not None:
                try:
                    candles = api.get_minute_candles(s["code"], count=6)
                    if len(candles) >= 2:
                        recent_tv = int(candles[0].get("cntg_vol", 0)) * int(candles[0].get("stck_prpr", 0) or current_price)
                        prev_tvs  = [
                            int(c.get("cntg_vol", 0)) * int(c.get("stck_prpr", 0) or current_price)
                            for c in candles[1:]
                        ]
                        avg_tv = sum(prev_tvs) / len(prev_tvs) if prev_tvs else 0
                        if avg_tv > 0:
                            tv_ratio = recent_tv / avg_tv
                            if tv_ratio < min_1min_ratio:
                                log.info(f"  {s['name']} 1분 거래대금 배율 {tv_ratio:.1f} < {min_1min_ratio} → 제외")
                                continue
                            log.info(f"  {s['name']} 1분 거래대금 배율 {tv_ratio:.1f} ✓")
                except Exception as ce:
                    log.warning(f"  {s['name']} 분봉 조회 실패 → 조건 미적용: {ce}")

            min_vwap_ratio = entry.get("min_vwap_ratio")
            if min_vwap_ratio is not None and vwap_ratio > 0:
                if vwap_ratio < min_vwap_ratio:
                    log.info(f"  {s['name']} 현재가/VWAP {vwap_ratio:.1f} < {min_vwap_ratio} → 제외")
                    continue
                log.info(f"  {s['name']} 현재가/VWAP {vwap_ratio:.1f} ✓")

            per, pbr   = None, None

            if min_volume_ratio is not None:
                acml_vol = int(price_info.get("acml_vol", 0))
                prdy_vol = int(price_info.get("prdy_vol", 0))
                if prdy_vol <= 0:
                    # 전일이 휴장인 경우 → OHLCV에서 가장 최근 개장일 거래량 사용
                    try:
                        ohlcv = api.get_ohlcv(s["code"], count=5)
                        prdy_vol = next(
                            (int(o.get("acml_vol", 0)) for o in ohlcv if int(o.get("acml_vol", 0)) > 0),
                            0,
                        )
                    except Exception:
                        prdy_vol = 0
                if prdy_vol <= 0:
                    log.info(f"  {s['name']} 최근 거래량 없음 → 제외")
                    continue
                vol_ratio = acml_vol / prdy_vol
                if vol_ratio < min_volume_ratio:
                    log.info(f"  {s['name']} 거래량비율 {vol_ratio:.1%} < {min_volume_ratio:.1%} → 제외")
                    continue
                log.info(f"  {s['name']} 거래량비율 {vol_ratio:.1%} ✓")

            if use_fundamental:
                per = float(price_info.get("per", 0) or 0)
                pbr = float(price_info.get("pbr", 0) or 0)
                if min_per is not None and per < min_per:
                    log.info(f"  {s['name']} PER {per} < {min_per} → 제외")
                    continue
                if max_per is not None and per > max_per:
                    log.info(f"  {s['name']} PER {per} > {max_per} → 제외")
                    continue
                if min_pbr is not None and pbr < min_pbr:
                    log.info(f"  {s['name']} PBR {pbr} < {min_pbr} → 제외")
                    continue
                if max_pbr is not None and pbr > max_pbr:
                    log.info(f"  {s['name']} PBR {pbr} > {max_pbr} → 제외")
                    continue

            rsi_val, macd_cross = None, None
            if use_indicators:
                candles = api.get_ohlcv(s["code"], count=40)
                closes  = [float(c["stck_clpr"]) for c in candles]
                closes.reverse()

                rsi_cfg  = indicator_cfg.get("rsi", {})
                macd_cfg = indicator_cfg.get("macd", {})
                rsi_val  = calc_rsi(closes, period=rsi_cfg.get("period", 14))
                macd_result = calc_macd(
                    closes,
                    fast=macd_cfg.get("fast", 12),
                    slow=macd_cfg.get("slow", 26),
                    signal=macd_cfg.get("signal", 9),
                )
                macd_cross = macd_result["golden_cross"]

                oversold = rsi_cfg.get("oversold", 35)
                if rsi_val >= oversold:
                    log.info(f"  {s['name']} RSI {rsi_val} >= {oversold} → 제외")
                    continue
                if not macd_cross:
                    log.info(f"  {s['name']} MACD 골든크로스 미충족 → 제외")
                    continue
                time.sleep(0.1)

            result.append({
                "code":               s["code"],
                "name":               s["name"],
                "price":              current_price,
                "change_rate":        s.get("change_rate", 0),
                "execution_strength": strength,
                "vwap_ratio":         vwap_ratio,
                "at_upper_limit":     at_limit,
                "per":                per,
                "pbr":                pbr,
                "rsi":                rsi_val,
                "macd_cross":         macd_cross,
            })
            log.info(
                f"  {s['name']}({s['code']}) 등락률 {s.get('change_rate', 0)}%"
                f" 체결강도 {strength} VWAP비율 {vwap_ratio:.1f}"
                + (f" PER {per}" if per else "")
                + (f" PBR {pbr}" if pbr else "")
                + (f" RSI {rsi_val}" if rsi_val is not None else "")
                + (" MACD_GC" if macd_cross else "")
                + (" [상한가]" if at_limit else "")
            )
            time.sleep(0.1)
        except Exception as e:
            log.warning(f"  {s['code']} 조회 오류: {e}")

    result.sort(key=lambda x: x["execution_strength"], reverse=True)

    top_n    = entry["max_stocks"]
    selected = []
    skipped  = []
    for stock in result:
        if len(selected) >= top_n:
            break
        if stock["at_upper_limit"]:
            log.info(f"  [{stock['name']}] 상한가 → 건너뜀")
            skipped.append(stock["name"])
        else:
            selected.append(stock)

    if skipped:
        from engine.notifier import send
        send(f"상한가 제외: {', '.join(skipped)} → 다음 순위 대체")

    log.info(f"최종 선택 {len(selected)}개: {[s['name'] for s in selected]}")
    return selected
