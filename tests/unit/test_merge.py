"""Three-way merge -- the five conflict categories (M-*).

Every category gets conflict cases *and* convergent counter-cases, written together
rather than in separate passes. The counter-cases are the ones that catch an
over-eager implementation: it is easy to write a merge that flags every overlap as a
conflict and passes all the conflict tests.

Read `M-01` first. It is the product thesis in one test: a rename on one branch and a
retype on the other, on the *same column*, merging cleanly -- which a name-keyed
differ cannot express at all.
"""
import pytest

from schemavcs.engine import (ConflictCategory, merge)
from schemavcs.model import schema


# ============================================================ category 4: auto-merge
# The money cases. Two engineers touched the same schema and neither has to care.

def test_M01_rename_on_one_side_retype_on_the_other_merges_clean(base_schema):
    """THE test. Same column, both sides changed it, clean merge.

    Attribute-level merge (D3) is what makes this work: `name` and `type` are
    independent slots, so there is nothing to conflict over. A name-keyed model sees
    a drop+add on one side and a retype on a column that no longer exists on the
    other, and has no honest answer.
    """
    ours = base_schema.evolve().rename_col("users.email", "contact").build()
    theirs = base_schema.evolve().retype_col("users.email", "text").build()

    r = merge(base_schema, ours, theirs)

    assert r.is_clean, r.conflicts
    col = r.merged.col("users.contact")
    assert col is not None, "the rename from ours must survive"
    assert str(col.type) == "text", "the retype from theirs must survive"


def test_M02_nullability_and_default_on_the_same_column(base_schema):
    ours = base_schema.evolve().set_nullable("users.email", True).build()
    theirs = base_schema.evolve().set_default("users.email", "''").build()

    r = merge(base_schema, ours, theirs)

    assert r.is_clean
    assert r.merged.col("users.email").nullable is True
    assert r.merged.col("users.email").default == "''"


def test_M03_two_new_columns_on_the_same_table(base_schema):
    ours = base_schema.evolve().add_col("users", "x", "int").build()
    theirs = base_schema.evolve().add_col("users", "y", "int").build()

    r = merge(base_schema, ours, theirs)

    assert r.is_clean
    assert {c.name for c in r.merged.table("users").columns} == {"id", "email", "x", "y"}


def test_M04_two_new_tables(base_schema):
    ours = base_schema.evolve().add_table("t1").build()
    theirs = base_schema.evolve().add_table("t2").build()

    r = merge(base_schema, ours, theirs)

    assert r.is_clean
    assert {t.name for t in r.merged.tables} == {"users", "t1", "t2"}


def test_M05_two_new_indexes_on_the_same_table(base_schema):
    ours = base_schema.evolve().add_index("users", "i1", ["email"]).build()
    theirs = base_schema.evolve().add_index("users", "i2", ["id"]).build()

    r = merge(base_schema, ours, theirs)

    assert r.is_clean
    assert {i.name for i in r.merged.table("users").indexes} == {"i1", "i2"}


def test_M06_table_rename_and_a_column_added_to_it(base_schema):
    ours = base_schema.evolve().rename_table("users", "accounts").build()
    theirs = base_schema.evolve().add_col("users", "age", "int").build()

    r = merge(base_schema, ours, theirs)

    assert r.is_clean
    assert r.merged.table("accounts") is not None
    assert r.merged.col("accounts.age") is not None, \
        "a column added under the old name belongs to the table, not to the name"


def test_M07_index_rename_and_column_list_change_are_different_attributes(base_schema):
    base = base_schema.evolve().add_index("users", "idx", ["email"]).build()
    ours = base.evolve().rename_index("users.idx", "idx_email").build()
    theirs = base.evolve().set_index_columns("users.idx", ["email", "id"]).build()

    r = merge(base, ours, theirs)

    assert r.is_clean
    idx = r.merged.table("users").index("idx_email")
    assert idx is not None
    assert r.merged.index_column_names("users.idx_email") == ["email", "id"]


