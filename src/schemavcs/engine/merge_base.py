"""Merge base computation -- the headline decision (D13).

Everybody stores the commit a branch was created from and calls it the merge base. That
is correct exactly once. After `main` has been merged into `feature`, the correct base
has advanced; keep using the stored branch point and every conflict already resolved
comes back and asks to be resolved again, forever. That specific behaviour is what makes
people abandon a merge tool.

What the base actually advances to is worth stating precisely, because it is easy to get
wrong (the project docs had it wrong at first): with `main = G->A->B`, `feature` branched
at `A` to `C`, and `main` merged into `feature` creating `M(C, B)`, the base for the next
merge is **B** -- the tip `main` had at merge time. Not `M`, and not `A`. A merge commit
can be the LCA, but only in cross-merge topologies where it is an ancestor of both sides.
"""
from __future__ import annotations


class MultipleMergeBasesError(Exception):
    """Criss-cross history: two or more incomparable candidate bases (D14).

    Real git merges the candidates recursively. That is correct and out of reach here, and
    picking one arbitrarily would put a subtly wrong result behind a confident UI -- so
    this refuses instead.
    """

    def __init__(self, candidates: list[str]):
        self.candidates = candidates
        super().__init__(
            f"ambiguous merge base: {len(candidates)} incomparable candidates "
            f"({', '.join(c[:8] for c in candidates)}). These branches have already "
            "merged each other, so no single common ancestor is closest. Merge one "
            "direction first, then retry."
        )


def _ancestors(repo, commit_id: str) -> set[str]:
    """Every commit reachable from `commit_id`, including itself."""
    seen, stack = set(), [commit_id]
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        stack.extend(repo.parents_of(cid))
    return seen


def merge_bases(repo, a: str, b: str) -> list[str]:
    """All lowest common ancestors: common ancestors with no common-ancestor descendant.

    A common ancestor is "lowest" when none of its descendants is also a common
    ancestor. Rather than test descendancy directly, we mark every *parent* of a
    common ancestor as not-lowest -- reachability upward is the direction the DAG
    stores, so this needs no reverse index.
    """
    common = _ancestors(repo, a) & _ancestors(repo, b)
    if not common:
        return []

    superseded: set[str] = set()
    for cid in common:
        for p in repo.parents_of(cid):
            if p in common:
                superseded.add(p)
    return sorted(common - superseded)


def merge_base(repo, a: str, b: str) -> str:
    """The single merge base, or raise if the history is criss-crossed."""
    bases = merge_bases(repo, a, b)
    if not bases:
        raise ValueError("no common ancestor: unrelated histories")
    if len(bases) > 1:
        raise MultipleMergeBasesError(bases)
    return bases[0]


def is_ancestor(repo, maybe_ancestor: str, of: str) -> bool:
    return maybe_ancestor in _ancestors(repo, of)


def is_fast_forward(repo, target_head: str, source_head: str) -> bool:
    """True when the target is wholly contained in the source: no three-way merge needed."""
    return is_ancestor(repo, target_head, source_head)
