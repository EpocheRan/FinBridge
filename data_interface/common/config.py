"""配置文件加载。

所有可变配置（QuestDB、Tushare token、限流、服务端口）统一放在项目根目录
``config.json``。环境变量只用于少量部署覆盖，便于 Docker/本地切换。

安全约定：``config.json`` 已被 ``.gitignore`` 排除，仓库只保留
``config.example.json``；CI/生产用环境变量 ``TUSHARE_TOKEN`` 等覆盖。
"""

from __future__ import annotations

import copy
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.json"


def project_path(*parts: str) -> Path:
    """返回项目根目录下的路径。"""
    return PROJECT_ROOT.joinpath(*parts)


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@lru_cache(maxsize=8)
def _load_config_cached(path_key: str) -> dict[str, Any]:
    config_path = Path(path_key) if path_key else DEFAULT_CONFIG_PATH
    if not config_path.exists() and not path_key:
        # 首次部署常见：只有 example，没有真实配置
        raise ConfigurationError(
            f"配置文件不存在: {config_path}。请复制 config.example.json 为 config.json 后填写。"
        )
    if not config_path.exists():
        raise ConfigurationError(f"配置文件不存在: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as fh:
            config = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"配置文件不是合法 JSON: {config_path}: {exc}") from exc

    overrides: dict[str, Any] = {}
    if os.getenv("QUESTDB_HOST"):
        overrides.setdefault("questdb", {})["host"] = os.environ["QUESTDB_HOST"]
    if os.getenv("QUESTDB_PORT"):
        overrides.setdefault("questdb", {})["port"] = int(os.environ["QUESTDB_PORT"])
    if os.getenv("TUSHARE_TOKEN"):
        overrides.setdefault("tushare", {})["token"] = os.environ["TUSHARE_TOKEN"]

    if overrides:
        config = _deep_merge(config, overrides)

    for section in ("questdb", "tushare", "market", "financial"):
        if section not in config:
            raise ConfigurationError(f"配置文件缺少 '{section}' 配置段")

    return config


def load_config(path: str | None = None) -> dict[str, Any]:
    """读取配置（按路径缓存，返回深拷贝避免调用方互相污染）。"""
    path_key = str(Path(path).expanduser().resolve()) if path else ""
    return copy.deepcopy(_load_config_cached(path_key))


def get_config() -> dict[str, Any]:
    return load_config()


def resolve_questdb_host(config: dict[str, Any] | None = None) -> str:
    """解析 QuestDB 主机名。

    Docker 内使用 ``questdb``；宿主机执行脚本时自动回退到 ``localhost``。
    """
    cfg = config or get_config()
    host = str(cfg["questdb"]["host"])
    if host == "questdb" and not _running_in_docker():
        return "localhost"
    return host


def _running_in_docker() -> bool:
    return os.path.exists("/.dockerenv") or os.getenv("RUNNING_IN_DOCKER") == "1"


def masked_tushare_token(token: str | None) -> str:
    if not token:
        return "<empty>"
    if len(token) <= 12:
        return token[:4] + "***"
    return token[:8] + "..." + token[-4:]
