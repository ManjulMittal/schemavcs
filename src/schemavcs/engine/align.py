"""Identity alignment: adopting one snapshot's identities into another.

The diff engine is identity-based, which means it can only compare snapshots that share
a lineage -- two commits descended from a common ancestor. Two *independently* parsed
snapshots share no UUIDs at all, so diffing them directly reports every object as
dropped-and-re-added, which is useless.

That is not a diff-engine bug; it is a missing step. Comparing independent snapshots is
a separate operation with a separate risk profile:

  * alignment by NAME is deterministic. If a table or column has the same name in both,
    it is the same object. No judgement required.
  * alignment across a RENAME is a guess, because the name is exactly the evidence that
    disappeared. That is rename inference (D22), it is heuristic, and it must never
    happen without human confirmation -- so it lives in the import path, not here.

This module does only the deterministic half. Keeping the guess out of it is what lets
`diff` stay honest: the engine never invents a rename (F-27).

Because references are ids (D30), adopting an identity is not a one-line swap: every
index and constraint pointing at the OLD id has to be rewritten to the adopted one, or
alignment would leave the snapshot full of dangling references. That remapping is the
bulk of this module.
"""
from __future__ import annotations

from dataclasses import replace


def align_identity(reference, incoming):
    """Return `incoming` with identities adopted from `reference` wherever names match.

    Used when a user re-imports DDL: the previous snapshot is the reference, so
    unchanged objects keep their identity and only genuine changes surface.
    """
    ref_tables = {t.name: t for t in reference.tables}

    # Pass 1: adopt table and column identities, recording every id we rewrote so
    # references can be repointed. Doing this in one pass would leave indexes and
    # constraints pointing at ids that no longer exist on their own table.
    remap: dict[str, str] = {}
    tables = []
    for t in incoming.tables:
        ref = ref_tables.get(t.name)
        if ref is None:
            tables.append(t)
            continue
        remap[t.id] = ref.id
        columns = []
        ref_cols = {c.name: c for c in ref.columns}
        for c in t.columns:
            match = ref_cols.get(c.name)
            if match is None:
                columns.append(c)
                continue
            remap[c.id] = match.id
            columns.append(replace(c, id=match.id))
        tables.append(replace(
            t, id=ref.id, columns=tuple(columns),
            constraints=_adopt_names(ref.constraints, t.constraints),
            indexes=_adopt_names(ref.indexes, t.indexes),
        ))

    # Pass 2: repoint every reference through the remap.
    return replace(incoming, tables=tuple(_remap_refs(t, remap) for t in tables))


def _adopt_names(reference_objs, incoming_objs) -> tuple:
    """Adopt ids for same-named indexes/constraints. They are not referenced by
    anything else, so no remapping is needed for them."""
    by_name = {o.name: o for o in reference_objs}
    return tuple(
        replace(o, id=by_name[o.name].id) if o.name in by_name else o
        for o in incoming_objs
    )


def _remap_refs(table, remap: dict[str, str]):
    def m(i):
        return remap.get(i, i)

    indexes = tuple(
        replace(idx, columns=tuple(replace(ic, column_id=m(ic.column_id))
                                   for ic in idx.columns))
        for idx in table.indexes
    )
    constraints = tuple(
        replace(c,
                column_ids=tuple(m(x) for x in c.column_ids),
                ref_table_id=m(c.ref_table_id) if c.ref_table_id else None,
                ref_column_ids=tuple(m(x) for x in c.ref_column_ids))
        for c in table.constraints
    )
    return replace(table, indexes=indexes, constraints=constraints)
