"""评估内容（F）：profile 的存取与解析。

profile 只描述「评估什么」：勾选哪些评估项、每项的参数、判定阈值、参照因子。
股票池 / 区间 / 成本 / horizon 是运行参数，不属于 profile——同一套 profile 可以
跑 200 池、中证500、全 A 做横向对比。

存储：QuestDB ``eval_profile``（版本化，不覆盖旧版）；DB 不可用时回落到内置默认。
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd

from research.report.db import query as _query

TABLE = "eval_profile"

#: 内置 profile：DB 首次初始化时灌入，也是 DB 不可用时的兜底
BUILTIN: dict[str, dict] = {
    "default": {
        "description": "深评：单因子 HTML 报告的完整评估项",
        "items": (
            "coverage", "ic", "spread", "turnover",
            "mono", "decay", "assertions", "reference_rho",
            "stability_by_year", "rating",
        ),
        "reference": "liquidity.turnover_std_bias_42_252",
    },
    "quick": {
        "description": "快筛：批量扫描用的最小集合",
        "items": ("coverage", "ic", "spread", "turnover", "reference_rho"),
        "reference": "liquidity.turnover_std_bias_42_252",
    },
}

DDL = f"""CREATE TABLE IF NOT EXISTS "{TABLE}" (
    updated_at TIMESTAMP,
    name SYMBOL,
    version INT,
    is_default BOOLEAN,
    description STRING,
    items STRING,
    item_params STRING,
    thresholds STRING,
    reference SYMBOL,
    profile_hash SYMBOL,
    enabled BOOLEAN
) TIMESTAMP(updated_at) PARTITION BY YEAR WAL
DEDUP UPSERT KEYS(updated_at, name, version)"""


def _hash(items: tuple[str, ...], params: dict, thresholds: dict, reference: str) -> str:
    payload = json.dumps(
        {"items": sorted(items), "params": params, "thresholds": thresholds, "reference": reference},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def ensure_table() -> None:
    _query(DDL, timeout=60)


def _latest_rows() -> pd.DataFrame:
    frame = _query(
        f"select name, version, items, item_params, thresholds, reference, "
        f"description, profile_hash from {TABLE} "
        "latest on updated_at partition by name",
        timeout=60,
    )
    return frame


def seed() -> list[str]:
    """把内置 profile 灌进库（只灌缺失的 name）。"""
    ensure_table()
    try:
        existing = {str(row["name"]) for _, row in _latest_rows().iterrows()}
    except Exception:  # noqa: BLE001
        existing = set()
    written: list[str] = []
    for name, payload in BUILTIN.items():
        if name in existing:
            continue
        save(
            name,
            items=tuple(payload["items"]),
            reference=str(payload["reference"]),
            description=str(payload["description"]),
        )
        written.append(name)
    return written


def load(name: str = "default") -> dict:
    """读 profile；DB 不可用或不存在时回落内置默认。"""
    try:
        frame = _latest_rows()
        rows = frame[frame["name"].astype(str) == str(name)]
        if not rows.empty:
            row = rows.iloc[-1]
            items = tuple(str(x) for x in json.loads(row["items"]))
            return {
                "name": str(row["name"]),
                "version": int(row["version"]),
                "items": items,
                "item_params": json.loads(row["item_params"] or "{}"),
                "thresholds": json.loads(row["thresholds"] or "{}"),
                "reference": str(row["reference"] or ""),
                "description": str(row["description"] or ""),
                "profile_hash": str(row["profile_hash"] or ""),
                "source": "db",
            }
    except Exception:  # noqa: BLE001 - DB 不可用不阻断评估
        pass
    if name in BUILTIN:
        payload = BUILTIN[name]
        items = tuple(payload["items"])
        return {
            "name": name,
            "version": 0,
            "items": items,
            "item_params": {},
            "thresholds": {},
            "reference": str(payload["reference"]),
            "description": str(payload["description"]),
            "profile_hash": _hash(items, {}, {}, str(payload["reference"])),
            "source": "builtin",
        }
    raise KeyError(f"未知 profile: {name!r}；可用: {sorted(BUILTIN)}（或库内自定义）")


def save(
    name: str,
    *,
    items: tuple[str, ...],
    item_params: dict | None = None,
    thresholds: dict | None = None,
    reference: str = "",
    description: str = "",
) -> dict:
    """写入新版 profile（version = 当前最大 + 1，不覆盖旧版）。"""
    ensure_table()
    item_params = item_params or {}
    thresholds = thresholds or {}
    try:
        frame = _latest_rows()
        rows = frame[frame["name"].astype(str) == str(name)]
        version = int(rows["version"].max()) + 1 if not rows.empty else 1
    except Exception:  # noqa: BLE001
        version = 1
    digest = _hash(tuple(items), item_params, thresholds, reference)
    now = pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    payload = json.dumps(list(items), ensure_ascii=False).replace("'", "''")
    params = json.dumps(item_params, ensure_ascii=False, sort_keys=True).replace("'", "''")
    limits = json.dumps(thresholds, ensure_ascii=False, sort_keys=True).replace("'", "''")
    desc = str(description).replace("'", "''")
    _query(
        f'insert into {TABLE} values ('
        f"'{now}', '{name}', {version}, false, '{desc}', '{payload}', "
        f"'{params}', '{limits}', '{reference}', '{digest}', true)",
        timeout=60,
    )
    return {"name": name, "version": version, "profile_hash": digest}


def list_profiles() -> pd.DataFrame:
    try:
        frame = _latest_rows()
    except Exception:  # noqa: BLE001
        frame = pd.DataFrame()
    if frame.empty:
        return pd.DataFrame(
            [
                {"name": name, "version": 0, "items": len(payload["items"]), "source": "builtin"}
                for name, payload in BUILTIN.items()
            ]
        )
    return frame.assign(source="db")


__all__ = ["BUILTIN", "TABLE", "ensure_table", "list_profiles", "load", "save", "seed"]
