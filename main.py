"""자동매매 시스템 진입점

사용법:
  python main.py --mode live    # 실투자
  python main.py --mode paper   # 모의투자
  python main.py --paper        # 모의투자 단축
"""

import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="자동매매 시스템")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mode", choices=["live", "paper"], help="실행 모드")
    group.add_argument("--paper", action="store_true", help="--mode paper 단축")
    args = parser.parse_args()

    mode = "paper" if args.paper else args.mode
    os.environ["KIS_MODE"] = mode

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from watchdog import run
    run()


if __name__ == "__main__":
    main()
