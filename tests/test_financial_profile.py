"""`sync_profile` 的列表状态 / 退市日期 / 已有字段保留回归。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from data_interface.common.timeutil import now_cn

from data_interface.financial.service import FinancialDataService


class FakeRepo:
    def __init__(self, existing=None):
        self.profile = existing
        self.upserted = None
        self.boards = []

    def get_profile(self, code):
        return self.profile

    def upsert_profile(self, rows):
        self.upserted = dict(rows[0])
        self.profile = dict(rows[0])
        return 1

    def upsert_stock_boards(self, rows):
        self.boards.extend(rows)
        return len(rows)


class FakeTushare:
    def __init__(self, rows_by_status):
        self.rows_by_status = rows_by_status

    def fetch_stock_basic(self, *, code=None, list_status=None, **kwargs):
        return self.rows_by_status.get(list_status, [])

    def fetch_daily_basic(self, *args, **kwargs):
        return []


class FakeAkshare:
    def fetch_profile(self, code):
        return None


def _service(repo, tushare):
    service = object.__new__(FinancialDataService)
    service.config = {"financial": {}}
    service.repository = repo
    service._tushare = tushare
    service.akshare = FakeAkshare()
    service._write_sync_state = lambda *args, **kwargs: None
    return service


def test_active_profile_gets_list_status_l_even_if_source_omits_it():
    repo = FakeRepo()
    tushare = FakeTushare(
        {"L": [{"ts_code": "600519.SH", "name": "贵州茅台", "list_date": "20010827"}]}
    )
    service = _service(repo, tushare)
    profile = service.sync_profile("600519")
    assert profile["list_status"] == "L"


def test_delisted_profile_uses_d_status_and_delist_date():
    repo = FakeRepo()
    tushare = FakeTushare(
        {
            "D": [
                {
                    "ts_code": "600001.SH",
                    "name": "邯郸钢铁(退)",
                    "list_status": "D",
                    "list_date": "19980122",
                    "delist_date": "20091229",
                }
            ]
        }
    )
    service = _service(repo, tushare)
    profile = service.sync_profile("600001")
    assert profile["list_status"] == "D"
    assert profile["delist_date"] == datetime(2009, 12, 29)


def test_existing_profile_fields_are_not_overwritten_by_nulls():
    repo = FakeRepo(
        {
            "code": "600519",
            "name": "贵州茅台",
            "area": "贵州",
            "list_status": "L",
            "delist_date": None,
        }
    )
    tushare = FakeTushare(
        {"L": [{"ts_code": "600519.SH", "name": "贵州茅台", "list_status": "L"}]}
    )
    service = _service(repo, tushare)
    profile = service.sync_profile("600519")
    assert profile["area"] == "贵州"
    assert profile["list_status"] == "L"


def test_valuation_empty_result_still_tries_akshare_fallback():
    service = object.__new__(FinancialDataService)
    service.config = {"financial": {}}
    service._tushare = SimpleNamespace(fetch_daily_basic=lambda *args, **kwargs: [])
    service.akshare = SimpleNamespace(
        fetch_valuation=lambda *args, **kwargs: [{"ts": datetime(2024, 1, 2), "code": "600519"}]
    )
    service.repository = SimpleNamespace(upsert_valuation=lambda rows: len(rows))
    service._write_sync_state = lambda *args, **kwargs: None

    count = service.sync_valuation(
        "600519", start_date="2024-01-01", end_date="2024-01-02", force=True
    )
    assert count == 1


def test_incomplete_profile_is_retried_before_ttl():
    """同步成功但 total_mv/total_share 为空时，不能被 24h TTL 锁死。"""
    service = object.__new__(FinancialDataService)
    fresh_state = {"updated_at": now_cn() - timedelta(minutes=5), "status": "OK"}
    service.repository = SimpleNamespace(get_sync_state=lambda code, ds: fresh_state)

    incomplete = {"name": "贵州茅台", "total_mv": None, "total_share": None}
    complete = {"name": "贵州茅台", "total_mv": 1.0, "total_share": 1.0}

    # 字段不全但刚同步过 -> 半小时内不再重试（节流）
    assert service._profile_is_fresh("600519", incomplete) is True
    assert service._profile_is_fresh("600519", complete) is True

    old_state = {"updated_at": now_cn() - timedelta(minutes=45), "status": "OK"}
    service.repository = SimpleNamespace(get_sync_state=lambda code, ds: old_state)
    # 字段不全且已过重试间隔 -> 允许重新抓取
    assert service._profile_is_fresh("600519", incomplete) is False
    # 字段完整 -> 仍在 24h TTL 内
    assert service._profile_is_fresh("600519", complete) is True
