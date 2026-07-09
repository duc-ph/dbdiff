"""MySQL connection endpoint + schema introspection.

Both sides of a diff (a live DSN or a restored dump) are represented as a
``MySQLEndpoint`` the rest of the code reads from uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymysql


@dataclass
class MySQLEndpoint:
    host: str
    port: int
    user: str
    password: str
    schema: str

    def connect(self):
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.schema,
            charset="utf8mb4",
            autocommit=True,
        )


@dataclass
class Column:
    name: str
    ordinal: int
    column_type: str  # e.g. "varchar(255)", "int(10) unsigned"
    nullable: bool
    default: str | None


@dataclass
class Table:
    name: str
    columns: list[Column]
    pk: list[str]  # ordered PRIMARY KEY columns (empty if none)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class SchemaSnapshot:
    label: str
    tables: dict[str, Table]


def introspect(conn, schema: str, label: str) -> SchemaSnapshot:
    """Read tables, columns, and primary keys for ``schema`` over ``conn``."""
    cols_by_table: dict[str, list[Column]] = {}
    pk_by_table: dict[str, list[str]] = {}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=%s AND table_type='BASE TABLE' "
            "ORDER BY table_name",
            (schema,),
        )
        names = [_text(r[0]) for r in cur.fetchall()]

        cur.execute(
            "SELECT table_name, column_name, ordinal_position, column_type, "
            "is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema=%s ORDER BY table_name, ordinal_position",
            (schema,),
        )
        for tname, cname, ordinal, ctype, is_nullable, cdefault in cur.fetchall():
            cols_by_table.setdefault(_text(tname), []).append(
                Column(
                    name=_text(cname),
                    ordinal=int(ordinal),
                    column_type=_text(ctype),
                    nullable=(_text(is_nullable) == "YES"),
                    default=_text(cdefault) if cdefault is not None else None,
                )
            )

        cur.execute(
            "SELECT table_name, column_name, seq_in_index "
            "FROM information_schema.statistics "
            "WHERE table_schema=%s AND index_name='PRIMARY' "
            "ORDER BY table_name, seq_in_index",
            (schema,),
        )
        for tname, cname, _seq in cur.fetchall():
            pk_by_table.setdefault(_text(tname), []).append(_text(cname))

    tables = {
        name: Table(
            name=name,
            columns=cols_by_table.get(name, []),
            pk=pk_by_table.get(name, []),
        )
        for name in names
    }
    return SchemaSnapshot(label=label, tables=tables)


def _text(v):
    """information_schema values come back as str under utf8mb4, but guard bytes."""
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return v