def test_M08_index_added_on_a_column_the_other_side_renamed(base_schema):
    """The index follows the rename for free, because it stores the column id (D30)."""
    ours = base_schema.evolve().rename_col("users.email", "contact").build()
    theirs = base_schema.evolve().add_index("users", "idx", ["email"]).build()

    r = merge(base_schema, ours, theirs)

    assert r.is_clean
    assert r.merged.index_column_names("users.idx") == ["contact"]
    assert r.merged.dangling_references() == []


def test_M09_unrelated_drop_and_unrelated_rename(base_schema):
    base = base_schema.evolve().add_col("users", "scratch", "int").build()
    ours = base.evolve().drop_col("users.scratch").build()
    theirs = base.evolve().rename_col("users.email", "contact").build()

    r = merge(base, ours, theirs)

    assert r.is_clean
    assert r.merged.col("users.scratch") is None
    assert r.merged.col("users.contact") is not None


# ==================================================== category 3: attribute conflict

def test_M20_both_rename_the_same_column_differently(base_schema):
    ours = base_schema.evolve().rename_col("users.email", "contact").build()
    theirs = base_schema.evolve().rename_col("users.email", "mail").build()

    r = merge(base_schema, ours, theirs)

    assert not r.is_clean
    c = one(r.conflicts)
    assert c.category is ConflictCategory.ATTRIBUTE
    assert c.attribute == "name"


def test_M21_both_retype_the_same_column_differently(base_schema):
    ours = base_schema.evolve().retype_col("users.email", "text").build()
    theirs = base_schema.evolve().retype_col("users.email", "varchar(100)").build()

    r = merge(base_schema, ours, theirs)

    assert one(r.conflicts).attribute == "type"


def test_M22_both_change_the_same_default_differently(base_schema):
    ours = base_schema.evolve().set_default("users.email", "'a'").build()
    theirs = base_schema.evolve().set_default("users.email", "'b'").build()

    r = merge(base_schema, ours, theirs)

    assert one(r.conflicts).attribute == "default"


def test_M23_both_change_nullability_differently(base_schema):
    base = base_schema.evolve().add_col("users", "note", "text", nullable=True).build()
    ours = base.evolve().set_nullable("users.note", False).build()
    theirs = base.evolve().set_default("users.note", "''").set_nullable("users.note",
                                                                       True).build()
    # theirs leaves nullable at the base value, so this must stay CLEAN --
    # guarding the assertion below against a false positive.
    assert merge(base, ours, theirs).is_clean

    theirs2 = base.evolve().drop_col("users.note").build()
    assert not merge(base, ours, theirs2).is_clean


def test_M24_conflict_payload_carries_base_ours_and_theirs(base_schema):
    """The resolution UI cannot render a three-way choice without all three values."""
    ours = base_schema.evolve().retype_col("users.email", "text").build()
    theirs = base_schema.evolve().retype_col("users.email", "varchar(100)").build()

    c = one(merge(base_schema, ours, theirs).conflicts)

    assert str(c.base) == "varchar(255)"
    assert str(c.ours) == "text"
    assert str(c.theirs) == "varchar(100)"
    assert c.path == "users.email"
    assert c.object_kind == "column"


def test_M25_both_rename_to_the_SAME_name_is_convergent_not_a_conflict(base_schema):
    """Two engineers who agreed are not in conflict. The naive implementation --
    'both sides touched `name`, therefore conflict' -- fails exactly here."""
    ours = base_schema.evolve().rename_col("users.email", "contact").build()
    theirs = base_schema.evolve().rename_col("users.email", "contact").build()

    r = merge(base_schema, ours, theirs)

    assert r.is_clean, r.conflicts
    assert r.merged.col("users.contact") is not None


def test_M26_both_retype_to_the_same_type_is_convergent(base_schema):
    ours = base_schema.evolve().retype_col("users.email", "text").build()
    theirs = base_schema.evolve().retype_col("users.email", "text").build()

    assert merge(base_schema, ours, theirs).is_clean


