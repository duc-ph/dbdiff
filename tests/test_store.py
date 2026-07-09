"""Unit tests for the SQLite store (no Docker)."""

from __future__ import annotations

from pathlib import Path

from dbdiff.row_diff import RowChange
from dbdiff.store import Store


def test_delete_run_cascades(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.sqlite")
    store.init()
    rid = store.create_run("base", "new", "base", "new")
    store.save_table_result(rid, "t", True, ["id"], "changed", 1, 1, 0, None)
    store.add_row_changes(
        rid,
        "t",
        [RowChange("modified", {"id": 1}, old={"id": 1, "x": "a"}, new={"id": 1, "x": "b"}, changed_columns=["x"])],
    )
    store.set_status(rid, "done")

    assert any(r["id"] == rid for r in store.list_runs())
    assert store.count_row_changes(rid, "t") == 1
    assert len(store.get_table_results(rid)) == 1

    assert store.delete_run(rid) is True

    assert all(r["id"] != rid for r in store.list_runs())
    assert store.count_row_changes(rid, "t") == 0   # row_changes removed
    assert store.get_table_results(rid) == []        # table_results removed
    assert store.delete_run(rid) is False            # already gone
