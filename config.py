import os
from dotenv import load_dotenv

MODE = os.getenv("KIS_MODE", "paper")

_env_file = os.path.join(os.path.dirname(__file__), f".env.{MODE}")
load_dotenv(_env_file)

APP_KEY    = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
ACCOUNT_NO = os.getenv("KIS_ACCOUNT_NO")
ACCOUNT_CD = os.getenv("KIS_ACCOUNT_CD", "01")

if not all([APP_KEY, APP_SECRET, ACCOUNT_NO]):
    raise EnvironmentError(f".env.{MODE} 파일에 KIS API 환경변수가 없습니다.")

IS_PAPER = MODE == "paper"

BASE_URL = (
    "https://openapivts.koreainvestment.com:29443"
    if IS_PAPER else
    "https://openapi.koreainvestment.com:9443"
)

WS_URL = (
    "ws://ops.koreainvestment.com:31000"
    if IS_PAPER else
    "ws://ops.koreainvestment.com:21000"
)
