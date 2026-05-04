"""네이버 증권 리서치 보고서 스크래퍼"""

import logging
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_BASE = "https://finance.naver.com"
_LIST_URL = f"{_BASE}/research/company_list.naver"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/research/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _parse_page(html: str, today_str: str) -> list[dict]:
    """
    HTML에서 오늘 날짜 보고서 행 파싱
    컬럼 순서: [0]종목명 [1]제목 [2]증권사 [3]PDF [4]날짜(YY.MM.DD) [5]목표가
    반환: [{"code", "name", "firm", "target_price", "title"}]
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table.type_1 tr")

    results = []
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 6:
            continue

        # 날짜: cols[4], 형식 "YY.MM.DD"
        report_date = cols[4].get_text(strip=True)
        if report_date != today_str:
            continue

        # 종목 링크에서 코드 추출
        stock_link = cols[0].find("a", href=re.compile(r"code=\d+"))
        if not stock_link:
            continue

        match = re.search(r"code=(\d+)", stock_link["href"])
        if not match:
            continue

        code  = match.group(1)
        name  = stock_link.get_text(strip=True)
        title = cols[1].get_text(strip=True)
        firm  = cols[2].get_text(strip=True)

        results.append({
            "code":  code,
            "name":  name,
            "title": title,
            "firm":  firm,
        })

    return results


def get_today_research(max_pages: int = 3) -> list[dict]:
    """
    오늘 날짜 리서치 보고서 종목 목록 반환 (중복 코드 제거)
    max_pages: 확인할 최대 페이지 수 (1페이지 = 20건)
    """
    today_str = date.today().strftime("%y.%m.%d")
    log.info(f"[리서치] 네이버 증권 보고서 조회 ({today_str})")

    seen_codes = set()
    all_results = []

    for page in range(1, max_pages + 1):
        try:
            res = requests.get(
                _LIST_URL,
                params={"page": page},
                headers=_HEADERS,
                timeout=10,
            )
            res.raise_for_status()
            res.encoding = "euc-kr"

            rows = _parse_page(res.text, today_str)

            if not rows:
                log.info(f"[리서치] {page}페이지 — 오늘 보고서 없음, 중단")
                break

            for r in rows:
                if r["code"] not in seen_codes:
                    seen_codes.add(r["code"])
                    all_results.append(r)

            log.info(f"[리서치] {page}페이지 — {len(rows)}건 수집")
            time.sleep(0.5)

        except Exception as e:
            log.warning(f"[리서치] {page}페이지 조회 실패: {e}")
            break

    log.info(f"[리서치] 총 {len(all_results)}개 종목: {[r['name'] for r in all_results]}")
    return all_results
