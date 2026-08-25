"""Migration planning -- ordering, collisions, safety (E-20..E-72).

Every assertion here is about the *operation sequence*, not about SQL text. That is
the point of splitting the planner from the renderers (D35): `E-25` and `E-40` are
questions about order and phasing, and string matching would bury them in quoting and
whitespace. SQL strings are tested separately, where they are actually the subject.
"""
import pytest

from schemavcs.engine.plan import (OpKind, Safety, UnacknowledgedRiskError,
                                   TEMP_PREFIX, plan)
from schemavcs.model import schema

LOSSY_OK = dict(acknowledge_lossy=True)


def kinds(p):
    return [o.kind for o in p]


def before(p, a: OpKind, b: OpKind, name_a=None, name_b=None):
    """Assert operation `a` precedes operation `b`."""
    assert p.index_of(a, name_a) < p.index_of(b, name_b), \
        f"{a.value} must precede {b.value}, got {[o.describe() for o in p]}"


# ============================================================== ordering (E-20+)
def test_E20_a_new_table_is_created_before_the_key_that_references_it():
    base = schema().table("users").col("id", "bigint", pk=True).build()
    target = (base.evolve()
              .add_table("orders").add_col("orders", "user_id", "bigint")
              .add_fk("orders", "fk_u", ["user_id"], "users", ["id"]).build())

    p = plan(base, target)

    before(p, OpKind.CREATE_TABLE, OpKind.ADD_CONSTRAINT)


def test_E21_an_inbound_foreign_key_is_dropped_before_its_target_table(two_tables):
    target = (two_tables.evolve()
              .drop_constraint("orders.fk_orders_user")
              .drop_table("users").build())

    p = plan(two_tables, target, **LOSSY_OK)

    before(p, OpKind.DROP_CONSTRAINT, OpKind.DROP_TABLE)


def test_E22_a_column_is_added_before_the_index_over_it(base_schema):
    target = (base_schema.evolve().add_col("users", "age", "int")
              .add_index("users", "idx_age", ["age"]).build())

    p = plan(base_schema, target)

    before(p, OpKind.ADD_COLUMN, OpKind.CREATE_INDEX)


def test_E23_an_index_is_dropped_before_the_column_it_covers(base_schema):
    base = base_schema.evolve().add_index("users", "idx", ["email"]).build()
    target = base.evolve().drop_index("users.idx").drop_col("users.email").build()

    p = plan(base, target, **LOSSY_OK)

    before(p, OpKind.DROP_INDEX, OpKind.DROP_COLUMN)


def test_E24_a_foreign_key_is_dropped_before_its_own_column(two_tables):
    target = (two_tables.evolve().drop_constraint("orders.fk_orders_user")
              .drop_col("orders.user_id").build())

    p = plan(two_tables, target, **LOSSY_OK)

    before(p, OpKind.DROP_CONSTRAINT, OpKind.DROP_COLUMN)


def test_E25_a_circular_foreign_key_pair_is_created_in_phases():
    """The case with no valid linear order if tables are created complete.

    Separating table creation from key addition dissolves it: both tables exist
    before either key is added, so there is no cycle to break.
    """
    base = schema().build()
    target = (schema()
              .table("users").col("id", "bigint", pk=True).col("org_id", "bigint")
              .table("orgs").col("id", "bigint", pk=True).col("owner_id", "bigint")
              .fk("fk_owner", ["owner_id"], "users", ["id"])
              .build())
    target = target.evolve().add_fk("users", "fk_org", ["org_id"], "orgs",
                                    ["id"]).build()

    p = plan(base, target)

    creates = [i for i, o in enumerate(p) if o.kind is OpKind.CREATE_TABLE]
    keys = [i for i, o in enumerate(p) if o.kind is OpKind.ADD_CONSTRAINT]
    assert len(creates) == 2 and len(keys) == 2
    assert max(creates) < min(keys), \
        "every table must exist before any foreign key is added"


def test_E26_a_self_referencing_foreign_key_is_added_after_the_table():
    base = schema().build()
    target = (schema().table("employees")
              .col("id", "bigint", pk=True).col("manager_id", "bigint")
              .fk("fk_mgr", ["manager_id"], "employees", ["id"]).build())

    p = plan(base, target)

    before(p, OpKind.CREATE_TABLE, OpKind.ADD_CONSTRAINT)


