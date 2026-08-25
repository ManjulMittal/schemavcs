"""Identity alignment -- the deterministic half of re-import.

Alignment by name requires no judgement. Alignment across a rename is a guess, because
the name is exactly the evidence that disappeared; that is rename inference (D22, I-*)
and it stays out of this module on purpose. These tests pin that boundary: align_identity
must NEVER infer a rename, because the diff engine trusts identity absolutely.
"""
from schemavcs.dialects import parse_ddl
from schemavcs.engine import ChangeKind, align_identity, diff
from schemavcs.model import schema


def test_matching_names_adopt_the_reference_identity():
    a = schema().table("t").col("x", "int").build()
    b = schema().table("t").col("x", "int").build()
    assert b.table("t").id != a.table("t").id

    aligned = align_identity(a, b)
    assert aligned.table("t").id == a.table("t").id
    assert aligned.col("t.x").id == a.col("t.x").id
    assert diff(a, aligned).is_empty


def test_a_renamed_column_is_NOT_inferred_as_a_rename():
    """The boundary. A guess here would let the diff engine silently claim a rename
    it has no evidence for, and the emitter would then generate ALTER ... RENAME
    instead of a drop plus an add."""
    a = schema().table("t").col("x", "int").build()
    b = schema().table("t").col("y", "int").build()

    d = diff(a, align_identity(a, b))
    assert set(c.kind for c in d.changes) == {ChangeKind.DROP_COLUMN, ChangeKind.ADD_COLUMN}
    assert ChangeKind.RENAME_COLUMN not in [c.kind for c in d.changes]


def test_new_objects_keep_their_fresh_identity():
    a = schema().table("t").col("x", "int").build()
    b = schema().table("t").col("x", "int").col("z", "int").table("other").col("q", "int").build()

    aligned = align_identity(a, b)
    assert aligned.col("t.x").id == a.col("t.x").id
    assert aligned.col("t.z").id == b.col("t.z").id, "genuinely new column keeps its id"
    d = diff(a, aligned)
    assert {c.kind for c in d.changes} == {ChangeKind.ADD_COLUMN, ChangeKind.CREATE_TABLE}


def test_alignment_covers_indexes_and_constraints_not_just_columns():
    a = parse_ddl("CREATE TABLE t (a int, CONSTRAINT uq UNIQUE (a));"
                  "CREATE INDEX i ON t (a)", dialect="postgres")
    b = parse_ddl("CREATE TABLE t (a int, CONSTRAINT uq UNIQUE (a));"
                  "CREATE INDEX i ON t (a)", dialect="postgres")
    aligned = align_identity(a, b)
    assert aligned.index("t.i").id == a.index("t.i").id
    assert aligned.constraint("t.uq").id == a.constraint("t.uq").id
    assert diff(a, aligned).is_empty


def test_alignment_is_idempotent():
    a = parse_ddl("CREATE TABLE t (a int, b varchar(3))", dialect="postgres")
    b = parse_ddl("CREATE TABLE t (b varchar(3), a int)", dialect="postgres")
    once = align_identity(a, b)
    assert align_identity(a, once) == once
    assert diff(a, once).is_empty


def test_alignment_does_not_mutate_either_input():
    a = schema().table("t").col("x", "int").build()
    b = schema().table("t").col("x", "int").build()
    ha, hb = a.content_hash(), b.content_hash()
    b_id = b.col("t.x").id
    align_identity(a, b)
    assert (a.content_hash(), b.content_hash()) == (ha, hb)
    assert b.col("t.x").id == b_id


def test_dropped_table_in_incoming_still_diffs_as_a_drop():
    a = schema().table("keep").col("x", "int").table("gone").col("y", "int").build()
    b = schema().table("keep").col("x", "int").build()
    d = diff(a, align_identity(a, b))
    assert [c.kind for c in d.changes] == [ChangeKind.DROP_TABLE]
    assert d.changes[0].name == "gone"
