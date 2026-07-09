"""Per-table row diff.

Strategy (cheap on the wire, fine up to ~1M rows/table):

1. Scan both sides for ``key + MD5(row)`` where the row hash covers the
   non-key, non-ignored columns common to both schemas.
2. Classify by key set: key only in new -> inserted, only in base -> deleted,
   in both with differing hash -> modified.
3. Fetch *full* rows only for the changed keys, and for modified rows compute
   which columns actually changed (before -> after).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import pymysql.cursors

# Sentinel substituted for NULL inside the row hash so that (x, NULL) and (x)
# don't collide.
_NULL = "~dbdiff-null~"

# Max rows of detail kept per (table, change_type). Counts are always exact;
# only the stored before/after rows are capped, to bound memory and DB size.
DETAIL_CAP = 5000


@dataclass
class RowChange:
    change_type: str  # "inserted" | "deleted" | "modified"
    key: dict[str, object]
    old: dict[str, object] | None = None
    new: dict[str, object] | None = None
    changed_columns: list[str] = field(default_factory=list)


@dataclass
class TableDiff:
    table: str
    key: list[str]
    keyed: bool
    inserted: int = 0
    modified: int = 0
    deleted: int = 0
    changes: list[RowChange] = field(default_factory=list)
    note: str | None = None

    @property
    def total(self) -> int:
        return self.inserted + self.modified + self.deleted


def diff_table(
    base_conn,
    new_conn,
    base_columns: Sequence[str],
    new_columns: Sequence[str],
    table: str,
    key: list[str],
    ignored: set[str],
) -> TableDiff:
    common = [c for c in base_columns if c in set(new_columns)]
    common_set = set(common)

    if not key:
        return _diff_keyless(
            base_conn, new_conn, table, common, ignored,
            note="no primary key — matched by full-row hash; a modified row "
            "appears as one delete + one insert (set keys.<table> in config for "
            "true modified detection)",
        )
    if any(k not in common_set for k in key):
        return _diff_keyless(
            base_conn, new_conn, table, common, ignored,
            note=f"configured key {key} not present on both sides — matched by "
            "full-row hash instead",
        )

    key_set = set(key)
    compare_cols = [c for c in common if c not in key_set and c not in ignored]
    display_cols = list(key) + [c for c in common if c not in key_set]

    base_hashes = _scan_hashes(base_conn, table, key, compare_cols)
    new_hashes = _scan_hashes(new_conn, table, key, compare_cols)

    inserted_keys = [k for k in new_hashes if k not in base_hashes]
    deleted_keys = [k for k in base_hashes if k not in new_hashes]
    modified_keys = [
        k for k, h in base_hashes.items() if k in new_hashes and new_hashes[k] != h
    ]

    td = TableDiff(
        table=table,
        key=key,
        keyed=True,
        inserted=len(inserted_keys),
        deleted=len(deleted_keys),
        modified=len(modified_keys),
    )

    new_rows = _fetch_rows(new_conn, table, key, inserted_keys + modified_keys, display_cols)
    base_rows = _fetch_rows(base_conn, table, key, deleted_keys + modified_keys, display_cols)

    for k in inserted_keys:
        td.changes.append(RowChange("inserted", _keydict(key, k), new=new_rows.get(k)))
    for k in deleted_keys:
        td.changes.append(RowChange("deleted", _keydict(key, k), old=base_rows.get(k)))
    for k in modified_keys:
        old, new = base_rows.get(k), new_rows.get(k)
        changed = [c for c in compare_cols if _get(old, c) != _get(new, c)]
        td.changes.append(
            RowChange("modified", _keydict(key, k), old=old, new=new, changed_columns=changed)
        )
    return td


def diff_table_sql(
    base_conn,
    new_conn,
    base_schema: str,
    new_schema: str,
    base_columns: Sequence[str],
    new_columns: Sequence[str],
    table: str,
    key: list[str],
    ignored: set[str],
    cap: int = DETAIL_CAP,
) -> TableDiff:
    """Diff a table when both sides live in the same MySQL server.

    Pushes the whole comparison into SQL by joining ``base.<t>`` and
    ``new.<t>`` on the key, so only changed rows cross the wire and memory stays
    flat regardless of table size. Change detection spans the *union* of
    columns: a column present on only one side is treated as NULL on the other,
    so an added column that gets backfilled shows its rows as modified
    (∅ → value) while a bare column-add with no data stays quiet. Counts are
    exact; detail is capped. Keyless tables fall back to the streaming diff.
    """
    base_set, new_set = set(base_columns), set(new_columns)
    common = [c for c in base_columns if c in new_set]
    common_set = set(common)

    if not key:
        return _diff_keyless(
            base_conn, new_conn, table, common, ignored,
            note="no primary key — matched by full-row hash; a modified row "
            "appears as one delete + one insert (set keys.<table> in config for "
            "true modified detection)",
        )
    if any(k not in common_set for k in key):
        return _diff_keyless(
            base_conn, new_conn, table, common, ignored,
            note=f"configured key {key} not present on both sides — matched by "
            "full-row hash instead",
        )

    key_set = set(key)
    # change-detection spans common + added-in-new + dropped-from-base columns
    cmp_cols = (
        [c for c in common if c not in key_set and c not in ignored]
        + [c for c in new_columns if c not in base_set and c not in key_set and c not in ignored]
        + [c for c in base_columns if c not in new_set and c not in key_set and c not in ignored]
    )

    bref = f"`{base_schema}`.`{table}`"
    nref = f"`{new_schema}`.`{table}`"
    on = " AND ".join(f"b.`{k}` <=> n.`{k}`" for k in key)
    fk = key[0]
    hb = _union_hash("b", cmp_cols, base_set)
    hn = _union_hash("n", cmp_cols, new_set)
    conn = base_conn  # one connection can read both schemas via qualified names

    inserted = _scalar(conn, f"SELECT COUNT(*) FROM {nref} n LEFT JOIN {bref} b ON {on} WHERE b.`{fk}` IS NULL")
    deleted = _scalar(conn, f"SELECT COUNT(*) FROM {bref} b LEFT JOIN {nref} n ON {on} WHERE n.`{fk}` IS NULL")
    modified = _scalar(conn, f"SELECT COUNT(*) FROM {bref} b JOIN {nref} n ON {on} WHERE {hb} <> {hn}")

    td = TableDiff(table=table, key=list(key), keyed=True,
                   inserted=inserted, modified=modified, deleted=deleted)
    if max(inserted, deleted, modified) > cap:
        td.note = f"detail capped at {cap} rows per category (counts are exact)"

    # inserted shows the whole NEW row; deleted shows the whole BASE row
    ins_cols = list(key) + [c for c in new_columns if c not in key_set]
    sel_n = ", ".join(f"n.`{c}` AS `{c}`" for c in ins_cols)
    for r in _fetch(conn, f"SELECT {sel_n} FROM {nref} n LEFT JOIN {bref} b ON {on} WHERE b.`{fk}` IS NULL LIMIT {cap}"):
        td.changes.append(RowChange("inserted", {k: r[k] for k in key}, new=r))

    del_cols = list(key) + [c for c in base_columns if c not in key_set]
    sel_b = ", ".join(f"b.`{c}` AS `{c}`" for c in del_cols)
    for r in _fetch(conn, f"SELECT {sel_b} FROM {bref} b LEFT JOIN {nref} n ON {on} WHERE n.`{fk}` IS NULL LIMIT {cap}"):
        td.changes.append(RowChange("deleted", {k: r[k] for k in key}, old=r))

    # modified shows the union; NULL stands in where a column is absent on a side
    disp = (
        list(key)
        + [c for c in common if c not in key_set]
        + [c for c in cmp_cols if c not in common_set]
    )
    mb = ", ".join((f"b.`{c}` AS `b__{c}`" if c in base_set else f"NULL AS `b__{c}`") for c in disp)
    mn = ", ".join((f"n.`{c}` AS `n__{c}`" if c in new_set else f"NULL AS `n__{c}`") for c in disp)
    for r in _fetch(conn, f"SELECT {mb}, {mn} FROM {bref} b JOIN {nref} n ON {on} WHERE {hb} <> {hn} LIMIT {cap}"):
        old = {c: r[f"b__{c}"] for c in disp}
        new = {c: r[f"n__{c}"] for c in disp}
        changed = [c for c in cmp_cols if old.get(c) != new.get(c)]
        td.changes.append(RowChange("modified", {k: old[k] for k in key}, old=old, new=new, changed_columns=changed))

    return td


def _union_hash(alias: str, cols: Sequence[str], present: set[str]) -> str:
    """Hash ``cols`` for ``alias``; substitute the NULL sentinel for any column
    not present on this side, so an absent column compares equal to NULL."""
    if not cols:
        return "''"
    parts = [
        f"IFNULL({alias}.`{c}`, '{_NULL}')" if c in present else f"'{_NULL}'"
        for c in cols
    ]
    return f"MD5(CONCAT_WS(CHAR(31), {', '.join(parts)}))"


def _scalar(conn, sql: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql)
        return int(cur.fetchone()[0])


def _fetch(conn, sql: str) -> list[dict]:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()


def _hash_expr(cols: Sequence[str]) -> str:
    if not cols:
        return "''"
    parts = ", ".join(f"IFNULL(`{c}`, '{_NULL}')" for c in cols)
    return f"MD5(CONCAT_WS(CHAR(31), {parts}))"


def _scan_hashes(conn, table: str, key: list[str], compare_cols: Sequence[str]) -> dict[tuple, str]:
    key_sel = ",".join(f"`{c}`" for c in key)
    sql = f"SELECT {key_sel}, {_hash_expr(compare_cols)} AS _h FROM `{table}`"
    out: dict[tuple, str] = {}
    klen = len(key)
    with conn.cursor(pymysql.cursors.SSCursor) as cur:
        cur.execute(sql)
        for row in cur:
            out[tuple(row[:klen])] = row[klen]
    return out


def _diff_keyless(
    base_conn, new_conn, table: str, common: Sequence[str], ignored: set[str],
    note: str, cap: int = DETAIL_CAP,
) -> TableDiff:
    """Match rows by a hash of the whole row when there's no key.

    Memory-bounded: only per-row hashes (not full rows) are streamed to build
    multiset counts, then full rows are fetched for just the differing hashes.
    Without a key we can't pair a before/after row, so there's no "modified": a
    row present on one side but not the other is an insert or delete. Counts are
    exact; stored detail is capped at ``cap`` rows per category.
    """
    hash_cols = [c for c in common if c not in ignored]
    display = list(common)
    base_counts = _scan_hash_counts(base_conn, table, hash_cols)
    new_counts = _scan_hash_counts(new_conn, table, hash_cols)

    inserted_h = {h: c - base_counts.get(h, 0) for h, c in new_counts.items() if c > base_counts.get(h, 0)}
    deleted_h = {h: c - new_counts.get(h, 0) for h, c in base_counts.items() if c > new_counts.get(h, 0)}

    td = TableDiff(table=table, key=[], keyed=False, note=note)
    td.inserted = sum(inserted_h.values())
    td.deleted = sum(deleted_h.values())
    if td.inserted > cap or td.deleted > cap:
        td.note = f"{note} — detail capped at {cap} rows per category"

    for r in _fetch_by_hash(new_conn, table, display, hash_cols, list(inserted_h)[:cap], cap):
        td.changes.append(RowChange("inserted", {}, new=r))
    for r in _fetch_by_hash(base_conn, table, display, hash_cols, list(deleted_h)[:cap], cap):
        td.changes.append(RowChange("deleted", {}, old=r))
    return td


def _scan_hash_counts(conn, table: str, hash_cols: Sequence[str]) -> Counter:
    """Stream only the per-row hash and return a multiset (keeps memory flat)."""
    sql = f"SELECT {_hash_expr(hash_cols)} AS _h FROM `{table}`"
    counts: Counter = Counter()
    with conn.cursor(pymysql.cursors.SSCursor) as cur:
        cur.execute(sql)
        for row in cur:
            counts[row[0]] += 1
    return counts


def _fetch_by_hash(
    conn, table: str, display_cols: Sequence[str], hash_cols: Sequence[str],
    hashes: list, cap: int,
) -> list[dict]:
    if not hashes:
        return []
    col_sel = ",".join(f"`{c}`" for c in display_cols)
    hexpr = _hash_expr(hash_cols)
    out: list[dict] = []
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        for batch in _batched(hashes, 1000):
            ph = ",".join(["%s"] * len(batch))
            cur.execute(f"SELECT {col_sel} FROM `{table}` WHERE {hexpr} IN ({ph})", batch)
            out.extend(cur.fetchall())
            if len(out) >= cap:
                break
    return out[:cap]


def _fetch_rows(
    conn, table: str, key: list[str], keys: Iterable[tuple], display_cols: Sequence[str]
) -> dict[tuple, dict]:
    keys = list(keys)
    if not keys:
        return {}
    col_sel = ",".join(f"`{c}`" for c in display_cols)
    out: dict[tuple, dict] = {}
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        for batch in _batched(keys, 500):
            where, params = _in_clause(key, batch)
            cur.execute(f"SELECT {col_sel} FROM `{table}` WHERE {where}", params)
            for r in cur.fetchall():
                out[tuple(r[c] for c in key)] = r
    return out


def _in_clause(key: list[str], batch: list[tuple]) -> tuple[str, list]:
    if len(key) == 1:
        col = f"`{key[0]}`"
        ph = ",".join(["%s"] * len(batch))
        return f"{col} IN ({ph})", [k[0] for k in batch]
    row = "(" + ",".join(f"`{c}`" for c in key) + ")"
    one = "(" + ",".join(["%s"] * len(key)) + ")"
    ph = ",".join([one] * len(batch))
    return f"{row} IN ({ph})", [v for k in batch for v in k]


def _batched(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _keydict(key: list[str], values: tuple) -> dict[str, object]:
    return dict(zip(key, values))


def _get(row: dict | None, col: str):
    return row.get(col) if row else None
