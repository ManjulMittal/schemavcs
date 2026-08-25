"""Minimal drivers for applying generated DDL to a real engine.

Tier 1 verification (D38): apply the migration and assert it executes. This does NOT
assert the resulting schema equals the intended snapshot -- that needs an introspector
per engine, which is a component in its own right and is deliberately deferred.

The distinction matters and is stated rather than blurred: these tests prove the
migration *runs*, which is exactly where naive emitters fail (ordering, intermediate
collisions, missing casts). They do not prove it produced the right schema.
"""
from __future__ import annotations

import os

MYSQL_URL_ENV = "SCHEMAVCS_MYSQL_URL"
PG_URL_ENV = "SCHEMAVCS_PG_URL"


class ApplyError(Exception):
    """A generated statement was rejected. Carries the statement, because 'the
    migration failed' without the offending SQL is unactionable."""

    def __init__(self, dialect: str, statement: str, index: int, cause: Exception):
        self.dialect, self.statement, self.index, self.cause = (
            dialect, statement, index, cause)
        super().__init__(
            f"{dialect} rejected statement {index + 1}:\n  {statement}\n"
            f"  -> {type(cause).__name__}: {cause}")


class Engine:
    dialect = ""

    def reset(self) -> None:                      # pragma: no cover - interface
        raise NotImplementedError

    def execute(self, sql: str) -> None:          # pragma: no cover - interface
        raise NotImplementedError

    def query(self, sql: str, params=()):         # pragma: no cover - interface
        """Read rows back. `params` is always bound by the driver, never formatted
        into `sql` -- these queries take table and column names as values when
        interrogating information_schema."""
        raise NotImplementedError

    def apply(self, script) -> None:
        """Run every statement in order, naming the one that fails."""
        for i, stmt in enumerate(script.statements):
            try:
                self.execute(stmt)
            except Exception as e:
                raise ApplyError(self.dialect, stmt, i, e) from e


class MySQLEngine(Engine):
    dialect = "mysql"

    def __init__(self, url: str):
        import pymysql
        from urllib.parse import urlparse
        u = urlparse(url)
        self._connect = lambda db=None: pymysql.connect(
            host=u.hostname or "127.0.0.1", port=u.port or 3306,
            user=u.username or "root", password=u.password or "",
            database=db, autocommit=True)
        self.db = (u.path or "/schemavcs_test").lstrip("/") or "schemavcs_test"
        self._conn = None

    def reset(self) -> None:
        admin = self._connect()
        with admin.cursor() as c:
            c.execute(f"DROP DATABASE IF EXISTS `{self.db}`")
            c.execute(f"CREATE DATABASE `{self.db}`")
        admin.close()
        if self._conn is not None:
            self._conn.close()
        self._conn = self._connect(self.db)

    def execute(self, sql: str) -> None:
        with self._conn.cursor() as c:
            c.execute(sql.rstrip(";"))

    def query(self, sql: str, params=()):
        with self._conn.cursor() as c:
            c.execute(sql.rstrip(";"), params or None)
            return c.fetchall()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()


class PostgresEngine(Engine):
    dialect = "postgres"

    def __init__(self, url: str):
        import psycopg
        self._psycopg = psycopg
        self.url = url
        self._conn = psycopg.connect(url, autocommit=True)

    def reset(self) -> None:
        with self._conn.cursor() as c:
            c.execute("DROP SCHEMA IF EXISTS public CASCADE")
            c.execute("CREATE SCHEMA public")

    def execute(self, sql: str) -> None:
        with self._conn.cursor() as c:
            c.execute(sql)

    def query(self, sql: str, params=()):
        with self._conn.cursor() as c:
            c.execute(sql, params or None)
            return c.fetchall()

    def close(self) -> None:
        self._conn.close()


def available() -> dict[str, str]:
    """Which engines this run can reach, from the environment."""
    out = {}
    if os.environ.get(MYSQL_URL_ENV):
        out["mysql"] = os.environ[MYSQL_URL_ENV]
    if os.environ.get(PG_URL_ENV):
        out["postgres"] = os.environ[PG_URL_ENV]
    return out
