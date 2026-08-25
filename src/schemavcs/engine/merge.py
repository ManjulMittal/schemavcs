"""Three-way merge.

The shape of this module is the whole design, so it is worth stating up front.

Merging happens at **attribute level**, keyed on object **identity** (D1, D3). For
every object that exists in the base, the merge asks one question per attribute: did
each side move this slot, and if so, to the same place? That single question produces
the entire auto-merge story -- a rename on one branch and a retype on the other touch
different slots, so there is nothing to reconcile (M-01).

Four of the five conflict categories fall out of that pairwise walk. The fifth cannot:
two engineers can each make a locally valid change that is *jointly* invalid -- one
renames `email` to `contact` while the other adds a new `contact` column. Pairwise
there is no overlap at all: different ids, different attribute sets. The only way to
catch it is to build the merged result and validate it as a whole (D11, M-80). That is
why `_validate` exists and why it runs on the *result* rather than on the inputs.

Ordered column lists are treated as ATOMIC values (D4). Merging two orderings
element-wise would produce a third ordering that neither engineer designed and neither
would notice -- so `columns` is one slot, all-or-nothing.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from ..model.objects import ConstraintKind
from ..model.snapshot import Snapshot

#: Attributes merged independently, per object kind. Adding an attribute to the model
#: without adding it here would silently drop that attribute from every merge, so the
#: lists are asserted against the dataclasses in tests/unit/test_merge_coverage.py.
MERGED_ATTRS = {
    "table": ("name",),
    "column": ("name", "type", "nullable", "default", "autoincrement"),
    "index": ("name", "columns", "unique", "where"),
    "constraint": ("name", "kind", "column_ids", "ref_table_id", "ref_column_ids",
                   "on_delete", "on_update", "expression"),
}


class ConflictCategory(str, Enum):
    #: One side deleted an object the other side modified.
    DELETE_MODIFY = "delete_modify"
    #: Both sides independently created objects with the same name.
    NAME_COLLISION = "name_collision"
    #: Both sides moved the same attribute of the same object, to different values.
    ATTRIBUTE = "attribute"


class MergeStatus(str, Enum):
    """Outcomes, shared by snapshot-level and branch-level merges.

    `merge()` returns CLEAN / CONFLICTED / INVALID. `merge_branches()` can also
    return UP_TO_DATE or FAST_FORWARD, which are decided from the DAG alone and
    never reach the merge algorithm at all.
    """
    CLEAN = "clean"
    CONFLICTED = "conflicted"
    INVALID = "invalid"          # merged cleanly, but the result violates an invariant
    UP_TO_DATE = "up_to_date"    # nothing to merge
    FAST_FORWARD = "fast_forward"
    MERGED = "merged"            # a real two-parent merge commit was written


@dataclass(frozen=True)
class Conflict:
    category: ConflictCategory
    object_kind: str                 # table | column | index | constraint
    object_id: str
    path: str                        # "users.email" -- for humans
    message: str
    attribute: str | None = None
    base: Any = None
    ours: Any = None
    theirs: Any = None
    #: For NAME_COLLISION, the id of the *other* colliding object.
    other_id: str | None = None

    @property
    def key(self) -> str:
        """Stable, serializable handle. The resolution payload comes back from a
        browser, so a conflict has to be addressable by a plain string."""
        return f"{self.category.value}:{self.object_kind}:{self.object_id}:" \
               f"{self.attribute or '-'}"


@dataclass(frozen=True)
class Violation:
    invariant: str
    message: str
    objects: tuple[str, ...] = ()


class Side(str, Enum):
    OURS = "ours"
    THEIRS = "theirs"


@dataclass(frozen=True)
class Resolution:
    """One conflict's answer: pick a side, or supply a third value (M-107)."""
    side: Side | None = None
    value: Any = None
    has_value: bool = False

    @classmethod
    def ours(cls) -> "Resolution":
        return cls(side=Side.OURS)

    @classmethod
    def theirs(cls) -> "Resolution":
        return cls(side=Side.THEIRS)

    @classmethod
    def with_value(cls, value) -> "Resolution":
        return cls(value=value, has_value=True)


