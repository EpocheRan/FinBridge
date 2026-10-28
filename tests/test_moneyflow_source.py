"""分档数据源（Tushare）离线测试：单位换算、代码规范化、空值处理。"""

from __future__ import annotations

import pandas as pd
import pytest

from data_interface.moneyflow.models import CANONICAL_COLUMNS
from data_interface.moneyflow.sources.tushare_source import TushareMoneyflowSource


def _raw_frame() -> pd.DataFrame:
    columns = ["ts_code", "trade_date"]
    for bucket in ("sm", "md", "lg", "elg"):
        columns += [
            f"buy_{bucket}_vol",
            f"buy_{bucket}_amount",
            f"sell_{bucket}_vol",
            f"sell_{bucket}_amount",
        ]
    columns += ["net_mf_vol", "net_mf_amount"]
    return pd.DataFrame(
        [
            [
                "600519.SH",
                "20261027",
                1, 7.44, 3, 46.75,
                15333, 207219.31, 16940, 228953.63,
                16514, 223246.99, 12629, 170700.84,
                6868, 92829.85, 9142, 123602.37,
                6255, 84534.30,
            ],
            [
                "000001.SZ",
                "20261027",
                10, 20.0, 20, 40.0,
                100, 200.0, 110, 210.0,
                120, 300.0, 130, 310.0,
                140, 400.0, 150, 410.0,
                5, 250.0,
            ],
        ],
        columns=columns,
    )


class FakePro:
    def __init__(self, frame: pd.DataFrame | None = None):
        self.frame = frame if frame is not None else _raw_frame()

    def moneyflow(self, **kwargs):
        return self.frame.copy()


def test_normalizes_units_codes_and_source():
    source = TushareMoneyflowSource(pro=FakePro())
    frame = source.fetch_by_date("2026-10-27")

    assert list(frame.columns) == CANONICAL_COLUMNS
    assert len(frame) == 2
    row = frame[frame["code"] == "600519"].iloc[0]
    # 手 -> 股
    assert row["buy_sm_vol"] == 100
    assert row["net_mf_vol"] == 625500
    # 万元 -> 元
    assert row["buy_sm_amount"] == pytest.approx(74_400.0)
    assert row["net_mf_amount"] == pytest.approx(845_343_000.0)
    assert str(row["source"]) == "tushare.moneyflow"
    assert str(row["source_version"]) == "tushare.moneyflow.v1"
    assert pd.Timestamp(row["ts"]) == pd.Timestamp("2026-10-27")
    assert row["market"] == "MLCN"


def test_fetch_by_code_uses_ts_code():
    class RecordingPro(FakePro):
        def __init__(self):
            super().__init__()
            self.calls = []

        def moneyflow(self, **kwargs):
            self.calls.append(kwargs)
            return super().moneyflow()

    pro = RecordingPro()
    source = TushareMoneyflowSource(pro=pro)
    source.fetch_by_code("600519", "2026-10-01", "2026-10-27")
    assert pro.calls[0]["ts_code"] == "600519.SH"
    assert pro.calls[0]["start_date"] == "20261001"
    assert pro.calls[0]["end_date"] == "20261027"


def test_empty_result_keeps_canonical_columns():
    source = TushareMoneyflowSource(pro=FakePro(pd.DataFrame()))
    frame = source.fetch_by_date("2026-10-27")
    assert frame.empty
    assert list(frame.columns) == CANONICAL_COLUMNS


def test_invalid_date_rejected():
    source = TushareMoneyflowSource(pro=FakePro())
    with pytest.raises(ValueError):
        source.fetch_by_date("2026/10/27")
