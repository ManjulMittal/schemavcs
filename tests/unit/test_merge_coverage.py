"""Guard: every mergeable attribute is actually merged.

`MERGED_ATTRS` is a hand-written list of the attribute slots merge reconciles. If
someone adds a field to `Column` -- a collation, a comment, a generated expression --
and forgets this list, merge silently ignores it: the field never conflicts, and
whichever side happens to lose simply has its value discarded. That is the worst
possible failure mode, because the merge reports CLEAN.

This test makes the omission impossible to commit. It is deliberately noisy: adding a
field is meant to force a decision about how it merges, not to fail quietly.
"""
import dataclasses

import pytest

from schemavcs.engine.merge import MERGED_ATTRS
from schemavcs.model.objects import Column, Constraint, Index, Table

#: Fields intentionally NOT merged as attributes, with the reason.
EXEMPT = {
    "table": {
        "id": "identity, never merged -- it is the merge key",
        "columns": "merged as children, recursively",
        "constraints": "merged as children, recursively",
        "indexes": "merged as children, recursively",
    },
    "column": {"id": "identity, never merged -- it is the merge key"},
    "index": {"id": "identity, never merged -- it is the merge key"},
    "constraint": {"id": "identity, never merged -- it is the merge key"},
}

DATACLASSES = {"table": Table, "column": Column, "index": Index,
               "constraint": Constraint}


@pytest.mark.parametrize("kind", sorted(DATACLASSES))
def test_every_field_is_either_merged_or_explicitly_exempt(kind):
    fields = {f.name for f in dataclasses.fields(DATACLASSES[kind])}
    accounted = set(MERGED_ATTRS[kind]) | set(EXEMPT[kind])

    unaccounted = fields - accounted
    assert not unaccounted, (
        f"{DATACLASSES[kind].__name__} has field(s) {sorted(unaccounted)} that merge "
        f"neither reconciles nor exempts. Add them to MERGED_ATTRS so they can "
        f"conflict, or to EXEMPT with a reason -- silently ignoring a field means a "
        f"merge can discard it and still report CLEAN.")


@pytest.mark.parametrize("kind", sorted(DATACLASSES))
def test_merged_attrs_names_only_real_fields(kind):
    """The mirror: a typo in MERGED_ATTRS would make `getattr` raise at merge time,
    but only on the code path that touches that object kind."""
    fields = {f.name for f in dataclasses.fields(DATACLASSES[kind])}
    for attr in MERGED_ATTRS[kind]:
        assert attr in fields, f"MERGED_ATTRS[{kind!r}] names unknown field {attr!r}"