@dataclass(frozen=True)
class MergeResult:
    status: MergeStatus
    merged: Snapshot | None
    conflicts: tuple[Conflict, ...] = ()
    violations: tuple[Violation, ...] = ()

    @property
    def is_clean(self) -> bool:
        return self.status is MergeStatus.CLEAN

    @property
    def token(self) -> str:
        """Fingerprint of the conflict set.

        A resolution is submitted against the conflicts the user was *shown*. If the
        branches moved in between, those choices no longer describe reality -- so the
        token is checked and a mismatch is rejected rather than silently applied
        (D12, M-106).
        """
        return _token(self.conflicts)


class StaleConflictsError(Exception):
    """Resolutions submitted against a conflict set that no longer matches."""


class UnresolvedConflictsError(Exception):
    """Resolution is all-or-nothing (D12): a partial answer is not applied.

    Applying half of a resolution would leave a schema that neither engineer
    designed, and there is no persisted mid-merge state to resume from -- so the
    only safe response is to refuse and name what is missing.
    """

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(
            f"{len(missing)} conflict(s) still unresolved: {', '.join(missing)}. "
            "Resolution is all-or-nothing; resubmit with every conflict answered."
        )


# ===================================================================== entry point
def merge(base, ours, theirs, *, resolutions=None, token=None) -> MergeResult:
    """Three-way merge of `ours` and `theirs` over their common ancestor `base`.

    With `resolutions`, every conflict must be answered or the call is rejected.
    """
    resolutions = resolutions or {}

    m = _Merger(base, ours, theirs, resolutions)
    merged = m.run()

    if resolutions:
        if token is not None and token != _token(m.conflicts_seen):
            raise StaleConflictsError(
                "the branches changed since these conflicts were computed; "
                "re-run the merge and resolve against the current conflict set")
        unanswered = [c.key for c in m.conflicts_seen if c.key not in resolutions]
        if unanswered:
            raise UnresolvedConflictsError(unanswered)

    unresolved = tuple(c for c in m.conflicts_seen if c.key not in resolutions)
    if unresolved:
        return MergeResult(MergeStatus.CONFLICTED, None, unresolved)

    violations = tuple(_validate(merged))
    if violations:
        return MergeResult(MergeStatus.INVALID, merged, (), violations)
    return MergeResult(MergeStatus.CLEAN, merged)


def _token(conflicts) -> str:
    """Hashes the *values*, not just the keys (D43).

    Keying on identity alone looked sufficient until the UI existed. A conflict key is
    `attribute:column:<id>:type`, which is unchanged if the other branch re-edits the
    same attribute to a different value -- so "take theirs", chosen while looking at
    `text`, would silently apply `varchar(200)`. The user is answering a question about
    values, so the values are what the token has to pin.
    """
    payload = sorted(
        [c.key, _show(c.base), _show(c.ours), _show(c.theirs)] for c in conflicts)
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


