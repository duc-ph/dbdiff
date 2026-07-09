# dbdiff

Diff two MySQL/MariaDB snapshots and browse what was **inserted**, **modified**, and **deleted** — row by row, column by column.

Built for data pipelines that snapshot a database at different stages: point dbdiff at the *before* and *after*, and see exactly what the run changed.

A snapshot is either:

- a **live connection** — `mysql://user:pass@host:3306/dbname`, or
- a **gzipped mysqldump** — `s3://bucket/path/snapshot.sql.gz` (or a local `.sql` / `.sql.gz` file).

You give it a `base` and a `new`. Dumps are restored into an **ephemeral MariaDB container**, the row-level diff is computed and persisted to a per-run **SQLite** database, and the container is torn down. A local **web app** lets you browse run history and drill into changes; a headless **CLI** fits into pipelines and CI.

## Requirements

- **Docker** (a usable daemon) — used to restore `.sql.gz` dumps. Not needed if *both* sides are live DSNs.
- **Python ≥ 3.11** and [**uv**](https://docs.astral.sh/uv/).

## Install

```bash
uv sync                 # creates .venv and installs deps
uv sync --extra dev     # also install pytest (to run the tests)
```

## Quickstart

**Web app** (browse runs, start diffs, drill into changes):

```bash
uv run dbdiff serve                       # → http://127.0.0.1:8000
uv run dbdiff serve --config dbdiff.yml   # with a default config for web runs
```

**Headless** (pipelines / CI):

```bash
uv run dbdiff run \
  --base  s3://my-bucket/snapshots/pre.sql.gz \
  --new   s3://my-bucket/snapshots/post.sql.gz \
  --config dbdiff.yml \
  --json   diff.json \
  --fail-on-change          # exit 1 if anything changed
```

Mixed sources work too — diff a live DB against a dump:

```bash
uv run dbdiff run --base mysql://root:pw@127.0.0.1:3306/app --new ./post.sql.gz
```

## How it works

Compute and serve are separate, so the UI never recomputes and the database container only lives for the duration of a run:

1. **Resolve** each side to a queryable MySQL schema — a live DSN is read directly; a `.sql.gz` dump is restored into a throwaway MariaDB container.
2. **Schema diff** — tables added/removed, columns added/removed/retyped.
3. **Row diff** — per table, scan `key + MD5(row)` on both sides, classify by primary key (or a configured key), then fetch full rows only for the changed keys to produce the column-level before→after.
4. **Persist** the diff to SQLite; tear the container down.
5. **Browse** in the web app, or consume the JSON artifact.

### What it detects

- **inserted / modified / deleted** rows, keyed on each table's PRIMARY KEY (override per table in config).
- **column-level** before→after for modified rows; ignored columns (e.g. `updated_at`) are excluded from change detection so they don't create noise.
- **schema changes** — added/removed tables, added/removed/retyped columns. Row hashing uses only the columns common to both sides, so a column add doesn't mark every row "modified".
- **keyless tables** (no PRIMARY KEY and no override) are matched by a hash of the whole row: inserts and deletes are reported (a modified row shows as one delete + one insert). Set `keys.<table>` in the config for true modified detection.

## Configuration

All sections are optional — see [`dbdiff.example.yml`](dbdiff.example.yml).

```yaml
tables:
  include: ["*"]            # globs of tables to diff
  exclude: ["audit_log", "tmp_*"]

keys:                       # override row identity (no PK, or a business key)
  order_items: [order_id, line_no]

ignore_columns:             # excluded from change detection ("*" = all tables)
  "*": [updated_at]
  users: [last_login_at]

mariadb:
  image: "mariadb:11"       # image for the ephemeral restore container
  startup_timeout: 90
```

## CLI

```
dbdiff run    --base SRC --new SRC [--config FILE] [--store PATH]
              [--json FILE] [--fail-on-change] [--cache/--no-cache]
dbdiff serve  [--host H] [--port P] [--store PATH] [--config FILE]
dbdiff cache  list | clear        # manage the snapshot cache
```

`--store` defaults to `.dbdiff/dbdiff.sqlite` (the shared run history). Source specs: `mysql://…`, `s3://…/x.sql.gz`, `file:///path`, or a local `.sql`/`.sql.gz` path.

### Snapshot cache

`--cache` (on by default) restores each dump **once**, keyed by its name, into a
persistent container and reuses it across runs — so diffing `18→19` then
`19→20` restores step 19 only once, and re-running a diff (e.g. after adjusting
`keys:`) skips the restore entirely. `dbdiff cache list` shows what's cached;
`dbdiff cache clear` removes it; `cache.max_snapshots` bounds disk (LRU).

## Notes & limitations

- Dumps are assumed to be **single-database** mysqldumps (no `CREATE DATABASE` / `USE`); objects are restored into a fresh schema.
- For **added/removed whole tables**, dbdiff reports the row counts; per-row detail for them is not captured yet.
- The row hash compares values via `MD5(CAST(... ))`; exotic float/binary edge cases may need a `keys`/`ignore_columns` tweak.
- Live reads are best taken against a quiescent snapshot (e.g. a paused pipeline stage).

## Tests

```bash
uv run --extra dev pytest -q     # the end-to-end test needs Docker; it skips otherwise
```

## License

MIT — see [LICENSE](LICENSE).

Vendored front-end assets: [htmx](https://htmx.org) (MIT) and [IBM Plex](https://github.com/IBM/plex) fonts (SIL Open Font License 1.1).
