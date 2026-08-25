"""F-* : diff engine. See docs/test-plan.md section 5.

The base schema arrives via the `base_schema` fixture (tests/conftest.py) rather than a
local helper. A helper called twice in one test yields two snapshots with no shared
UUIDs, and since diff is identity-based that silently compares unrelated objects -- a
mistake I made in eight tests here before the D29 guard existed to catch it.

Note on the idempotency block: `diff` is identity-based, so two *independently
parsed* snapshots share no UUIDs and cannot be compared directly. Re-importing DDL
is an align-then-diff operation -- alignment by name is deterministic, and rename
inference (the heuristic half) is deliberately not part of it. See engine/align.py.

Two groups carry the weight:

  F-20..F-27  renames reported AS renames. This is the entire reason identity exists
              (D1); without it a rename is indistinguishable from a data-destroying
              drop-plus-add.
  F-40..F-46  idempotency. A diff tool that cries wolf gets closed and never reopened,
              so these are the gate before any merge work starts.
"""
import pytest

from schemavcs.dialects import parse_ddl
from schemavcs.engine import ChangeKind, align_identity, diff
from schemavcs.model import ColumnType, IndexColumn, schema


def kinds(d):
    return [c.kind for c in d.changes]


# ==================================================== happy path (F-01..F-08)
def test_f01_f02_add_and_drop_table(base_schema):
    s = base_schema
    added = s.evolve().add_table("orders").build()
    assert kinds(diff(s, added)) == [ChangeKind.CREATE_TABLE]
    assert kinds(diff(added, s)) == [ChangeKind.DROP_TABLE]


def test_f03_f04_add_and_drop_column(base_schema):
    s = base_schema
    added = s.evolve().add_col("users", "bio", "text").build()
    d = diff(s, added)
    assert kinds(d) == [ChangeKind.ADD_COLUMN]
    assert d.changes[0].name == "bio"
    assert kinds(diff(added, s)) == [ChangeKind.DROP_COLUMN]


def test_f05_nullability_change(base_schema):
    s = base_schema
    d = diff(s, s.evolve().set_nullable("users.email", True).build())
    assert kinds(d) == [ChangeKind.ALTER_COLUMN]
    assert [x.attribute for x in d.changes[0].deltas] == ["nullable"]


def test_f06_default_change(base_schema):
    s = base_schema
    d = diff(s, s.evolve().set_default("users.email", "'x'").build())
    assert [x.attribute for x in d.changes[0].deltas] == ["default"]


def test_f07_add_and_drop_index(base_schema):
    s = base_schema
    with_idx = s.evolve().add_index("users", "i", ["email"]).build()
    assert kinds(diff(s, with_idx)) == [ChangeKind.CREATE_INDEX]
    assert kinds(diff(with_idx, s)) == [ChangeKind.DROP_INDEX]


def test_f08_add_and_drop_constraint(base_schema):
    s = base_schema
    with_uq = s.evolve().add_unique("users", "uq_email", ["email"]).build()
    assert kinds(diff(s, with_uq)) == [ChangeKind.ADD_CONSTRAINT]
    assert kinds(diff(with_uq, s)) == [ChangeKind.DROP_CONSTRAINT]


def test_add_unique_resolves_column_names_to_ids(base_schema):
    """The editor takes names at the edge and stores ids (D30)."""
    s = base_schema.evolve().add_unique("users", "uq_email", ["email"]).build()
    assert s.constraint("users.uq_email").column_ids == (s.col("users.email").id,)
    assert s.constraint_column_names("users.uq_email") == ["email"]


def test_add_unique_on_an_unknown_column_is_rejected(base_schema):
    from schemavcs.model import SchemaError
    with pytest.raises(SchemaError, match="ghost"):
        base_schema.evolve().add_unique("users", "uq", ["ghost"]).build()


# ======================================================= renames (F-20..F-27)
def test_f20_rename_is_a_rename_not_a_drop_and_add(base_schema):
    """The single most important behaviour in the diff engine (D1)."""
    s = base_schema
    d = diff(s, s.evolve().rename_col("users.email", "email_address").build())
    assert kinds(d) == [ChangeKind.RENAME_COLUMN]
    assert (d.changes[0].before_name, d.changes[0].name) == ("email", "email_address")
    assert ChangeKind.DROP_COLUMN not in kinds(d)
    assert ChangeKind.ADD_COLUMN not in kinds(d)


def test_f21_rename_table_does_not_re_report_its_columns(base_schema):
    s = base_schema
    d = diff(s, s.evolve().rename_table("users", "accounts").build())
    assert kinds(d) == [ChangeKind.RENAME_TABLE]


