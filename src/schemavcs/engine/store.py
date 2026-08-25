"""Commit DAG and branch refs.

Commits store whole snapshots rather than operation logs (D2): reading any commit
costs one lookup, and identity (D1) already makes renames explicit without a log to
replay.

`Repo` owns the *semantics* -- what a commit means, what makes a head stale, how
history is walked. It delegates *durability* to a `Store`, which is the seam that lets
the same 250-odd tests run against both an in-memory dict and a real database. An
abstraction exercised through one implementation is not actually abstract, and the
in-memory store can trivially pass the one test that matters most here: concurrent
head updates. So both run everything.

Note what is NOT here: any mention of a specific database. The engine defines the
contract; picking a datastore is the storage layer's job (D5, D10) -- and the
architecture test enforces exactly that by refusing to let the word appear in this
package.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol


class StaleHeadError(Exception):
    """A write lost a race: the branch moved since the caller last read it."""

    def __init__(self, branch: str, expected: str, actual: str):
        self.branch, self.expected, self.actual = branch, expected, actual
        super().__init__(
            f"branch {branch!r} has moved: expected head {expected}, found {actual}. "
            "Re-read the branch and retry -- your changes were not applied."
        )


@dataclass(frozen=True)
class Commit:
    id: str
    snapshot: object
    parents: tuple[str, ...]
    message: str = ""

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


# ============================================================== the durability seam
class Store(Protocol):
    """Everything `Repo` needs from a datastore, and nothing more.

    Deliberately narrow: no queries reach *inside* a snapshot, because nothing ever
    needs to. That is what licenses storing snapshots as opaque blobs (D34) instead
    of modeling the schema model a second time in SQL.
    """

    def put_commit(self, commit: Commit) -> None: ...

    def get_commit(self, commit_id: str) -> Commit: ...

    def commit_count(self) -> int: ...

    def create_branch(self, name: str, head: str, *, branch_point: str) -> None: ...

    def branch_head(self, name: str) -> str: ...

    def branch_names(self) -> list[str]: ...

    def branch_point(self, name: str) -> str: ...

    def compare_and_set_head(self, name: str, *, expected: str, new: str) -> bool:
        """Atomically move a head, but only if it is still where the caller thinks.

        Returns False on a lost race rather than raising, so the caller decides how
        loud to be. This single method is the whole of the concurrency story (D25).
        """
        ...


class InMemoryStore:
    """Dicts. Used by the entire test suite and by nothing in production."""

    def __init__(self):
        self._commits: dict[str, Commit] = {}
        self._branches: dict[str, str] = {}
        self._points: dict[str, str] = {}

    def put_commit(self, commit: Commit) -> None:
        self._commits[commit.id] = commit

    def get_commit(self, commit_id: str) -> Commit:
        try:
            return self._commits[commit_id]
        except KeyError:
            raise KeyError(f"no such commit: {commit_id}") from None

    def commit_count(self) -> int:
        return len(self._commits)

    def create_branch(self, name: str, head: str, *, branch_point: str) -> None:
        if name in self._branches:
            raise ValueError(f"branch already exists: {name!r}")
        self._branches[name] = head
        self._points[name] = branch_point

    def branch_head(self, name: str) -> str:
        try:
            return self._branches[name]
        except KeyError:
            raise KeyError(f"no such branch: {name!r}") from None

    def branch_names(self) -> list[str]:
        return sorted(self._branches)

    def branch_point(self, name: str) -> str:
        return self._points[name]

    def compare_and_set_head(self, name: str, *, expected: str, new: str) -> bool:
        if self._branches.get(name) != expected:
            return False
        self._branches[name] = new
        return True


# ========================================================================== the repo
class Repo:
    def __init__(self, store: Store | None = None):
        self.store: Store = store if store is not None else InMemoryStore()

    # ------------------------------------------------------------- setup
    @classmethod
    def init(cls, default_branch: str = "main", store: Store | None = None) -> "Repo":
        from ..model import Snapshot
        r = cls(store)
        genesis = r._write(Snapshot(), parents=(), message="genesis")
        r.store.create_branch(default_branch, genesis.id, branch_point=genesis.id)
        return r

    @classmethod
    def open(cls, store: Store) -> "Repo":
        """Attach to a store that already holds a history. No genesis commit."""
        return cls(store)

    def _write(self, snapshot, parents: tuple[str, ...], message: str) -> Commit:
        c = Commit(id=str(uuid.uuid4()), snapshot=snapshot, parents=parents,
                   message=message)
        self.store.put_commit(c)
        return c

    # -------------------------------------------------------------- refs
    def head(self, branch: str) -> Commit:
        return self.store.get_commit(self.store.branch_head(branch))

    def snapshot(self, branch: str):
        return self.head(branch).snapshot

    def snapshot_at(self, commit_id: str):
        return self.store.get_commit(commit_id).snapshot

    def branches(self) -> list[str]:
        return self.store.branch_names()

    def commit_count(self) -> int:
        return self.store.commit_count()

    def branch(self, name: str, from_branch: str) -> str:
        at = self.head(from_branch).id
        # The branch point is recorded ONLY so L-21 can demonstrate what the naive
        # implementation would do. Nothing in the merge path reads it (D13).
        self.store.create_branch(name, at, branch_point=at)
        return at

    def branch_point(self, name: str) -> str:
        """The commit a branch was created from. Deliberately NOT the merge base."""
        return self.store.branch_point(name)

    def set_head(self, branch: str, new_head: str, *, expected: str) -> None:
        """Move a branch, refusing if it has moved underneath us."""
        if not self.store.compare_and_set_head(branch, expected=expected,
                                               new=new_head):
            raise StaleHeadError(branch, expected, self.store.branch_head(branch))

    # ----------------------------------------------------------- commits
    def commit(self, branch: str, snapshot, *, message: str = "",
               expected_head: str | None = None) -> Commit:
        current = self.head(branch)
        if expected_head is not None and expected_head != current.id:
            raise StaleHeadError(branch, expected_head, current.id)
        if snapshot == current.snapshot:
            raise ValueError(f"no changes to commit on {branch!r}")
        c = self._write(snapshot, parents=(current.id,), message=message)
        self.set_head(branch, c.id, expected=current.id)
        return c

    def merge_commit(self, branch: str, *, ours: str, theirs: str, snapshot,
                     message: str = "", expected_head: str | None = None) -> Commit:
        """Two-parent commit. `ours` must be the branch's current head."""
        current = self.head(branch)
        if expected_head is not None and expected_head != current.id:
            raise StaleHeadError(branch, expected_head, current.id)
        if ours != current.id:
            raise ValueError(
                f"merge target moved: {branch!r} is at {current.id}, not {ours}")
        c = self._write(snapshot, parents=(ours, theirs), message=message)
        self.set_head(branch, c.id, expected=current.id)
        return c

    # ----------------------------------------------------------- history
    def history(self, branch: str) -> list[Commit]:
        """Reachable commits, newest first. Breadth-first so merges don't recurse deep."""
        seen, order, queue = set(), [], [self.head(branch).id]
        while queue:
            cid = queue.pop(0)
            if cid in seen:
                continue
            seen.add(cid)
            c = self.store.get_commit(cid)
            order.append(c)
            queue.extend(c.parents)
        return order

    def parents_of(self, commit_id: str) -> tuple[str, ...]:
        return self.store.get_commit(commit_id).parents
