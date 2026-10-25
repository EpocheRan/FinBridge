"""V1 调试入口。

V1 只使用 inline 调度；本模块不提供常驻 external worker。
"""

from __future__ import annotations

import argparse

from screener.bootstrap import build_screener_application


def main() -> int:
    parser = argparse.ArgumentParser(description="Screener inline worker")
    parser.add_argument("--recover", action="store_true", help="恢复未完成任务")
    parser.add_argument("--run-once", default=None, help="执行指定 build_id")
    args = parser.parse_args()

    app = build_screener_application()
    app.start()
    if args.recover:
        app.dispatcher.recover()
    if args.run_once:
        app.dispatcher.worker.run(args.run_once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
