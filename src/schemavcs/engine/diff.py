"""Identity-based semantic diff.

Objects are matched by UUID, never by name (D1). That single choice is what makes a
rename representable: `same id, new name` is a one-line rule, where a name-keyed differ
can only ever report a data-destroying drop-plus-add.

The engine never *infers* identity. If two objects have different ids they are different
objects, full stop -- even if a human would obviously call it a rename (F-27). Guessing
belongs to the import path, and only with human confirmation (D22).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class UnalignedSnapshotsError(Exception):
    """Two snapshots share object names but no object identities.

    Diff is identity-based (D1), so this input produces a technically-correct but
    useless answer: every object reported as dropped and re-added. It almost always
    means two independently parsed snapshots were compared without aligning them
    first (D29). Failing loudly beats returning a diff that looks plausible and says
    nothing true.
    """

    def __init__(self, shared_names: set[str]):
        self.shared_names = shared_names
        sample = ", ".join(sorted(shared_names)[:5])
        super().__init__(
            f"snapshots share {len(shared_names)} table name(s) ({sample}) but no object "
            "identities, so every object would be reported as dropped and re-added. "
            "If these were parsed independently, align them first: "
            "diff(base, align_identity(base, incoming))."
        )


class ChangeKind(str, Enum):
    CREATE_TABLE = "create_table"
    DROP_TABLE = "drop_table"
    RENAME_TABLE = "rename_table"

    ADD_COLUMN = "add_column"
    DROP_COLUMN = "drop_column"
    RENAME_COLUMN = "rename_column"
    ALTER_COLUMN = "alter_column"

    CREATE_INDEX = "create_index"
    DROP_INDEX = "drop_index"
    RENAME_INDEX = "rename_index"
    ALTER_INDEX = "alter_index"

    ADD_CONSTRAINT = "add_constraint"
    DROP_CONSTRAINT = "drop_constraint"
    ALTER_CONSTRAINT = "alter_constraint"


#: Sort weight, so a diff reads in a stable, sensible order regardless of dict
#: iteration. Not execution order -- the emitter owns that (D17).
_ORDER = {k: i for i, k in enumerate([
    ChangeKind.DROP_CONSTRAINT, ChangeKind.DROP_INDEX, ChangeKind.DROP_COLUMN,
    ChangeKind.DROP_TABLE, ChangeKind.CREATE_TABLE, ChangeKind.RENAME_TABLE,
    ChangeKind.ADD_COLUMN, ChangeKind.RENAME_COLUMN, ChangeKind.ALTER_COLUMN,
    ChangeKind.RENAME_INDEX, ChangeKind.ALTER_INDEX, ChangeKind.CREATE_INDEX,
    ChangeKind.ALTER_CONSTRAINT, ChangeKind.ADD_CONSTRAINT,
])}


@dataclass(frozen=True)
class AttributeDelta:
    attribute: str
    before: Any
    after: Any


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    object_id: str
    table: str                          # table name in the TARGET (post-rename)
    name: str | None = None             # object name in the target
    before_name: str | None = None      # object name in the base, if it changed
    deltas: tuple[AttributeDelta, ...] = ()
    before: Any = None
    after: Any = None

    @property
    def is_rename(self) -> bool:
        return any(d.attribute == "name" for d in self.deltas)

    def summary(self) -> str:
        if self.deltas:
            attrs = ",".join(d.attribute for d in self.deltas)
            return f"{self.kind.value}{{{attrs}}}"
        return self.kind.value


@dataclass(frozen=True)
class Diff:
    changes: tuple[Change, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def of_kind(self, *kinds: ChangeKind) -> tuple[Change, ...]:
        return tuple(c for c in self.changes if c.kind in kinds)

    def __len__(self) -> int:
        return len(self.changes)


# --------------------------------------------------------------------- diff
def diff(base, target) -> Diff:
    """Semantic difference between two snapshots, keyed on object identity.

    Raises UnalignedSnapshotsError if the two snapshots have clearly never shared a
    lineage -- see D29.
    """
    _require_shared_lineage(base, target)
    changes: list[Change] = []

    base_tables = {t.id: t for t in base.tables}
    target_tables = {t.id: t for t in target.tables}

    for tid, t in base_tables.items():
        if tid not in target_tables:
            changes.append(Change(ChangeKind.DROP_TABLE, tid, t.name, name=t.name,
                                  before=t))

    for tid, t in target_tables.items():
        if tid not in base_tables:
            changes.append(Change(ChangeKind.CREATE_TABLE, tid, t.name, name=t.name,
                                  after=t))
            continue
        changes.extend(_diff_table(base_tables[tid], t))

    changes.sort(key=lambda c: (_ORDER[c.kind], c.table, c.name or ""))
    return Diff(tuple(changes))


def _diff_table(before, after) -> list[Change]:
    out: list[Change] = []
    if before.name != after.name:
        out.append(Change(ChangeKind.RENAME_TABLE, after.id, after.name,
                          name=after.name, before_name=before.name,
                          deltas=(AttributeDelta("name", before.name, after.name),)))

    out.extend(_diff_columns(before, after))
    out.extend(_diff_indexes(before, after))
    out.extend(_diff_constraints(before, after))
    return out


def _diff_columns(before, after) -> list[Change]:
    out: list[Change] = []
    b = {c.id: c for c in before.columns}
    a = {c.id: c for c in after.columns}

    for cid, col in b.items():
        if cid not in a:
            out.append(Change(ChangeKind.DROP_COLUMN, cid, after.name, name=col.name,
                              before=col))
    for cid, col in a.items():
        if cid not in b:
            out.append(Change(ChangeKind.ADD_COLUMN, cid, after.name, name=col.name,
                              after=col))
            continue
        old = b[cid]
        deltas = _column_deltas(old, col)
        if not deltas:
            continue
        # A rename is still a rename even when other attributes moved with it: the
        # kind reflects identity-preserving renaming, the deltas carry everything
        # that actually changed (F-23).
        kind = (ChangeKind.RENAME_COLUMN
                if any(d.attribute == "name" for d in deltas)
                else ChangeKind.ALTER_COLUMN)
        out.append(Change(kind, cid, after.name, name=col.name,
                          before_name=old.name if old.name != col.name else None,
                          deltas=deltas, before=old, after=col))
    return out


def _column_deltas(old, new) -> tuple[AttributeDelta, ...]:
    out = []
    for attr in ("name", "type", "nullable", "default", "autoincrement"):
        ov, nv = getattr(old, attr), getattr(new, attr)
        if ov != nv:
            out.append(AttributeDelta(attr, ov, nv))
    return tuple(out)


def _diff_indexes(before, after) -> list[Change]:
    out: list[Change] = []
    b = {i.id: i for i in before.indexes}
    a = {i.id: i for i in after.indexes}

    for iid, idx in b.items():
        if iid not in a:
            out.append(Change(ChangeKind.DROP_INDEX, iid, after.name, name=idx.name,
                              before=idx))
    for iid, idx in a.items():
        if iid not in b:
            out.append(Change(ChangeKind.CREATE_INDEX, iid, after.name, name=idx.name,
                              after=idx))
            continue
        old = b[iid]
        deltas = []
        if old.name != idx.name:
            deltas.append(AttributeDelta("name", old.name, idx.name))
        # Ordered column lists are compared as ATOMIC values (D4): one delta for the
        # whole list, never element-wise, so a merge can never silently interleave
        # two engineers' orderings into a third nobody designed.
        if old.columns != idx.columns:
            deltas.append(AttributeDelta("columns", old.columns, idx.columns))
        if old.unique != idx.unique:
            deltas.append(AttributeDelta("unique", old.unique, idx.unique))
        if old.where != idx.where:
            deltas.append(AttributeDelta("where", old.where, idx.where))
        if not deltas:
            continue
        kind = (ChangeKind.RENAME_INDEX
                if any(d.attribute == "name" for d in deltas)
                else ChangeKind.ALTER_INDEX)
        out.append(Change(kind, iid, after.name, name=idx.name,
                          before_name=old.name if old.name != idx.name else None,
                          deltas=tuple(deltas), before=old, after=idx))
    return out


def _diff_constraints(before, after) -> list[Change]:
    out: list[Change] = []
    b = {c.id: c for c in before.constraints}
    a = {c.id: c for c in after.constraints}

    for cid, con in b.items():
        if cid not in a:
            out.append(Change(ChangeKind.DROP_CONSTRAINT, cid, after.name,
                              name=con.name, before=con))
    for cid, con in a.items():
        if cid not in b:
            out.append(Change(ChangeKind.ADD_CONSTRAINT, cid, after.name,
                              name=con.name, after=con))
            continue
        old = b[cid]
        deltas = []
        # Reference fields are ids (D30). Comparing ids is correct *within* a
        # lineage, which is the only place diff is valid at all (D29).
        for attr in ("name", "kind", "column_ids", "ref_table_id", "ref_column_ids",
                     "on_delete", "on_update", "expression"):
            ov, nv = getattr(old, attr), getattr(con, attr)
            if ov != nv:
                deltas.append(AttributeDelta(attr, ov, nv))
        if deltas:
            out.append(Change(ChangeKind.ALTER_CONSTRAINT, cid, after.name,
                              name=con.name, deltas=tuple(deltas),
                              before=old, after=con))
    return out


def _require_shared_lineage(base, target) -> None:
    """Guard against diffing two snapshots that never shared an ancestor (D29).

    The discriminator is name overlap WITHOUT id overlap. A rename preserves ids, so
    there is no legitimate way for two related snapshots to share a table name and no
    identity. Deliberately silent when either side is empty -- diffing genesis against
    a first commit is normal and shares nothing.
    """
    if not base.tables or not target.tables:
        return
    if {t.id for t in base.tables} & {t.id for t in target.tables}:
        return
    shared_names = {t.name for t in base.tables} & {t.name for t in target.tables}
    if shared_names:
        raise UnalignedSnapshotsError(shared_names)
