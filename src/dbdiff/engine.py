"""Orchestrate a full diff run: resolve sources, (re)store dumps into an
ephemeral MariaDB, diff schema + rows, persist to the store, tear everything
down. Emits progress through a ``log`` callback (stderr for the CLI, SSE for
the web app).
"""

from __future__ import annotations

import tempfile
from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .cache import SnapshotCache
from .config import Config
from .mariadb_container import Container, ephemeral_mariadb, restore_dump
from .mysql_io import MySQLEndpoint, introspect
from .row_diff import diff_table, diff_table_sql
from .schema_diff import diff_schemas
from .sources import DumpSource, LiveSource, parse_spec, redact, safe_label
from .store import Store

Logger = Callable[[str], None]


def _noop(_: str) -> None:
    pass


@dataclass
class RunSummary:
    run_id: int
    tables_compared: int = 0
    tables_changed: int = 0
    tables_added: int = 0
    tables_removed: int = 0
    keyless_tables: int = 0
    rows_inserted: int = 0
    rows_modified: int = 0
    rows_deleted: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(
            self.rows_inserted
            or self.rows_modified
            or self.rows_deleted
            or self.tables_added
            or self.tables_removed
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["has_changes"] = self.has_changes
        return d


def run_diff(
    base_spec: str,
    new_spec: str,
    config: Config,
    store: Store,
    log: Logger = _noop,
    download_dir: str | Path | None = None,
    run_id: int | None = None,
    cache: SnapshotCache | None = None,
) -> RunSummary:
    """Compute and persist a diff between two source specs.

    When ``run_id`` is given the run row already exists (the web app pre-creates
    it so it can redirect immediately); otherwise one is created here. Labels are
    derived without touching the network and never contain DSN passwords.
    """
    if run_id is None:
        run_id = store.create_run(
            safe_label(base_spec), safe_label(new_spec), redact(base_spec), redact(new_spec)
        )
    try:
        if cache is not None:
            summary = _execute_cached(run_id, base_spec, new_spec, config, store, log, cache)
        else:
            dd = Path(download_dir or tempfile.mkdtemp(prefix="dbdiff-"))
            base_src = parse_spec(base_spec, dd, log)
            new_src = parse_spec(new_spec, dd, log)
            summary = _execute(run_id, base_src, new_src, config, store, log)
        store.set_summary(run_id, summary.to_dict())
        store.set_status(run_id, "done")
        log("done.")
        return summary
    except Exception as e:  # noqa: BLE001 — record then re-raise
        log(f"ERROR: {e}")
        store.set_status(run_id, "error", str(e))
        raise


def _execute(
    run_id: int,
    base_src: LiveSource | DumpSource,
    new_src: LiveSource | DumpSource,
    config: Config,
    store: Store,
    log: Logger,
) -> RunSummary:
    """Ephemeral path: restore any dumps into a throwaway container, then diff."""
    with ExitStack() as stack:
        container: Container | None = None
        if base_src.needs_restore or new_src.needs_restore:
            container = stack.enter_context(
                ephemeral_mariadb(config.mariadb.image, config.mariadb.startup_timeout, log)
            )
        base_ep = _resolve(base_src, container, "base", log)
        new_ep = _resolve(new_src, container, "new", log)
        return _diff_endpoints(
            run_id, base_ep, new_ep, base_src.label, new_src.label, config, store, log
        )


def _execute_cached(
    run_id: int,
    base_spec: str,
    new_spec: str,
    config: Config,
    store: Store,
    log: Logger,
    cache: SnapshotCache,
) -> RunSummary:
    """Cached path: reuse snapshots already restored in the persistent cache
    container, restoring only the ones not present yet."""
    base_ep, base_label = _cache_endpoint(cache, base_spec, log)
    new_ep, new_label = _cache_endpoint(cache, new_spec, log)
    return _diff_endpoints(run_id, base_ep, new_ep, base_label, new_label, config, store, log)


def _cache_endpoint(cache: SnapshotCache, spec: str, log: Logger) -> tuple[MySQLEndpoint, str]:
    if spec.strip().lower().startswith(("mysql://", "mariadb://")):
        src = LiveSource(spec)  # live sources aren't cached
        return src.endpoint(), src.label
    cont, schema = cache.ensure(spec)
    return (
        MySQLEndpoint(host=cont.host, port=cont.port, user="root", password=cont.password, schema=schema),
        safe_label(spec),
    )


def _diff_endpoints(
    run_id: int,
    base_ep: MySQLEndpoint,
    new_ep: MySQLEndpoint,
    base_label: str,
    new_label: str,
    config: Config,
    store: Store,
    log: Logger,
) -> RunSummary:
    with closing(base_ep.connect()) as bconn, closing(new_ep.connect()) as nconn:
        # Both sides on the same server (two restored dumps or two cached
        # snapshots) → push the diff into SQL; only changed rows cross the wire.
        # Cross-server (live vs dump) uses the streaming Python diff.
        same_server = (base_ep.host, base_ep.port) == (new_ep.host, new_ep.port)

        log("introspecting schemas …")
        base_snap = introspect(bconn, base_ep.schema, base_label)
        new_snap = introspect(nconn, new_ep.schema, new_label)
        sdiff = diff_schemas(base_snap, new_snap)
        store.save_schema_diff(run_id, sdiff)

        summary = RunSummary(
            run_id=run_id,
            tables_added=len([t for t in sdiff.tables_added if config.is_included(t)]),
            tables_removed=len([t for t in sdiff.tables_removed if config.is_included(t)]),
        )

        for tname in sorted(set(base_snap.tables) & set(new_snap.tables)):
            if not config.is_included(tname):
                continue
            bt, nt = base_snap.tables[tname], new_snap.tables[tname]
            key = config.key_for(tname, bt.pk)
            ignored = config.ignored_columns(tname)
            log(f"diffing `{tname}` …")
            if same_server:
                td = diff_table_sql(
                    bconn, nconn, base_ep.schema, new_ep.schema,
                    bt.column_names, nt.column_names, tname, key, ignored,
                )
            else:
                td = diff_table(
                    bconn, nconn, bt.column_names, nt.column_names, tname, key, ignored
                )
            store.save_table_result(
                run_id, tname, td.keyed, td.key, sdiff.table_diffs[tname].status,
                td.inserted, td.modified, td.deleted, td.note,
            )
            store.add_row_changes(run_id, tname, td.changes)

            summary.tables_compared += 1
            if td.total:
                summary.tables_changed += 1
            if not td.keyed:
                summary.keyless_tables += 1
            summary.rows_inserted += td.inserted
            summary.rows_modified += td.modified
            summary.rows_deleted += td.deleted

        # Whole added/removed tables: record counts (per-row detail deferred).
        for tname in sdiff.tables_added:
            if not config.is_included(tname):
                continue
            n = _row_count(nconn, tname)
            store.save_table_result(
                run_id, tname, False, [], "added", n, 0, 0,
                "table is new — per-row detail not captured yet",
            )
            summary.rows_inserted += n
        for tname in sdiff.tables_removed:
            if not config.is_included(tname):
                continue
            n = _row_count(bconn, tname)
            store.save_table_result(
                run_id, tname, False, [], "removed", 0, 0, n,
                "table was dropped — per-row detail not captured yet",
            )
            summary.rows_deleted += n

        return summary


def _resolve(
    src: LiveSource | DumpSource,
    container: Container | None,
    schema_name: str,
    log: Logger,
) -> MySQLEndpoint:
    if isinstance(src, LiveSource):
        return src.endpoint()
    assert container is not None, "a dump source requires an ephemeral container"
    restore_dump(container, schema_name, src.local_path, log)
    return MySQLEndpoint(
        host=container.host,
        port=container.port,
        user="root",
        password=container.password,
        schema=schema_name,
    )


def _row_count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        return int(cur.fetchone()[0])
