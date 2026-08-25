"""N-* : canonical model & identity. See docs/test-plan.md section 1.

The two subtle cases are N-05 vs N-06: column declaration order within a table is
NOT semantically modeled (D27), but index column order IS (D4). Getting these
backwards produces either spurious diffs or silently broken indexes.
"""
import pytest

from schemavcs.model import ColumnType, Index, Snapshot, Table, schema


# ------------------------------------------------------------------ identity
def test_n01_new_objects_get_unique_ids():
    s = (schema()
         .table("users").col("id", "bigint", pk=True).col("email", "varchar(255)")
         .build())
    ids = [s.col("users.id").id, s.col("users.email").id, s.table("users").id]
    assert all(i for i in ids), "every object must carry an id"
    assert len(set(ids)) == len(ids), "ids must be unique within a snapshot"


def test_n02_rename_preserves_id():
    """D1: a rename is `same id, new name`. This is the whole product thesis."""
    s = schema().table("users").col("email", "varchar(255)").build()
    before = s.col("users.email").id

    s2 = s.evolve().rename_col("users.email", "email_address").build()

    assert s2.col("users.email_address").id == before
    assert s2.col("users.email_address").name == "email_address"


def test_n03_drop_then_readd_same_name_is_not_a_rename():
    s = schema().table("users").col("email", "varchar(255)").build()
    before = s.col("users.email").id

    s2 = (s.evolve()
          .drop_col("users.email")
          .add_col("users", "email", "varchar(255)")
          .build())

    assert s2.col("users.email").id != before, "drop+re-add must mint a new identity"


def test_n02b_rename_chain_preserves_id_across_steps():
    """F-24 depends on this being free."""
    s = schema().table("t").col("a", "int").build()
    first = s.col("t.a").id
    s3 = (s.evolve().rename_col("t.a", "b").build()
           .evolve().rename_col("t.b", "c").build())
    assert s3.col("t.c").id == first


# ------------------------------------------------- structural equality (N-04..07)
def test_n04_table_declaration_order_is_ignored():
    a = schema().table("x").col("id", "int").table("y").col("id", "int").build()
    b = schema().table("y").col("id", "int").table("x").col("id", "int").build()
    assert a == b, "tables are a set, not a list"


def test_n05_column_declaration_order_is_ignored():
    """D27: modeling column position would make every mid-table insert a conflict."""
    a = schema().table("t").col("a", "int").col("b", "int").build()
    b = schema().table("t").col("b", "int").col("a", "int").build()
    assert a == b


def test_n06_index_column_order_is_significant():
    """D4: index column order determines whether the index is usable at all."""
    a = schema().table("t").col("a", "int").col("b", "int").index("i", ["a", "b"]).build()
    b = schema().table("t").col("a", "int").col("b", "int").index("i", ["b", "a"]).build()
    assert a != b, "[a,b] and [b,a] are different indexes"


def test_n07_fk_column_pairing_order_is_significant():
    def s(local, remote):
        return (schema()
                .table("parent").col("x", "int").col("y", "int")
                    .unique("uq_p", ["x", "y"])
                .table("child").col("a", "int").col("b", "int")
                    .fk("fk_c", local, "parent", remote)
                .build())
    assert s(["a", "b"], ["x", "y"]) != s(["a", "b"], ["y", "x"])


def test_equality_ignores_identity():
    """Two independently built identical schemas are equal despite different UUIDs.

    R-02 (introspect -> emit -> apply -> introspect) depends on this: a live engine
    cannot return our UUIDs, so structural equality is what round-tripping asserts.
    """
    a = schema().table("t").col("a", "int").build()
    b = schema().table("t").col("a", "int").build()
    assert a.col("t.a").id != b.col("t.a").id
    assert a == b


# ---------------------------------------------------------------- hashing (N-08/09)
def test_n08_content_hash_stable_across_serialization():
    s = (schema()
         .table("users").col("id", "bigint", pk=True)
                        .col("email", "varchar(255)", nullable=False)
                        .index("i", ["email"], unique=True)
         .build())
    from schemavcs.model import Snapshot
    assert Snapshot.from_dict(s.to_dict()).content_hash() == s.content_hash()


def test_n08b_content_hash_ignores_declaration_order():
    a = schema().table("t").col("a", "int").col("b", "int").build()
    b = schema().table("t").col("b", "int").col("a", "int").build()
    assert a.content_hash() == b.content_hash()


@pytest.mark.parametrize("mutate", [
    lambda s: s.evolve().rename_col("t.a", "z").build(),
    lambda s: s.evolve().retype_col("t.a", "bigint").build(),
    lambda s: s.evolve().set_nullable("t.a", False).build(),
    lambda s: s.evolve().set_default("t.a", "0").build(),
    lambda s: s.evolve().add_col("t", "c", "int").build(),
    lambda s: s.evolve().drop_col("t.b").build(),
    lambda s: s.evolve().rename_table("t", "t2").build(),
], ids=["rename", "retype", "nullable", "default", "add_col", "drop_col", "rename_table"])
def test_n09_hash_changes_on_every_single_attribute_change(mutate):
    s = schema().table("t").col("a", "int").col("b", "int").build()
    assert mutate(s).content_hash() != s.content_hash()


def test_n10_snapshots_are_immutable():
    s = schema().table("t").col("a", "int").build()
    with pytest.raises((AttributeError, TypeError)):
        s.tables = ()
    with pytest.raises((AttributeError, TypeError)):
        s.col("t.a").name = "zzz"


def test_evolve_does_not_mutate_the_original():
    """Every M-* test builds two divergent branches from one base snapshot."""
    base = schema().table("t").col("a", "int").build()
    h = base.content_hash()
    base.evolve().rename_col("t.a", "b").build()
    assert base.content_hash() == h
    assert base.col("t.a").name == "a"


# ------------------------------------------------------------------ types
def test_type_equality_is_canonical_not_textual():
    assert ColumnType.parse("varchar(255)") == ColumnType.parse("varchar(255)")
    assert ColumnType.parse("varchar(255)") != ColumnType.parse("varchar(256)")
    assert ColumnType.parse("int") != ColumnType.parse("bigint")


# ------------------------------------- fingerprint totality (found by M91 on CI)
def test_equality_survives_a_snapshot_that_is_malformed():
    """`__eq__` and `__hash__` must not raise, even on an invalid schema.

    Fingerprints are sorted to make the comparison order-insensitive, and sorting the
    tuples directly compares them element-wise. Several fields are optional -- an index
    `where`, a column `default`, a constraint `on_delete` -- so two entries that tie on
    everything before one of those go on to compare `None` against a string, which is a
    TypeError rather than an answer.

    That is reachable: a merge may produce an invalid snapshot and report the violations
    rather than refuse (D11), so equality is asked about malformed schemas by design.
    Two indexes sharing a name is the smallest way to force the tie.
    """
    tie = Table(name="t", indexes=(
        Index(name="i", columns=(), unique=False, where=None),
        Index(name="i", columns=(), unique=False, where="x > 0"),
    ))
    a, b = Snapshot(tables=(tie,)), Snapshot(tables=(tie,))

    assert a == b                     # must answer, not raise
    assert hash(a) == hash(b)
    assert a.content_hash() == b.content_hash()


def test_a_none_valued_optional_does_not_collide_with_a_string():
    """The order-only fix must not make `None` and a string indistinguishable."""
    unnamed = Table(name="t", indexes=(Index(name="i", where=None),))
    named = Table(name="t", indexes=(Index(name="i", where="x > 0"),))

    assert Snapshot(tables=(unnamed,)) != Snapshot(tables=(named,))
