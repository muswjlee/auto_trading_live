"""미국 반도체 선행지표 기반 한국 반도체 데일리 트레이딩 전략

흐름:
  1. 전일 미국장 SOX/NVDA/SMH 수익률 → 2개 이상 조건 충족 시 매매
  2. 한국 반도체 후보 14종목 전일 필터 (거래대금/MA20/거래대금비율/20일수익률)
  3. 09:05 실시간 필터 (현재가>시가, 현재가>전일종가, 갭<7%)
  4. 점수 계산 후 상위 5종목 1주씩 지정가 매수 (최우선매도호가)
  5. 15:20 보유 종목 전량 지정가 청산 (최우선매수호가)
  6. 미체결 30초 대기→1회 정정, 15:25 이후 잔량 시장가
  7. CSV 로그 저장
"""

import csv
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, date

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kis_api import KISApi
from engine.monitor import add_position, remove_position, record_trade, load_positions, reserve_or_skip, cancel_reservation
from engine.notifier import send, notify_buy, notify_sell, notify_error

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE = os.getenv("KIS_MODE", "live")
_SID  = "us_semicon"

log = logging.getLogger(_SID)

BUY_FEE  = 0.000018
SELL_FEE = 0.000018
SELL_TAX = 0.002


# ── 로거 ──────────────────────────────────────────────────────────────────────

def _setup_logger():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        fh = logging.FileHandler(
            os.path.join(_BASE, f"trading_{_SID}_{_MODE}.log"), encoding="utf-8"
        )
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(fh)
        root.addHandler(sh)


# ── 전략 로드 ──────────────────────────────────────────────────────────────────

def _load_strategy() -> dict:
    with open(os.path.join(_BASE, "strategies", "us_semicon.json"), encoding="utf-8") as f:
        return json.load(f)


# ── 미국 선행지표 ─────────────────────────────────────────────────────────────

