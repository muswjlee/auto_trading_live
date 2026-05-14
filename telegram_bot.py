"""텔레그램 봇 명령어 수신기 — 핸드폰에서 자동매매 시스템 원격 제어"""

import os
import sys
import json
import time
import logging
import subprocess
import requests
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.notifier import BOT_TOKEN, CHAT_ID

_BASE      = os.path.dirname(os.path.abspath(__file__))
_MAIN      = os.path.join(_BASE, "main.py")
_PYTHON    = sys.executable
_MODE      = os.getenv("KIS_MODE", "paper")
LOG_FILE   = os.path.join(_BASE, "telegram_bot.log")
_PNL_FILE  = os.path.join(_BASE, f"daily_pnl_{_MODE}.json")
_POS_FILE  = os.path.join(_BASE, f"positions_{_MODE}.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("telegram_bot")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send(text: str):
    try:
        requests.post(f"{API}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                      timeout=5)
    except Exception as e:
        log.warning(f"전송 실패: {e}")


def is_watchdog_running() -> bool:
    pid_file = os.path.join(_BASE, "watchdog.pid")
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return False


def get_running_strategies() -> list[str]:
    import psutil
    strategies = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if "runner.py" in cmdline:
                parts = proc.info["cmdline"]
                sid = parts[-1] if parts else "unknown"
                strategies.append(sid)
        except Exception:
            pass
    return strategies


def cmd_start():
    if is_watchdog_running():
        send("✅ 자동매매 시스템이 이미 실행 중입니다.")
        return

    try:
        subprocess.Popen(
            [_PYTHON, _MAIN, "--mode", _MODE],
            cwd=_BASE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        time.sleep(5)
        if is_watchdog_running():
            send(f"🚀 자동매매 시스템을 시작했습니다. (모드: {_MODE})")
            log.info("watchdog 시작 완료")
        else:
            send("⚠️ 시작 명령을 보냈으나 프로세스를 확인할 수 없습니다. watchdog 로그를 확인하세요.")
    except Exception as e:
        send(f"⚠️ 시작 실패: {e}")
        log.error(f"watchdog 시작 실패: {e}")


def cmd_status():
    now = datetime.now().strftime("%H:%M:%S")
    launcher = is_watchdog_running()
    strategies = get_running_strategies()

    lines = [f"📡 <b>시스템 상태</b> ({now})\n"]
    lines.append(f"런처: {'✅ 실행 중' if launcher else '🔴 중단'}")

    if strategies:
        lines.append(f"\n실행 중인 전략 ({len(strategies)}개):")
        for s in strategies:
            lines.append(f"  • {s}")
    else:
        lines.append("\n실행 중인 전략 없음")

    # 오늘 손익
    try:
        pnl_file = _PNL_FILE
        if os.path.exists(pnl_file):
            with open(pnl_file, "r", encoding="utf-8") as f:
                pnl = json.load(f)
            if pnl.get("date") == str(datetime.now().date()):
                total = pnl.get("total_pnl", 0)
                trades = len(pnl.get("trades", []))
                sign = "+" if total >= 0 else ""
                lines.append(f"\n💰 당일 순손익: {sign}{total:,}원 ({trades}건)")
    except Exception:
        pass

    # 보유 포지션
    try:
        pos_file = _POS_FILE
        if os.path.exists(pos_file):
            with open(pos_file, "r", encoding="utf-8") as f:
                pos = json.load(f)
            if pos:
                lines.append(f"\n📦 보유 포지션 ({len(pos)}개):")
                for v in pos.values():
                    lines.append(f"  • {v['name']} {v['qty']}주 @ {v['buy_price']:,}원")
            else:
                lines.append("\n📦 보유 포지션 없음")
    except Exception:
        pass

    send("\n".join(lines))


def cmd_stop():
    import psutil
    killed = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if ("watchdog.py" in cmdline or "runner.py" in cmdline) and "telegram_bot" not in cmdline:
                proc.kill()
                killed.append(proc.info["cmdline"][-1] if proc.info["cmdline"] else str(proc.pid))
        except Exception:
            pass
    if killed:
        send(f"🛑 자동매매 시스템을 종료했습니다.\n종료된 프로세스: {len(killed)}개")
        log.info(f"프로세스 종료: {killed}")
    else:
        send("ℹ️ 실행 중인 자동매매 프로세스가 없습니다.")


def cmd_pnl():
    try:
        pnl_file = _PNL_FILE
        if not os.path.exists(pnl_file):
            send("📭 오늘 거래 내역이 없습니다.")
            return
        with open(pnl_file, "r", encoding="utf-8") as f:
            pnl = json.load(f)
        if pnl.get("date") != str(datetime.now().date()):
            send("📭 오늘 거래 내역이 없습니다.")
            return

        from collections import defaultdict
        groups = defaultdict(list)
        for t in pnl.get("trades", []):
            groups[t.get("strategy_id", "기타")].append(t)

        lines = [f"📊 <b>전략별 손익 ({pnl['date']})</b>\n"]
        for sid, trades in groups.items():
            subtotal = sum(t["pnl"] for t in trades)
            sign = "✅" if subtotal >= 0 else "🔴"
            lines.append(f"{sign} [{sid}]  {subtotal:+,}원 ({len(trades)}건)")
            for t in trades:
                inv = t["buy_price"] * t["qty"]
                rate = t["pnl"] / inv * 100
                icon = "↑" if t["pnl"] >= 0 else "↓"
                lines.append(f"   {icon} {t['name']} {t['pnl']:+,}원 ({rate:+.1f}%)")

        total = pnl.get("total_pnl", 0)
        cost  = pnl.get("total_cost", 0)
        lines.append(f"\n💰 합계: {total:+,}원  |  비용: -{cost:,}원")
        send("\n".join(lines))
    except Exception as e:
        send(f"⚠️ 손익 조회 오류: {e}")


def cmd_position():
    try:
        pos_file = _POS_FILE
        if not os.path.exists(pos_file):
            send("📦 보유 포지션 없음")
            return
        with open(pos_file, "r", encoding="utf-8") as f:
            pos = json.load(f)
        if not pos:
            send("📦 보유 포지션 없음")
            return

        sys.path.insert(0, _BASE)
        from kis_api import KISApi
        api = KISApi()

        lines = [f"📦 <b>보유 포지션 ({len(pos)}개)</b>\n"]
        for v in pos.values():
            try:
                info    = api.get_price(v["code"])
                current = int(info["stck_prpr"])
                rate    = (current - v["buy_price"]) / v["buy_price"] * 100
                pnl     = (current - v["buy_price"]) * v["qty"]
                icon    = "📈" if pnl >= 0 else "📉"
                lines.append(
                    f"{icon} <b>{v['name']}</b> [{v['strategy_id']}]\n"
                    f"   매입 {v['buy_price']:,} → 현재 {current:,}원\n"
                    f"   {rate:+.2f}%  /  {pnl:+,}원  ({v['qty']}주)"
                )
            except Exception:
                lines.append(f"• {v['name']} {v['qty']}주 @ {v['buy_price']:,}원 (현재가 조회 실패)")
        send("\n".join(lines))
    except Exception as e:
        send(f"⚠️ 포지션 조회 오류: {e}")


def cmd_restart():
    send("🔄 시스템을 재시작합니다...")
    cmd_stop()
    time.sleep(3)
    cmd_start()


def cmd_balance():
    try:
        sys.path.insert(0, _BASE)
        from kis_api import KISApi
        api = KISApi()
        bal = api.get_balance()
        s   = bal["summary"][0] if isinstance(bal["summary"], list) else bal["summary"]

        cash        = int(s.get("dnca_tot_amt", 0))
        total       = int(s.get("tot_evlu_amt", 0))
        asset_chg   = int(s.get("asst_icdc_amt", 0))
        buy_amt     = int(s.get("thdt_buy_amt", 0))
        sell_amt    = int(s.get("thdt_sll_amt", 0))
        sign        = "+" if asset_chg >= 0 else ""

        send(
            f"💼 <b>계좌 잔고</b>\n\n"
            f"총평가금액: {total:,}원\n"
            f"예수금    : {cash:,}원\n"
            f"당일 자산증감: {sign}{asset_chg:,}원\n"
            f"당일 매수금액: {buy_amt:,}원\n"
            f"당일 매도금액: {sell_amt:,}원"
        )
    except Exception as e:
        send(f"⚠️ 잔고 조회 오류: {e}")


def cmd_strategy():
    try:
        from glob import glob
        files = sorted(glob(os.path.join(_BASE, "strategies", "*.json")))
        if not files:
            send("📭 전략 파일이 없습니다.")
            return

        lines = ["📋 <b>전략별 조건</b>\n"]
        for path in files:
            with open(path, "r", encoding="utf-8") as f:
                s = json.load(f)

            enabled  = s.get("enabled", False)
            icon     = "✅" if enabled else "⛔"
            sch      = s.get("schedule", {})
            uni      = s.get("universe", {})
            ent      = s.get("entry", {})
            ext      = s.get("exit", {})

            entry_t  = sch.get("entry_time", "-")
            end_t    = sch.get("entry_end_time", "")
            time_str = f"{entry_t}~{end_t}" if end_t else entry_t
            cont     = "연속" if sch.get("continuous_entry") else "1회"

            min_p = uni.get('min_price')
            max_p = uni.get('max_price')
            if min_p and max_p:
                price_str = f"{min_p:,}~{max_p:,}원"
            elif min_p:
                price_str = f"{min_p:,}원 이상"
            elif max_p:
                price_str = f"{max_p:,}원 이하"
            else:
                price_str = "제한없음"

            lines.append(
                f"{icon} <b>{s.get('name', s['id'])}</b>\n"
                f"  진입: {time_str} ({cont})  |  종목수: {ent.get('max_stocks','-')}개\n"
                f"  변화율: {ent.get('min_change_rate','-')}~{ent.get('max_change_rate','-')}%  |  체결강도: ≥{ent.get('min_execution_strength','-')}\n"
                f"  가격: {price_str}  |  ETF: {'제외' if uni.get('exclude_etf') else '포함'}\n"
                f"  TP: +{ext.get('take_profit','-')}%  /  SL: {ext.get('stop_loss','-')}%"
            )

        send("\n\n".join(lines))
    except Exception as e:
        send(f"⚠️ 전략 조회 오류: {e}")


def cmd_help():
    send(
        "📋 <b>사용 가능한 명령어</b>\n\n"
        "/start — 자동매매 시스템 시작\n"
        "/stop — 모든 전략 강제 종료\n"
        "/restart — 시스템 재시작\n"
        "/status — 실행 상태 + 당일 손익 요약\n"
        "/pnl — 전략별 손익 상세\n"
        "/position — 보유 포지션 + 평가손익\n"
        "/balance — 예수금 및 총평가금액\n"
        "/strategy — 전략별 조건 확인\n"
        "/help — 명령어 목록"
    )


COMMANDS = {
    "/start":    cmd_start,
    "/stop":     cmd_stop,
    "/restart":  cmd_restart,
    "/status":   cmd_status,
    "/pnl":      cmd_pnl,
    "/position": cmd_position,
    "/balance":  cmd_balance,
    "/strategy": cmd_strategy,
    "/help":     cmd_help,
}


def process_update(update: dict):
    msg = update.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip().split()[0]  # 첫 단어만 (봇 멘션 제거)

    if chat_id != CHAT_ID:
        log.warning(f"허가되지 않은 chat_id: {chat_id}")
        return

    if text in COMMANDS:
        log.info(f"명령어 수신: {text}")
        COMMANDS[text]()


def main():
    log.info("텔레그램 봇 시작")
    send("🤖 자동매매 봇이 시작됐습니다.\n/help 로 명령어를 확인하세요.")

    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            res = requests.get(f"{API}/getUpdates", params=params, timeout=35)
            data = res.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                process_update(update)
        except Exception as e:
            log.warning(f"polling 오류: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
