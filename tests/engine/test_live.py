"""Generated migrations, applied to a real database (R-10..R-20, tier 1).

These are the tests that cannot be faked. A migration can be syntactically perfect and
still fail on execution -- wrong statement order, a rename onto an occupied name, a
missing cast. Every one of those produces valid-looking SQL, so no amount of string
assertion catches them. Only a real engine does.

What these assert: **the migration executes**. What they deliberately do not assert:
that the resulting schema equals the intended snapshot. That needs an introspector per
engine (D38), and conflating the two would overstate what is verified here.
"""
import pytest

from schemavcs.dialects import UnrepresentableError, emit
from schemavcs.engine.plan import plan
from schemavcs.model import schema

pytestmark = pytest.mark.engine

LOSSY_OK = dict(acknowledge_lossy=True)
EMPTY = schema().build()


def run(db, base, target, **kw):
    """Seed `base`, then apply the migration to `target`. Returns the script."""
    db.apply(emit(plan(EMPTY, base, **LOSSY_OK), db.dialect))
    script = emit(plan(base, target, **kw), db.dialect)
    db.apply(script)
    return script


def seed_only(db, snap):
    db.apply(emit(plan(EMPTY, snap, **LOSSY_OK), db.dialect))


# ==================================================== R-10: the happy path, for real
def test_R10_a_table_with_every_supported_construct_creates(db):
    target = (schema().table("t")
              .col("id", "bigint", pk=True, autoincrement=True)
              .col("name", "varchar(100)", nullable=False)
              .col("note", "varchar(200)", default="''")
              .col("qty", "int", default="0")
              .unique("uq_name", ["name"])
              .check("ck_qty", "qty >= 0")
              .index("idx_note", ["note"])
              .build())

    seed_only(db, target)


@pytest.mark.parametrize("evolve", [
    pytest.param(lambda s: s.evolve().add_col("t", "extra", "int").build(),
                 id="add_column"),
    pytest.param(lambda s: s.evolve().drop_col("t.note").build(), id="drop_column"),
    pytest.param(lambda s: s.evolve().rename_col("t.note", "memo").build(),
                 id="rename_column"),
    pytest.param(lambda s: s.evolve().retype_col("t.note", "varchar(500)").build(),
                 id="retype_narrowing"),
    pytest.param(lambda s: s.evolve().retype_col("t.qty", "bigint").build(),
                 id="retype_widening"),
    pytest.param(lambda s: s.evolve().set_nullable("t.name", True).build(),
                 id="drop_not_null"),
    pytest.param(lambda s: s.evolve().set_default("t.qty", "7").build(),
                 id="set_default"),
    pytest.param(lambda s: s.evolve().add_index("t", "idx_name", ["name"]).build(),
                 id="create_index"),
    pytest.param(lambda s: s.evolve().add_unique("t", "uq_name", ["name"]).build(),
                 id="add_unique"),
    pytest.param(lambda s: s.evolve().rename_table("t", "renamed").build(),
                 id="rename_table"),
    pytest.param(lambda s: s.evolve().drop_table("t").build(), id="drop_table"),
])
def test_R10_each_operation_kind_executes(db, evolve):
    base = (schema().table("t")
            .col("id", "bigint", pk=True)
            .col("name", "varchar(100)", nullable=False)
            .col("note", "text")
            .col("qty", "int")
            .build())

    run(db, base, evolve(base), **LOSSY_OK)


def test_R10_a_cast_requiring_retype_executes(db):
    """Postgres refuses `int -> text` without a USING clause. The plan knows that
    (`requires_cast`); this proves the clause we emit is actually accepted."""
    base = schema().table("t").col("id", "bigint", pk=True).col("n", "int").build()

    script = run(db, base, base.evolve().retype_col("t.n", "text").build(), **LOSSY_OK)

    if db.dialect == "postgres":
        assert any("USING" in s for s in script.statements)


# ================================================== R-11: ordering, for real (E-20+)
def test_R11_new_table_then_its_foreign_key(db):
    base = schema().table("users").col("id", "bigint", pk=True).build()
    target = (base.evolve().add_table("orders")
              .add_col("orders", "id", "bigint")
              .add_col("orders", "user_id", "bigint")
              .add_fk("orders", "fk_u", ["user_id"], "users", ["id"]).build())

    run(db, base, target)


def test_R11_dropping_a_table_with_an_inbound_key(db, two_tables):
    target = (two_tables.evolve().drop_constraint("orders.fk_orders_user")
              .drop_table("users").build())

    run(db, two_tables, target, **LOSSY_OK)


def test_R11_dropping_a_column_covered_by_an_index(db):
    base = (schema().table("t").col("id", "bigint", pk=True).col("c", "int")
            .index("idx_c", ["c"]).build())
    target = base.evolve().drop_index("t.idx_c").drop_col("t.c").build()

    run(db, base, target, **LOSSY_OK)


def test_R11_dropping_a_column_covered_by_a_foreign_key(db, two_tables):
    target = (two_tables.evolve().drop_constraint("orders.fk_orders_user")
              .drop_col("orders.user_id").build())

    run(db, two_tables, target, **LOSSY_OK)


def test_R11_a_circular_foreign_key_pair_applies(db):
    """E-25 for real. If foreign keys were inlined into CREATE TABLE, the first
    statement would reference a table that does not exist yet and this would fail."""
    target = (schema()
              .table("users").col("id", "bigint", pk=True).col("org_id", "bigint")
              .table("orgs").col("id", "bigint", pk=True).col("owner_id", "bigint")
              .fk("fk_owner", ["owner_id"], "users", ["id"])
              .build())
    target = target.evolve().add_fk("users", "fk_org", ["org_id"], "orgs",
                                    ["id"]).build()

    seed_only(db, target)