def _get_us_return(symbol: str) -> float | None:
    """Yahoo Finance에서 미국 주식/지수 전일 수익률 조회 (최근 5거래일 일봉 사용)"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = requests.get(
            url,
            params={"range": "5d", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        result = r.json()["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if len(closes) < 2:
            return None
        ret = closes[-1] / closes[-2] - 1
        return round(ret, 6)
    except Exception as e:
        log.warning(f"Yahoo Finance 조회 실패 ({symbol}): {e}")
        return None


def _check_us_signal(cfg: dict) -> tuple[bool, dict]:
    """SOX/NVDA/SMH 수익률 조회 → 2개 이상 조건 충족 여부 반환"""
    sox_ret  = _get_us_return("^SOX")
    nvda_ret = _get_us_return("NVDA")
    smh_ret  = _get_us_return("SMH")

    sox_pass  = sox_ret  is not None and sox_ret  >= cfg["sox_threshold"]  / 100
    nvda_pass = nvda_ret is not None and nvda_ret >= cfg["nvda_threshold"] / 100
    smh_pass  = smh_ret  is not None and smh_ret  >= cfg["smh_threshold"]  / 100

    passed_count = sum([sox_pass, nvda_pass, smh_pass])
    signal_ok    = passed_count >= cfg["min_conditions"]

    returns = {
        "sox":  sox_ret,  "sox_pass":  sox_pass,
        "nvda": nvda_ret, "nvda_pass": nvda_pass,
        "smh":  smh_ret,  "smh_pass":  smh_pass,
        "passed_count": passed_count,
    }

    log.info(
        f"[US신호] SOX={_pct(sox_ret)}({'O' if sox_pass else 'X'})  "
        f"NVDA={_pct(nvda_ret)}({'O' if nvda_pass else 'X'})  "
        f"SMH={_pct(smh_ret)}({'O' if smh_pass else 'X'})  "
        f"충족={passed_count}/{cfg['min_conditions']}  -> {'통과' if signal_ok else '불통'}"
    )
    return signal_ok, returns


def _pct(v) -> str:
    return f"{v*100:+.2f}%" if v is not None else "N/A"


# ── 전일 필터 ─────────────────────────────────────────────────────────────────

def _prefilter(api: KISApi, candidates: list[dict], cfg: dict, cfg_sc: dict) -> list[dict]:
    """전일 기준 필터 적용 후 통과 종목 반환 (OHLCV 데이터 및 base_score 포함)"""
    days       = cfg.get("ohlcv_days", 22)
    min_trn_b  = cfg["min_turnover_billion"] * 1e8   # 200억 → 원
    vr_thr     = cfg["volume_ratio_threshold"]        # 1.2
    passed     = []

    for stock in candidates:
        code = stock["code"]
        name = stock["name"]
        try:
            ohlcv = api.get_ohlcv(code, period="D", count=days)
            if len(ohlcv) < days:
                log.info(f"  [{name}] 데이터 부족 ({len(ohlcv)}일) → 스킵")
                continue

            # 최신순 정렬. ohlcv[0]이 당일 장 시작 전 캔들(거래대금≈0)일 수 있으므로
            # 거래대금 10억 이상인 첫 번째 캔들을 실제 전일 거래일로 사용
            MIN_TRN = 10_000_000_000  # 10억
            start_idx = 0
            for i, o in enumerate(ohlcv):
                if float(o.get("acml_tr_pbmn", 0)) >= MIN_TRN:
                    start_idx = i
                    break
            else:
                log.info(f"  [{name}] 유효 거래일 데이터 없음 → 스킵")
                continue

            if start_idx > 0:
                log.info(f"  [{name}] ohlcv[0] 거래대금 미달 → ohlcv[{start_idx}] (전일 거래일)로 대체")

            ohlcv_td = ohlcv[start_idx:]   # 전일 거래일부터의 슬라이스
            if len(ohlcv_td) < 20:
                log.info(f"  [{name}] 유효 데이터 부족 ({len(ohlcv_td)}일) → 스킵")
                continue

            closes   = [float(o["stck_clpr"]) for o in ohlcv_td]
            turnover = [float(o.get("acml_tr_pbmn", 0)) for o in ohlcv_td]

            prev_close   = closes[0]            # 전일 종가 (실제 전일 거래일)
            prev_trn     = turnover[0]          # 전일 거래대금
            ma20         = sum(closes[:20]) / 20
            ret20        = closes[0] / closes[19] - 1 if closes[19] > 0 else 0
            avg5_trn     = sum(turnover[:5]) / 5
            avg20_trn    = sum(turnover[:20]) / 20
            vol_ratio    = avg5_trn / avg20_trn if avg20_trn > 0 else 0

            cond1 = prev_trn  >= min_trn_b
            cond2 = prev_close > ma20
            cond3 = vol_ratio  >= vr_thr
            cond4 = ret20      > 0

            ok = cond1 and cond2 and cond3 and cond4
            log.info(
                f"  [{name}({code})] 거래대금={prev_trn/1e8:.0f}억({'O' if cond1 else 'X'})  "
                f"MA20={'O' if cond2 else 'X'}({prev_close:,.0f}/{ma20:,.0f})  "
                f"거래대금비율={vol_ratio:.2f}({'O' if cond3 else 'X'})  "
                f"20일수익={ret20*100:+.2f}%({'O' if cond4 else 'X'})  "
                f"-> {'통과' if ok else '제외'}"
            )
            if ok:
                base_score = (
                    cfg_sc["ret20_weight"]        * ret20
                    + cfg_sc["volume_score_weight"] * vol_ratio
                )
                passed.append({
                    **stock,
                    "prev_close": prev_close,
                    "ma20":       ma20,
                    "ret20":      ret20,
                    "vol_ratio":  vol_ratio,
                    "avg5_trn":   avg5_trn,
                    "avg20_trn":  avg20_trn,
                    "base_score": base_score,
                })
            time.sleep(0.15)
        except Exception as e:
            log.warning(f"  [{name}({code})] 전일 필터 오류: {e}")

    log.info(f"전일 필터 완료: {len(passed)}/{len(candidates)}종목 통과")
    return passed


# ── 09:05 실시간 필터 + 점수 ──────────────────────────────────────────────────

def _score_stock(api: KISApi, stock: dict, cfg_rt: dict, cfg_sc: dict) -> dict | None:
    """실시간 필터 + 점수 계산. 통과 시 score 포함 dict 반환, 실패 시 None"""
    code       = stock["code"]
    name       = stock["name"]
    prev_close = stock["prev_close"]
    max_gap    = cfg_rt["max_gap_pct"] / 100

    try:
        info = api.get_price(code)

        # 종목 상태 체크 (단기과열/투자경고/관리종목)
        mrkt_warn = info.get("mrkt_warn_cls_code", "00")
        mang_issu = info.get("mang_issu_cls_code", "N")
        if mrkt_warn not in ("00", "") or mang_issu == "Y":
            log.info(f"  [{name}] 시장경고={mrkt_warn} 관리종목={mang_issu} → 제외")
            return None

        cur   = int(info.get("stck_prpr", 0))
        open_ = int(info.get("stck_oprc", 0))

        if cur <= 0 or open_ <= 0:
            log.info(f"  [{name}] 현재가/시가 조회 실패 → 제외")
            return None

        gap = (open_ - prev_close) / prev_close if prev_close > 0 else 0

        cond1 = cur   > open_           # 현재가 > 시가
        cond2 = cur   > prev_close      # 현재가 > 전일종가
        cond3 = gap   < max_gap         # 갭 < 7%

        # 점수: 전일 부분(base_score)은 prefilter에서 계산 완료, 장중 부분만 추가
        intraday_score = (cur - open_) / open_ if open_ > 0 else 0
        score = stock["base_score"] + cfg_sc["intraday_score_weight"] * intraday_score

        ok = cond1 and cond2 and cond3
        log.info(
            f"  [{name}({code})] 현재가={cur:,} 시가={open_:,} 갭={gap*100:+.2f}%  "
            f"필터={'O' if cond1 else 'X'}{'' if cond2 else 'X'}{'' if cond3 else 'X'}  "
            f"score={score:+.4f}({'통과' if ok else '제외'})"
        )
        if not ok:
            return None

        return {
            **stock,
            "cur":            cur,
            "open":           open_,
            "gap":            gap,
            "intraday_score": intraday_score,
            "score":          score,
        }
    except Exception as e:
        log.warning(f"  [{name}({code})] 실시간 필터 오류: {e}")
        return None


# ── 미체결 확인 ───────────────────────────────────────────────────────────────

def _is_pending(api: KISApi, order_no: str) -> bool:
    try:
        pending = api.get_pending_orders()
        return any(p.get("odno") == order_no for p in pending)
    except Exception:
        return False


def _filled_qty(api: KISApi, order_no: str, code: str) -> int:
    """당일 체결 내역에서 해당 주문번호의 체결수량 합산"""
    try:
        today  = date.today().strftime("%Y%m%d")
        from config import ACCOUNT_NO, ACCOUNT_CD, IS_PAPER, BASE_URL, APP_KEY, APP_SECRET
        tr_id  = "VTTC8001R" if IS_PAPER else "TTTC8001R"
        res = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            headers=api._headers(tr_id),
            params={
                "CANO": ACCOUNT_NO, "ACNT_PRDT_CD": ACCOUNT_CD,
                "INQR_STRT_DT": today, "INQR_END_DT": today,
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN": "00", "PDNO": code,
                "ORD_GNO_BRNO": "", "ODNO": order_no,
                "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            },
            timeout=10,
        )
        total = sum(int(o.get("tot_ccld_qty", 0)) for o in res.json().get("output1", [])
                    if o.get("odno") == order_no)
        return total
    except Exception:
        return 0


# ── 매수 (지정가, ask_price_1) ────────────────────────────────────────────────

def _buy_stock(api: KISApi, code: str, name: str, qty: int, wait_sec: int) -> int | None:
    """지정가 매수 → 30초 미체결 시 1회 정정 → 추가 30초 미체결 시 취소
    성공 시 체결가 반환, 실패 시 None 반환"""
    try:
        ob = api.get_orderbook(code)
        ask1 = int(ob.get("askp1", 0))
        if ask1 <= 0:
            log.warning(f"  [{name}] 매도호가1 조회 실패 → 매수 포기")
            return None

        log.info(f"[매수주문] {name}({code}) {qty}주 @ {ask1:,}원 (지정가)")
        result   = api.buy(code, qty, price=ask1)
        order_no = result.get("odno", "")
        log.info(f"  주문번호={order_no}")

        time.sleep(wait_sec)

        if not _is_pending(api, order_no):
            # 체결 완료
            return _get_buy_price(api, code)

        # 1회 정정
        ob2  = api.get_orderbook(code)
        ask2 = int(ob2.get("askp1", ask1))
        log.info(f"  미체결 → 정정 {ask1:,} → {ask2:,}원")
        try:
            result2  = api.modify_order(order_no, code, qty, ask2)
            order_no = result2.get("odno", order_no)
        except Exception as e:
            log.warning(f"  정정 실패: {e} → 취소 시도")
            try:
                api.cancel_order(order_no, code, qty)
            except Exception:
                pass
            return None

        time.sleep(wait_sec)

        if not _is_pending(api, order_no):
            return _get_buy_price(api, code)

        # 여전히 미체결 → 취소
        log.info(f"  재정정 후도 미체결 → 취소")
        try:
            api.cancel_order(order_no, code, qty)
        except Exception:
            pass
        return None

    except Exception as e:
        log.error(f"  [{name}] 매수 오류: {e}")
        return None


def _get_buy_price(api: KISApi, code: str) -> int:
    for _ in range(5):
        p = api.get_avg_buy_price(code)
        if p > 0:
            return p
        time.sleep(1.0)
    return 0


# ── 매도 (지정가, bid_price_1 → 시장가) ──────────────────────────────────────

def _sell_stock(api: KISApi, code: str, name: str, qty: int,
                buy_price: int, wait_sec: int, reason: str) -> int:
    """지정가 매도 → 30초 미체결 시 1회 정정 → 15:25 이후 시장가
    실제 매도가 반환 (실패 시 0)"""
    order_no = ""
    try:
        ob    = api.get_orderbook(code)
        bid1  = int(ob.get("bidp1", 0))
        if bid1 <= 0:
            bid1 = int(api.get_price(code).get("stck_prpr", 0))

        log.info(f"[매도주문] {name}({code}) {qty}주 @ {bid1:,}원 (지정가) 사유={reason}")
        result   = api.sell(code, qty, price=bid1)
        order_no = result.get("odno", "")
        log.info(f"  주문번호={order_no}")

        time.sleep(wait_sec)

        if not _is_pending(api, order_no):
            return _get_sell_price(api, order_no, code, buy_price)

        # 1회 정정
        ob2   = api.get_orderbook(code)
        bid2  = int(ob2.get("bidp1", bid1))
        log.info(f"  미체결 → 정정 {bid1:,} → {bid2:,}원")
        try:
            result2  = api.modify_order(order_no, code, qty, bid2)
            order_no = result2.get("odno", order_no)
        except Exception as e:
            log.warning(f"  정정 실패: {e}")

        time.sleep(wait_sec)

        if not _is_pending(api, order_no):
            return _get_sell_price(api, order_no, code, buy_price)

        # 15:25 이후면 시장가
        now_t = datetime.now().hour * 100 + datetime.now().minute
        if now_t >= 1525:
            log.info(f"  15:25 초과 → 시장가 강제 매도")
            try:
                api.cancel_order(order_no, code, qty)
                time.sleep(0.5)
            except Exception:
                pass
            result3  = api.sell(code, qty, price=0)
            order_no = result3.get("odno", "")
            time.sleep(3.0)
            return _get_sell_price(api, order_no, code, buy_price)

        return _get_sell_price(api, order_no, code, buy_price)

    except Exception as e:
        log.error(f"  [{name}] 매도 오류: {e}")
        return 0


def _get_sell_price(api: KISApi, order_no: str, code: str, fallback: int) -> int:
    for _ in range(5):
        p = api.get_sell_execution_price(order_no, code)
        if p > 0:
            return p
        time.sleep(1.0)
    cur = int(api.get_price(code).get("stck_prpr", fallback))
    log.warning(f"  매도 체결가 조회 실패 → 현재가 {cur:,}원 사용")
    return cur or fallback


# ── CSV 로그 ──────────────────────────────────────────────────────────────────

def _append_csv(row: dict):
    csv_path = os.path.join(_BASE, f"us_semicon_log_{_MODE}.csv")
    is_new   = not os.path.exists(csv_path)
    fieldnames = [
        "date", "sox_ret", "nvda_ret", "smh_ret", "us_signal",
        "code", "name",
        "ret20", "vol_ratio", "intraday_score", "score",
        "buy_price", "sell_price", "ret_pct", "pnl",
    ]
    with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerow(row)


# ── 메인 ──────────────────────────────────────────────────────────────────────

def run():
    _setup_logger()
    strategy = _load_strategy()

    if not strategy.get("enabled", True):
        log.info(f"[{_SID}] 비활성화 → 종료")
        return

    cfg_sig = strategy["us_signal"]
    cfg_pre = strategy["prefilter"]
    cfg_rt  = strategy["realtime"]
    cfg_sc  = strategy["score"]
    cfg_ent = strategy["entry"]
    cfg_ex  = strategy["exit"]

    entry_t     = int(cfg_rt["entry_time"].replace(":", ""))    # 905
    exit_t      = int(cfg_ex["force_exit_time"].replace(":", ""))  # 1520
    mkt_t       = int(cfg_ex["market_order_time"].replace(":", "")) # 1525
    wait_sec    = cfg_ent["unfilled_wait_sec"]
    qty         = cfg_ent["qty_per_stock"]
    top_n       = cfg_sc["top_n"]
    today_str   = date.today().strftime("%Y-%m-%d")

    log.info(f"=== [{_SID}] 시작 [{today_str}] ===")

    # ── 1. 미국 선행지표 ─────────────────────────────────────────────────────
    signal_ok, us_ret = _check_us_signal(cfg_sig)
    us_msg = (
        f"[US신호] SOX={_pct(us_ret['sox'])} NVDA={_pct(us_ret['nvda'])} SMH={_pct(us_ret['smh'])}  "
        f"충족={us_ret['passed_count']}/{cfg_sig['min_conditions']} -> {'통과' if signal_ok else '불통'}"
    )
    send(f"<b>[{strategy['name']}]</b> {us_msg}")

    if not signal_ok:
        log.info("미국 신호 불충족 → 당일 매매 없음")
        # 15:35 이후 종료
        while datetime.now().hour * 100 + datetime.now().minute < 1535:
            time.sleep(60)
        return

    # ── 2. 전일 필터 ─────────────────────────────────────────────────────────
    api = KISApi()
    log.info("전일 필터 시작...")
    candidates = _prefilter(api, strategy["candidates"], cfg_pre, cfg_sc)
    if not candidates:
        log.info("전일 필터 통과 종목 없음 → 당일 매매 없음")
        send(f"<b>[{strategy['name']}]</b> 전일 필터 통과 종목 없음 → 매매 없음")
        while datetime.now().hour * 100 + datetime.now().minute < 1535:
            time.sleep(60)
        return

    # ── 3. 09:05 대기 ────────────────────────────────────────────────────────
    log.info(f"09:05 대기 중... (전일 필터 통과: {len(candidates)}종목)")
    while True:
        now_t = datetime.now().hour * 100 + datetime.now().minute
        if now_t >= entry_t:
            break
        time.sleep(5)

    # ── 4. 실시간 필터 + 점수 ────────────────────────────────────────────────
    log.info("09:05 실시간 필터 시작...")
    scored = []
    for stock in candidates:
        r = _score_stock(api, stock, cfg_rt, cfg_sc)
        if r:
            scored.append(r)
        time.sleep(0.15)

    scored.sort(key=lambda x: -x["score"])
    selected = scored[:top_n]

    log.info(f"실시간 필터 통과: {len(scored)}종목 → 상위 {len(selected)}종목 선정")
    if not selected:
        log.info("선정 종목 없음 → 당일 매매 없음")
        send(f"<b>[{strategy['name']}]</b> 09:05 필터 통과 종목 없음 → 매매 없음")
        while datetime.now().hour * 100 + datetime.now().minute < 1535:
            time.sleep(60)
        return

    # 선정 종목 알림
    lines = [f"<b>[{strategy['name']}] 선정 {len(selected)}종목</b>\n"]
    for s in selected:
        lines.append(
            f"  {s['name']}({s['code']}) "
            f"score={s['score']:+.4f} "
            f"20일수익={s['ret20']*100:+.1f}% "
            f"거래대금비율={s['vol_ratio']:.2f} "
            f"장중={s['intraday_score']*100:+.2f}%"
        )
    send("\n".join(lines))

    # ── 5. 매수 ──────────────────────────────────────────────────────────────
    positions: dict[str, dict] = {}   # code → {buy_price, qty, ...}

    for stock in selected:
        code = stock["code"]
        name = stock["name"]

        # 전략 내 중복 방지 (원자적 예약)
        if not reserve_or_skip(code, _SID):
            log.info(f"[중복방지] {name}({code}) 이미 보유/예약 중 → 스킵")
            continue

        buy_price = _buy_stock(api, code, name, qty, wait_sec)
        if buy_price and buy_price > 0:
            add_position(code, name, buy_price, qty, _SID)
            notify_buy(name, code, qty, buy_price, strategy["name"])
            positions[code] = {**stock, "buy_price": buy_price, "qty": qty}
            log.info(f"  매수 완료: {name}({code}) @ {buy_price:,}원")
        else:
            cancel_reservation(code, _SID)
            log.info(f"  매수 실패/취소: {name}({code})")

    if not positions:
        log.info("매수 완료 종목 없음")
        send(f"<b>[{strategy['name']}]</b> 매수 완료 종목 없음")
        while datetime.now().hour * 100 + datetime.now().minute < 1535:
            time.sleep(60)
        return

    log.info(f"보유 {len(positions)}종목 — 15:20 청산 대기")

    # ── 6. 15:20 강제 청산 ───────────────────────────────────────────────────
    while True:
        now_t = datetime.now().hour * 100 + datetime.now().minute
        if now_t >= exit_t:
            break
        time.sleep(10)

    log.info("=== 15:20 강제 청산 시작 ===")
    total_pnl = 0

    for code, pos in list(positions.items()):
        name      = pos["name"]
        buy_price = pos["buy_price"]
        sell_qty  = pos["qty"]

        sell_price = _sell_stock(api, code, name, sell_qty, buy_price, wait_sec, "강제청산")
        if sell_price <= 0:
            log.error(f"  [{name}] 매도 실패 — 수동 확인 필요")
            notify_error(f"[{_SID}] {name}({code}) 매도 실패 — 수동 확인 필요")
            continue

        gross = (sell_price - buy_price) * sell_qty
        cost  = round(
            buy_price  * sell_qty * BUY_FEE
            + sell_price * sell_qty * (SELL_FEE + SELL_TAX)
        )
        pnl   = gross - cost
        ret_pct = (sell_price / buy_price - 1) * 100 if buy_price > 0 else 0

        record_trade(name, code, sell_qty, buy_price, sell_price, "강제청산", _SID)
        remove_position(f"{code}_{_SID}")
        notify_sell(name, code, sell_qty, buy_price, sell_price, "강제청산", _SID)
        total_pnl += pnl

        log.info(
            f"  [{name}] 매도완료 {buy_price:,}->{sell_price:,}원  "
            f"수익={ret_pct:+.2f}%  순손익={pnl:+,}원"
        )

        # CSV 기록
        _append_csv({
            "date":           today_str,
            "sox_ret":        f"{us_ret['sox']*100:+.2f}%" if us_ret["sox"] else "N/A",
            "nvda_ret":       f"{us_ret['nvda']*100:+.2f}%" if us_ret["nvda"] else "N/A",
            "smh_ret":        f"{us_ret['smh']*100:+.2f}%" if us_ret["smh"] else "N/A",
            "us_signal":      "O",
            "code":           code,
            "name":           name,
            "ret20":          f"{pos['ret20']*100:+.2f}%",
            "vol_ratio":      f"{pos['vol_ratio']:.2f}",
            "intraday_score": f"{pos.get('intraday_score', 0)*100:+.2f}%",
            "score":          f"{pos.get('score', 0):+.4f}",
            "buy_price":      buy_price,
            "sell_price":     sell_price,
            "ret_pct":        f"{ret_pct:+.2f}%",
            "pnl":            pnl,
        })

    # 결산 알림
    send(
        f"<b>[{strategy['name']}] 당일 결산</b>\n"
        f"종목수: {len(positions)}  순손익: {total_pnl:+,}원"
    )
    log.info(f"=== [{_SID}] 종료 | 당일 순손익 {total_pnl:+,}원 ===")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info(f"[{_SID}] 수동 종료")
    except Exception as e:
        log.error(f"[{_SID}] 오류:\n{traceback.format_exc()}")
        notify_error(f"[{_SID}] 오류 종료: {e}")