def test_M27_same_default_spelled_differently_is_convergent():
    """The sleeper. A normalization bug that only produces a spurious *diff* is
    annoying; the same bug here produces a spurious *conflict*, which blocks work.

    `now()` and `CURRENT_TIMESTAMP` are the same default. Normalization happens at
    parse time (P-23), so by the time merge sees them they are identical strings --
    this test pins that the merge path actually benefits from it.
    """
    from schemavcs.dialects import parse_ddl
    from schemavcs.engine import align_identity

    base = parse_ddl("CREATE TABLE t (id int, at timestamp)", dialect="postgres")
    ours = align_identity(base, parse_ddl(
        "CREATE TABLE t (id int, at timestamp DEFAULT now())", dialect="postgres"))
    theirs = align_identity(base, parse_ddl(
        "CREATE TABLE t (id int, at timestamp DEFAULT CURRENT_TIMESTAMP)",
        dialect="postgres"))

    assert ours.col("t.at").default == theirs.col("t.at").default, \
        "precondition: normalization must have collapsed the two spellings"
    assert merge(base, ours, theirs).is_clean


# ======================================================= category 1: delete/modify

def test_M40_one_side_drops_a_column_the_other_renames_it(base_schema):
    ours = base_schema.evolve().drop_col("users.email").build()
    theirs = base_schema.evolve().rename_col("users.email", "contact").build()

    r = merge(base_schema, ours, theirs)

    c = one(r.conflicts)
    assert c.category is ConflictCategory.DELETE_MODIFY
    assert c.path == "users.email"


def test_M41_one_side_drops_a_column_the_other_retypes_it(base_schema):
    ours = base_schema.evolve().drop_col("users.email").build()
    theirs = base_schema.evolve().retype_col("users.email", "text").build()

    assert one(merge(base_schema, ours, theirs).conflicts).category \
        is ConflictCategory.DELETE_MODIFY


def test_M42_one_side_drops_a_table_the_other_adds_a_column_to_it(base_schema):
    base = base_schema.evolve().add_table("temp").build()
    ours = base.evolve().drop_table("temp").build()
    theirs = base.evolve().add_col("temp", "c", "int").build()

    c = one(merge(base, ours, theirs).conflicts)
    assert c.category is ConflictCategory.DELETE_MODIFY
    assert c.object_kind == "table"


def test_M43_one_side_drops_a_table_the_other_renames_it(base_schema):
    base = base_schema.evolve().add_table("temp").build()
    ours = base.evolve().drop_table("temp").build()
    theirs = base.evolve().rename_table("temp", "staging").build()

    assert one(merge(base, ours, theirs).conflicts).category \
        is ConflictCategory.DELETE_MODIFY


def test_M44_one_side_drops_an_index_the_other_changes_its_columns(base_schema):
    base = base_schema.evolve().add_index("users", "idx", ["email"]).build()
    ours = base.evolve().drop_index("users.idx").build()
    theirs = base.evolve().set_index_columns("users.idx", ["id"]).build()

    c = one(merge(base, ours, theirs).conflicts)
    assert c.category is ConflictCategory.DELETE_MODIFY
    assert c.object_kind == "index"


def test_M45_both_drop_the_same_column_is_convergent(base_schema):
    base = base_schema.evolve().add_col("users", "scratch", "int").build()
    ours = base.evolve().drop_col("users.scratch").build()
    theirs = base.evolve().drop_col("users.scratch").build()

    r = merge(base, ours, theirs)

    assert r.is_clean, r.conflicts
    assert r.merged.col("users.scratch") is None


def test_M46_both_drop_the_same_table_is_convergent(base_schema):
    base = base_schema.evolve().add_table("temp").build()
    ours = base.evolve().drop_table("temp").build()
    theirs = base.evolve().drop_table("temp").build()

    r = merge(base, ours, theirs)

    assert r.is_clean
    assert r.merged.table("temp") is None


def test_M47_one_side_drops_the_other_is_untouched(base_schema):
    base = base_schema.evolve().add_col("users", "scratch", "int").build()
    ours = base.evolve().drop_col("users.scratch").build()

    r = merge(base, ours, base)

    assert r.is_clean
    assert r.merged.col("users.scratch") is None


