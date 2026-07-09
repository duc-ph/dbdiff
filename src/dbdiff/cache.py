"""Persistent snapshot cache.

Restores each dump once — keyed by its snapshot name (the dump's basename) —
into a long-lived container, and reuses it on later runs. So diffing 18→19 then
19→20 restores step 19 only once, and a later 19→20 run skips download+restore
entirely. A small registry schema tracks what's cached; an LRU cap bounds disk.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import tempfile
from pathlib import Path
from typing import Callable

from .mariadb_container import (
    ROOT_PASSWORD,
    Container,
    _client_for,
    _mapped_port,
    _run,
    _wait_ready,
    restore_dump,
)
from .sources import DumpSource, parse_spec, safe_label

Logger = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def schema_for(snapshot_name: str) -> str:
    """A deterministic, valid MySQL schema name for a snapshot name."""
    base = snapshot_name
    for ext in (".sql.gz", ".sql", ".gz"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    slug = re.sub(r"[^0-9A-Za-z]+", "_", base).strip("_").lower()[:40]
    digest = hashlib.sha1(snapshot_name.encode()).hexdigest()[:8]
    return f"snap_{slug}_{digest}"[:64]


class SnapshotCache:
    REGISTRY = "_dbdiff_cache"

    def __init__(
        self,
        image: str = "mysql:8.0",
        container_name: str = "dbdiff-cache",
        max_snapshots: int = 6,
        startup_timeout: float = 120.0,
        log: Logger = _noop,
        download_dir: str | Path | None = None,
    ):
        self.image = image
        self.container_name = container_name
        self.max_snapshots = max_snapshots
        self.startup_timeout = startup_timeout
        self.log = log
        self.download_dir = Path(download_dir or tempfile.mkdtemp(prefix="dbdiff-cache-"))
        self._cont: Container | None = None

    # ---- container lifecycle (persistent, NOT --rm) ---------------------------

    def _container(self) -> Container:
        if self._cont is not None:
            return self._cont
        insp = _run(["docker", "inspect", "-f", "{{.State.Running}}", self.container_name])
        running = insp.returncode == 0 and insp.stdout.strip() == "true"
        if not running:
            _run(["docker", "rm", "-f", self.container_name])
            self.log(f"starting snapshot-cache container {self.container_name} ({self.image}) …")
            proc = _run(
                [
                    "docker", "run", "-d", "--name", self.container_name,
                    "-e", f"MYSQL_ROOT_PASSWORD={ROOT_PASSWORD}",
                    "-e", f"MARIADB_ROOT_PASSWORD={ROOT_PASSWORD}",
                    "-e", "MYSQL_ROOT_HOST=%", "-e", "MARIADB_ROOT_HOST=%",
                    "-p", "127.0.0.1::3306", self.image,
                    "--innodb-flush-log-at-trx-commit=0", "--innodb-doublewrite=0", "--skip-log-bin",
                ]
            )
            if proc.returncode != 0:
                raise RuntimeError(f"could not start cache container: {proc.stderr.strip()}")
        cont = Container(
            name=self.container_name, host="127.0.0.1",
            port=_mapped_port(self.container_name), client=_client_for(self.image),
        )
        if not running:
            _wait_ready(cont, self.startup_timeout)
        self._init_registry(cont)
        self._cont = cont
        return cont

    def _init_registry(self, cont: Container) -> None:
        with cont.connect() as c, c.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.REGISTRY}`")
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS `{self.REGISTRY}`.snapshots ("
                "schema_name VARCHAR(64) PRIMARY KEY, snapshot_name VARCHAR(255), "
                "source_spec TEXT, created_at DATETIME, last_used DATETIME)"
            )

    # ---- public API -----------------------------------------------------------

    def ensure(self, spec: str) -> tuple[Container, str]:
        """Return (container, schema) for ``spec``, restoring it only on a miss."""
        name = safe_label(spec)
        schema = schema_for(name)
        cont = self._container()
        if self._has(cont, schema):
            self._touch(cont, schema)
            self.log(f"cache hit   {name}  →  `{schema}`")
            return cont, schema

        self.log(f"cache miss  {name}  — downloading + restoring …")
        src = parse_spec(spec, self.download_dir, self.log)
        if not isinstance(src, DumpSource):
            raise ValueError(f"only dump sources can be cached, not {spec!r}")
        self._drop(cont, schema)
        restore_dump(cont, schema, src.local_path, self.log)
        self._register(cont, schema, name, spec)
        try:
            src.local_path.unlink()  # the restored DB is the cache; drop the dump file
        except OSError:
            pass
        self._evict(cont, keep={schema})
        return cont, schema

    def list(self) -> list[dict]:
        insp = _run(["docker", "inspect", "-f", "{{.State.Running}}", self.container_name])
        if not (insp.returncode == 0 and insp.stdout.strip() == "true"):
            return []
        cont = self._container()
        with cont.connect() as c, c.cursor() as cur:
            cur.execute(
                f"SELECT snapshot_name, schema_name, created_at, last_used "
                f"FROM `{self.REGISTRY}`.snapshots ORDER BY last_used DESC"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def clear(self) -> None:
        """Drop the whole cache (removes the container and all cached snapshots)."""
        _run(["docker", "rm", "-f", self.container_name])
        self._cont = None
        self.log(f"cleared snapshot cache ({self.container_name} removed)")

    # ---- registry helpers -----------------------------------------------------

    def _has(self, cont: Container, schema: str) -> bool:
        with cont.connect() as c, c.cursor() as cur:
            cur.execute(f"SELECT 1 FROM `{self.REGISTRY}`.snapshots WHERE schema_name=%s", (schema,))
            if not cur.fetchone():
                return False
            cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name=%s", (schema,))
            return cur.fetchone() is not None

    def _touch(self, cont: Container, schema: str) -> None:
        with cont.connect() as c, c.cursor() as cur:
            cur.execute(
                f"UPDATE `{self.REGISTRY}`.snapshots SET last_used=%s WHERE schema_name=%s",
                (datetime.datetime.now(), schema),
            )

    def _register(self, cont: Container, schema: str, name: str, spec: str) -> None:
        now = datetime.datetime.now()
        with cont.connect() as c, c.cursor() as cur:
            cur.execute(
                f"REPLACE INTO `{self.REGISTRY}`.snapshots "
                "(schema_name, snapshot_name, source_spec, created_at, last_used) "
                "VALUES (%s,%s,%s,%s,%s)",
                (schema, name, spec, now, now),
            )

    def _drop(self, cont: Container, schema: str) -> None:
        with cont.connect() as c, c.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{schema}`")
            cur.execute(f"DELETE FROM `{self.REGISTRY}`.snapshots WHERE schema_name=%s", (schema,))

    def _evict(self, cont: Container, keep: set[str]) -> None:
        with cont.connect() as c, c.cursor() as cur:
            cur.execute(f"SELECT schema_name FROM `{self.REGISTRY}`.snapshots ORDER BY last_used DESC")
            ordered = [r[0] for r in cur.fetchall()]
        for schema in ordered[self.max_snapshots :]:
            if schema in keep:
                continue
            self.log(f"evicting cached snapshot `{schema}` (over cap {self.max_snapshots})")
            self._drop(cont, schema)
