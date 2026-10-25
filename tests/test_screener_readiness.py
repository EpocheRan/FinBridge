"""BaoStock 共享会话与"缺基准不影响行完整性"的回归测试。"""

from __future__ import annotations

import sys
import types

import numpy as np
import pandas as pd

from screener.domain.factor_engine import DailyFactorConfig, FactorEngine


# --------------------------------------------------------------------- BaoStock
class _FakeResult:
    def __init__(self, code: str = "0", msg: str = "success"):
        self.error_code = code
        self.error_msg = msg


def _install_fake_baostock(monkeypatch):
    calls = {"login": 0, "logout": 0}

    fake = types.ModuleType("baostock")
    fake.login = lambda: (calls.__setitem__("login", calls["login"] + 1), _FakeResult())[1]
    fake.logout = lambda: (calls.__setitem__("logout", calls["logout"] + 1), _FakeResult())[1]
    fake.query_trade_dates = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "baostock", fake)
    return calls


def test_shared_baostock_client_logs_in_once(monkeypatch):
    calls = _install_fake_baostock(monkeypatch)
    import data_interface.common.baostock_client as module

    monkeypatch.setattr(module, "_shared_client", None)
    first = module.get_shared_baostock_client()
    second = module.get_shared_baostock_client()

    assert first is second
    assert calls["login"] == 1, "重复获取不应重复登录（会话是全局的）"
    assert first.is_logged_in()


def test_baostock_client_has_no_destructive_destructor(monkeypatch):
    """__del__ 里 logout 会把其它调用方的全局 socket 关掉。"""
    calls = _install_fake_baostock(monkeypatch)
    import data_interface.common.baostock_client as module

    client = module.BaoStockClient()
    del client

    assert calls["logout"] == 0


# ------------------------------------------------------------------ 因子完整性
def _frames(days: int, benchmark: pd.DataFrame | None):
    dates = pd.bdate_range("2025-01-01", periods=days)
    close = pd.Series(np.linspace(10.0, 20.0, days))
    raw = pd.DataFrame(
        {
            "ts": dates,
            "code": "600519",
            "open": close.to_numpy() * 0.99,
            "high": close.to_numpy() * 1.01,
            "low": close.to_numpy() * 0.98,
            "close": close.to_numpy(),
            "volume": 1_000_000,
            "amount": 10_000_000,
            "turnover": 1.0,
        }
    )
    reference = pd.DataFrame([{"code": "600519", "total_share": 1e9, "float_share": 1e9}])
    return raw, benchmark, reference


def test_missing_benchmark_does_not_mark_rows_partial():
    """基准缺失时 beta 为 NaN，但 252 根以上历史的行仍应视为完整。

    否则查询默认 `is_partial = false` 会把整个市场过滤掉（实测 0 行）。
    """
    raw, _, reference = _frames(300, None)
    frame = FactorEngine(DailyFactorConfig()).compute_daily(raw, None, reference)

    last = frame.iloc[-1]
    assert pd.isna(last["beta_252"])
    assert bool(last["full_ready"]) is True
    assert bool(last["is_partial"]) is False


def test_short_history_still_partial_even_with_benchmark():
    raw, _, reference = _frames(30, None)
    benchmark = pd.DataFrame(
        {"ts": pd.bdate_range("2025-01-01", periods=30), "close": np.linspace(100, 110, 30)}
    )
    frame = FactorEngine(DailyFactorConfig()).compute_daily(raw, benchmark, reference)
    assert bool(frame.iloc[-1]["is_partial"]) is True
