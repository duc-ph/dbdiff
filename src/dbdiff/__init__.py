"""dbdiff — diff two MySQL/MariaDB snapshots and browse the row-level changes.

A snapshot is either a live connection (``mysql://user:pass@host/db``) or a
gzipped mysqldump on S3 / disk (``s3://bucket/key.sql.gz``). Dumps are restored
into an ephemeral MariaDB container; the computed diff is persisted to a per-run
SQLite database that the web app reads from.
"""

__version__ = "0.1.0"
