"""P-* : DDL ingest. See docs/test-plan.md section 2.

The highest-stakes group here is P-60..P-68: unsupported constructs must be REJECTED,
never skipped. A quietly dropped CHECK constraint makes every subsequent diff, merge and
migration wrong with no signal to the user (D21). Refusing an import is recoverable;
importing something subtly incorrect is not.
"""
import pytest

from schemavcs.dialects import DDLError, parse_ddl
from schemavcs.model import ColumnType, ConstraintKind


def pg(sql):
    return parse_ddl(sql, dialect="postgres")


def my(sql):
    return parse_ddl(sql, dialect="mysql")


# ============================================================ happy path
def test_p01_create_table_full_fidelity():
    s = pg("""
        CREATE TABLE users (
            id           bigserial PRIMARY KEY,
            email        varchar(255) NOT NULL,
            is_active    boolean DEFAULT true,
            signup_count integer NOT NULL DEFAULT 0,
            created_at   timestamptz DEFAULT now(),
            bio          text,
            CONSTRAINT uq_users_email UNIQUE (email),
            CONSTRAINT ck_users_count CHECK (signup_count >= 0)
        );
    """)
    t = s.table("users")
    assert [c.name for c in t.columns] == [
        "id", "email", "is_active", "signup_count", "created_at", "bio"]

    assert t.column("email").type == ColumnType.parse("varchar(255)")
    assert t.column("email").nullable is False
    assert t.column("bio").nullable is True
    assert t.column("signup_count").default == "0"

    # bigserial is canonicalised into type + autoincrement (D28 finding 3)
    assert t.column("id").type == ColumnType.parse("bigint")
    assert t.column("id").autoincrement is True

    kinds = {c.kind for c in t.constraints}
    assert ConstraintKind.PRIMARY_KEY in kinds
    assert ConstraintKind.UNIQUE in kinds
    assert ConstraintKind.CHECK in kinds


def test_p03_composite_pk_preserves_order():
    """References are ids (D30), so names come back through the snapshot's resolver."""
    s = pg("CREATE TABLE t (a int, b int, PRIMARY KEY (b, a))")
    pkc = next(c for c in s.table("t").constraints if c.kind == ConstraintKind.PRIMARY_KEY)
    assert s.constraint_column_names(f"t.{pkc.name}") == ["b", "a"]


def test_p04_fk_with_actions():
    s = pg("""CREATE TABLE parent (id int PRIMARY KEY);
              CREATE TABLE child (pid int,
                CONSTRAINT fk_c FOREIGN KEY (pid) REFERENCES parent (id)
                  ON DELETE CASCADE ON UPDATE RESTRICT)""")
    fk = s.constraint("child.fk_c")
    assert s.constraint_column_names("child.fk_c") == ["pid"]
    assert s.constraint_ref("child.fk_c") == ("parent", ["id"])
    assert fk.on_delete.lower() == "cascade"
    assert fk.on_update.lower() == "restrict"


def test_p06_check_expression_captured():
    s = pg("CREATE TABLE t (n int, CONSTRAINT ck CHECK (n >= 0))")
    assert "n" in s.constraint("t.ck").expression


def test_p10_index_order_uniqueness_and_partial():
    s = pg("""CREATE TABLE t (a int, b int, deleted_at timestamptz);
              CREATE UNIQUE INDEX i1 ON t (a);
              CREATE INDEX i2 ON t (b, a DESC);
              CREATE INDEX i3 ON t (a) WHERE deleted_at IS NULL;""")
    assert s.index("t.i1").unique is True
    assert s.index_column_names("t.i2") == ["b", "a"], "order is significant (N-06)"
    assert [c.desc for c in s.index("t.i2").columns] == [False, True]
    assert s.index("t.i3").where is not None


def test_p11_multiple_statements_with_and_without_trailing_semicolon():
    a = pg("CREATE TABLE x (a int); CREATE TABLE y (b int);")
    b = pg("CREATE TABLE x (a int); CREATE TABLE y (b int)")
    assert a == b
    assert {t.name for t in a.tables} == {"x", "y"}


