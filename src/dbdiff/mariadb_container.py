"""Ephemeral MariaDB container used to restore ``.sql.gz`` dumps for querying.

The container is bound to 127.0.0.1 on a random port, torn down on exit, and
restores happen by streaming a gunzip of the dump into the container's
``mariadb`` client via ``docker exec -i``.
"""

from __future__ import annotations

import gzip
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import pymysql

# Ephemeral, bound to localhost only, and the container is removed after the run.
ROOT_PASSWORD = "dbdiff"

Logger = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def _client_for(image: str) -> str:
    """The in-container client binary: `mysql` for MySQL images, else `mariadb`."""
    return "mariadb" if "mariadb" in image.lower() else "mysql"


@dataclass
class Container:
    name: str
    host: str
    port: int
    password: str = ROOT_PASSWORD
    client: str = "mariadb"  # in-container CLI used for restores

    def connect(self, schema: str | None = None):
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user="root",
            password=self.password,
            database=schema,
            charset="utf8mb4",
            autocommit=True,
        )


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


@contextmanager
def ephemeral_mariadb(
    image: str = "mariadb:11",
    startup_timeout: float = 90.0,
    log: Logger = _noop,
) -> Iterator[Container]:
    name = f"dbdiff-{uuid.uuid4().hex[:12]}"
    log(f"starting {image} as {name} …")
    proc = _run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            # Set both vendors' vars so the same code works for mysql:* and mariadb:*.
            "-e", f"MYSQL_ROOT_PASSWORD={ROOT_PASSWORD}",
            "-e", f"MARIADB_ROOT_PASSWORD={ROOT_PASSWORD}",
            "-e", "MYSQL_ROOT_HOST=%",
            "-e", "MARIADB_ROOT_HOST=%",
            "-p", "127.0.0.1::3306",
            image,
            # The DB is a throwaway, so trade durability for fast bulk restores.
            "--innodb-flush-log-at-trx-commit=0",
            "--innodb-doublewrite=0",
            "--skip-log-bin",
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(f"`docker run` failed: {proc.stderr.strip()}")
    try:
        port = _mapped_port(name)
        cont = Container(name=name, host="127.0.0.1", port=port, client=_client_for(image))
        _wait_ready(cont, startup_timeout)
        log(f"mariadb ready on 127.0.0.1:{port}")
        yield cont
    finally:
        log(f"removing container {name}")
        _run(["docker", "rm", "-f", name])


def _mapped_port(name: str) -> int:
    last = ""
    for _ in range(100):
        p = _run(["docker", "port", name, "3306/tcp"])
        out = p.stdout.strip()
        if out:
            # e.g. "127.0.0.1:49153"
            return int(out.splitlines()[0].rsplit(":", 1)[1])
        last = p.stderr.strip() or out
        time.sleep(0.2)
    raise RuntimeError(f"could not determine mapped port for {name}: {last}")


def _wait_ready(cont: Container, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            cont.connect().close()
            return
        except Exception as e:  # connection refused / server starting up
            last_err = e
            time.sleep(0.5)
    raise RuntimeError(f"mariadb did not become ready in {timeout:.0f}s: {last_err}")


def restore_dump(
    cont: Container,
    schema: str,
    dump_path: Path,
    log: Logger = _noop,
) -> None:
    """Create ``schema`` and stream a (optionally gzipped) SQL dump into it.

    Assumes a single-database mysqldump (no ``CREATE DATABASE``/``USE``); the
    dump's objects land in ``schema``.
    """
    with cont.connect() as conn, conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{schema}` CHARACTER SET utf8mb4")

    log(f"restoring {dump_path.name} → `{schema}` …")
    proc = subprocess.Popen(
        [
            "docker", "exec",
            "-e", f"MYSQL_PWD={cont.password}",
            "-i", cont.name,
            cont.client, "-uroot", schema,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stderr is not None

    # Feed (decompressed) SQL from a writer thread so a full stderr pipe can
    # never deadlock against our writes; the main thread drains stderr.
    feed_error: list[Exception] = []

    def _feed() -> None:
        opener = gzip.open if str(dump_path).endswith(".gz") else open
        try:
            with opener(dump_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    proc.stdin.write(chunk)
        except Exception as e:  # noqa: BLE001 — surfaced after join
            feed_error.append(e)
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass

    writer = threading.Thread(target=_feed, daemon=True)
    writer.start()
    err = proc.stderr.read()
    writer.join()
    rc = proc.wait()

    if feed_error:
        raise RuntimeError(f"reading dump {dump_path.name} failed: {feed_error[0]}")
    if rc != 0:
        raise RuntimeError(
            f"restore of {dump_path.name} failed: "
            f"{err.decode('utf-8', 'replace').strip()}"
        )
