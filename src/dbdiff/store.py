"""Per-run results persisted to SQLite — the boundary between compute and serve.

The engine writes the diff here; the web app and JSON export read from here, so
nothing is ever recomputed and the ephemeral MariaDB can be torn down
immediately after a run. A single SQLite file holds the whole run history,
partitioned by ``run_id``.
"""

from __future__ import annotations

import datetime
import decimal
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .row_diff import RowChange
from .schema_diff import SchemaDiff

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    base_label  TEXT NOT NULL,
    new_label   TEXT NOT NULL,
    base_spec   TEXT NOT NULL,
    new_spec    TEXT NOT NULL,
    status      TEXT NOT NULL,          -- running | done | error
    error       TEXT,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS schema_changes (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    change_kind TEXT NOT NULL,          -- table_added | table_removed | column_*
    table_name  TEXT NOT NULL,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_schema_changes_run ON schema_changes(run_id);

CREATE TABLE IF NOT EXISTS table_results (
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    table_name    TEXT NOT NULL,
    keyed         INTEGER NOT NULL,
    key_json      TEXT NOT NULL,
    schema_status TEXT,                 -- same | changed | added | removed
    inserted      INTEGER NOT NULL DEFAULT 0,
    modified      INTEGER NOT NULL DEFAULT 0,
    deleted       INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS ix_table_results_run ON table_results(run_id);

CREATE TABLE IF NOT EXISTS row_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    table_name   TEXT NOT NULL,
    change_type  TEXT NOT NULL,         -- inserted | modified | deleted
    key_json     TEXT NOT NULL,
    old_json     TEXT,
    new_json     TEXT,
    changed_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_row_changes_lookup
    ON row_changes(run_id, table_name, change_type);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA_SQL)

    # ---- writes (engine side) -------------------------------------------------

    def create_run(self, base_label, new_label, base_spec, new_spec) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO runs (created_at, base_label, new_label, base_spec, "
                "new_spec, status) VALUES (?,?,?,?,?, 'running')",
                (_now(), base_label, new_label, base_spec, new_spec),
            )
            return int(cur.lastrowid)

    def set_status(self, run_id: int, status: str, error: str | None = None) -> None:
        finished = _now() if status in ("done", "error") else None
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET status=?, error=?, finished_at=COALESCE(?, finished_at) "
                "WHERE id=?",
                (status, error, finished, run_id),
            )

    def set_summary(self, run_id: int, summary: dict) -> None:
        with self._conn() as c:
            c.execute("UPDATE runs SET summary_json=? WHERE id=?", (_dumps(summary), run_id))

    def save_schema_diff(self, run_id: int, sd: SchemaDiff) -> None:
        rows: list[tuple] = []
        for t in sd.tables_added:
            rows.append((run_id, "table_added", t, None))
        for t in sd.tables_removed:
            rows.append((run_id, "table_removed", t, None))
        for tname, td in sd.table_diffs.items():
            for ch in td.column_changes:
                rows.append(
                    (
                        run_id,
                        f"column_{ch.kind}",
                        tname,
                        _dumps(
                            {"column": ch.column, "base_type": ch.base_type, "new_type": ch.new_type}
                        ),
                    )
                )
        if rows:
            with self._conn() as c:
                c.executemany(
                    "INSERT INTO schema_changes (run_id, change_kind, table_name, "
                    "detail_json) VALUES (?,?,?,?)",
                    rows,
                )

    def save_table_result(
        self,
        run_id: int,
        table: str,
        keyed: bool,
        key: list[str],
        schema_status: str,
        inserted: int,
        modified: int,
        deleted: int,
        note: str | None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO table_results (run_id, table_name, keyed, key_json, "
                "schema_status, inserted, modified, deleted, note) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, table, int(keyed), _dumps(key), schema_status, inserted, modified, deleted, note),
            )

    def add_row_changes(self, run_id: int, table: str, changes: Iterable[RowChange]) -> None:
        rows = [
            (
                run_id,
                table,
                ch.change_type,
                _dumps(ch.key),
                _dumps(ch.old) if ch.old is not None else None,
                _dumps(ch.new) if ch.new is not None else None,
                _dumps(ch.changed_columns) if ch.changed_columns else None,
            )
            for ch in changes
        ]
        if rows:
            with self._conn() as c:
                c.executemany(
                    "INSERT INTO row_changes (run_id, table_name, change_type, "
                    "key_json, old_json, new_json, changed_json) VALUES (?,?,?,?,?,?,?)",
                    rows,
                )

    # ---- reads (serve / export side) ------------------------------------------

    def list_runs(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC")]

    def delete_run(self, run_id: int) -> bool:
        """Delete a run and all of its diff data. Returns False if it didn't exist."""
        with self._conn() as c:
            c.execute("DELETE FROM row_changes WHERE run_id=?", (run_id,))
            c.execute("DELETE FROM table_results WHERE run_id=?", (run_id,))
            c.execute("DELETE FROM schema_changes WHERE run_id=?", (run_id,))
            cur = c.execute("DELETE FROM runs WHERE id=?", (run_id,))
            return cur.rowcount > 0

    def get_run(self, run_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(r) if r else None

    def get_table_results(self, run_id: int) -> list[dict]:
        with self._conn() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM table_results WHERE run_id=? ORDER BY table_name", (run_id,)
                )
            ]

    def get_schema_changes(self, run_id: int) -> list[dict]:
        with self._conn() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM schema_changes WHERE run_id=? ORDER BY change_kind, table_name",
                    (run_id,),
                )
            ]

    def count_row_changes(self, run_id: int, table: str, change_type: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM row_changes WHERE run_id=? AND table_name=?"
        params: list[Any] = [run_id, table]
        if change_type:
            sql += " AND change_type=?"
            params.append(change_type)
        with self._conn() as c:
            return int(c.execute(sql, params).fetchone()[0])

    def get_row_changes(
        self,
        run_id: int,
        table: str,
        change_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        sql = "SELECT * FROM row_changes WHERE run_id=? AND table_name=?"
        params: list[Any] = [run_id, table]
        if change_type:
            sql += " AND change_type=?"
            params.append(change_type)
        sql += " ORDER BY id LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._conn() as c:
            out = []
            for r in c.execute(sql, params):
                out.append(
                    {
                        "id": r["id"],
                        "change_type": r["change_type"],
                        "key": json.loads(r["key_json"]),
                        "old": json.loads(r["old_json"]) if r["old_json"] else None,
                        "new": json.loads(r["new_json"]) if r["new_json"] else None,
                        "changed": json.loads(r["changed_json"]) if r["changed_json"] else [],
                    }
                )
            return out

    def export_run(self, run_id: int) -> dict:
        run = self.get_run(run_id)
        if not run:
            raise KeyError(f"no such run: {run_id}")
        tables = []
        for tr in self.get_table_results(run_id):
            tables.append(
                {
                    **{k: tr[k] for k in ("table_name", "schema_status", "inserted", "modified", "deleted", "note")},
                    "keyed": bool(tr["keyed"]),
                    "key": json.loads(tr["key_json"]),
                    "changes": self.get_row_changes(run_id, tr["table_name"], limit=10**9),
                }
            )
        return {
            "run": {k: run[k] for k in ("id", "created_at", "finished_at", "base_label", "new_label", "status")},
            "summary": json.loads(run["summary_json"]) if run["summary_json"] else None,
            "schema_changes": self.get_schema_changes(run_id),
            "tables": tables,
        }


# ---- value serialization -----------------------------------------------------


def _json_default(o: Any):
    if isinstance(o, (datetime.datetime, datetime.date, datetime.time)):
        return o.isoformat()
    if isinstance(o, datetime.timedelta):
        return str(o)
    if isinstance(o, decimal.Decimal):
        return str(o)
    if isinstance(o, (bytes, bytearray)):
        return "0x" + bytes(o).hex()
    return str(o)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, ensure_ascii=False)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")