def test_E27_a_three_table_cycle_is_phased_correctly():
    base = schema().build()
    b = schema()
    for name in ("a", "b", "c"):
        b = b.table(name).col("id", "bigint", pk=True).col("ref", "bigint")
    target = b.build()
    e = target.evolve()
    for src, dst in (("a", "b"), ("b", "c"), ("c", "a")):
        e = e.add_fk(src, f"fk_{src}", ["ref"], dst, ["id"])
    target = e.build()

    p = plan(base, target)

    creates = [i for i, o in enumerate(p) if o.kind is OpKind.CREATE_TABLE]
    keys = [i for i, o in enumerate(p) if o.kind is OpKind.ADD_CONSTRAINT]
    assert len(creates) == 3 and len(keys) == 3
    assert max(creates) < min(keys)


def test_E28_renaming_a_column_does_not_recreate_the_index_over_it(base_schema):
    """The index stores a column id, so a rename leaves it untouched (D30). A
    name-keyed emitter would spuriously drop and recreate it -- which on a large
    table is an outage, not an inefficiency."""
    base = base_schema.evolve().add_index("users", "idx", ["email"]).build()
    target = base.evolve().rename_col("users.email", "contact").build()

    p = plan(base, target)

    assert kinds(p) == [OpKind.RENAME_COLUMN]
    assert not p.of_kind(OpKind.DROP_INDEX, OpKind.CREATE_INDEX)


def test_E29_a_wide_foreign_key_graph_plans_without_blowing_up():
    base = schema().build()
    b = schema()
    for i in range(50):
        b = b.table(f"t{i}").col("id", "bigint", pk=True).col("ref", "bigint")
    target = b.build()
    e = target.evolve()
    for i in range(1, 50):
        e = e.add_fk(f"t{i}", f"fk_{i}", ["ref"], f"t{i-1}", ["id"])
    target = e.build()

    p = plan(base, target)

    assert len(p.of_kind(OpKind.CREATE_TABLE)) == 50
    assert len(p.of_kind(OpKind.ADD_CONSTRAINT)) == 49
    creates = [i for i, o in enumerate(p) if o.kind is OpKind.CREATE_TABLE]
    keys = [i for i, o in enumerate(p) if o.kind is OpKind.ADD_CONSTRAINT]
    assert max(creates) < min(keys)


# ================================================= intermediate collisions (E-40+)
def test_E40_a_rename_swap_is_routed_through_a_temporary_name(base_schema):
    """The 15-second reviewer test: valid start, valid end, invalid middle.

    Every naive emitter produces `RENAME a TO b; RENAME b TO a` and the first
    statement fails.
    """
    base = (base_schema.evolve().add_col("users", "x", "int")
            .add_col("users", "y", "text").build())
    target = (base.evolve().rename_col("users.x", "__swap")
              .rename_col("users.y", "x").rename_col("users.__swap", "y").build())

    p = plan(base, target)

    renames = p.of_kind(OpKind.RENAME_COLUMN)
    assert len(renames) == 3, "two swaps plus one detour"
    assert sum(1 for o in renames if o.temp) == 2, "the detour is marked as machinery"
    # No statement may ever rename onto a name that is still occupied.
    live = {"x", "y"}
    for o in renames:
        assert o.new_name not in live - {o.name}, \
            f"{o.describe()} collides with a live name"
        live.discard(o.name)
        live.add(o.new_name)


def test_E41_a_three_way_rename_cycle_executes(base_schema):
    base = (base_schema.evolve().add_col("users", "a", "int")
            .add_col("users", "b", "int").add_col("users", "c", "int").build())
    target = (base.evolve().rename_col("users.a", "tmp")
              .rename_col("users.c", "a").rename_col("users.b", "c")
              .rename_col("users.tmp", "b").build())

    p = plan(base, target)

    live = {"a", "b", "c"}
    for o in p.of_kind(OpKind.RENAME_COLUMN):
        assert o.new_name not in live - {o.name}
        live.discard(o.name)
        live.add(o.new_name)
    assert live == {"a", "b", "c"}


def test_E42_a_table_name_swap_is_routed_through_a_temporary_name():
    base = (schema().table("a").col("id", "int")
            .table("b").col("id", "int").build())
    target = (base.evolve().rename_table("a", "__t").rename_table("b", "a")
              .rename_table("__t", "b").build())

    p = plan(base, target)

    live = {"a", "b"}
    for o in p.of_kind(OpKind.RENAME_TABLE):
        assert o.new_name not in live - {o.table}
        live.discard(o.table)
        live.add(o.new_name)
    assert live == {"a", "b"}


def test_E43_renaming_onto_a_dropped_name_needs_no_temporary(base_schema):
    """Minimality. The drop already frees the name, so a temp name would be pure
    noise -- and noise in generated DDL is what stops people reading it."""
    base = (base_schema.evolve().add_col("users", "a", "int")
            .add_col("users", "b", "int").build())
    target = base.evolve().drop_col("users.b").rename_col("users.a", "b").build()

    p = plan(base, target, **LOSSY_OK)

    before(p, OpKind.DROP_COLUMN, OpKind.RENAME_COLUMN)
    assert not any(o.temp for o in p)


