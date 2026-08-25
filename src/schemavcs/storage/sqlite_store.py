"""A `Store` backed by SQLite, or by libSQL/Turso, which speaks the same SQL.

Three design calls worth reading before the code.

**Snapshots are stored as JSON blobs, not normalized into relational tables** (D34).
Shredding tables/columns/constraints into rows is the obvious move and the wrong one:
nothing ever queries *inside* a snapshot -- every read is "give me the whole schema at
commit X" -- so normalizing would mean modeling the schema model a second time, in SQL,
with its own migration burden, for zero query benefit. `Snapshot.to_dict` already
round-trips losslessly and is property-tested (M-93).

**Moving a branch head is a single conditional UPDATE.** That is what makes optimistic
concurrency real rather than aspirational (D25): the `WHERE head = ?` clause is the
compare-and-swap, enforced by the database rather than by a check-then-write in Python
that two requests can interleave through.

**Every row is scoped to a workspace** (D51). One deployed database holds every
visitor's repo, so `workspace` is part of both primary keys and appears in every
statement. It is not a security boundary -- see D42 -- but it is the thing that stops
one visitor's `main` from being another's, and it is why no query here may omit it.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..engine.store import Commit
from ..model import Snapshot

#: libSQL is a SQLite fork and matches it almost exactly -- same `?` placeholders, same
#: `rowcount` semantics for the compare-and-swap, which is the part that had to match.
#: It does differ on how it reports a constraint violation: `sqlite3` raises
#: `IntegrityError`, `libsql` raises a plain `ValueError`. Both are caught and then
#: filtered on the message, so a `ValueError` that is not a uniqueness violation still
#: propagates rather than being silently retold as "branch already exists".
INTEGRITY_ERRORS: tuple[type[BaseException], ...] = (sqlite3.IntegrityError, ValueError)

#: Remote databases whose schema statements have already been run in this process.
_INITIALIZED: set[str] = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS commits (
    workspace TEXT NOT NULL,
    id        TEXT NOT NULL,
    parents   TEXT NOT NULL,          -- JSON array, ordered: [ours, theirs]
    message   TEXT NOT NULL DEFAULT '',
    snapshot  TEXT NOT NULL,          -- JSON, opaque to SQL by design (D34)
    PRIMARY KEY (workspace, id)
);
CREATE TABLE IF NOT EXISTS branches (
    name         TEXT NOT NULL,
    workspace    TEXT NOT NULL,
    head         TEXT NOT NULL,
    branch_point TEXT NOT NULL,
    PRIMARY KEY (workspace, name),
    FOREIGN KEY (workspace, head)         REFERENCES commits(workspace, id),
    FOREIGN KEY (workspace, branch_point) REFERENCES commits(workspace, id)
);
"""


class SqliteStore:
    """Durable store. `path=None` gives an in-process database, which is still a real
    SQLite engine -- so the CAS path is exercised for real in tests that use it.

    `workspace` partitions the tables. The default is a single unnamed workspace, which
    is what every engine-level test wants: they care about commits and branches, not
    about who owns them.
    """

    def __init__(self, path: str | Path | None = None, *, workspace: str = ""):
        self.path = ":memory:" if path is None else str(path)
        self.workspace = workspace
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    @classmethod
    def remote(cls, url: str, auth_token: str, *, workspace: str = "") -> "SqliteStore":
        """A Turso/libSQL database instead of a local file.

        The driver is imported here rather than at module scope so that importing the
        storage package never loads a native extension it may not use.
        """
        import libsql

        self = cls.__new__(cls)
        self.path = url
        self.workspace = workspace
        # `isolation_level=None` is not a detail: libSQL's default opens an implicit
        # transaction and never commits it, so every write is silently discarded when
        # the connection closes -- and a connection closes at the end of every request.
        # The sqlite3 path has always passed this; matching it is what makes the two
        # drivers interchangeable rather than merely similar.
        self._db = libsql.connect(url, auth_token=auth_token, isolation_level=None)
        # A connection is opened per request, and the schema statements are a network
        # round trip each -- so they run once per process per database rather than on
        # every request. Deliberately not applied to local stores: `:memory:` is a new
        # empty database on every connect, so skipping its setup would leave it
        # tableless.
        if url not in _INITIALIZED:
            self._db.executescript(SCHEMA)
            _INITIALIZED.add(url)
        return self

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ commits
    def put_commit(self, commit: Commit) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO commits (workspace, id, parents, message, snapshot) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.workspace, commit.id, json.dumps(list(commit.parents)),
             commit.message, json.dumps(commit.snapshot.to_dict())))

    def get_commit(self, commit_id: str) -> Commit:
        row = self._db.execute(
            "SELECT id, parents, message, snapshot FROM commits "
            "WHERE workspace = ? AND id = ?",
            (self.workspace, commit_id)).fetchone()
        if row is None:
            raise KeyError(f"no such commit: {commit_id}")
        return Commit(id=row[0], parents=tuple(json.loads(row[1])), message=row[2],
                      snapshot=Snapshot.from_dict(json.loads(row[3])))

    def commit_count(self) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM commits WHERE workspace = ?",
            (self.workspace,)).fetchone()[0]

    # ----------------------------------------------------------- branches
    def create_branch(self, name: str, head: str, *, branch_point: str) -> None:
        try:
            self._db.execute(
                "INSERT INTO branches (workspace, name, head, branch_point) "
                "VALUES (?, ?, ?, ?)",
                (self.workspace, name, head, branch_point))
        except INTEGRITY_ERRORS as e:
            if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e):
                raise ValueError(f"branch already exists: {name!r}") from None
            raise

    def branch_head(self, name: str) -> str:
        row = self._db.execute(
            "SELECT head FROM branches WHERE workspace = ? AND name = ?",
            (self.workspace, name)).fetchone()
        if row is None:
            raise KeyError(f"no such branch: {name!r}")
        return row[0]

    def branch_names(self) -> list[str]:
        # `fetchall()` rather than iterating the cursor: sqlite3 cursors are iterable
        # and libSQL's are not, and this is the only place that difference shows.
        return [r[0] for r in self._db.execute(
            "SELECT name FROM branches WHERE workspace = ? ORDER BY name",
            (self.workspace,)).fetchall()]

    def branch_point(self, name: str) -> str:
        row = self._db.execute(
            "SELECT branch_point FROM branches WHERE workspace = ? AND name = ?",
            (self.workspace, name)).fetchone()
        if row is None:
            raise KeyError(f"no such branch: {name!r}")
        return row[0]

    def compare_and_set_head(self, name: str, *, expected: str, new: str) -> bool:
        """The whole concurrency story, in one statement.

        A check-then-write in Python would leave a window two requests can interleave
        through; the `WHERE head = ?` makes the database arbitrate. Zero rows updated
        means we lost the race -- which is information, not an error, so the caller
        decides how loud to be.
        """
        cur = self._db.execute(
            "UPDATE branches SET head = ? WHERE workspace = ? AND name = ? AND head = ?",
            (new, self.workspace, name, expected))
        return cur.rowcount == 1
