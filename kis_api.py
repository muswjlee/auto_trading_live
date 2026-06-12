"""한국투자증권 KIS Developers API 래퍼"""

import time
import json
import os
import requests
from datetime import datetime, timedelta
from config import APP_KEY, APP_SECRET, ACCOUNT_NO, ACCOUNT_CD, BASE_URL, IS_PAPER, MODE

_TOKEN_CACHE_FILE = os.path.join(os.path.dirname(__file__), f".token_cache_{MODE}.json")
_TIMEOUT = 10   # 조회 API 기본 timeout (초)
_ORDER_TIMEOUT = 15  # 주문 API timeout (초)


class KISApi:
    def __init__(self):
        self._token = None
        self._token_expires = None
        self._load_token_cache()

    # ── 인증 ──────────────────────────────────────────────────────────────

    def _load_token_cache(self):
        """파일에 저장된 토큰을 로드 (재발급 횟수 절감)"""
        try:
            with open(_TOKEN_CACHE_FILE, "r") as f:
                data = json.load(f)
            expires = datetime.fromisoformat(data["expires"])
            if datetime.now() < expires:
                self._token = data["token"]
                self._token_expires = expires
        except (FileNotFoundError, KeyError, ValueError):
            pass

    def _save_token_cache(self):
        with open(_TOKEN_CACHE_FILE, "w") as f:
            json.dump({"token": self._token, "expires": self._token_expires.isoformat()}, f)

    def _refresh_token(self):
        url = f"{BASE_URL}/oauth2/tokenP"
        res = requests.post(url, json={
            "grant_type": "client_credentials",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
        }, timeout=_TIMEOUT)
        res.raise_for_status()
        data = res.json()
        self._token = data["access_token"]
        self._token_expires = datetime.now() + timedelta(hours=23)
        self._save_token_cache()

    @property
    def token(self):
        if not self._token or datetime.now() >= self._token_expires:
            self._refresh_token()
        return self._token

    def _headers(self, tr_id: str, extra: dict = None) -> dict:
        h = {
            "authorization": f"Bearer {self.token}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": tr_id,
            "content-type": "application/json; charset=utf-8",
        }
        if extra:
            h.update(extra)
        return h

    # ── 시세 조회 ──────────────────────────────────────────────────────────

    def get_price(self, stock_code: str) -> dict:
        """현재가 조회 (500 에러 시 최대 3회 재시도)"""
        last_err = None
        for attempt in range(3):
            try:
                res = requests.get(
                    f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
                    headers=self._headers("FHKST01010100"),
                    params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
                    timeout=_TIMEOUT,
                )
                res.raise_for_status()
                return res.json()["output"]
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        raise last_err

    def _fetch_volume_rank_page(self, market: str, min_price: int, max_price: int, min_volume: int, div_cls_code: str = "0") -> list[dict]:
        """단일 시장 거래량/거래대금 상위 30개 조회 (내부용)"""
        res = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/volume-rank",
            headers=self._headers("FHPST01710000"),
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": div_cls_code,
                "FID_BLNG_CLS_CODE": market,
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": str(min_price),
                "FID_INPUT_PRICE_2": str(max_price),
                "FID_VOL_CNT": str(min_volume),
                "FID_INPUT_DATE_1": "",
            },
            timeout=_TIMEOUT,
        )
        res.raise_for_status()
        return res.json().get("output", [])

    @staticmethod
    def _make_price_bands(p_min: int, p_max: int) -> list[tuple[int, int]]:
        """가격 범위를 여러 구간으로 분할 (각 구간 내 거래량 ≈ 거래대금 순서)"""
        FIXED_BREAKS = [10_000, 15_000, 25_000, 40_000, 70_000, 120_000, 200_000, 300_000, 500_000]
        breaks = sorted({p_min} | {b for b in FIXED_BREAKS if p_min < b < p_max} | {p_max})
        return [(breaks[i], breaks[i + 1]) for i in range(len(breaks) - 1)]

    def _get_top_by_turnover(self, top_n: int, min_price: int, max_price: int) -> list[dict]:
        """
        코스피/코스닥 각각 가격대별 분할 조회(~200개 풀) 후 acml_tr_pbmn 기준 top_n 추출
        가격대를 7개 구간으로 나눠 구간별 거래량 top30 수집 → 각 시장 ~210개 → 합산 정렬
        """
        p_min  = max(min_price, 1_000)
        p_max  = max_price if max_price > 0 else 500_000
        bands  = self._make_price_bands(p_min, p_max)

        seen_codes: set[str] = set()
        raw_all: list[dict]  = []

        for mkt in ["1", "2"]:                      # 코스피, 코스닥
            for band_min, band_max in bands:
                try:
                    page = self._fetch_volume_rank_page(mkt, band_min, band_max, 0, "0")
                    for item in page:
                        code = item.get("mksc_shrn_iscd", "")
                        if code and code not in seen_codes:
                            seen_codes.add(code)
                            raw_all.append(item)
                except Exception:
                    pass
                time.sleep(0.1)

        raw_all.sort(key=lambda x: int(x.get("acml_tr_pbmn", 0)), reverse=True)

        result = []
        for rank, item in enumerate(raw_all[:top_n], start=1):
            result.append({
                "rank":        rank,
                "code":        item.get("mksc_shrn_iscd", ""),
                "name":        item.get("hts_kor_isnm", ""),
                "price":       int(item.get("stck_prpr", 0)),
                "volume":      int(item.get("acml_vol", 0)),
                "change":      int(item.get("prdy_vrss", 0)),
                "change_rate": float(item.get("prdy_ctrt", 0)),
                "high":        int(item.get("stck_hgpr", 0)),
                "low":         int(item.get("stck_lwpr", 0)),
            })
        return result

    def get_volume_rank(
        self,
        top_n: int = 100,
        market: str = "0",          # 0: 전체, 1: 코스피, 2: 코스닥
        min_price: int = 0,
        max_price: int = 0,
        min_volume: int = 0,
        by_turnover: bool = False,  # True: 거래대금 기준, False: 거래량 기준
    ) -> list[dict]:
        """거래량/거래대금 상위 종목 조회"""
        if by_turnover:
            return self._get_top_by_turnover(top_n, min_price, max_price)

        # 거래량 기준: 기존 로직 유지
        if market == "0":
            markets = ["0", "1", "2", "3", "4", "5"]
        else:
            markets = [market]

        seen_codes = set()
        raw_all = []
        for mkt in markets:
            try:
                page = self._fetch_volume_rank_page(mkt, min_price, max_price, min_volume, "0")
            except Exception:
                time.sleep(0.2)
                continue
            time.sleep(0.2)
            for item in page:
                code = item.get("mksc_shrn_iscd", "")
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    raw_all.append(item)

        raw_all.sort(key=lambda x: int(x.get("acml_vol", 0)), reverse=True)

        result = []
        for rank, item in enumerate(raw_all[:top_n], start=1):
            result.append({
                "rank":        rank,
                "code":        item.get("mksc_shrn_iscd", ""),
                "name":        item.get("hts_kor_isnm", ""),
                "price":       int(item.get("stck_prpr", 0)),
                "volume":      int(item.get("acml_vol", 0)),
                "change":      int(item.get("prdy_vrss", 0)),
                "change_rate": float(item.get("prdy_ctrt", 0)),
                "high":        int(item.get("stck_hgpr", 0)),
                "low":         int(item.get("stck_lwpr", 0)),
            })
        return result

    def is_trading_day(self, date: datetime = None) -> bool:
        """거래일 확인: KIS API → 실패 시 holidays 라이브러리 + 근로자의날 fallback"""
        if date is None:
            date = datetime.now()
        if date.weekday() >= 5:
            return False

        date_str = date.strftime("%Y%m%d")

        # 1차: KIS API (실거래 환경에서만 동작, 모의투자 서버는 미지원)
        try:
            res = requests.get(
                f"{BASE_URL}/uapi/domestic-stock/v1/quotations/chk-holiday",
                headers=self._headers("CTCA0903R"),
                params={"BASS_DT": date_str, "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
                timeout=_TIMEOUT,
            )
            res.raise_for_status()
            data = res.json()
            if data.get("rt_cd") == "0":  # API 정상 응답
                for item in data.get("output", []):
                    if item.get("bass_dt") == date_str:
                        return item.get("opnd_yn") == "Y"
        except Exception:
            pass

        # 2차: holidays 라이브러리 (모의투자 환경 fallback)
        try:
            import holidays as _holidays
            d = date.date()
            kr = _holidays.KR(years=d.year)
            if d.month == 5 and d.day == 1:  # 근로자의 날 (라이브러리 누락)
                return False
            return d not in kr
        except Exception:
            pass

        return True  # 모든 확인 실패 시 거래일로 간주

    def get_execution_strength(self, stock_code: str) -> float:
        """당일 체결강도 조회 (inquire-ccnl의 tday_rltv 필드) — 0.0 반환 시 1회 재시도"""
        for attempt in range(2):
            try:
                res = requests.get(
                    f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-ccnl",
                    headers=self._headers("FHKST01010300"),
                    params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
                    timeout=_TIMEOUT,
                )
                res.raise_for_status()
                output = res.json().get("output", [])
                if output:
                    val = float(output[0].get("tday_rltv", 0))
                    if val > 0:
                        return val
            except Exception:
                pass
            if attempt == 0:
                time.sleep(0.3)
        return 0.0

    def get_instant_execution_strength(self, stock_code: str, window_sec: int = None) -> float:
        """순간 체결강도: 최근 틱 기준 매수/매도 체결량 비율
        - window_sec: 시간 창(초). None이면 전체 틱 사용, 30이면 최근 30초 틱만 사용.
        - 가격 상승 틱 → 매수 체결, 하락 틱 → 매도 체결 (Lee-Ready 방식)
        - 동일 가격은 직전 방향 유지
        """
        for attempt in range(2):
            try:
                res = requests.get(
                    f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-ccnl",
                    headers=self._headers("FHKST01010300"),
                    params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
                    timeout=_TIMEOUT,
                )
                res.raise_for_status()
                ticks = res.json().get("output", [])
                if not ticks:
                    continue

                now = datetime.now()

                # 최신 틱이 60초 이상 오래됐으면 stale → 모멘텀 없음
                recent_hour = ticks[0].get("stck_cntg_hour", "")
                if len(recent_hour) == 6:
                    tick_time = now.replace(
                        hour=int(recent_hour[:2]),
                        minute=int(recent_hour[2:4]),
                        second=int(recent_hour[4:6]),
                        microsecond=0,
                    )
                    if (now - tick_time).total_seconds() > 60:
                        return 0.0

                # 시간 창 필터 (window_sec 지정 시)
                if window_sec is not None:
                    cutoff = now - timedelta(seconds=window_sec)
                    filtered = []
                    for t in ticks:
                        th = t.get("stck_cntg_hour", "")
                        if len(th) == 6:
                            try:
                                tt = now.replace(
                                    hour=int(th[:2]), minute=int(th[2:4]),
                                    second=int(th[4:6]), microsecond=0,
                                )
                                if tt >= cutoff:
                                    filtered.append(t)
                            except ValueError:
                                pass
                    if not filtered:
                        return 0.0
                    ticks = filtered

                buy_vol = sell_vol = 0
                last_dir = None
                # ticks[0]이 최신, ticks[-1]이 가장 오래됨
                for i in range(len(ticks)):
                    price = int(ticks[i].get("stck_prpr", 0))
                    vol   = int(ticks[i].get("cntg_vol", 0))
                    prev  = int(ticks[i + 1].get("stck_prpr", price)) if i < len(ticks) - 1 else price
                    if price > prev:
                        last_dir = "buy"
                    elif price < prev:
                        last_dir = "sell"
                    if last_dir == "buy":
                        buy_vol += vol
                    elif last_dir == "sell":
                        sell_vol += vol

                if sell_vol == 0:
                    return 999.9 if buy_vol > 0 else 0.0
                return round(buy_vol / sell_vol * 100, 2)
            except Exception:
                pass
            if attempt == 0:
                time.sleep(0.3)
        return 0.0

    def get_ohlcv(self, stock_code: str, period: str = "D", count: int = 30) -> list[dict]:
        """일/주/월 OHLCV 조회 (period: D/W/M) — count만큼 확보하기 위해 시작일 자동 계산"""
        # 주말·공휴일 감안해 count * 2 캘린더일 이전부터 조회
        start = (datetime.now() - timedelta(days=count * 2)).strftime("%Y%m%d")
        res = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            headers=self._headers("FHKST01010400"),
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": stock_code,
                "FID_PERIOD_DIV_CODE": period,
                "FID_ORG_ADJ_PRC": "0",
                "FID_INPUT_DATE_1": start,
            },
            timeout=_TIMEOUT,
        )
        res.raise_for_status()
        return res.json()["output"][:count]

    def get_minute_candles(self, stock_code: str, count: int = 10) -> list[dict]:
        """1분봉 조회 — 최신순 반환 (output2)"""
        now = datetime.now().strftime("%H%M%S")
        res = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            headers=self._headers("FHKST03010200"),
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": stock_code,
                "FID_INPUT_HOUR_1": now,
                "FID_PW_DATA_INCU_YN": "N",
                "FID_ETC_CLS_CODE": "",
            },
            timeout=_TIMEOUT,
        )
        res.raise_for_status()
        return res.json().get("output2", [])[:count]

    # ── 잔고 조회 ──────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """주식 잔고 및 예수금 조회 (500 에러 시 최대 3회 재시도)"""
        tr_id = "VTTC8434R" if IS_PAPER else "TTTC8434R"
        params = {
            "CANO": ACCOUNT_NO,
            "ACNT_PRDT_CD": ACCOUNT_CD,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "N",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        last_err = None
        for attempt in range(3):
            try:
                res = requests.get(
                    f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
                    headers=self._headers(tr_id),
                    params=params,
                    timeout=_TIMEOUT,
                )
                res.raise_for_status()
                data = res.json()
                return {
                    "stocks": data.get("output1", []),   # 보유 종목
                    "summary": data.get("output2", {}),  # 총평가액 등
                }
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        raise last_err

    def get_cash(self) -> int:
        """주문 가능 현금 조회"""
        balance = self.get_balance()
        summary = balance["summary"]
        if isinstance(summary, list) and summary:
            return int(summary[0].get("dnca_tot_amt", 0))
        if isinstance(summary, dict):
            return int(summary.get("dnca_tot_amt", 0))
        return 0

    # ── 주문 ──────────────────────────────────────────────────────────────

    def _order(self, stock_code: str, qty: int, price: int, side: str) -> dict:
        """
        side: 'buy' | 'sell'
        price=0 이면 시장가 주문
        """
        if side == "buy":
            tr_id = "VTTC0802U" if IS_PAPER else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if IS_PAPER else "TTTC0801U"

        ord_dvsn = "01" if price == 0 else "00"  # 01: 시장가, 00: 지정가

        res = requests.post(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
            headers=self._headers(tr_id),
            json={
                "CANO": ACCOUNT_NO,
                "ACNT_PRDT_CD": ACCOUNT_CD,
                "PDNO": stock_code,
                "ORD_DVSN": ord_dvsn,
                "ORD_QTY": str(qty),
                "ORD_UNPR": str(price),
            },
            timeout=_ORDER_TIMEOUT,
        )
        res.raise_for_status()
        result = res.json()
        if result.get("rt_cd") != "0":
            raise RuntimeError(f"주문 오류: {result.get('msg1')}")
        return result["output"]

    def buy(self, stock_code: str, qty: int, price: int = 0) -> dict:
        """매수 (price=0: 시장가)"""
        return self._order(stock_code, qty, price, "buy")

    def sell(self, stock_code: str, qty: int, price: int = 0) -> dict:
        """매도 (price=0: 시장가)"""
        return self._order(stock_code, qty, price, "sell")

    # ── 미체결 조회 / 취소 ─────────────────────────────────────────────────

    def get_pending_orders(self) -> list[dict]:
        """미체결 주문 목록"""
        tr_id = "VTTC8036R" if IS_PAPER else "TTTC8036R"
        res = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            headers=self._headers(tr_id),
            params={
                "CANO": ACCOUNT_NO,
                "ACNT_PRDT_CD": ACCOUNT_CD,
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "INQR_DVSN_1": "0",
                "INQR_DVSN_2": "0",
            },
            timeout=_TIMEOUT,
        )
        res.raise_for_status()
        return res.json().get("output", [])

    def cancel_order(self, order_no: str, stock_code: str, qty: int) -> dict:
        """주문 취소"""
        tr_id = "VTTC0803U" if IS_PAPER else "TTTC0803U"
        res = requests.post(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-rvsecncl",
            headers=self._headers(tr_id),
            json={
                "CANO": ACCOUNT_NO,
                "ACNT_PRDT_CD": ACCOUNT_CD,
                "KRX_FWDG_ORD_ORGNO": "",
                "ORGN_ODNO": order_no,
                "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02",  # 02: 취소
                "ORD_QTY": str(qty),
                "ORD_UNPR": "0",
                "PDNO": stock_code,
                "QTY_ALL_ORD_YN": "Y",
            },
            timeout=_ORDER_TIMEOUT,
        )
        res.raise_for_status()
        return res.json()["output"]

    def get_avg_buy_price(self, stock_code: str) -> int:
        """잔고에서 해당 종목의 실제 평균매입가 조회 (시장가 매수 후 체결가 확인용)"""
        try:
            bal = self.get_balance()
            for s in bal.get("stocks", []):
                if s.get("pdno") == stock_code:
                    return int(float(s.get("pchs_avg_pric", 0)))
        except Exception:
            pass
        return 0

    def get_sell_execution_price(self, order_no: str, stock_code: str) -> int:
        """체결된 매도 주문의 실제 평균 체결가 조회 (주문번호 기반)"""
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        tr_id = "VTTC8001R" if IS_PAPER else "TTTC8001R"
        try:
            res = requests.get(
                f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                headers=self._headers(tr_id),
                params={
                    "CANO":            ACCOUNT_NO,
                    "ACNT_PRDT_CD":    ACCOUNT_CD,
                    "INQR_STRT_DT":    today,
                    "INQR_END_DT":     today,
                    "SLL_BUY_DVSN_CD": "01",
                    "INQR_DVSN":       "00",
                    "PDNO":            stock_code,
                    "ORD_GNO_BRNO":    "",
                    "ODNO":            order_no,
                    "INQR_DVSN_3":     "00",
                    "INQR_DVSN_1":     "",
                    "CTX_AREA_FK100":  "",
                    "CTX_AREA_NK100":  "",
                },
                timeout=_TIMEOUT,
            )
            res.raise_for_status()
            for item in res.json().get("output1", []):
                if item.get("odno") == order_no:
                    avg = int(item.get("avg_prvs", 0))
                    if avg > 0:
                        return avg
        except Exception:
            pass
        return 0