def test_p12_comments_are_ignored():
    plain = pg("CREATE TABLE t (a int)")
    noisy = pg("""
        -- leading comment
        CREATE TABLE t (a int); /* trailing block
           spanning lines */
    """)
    assert plain == noisy


def test_p13_empty_input_is_an_empty_schema_not_an_error():
    assert pg("").tables == ()
    assert pg("   \n  -- just a comment\n").tables == ()


# =================================================== determinism (P-20..P-26)
def test_p20_parsing_is_deterministic():
    sql = "CREATE TABLE t (a varchar(10) NOT NULL DEFAULT 'x', b int)"
    assert pg(sql) == pg(sql)


@pytest.mark.parametrize("a,b", [
    ("varchar(255)", "character varying(255)"),
    ("int", "integer"),
    ("int", "int4"),
    ("bigint", "int8"),
    ("timestamptz", "timestamp with time zone"),
    ("boolean", "bool"),
])
def test_p21_p22_type_spellings_collapse(a, b):
    assert pg(f"CREATE TABLE t (c {a})") == pg(f"CREATE TABLE t (c {b})")


@pytest.mark.parametrize("dialect", ["postgres", "mysql"])
@pytest.mark.parametrize("variant", ["now()", "NOW()", "CURRENT_TIMESTAMP"])
def test_p23_default_expression_normalization(dialect, variant):
    """The highest-floor test in the suite. A diff tool that reports a change on an
    untouched created_at column gets closed and never reopened.

    Free on Postgres, ours to do on MySQL (D28 finding 2)."""
    ts = "timestamptz" if dialect == "postgres" else "datetime"
    base = parse_ddl(f"CREATE TABLE t (a {ts} DEFAULT CURRENT_TIMESTAMP)", dialect=dialect)
    other = parse_ddl(f"CREATE TABLE t (a {ts} DEFAULT {variant})", dialect=dialect)
    assert base == other, f"{variant} must normalize to CURRENT_TIMESTAMP on {dialect}"


def test_p24_p25_whitespace_and_keyword_case_are_irrelevant():
    canonical = pg("CREATE TABLE t (a int NOT NULL, b varchar(3))")
    assert pg("create table t (a INT not null,\n\n   b VarChar(3)\n)") == canonical


# ==================================================== dialect traps (P-30+)
def test_p30_mysql_tinyint1_is_boolean():
    assert my("CREATE TABLE t (a tinyint(1))").col("t.a").type == ColumnType.parse("boolean")


def test_p30b_mysql_boolean_and_bool_are_the_same_type():
    a = my("CREATE TABLE t (a boolean)")
    assert a == my("CREATE TABLE t (a bool)") == my("CREATE TABLE t (a tinyint(1))")


@pytest.mark.parametrize("ty", ["tinyint(1) unsigned", "tinyint(1) zerofill"])
def test_p30c_mysql_tinyint1_variants_are_not_boolean(ty):
    """From 8.0.19 only TINYINT(1) with no UNSIGNED/ZEROFILL carries the bool assumption."""
    assert my(f"CREATE TABLE t (a {ty})").col("t.a").type != ColumnType.parse("boolean")


def test_p30d_mysql_bare_tinyint_is_an_integer():
    assert my("CREATE TABLE t (a tinyint)").col("t.a").type != ColumnType.parse("boolean")


def test_p31_p32_quoting_styles():
    assert my("CREATE TABLE `t` (`a` int)").col("t.a") is not None
    assert pg('CREATE TABLE "t" ("a" int)').col("t.a") is not None


def test_p33_postgres_folds_unquoted_identifiers_to_lower():
    s = pg("CREATE TABLE Users (Email int)")
    assert s.table("users") is not None
    assert s.col("users.email") is not None


def test_p34c_mysql_column_names_are_case_insensitive():
    """Column names are case-insensitive on every platform -- so this is a duplicate."""
    with pytest.raises(DDLError) as e:
        my("CREATE TABLE t (Email int, email int)")
    assert "duplicate" in str(e.value).lower()


