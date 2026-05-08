"""자동매매 런처 — 활성화된 전략을 병렬 프로세스로 실행"""

import json
import os
import sys
import signal
import logging
import subprocess
import traceback
from glob import glob
from datetime import datetime

from kis_api import KISApi
from engine.monitor import load_daily_pnl
from engine.notifier import send, notify_error

_BASE     = os.path.dirname(os.path.abspath(__file__))
LOG_FILE  = os.path.join(_BASE, "launcher.log")
_PID_FILE = os.path.join(_BASE, "launcher.pid")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("launcher")

_children: list[subprocess.Popen] = []


def _check_already_running() -> bool:
    """이미 실행 중인 런처가 있으면 True 반환"""
    if not os.path.exists(_PID_FILE):
        return False
    try:
        with open(_PID_FILE, "r") as f:
            pid = int(f.read().strip())
        import psutil
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            if "python" in proc.name().lower():
                return True
    except Exception:
        pass
    return False


def _write_pid():
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _remove_pid():
    try:
        os.remove(_PID_FILE)
    except Exception:
        pass


def _shutdown(signum, frame):
    log.info("종료 신호 수신 — 자식 프로세스 종료 중...")
    for p in _children:
        p.terminate()
    _remove_pid()
    send(f"🛑 자동매매 강제 종료  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    sys.exit(0)


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


def load_enabled_strategies() -> list[dict]:
    pattern = os.path.join(_BASE, "strategies", "*.json")
    result = []
    for path in glob(pattern):
        with open(path, "r", encoding="utf-8") as f:
            s = json.load(f)
        if s.get("enabled", False):
            result.append(s)
    result.sort(key=lambda s: s["schedule"].get("entry_time", s["schedule"].get("start_time", "")))
    return result


def send_total_summary(today: str):
    """전체 전략 통합 결산 전송"""
    try:
        api      = KISApi()
        pnl_data = load_daily_pnl()
        bal      = api.get_balance()
        s        = bal["summary"][0] if isinstance(bal["summary"], list) else bal["summary"]
        cash     = int(s.get("dnca_tot_amt", 0))
        total    = int(s.get("tot_evlu_amt", 0))

        total_pnl  = pnl_data.get("total_pnl", 0)
        total_cost = pnl_data.get("total_cost", 0)
        trades     = pnl_data.get("trades", [])

        lines = [f"📊 <b>전체 통합 결산 ({today})</b>\n"]

        if trades:
            from collections import defaultdict
            groups = defaultdict(list)
            for tr in trades:
                groups[tr.get("strategy_id", "기타")].append(tr)
            for sid, group in groups.items():
                group_pnl = sum(t["pnl"] for t in group)
                sign = "✅" if group_pnl >= 0 else "🔴"
                lines.append(f"{sign} [{sid}]  소계: {group_pnl:+,}원")
        else:
            lines.append("거래 없음")

        pnl_sign = "+" if total_pnl >= 0 else ""
        lines.append(f"\n💰 당일 순실현손익: {pnl_sign}{total_pnl:,}원")
        lines.append(f"📋 당일 부대비용  : -{total_cost:,}원 (수수료+세금)")
        lines.append(f"💼 총평가금액     : {total:,}원")
        lines.append(f"🏦 예수금         : {cash:,}원")

        send("\n".join(lines))
        log.info("통합 결산 전송 완료")
    except Exception as e:
        log.error(f"통합 결산 전송 실패: {e}")
        notify_error(f"통합 결산 전송 실패: {e}")


def main():
    if _check_already_running():
        log.warning("이미 실행 중인 런처가 있습니다. 중복 실행 방지로 종료합니다.")
        sys.exit(0)
    _write_pid()

    strategies = load_enabled_strategies()
    if not strategies:
        log.error("활성화된 전략이 없습니다.")
        send("⚠️ 활성화된 전략이 없습니다.")
        return

    api = KISApi()
    now = datetime.now()
    if not api.is_trading_day(now):
        msg = f"📵 오늘({now.date()})은 비거래일입니다."
        log.info(msg)
        send(msg)
        return

    # 체결강도 API 점검
    def _check_execution_strength() -> str:
        try:
            samples = [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("005380", "현대차")]
            results = []
            for code, name in samples:
                v = api.get_execution_strength(code)
                results.append(f"{name}: {v}")
            non_zero = [v for _, v in [(c, api.get_execution_strength(c)) for c, _ in samples] if v > 0]
            status = "✅ 정상" if non_zero else "❌ 비정상(0.0)"
            return status + "\n" + "\n".join(f"  {r}" for r in results)
        except Exception as e:
            return f"❌ 점검 오류: {e}"

    strength_check = _check_execution_strength()
    log.info(f"체결강도 점검: {strength_check}")

    # 시작 메시지
    names = "\n".join(
        f"  • {s['name']} ({s['schedule'].get('entry_time', s['schedule'].get('start_time', ''))})"
        for s in strategies
    )
    log.info(f"자동매매 시작 | {len(strategies)}개 전략")
    send(
        f"🚀 <b>자동매매 시작</b>  {now.strftime('%Y-%m-%d %H:%M')}\n"
        f"활성 전략 {len(strategies)}개:\n{names}\n\n"
        f"📡 체결강도 점검: {strength_check}"
    )

    # 전략별 프로세스 실행 (type에 따라 runner 분기)
    runner_map = {
        "tick": os.path.join(_BASE, "engine", "tick_runner.py"),
    }
    default_runner = os.path.join(_BASE, "engine", "runner.py")
    for s in strategies:
        runner = runner_map.get(s.get("type"), default_runner)
        proc = subprocess.Popen(
            [sys.executable, runner, s["id"]],
            cwd=_BASE,
        )
        _children.append(proc)
        log.info(f"  [{s['id']}] PID={proc.pid} 시작 (runner={os.path.basename(runner)})")

    # 모든 전략 프로세스 종료 대기
    for proc in _children:
        proc.wait()
    log.info("모든 전략 프로세스 종료 완료")

    # 통합 결산 전송
    send_total_summary(str(now.date()))
    _remove_pid()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error(f"런처 오류:\n{traceback.format_exc()}")
        notify_error(f"런처 오류: {e}")
        _remove_pid()
