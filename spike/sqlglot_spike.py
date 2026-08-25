"""Day-1 gate (see docs/test-plan.md, TDD step 2).

Question is NOT "does sqlglot parse SQL" - that's documented. It is:
can we extract tables/columns/types/constraints/indexes with enough
fidelity to build the canonical model on top, for BOTH dialects?

Prints findings; exits non-zero if a HARD requirement fails.
"""
import sqlglot
from sqlglot import exp

hard_fail = []
soft = []

def hdr(t): print(f"\n{'='*72}\n{t}\n{'='*72}")

# ---------------------------------------------------------------- 1. PG DDL
hdr("1. POSTGRES: CREATE TABLE fidelity (P-01)")
PG = """
CREATE TABLE users (
    id            bigserial PRIMARY KEY,
    email         varchar(255) NOT NULL,
    is_active     boolean DEFAULT true,
    signup_count  integer NOT NULL DEFAULT 0,
    created_at    timestamptz DEFAULT now(),
    bio           text,
    CONSTRAINT uq_users_email UNIQUE (email),
    CONSTRAINT ck_users_count CHECK (signup_count >= 0)
);
"""
t = sqlglot.parse_one(PG, dialect="postgres")
print("top-level node:", type(t).__name__)
schema_expr = t.this
print("kind:", t.args.get("kind"))

cols, tbl_constraints = [], []
for d in schema_expr.expressions:
    if isinstance(d, exp.ColumnDef):
        ctype = d.args.get("kind")
        constraints = [type(c.args.get("kind")).__name__ for c in d.constraints]
        cols.append((d.name, ctype.sql(dialect="postgres") if ctype else None, constraints))
    else:
        tbl_constraints.append((type(d).__name__, d.sql(dialect="postgres")))

print(f"\ncolumns ({len(cols)}):")
for n, ty, cs in cols:
    print(f"   {n:14} {str(ty):16} {cs}")
print(f"\ntable-level constraints ({len(tbl_constraints)}):")
for k, s in tbl_constraints:
    print(f"   {k:22} {s}")

if len(cols) != 6:
    hard_fail.append(f"PG: expected 6 columns, got {len(cols)}")
if not tbl_constraints:
    hard_fail.append("PG: table-level UNIQUE/CHECK constraints not surfaced")

# --- can we reach into a DEFAULT and a CHECK expression?
default_col = next(d for d in schema_expr.expressions
                   if isinstance(d, exp.ColumnDef) and d.name == "created_at")
dc = [c for c in default_col.constraints if isinstance(c.args.get("kind"), exp.DefaultColumnConstraint)]
print("\ncreated_at DEFAULT ->", dc[0].args["kind"].this.sql() if dc else "NOT FOUND")
if not dc:
    hard_fail.append("PG: cannot reach DEFAULT expression")

# ---------------------------------------------------------------- 2. defaults
hdr("2. DEFAULT expression normalization (P-23) -- the highest-floor test")
variants = {
    "postgres": ["now()", "CURRENT_TIMESTAMP", "NOW()"],
    "mysql":    ["now()", "CURRENT_TIMESTAMP", "NOW()"],
}
for dialect, vs in variants.items():
    print(f"\n{dialect}:")
    seen = {}
    for v in vs:
        ddl = f"CREATE TABLE t (a timestamp DEFAULT {v})"
        cd = sqlglot.parse_one(ddl, dialect=dialect).this.expressions[0]
        d = [c for c in cd.constraints if isinstance(c.args.get("kind"), exp.DefaultColumnConstraint)][0]
        node = d.args["kind"].this
        key = (type(node).__name__, node.sql(dialect=dialect).upper())
        seen[v] = key
        print(f"   {v:20} -> {type(node).__name__:22} sql={node.sql(dialect=dialect)!r}")
    if len(set(seen.values())) == 1:
        print("   => all three collapse to ONE node. normalization is FREE.")
    else:
        soft.append(f"{dialect}: now()/CURRENT_TIMESTAMP differ -> we must normalize (expected; P-23 is ours)")
        print("   => they DIFFER. we own the normalization rule (expected).")

# ---------------------------------------------------------------- 3. types
hdr("3. Type extraction + comparability (P-21/P-22, D6)")
pairs = [("postgres", "varchar(255)"), ("postgres", "character varying(255)"),
         ("postgres", "int"), ("postgres", "integer"), ("postgres", "int4"),
         ("postgres", "timestamptz"), ("postgres", "text"),
         ("mysql", "tinyint(1)"), ("mysql", "boolean"), ("mysql", "bool"),
         ("mysql", "int unsigned"), ("mysql", "text"), ("mysql", "datetime")]
