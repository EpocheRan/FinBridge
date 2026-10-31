"""报告模块的 QuestDB REST 访问（唯一入口，便于测试替换）。"""

from __future__ import annotations

import pandas as pd
import requests

from data_interface.common.config import get_config, resolve_questdb_host

_cfg = get_config()
BASE = f"http://{resolve_questdb_host(_cfg)}:{_cfg['questdb'].get('rest_port', 9000)}"


def query(sql: str, timeout: int = 300) -> pd.DataFrame:
    resp = requests.get(f"{BASE}/exec", params={"query": sql}, timeout=timeout)
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    # DDL / INSERT 不返回 dataset，返回空表而不是 KeyError
    dataset = payload.get("dataset") or []
    columns = [item["name"] for item in payload.get("columns", [])]
    return pd.DataFrame(dataset, columns=columns)
