"""
워치독 프로세스 — 전략 runner 감시 및 자동 재시작
매 60초마다 프로세스 생존 확인, 죽으면 재시작 + 텔레그램 알림
텔레그램 봇은 별도 프로세스로 독립 실행 (Task Scheduler 로그인 시 기동)
"""

import os
import sys
import json
import time
import subprocess
import logging
from datetime import datetime
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine.notifier import send

_BASE     = os.path.dirname(os.path.abspath(__file__))
_PYTHON   = sys.executable
_MODE     = os.getenv("KIS_MODE", "paper")
_LOG_FILE = os.path.join(_BASE, f"watchdog_{_MODE}.log")
_PID_FILE = os.path.join(_BASE, "watchdog.pid")

CHECK_INTERVAL = 60   # 초
START_HOUR     = 850  # 감시 시작 (08:50)
END_HOUR       = 1535 # 감시 종료 (15:35, 결산 후)


def _setup_logger() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("watchdog")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)
    return log


def _load_enabled_strategies() -> list[str]:
    """활성화된 전략 ID 목록 반환"""
    ids = []
    for path in glob(os.path.join(_BASE, "strategies", "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = json.load(f)
            if s.get("enabled", False):
                ids.append(s["id"])
        except Exception:
            pass
    return ids


def _start_process(label: str, args: list, log) -> subprocess.Popen:
    proc = subprocess.Popen(args, cwd=_BASE)
    log.info(f"[재시작] {label} PID={proc.pid}")
    send(f"🔄 <b>[워치독]</b> {label} 프로세스 재시작 (PID {proc.pid})")
    return proc


def _ensure_single_instance(log) -> bool:
    """이미 실행 중인 워치독이 있으면 True 반환 (중복 기동 방지)"""
    try:
        with open(_PID_FILE) as f:
            existing_pid = int(f.read().strip())
        import psutil
        if psutil.pid_exists(existing_pid) and existing_pid != os.getpid():
            log.warning(f"워치독 이미 실행 중 (PID={existing_pid}) → 중복 기동 종료")
            return True
    except Exception:
        pass
    return False


def _send_daily_summary(log):
    """전략별 당일 손익 합산 요약 텔레그램 전송"""
    try:
        pnl_file = os.path.join(_BASE, f"daily_pnl_{_MODE}.json")
        with open(pnl_file, encoding="utf-8") as f:
            data = json.load(f)
        trades = data.get("trades", [])
        today  = data.get("date", "")
        if not trades:
            send(f"📊 <b>[{today} 전략별 손익]</b>\n거래 없음")
            return

        from collections import defaultdict
        by_strat = defaultdict(list)
        for t in trades:
            by_strat[t.get("strategy_id", "?")].append(t)

        lines = [f"📊 <b>[{today} 전략별 손익 요약]</b>\n"]
        total_pnl = 0
        total_cnt = 0
        for sid, ts in sorted(by_strat.items()):
            pnl = sum(t.get("pnl", 0) for t in ts)
            cnt = len(ts)
            emoji = "✅" if pnl >= 0 else "🔴"
            lines.append(f"{emoji} {sid}: {pnl:+,}원 ({cnt}건)")
            total_pnl += pnl
            total_cnt += cnt

        total_emoji = "✅" if total_pnl >= 0 else "🔴"
        lines.append(f"\n{total_emoji} <b>합계: {total_pnl:+,}원 ({total_cnt}건)</b>")
        send("\n".join(lines))
    except Exception as e:
        log.error(f"[일일 요약 전송 실패] {e}")


def run():
    log = _setup_logger()

    if _ensure_single_instance(log):
        return

    log.info("=== 워치독 시작 ===")

    # PID 파일 기록 (telegram_bot 생존 확인용)
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    # 감시 대상: {label: (args, Popen|None)}
    targets: dict[str, dict] = {}

    # 활성 전략 runner
    for sid in _load_enabled_strategies():
        targets[sid] = {
            "args": [_PYTHON, os.path.join(_BASE, "engine", "runner.py"), sid],
            "proc": None,
        }

    # 초기 기동 (장 시간 내에서만)
    now_t = datetime.now().hour * 100 + datetime.now().minute
    if START_HOUR <= now_t < END_HOUR:
        for label, t in targets.items():
            t["proc"] = subprocess.Popen(t["args"], cwd=_BASE)
            log.info(f"[초기 기동] {label} PID={t['proc'].pid}")
        send("🐕 <b>[워치독]</b> 감시 시작 — 전략 프로세스 기동 완료")
    else:
        log.info("장 시간 외 — 초기 기동 생략, 감시만 대기")

    last_heartbeat = 0  # 마지막 heartbeat 시각 (epoch)

    while True:
        time.sleep(CHECK_INTERVAL)
        now      = datetime.now()
        now_t    = now.hour * 100 + now.minute

        # heartbeat: 5분마다 생존 로그
        epoch = now.timestamp()
        if epoch - last_heartbeat >= 300:
            log.info(f"[워치독 생존] 감시 중 — 전략 {list(targets.keys())}")
            last_heartbeat = epoch

        # 장 종료 후 감시 종료
        if now_t >= END_HOUR:
            log.info("15:35 이후 — 워치독 종료")
            send("🐕 <b>[워치독]</b> 오늘 감시 종료")
            _send_daily_summary(log)
            try:
                os.remove(_PID_FILE)
            except OSError:
                pass
            return

        # 장 시작 전 대기
        if now_t < START_HOUR:
            continue

        try:
            # 전략 목록 재로드 (장 중 전략 변경 반영)
            enabled = set(_load_enabled_strategies())

            for label, t in list(targets.items()):
                # 비활성화된 전략은 감시 목록에서 제거
                if label not in enabled:
                    if t["proc"] and t["proc"].poll() is None:
                        t["proc"].terminate()
                    del targets[label]
                    log.info(f"[제거] {label} 비활성화됨")
                    continue

                # 이미 정상 종료된 전략은 재시작하지 않음
                if t.get("gracefully_stopped"):
                    continue

                proc = t["proc"]
                if proc is None or proc.poll() is not None:
                    # 프로세스가 없거나 종료됨
                    exit_code = proc.returncode if proc else None
                    if proc is not None and exit_code == 0:
                        # 정상 종료(exit=0) — 재시작 없음 (15:30 자동 종료 등)
                        log.info(f"[{label}] 정상 종료 (exit=0) → 재시작 없음")
                        t["gracefully_stopped"] = True
                        continue
                    log.warning(f"[감지] {label} 종료됨 (exit={exit_code}) → 재시작")
                    t["proc"] = _start_process(label, t["args"], log)

            # 새로 활성화된 전략 추가
            for sid in enabled:
                if sid not in targets:
                    targets[sid] = {
                        "args": [_PYTHON, os.path.join(_BASE, "engine", "runner.py"), sid],
                        "proc": None,
                    }
                    targets[sid]["proc"] = _start_process(sid, targets[sid]["args"], log)

        except Exception as e:
            log.error(f"[워치독 루프 오류] {e} — 다음 주기에 재시도", exc_info=True)
            send(f"⚠️ <b>[워치독]</b> 루프 오류 발생: {e}")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        logging.getLogger("watchdog").info("수동 종료")
    finally:
        try:
            os.remove(_PID_FILE)
        except OSError:
            pass