# ======================================================= category 2: name collision

def test_M60_both_add_a_column_with_the_same_name(base_schema):
    ours = base_schema.evolve().add_col("users", "foo", "int").build()
    theirs = base_schema.evolve().add_col("users", "foo", "text").build()

    c = one(merge(base_schema, ours, theirs).conflicts)

    assert c.category is ConflictCategory.NAME_COLLISION
    assert c.path == "users.foo"


def test_M61_both_add_a_table_with_the_same_name(base_schema):
    ours = base_schema.evolve().add_table("t").build()
    theirs = base_schema.evolve().add_table("t").build()

    c = one(merge(base_schema, ours, theirs).conflicts)

    assert c.category is ConflictCategory.NAME_COLLISION
    assert c.object_kind == "table"


def test_M62_identical_definitions_added_on_both_sides_still_conflict(base_schema):
    """A deliberate call, and the one most likely to be questioned.

    The definitions are identical, so taking either would produce the same DDL --
    tempting to auto-merge. But the two columns have *distinct identities*, and the
    moment anyone renames one the divergence resurfaces as a phantom drop+add. The
    conflict is cheap to resolve and the silent identity fork is not.
    """
    ours = base_schema.evolve().add_col("users", "foo", "int").build()
    theirs = base_schema.evolve().add_col("users", "foo", "int").build()

    c = one(merge(base_schema, ours, theirs).conflicts)

    assert c.category is ConflictCategory.NAME_COLLISION
    assert "identit" in c.message.lower(), \
        "the message must explain why identical definitions still conflict"


def test_M63_both_add_an_index_with_the_same_name(base_schema):
    ours = base_schema.evolve().add_index("users", "idx", ["email"]).build()
    theirs = base_schema.evolve().add_index("users", "idx", ["id"]).build()

    c = one(merge(base_schema, ours, theirs).conflicts)

    assert c.category is ConflictCategory.NAME_COLLISION
    assert c.object_kind == "index"


def test_M64_same_name_in_different_tables_is_not_a_collision(base_schema):
    base = base_schema.evolve().add_table("other").build()
    ours = base.evolve().add_col("users", "foo", "int").build()
    theirs = base.evolve().add_col("other", "foo", "int").build()

    r = merge(base, ours, theirs)

    assert r.is_clean, r.conflicts
    assert r.merged.col("users.foo") is not None
    assert r.merged.col("other.foo") is not None


# ================================================== category 5: integrity violation

def test_M80_rename_into_a_name_the_other_side_added(base_schema):
    """The test that proves category 5 must exist.

    Pairwise merge sees nothing wrong: two different UUIDs, disjoint attribute sets,
    zero overlap. Only validating the merged *result* catches the duplicate name.
    Without this pass the tool reports a clean merge and emits DDL the database
    rejects.
    """
    ours = base_schema.evolve().rename_col("users.email", "contact").build()
    theirs = base_schema.evolve().add_col("users", "contact", "text").build()

    r = merge(base_schema, ours, theirs)

    assert r.conflicts == (), "pairwise, there is genuinely no conflict here"
    assert not r.is_clean, "but the merged result is invalid"
    v = one(r.violations)
    assert v.invariant == "duplicate_column_name"
    assert "contact" in v.message


def test_M81_drop_a_table_the_other_side_adds_a_foreign_key_to():
    base = (schema()
            .table("users").col("id", "bigint", pk=True)
            .table("orders").col("id", "bigint", pk=True).col("user_id", "bigint")
            .build())
    ours = base.evolve().drop_table("users").build()
    theirs = base.evolve().add_fk("orders", "fk_u", ["user_id"], "users", ["id"]).build()

    r = merge(base, ours, theirs)

    assert r.conflicts == ()
    v = one(r.violations)
    assert v.invariant == "dangling_reference"


def test_M82_drop_a_column_the_other_side_indexes(base_schema):
    ours = base_schema.evolve().drop_col("users.email").build()
    theirs = base_schema.evolve().add_index("users", "idx", ["email"]).build()

    r = merge(base_schema, ours, theirs)

    assert r.conflicts == ()
    assert one(r.violations).invariant == "dangling_reference"