for d, ty in pairs:
    cd = sqlglot.parse_one(f"CREATE TABLE t (a {ty})", dialect=d).this.expressions[0]
    k = cd.args["kind"]
    print(f"   {d:9} {ty:24} -> this={str(k.this):28} args={ {kk:vv for kk,vv in k.args.items() if kk!='this'} }")

# ---------------------------------------------------------------- 4. indexes
hdr("4. Indexes: column order, uniqueness, partial, prefix (N-06, P-39/P-40)")
idx_cases = [
    ("postgres", "CREATE UNIQUE INDEX idx_a ON users (email)"),
    ("postgres", "CREATE INDEX idx_b ON users (tenant_id, created_at DESC)"),
    ("postgres", "CREATE INDEX idx_c ON users (email) WHERE deleted_at IS NULL"),
    ("mysql",    "CREATE INDEX idx_d ON users (url(64))"),
]
for d, sql in idx_cases:
    n = sqlglot.parse_one(sql, dialect=d)
    print(f"\n   [{d}] {sql}")
    print(f"      node={type(n).__name__} unique={n.args.get('unique')}")
    params = n.args.get("params")
    if params:
        cols_ = params.args.get("columns")
        print(f"      columns={[c.sql(dialect=d) for c in cols_] if cols_ else None}")
        print(f"      where={params.args.get('where').sql(dialect=d) if params.args.get('where') else None}")
    else:
        soft.append(f"{d}: index params shape unexpected for: {sql}")
        print(f"      args keys={list(n.args)}")

# ---------------------------------------------------------------- 5. mysql
hdr("5. MySQL DDL specifics (P-30..P-39)")
MY = """
CREATE TABLE `orders` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int unsigned NOT NULL,
  `is_paid` tinyint(1) DEFAULT '0',
  `note` text,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_orders_user` (`user_id`),
  KEY `idx_note` (`note`(32)),
  CONSTRAINT `fk_orders_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""
try:
    m = sqlglot.parse_one(MY, dialect="mysql")
    defs = m.this.expressions
    print(f"   parsed OK, {len(defs)} definitions")
    for d in defs:
        if isinstance(d, exp.ColumnDef):
            print(f"      COL  {d.name:12} {d.args['kind'].sql(dialect='mysql'):18} "
                  f"{[type(c.args.get('kind')).__name__ for c in d.constraints]}")
        else:
            print(f"      {type(d).__name__:24} {d.sql(dialect='mysql')[:70]}")
    print(f"   table props: {[type(p).__name__ for p in (m.args.get('properties').expressions if m.args.get('properties') else [])]}")
except Exception as e:
    hard_fail.append(f"MySQL real-world DDL failed to parse: {e}")
    print("   FAILED:", e)

# ---------------------------------------------------------------- 6. errors
hdr("6. Error detection (P-50/P-51) + unsupported constructs (P-60+)")
bad = [("unbalanced parens", "CREATE TABLE t (a int"),
       ("garbage",           "this is not sql at all ((("),
       ("truncated",         "CREATE TABLE t (a int, b")]
for label, sql in bad:
    try:
        sqlglot.parse_one(sql, dialect="postgres", error_level=sqlglot.ErrorLevel.RAISE)
        print(f"   {label:20} -> NO ERROR RAISED  <-- we must detect this ourselves")
        soft.append(f"parser did not raise on: {label}")
    except Exception as e:
        msg = str(e).splitlines()[0][:90]
        has_line = ("line" in str(e).lower()) or ("Line" in str(e))
        print(f"   {label:20} -> {type(e).__name__}: {msg}  [line info: {has_line}]")

print()
for label, sql in [("CREATE VIEW", "CREATE VIEW v AS SELECT 1"),
                   ("CREATE TRIGGER", "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW SET @x=1"),
                   ("GENERATED col", "CREATE TABLE t (a int, b int GENERATED ALWAYS AS (a*2) STORED)"),
                   ("PARTITION BY", "CREATE TABLE t (a int) PARTITION BY RANGE (a)")]:
    try:
        n = sqlglot.parse_one(sql, dialect="postgres")
        print(f"   {label:16} -> parses as {type(n).__name__:14} kind={n.args.get('kind')!r} "
              f"=> identifiable, we can reject it")
    except Exception as e:
        print(f"   {label:16} -> raises {type(e).__name__} => also fine (reject path)")

# ---------------------------------------------------------------- 7. verdict
hdr("VERDICT")
if hard_fail:
    print("GATE FAILED - hard requirements unmet:")
    for f in hard_fail: print("   X", f)
else:
    print("GATE PASSED - structured extraction works for both dialects.")
if soft:
    print("\nnotes (work we own, not blockers):")
    for s in soft: print("   -", s)
raise SystemExit(1 if hard_fail else 0)
