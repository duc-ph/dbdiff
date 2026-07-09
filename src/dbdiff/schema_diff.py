"""Compare two schema snapshots: tables added/removed, columns added/removed/retyped."""

from __future__ import annotations

from dataclasses import dataclass, field

from .mysql_io import SchemaSnapshot, Table


@dataclass
class ColumnChange:
    column: str
    kind: str  # "added" | "removed" | "type_changed"
    base_type: str | None = None
    new_type: str | None = None


@dataclass
class TableSchemaDiff:
    table: str
    status: str  # "changed" | "same"
    column_changes: list[ColumnChange] = field(default_factory=list)


@dataclass
class SchemaDiff:
    tables_added: list[str]
    tables_removed: list[str]
    table_diffs: dict[str, TableSchemaDiff]  # for tables present in both sides

    @property
    def has_changes(self) -> bool:
        return bool(self.tables_added or self.tables_removed) or any(
            d.status == "changed" for d in self.table_diffs.values()
        )


def diff_schemas(base: SchemaSnapshot, new: SchemaSnapshot) -> SchemaDiff:
    base_names = set(base.tables)
    new_names = set(new.tables)
    table_diffs: dict[str, TableSchemaDiff] = {}
    for name in sorted(base_names & new_names):
        changes = _column_changes(base.tables[name], new.tables[name])
        table_diffs[name] = TableSchemaDiff(
            table=name,
            status="changed" if changes else "same",
            column_changes=changes,
        )
    return SchemaDiff(
        tables_added=sorted(new_names - base_names),
        tables_removed=sorted(base_names - new_names),
        table_diffs=table_diffs,
    )


def _column_changes(bt: Table, nt: Table) -> list[ColumnChange]:
    b = {c.name: c for c in bt.columns}
    n = {c.name: c for c in nt.columns}
    changes: list[ColumnChange] = []
    for name in sorted(n.keys() - b.keys()):
        changes.append(ColumnChange(column=name, kind="added", new_type=n[name].column_type))
    for name in sorted(b.keys() - n.keys()):
        changes.append(ColumnChange(column=name, kind="removed", base_type=b[name].column_type))
    for name in sorted(b.keys() & n.keys()):
        if b[name].column_type != n[name].column_type:
            changes.append(
                ColumnChange(
                    column=name,
                    kind="type_changed",
                    base_type=b[name].column_type,
                    new_type=n[name].column_type,
                )
            )
    return changes
