"""模块依赖方向检查（M0 起生效）。

规则（architecture_v2.md）：
- factors 不得 import screener / backtest / data_interface /
  factor_engine / acquisition / pybroker / psycopg2。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACTORS = ROOT / "factors"

FORBIDDEN_ROOTS = {
    "screener",
    "indicator",
    "backtest",
    "data_interface",
    "factor_engine",
    "acquisition",
    "pybroker",
    "psycopg2",
}

#: M1 起不再有遗留例外
LEGACY_ALLOWLIST: set[str] = set()


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_factors_has_no_forbidden_imports():
    violations: list[str] = []
    for path in sorted(FACTORS.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in LEGACY_ALLOWLIST:
            continue
        roots = _import_roots(path) & FORBIDDEN_ROOTS
        if roots:
            violations.append(f"{rel}: {sorted(roots)}")
    assert not violations, "factors 出现反向依赖:\n" + "\n".join(violations)


ENGINE_FORBIDDEN = {"screener", "backtest", "indicator", "acquisition"}


def test_factor_engine_does_not_import_screener_or_acquisition():
    violations: list[str] = []
    for path in sorted((ROOT / "factor_engine").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        roots = _import_roots(path) & ENGINE_FORBIDDEN
        if roots:
            violations.append(f"{rel}: {sorted(roots)}")
    assert not violations, "factor_engine 出现反向依赖:\n" + "\n".join(violations)
