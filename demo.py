"""A narrated walkthrough of everything that works today, runnable in one command.

    .venv/bin/python demo.py

There is no web UI yet, so this is the way to watch the engine work end to end:
two engineers change the same table on different branches, the merge resolves what
it can and refuses what it cannot, and the result comes out as SQL for two engines.

Nothing here is a test. The suite proves correctness; this shows behaviour.
"""
import os
import tempfile

from schemavcs.dialects import emit, parse_ddl
from schemavcs.engine import Repo, Resolution, Side, diff, merge_branches
from schemavcs.engine.plan import plan
from schemavcs.model.snapshot import Snapshot
from schemavcs.storage import SqliteStore

DB = os.path.join(tempfile.gettempdir(), "schemavcs-demo.db")


def title(n, text):
    print(f"\n\033[1m{n}. {text}\033[0m\n" + "─" * 72)


def sql(script):
    print("\n".join("    " + line for line in script.text().splitlines()))


START = """
CREATE TABLE users (
    id       bigint PRIMARY KEY,
    email    varchar(255) NOT NULL,
    nickname varchar(64)
);
CREATE TABLE orders (
    id       bigint PRIMARY KEY,
    user_id  bigint NOT NULL,
    total    numeric(10,2) NOT NULL,
    CONSTRAINT orders_user_fk FOREIGN KEY (user_id) REFERENCES users (id)
);
CREATE INDEX orders_by_user ON orders (user_id);
"""

# ---------------------------------------------------------------- 1. ingest
if os.path.exists(DB):
    os.remove(DB)
repo = Repo.init("main", store=SqliteStore(DB))
repo.commit("main", parse_ddl(START, dialect="postgres"), message="initial schema")

title(1, "Ingest: DDL in, identity-carrying snapshot out")
snap = repo.snapshot("main")
for t in snap.tables:
    print(f"    {t.name:8} {', '.join(c.name for c in t.columns)}")
print(f"\n    every object carries a stable id -- users.email is {snap.col('users.email').id[:8]}…")
print(f"    stored at {DB}")

# ------------------------------------------------------- 2. two branches
repo.branch("rename-email", "main")
repo.commit("rename-email",
            repo.snapshot("rename-email").evolve()
                .rename_col("users.email", "contact_email")
                .add_col("users", "verified_at", "timestamptz")
                .build(),
            message="clarify the email column, track verification")

repo.commit("main",
            repo.snapshot("main").evolve()
                .retype_col("users.email", "text")
                .add_index("users", "users_by_nickname", ["nickname"], unique=True)
                .build(),
            message="widen email, enforce unique nicknames")

title(2, "Two engineers, two branches, the same column")
print("    rename-email:  users.email -> users.contact_email,  + verified_at")
print("    main:          users.email retyped varchar(255) -> text,  + unique index")
print("\n    A name-keyed tool sees a DROP and an ADD here and loses the retype.")

# ------------------------------------------------------ 3. the clean merge
deployed = repo.snapshot("main")          # what the live database looks like NOW
result = merge_branches(repo, ours="main", theirs="rename-email")

title(3, "Merge: rename and retype are different attributes, so both survive")
print(f"    status: {result.status.value}")
merged = result.commit.snapshot
col = merged.col("users.contact_email")
print(f"    users.contact_email is {col.type.render()}  <- the rename AND the retype applied")
print(f"    columns now: {', '.join(c.name for c in merged.table('users').columns)}")

# ------------------------------------------------------ 4. the migration
title(4, "Migration: from what is DEPLOYED to the merged result")
for c in diff(deployed, merged).changes:
    print(f"    {c.kind.value:16} {c.table}.{c.name or ''}")
p = plan(deployed, merged)
print(f"\n    {len(p)} operations, worst safety: {p.worst_safety.value}\n")
print("    -- postgres --")
sql(emit(p, "postgres"))
print("\n    -- mysql (same plan, different engine) --")
try:
    sql(emit(p, "mysql"))
except Exception as e:
    print(f"    refused: {e}")

