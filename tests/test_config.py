"""Unit tests for the config model (no Docker)."""

from __future__ import annotations

from pathlib import Path

from dbdiff.config import Config


def test_defaults() -> None:
    c = Config.default()
    assert c.is_included("anything")
    assert c.key_for("t", ["id"]) == ["id"]
    assert c.ignored_columns("t") == set()


def test_load_full(tmp_path: Path) -> None:
    p = tmp_path / "c.yml"
    p.write_text(
        """
tables:
  include: ["app_*"]
  exclude: ["app_tmp"]
keys:
  order_items: [order_id, line_no]
ignore_columns:
  "*": [updated_at]
  users: last_login_at
mariadb:
  image: mariadb:10
  startup_timeout: 30
"""
    )
    c = Config.load(p)

    assert c.is_included("app_users")
    assert not c.is_included("app_tmp")
    assert not c.is_included("other")

    # explicit key override wins; otherwise fall back to the PK
    assert c.key_for("order_items", ["pk"]) == ["order_id", "line_no"]
    assert c.key_for("nope", ["id"]) == ["id"]

    # scalar ignore value coerces to a list; "*" merges with per-table
    assert c.ignored_columns("users") == {"updated_at", "last_login_at"}
    assert c.ignored_columns("orders") == {"updated_at"}

    assert c.mariadb.image == "mariadb:10"
    assert c.mariadb.startup_timeout == 30.0
