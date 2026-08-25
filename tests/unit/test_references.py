"""D30 regression suite: references are identities, not names.

Found by adversarial probing, not by the test plan -- which is itself the lesson. The
suite was 141 tests green while every rename silently corrupted every index and
constraint referring to the renamed object, because both the model and the tests spoke
names throughout.

These tests are the ones that would have caught it.
"""
import pytest

from schemavcs.dialects import DDLError, parse_ddl
from schemavcs.engine import align_identity, diff
from schemavcs.model import schema


# ================================================ renames must not break references
def test_renaming_a_column_does_not_break_an_index_over_it():
    s = (schema().table("t").col("a", "int").col("b", "int")
         .index("i", ["a", "b"]).build())
    r = s.evolve().rename_col("t.a", "z").build()
    assert r.index_column_names("t.i") == ["z", "b"], "index must follow the rename"
    assert r.dangling_references() == []


def test_renaming_a_column_does_not_break_a_constraint_over_it():
    s = schema().table("t").col("a", "int").unique("uq", ["a"]).build()
    r = s.evolve().rename_col("t.a", "z").build()
    assert r.constraint_column_names("t.uq") == ["z"]
    assert r.dangling_references() == []


def test_renaming_a_table_does_not_break_a_foreign_key_to_it():
    """scope.md M-86: renames must NOT break references, because references are ids."""
    s = (schema()
         .table("users").col("id", "bigint", pk=True)
         .table("orders").col("uid", "bigint").fk("fk", ["uid"], "users", ["id"])
         .build())
    r = s.evolve().rename_table("users", "accounts").build()
    assert r.constraint_ref("orders.fk") == ("accounts", ["id"])
    assert r.dangling_references() == []


def test_renaming_a_referenced_column_does_not_break_the_foreign_key():
    s = (schema()
         .table("users").col("id", "bigint", pk=True)
         .table("orders").col("uid", "bigint").fk("fk", ["uid"], "users", ["id"])
         .build())
    r = s.evolve().rename_col("users.id", "user_id").build()
    assert r.constraint_ref("orders.fk") == ("users", ["user_id"])
    assert r.dangling_references() == []


def test_a_rename_is_invisible_to_the_diff_of_an_untouched_index():
    """The payoff: renaming a column produces exactly ONE change, not a cascade."""
    from schemavcs.engine import ChangeKind
    s = (schema().table("t").col("a", "int").index("i", ["a"]).build())
    d = diff(s, s.evolve().rename_col("t.a", "z").build())
    assert [c.kind for c in d.changes] == [ChangeKind.RENAME_COLUMN]


# ================================================== dangling refs are now DETECTABLE
def test_dropping_a_column_makes_its_index_reference_dangling():
    """Previously undetectable: a stale name and a never-existed name looked identical.
    This is what category-5 integrity validation (D11) needs to work at all."""
    s = schema().table("t").col("a", "int").col("b", "int").index("i", ["a"]).build()
    broken = s.evolve().drop_col("t.a").build()
    problems = broken.dangling_references()
    assert len(problems) == 1
    assert "index t.i" in problems[0]


def test_dropping_a_column_makes_its_constraint_reference_dangling():
    s = schema().table("t").col("a", "int").unique("uq", ["a"]).build()
    problems = s.evolve().drop_col("t.a").build().dangling_references()
    assert len(problems) == 1 and "constraint t.uq" in problems[0]


def test_dropping_a_referenced_table_makes_the_foreign_key_dangling():
    s = (schema()
         .table("users").col("id", "bigint", pk=True)
         .table("orders").col("uid", "bigint").fk("fk", ["uid"], "users", ["id"])
         .build())
    problems = s.evolve().drop_table("users").build().dangling_references()
    assert any("foreign key orders.fk" in p for p in problems)


def test_a_healthy_schema_reports_no_dangling_references():
    s = (schema()
         .table("users").col("id", "bigint", pk=True).col("email", "varchar(255)")
              .index("i", ["email"], unique=True)
         .table("orders").col("uid", "bigint").fk("fk", ["uid"], "users", ["id"])
         .build())
    assert s.dangling_references() == []


# ================================================= alignment must remap references
def test_alignment_repoints_index_references_to_the_adopted_ids():
    """Adopting a column identity without rewriting the indexes that point at it
    would leave the aligned snapshot full of dangling references."""
    sql = "CREATE TABLE t (a int, b int); CREATE INDEX i ON t (a, b)"
    a = parse_ddl(sql, dialect="postgres")
    b = parse_ddl(sql, dialect="postgres")
    aligned = align_identity(a, b)

    assert aligned.dangling_references() == []
    assert aligned.index("t.i").columns[0].column_id == a.col("t.a").id
    assert diff(a, aligned).is_empty


def test_alignment_repoints_foreign_key_references():
    sql = ("CREATE TABLE users (id bigint PRIMARY KEY);"
           "CREATE TABLE orders (uid bigint, CONSTRAINT fk FOREIGN KEY (uid) "
           "REFERENCES users (id))")
    a = parse_ddl(sql, dialect="postgres")
    aligned = align_identity(a, parse_ddl(sql, dialect="postgres"))

    assert aligned.dangling_references() == []
    assert aligned.constraint("orders.fk").ref_table_id == a.table("users").id
    assert diff(a, aligned).is_empty