def test_E44_renaming_onto_a_name_being_added_is_ordered_not_temped(base_schema):
    base = base_schema.evolve().add_col("users", "a", "int").build()
    target = (base.evolve().rename_col("users.a", "b")
              .add_col("users", "a", "text").build())

    p = plan(base, target)

    before(p, OpKind.RENAME_COLUMN, OpKind.ADD_COLUMN)
    assert not any(o.temp for o in p)


def test_E45_a_temporary_name_never_shadows_an_existing_identifier():
    """Asserted against an adversarial schema that already occupies the obvious
    temp names."""
    b = schema().table("t").col("x", "int").col("y", "int")
    for i in range(1, 4):
        b = b.col(f"{TEMP_PREFIX}_{i}", "int")
    base = b.build()
    target = (base.evolve().rename_col("t.x", "__s").rename_col("t.y", "x")
              .rename_col("t.__s", "y").build())

    p = plan(base, target)

    existing = {c.name for c in base.table("t").columns}
    for o in p.of_kind(OpKind.RENAME_COLUMN):
        if o.temp:
            assert o.new_name not in existing or o.new_name in ("x", "y")


def test_E46_no_temporary_name_is_emitted_when_none_is_needed(base_schema):
    """Guards against unconditional temp-routing, which would be the lazy fix for
    E-40 and would double the length of every rename migration."""
    target = base_schema.evolve().rename_col("users.email", "contact").build()

    p = plan(base_schema, target)

    assert kinds(p) == [OpKind.RENAME_COLUMN]
    assert not any(o.temp for o in p)
    assert TEMP_PREFIX not in str([o.describe() for o in p])


# ================================================== safety classification (E-60+)
@pytest.mark.parametrize("frm,to,expected,cast", [
    ("varchar(50)", "varchar(255)", Safety.SAFE, False),      # E-60
    ("varchar(255)", "varchar(50)", Safety.LOSSY, False),     # E-61
    ("int", "bigint", Safety.SAFE, False),                    # E-62
    ("bigint", "int", Safety.LOSSY, False),                   # E-63
    ("int", "text", Safety.SAFE, True),                       # E-64
    ("text", "int", Safety.LOSSY, True),                      # E-65
    ("varchar(50)", "text", Safety.SAFE, False),
    ("text", "varchar(50)", Safety.LOSSY, False),
    ("decimal(10,2)", "decimal(12,2)", Safety.SAFE, False),
    ("decimal(12,2)", "decimal(10,2)", Safety.LOSSY, False),
])
def test_E60_to_E65_retype_safety(base_schema, frm, to, expected, cast):
    base = base_schema.evolve().add_col("users", "c", frm).build()
    target = base.evolve().retype_col("users.c", to).build()

    op = plan(base, target, **LOSSY_OK).of_kind(OpKind.ALTER_COLUMN_TYPE)[0]

    assert op.safety is expected
    assert op.requires_cast is cast


def test_E66_dropping_a_column_is_lossy(base_schema):
    target = base_schema.evolve().drop_col("users.email").build()

    p = plan(base_schema, target, **LOSSY_OK)

    assert p.of_kind(OpKind.DROP_COLUMN)[0].safety is Safety.LOSSY


def test_E67_adding_a_nullable_column_is_safe(base_schema):
    target = base_schema.evolve().add_col("users", "note", "text").build()

    assert plan(base_schema, target).worst_safety is Safety.SAFE


def test_E68_adding_a_not_null_column_with_no_default_is_unsafe(base_schema):
    """It fails outright on any table that already has rows. Reporting this as
    'safe' because the DDL is syntactically fine would be the tool lying."""
    target = base_schema.evolve().add_col("users", "code", "int",
                                          nullable=False).build()

    op = plan(base_schema, target, **LOSSY_OK).of_kind(OpKind.ADD_COLUMN)[0]

    assert op.safety is Safety.UNSAFE


def test_E69_adding_a_not_null_column_with_a_default_is_lock_heavy(base_schema):
    target = (base_schema.evolve().add_col("users", "code", "int", nullable=False)
              .set_default("users.code", "0").build())

    op = plan(base_schema, target).of_kind(OpKind.ADD_COLUMN)[0]

    assert op.safety is Safety.LOCK_HEAVY


def test_E70_creating_an_index_is_lock_heavy(base_schema):
    target = base_schema.evolve().add_index("users", "idx", ["email"]).build()

    assert plan(base_schema, target).worst_safety is Safety.LOCK_HEAVY