# ========================================================================= merging
class _Merger:
    """Walks base/ours/theirs in parallel by identity.

    Conflicts are *collected* rather than raised, and merging continues, because a
    user needs to see every conflict at once to judge whether to proceed -- surfacing
    them one round-trip at a time is the worst possible resolution UX.
    """

    def __init__(self, base, ours, theirs, resolutions):
        self.base, self.ours, self.theirs = base, ours, theirs
        self.resolutions = resolutions
        self.conflicts_seen: list[Conflict] = []

    def run(self) -> Snapshot:
        tables = self._merge_tables()
        return Snapshot(dialect=self.base.dialect, tables=tuple(tables))

    # ---------------------------------------------------------------- tables
    def _merge_tables(self) -> list:
        b = {t.id: t for t in self.base.tables}
        o = {t.id: t for t in self.ours.tables}
        t_ = {t.id: t for t in self.theirs.tables}

        out = []
        for tid in _ordered_ids(b, o, t_):
            if tid in b:
                kept = self._three_way_object(
                    "table", tid, b[tid], o.get(tid), t_.get(tid),
                    path=b[tid].name)
                if kept is not None:
                    out.append(self._merge_table_contents(b[tid], o.get(tid),
                                                          t_.get(tid), kept))
            else:
                out.append(o[tid] if tid in o else t_[tid])

        self._check_name_collisions("table", b, o, t_, scope="")
        return out

    def _merge_table_contents(self, base_t, our_t, their_t, merged_t):
        """Children of a table that survived. A side that dropped the table
        contributes nothing -- its absence was already resolved above."""
        path = merged_t.name
        return replace(
            merged_t,
            columns=tuple(self._merge_children("column", base_t, our_t, their_t,
                                               "columns", path)),
            indexes=tuple(self._merge_children("index", base_t, our_t, their_t,
                                               "indexes", path)),
            constraints=tuple(self._merge_children("constraint", base_t, our_t,
                                                   their_t, "constraints", path)),
        )

    def _merge_children(self, kind, base_t, our_t, their_t, attr, table_path):
        b = {x.id: x for x in getattr(base_t, attr)}
        o = {x.id: x for x in getattr(our_t, attr)} if our_t else dict(b)
        t_ = {x.id: x for x in getattr(their_t, attr)} if their_t else dict(b)

        out = []
        for oid in _ordered_ids(b, o, t_):
            if oid in b:
                kept = self._three_way_object(
                    kind, oid, b[oid], o.get(oid), t_.get(oid),
                    path=f"{table_path}.{b[oid].name}")
                if kept is not None:
                    out.append(kept)
            else:
                out.append(o[oid] if oid in o else t_[oid])

        self._check_name_collisions(kind, b, o, t_, scope=table_path)
        return out

    # ------------------------------------------------- the core three-way rule
    def _three_way_object(self, kind, oid, base_o, our_o, their_o, *, path):
        """Returns the merged object, or None if it should be deleted.

        Category 1 lives here: deletion is only clean when the other side did not
        also modify the object. Both sides deleting is convergent, not a conflict
        (M-45/M-46) -- two engineers who agree are not in conflict.
        """
        if our_o is None and their_o is None:
            return None
        for side, gone, other in ((Side.OURS, our_o, their_o),
                                  (Side.THEIRS, their_o, our_o)):
            if gone is None:
                if other == base_o:
                    return None                      # clean delete (M-47)
                c = self._conflict(Conflict(
                    ConflictCategory.DELETE_MODIFY, kind, oid, path,
                    message=(f"{kind} {path!r} was deleted on "
                             f"{side.value} and modified on "
                             f"{'theirs' if side is Side.OURS else 'ours'}"),
                    base=base_o,
                    ours=None if side is Side.OURS else other,
                    theirs=None if side is Side.THEIRS else other))
                return self._apply_delete_modify(c, base_o, other, side)

        return self._merge_attributes(kind, oid, base_o, our_o, their_o, path)

    def _merge_attributes(self, kind, oid, base_o, our_o, their_o, path):
        """Category 3 and category 4, which are the same walk with two outcomes.

        Per attribute: if only one side moved, take that move. If both moved to the
        same value, that is convergence, not conflict (M-25/M-26/M-27). If both moved
        to different values, conflict -- carrying all three values, because a
        resolution UI cannot render a three-way choice without them (M-24).
        """
        changes = {}
        for attr in MERGED_ATTRS[kind]:
            bv = getattr(base_o, attr)
            ov = getattr(our_o, attr)
            tv = getattr(their_o, attr)

            if ov == tv:
                chosen = ov                       # includes "neither side moved"
            elif ov == bv:
                chosen = tv
            elif tv == bv:
                chosen = ov
            else:
                c = self._conflict(Conflict(
                    ConflictCategory.ATTRIBUTE, kind, oid, path,
                    message=(f"{kind} {path!r}: {attr} was changed on both sides "
                             f"({_show(ov)} vs {_show(tv)})"),
                    attribute=attr, base=bv, ours=ov, theirs=tv))
                chosen = self._apply_attribute(c, bv, ov, tv)
            if chosen != bv:
                changes[attr] = chosen
        return replace(base_o, **changes) if changes else base_o

    # ------------------------------------------------------- category 2
    def _check_name_collisions(self, kind, b, o, t_, scope):
        """Objects *added independently on both sides* under the same name.

        Distinct from a category-5 duplicate name: here both sides created something
        new, which is a genuine authoring conflict the user must settle. A rename
        colliding with an addition is not settleable pairwise and is caught by
        validation instead (M-80).
        """
        ours_new = {x.name: x for oid, x in o.items() if oid not in b}
        theirs_new = {x.name: x for oid, x in t_.items() if oid not in b}
        for name in sorted(set(ours_new) & set(theirs_new)):
            a, c = ours_new[name], theirs_new[name]
            path = f"{scope}.{name}" if scope else name
            identical = _same_shape(kind, a, c)
            note = (" -- the definitions are identical, but they are separate "
                    "identities: renaming one later would resurface as a phantom "
                    "drop and add" if identical else "")
            # The conflict is about a PAIR, so its key must not depend on which
            # branch was called "ours": the lower id anchors it, and the payload
            # keeps the sides straight. Without this, merging A into B and B into A
            # produce different resolution keys for the same conflict (M-91).
            anchor, other = sorted((a.id, c.id))
            self._conflict(Conflict(
                ConflictCategory.NAME_COLLISION, kind, anchor, path,
                message=(f"both sides added a {kind} named {name!r}"
                         f"{' in ' + scope if scope else ''}{note}"),
                ours=a, theirs=c, other_id=other))

    # ------------------------------------------------------- resolution plumbing
    def _conflict(self, c: Conflict) -> Conflict:
        self.conflicts_seen.append(c)
        return c

    def _apply_attribute(self, c, bv, ov, tv):
        r = self.resolutions.get(c.key)
        if r is None:
            return bv                              # unresolved; result is discarded
        if r.has_value:
            return r.value
        return ov if r.side is Side.OURS else tv

    def _apply_delete_modify(self, c, base_o, other, deleting_side):
        r = self.resolutions.get(c.key)
        if r is None:
            return base_o                          # unresolved; result is discarded
        if r.has_value:
            return r.value
        keep_deletion = (r.side is deleting_side)
        return None if keep_deletion else other