# ================================================ parse-time resolution + validation
def test_parsed_schemas_have_no_dangling_references():
    s = parse_ddl("""
        CREATE TABLE users (id bigserial PRIMARY KEY, email varchar(255));
        CREATE TABLE orders (id bigserial PRIMARY KEY, uid bigint,
            CONSTRAINT fk FOREIGN KEY (uid) REFERENCES users (id));
        CREATE UNIQUE INDEX i ON users (email);
    """, dialect="postgres")
    assert s.dangling_references() == []
    assert s.constraint_ref("orders.fk") == ("users", ["id"])


def test_forward_foreign_key_reference_resolves():
    """`orders` references `users` before `users` is declared -- legal, and it needs
    the two-phase resolve to work."""
    s = parse_ddl("""
        CREATE TABLE orders (uid bigint,
            CONSTRAINT fk FOREIGN KEY (uid) REFERENCES users (id));
        CREATE TABLE users (id bigint PRIMARY KEY);
    """, dialect="postgres")
    assert s.constraint_ref("orders.fk") == ("users", ["id"])
    assert s.dangling_references() == []


def test_circular_foreign_keys_resolve_in_both_directions():
    """E-25's precondition. Neither table can be resolved before the other exists."""
    s = parse_ddl("""
        CREATE TABLE users (id bigint PRIMARY KEY, org_id bigint,
            CONSTRAINT fk_u FOREIGN KEY (org_id) REFERENCES orgs (id));
        CREATE TABLE orgs (id bigint PRIMARY KEY, owner_id bigint,
            CONSTRAINT fk_o FOREIGN KEY (owner_id) REFERENCES users (id));
    """, dialect="postgres")
    assert s.constraint_ref("users.fk_u") == ("orgs", ["id"])
    assert s.constraint_ref("orgs.fk_o") == ("users", ["id"])
    assert s.dangling_references() == []


# ============================================ P2 defect: duplicate constraint names
def test_two_primary_keys_are_rejected():
    with pytest.raises(DDLError, match="(?i)primary key"):
        parse_ddl("CREATE TABLE t (a int PRIMARY KEY, b int PRIMARY KEY)",
                  dialect="postgres")


def test_inline_pk_plus_table_level_pk_is_rejected():
    with pytest.raises(DDLError) as e:
        parse_ddl("CREATE TABLE t (a int PRIMARY KEY, b int, PRIMARY KEY (b))",
                  dialect="postgres")
    assert "duplicate constraint name" in str(e.value).lower() or \
           "primary key" in str(e.value).lower()


def test_duplicate_explicit_constraint_names_are_rejected():
    with pytest.raises(DDLError, match="(?i)duplicate constraint name"):
        parse_ddl("CREATE TABLE t (a int, b int, "
                  "CONSTRAINT c UNIQUE (a), CONSTRAINT c UNIQUE (b))",
                  dialect="postgres")


def test_duplicate_index_names_on_one_table_are_rejected():
    with pytest.raises(DDLError, match="(?i)duplicate index name"):
        parse_ddl("CREATE TABLE t (a int, b int); "
                  "CREATE INDEX i ON t (a); CREATE INDEX i ON t (b)",
                  dialect="postgres")


# =========================================================== serialization holds
def test_references_survive_a_serialization_round_trip():
    from schemavcs.model import Snapshot
    s = (schema()
         .table("users").col("id", "bigint", pk=True)
         .table("orders").col("uid", "bigint").fk("fk", ["uid"], "users", ["id"])
         .build())
    back = Snapshot.from_dict(s.to_dict())
    assert back.constraint_ref("orders.fk") == ("users", ["id"])
    assert back.dangling_references() == []
    assert back.content_hash() == s.content_hash()


# ================================ P1 defect: schema qualifiers were silently dropped
def test_schema_qualified_table_name_is_rejected_not_silently_flattened():
    """`public.users` used to parse as `users`, discarding the qualifier -- so
    public.users and audit.users collapsed into one object. Silent loss is exactly
    the class D21 forbids, and it is worse than the constructs we reject outright
    because it looks like it worked."""
    with pytest.raises(DDLError) as e:
        parse_ddl("CREATE TABLE public.users (id int)", dialect="postgres")
    assert "public.users" in str(e.value)
    assert "not supported" in str(e.value)


def test_two_qualified_tables_no_longer_collide_silently():
    with pytest.raises(DDLError) as e:
        parse_ddl("CREATE TABLE public.users (id int); CREATE TABLE audit.users (id int)",
                  dialect="postgres")
    assert len(e.value.problems) == 2


def test_qualified_foreign_key_target_fails_loudly_with_the_qualifier_visible():
    with pytest.raises(DDLError) as e:
        parse_ddl("CREATE TABLE a (i int PRIMARY KEY);"
                  "CREATE TABLE b (j int, CONSTRAINT fk FOREIGN KEY (j) "
                  "REFERENCES public.a (i))", dialect="postgres")
    assert "public.a" in str(e.value), "the qualifier must appear in the error"


def test_qualified_index_target_fails_loudly():
    with pytest.raises(DDLError) as e:
        parse_ddl("CREATE TABLE t (a int); CREATE INDEX i ON public.t (a)",
                  dialect="postgres")
    assert "public.t" in str(e.value)


def test_unqualified_names_are_unaffected():
    s = parse_ddl("CREATE TABLE users (id int PRIMARY KEY)", dialect="postgres")
    assert s.table("users") is not None
