"""텔레그램 알림 전송"""

import os
import requests
import logging
from dotenv import load_dotenv

log = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE = os.getenv("KIS_MODE", "paper")
load_dotenv(os.path.join(_BASE, f".env.{_MODE}"))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

_MODE_TAG = "[모의] " if _MODE == "paper" else ""


def send(message: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": _MODE_TAG + message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"텔레그램 전송 실패: {e}")


def notify_buy(name: str, code: str, qty: int, price: int, strategy: str = ""):
    prefix = f"[{strategy}] " if strategy else ""
    send(
        f"🟢 <b>{prefix}[매수]</b> {name} ({code})\n"
        f"가격: {price:,}원 × {qty:,}주\n"
        f"금액: {price * qty:,}원"
    )


def notify_sell(name: str, code: str, qty: int, buy_price: int, current: int, reason: str, strategy: str = ""):
    from engine.monitor import BUY_FEE_RATE, SELL_FEE_RATE, SELL_TAX_RATE
    gross_pnl = (current - buy_price) * qty
    cost = round(buy_price * qty * BUY_FEE_RATE + current * qty * (SELL_FEE_RATE + SELL_TAX_RATE))
    net_pnl = gross_pnl - cost
    pnl_pct = (current - buy_price) / buy_price * 100
    emoji = "✅" if net_pnl >= 0 else "🔴"
    prefix = f"[{strategy}] " if strategy else ""
    send(
        f"{emoji} <b>{prefix}[{reason}]</b> {name} ({code})\n"
        f"매입가: {buy_price:,}원 → 현재가: {current:,}원\n"
        f"수익률: {pnl_pct:+.2f}%  손익: {net_pnl:+,}원"
    )


def notify_no_signal(strategy: str = ""):
    prefix = f"[{strategy}] " if strategy else ""
    send(f"📭 {prefix}오늘 매수 조건 충족 종목 없음")


def notify_error(message: str):
    send(f"⚠️ <b>[오류]</b> {message}")