def test_p35_p36_autoincrement_is_canonical_across_dialects():
    p = pg("CREATE TABLE t (id bigserial PRIMARY KEY)").col("t.id")
    m = my("CREATE TABLE t (id bigint NOT NULL AUTO_INCREMENT, PRIMARY KEY (id))").col("t.id")
    assert p.autoincrement is m.autoincrement is True
    assert p.type == m.type == ColumnType.parse("bigint")


def test_p38_mysql_unsigned_is_preserved():
    t = my("CREATE TABLE t (a int unsigned)").col("t.a").type
    assert t.unsigned is True
    assert t != ColumnType.parse("int")


def test_p39_mysql_prefix_index_round_trips_without_mangling_the_column():
    """D28 finding 1: sqlglot parses `note(32)` as a function call and re-emits it as
    `NOTE(32)`, corrupting the identifier. The adapter must reconstruct it."""
    s = my("CREATE TABLE t (`note` text, `url` varchar(255), "
           "KEY `i1` (`note`(32)), KEY `i2` (`url`(64), `note`))")
    assert s.index_column_names("t.i1") == ["note"], "column name must NOT be mangled"
    assert s.index("t.i1").columns[0].prefix_length == 32

    assert list(zip(s.index_column_names("t.i2"),
                    [c.prefix_length for c in s.index("t.i2").columns])) == \
        [("url", 64), ("note", None)]


def test_p40_postgres_partial_index_predicate_captured():
    s = pg("CREATE TABLE t (a int, del timestamptz); "
           "CREATE INDEX i ON t (a) WHERE del IS NULL")
    assert "del" in s.index("t.i").where


