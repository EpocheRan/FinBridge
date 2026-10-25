"""响应信封与序列化工具的测试。"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from data_interface.api.utils import dataframe_to_records, fail, from_exception, json_safe, ok
from data_interface.common.exceptions import DataNotFoundError, RateLimitExceededError


def test_ok_uses_single_data_key():
    response = ok("done", {"price": 1.5})
    assert response["success"] is True
    assert response["data"] == {"price": 1.5}
    # 不再同时输出 result，避免载荷翻倍
    assert "result" not in response


def test_ok_with_warnings():
    response = ok("done", {"a": 1}, warnings=["缺少复权因子"])
    assert response["warnings"] == ["缺少复权因子"]


def test_fail_structure():
    response = fail("boom", "InvalidArgument", "换个参数")
    assert response["success"] is False
    assert response["error_details"]["type"] == "InvalidArgument"
    assert response["error_details"]["suggestion"] == "换个参数"


def test_from_exception_maps_domain_errors():
    response = from_exception(RateLimitExceededError("太快了", interface="income", limit=200))
    assert response["success"] is False
    details = response["error_details"]
    assert details["type"] == "RateLimitExceededError"
    assert details["interface"] == "income"
    assert "限流" in details["suggestion"]


def test_from_exception_for_not_found():
    response = from_exception(DataNotFoundError("没数据"))
    assert response["error_details"]["type"] == "DataNotFoundError"


def test_json_safe_handles_nan_and_datetime():
    payload = {
        "nan": float("nan"),
        "inf": float("inf"),
        "when": datetime(2024, 1, 2, 3, 4, 5),
        "np_float": np.float64(1.5),
        "nested": [np.int64(2), None],
    }
    safe = json_safe(payload)
    assert safe["nan"] is None
    assert safe["inf"] is None
    assert safe["when"] == "2024-01-02T03:04:05"
    assert safe["np_float"] == 1.5
    assert safe["nested"] == [2, None]


def test_json_safe_does_not_choke_on_objects():
    assert json_safe({"series": pd.Series([1, 2])}) is not None


def test_dataframe_to_records_drops_requested_columns():
    frame = pd.DataFrame(
        {"close": [1.0, 2.0], "raw_close": [1.0, 2.0]},
        index=pd.date_range("2024-01-01", periods=2, freq="D"),
    )
    records = dataframe_to_records(frame, drop_columns=("raw_close",))
    assert records and "raw_close" not in records[0]
    assert "ts" in records[0]