def _ordered_ids(*maps) -> list[str]:
    """Union of ids, first-seen order. Deterministic output matters: two merges of
    the same inputs must produce byte-identical snapshots (M-90)."""
    seen, out = set(), []
    for m in maps:
        for k in m:
            if k not in seen:
                seen.add(k)
                out.append(k)
    return out


def _same_shape(kind, a, b) -> bool:
    return all(getattr(a, attr) == getattr(b, attr) for attr in MERGED_ATTRS[kind])


def _show(v) -> str:
    return "NULL" if v is None else str(v)


# ====================================================================== category 5
def _validate(snap: Snapshot) -> list[Violation]:
    """Global invariants on the merged result.

    Everything here is invisible to a pairwise merge by construction: each check
    describes a relationship *between* changes that were individually fine.
    """
    out: list[Violation] = []
    out += _duplicate_names(snap)
    out += _dangling(snap)
    out += _nullable_pk(snap)
    out += _fk_targets_unique(snap)
    return out


def _duplicate_names(snap) -> list[Violation]:
    out = []
    out += _dupes([(t.name, t.id) for t in snap.tables],
                  "duplicate_table_name", "two tables named {name!r}")
    for t in snap.tables:
        out += _dupes([(c.name, c.id) for c in t.columns], "duplicate_column_name",
                      f"table {t.name!r} has two columns named {{name!r}}")
        out += _dupes([(i.name, i.id) for i in t.indexes], "duplicate_index_name",
                      f"table {t.name!r} has two indexes named {{name!r}}")
        out += _dupes([(c.name, c.id) for c in t.constraints],
                      "duplicate_constraint_name",
                      f"table {t.name!r} has two constraints named {{name!r}}")
    return out


def _dupes(pairs, invariant, template) -> list[Violation]:
    by_name: dict[str, list[str]] = {}
    for name, oid in pairs:
        by_name.setdefault(name, []).append(oid)
    return [Violation(invariant, template.format(name=name), tuple(ids))
            for name, ids in sorted(by_name.items()) if len(ids) > 1]


def _dangling(snap) -> list[Violation]:
    """Reuses the snapshot's own reference check, which is only meaningful because
    references are ids: a stale name and a never-existed name are the same string."""
    return [Violation("dangling_reference", msg) for msg in snap.dangling_references()]


