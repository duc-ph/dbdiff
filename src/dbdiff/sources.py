"""Resolve a source spec into something the engine can read.

Accepted specs:
  * ``mysql://user:pass@host:3306/dbname``  -> live connection (used directly)
  * ``s3://bucket/path/snap.sql.gz``         -> downloaded, then restored
  * ``file:///abs/path/snap.sql.gz`` or a    -> restored
    local path ending in .sql / .sql.gz
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from .mysql_io import MySQLEndpoint

Logger = Callable[[str], None]


def _noop(_: str) -> None:
    pass


@dataclass
class LiveSource:
    """A live MySQL/MariaDB connection given as a DSN."""

    dsn: str

    def endpoint(self) -> MySQLEndpoint:
        u = urlparse(self.dsn)
        schema = u.path.lstrip("/")
        if not schema:
            raise ValueError(f"DSN is missing a database name: {self.dsn}")
        return MySQLEndpoint(
            host=u.hostname or "127.0.0.1",
            port=u.port or 3306,
            user=unquote(u.username or "root"),
            password=unquote(u.password or ""),
            schema=schema,
        )

    @property
    def label(self) -> str:
        u = urlparse(self.dsn)
        return f"live:{u.hostname or '127.0.0.1'}/{u.path.lstrip('/')}"

    @property
    def needs_restore(self) -> bool:
        return False


@dataclass
class DumpSource:
    """A mysqldump (.sql / .sql.gz) on disk that must be restored before querying."""

    origin: str  # original spec, used for display
    local_path: Path

    @property
    def label(self) -> str:
        return os.path.basename(self.origin)

    @property
    def needs_restore(self) -> bool:
        return True


def parse_spec(spec: str, download_dir: Path, log: Logger = _noop) -> LiveSource | DumpSource:
    spec = spec.strip()
    lower = spec.lower()

    if lower.startswith(("mysql://", "mariadb://")):
        return LiveSource(dsn=spec)

    if lower.startswith("s3://"):
        local = _download_s3(spec, download_dir, log)
        return DumpSource(origin=spec, local_path=local)

    if lower.startswith("file://"):
        path = Path(unquote(urlparse(spec).path))
        return DumpSource(origin=spec, local_path=_require_file(path))

    # bare local path
    path = Path(spec).expanduser()
    if path.suffix in {".gz", ".sql"} or "".join(path.suffixes).endswith(".sql.gz"):
        return DumpSource(origin=spec, local_path=_require_file(path))

    raise ValueError(
        f"unrecognized source spec: {spec!r} "
        "(expected mysql://…, s3://…/x.sql.gz, or a path to a .sql/.sql.gz file)"
    )


def safe_label(spec: str) -> str:
    """A short, password-free display label for a source spec."""
    s = spec.strip()
    low = s.lower()
    if low.startswith(("mysql://", "mariadb://")):
        u = urlparse(s)
        return f"live:{u.hostname or '127.0.0.1'}/{u.path.lstrip('/')}"
    if low.startswith(("s3://", "file://")):
        return os.path.basename(urlparse(s).path) or s
    return os.path.basename(s) or s


def redact(spec: str) -> str:
    """Mask the password in a DSN so it is safe to persist/display."""
    if spec.lower().startswith(("mysql://", "mariadb://")):
        u = urlparse(spec)
        if u.password:
            return spec.replace(f":{u.password}@", ":***@", 1)
    return spec


def _require_file(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"dump file not found: {path}")
    return path


def _download_s3(uri: str, download_dir: Path, log: Logger) -> Path:
    import boto3  # imported lazily so non-S3 runs don't pay for it

    u = urlparse(uri)
    bucket, key = u.netloc, u.path.lstrip("/")
    download_dir.mkdir(parents=True, exist_ok=True)
    dest = download_dir / os.path.basename(key)
    log(f"downloading s3://{bucket}/{key} …")
    boto3.client("s3").download_file(bucket, key, str(dest))
    log(f"downloaded {dest.name} ({dest.stat().st_size:,} bytes)")
    return dest
