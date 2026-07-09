"""``dbdiff`` command-line interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .cache import SnapshotCache
from .config import Config
from .engine import run_diff
from .store import Store

DEFAULT_STORE = ".dbdiff/dbdiff.sqlite"


@click.group()
@click.version_option(package_name="dbdiff")
def main() -> None:
    """Diff two MySQL/MariaDB snapshots (live DSN or .sql.gz) row by row."""


@main.command()
@click.option("--base", required=True, help="base source (mysql://… | s3://…/x.sql.gz | path)")
@click.option("--new", "new", required=True, help="new source (same forms as --base)")
@click.option("--config", "config_path", type=click.Path(exists=True), help="dbdiff.yml")
@click.option("--store", "store_path", default=DEFAULT_STORE, show_default=True)
@click.option("--json", "json_out", type=click.Path(), help="write the full diff as JSON")
@click.option("--fail-on-change", is_flag=True, help="exit 1 if any change is found")
@click.option("--cache/--no-cache", "use_cache", default=True, show_default=True,
              help="reuse restored snapshots across runs (cached by name)")
def run(base, new, config_path, store_path, json_out, fail_on_change, use_cache) -> None:
    """Compute a diff between BASE and NEW and store it."""
    cfg = Config.load(config_path)
    store = Store(store_path)
    store.init()

    echo = lambda m: click.echo(m, err=True)  # noqa: E731
    snap_cache = None
    if use_cache:
        snap_cache = SnapshotCache(
            image=cfg.mariadb.image,
            max_snapshots=cfg.cache_max_snapshots,
            startup_timeout=cfg.mariadb.startup_timeout,
            log=echo,
        )
    summary = run_diff(base, new, cfg, store, log=echo, cache=snap_cache)

    _print_summary(store, summary)

    if json_out:
        Path(json_out).write_text(json.dumps(store.export_run(summary.run_id), indent=2))
        click.echo(f"\nwrote {json_out}", err=True)

    if fail_on_change and summary.has_changes:
        sys.exit(1)


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option("--store", "store_path", default=DEFAULT_STORE, show_default=True)
@click.option("--config", "config_path", type=click.Path(exists=True), help="default dbdiff.yml for web runs")
def serve(host, port, store_path, config_path) -> None:
    """Launch the local web app to browse run history and drill into diffs."""
    import uvicorn

    from .web.app import create_app

    app = create_app(store_path, config_path)
    click.echo(f"dbdiff serving on http://{host}:{port}  (store: {store_path})")
    uvicorn.run(app, host=host, port=port)


@main.group()
def cache() -> None:
    """Manage the snapshot cache (restored snapshots reused across runs)."""


@cache.command("list")
@click.option("--config", "config_path", type=click.Path(exists=True))
def cache_list(config_path) -> None:
    """List cached snapshots."""
    cfg = Config.load(config_path)
    rows = SnapshotCache(image=cfg.mariadb.image, max_snapshots=cfg.cache_max_snapshots).list()
    if not rows:
        click.echo("snapshot cache is empty")
        return
    for r in rows:
        click.echo(f"{r['snapshot_name']:<44} {r['schema_name']:<34} used {r['last_used']}")


@cache.command("clear")
@click.option("--config", "config_path", type=click.Path(exists=True))
def cache_clear(config_path) -> None:
    """Remove the snapshot-cache container and everything in it."""
    cfg = Config.load(config_path)
    SnapshotCache(image=cfg.mariadb.image).clear()
    click.echo("snapshot cache cleared")


@main.group()
def runs() -> None:
    """List or delete stored diff runs."""


@runs.command("list")
@click.option("--store", "store_path", default=DEFAULT_STORE, show_default=True)
def runs_list(store_path) -> None:
    """List stored diff runs."""
    store = Store(store_path)
    store.init()
    rows = store.list_runs()
    if not rows:
        click.echo("no runs")
        return
    for r in rows:
        s = json.loads(r["summary_json"]) if r["summary_json"] else {}
        click.echo(
            f"#{r['id']:<4} {r['created_at']}  {r['base_label']} -> {r['new_label']}  "
            f"[{r['status']}]  +{s.get('rows_inserted', '?')} ~{s.get('rows_modified', '?')} "
            f"-{s.get('rows_deleted', '?')}"
        )


@runs.command("delete")
@click.argument("run_id", type=int)
@click.option("--store", "store_path", default=DEFAULT_STORE, show_default=True)
def runs_delete(run_id, store_path) -> None:
    """Delete run RUN_ID and all of its diff data."""
    store = Store(store_path)
    store.init()
    if store.delete_run(run_id):
        click.echo(f"deleted run #{run_id}")
    else:
        click.echo(f"no run #{run_id}", err=True)
        sys.exit(1)


def _print_summary(store: Store, summary) -> None:
    results = [r for r in store.get_table_results(summary.run_id) if (r["inserted"] or r["modified"] or r["deleted"])]
    click.echo("")
    if results:
        width = max(len(r["table_name"]) for r in results)
        click.echo(f"  {'table'.ljust(width)}   +ins   ~mod   -del")
        click.echo(f"  {'-' * width}   ----   ----   ----")
        for r in results:
            click.echo(
                f"  {r['table_name'].ljust(width)}  {r['inserted']:>5}  "
                f"{r['modified']:>5}  {r['deleted']:>5}"
            )
    else:
        click.echo("  no row-level changes")

    s = summary
    click.echo("")
    click.echo(
        f"  tables: {s.tables_compared} compared, {s.tables_changed} changed, "
        f"+{s.tables_added}/-{s.tables_removed} added/removed"
    )
    click.echo(
        f"  rows:   +{s.rows_inserted} inserted, ~{s.rows_modified} modified, "
        f"-{s.rows_deleted} deleted"
    )
    if s.keyless_tables:
        click.echo(
            f"  note:   {s.keyless_tables} keyless table(s) matched by full-row "
            "hash (no modified detection)"
        )
    click.echo(f"  run id: {s.run_id}")


if __name__ == "__main__":
    main()
