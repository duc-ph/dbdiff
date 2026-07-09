"""Configuration model for a diff run, loaded from an optional YAML file.

All settings are optional; ``Config.default()`` is a usable zero-config run that
diffs every table on its PRIMARY KEY.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class MariaDBConfig:
    """Settings for the ephemeral container used to restore dumps.

    Defaults to ``mysql:8.0`` since snapshots are typically MySQL dumps; set
    ``mariadb.image`` (e.g. ``mariadb:11``) to override. The container manager
    supports both mysql:* and mariadb:* images.
    """

    image: str = "mysql:8.0"
    startup_timeout: float = 120.0  # seconds to wait for the server to be ready


@dataclass
class Config:
    include: list[str] = field(default_factory=lambda: ["*"])
    exclude: list[str] = field(default_factory=list)
    # table name -> ordered key columns (overrides the detected PRIMARY KEY)
    keys: dict[str, list[str]] = field(default_factory=dict)
    # table name (or "*" for all) -> columns excluded from change detection
    ignore_columns: dict[str, list[str]] = field(default_factory=dict)
    mariadb: MariaDBConfig = field(default_factory=MariaDBConfig)
    # snapshot cache: how many restored snapshots to keep before LRU eviction
    cache_max_snapshots: int = 6

    @classmethod
    def default(cls) -> "Config":
        return cls()

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        """Load config from a YAML file. Returns defaults when ``path`` is falsy."""
        if not path:
            return cls.default()
        data = yaml.safe_load(Path(path).read_text()) or {}
        tables = data.get("tables") or {}
        mariadb = data.get("mariadb") or {}
        cache = data.get("cache") or {}
        return cls(
            include=tables.get("include") or ["*"],
            exclude=tables.get("exclude") or [],
            keys={k: _as_list(v) for k, v in (data.get("keys") or {}).items()},
            ignore_columns={
                k: _as_list(v) for k, v in (data.get("ignore_columns") or {}).items()
            },
            mariadb=MariaDBConfig(
                image=mariadb.get("image", "mysql:8.0"),
                startup_timeout=float(mariadb.get("startup_timeout", 120.0)),
            ),
            cache_max_snapshots=int(cache.get("max_snapshots", 6)),
        )

    def is_included(self, table: str) -> bool:
        included = (not self.include) or any(
            fnmatch.fnmatch(table, p) for p in self.include
        )
        excluded = any(fnmatch.fnmatch(table, p) for p in self.exclude)
        return included and not excluded

    def key_for(self, table: str, pk_cols: list[str]) -> list[str]:
        """Explicit key override wins; otherwise fall back to the PRIMARY KEY."""
        return list(self.keys.get(table, pk_cols))

    def ignored_columns(self, table: str) -> set[str]:
        out: set[str] = set(self.ignore_columns.get("*", []))
        out.update(self.ignore_columns.get(table, []))
        return out


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)
