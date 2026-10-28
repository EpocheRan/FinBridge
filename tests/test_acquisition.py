"""acquisition：Scope 与编排器离线测试。"""

from __future__ import annotations

from datetime import date

import pytest

from acquisition.orchestrator import DATASET_ORDER, Orchestrator, Scope
from acquisition.syncers import REAL_SYNCERS


class FakeProgress:
    def __init__(self):
        self.events = []

    def start(self, dataset, total, message=None):
        self.events.append(("start", dataset, total))

    def update(self, event):
        self.events.append(("update", event.dataset, event.done, event.total))

    def finish(self, dataset, total, message=None):
        self.events.append(("finish", dataset, total, message))


def make_scope(**kwargs):
    payload = dict(start=date(2026, 10, 1), end=date(2026, 10, 27))
    payload.update(kwargs)
    return Scope(**payload)


def test_all_order_covers_screener_and_factors():
    assert "screener" in DATASET_ORDER
    assert "factors" in DATASET_ORDER
    assert DATASET_ORDER.index("factors") < DATASET_ORDER.index("screener")
    assert set(REAL_SYNCERS) == set(DATASET_ORDER)


def test_scope_sorts_codes_and_validates_range():
    scope = make_scope(codes=("600519", "000001", "600519"))
    assert scope.codes == ("000001", "600519")
    with pytest.raises(ValueError):
        Scope(start=date(2026, 10, 27), end=date(2026, 10, 1))


def test_orchestrator_runs_in_order_and_reports_progress():
    calls = []
    progress = FakeProgress()

    def a(scope):
        calls.append("a")
        return {"rows_written": 3}

    def b(scope):
        calls.append("b")
        return {"rows_written": 4}

    orchestrator = Orchestrator(
        syncers={"a": a, "b": b},
        order=("a", "b"),
        progress=progress,
    )
    result = orchestrator.run(make_scope())
    assert calls == ["a", "b"]
    assert result["errors"] == []
    assert result["results"]["a"]["rows_written"] == 3
    assert [item[0] for item in progress.events] == [
        "start",
        "finish",
        "start",
        "finish",
    ]


def test_orchestrator_selected_datasets_and_errors():
    def ok(scope):
        return {"rows_written": 1}

    def bad(scope):
        raise RuntimeError("boom")

    orchestrator = Orchestrator(
        syncers={"a": ok, "b": bad},
        order=("a", "b"),
        progress=FakeProgress(),
    )
    result = orchestrator.run(make_scope(datasets=("b", "a")))
    assert result["results"]["b"]["error"] == "boom"
    assert result["results"]["a"]["rows_written"] == 1
    assert result["errors"][0]["dataset"] == "b"


def test_unknown_dataset_rejected():
    orchestrator = Orchestrator(
        syncers={"a": lambda scope: {}},
        order=("a",),
        progress=FakeProgress(),
    )
    with pytest.raises(ValueError):
        orchestrator.run(make_scope(datasets=("nope",)))
