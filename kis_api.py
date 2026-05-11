"""한국투자증권 KIS Developers API 래퍼"""

import time
import json
import os
import requests
from datetime import datetime, timedelta
from config import APP_KEY, APP_SECRET, ACCOUNT_NO, ACCOUNT_CD, BASE_URL, IS_PAPER

_TOKEN_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".token_cache.json")


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
        })
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
                )
                res.raise_for_status()
                return res.json()["output"]
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        raise last_err

    def _fetch_volume_rank_page(self, market: str, min_price: int, max_price: int, min_volume: int) -> list[dict]:
        """단일 시장 거래량 상위 30개 조회 (내부용)"""
        res = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/volume-rank",
            headers=self._headers("FHPST01710000"),
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": market,
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": str(min_price),
                "FID_INPUT_PRICE_2": str(max_price),
                "FID_VOL_CNT": str(min_volume),
                "FID_INPUT_DATE_1": "",
            },
        )
        res.raise_for_status()
        return res.json().get("output", [])

    def get_volume_rank(
        self,
        top_n: int = 100,
        market: str = "0",       # 0: 전체, 1: 코스피, 2: 코스닥
        min_price: int = 0,
        max_price: int = 0,
        min_volume: int = 0,
    ) -> list[dict]:
        """
        거래량 상위 종목 조회 (최대 100개)
        API 1회 호출 한계(30개)를 코스피/코스닥/ETF 분리 호출로 우회 후 거래량 기준 정렬
        """
        # 요청 시장에 따라 호출할 세부 시장 코드 결정
        # 1: 코스피, 2: 코스닥, 3: ELW, 4: ETF, 5: KONEX
        if market == "0":
            markets = ["0", "1", "2", "3", "4", "5"]  # "0" 전체 통합 top30 추가
        else:
            markets = [market]

        seen_codes = set()
        raw_all = []
        for mkt in markets:
            try:
                page = self._fetch_volume_rank_page(mkt, min_price, max_price, min_volume)
            except Exception:
                time.sleep(0.2)
                continue
            time.sleep(0.2)
            for item in page:
                code = item.get("mksc_shrn_iscd", "")
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    raw_all.append(item)

        # 거래량 내림차순 정렬 후 top_n 개 추출
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

    def get_ohlcv(self, stock_code: str, period: str = "D", count: int = 30) -> list[dict]:
        """일/주/월 OHLCV 조회 (period: D/W/M)"""
        res = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            headers=self._headers("FHKST01010400"),
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": stock_code,
                "FID_PERIOD_DIV_CODE": period,
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        res.raise_for_status()
        return res.json()["output"][:count]

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
        )
        res.raise_for_status()
        return res.json()["output"]
