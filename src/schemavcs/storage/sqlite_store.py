"""A `Store` backed by SQLite -- or by libSQL/Turso, which speaks the same SQL.

Two design calls worth reading before the code.

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
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..engine.store import Commit
from ..model import Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS commits (
    id       TEXT PRIMARY KEY,
    parents  TEXT NOT NULL,          -- JSON array, ordered: [ours, theirs]
    message  TEXT NOT NULL DEFAULT '',
    snapshot TEXT NOT NULL           -- JSON, opaque to SQL by design (D34)
);
CREATE TABLE IF NOT EXISTS branches (
    name         TEXT PRIMARY KEY,
    head         TEXT NOT NULL REFERENCES commits(id),
    branch_point TEXT NOT NULL REFERENCES commits(id)
);
"""


class SqliteStore:
    """Durable store. `path=None` gives an in-process database, which is still a real
    SQLite engine -- so the CAS path is exercised for real in tests that use it."""

    def __init__(self, path: str | Path | None = None):
        self.path = ":memory:" if path is None else str(path)
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ commits
    def put_commit(self, commit: Commit) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO commits (id, parents, message, snapshot) "
            "VALUES (?, ?, ?, ?)",
            (commit.id, json.dumps(list(commit.parents)), commit.message,
             json.dumps(commit.snapshot.to_dict())))

    def get_commit(self, commit_id: str) -> Commit:
        row = self._db.execute(
            "SELECT id, parents, message, snapshot FROM commits WHERE id = ?",
            (commit_id,)).fetchone()
        if row is None:
            raise KeyError(f"no such commit: {commit_id}")
        return Commit(id=row[0], parents=tuple(json.loads(row[1])), message=row[2],
                      snapshot=Snapshot.from_dict(json.loads(row[3])))

    def commit_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM commits").fetchone()[0]

    # ----------------------------------------------------------- branches
    def create_branch(self, name: str, head: str, *, branch_point: str) -> None:
        try:
            self._db.execute(
                "INSERT INTO branches (name, head, branch_point) VALUES (?, ?, ?)",
                (name, head, branch_point))
        except sqlite3.IntegrityError as e:
            if "UNIQUE" in str(e) or "PRIMARY KEY" in str(e):
                raise ValueError(f"branch already exists: {name!r}") from None
            raise

    def branch_head(self, name: str) -> str:
        row = self._db.execute("SELECT head FROM branches WHERE name = ?",
                               (name,)).fetchone()
        if row is None:
            raise KeyError(f"no such branch: {name!r}")
        return row[0]

    def branch_names(self) -> list[str]:
        return [r[0] for r in self._db.execute(
            "SELECT name FROM branches ORDER BY name")]

    def branch_point(self, name: str) -> str:
        row = self._db.execute("SELECT branch_point FROM branches WHERE name = ?",
                               (name,)).fetchone()
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
            "UPDATE branches SET head = ? WHERE name = ? AND head = ?",
            (new, name, expected))
        return cur.rowcount == 1
