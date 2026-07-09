"""Unit tests for source-spec parsing and label sanitization (no Docker/S3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbdiff.sources import DumpSource, LiveSource, parse_spec, redact, safe_label


def test_parse_live_dsn(tmp_path: Path) -> None:
    s = parse_spec("mysql://u:p@h:3307/db", tmp_path)
    assert isinstance(s, LiveSource)
    ep = s.endpoint()
    assert (ep.host, ep.port, ep.user, ep.password, ep.schema) == ("h", 3307, "u", "p", "db")
    assert s.label == "live:h/db"
    assert not s.needs_restore


def test_parse_local_dump(tmp_path: Path) -> None:
    f = tmp_path / "snap.sql.gz"
    f.write_bytes(b"-- dump")
    s = parse_spec(str(f), tmp_path)
    assert isinstance(s, DumpSource)
    assert s.needs_restore
    assert s.label == "snap.sql.gz"


def test_parse_file_uri(tmp_path: Path) -> None:
    f = tmp_path / "d.sql"
    f.write_text("")
    s = parse_spec(f"file://{f}", tmp_path)
    assert isinstance(s, DumpSource)


def test_parse_missing_dump(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_spec(str(tmp_path / "nope.sql.gz"), tmp_path)


def test_parse_unknown_spec(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        parse_spec("ftp://foo/bar", tmp_path)


def test_dsn_missing_database(tmp_path: Path) -> None:
    s = parse_spec("mysql://u:p@h/", tmp_path)
    with pytest.raises(ValueError):
        s.endpoint()


def test_safe_label() -> None:
    assert safe_label("mysql://u:secret@host:3306/app") == "live:host/app"
    assert safe_label("s3://bucket/path/snap.sql.gz") == "snap.sql.gz"
    assert safe_label("/tmp/a/b.sql.gz") == "b.sql.gz"


def test_redact_hides_password() -> None:
    assert redact("mysql://u:secret@host/app") == "mysql://u:***@host/app"
    assert "secret" not in redact("mysql://u:secret@host/app")
    assert redact("s3://b/k.sql.gz") == "s3://b/k.sql.gz"
