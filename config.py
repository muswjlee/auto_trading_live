import os
from dotenv import load_dotenv

load_dotenv()

# KIS API 설정
APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO")
ACCOUNT_CD = os.getenv("KIS_ACCOUNT_CD", "01")  # 계좌 상품코드

if not all([APP_KEY, APP_SECRET, ACCOUNT_NO]):
    raise EnvironmentError("KIS API 환경변수가 설정되지 않았습니다. .env 파일을 확인하세요.")

# 모의투자: True / 실투자: False
IS_PAPER = os.getenv("KIS_IS_PAPER", "true").lower() == "true"

BASE_URL = (
    "https://openapivts.koreainvestment.com:29443"  # 모의투자
    if IS_PAPER else
    "https://openapi.koreainvestment.com:9443"       # 실투자
)

WS_URL = (
    "ws://ops.koreainvestment.com:31000"   # 모의투자 WebSocket
    if IS_PAPER else
    "ws://ops.koreainvestment.com:21000"   # 실투자 WebSocket
)
