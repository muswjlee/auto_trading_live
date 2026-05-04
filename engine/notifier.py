"""텔레그램 알림 전송"""

import requests
import logging

log = logging.getLogger(__name__)

BOT_TOKEN = "8527515865:AAGgyyYvhWSNLXVz3P6-pVqJPWXP-jAhDl0"
CHAT_ID   = "8221120885"


def send(message: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"텔레그램 전송 실패: {e}")


def notify_buy(name: str, code: str, qty: int, price: int):
    send(
        f"🟢 <b>[매수]</b> {name} ({code})\n"
        f"가격: {price:,}원 × {qty:,}주\n"
        f"금액: {price * qty:,}원"
    )


def notify_sell(name: str, code: str, qty: int, buy_price: int, current: int, reason: str):
    pnl_pct = (current - buy_price) / buy_price * 100
    pnl_amt = (current - buy_price) * qty
    emoji = "✅" if pnl_amt >= 0 else "🔴"
    send(
        f"{emoji} <b>[{reason}]</b> {name} ({code})\n"
        f"매입가: {buy_price:,}원 → 현재가: {current:,}원\n"
        f"수익률: {pnl_pct:+.2f}%  손익: {pnl_amt:+,}원"
    )


def notify_no_signal():
    send("📭 오늘 매수 조건 충족 종목 없음")


def notify_error(message: str):
    send(f"⚠️ <b>[오류]</b> {message}")