def _nullable_pk(snap) -> list[Violation]:
    out = []
    for t in snap.tables:
        for c in t.constraints:
            if c.kind is not ConstraintKind.PRIMARY_KEY:
                continue
            for cid in c.column_ids:
                col = t.column_by_id(cid)
                if col is not None and col.nullable:
                    out.append(Violation(
                        "nullable_primary_key",
                        f"column {t.name}.{col.name} is nullable but is part of "
                        f"primary key {c.name!r}",
                        (col.id, c.id)))
    return out


def _fk_targets_unique(snap) -> list[Violation]:
    """A foreign key must point at columns the target guarantees are unique.

    Databases enforce this at DDL time, so skipping it would mean emitting a
    migration that fails halfway -- the exact failure mode this validation pass
    exists to prevent.
    """
    out = []
    for t in snap.tables:
        for c in t.constraints:
            if c.kind is not ConstraintKind.FOREIGN_KEY or not c.ref_table_id:
                continue
            target = snap.table_by_id(c.ref_table_id)
            if target is None:
                continue                          # already reported as dangling
            wanted = set(c.ref_column_ids)
            if not wanted:
                continue
            unique_sets = [set(k.column_ids) for k in target.constraints
                           if k.kind in (ConstraintKind.PRIMARY_KEY,
                                         ConstraintKind.UNIQUE)]
            unique_sets += [{ic.column_id for ic in i.columns}
                            for i in target.indexes if i.unique]
            if wanted not in unique_sets:
                names = ", ".join(target.column_by_id(x).name
                                  for x in c.ref_column_ids
                                  if target.column_by_id(x))
                out.append(Violation(
                    "fk_target_not_unique",
                    f"foreign key {t.name}.{c.name} references "
                    f"{target.name}({names}), which has no unique constraint",
                    (c.id, target.id)))
    return out


# ================================================================ branch-level merge
@dataclass(frozen=True)
class BranchMerge:
    status: MergeStatus
    #: The commit written, if any. None for UP_TO_DATE, CONFLICTED and INVALID.
    commit: Any = None
    #: The underlying snapshot merge. None when the DAG settled it (UP_TO_DATE / FF).
    result: MergeResult | None = None
    base_commit: str | None = None

    @property
    def conflicts(self) -> tuple:
        return self.result.conflicts if self.result else ()

    @property
    def violations(self) -> tuple:
        return self.result.violations if self.result else ()

    @property
    def token(self) -> str | None:
        return self.result.token if self.result else None


def merge_branches(repo, *, ours: str, theirs: str, resolutions=None,
                   token=None, message: str | None = None) -> BranchMerge:
    """Merge branch `theirs` into branch `ours`.

    The three cheap cases are recognized from the DAG *before* any snapshot work.
    That is not just an optimization: computing conflicts for a fast-forward would
    invite the user to resolve differences that only exist because the wrong pair of
    snapshots was compared.
    """
    from .merge_base import is_ancestor, merge_base

    ours_head = repo.head(ours).id
    theirs_head = repo.head(theirs).id

    if ours_head == theirs_head or is_ancestor(repo, theirs_head, ours_head):
        return BranchMerge(MergeStatus.UP_TO_DATE)          # M-100, M-101

    if is_ancestor(repo, ours_head, theirs_head):           # M-102
        repo.set_head(ours, theirs_head, expected=ours_head)
        return BranchMerge(MergeStatus.FAST_FORWARD, commit=repo.head(ours))

    base_id = merge_base(repo, ours_head, theirs_head)      # the LCA (D13)
    result = merge(repo.snapshot_at(base_id), repo.snapshot_at(ours_head),
                   repo.snapshot_at(theirs_head),
                   resolutions=resolutions, token=token)

    if not result.is_clean:
        # Deliberately no partial write and no persisted mid-merge state (D12):
        # a failed merge leaves the branch exactly where it was.
        return BranchMerge(result.status, result=result, base_commit=base_id)

    commit = repo.merge_commit(
        ours, ours=ours_head, theirs=theirs_head, snapshot=result.merged,
        message=message or f"merge {theirs} into {ours}")
    return BranchMerge(MergeStatus.MERGED, commit=commit, result=result,
                       base_commit=base_id)
