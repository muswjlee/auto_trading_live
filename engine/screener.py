"""종목 스크리닝 — 거래량 유니버스 또는 리서치 유니버스 지원"""

import time
import logging
from kis_api import KISApi
from engine.indicators import rsi as calc_rsi, macd as calc_macd
from engine.research import get_today_research

log = logging.getLogger(__name__)

_ETF_PREFIXES = (
    "KODEX", "TIGER", "ARIRANG", "KBSTAR", "HANARO", "KOSEF",
    "KINDEX", "ACE", "SOL", "FOCUS", "SMART", "TIMEFOLIO",
    "TREX", "PLUS", "NEWTON", "RISE", "WOORI ETF",
)

def is_etf(name: str) -> bool:
    upper = name.upper()
    if "ETN" in upper:
        return True
    return any(upper.startswith(p) for p in _ETF_PREFIXES)


def get_execution_strength(api: KISApi, stock_code: str) -> float:
    return api.get_execution_strength(stock_code)


def is_upper_limit(price_info: dict) -> bool:
    current = int(price_info.get("stck_prpr", 0))
    upper   = int(price_info.get("stck_mxpr", 0))
    return upper > 0 and current >= upper


# ── 유니버스 빌더 ──────────────────────────────────────────────────────────

def _build_volume_candidates(api: KISApi, univ: dict, entry: dict) -> list[dict]:
    """거래량 상위 종목 조회 후 등락률 필터"""
    log.info("거래량 상위 종목 조회 중...")
    volume_stocks = api.get_volume_rank(
        top_n=univ["top_volume"],
        market=univ["market"],
        min_price=univ["min_price"],
        max_price=univ["max_price"],
    )
    min_change  = entry["min_change_rate"]
    max_change  = entry.get("max_change_rate", 100.0)
    exclude_etf = univ.get("exclude_etf", False)

    candidates = []
    for s in volume_stocks:
        if not (min_change <= s["change_rate"] <= max_change):
            continue
        if exclude_etf and is_etf(s["name"]):
            log.info(f"  {s['name']} ETF → 제외")
            continue
        candidates.append(s)
    log.info(f"등락률 {min_change}% ~ {max_change}% 필터 후: {len(candidates)}개")
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

    result = []
    for s in candidates:
        try:
            price_info   = api.get_price(s["code"])
            strength     = get_execution_strength(api, s["code"])
            min_strength = entry.get("min_execution_strength")
            if min_strength is not None and strength < min_strength:
                log.info(f"  {s['name']} 체결강도 {strength} < {min_strength} → 제외")
                continue
            at_limit = is_upper_limit(price_info)

            vwap = float(price_info.get("wghn_avrg_stck_prc", 0) or 0)
            vwap_ratio = round(int(price_info["stck_prpr"]) / vwap * 100, 2) if vwap > 0 else 0.0

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
                "price":              int(price_info["stck_prpr"]),
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
