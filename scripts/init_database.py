#!/usr/bin/env python
"""数据库初始化主脚本。

使用方式：

    python scripts/init_database.py
    python scripts/init_database.py --only market,financial
    python scripts/init_database.py --dry-run
    python scripts/init_database.py --host 127.0.0.1 --port 8812

主脚本会：
1. 读取 config.json；
2. 判断当前是 Docker 还是宿主机（Windows/Linux/macOS）；
3. 自动把 ``questdb`` 主机名在宿主机上解析为 ``localhost``；
4. 连接 QuestDB 并按顺序调用子脚本；
5. 任何子脚本失败立即退出，返回非 0 退出码。

程序运行期（MCPserver）不再检查/创建表；Docker 由 ``db-init`` 服务调用本脚本，
非 Docker 环境由用户手动执行一次。
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Windows 控制台默认 GBK，统一用 UTF-8 输出，避免中文日志乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from data_interface.common.config import load_config, resolve_questdb_host  # noqa: E402
from data_interface.common.questdb_client import QuestDBClient  # noqa: E402
from scripts.schemas import (  # noqa: E402
    create_financial_tables,
    create_market_tables,
    create_position_tables,
)

logger = logging.getLogger("Scripts.InitDatabase")

SUB_SCRIPTS = {
    "market": create_market_tables,
    "financial": create_financial_tables,
    "position": create_position_tables,
}


@dataclass
class RuntimePlan:
    os_name: str
    is_docker: bool
    configured_host: str
    resolved_host: str
    run_mode: str
    notes: list[str]


def detect_runtime(config: dict) -> RuntimePlan:
    os_name = platform.system() or os.name
    is_docker = os.path.exists("/.dockerenv") or os.getenv("RUNNING_IN_DOCKER") == "1"
    configured_host = str(config["questdb"]["host"])
    resolved_host = resolve_questdb_host(config)
    notes: list[str] = []

    if is_docker:
        run_mode = "docker"
        if configured_host != "questdb":
            notes.append(
                f"当前在 Docker 内，但 questdb.host={configured_host}；"
                "请确认容器网络能解析该主机名。"
            )
    elif configured_host == "questdb":
        run_mode = "local-fallback"
        notes.append(
            "当前不在 Docker 内，questdb.host=questdb 无法解析，已自动回退到 localhost。"
        )
    else:
        run_mode = "local"

    if os_name == "Windows":
        notes.append("Windows 环境：请确保 QuestDB 已启动并监听 8812 端口。")
    elif os_name == "Linux" and not is_docker:
        notes.append("Linux 宿主机环境：可直接运行本脚本，无需额外包装。")

    return RuntimePlan(
        os_name=os_name,
        is_docker=is_docker,
        configured_host=configured_host,
        resolved_host=resolved_host,
        run_mode=run_mode,
        notes=notes,
    )


def connect_with_retry(
    config: dict, plan: RuntimePlan, host_override: str | None = None, port_override: int | None = None
) -> QuestDBClient:
    cfg = config["questdb"]
    retries = int(cfg.get("connect_retries", 30))
    interval = float(cfg.get("connect_retry_interval_seconds", 2.0))
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        client = QuestDBClient(config, connect=False)
        if client.connect(host=host_override or plan.resolved_host, port=port_override):
            logger.info(
                "QuestDB 连接成功: %s:%s (%s/%s)",
                host_override or plan.resolved_host,
                port_override or cfg["port"],
                plan.os_name,
                plan.run_mode,
            )
            return client
        last_error = ConnectionError("connect failed")
        if attempt < retries:
            logger.warning("QuestDB 连接失败，第 %s/%s 次重试，%s 秒后继续", attempt, retries, interval)
            time.sleep(interval)

    raise ConnectionError(
        f"无法连接 QuestDB ({host_override or plan.resolved_host}:{port_override or cfg['port']})。"
        "请先启动 QuestDB，或修改 config.json 中的 questdb.host/port。"
    ) from last_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FinBridge QuestDB 初始化主脚本")
    parser.add_argument(
        "--only",
        default="market,financial,position",
        help="只运行指定子脚本，逗号分隔：market,financial,position",
    )
    parser.add_argument("--host", default=None, help="覆盖 QuestDB host")
    parser.add_argument("--port", type=int, default=None, help="覆盖 QuestDB PG 端口")
    parser.add_argument("--dry-run", action="store_true", help="只打印 DDL，不执行")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        config = load_config()
    except Exception as exc:
        logger.error("加载配置失败: %s", exc)
        return 2

    plan = detect_runtime(config)
    logger.info("=" * 72)
    logger.info("运行环境: %s | Docker: %s | 模式: %s", plan.os_name, plan.is_docker, plan.run_mode)
    logger.info("配置主机: %s -> 实际主机: %s", plan.configured_host, plan.resolved_host)
    for note in plan.notes:
        logger.info("提示: %s", note)
    logger.info("=" * 72)

    selected = [name.strip() for name in args.only.split(",") if name.strip()]
    unknown = [name for name in selected if name not in SUB_SCRIPTS]
    if unknown:
        logger.error("未知子脚本: %s，可选值: %s", unknown, list(SUB_SCRIPTS))
        return 2

    if args.dry_run:
        for name in selected:
            logger.info("[dry-run] 运行子脚本: %s", name)
            SUB_SCRIPTS[name](None, dry_run=True)
        logger.info("dry-run 完成，未连接数据库。")
        return 0

    try:
        db = connect_with_retry(config, plan, args.host, args.port)
    except Exception as exc:
        logger.error("%s", exc)
        return 3

    try:
        for name in selected:
            logger.info("-" * 72)
            logger.info("执行子脚本: %s", name)
            SUB_SCRIPTS[name](db, dry_run=False)
            logger.info("子脚本完成: %s", name)
    except Exception as exc:
        logger.exception("初始化失败: %s", exc)
        return 4
    finally:
        db.close()

    logger.info("=" * 72)
    logger.info("数据库初始化完成: %s", ", ".join(selected))
    logger.info("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
