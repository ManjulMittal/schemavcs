"""Version control engine.

Dialect-neutral by construction (D5) and enforced by tests/unit/test_architecture.py:
nothing in this package knows what Postgres or MySQL is.
"""
from .align import align_identity
from .diff import (AttributeDelta, Change, ChangeKind, Diff, diff,
                   UnalignedSnapshotsError)
from .merge import (Conflict, ConflictCategory, MergeResult, MergeStatus,
                    Resolution, Side, StaleConflictsError, UnresolvedConflictsError,
                    Violation, merge, merge_branches, BranchMerge)
from .merge_base import (MultipleMergeBasesError, is_ancestor, is_fast_forward,
                         merge_base, merge_bases)
from .store import Commit, InMemoryStore, Repo, StaleHeadError, Store

__all__ = [
    "Repo", "Commit", "StaleHeadError", "Store", "InMemoryStore",
    "diff", "Diff", "align_identity", "UnalignedSnapshotsError", "Change", "ChangeKind", "AttributeDelta",
    "merge_base", "merge_bases", "MultipleMergeBasesError",
    "merge", "merge_branches", "BranchMerge", "MergeResult", "MergeStatus", "Conflict", "ConflictCategory",
    "Violation", "Resolution", "Side",
    "StaleConflictsError", "UnresolvedConflictsError",
    "is_ancestor", "is_fast_forward",
]