def test_E71_a_lossy_plan_is_refused_by_default(base_schema):
    """The product call. A tool that silently emits DROP COLUMN because that was the
    mathematically correct diff is not one anyone should aim at production."""
    target = base_schema.evolve().drop_col("users.email").build()

    with pytest.raises(UnacknowledgedRiskError) as e:
        plan(base_schema, target)

    assert "destroys data" in str(e.value)
    assert "users.email" in str(e.value), "the message must name what would be lost"


def test_E72_the_same_plan_proceeds_once_acknowledged(base_schema):
    target = base_schema.evolve().drop_col("users.email").build()

    p = plan(base_schema, target, acknowledge_lossy=True)

    assert kinds(p) == [OpKind.DROP_COLUMN]


def test_tightening_nullability_is_unsafe_loosening_is_safe(base_schema):
    tighten = base_schema.evolve().add_col("users", "n", "int").build()
    assert plan(tighten, tighten.evolve().set_nullable("users.n", False).build(),
                **LOSSY_OK).of_kind(OpKind.SET_NOT_NULL)[0].safety is Safety.UNSAFE

    loosen = base_schema.evolve().set_nullable("users.email", True).build()
    assert plan(base_schema, loosen).worst_safety is Safety.SAFE


# ------------------------------------------------------------------ name replay
def test_operations_after_a_table_rename_address_the_new_name(base_schema):
    """DDL addresses objects by name, and names move mid-migration. A statement after
    `RENAME TABLE users TO accounts` must say `accounts`."""
    target = (base_schema.evolve().rename_table("users", "accounts")
              .add_col("accounts", "age", "int").build())

    p = plan(base_schema, target)

    rename = p.of_kind(OpKind.RENAME_TABLE)[0]
    add = p.of_kind(OpKind.ADD_COLUMN)[0]
    assert rename.table == "users" and rename.new_name == "accounts"
    assert add.table == "accounts", "the column is added to the renamed table"


def test_an_empty_diff_produces_an_empty_plan(base_schema):
    assert plan(base_schema, base_schema).is_empty


def test_plans_are_deterministic(base_schema):
    target = (base_schema.evolve().add_col("users", "a", "int")
              .add_col("users", "b", "int")
              .add_index("users", "i", ["a"]).build())

    first = [o.describe() for o in plan(base_schema, target)]
    second = [o.describe() for o in plan(base_schema, target)]

    assert first == second


@pytest.mark.parametrize("frm,to", [
    ("date", "json"),
    ("boolean", "date"),
    ("uuid", "int"),
    ("json", "boolean"),
])
def test_an_unrecognized_retype_is_assumed_lossy(base_schema, frm, to):
    """The conservative default, and a deliberate one.

    Guessing 'probably fine' on a type pair the classifier does not recognize is how a
    tool silently truncates a production column. Unknown means lossy, which means the
    plan is refused until a human acknowledges it -- the failure mode is an
    unnecessary confirmation prompt, not data loss.
    """
    base = base_schema.evolve().add_col("users", "c", frm).build()
    target = base.evolve().retype_col("users.c", to).build()

    with pytest.raises(UnacknowledgedRiskError):
        plan(base, target)

    op = plan(base, target, **LOSSY_OK).of_kind(OpKind.ALTER_COLUMN_TYPE)[0]
    assert op.safety is Safety.LOSSY
    assert op.requires_cast is True


def test_the_migration_is_diff_from_the_deployed_schema_not_from_the_merge_base():
    """D15, and a mistake I made in my own README before this test existed.

    After a merge the branch head *is* the merged result, so planning against the
    branch produces an empty migration -- correct, and useless. The migration a user
    needs is from whatever is actually deployed to the merged result. Easy to get
    wrong precisely because the wrong version silently succeeds.
    """
    from schemavcs.engine import Repo, merge_branches
    from schemavcs.model import schema

    base = (schema().table("users").col("id", "bigint", pk=True)
            .col("email", "varchar(255)").build())
    repo = Repo.init("main")
    repo.commit("main", base)
    repo.branch("feature", "main")
    repo.commit("feature", base.evolve().rename_col("users.email",
                                                    "contact").build())
    repo.commit("main", base.evolve().retype_col("users.email", "text").build())

    deployed = repo.snapshot("main")
    result = merge_branches(repo, ours="main", theirs="feature")
    merged = result.commit.snapshot

    assert plan(merged, merged).is_empty, \
        "planning against the post-merge branch head yields nothing"

    p = plan(deployed, merged)
    assert kinds(p) == [OpKind.RENAME_COLUMN], \
        "the deployed database only needs the change it has not seen"
