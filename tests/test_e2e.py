"""End-to-end test: restore two synthetic .sql.gz dumps into an ephemeral
MariaDB and assert the classified diff. Requires a usable Docker daemon."""

from __future__ import annotations

import gzip
import shutil
import subprocess
from pathlib import Path

import pytest

from dbdiff.config import Config
from dbdiff.engine import run_diff
from dbdiff.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "ps"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="docker daemon not available")


@pytest.fixture
def dumps(tmp_path: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for name in ("base", "new"):
        gz = tmp_path / f"{name}.sql.gz"
        with open(FIXTURES / f"{name}.sql", "rb") as fi, gzip.open(gz, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        out[name] = gz
    return out


def test_full_diff(tmp_path: Path, dumps: dict[str, Path]) -> None:
    store = Store(tmp_path / "results.sqlite")
    store.init()
    cfg = Config.load(FIXTURES / "ignore_updated.yml")

    summary = run_diff(str(dumps["base"]), str(dumps["new"]), cfg, store)

    # Totals
    assert summary.rows_inserted == 5   # users:1 + orders:1 + tags:1 + sessions:2
    assert summary.rows_modified == 3   # users:2 (email + phone backfill) + orders:1
    assert summary.rows_deleted == 2    # users:1 + tags:1
    assert summary.tables_added == 1    # sessions
    assert summary.tables_removed == 0
    assert summary.keyless_tables == 1  # tags (diffed by full-row hash)
    assert summary.has_changes

    export = store.export_run(summary.run_id)
    tables = {t["table_name"]: t for t in export["tables"]}

    # users — ignoring updated_at: id2's email changed, and id3 gets the newly
    # added `phone` column backfilled (∅ → value). id1 (updated_at-only) must
    # NOT appear; id5 inserted, id4 deleted.
    users = tables["users"]
    assert (users["inserted"], users["modified"], users["deleted"]) == (1, 2, 1)
    mods = {c["key"]["id"]: c for c in users["changes"] if c["change_type"] == "modified"}
    assert set(mods) == {2, 3}
    assert mods[2]["changed"] == ["email"]
    assert mods[3]["changed"] == ["phone"]  # backfill of the added column is now caught
    assert all(c["key"] != {"id": 1} for c in users["changes"])
    inserted = [c for c in users["changes"] if c["change_type"] == "inserted"]
    assert inserted[0]["key"] == {"id": 5}
    assert inserted[0]["new"]["phone"] == "555-1234"  # inserted rows include added columns

    # orders — composite primary key
    orders = tables["orders"]
    assert (orders["inserted"], orders["modified"], orders["deleted"]) == (1, 1, 0)
    omod = next(c for c in orders["changes"] if c["change_type"] == "modified")
    assert omod["key"] == {"order_id": 100, "line_no": 2}
    assert omod["changed"] == ["qty"]

    # tags — no key: matched by full-row hash (insert/delete only, no modified)
    tags = tables["tags"]
    assert tags["keyed"] is False
    assert "full-row hash" in tags["note"]
    assert (tags["inserted"], tags["modified"], tags["deleted"]) == (1, 0, 1)
    tag_ins = next(c for c in tags["changes"] if c["change_type"] == "inserted")
    tag_del = next(c for c in tags["changes"] if c["change_type"] == "deleted")
    assert tag_ins["new"]["name"] == "green"
    assert tag_del["old"]["name"] == "blue"

    # schema diff
    kinds = {(s["change_kind"], s["table_name"]) for s in export["schema_changes"]}
    assert ("column_added", "users") in kinds
    assert ("table_added", "sessions") in kinds