# ------------------------------------------------------ 5. a real conflict
repo.branch("team-a", "main")
repo.branch("team-b", "main")
repo.commit("team-a", repo.snapshot("team-a").evolve()
            .retype_col("users.nickname", "varchar(128)").build(), message="widen nickname")
repo.commit("team-b", repo.snapshot("team-b").evolve()
            .retype_col("users.nickname", "text").build(), message="nickname unbounded")

title(5, "Conflict: same attribute, both sides moved, no correct answer")
bad = merge_branches(repo, ours="team-a", theirs="team-b")
print(f"    status: {bad.status.value}")
for c in bad.conflicts:
    print(f"    [{c.category.value}] {c.path}.{c.attribute}")
    print(f"        base={c.base}  ours={c.ours}  theirs={c.theirs}")
print("\n    Nothing was written. The branch head is unchanged until a human decides.")

fixed = merge_branches(repo, ours="team-a", theirs="team-b",
                       resolutions={bad.conflicts[0].key: Resolution.theirs()},
                       token=bad.token)
print(f"\n    resolved as THEIRS -> {fixed.status.value}, "
      f"nickname is {fixed.commit.snapshot.col('users.nickname').type.render()}")

# --------------------------------------------- 6. an integrity violation
title(6, "Integrity: every side is fine, the combination is not")
repo.branch("drop-users", "main")
repo.branch("add-sessions", "main")
repo.commit("drop-users", repo.snapshot("drop-users").evolve()
            .drop_constraint("orders.orders_user_fk")
            .drop_table("users").build(), message="users moved to another service")
repo.commit("add-sessions", repo.snapshot("add-sessions").evolve()
            .add_table("sessions")
            .add_col("sessions", "id", "bigint", nullable=False)
            .add_col("sessions", "user_id", "bigint", nullable=False)
            .add_fk("sessions", "sessions_user_fk", ["user_id"], "users", ["id"])
            .build(), message="track sessions")
print("    ours:   + sessions, with an FK to users")
print("    theirs: drop users entirely")
print("    No single object was touched by both sides -- there is nothing to conflict on.\n")
bad2 = merge_branches(repo, ours="add-sessions", theirs="drop-users")
print(f"    status: {bad2.status.value}")
for v in bad2.violations:
    print(f"    [{v.invariant}] {v.message}")
print("\n    Caught by validating the merged snapshot as a whole, not object by object.")

# ------------------------------------------------------- 7. persistence
title(7, "Persistence: the repo outlives the process")
reopened = Repo.open(SqliteStore(DB))
print(f"    reopened {DB}")
print(f"    branches: {', '.join(reopened.branches())}")
print(f"    commits:  {reopened.commit_count()}")
for c in reopened.history("main"):
    print(f"      {c.id[:8]}  {c.message}")

# --------------------------------------------------------- 8. the boundary
title(8, "The boundary: change scripts are refused on purpose (D41)")
try:
    parse_ddl("ALTER TABLE users ADD COLUMN phone varchar(32);", dialect="postgres")
except Exception as e:
    print("    " + str(e).replace("\n", "\n    "))

# ---------------------------------------------- 9. against a real server
title(9, "Optional: run the generated SQL on a real Postgres")
pg = os.environ.get("SCHEMAVCS_PG_URL")
if not pg:
    print("    skipped -- set SCHEMAVCS_PG_URL to a throwaway database to run this.")
    print("    See the README for a two-line disposable server.")
else:
    import psycopg
    with psycopg.connect(pg, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS demo CASCADE; CREATE SCHEMA demo; SET search_path=demo")
        with conn.cursor() as cur:
            cur.execute("SET search_path=demo")
            for stmt in emit(plan(Snapshot(dialect=deployed.dialect), deployed), "postgres").statements:
                cur.execute(stmt)
            print(f"    built the deployed schema on {pg}")
            for stmt in emit(p, "postgres").statements:
                cur.execute(stmt)
            print("    applied the merge migration -- the server accepted every statement")
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='demo' AND table_name='users' ORDER BY ordinal_position")
            print(f"    users is now: {', '.join(r[0] for r in cur.fetchall())}")

print(f"\n\033[1mdone.\033[0m  demo database left at {DB}\n")