def test_M83_one_side_makes_a_column_nullable_the_other_adds_it_to_the_pk(base_schema):
    ours = base_schema.evolve().set_nullable("users.email", True).build()
    theirs = _extend_pk(base_schema, "users", ["id", "email"])

    r = merge(base_schema, ours, theirs)

    assert r.conflicts == ()
    v = one(r.violations)
    assert v.invariant == "nullable_primary_key"
    assert "email" in v.message


def test_M84_drop_a_unique_the_other_side_targets_with_a_foreign_key():
    base = (schema()
            .table("users").col("id", "bigint", pk=True).col("email", "varchar(255)")
              .unique("uq_email", ["email"])
            .table("orders").col("id", "bigint", pk=True).col("owner", "varchar(255)")
            .build())
    ours = base.evolve().drop_constraint("users.uq_email").build()
    theirs = base.evolve().add_fk("orders", "fk_o", ["owner"], "users",
                                  ["email"]).build()

    r = merge(base, ours, theirs)

    assert r.conflicts == ()
    v = one(r.violations)
    assert v.invariant == "fk_target_not_unique"


def test_M85_drop_a_column_the_other_side_adds_a_check_over(base_schema):
    ours = base_schema.evolve().drop_col("users.email").build()
    theirs = base_schema.evolve().add_check("users", "ck_email",
                                           "email <> ''", columns=["email"]).build()

    r = merge(base_schema, ours, theirs)

    assert r.conflicts == ()
    assert one(r.violations).invariant == "dangling_reference"


def test_M86_renaming_a_table_does_NOT_break_a_foreign_key_added_to_it():
    """The mirror image of M-80, guarding against over-correcting.

    Renames must not break references, because references are identities (D30). An
    implementation that validated names instead of ids would flag this -- and would
    make renaming a referenced table impossible, which is precisely the workflow
    this tool exists to enable.
    """
    base = (schema()
            .table("users").col("id", "bigint", pk=True)
            .table("orders").col("id", "bigint", pk=True).col("user_id", "bigint")
            .build())
    ours = base.evolve().rename_table("users", "accounts").build()
    theirs = base.evolve().add_fk("orders", "fk_u", ["user_id"], "users", ["id"]).build()

    r = merge(base, ours, theirs)

    assert r.is_clean, (r.conflicts, r.violations)
    assert r.merged.constraint_ref("orders.fk_u") == ("accounts", ["id"])


def test_M87_violation_payload_names_the_invariant_and_the_objects(base_schema):
    """'merge failed' is not an error message. A violation must say which rule broke
    and which objects broke it, or the user cannot act on it."""
    ours = base_schema.evolve().rename_col("users.email", "contact").build()
    theirs = base_schema.evolve().add_col("users", "contact", "text").build()

    v = one(merge(base_schema, ours, theirs).violations)

    assert v.invariant == "duplicate_column_name"
    assert len(v.objects) == 2, "both colliding column ids must be named"
    assert v.objects[0] != v.objects[1]
    assert "users" in v.message and "contact" in v.message


# ------------------------------------------------------------------- helpers
def one(items):
    """Assert exactly one item and return it. A merge that reports the right
    conflict plus three spurious ones is still broken."""
    assert len(items) == 1, f"expected exactly 1, got {len(items)}: {items}"
    return items[0]


def _extend_pk(snap, table, column_names):
    """Replace the primary key with one over `column_names`, preserving its id."""
    from dataclasses import replace
    t = snap.table(table)
    pk = next(c for c in t.constraints if c.kind.value == "primary_key")
    ids = tuple(t.column(n).id for n in column_names)
    new_pk = replace(pk, column_ids=ids)
    others = tuple(c for c in t.constraints if c.id != pk.id)
    return replace(snap, tables=tuple(
        replace(x, constraints=others + (new_pk,)) if x.id == t.id else x
        for x in snap.tables))
