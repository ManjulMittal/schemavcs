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
import threading
from collections import OrderedDict
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

#: Commits already read or written in this process, keyed by (database, workspace, id).
#:
#: Safe to cache and never invalidate, because a commit is immutable: its id is a UUID
#: minted once when it is created, and nothing ever rewrites one. That is a property of
#: the model (D2 -- commits store whole snapshots, so there is no later mutation to
#: apply), not a convention this layer is choosing to rely on.
#:
#: It exists because a hosted database turns every read into a network round trip, and is
#: used only for those: a local file is already fast, and every in-process database shares
#: the path ":memory:", so caching them would key two unrelated databases together.
_COMMITS: "OrderedDict[tuple[str, str, str], Commit]" = OrderedDict()
_COMMIT_CACHE_MAX = 512


def _cache_commit(key, commit: Commit) -> None:
    _COMMITS[key] = commit
    # Bounded, oldest out first: snapshots are the largest thing this process holds, and
    # an unbounded cache in a long-lived server is a leak with extra steps.
    while len(_COMMITS) > _COMMIT_CACHE_MAX:
        _COMMITS.popitem(last=False)


#: Turso's dashboard and CLI show a database URL as `turso://host`. The driver does not
#: accept that scheme -- it falls through to "open a local file called turso://host" and
#: fails with a message about a local database, which is a confusing way to learn that
#: you copied the URL exactly as you were shown it. Rewriting it is friendlier than
#: documenting it.
_SCHEME_ALIASES = {"turso://": "libsql://"}


def normalize_url(url: str) -> str:
    for alias, real in _SCHEME_ALIASES.items():
        if url.startswith(alias):
            return real + url[len(alias):]
    return url


#: Live connections to remote databases, one per thread per database.
#:
#: A hosted database is a network hop, and this app opens a store more than once per
#: request -- so connecting per store cost two TLS handshakes before any query ran, and
#: made the landing page take ten seconds. Connections are therefore reused.
#:
#: Thread-local rather than a shared pool with a lock: uvicorn runs sync endpoints in a
#: worker threadpool, and a libSQL connection is not safe to hand between threads. One
#: connection per worker thread needs no locking and cannot interleave two requests
#: mid-statement.
_CONNECTIONS = threading.local()


def _remote_connection(url: str, auth_token: str):
    import libsql

    cache = getattr(_CONNECTIONS, "by_url", None)
    if cache is None:
        cache = _CONNECTIONS.by_url = {}
    db = cache.get(url)
    if db is None:
        # `isolation_level=None` is not a detail: libSQL's default opens an implicit
        # transaction and never commits it, so every write is silently discarded when
        # the connection closes. The sqlite3 path has always been autocommit; matching
        # it is what makes the two drivers interchangeable rather than merely similar.
        db = libsql.connect(url, auth_token=auth_token, isolation_level=None)
        db.executescript(SCHEMA)
        cache[url] = db
    return db

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
        self._shared = False
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    @classmethod
    def remote(cls, url: str, auth_token: str, *, workspace: str = "") -> "SqliteStore":
        """A Turso/libSQL database instead of a local file.

        The driver is imported here rather than at module scope so that importing the
        storage package never loads a native extension it may not use.
        """
        self = cls.__new__(cls)
        self.path = normalize_url(url)
        self.workspace = workspace
        self._shared = True
        self._db = _remote_connection(self.path, auth_token)
        return self

    def close(self) -> None:
        # A pooled connection outlives the store that borrowed it: closing it here
        # would break the next request on this thread, and there is nothing to flush
        # because every statement is already committed.
        if not self._shared:
            self._db.close()

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ commits
    def _key(self, commit_id: str) -> tuple[str, str, str]:
        return (self.path, self.workspace, commit_id)

    def put_commit(self, commit: Commit) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO commits (workspace, id, parents, message, snapshot) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.workspace, commit.id, json.dumps(list(commit.parents)),
             commit.message, json.dumps(commit.snapshot.to_dict())))
        # Write-through: a commit is almost always read back immediately -- the merge
        # that just wrote it needs its parents -- and seeding the demo wrote seven
        # commits and then read fifteen.
        if self._shared:
            _cache_commit(self._key(commit.id), commit)

    def get_commit(self, commit_id: str) -> Commit:
        key = self._key(commit_id)
        if self._shared and (cached := _COMMITS.get(key)) is not None:
            return cached
        row = self._db.execute(
            "SELECT id, parents, message, snapshot FROM commits "
            "WHERE workspace = ? AND id = ?",
            (self.workspace, commit_id)).fetchone()
        if row is None:
            raise KeyError(f"no such commit: {commit_id}")
        commit = Commit(id=row[0], parents=tuple(json.loads(row[1])), message=row[2],
                        snapshot=Snapshot.from_dict(json.loads(row[3])))
        if self._shared:
            _cache_commit(key, commit)
        return commit

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