def test_f22_retype_column(base_schema):
    s = base_schema
    d = diff(s, s.evolve().retype_col("users.email", "text").build())
    assert kinds(d) == [ChangeKind.ALTER_COLUMN]
    delta = d.changes[0].deltas[0]
    assert delta.attribute == "type"
    assert delta.after == ColumnType.parse("text")


def test_f23_rename_plus_retype_is_one_change_with_two_deltas(base_schema):
    """A name-keyed differ cannot express this at all."""
    s = base_schema
    target = (s.evolve()
              .rename_col("users.email", "email_address")
              .retype_col("users.email_address", "text")
              .build())
    d = diff(s, target)
    assert len(d.changes) == 1, "one column changed, so one change"
    c = d.changes[0]
    assert c.kind == ChangeKind.RENAME_COLUMN, "a rename is still a rename"
    assert {x.attribute for x in c.deltas} == {"name", "type"}


def test_f24_rename_chain_across_commits_collapses_to_one_rename(base_schema):
    """Free because identity survives every step (D2)."""
    s = base_schema
    end = (s.evolve().rename_col("users.email", "b").build()
            .evolve().rename_col("users.b", "c").build())
    d = diff(s, end)
    assert kinds(d) == [ChangeKind.RENAME_COLUMN]
    assert (d.changes[0].before_name, d.changes[0].name) == ("email", "c")


def test_f25_rename_swap_is_two_renames_with_no_drops():
    s = (schema().table("t").col("x", "int").col("y", "int").build())
    swapped = (s.evolve()
               .rename_col("t.x", "_tmp").rename_col("t.y", "x").rename_col("t._tmp", "y")
               .build())
    d = diff(s, swapped)
    assert kinds(d) == [ChangeKind.RENAME_COLUMN, ChangeKind.RENAME_COLUMN]
    assert {(c.before_name, c.name) for c in d.changes} == {("x", "y"), ("y", "x")}


def test_f26_table_rename_and_column_change_are_both_reported(base_schema):
    s = base_schema
    target = (s.evolve()
              .rename_table("users", "accounts")
              .retype_col("accounts.email", "text")
              .build())
    d = diff(s, target)
    assert set(kinds(d)) == {ChangeKind.RENAME_TABLE, ChangeKind.ALTER_COLUMN}
    # the column change is attributed to the table by identity, under its new name
    col_change = next(c for c in d.changes if c.kind == ChangeKind.ALTER_COLUMN)
    assert col_change.table == "accounts"


def test_f27_drop_one_add_another_is_not_inferred_as_a_rename(base_schema):
    """Identity is authoritative: the diff engine never guesses. Guessing is the
    import path's job, and only with human confirmation (D22)."""
    s = base_schema
    target = (s.evolve()
              .drop_col("users.email")
              .add_col("users", "email_address", "varchar(255)")
              .build())
    d = diff(s, target)
    assert set(kinds(d)) == {ChangeKind.DROP_COLUMN, ChangeKind.ADD_COLUMN}
    assert ChangeKind.RENAME_COLUMN not in kinds(d)


def test_index_rename_and_column_list_change_are_distinguished():
    s = (schema().table("t").col("a", "int").col("b", "int")
         .index("i", ["a"]).build())
    assert kinds(diff(s, s.evolve().rename_index("t.i", "j").build())) == \
        [ChangeKind.RENAME_INDEX]
    d = diff(s, s.evolve().set_index_columns("t.i", ["a", "b"]).build())
    assert kinds(d) == [ChangeKind.ALTER_INDEX]
    assert [x.attribute for x in d.changes[0].deltas] == ["columns"]


# =================================================== idempotency (F-40..F-46)
def test_f40_self_diff_is_empty(base_schema):
    s = base_schema
    assert diff(s, s).is_empty
    assert diff(s, s).changes == ()


def test_f41_f42_diff_after_ddl_round_trip_and_reformatting_is_empty():
    sql = """CREATE TABLE users (
                 id bigserial PRIMARY KEY,
                 email varchar(255) NOT NULL DEFAULT 'x'
             );
             CREATE UNIQUE INDEX i ON users (email);"""
    reformatted = ("create table USERS ( ID BIGSERIAL primary key,\n\n"
                   "  EMAIL VARCHAR(255) not null default 'x' );\n"
                   "create unique index I on USERS (EMAIL)")
    a = parse_ddl(sql, dialect="postgres")
    b = parse_ddl(reformatted, dialect="postgres")
    assert diff(a, align_identity(a, b)).is_empty, "formatting is not a schema change"