def test_mysql_table_options_are_ignored_not_rejected():
    """ENGINE/CHARSET are noise for schema versioning, not unsupported constructs."""
    s = my("CREATE TABLE t (a int) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    assert s.table("t") is not None


# ======================================================= malformed (P-50+)
@pytest.mark.parametrize("sql,label", [
    ("CREATE TABLE t (a int", "unbalanced parens"),
    ("this is not sql at all (((", "garbage"),
    ("CREATE TABLE t (a int, b", "truncated"),
])
def test_p50_p52_malformed_input_raises_with_a_line_number(sql, label):
    with pytest.raises(DDLError) as e:
        pg(sql)
    assert e.value.problems[0].line >= 1, label


def test_p53_duplicate_table_name():
    with pytest.raises(DDLError, match="(?i)duplicate"):
        pg("CREATE TABLE t (a int); CREATE TABLE t (b int)")


def test_p54_duplicate_column_name():
    with pytest.raises(DDLError, match="(?i)duplicate"):
        pg("CREATE TABLE t (a int, a int)")


def test_p55_fk_to_missing_table():
    with pytest.raises(DDLError, match="(?i)nope"):
        pg("CREATE TABLE t (a int, CONSTRAINT fk FOREIGN KEY (a) REFERENCES nope (id))")


def test_p56_index_on_missing_column():
    with pytest.raises(DDLError, match="(?i)ghost"):
        pg("CREATE TABLE t (a int); CREATE INDEX i ON t (ghost)")


def test_p57_pk_on_missing_column():
    with pytest.raises(DDLError, match="(?i)ghost"):
        pg("CREATE TABLE t (a int, PRIMARY KEY (ghost))")


def test_p58_line_numbers_are_1_based_and_point_at_the_offending_statement():
    sql = "CREATE TABLE ok1 (a int);\n\nCREATE TABLE ok2 (b int);\n\nCREATE VIEW v AS SELECT 1;\n"
    with pytest.raises(DDLError) as e:
        pg(sql)
    assert e.value.problems[0].line == 5, f"got {e.value.problems[0].line}"


def test_index_on_missing_table():
    with pytest.raises(DDLError, match="(?i)nosuch"):
        pg("CREATE INDEX i ON nosuch (a)")


# ==================================================== unsupported (P-60+)
@pytest.mark.parametrize("sql,needle", [
    ("CREATE VIEW v AS SELECT 1", "view"),
    ("CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW SELECT 1", "trigger"),
    ("CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql", "function"),
    ("CREATE TABLE t (a int, b int GENERATED ALWAYS AS (a*2) STORED)", "generated"),
    ("CREATE TABLE t (a int) PARTITION BY RANGE (a)", "partition"),
])
def test_p60_p64_unsupported_constructs_are_rejected_by_name(sql, needle):
    with pytest.raises(DDLError) as e:
        pg(sql)
    assert needle in str(e.value).lower()


def test_p66_collate_is_rejected_rather_than_silently_dropped():
    with pytest.raises(DDLError, match="(?i)collate"):
        pg("CREATE TABLE t (a varchar(10) COLLATE \"en_US\")")


def test_p67_whole_import_is_rejected_not_partially_applied():
    """The one that matters most in this block. Partial import is worse than none:
    the user gets a schema they believe is complete."""
    with pytest.raises(DDLError):
        pg("CREATE TABLE keep_me (a int); CREATE VIEW v AS SELECT 1;")


def test_p68_rejection_lists_what_is_supported():
    with pytest.raises(DDLError) as e:
        pg("CREATE VIEW v AS SELECT 1")
    assert "supported" in str(e.value).lower()


def test_all_problems_are_reported_not_just_the_first():
    """Fixing errors one round-trip at a time is a miserable experience."""
    with pytest.raises(DDLError) as e:
        pg("CREATE VIEW v1 AS SELECT 1; CREATE VIEW v2 AS SELECT 2;")
    assert len(e.value.problems) == 2


# ------------------------------------------------- D41: the origin constraint
@pytest.mark.parametrize("sql,verb", [
    ("ALTER TABLE users ADD COLUMN age int", "ALTER"),
    ("DROP TABLE users", "DROP"),
    ("TRUNCATE TABLE users", "TRUNCATE"),
])
def test_a_change_script_is_refused_as_a_product_boundary_not_a_missing_feature(sql,
                                                                               verb):
    """D41. The wording matters as much as the rejection.

    A user who reads "unsupported" assumes the feature is coming and waits. A user who
    reads "this tool is the source of truth" knows to import differently. Two
    rejections that look alike but mean different things is a UX bug, so this test
    pins the distinction rather than just the exception type.
    """
    with pytest.raises(DDLError) as e:
        parse_ddl(sql, dialect="postgres")

    text = str(e.value)
    assert "change" in text, "the message must say WHAT KIND of input this is"
    assert "source of truth" in text
    assert "CREATE TABLE statements" in text, "and must name the way in"


def test_an_unimplemented_construct_gets_the_OTHER_message():
    """The counter-case. CREATE VIEW is genuinely not built; it is not a boundary.
    Conflating the two would make the D41 message meaningless."""
    with pytest.raises(DDLError) as e:
        parse_ddl("CREATE VIEW v AS SELECT 1", dialect="postgres")

    text = str(e.value)
    assert "unsupported construct" in text
    assert "source of truth" not in text, \
        "an unbuilt feature must not be dressed up as a deliberate boundary"


def test_a_pg_dump_style_constraint_statement_is_refused_with_the_boundary_message():
    """The concrete case a real user hits: `pg_dump` emits every key as a separate
    ALTER, so pasting a dump fails here. D41 accepts that cost explicitly."""
    dump = (
        "CREATE TABLE users (id bigint NOT NULL, email character varying(255));\n"
        "ALTER TABLE ONLY users ADD CONSTRAINT users_pkey PRIMARY KEY (id);\n"
    )

    with pytest.raises(DDLError) as e:
        parse_ddl(dump, dialect="postgres")

    assert e.value.problems[0].line == 2, "the CREATE TABLE line is fine"
    assert "source of truth" in str(e.value)


def test_an_unparseable_change_script_gets_the_boundary_message_too():
    """`RENAME TABLE` is not even parseable by sqlglot, so it arrives as an opaque
    Command. It must still reach the D41 message -- otherwise the boundary is
    explained or not depending on an implementation detail of the parser."""
    with pytest.raises(DDLError) as e:
        parse_ddl("RENAME TABLE a TO b", dialect="mysql")

    assert "RENAME describes a change" in str(e.value)
    assert "source of truth" in str(e.value)