def test_R11_a_self_referencing_foreign_key_applies(db):
    target = (schema().table("employees")
              .col("id", "bigint", pk=True).col("manager_id", "bigint")
              .fk("fk_mgr", ["manager_id"], "employees", ["id"]).build())

    seed_only(db, target)


def test_R11_a_three_table_cycle_applies(db):
    b = schema()
    for name in ("a", "b", "c"):
        b = b.table(name).col("id", "bigint", pk=True).col("ref", "bigint")
    target = b.build()
    e = target.evolve()
    for src, dst in (("a", "b"), ("b", "c"), ("c", "a")):
        e = e.add_fk(src, f"fk_{src}", ["ref"], dst, ["id"])

    seed_only(db, e.build())


# ============================================ R-12: intermediate collisions (E-40+)
def test_R12_a_column_rename_swap_applies(db):
    """The case every naive emitter fails. Valid start, valid end, and the obvious
    two-statement sequence dies on the first statement."""
    base = (schema().table("t").col("id", "bigint", pk=True)
            .col("x", "int").col("y", "int").build())
    target = (base.evolve().rename_col("t.x", "__s").rename_col("t.y", "x")
              .rename_col("t.__s", "y").build())

    script = run(db, base, target)

    assert len(script) == 3, "two swaps plus one detour"


def test_R12_a_three_way_column_rename_cycle_applies(db):
    base = (schema().table("t").col("id", "bigint", pk=True)
            .col("a", "int").col("b", "int").col("c", "int").build())
    target = (base.evolve().rename_col("t.a", "__s").rename_col("t.c", "a")
              .rename_col("t.b", "c").rename_col("t.__s", "b").build())

    run(db, base, target)


def test_R12_a_table_name_swap_applies(db):
    base = (schema().table("a").col("id", "bigint", pk=True)
            .table("b").col("id", "bigint", pk=True).build())
    target = (base.evolve().rename_table("a", "__t").rename_table("b", "a")
              .rename_table("__t", "b").build())

    run(db, base, target)


def test_R12_renaming_onto_a_dropped_name_applies_without_a_temp(db):
    base = (schema().table("t").col("id", "bigint", pk=True)
            .col("a", "int").col("b", "int").build())
    target = base.evolve().drop_col("t.b").rename_col("t.a", "b").build()

    script = run(db, base, target, **LOSSY_OK)

    assert len(script) == 2, "a temp name here would be pure noise"


def test_R12_renaming_onto_a_name_being_added_applies(db):
    base = schema().table("t").col("id", "bigint", pk=True).col("a", "int").build()
    target = base.evolve().rename_col("t.a", "b").add_col("t", "a", "text").build()

    run(db, base, target)


# ============================================== R-20: a real merge, applied for real
def test_R20_a_merged_schema_produces_an_executable_migration(db, base_schema):
    """End to end: two branches diverge, merge cleanly, and the resulting migration
    runs. This is the whole product in one test."""
    from schemavcs.engine import merge

    ours = base_schema.evolve().rename_col("users.email", "contact").build()
    theirs = base_schema.evolve().retype_col("users.email", "text").build()
    result = merge(base_schema, ours, theirs)
    assert result.is_clean

    run(db, ours, result.merged, **LOSSY_OK)


def test_R20b_the_M80_scenario_once_resolved_applies(db, base_schema):
    """M-80 is the case where a pairwise-clean merge produces an invalid schema. Once
    a human resolves it, the migration must actually run."""
    from schemavcs.engine import merge

    ours = base_schema.evolve().rename_col("users.email", "contact").build()
    theirs = base_schema.evolve().add_col("users", "contact", "text").build()
    invalid = merge(base_schema, ours, theirs)
    assert invalid.violations, "precondition: the naive merge is invalid"

    resolved = ours.evolve().add_col("users", "contact_new", "text").build()

    run(db, ours, resolved)


# ================================================================ honest refusals
def test_indexing_a_TEXT_column_diverges_between_the_engines(db):
    """Found by running generated DDL against a real server, not by reading docs.

    MySQL stores TEXT out of line and refuses to index it without a prefix length;
    Postgres indexes it happily. The emitter now refuses rather than emitting DDL the
    server rejects -- and the refusal names the fix.
    """
    target = (schema().table("t").col("id", "bigint", pk=True).col("note", "text")
              .index("idx_note", ["note"]).build())
    p = plan(EMPTY, target, **LOSSY_OK)

    if db.dialect == "mysql":
        with pytest.raises(UnrepresentableError, match="prefix length"):
            emit(p, "mysql")
    else:
        db.apply(emit(p, "postgres"))


def test_an_unrepresentable_construct_is_refused_before_touching_the_database(db,
                                                                             base_schema):
    """A partial index has no MySQL equivalent. Refusing at emit time means the
    database is never touched -- far better than failing halfway through."""
    target = base_schema.evolve().add_index("users", "idx", ["email"],
                                            where="email IS NOT NULL").build()
    p = plan(base_schema, target)

    if db.dialect == "mysql":
        with pytest.raises(UnrepresentableError):
            emit(p, "mysql")
    else:
        seed_only(db, base_schema)
        db.apply(emit(p, "postgres"))