@pytest.mark.parametrize("a,b", [
    ("varchar(255)", "character varying(255)"),
    ("int", "integer"),
    ("timestamptz", "timestamp with time zone"),
])
def test_f43_equivalent_type_spellings_produce_no_diff(a, b):
    x = parse_ddl(f"CREATE TABLE t (c {a})", dialect="postgres")
    y = parse_ddl(f"CREATE TABLE t (c {b})", dialect="postgres")
    assert diff(x, align_identity(x, y)).is_empty


def test_f43b_equivalent_default_spellings_produce_no_diff():
    """P-23 reaching into the diff: this is the spurious-diff killer."""
    x = parse_ddl("CREATE TABLE t (a datetime DEFAULT now())", dialect="mysql")
    y = parse_ddl("CREATE TABLE t (a datetime DEFAULT CURRENT_TIMESTAMP)", dialect="mysql")
    assert diff(x, align_identity(x, y)).is_empty


def test_f44_reordered_create_table_statements_produce_no_diff():
    x = parse_ddl("CREATE TABLE a (i int); CREATE TABLE b (j int)", dialect="postgres")
    y = parse_ddl("CREATE TABLE b (j int); CREATE TABLE a (i int)", dialect="postgres")
    assert diff(x, align_identity(x, y)).is_empty


def test_f45_reordered_columns_produce_no_diff():
    """D27: column position is not modeled, so this must be silent."""
    x = parse_ddl("CREATE TABLE t (a int, b int)", dialect="postgres")
    y = parse_ddl("CREATE TABLE t (b int, a int)", dialect="postgres")
    assert diff(x, align_identity(x, y)).is_empty


def test_f46_reordered_index_columns_DO_produce_a_diff():
    """D4: index column order determines whether the index is usable at all."""
    x = parse_ddl("CREATE TABLE t (a int, b int); CREATE INDEX i ON t (a, b)",
                  dialect="postgres")
    y = parse_ddl("CREATE TABLE t (a int, b int); CREATE INDEX i ON t (b, a)",
                  dialect="postgres")
    assert not diff(x, align_identity(x, y)).is_empty


def test_prefix_length_change_is_a_diff():
    x = parse_ddl("CREATE TABLE t (`u` varchar(255), KEY `i` (`u`(32)))", dialect="mysql")
    y = parse_ddl("CREATE TABLE t (`u` varchar(255), KEY `i` (`u`(64)))", dialect="mysql")
    assert not diff(x, align_identity(x, y)).is_empty


# ------------------------------------------------------------------ shape
def test_diff_is_symmetric_in_size(base_schema):
    s = base_schema
    t = s.evolve().add_col("users", "z", "int").build()
    assert len(diff(s, t).changes) == len(diff(t, s).changes)


def test_changes_are_stably_ordered(base_schema):
    """Unstable ordering would make the UI jitter and golden files churn."""
    s = base_schema
    t = (s.evolve().add_col("users", "z", "int").add_table("orders")
         .rename_col("users.email", "mail").build())
    assert [c.kind for c in diff(s, t).changes] == [c.kind for c in diff(s, t).changes]


# ------------------------------------------------- the D29 guard, made loud
def test_diffing_unaligned_snapshots_raises_instead_of_lying():
    """The mistake D29 describes -- and the one I made in eight tests while building
    this. Comparing two independently parsed snapshots reports every object as
    dropped and re-added, which is technically true of the ids and useless to a user.
    """
    from schemavcs.engine import UnalignedSnapshotsError
    a = parse_ddl("CREATE TABLE t (a int)", dialect="postgres")
    b = parse_ddl("CREATE TABLE t (a int)", dialect="postgres")
    with pytest.raises(UnalignedSnapshotsError) as e:
        diff(a, b)
    assert "align_identity" in str(e.value), "the error must name the fix"
    assert e.value.shared_names == {"t"}


def test_the_guard_permits_diffing_against_an_empty_snapshot():
    """Genesis against a first commit legitimately shares nothing."""
    from schemavcs.model import Snapshot
    populated = parse_ddl("CREATE TABLE t (a int)", dialect="postgres")
    assert len(diff(Snapshot(), populated).changes) == 1


def test_the_guard_permits_diffing_genuinely_unrelated_schemas():
    """No shared names means no reason to think alignment was forgotten."""
    a = parse_ddl("CREATE TABLE alpha (a int)", dialect="postgres")
    b = parse_ddl("CREATE TABLE beta (b int)", dialect="postgres")
    assert {c.kind for c in diff(a, b).changes} == {
        ChangeKind.DROP_TABLE, ChangeKind.CREATE_TABLE}


def test_fixture_based_tests_are_correct_by_construction(base_schema):
    """Using the fixture, the two-call mistake is not expressible."""
    renamed = base_schema.evolve().rename_col("users.email", "mail").build()
    assert [c.kind for c in diff(base_schema, renamed).changes] == [ChangeKind.RENAME_COLUMN]
