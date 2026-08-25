# schemavcs — version control for database schemas

Branch, diff, and merge **database schemas** the way git handles source. Row data is out
of scope: the artifact under version control is the schema itself.

The interesting claim is that a **rename is not a drop plus an add**. Every schema object
carries a stable UUID, so renaming a column is `same id, new name` — which means one
engineer can rename `users.email` while another retypes it, and the two changes merge
cleanly. A name-keyed tool cannot express that at all.

## Quick start

```bash
make dev      # creates .venv, installs the package and dev dependencies
make test     # 422 tests, ~3 seconds, no database required
make serve    # the web app on http://localhost:8000
```

That's it. `make test` needs no database, no Docker, and no configuration — which is the
only reason a suite this size actually gets run on every save.

> If your machine has a private `extra-index-url` in `~/.config/pip/pip.conf`, the
> Makefile already works around it: installs pin public PyPI explicitly, so a stranger's
> pip config can't break setup.

## See it work

```bash
make serve      # then open http://localhost:8000
```

Opening it drops you straight into a workspace that already contains the scenario: a
small schema plus four branches holding two engineers' divergent changes, so there is
something to merge on arrival. A five-step rail across the top is the navigation and the
explanation — **the schema**, **two engineers diverge**, **a diff that understands
renames**, **merge and a real conflict**, **get the SQL** — each step linking to the
exact comparison worth looking at.

Every edit is a commit; there is no save button. `/start` takes your own DDL instead.
Each visitor gets an isolated workspace, and nothing is authenticated — see D42.

For the same story without a browser:

```bash
make demo
```

A narrated end-to-end walkthrough: two engineers change the same table on different
branches, the merge keeps both changes, a real conflict is refused and then resolved, an
integrity violation is caught that no single-object check could see, and the result comes
out as Postgres and MySQL migrations. The repo is reopened from disk at the end to show
it persisted.

Point it at a throwaway server and the last step applies the generated migration for real:

```bash
SCHEMAVCS_PG_URL=postgresql://postgres@127.0.0.1:15432/schemavcs_demo make demo
```

## What works today

```python
from schemavcs.dialects import parse_ddl
from schemavcs.engine import Repo, merge_branches, diff
from schemavcs.engine.plan import plan
from schemavcs.dialects import emit
from schemavcs.storage import SqliteStore

repo = Repo.init("main", store=SqliteStore("schemas.db"))
repo.commit("main", parse_ddl("CREATE TABLE users (id bigint PRIMARY KEY, email varchar(255))",
                              dialect="postgres"))

repo.branch("rename-email", "main")
repo.commit("rename-email", repo.snapshot("main").evolve()
            .rename_col("users.email", "contact").build())
repo.commit("main", repo.snapshot("main").evolve()
            .retype_col("users.email", "text").build())

deployed = repo.snapshot("main")           # what the live database looks like now
result = merge_branches(repo, ours="main", theirs="rename-email")
print(result.status)                       # MergeStatus.MERGED — both changes applied

# The migration is the diff from what is deployed to the merged result, not a
# rendering of the merge itself.
script = emit(plan(deployed, result.commit.snapshot), "postgres")
print(script.text())
# BEGIN;
# ALTER TABLE "users" RENAME COLUMN "email" TO "contact";
# COMMIT;
```

| Area | State |
|---|---|
| Identity-based model, diff, three-way merge | done |
| Merge base as a true LCA over the commit DAG | done |
| Five-category conflict taxonomy + global integrity validation | done |
| Postgres & MySQL ingest (`CREATE TABLE` / `CREATE INDEX`) | done |
| Migration planning: ordering, FK cycles, rename swaps, safety classification | done |
| Postgres & MySQL emitters, verified against real servers | done |
| Durable storage (SQLite / libSQL) | done |
| Web app: branch, edit, diff, merge, resolve conflicts, generate SQL | done |
| `ALTER TABLE` ingest, migration-history import | **deliberately out of scope** — see below |
| Rename inference, rollback generation | out of scope for now |

See [`decisions.md`](decisions.md) for why, including what was deliberately cut.

### The schema starts here

This tool is the **source of truth** for the schema. It reads schema *definitions* —
`CREATE TABLE` / `CREATE INDEX`, or the structured editor — and refuses change scripts
(`ALTER`, `DROP`, `RENAME`). There is no path that imports an existing `migrations/`
folder, and `pg_dump --schema-only` output will not load, because `pg_dump` emits every
key as a separate `ALTER TABLE ONLY ... ADD CONSTRAINT`.

That is a product boundary, not a missing parser. The whole design rests on every object
owning an identity assigned once at creation; a history we did not witness cannot tell a
rename apart from a drop-plus-add, so importing one would mean *guessing* identity — the
one thing this tool exists not to do. Full reasoning, including what it costs, is
[D41](decisions.md).

Positioning, plainly: **greenfield-first**. Design a schema here, evolve it here, let the
tool generate the migrations. It is not an adoption path for a legacy database.

## Deploying

The app is a single container with no database to provision — schemas live in per-visitor
SQLite files created at runtime.

```bash
docker build -t schemavcs .
docker run -p 8000:8000 schemavcs      # http://localhost:8000
```

On [Render](https://render.com), `render.yaml` is a blueprint: point a new Blueprint
instance at the repo and it reads the service definition from the file rather than from a
sequence of dashboard clicks. The free plan needs no credit card and allows 750
instance-hours a month, which one always-on service fits inside.

Two properties of the free tier shaped the setup, and neither is hidden:

**The first request after idle is slow.** Free services are suspended after 15 minutes of
inactivity and take about a minute to wake. There is no way around this that does not
involve self-pinging, which Render treats as abnormal traffic and grounds for suspension —
a dead link is worse than a slow one. So it is documented instead.

**What is not preserved.** Free instances cannot mount a persistent disk, so `/data` is
container-local and is lost on redeploy or spin-down. Within the life of an instance
everything holds: every edit is a commit written to disk immediately (D44), the repo is
reopened per request, and branches, merges and history survive across requests and across
workers. What does not survive is the container. A returning visitor whose workspace is
gone lands on a fresh demo rather than an error.

That is a fit with the design rather than a compromise forced on it — workspaces are
anonymous, per-visitor and throwaway by decision (D42), so there is nothing here whose loss
matters. It is also why no managed database is provisioned: a free Postgres would expire 30
days after creation, which is precisely the wrong property for a link someone else opens.

`/healthz` exists for the platform's health check because `/` is not idempotent — it mints
a workspace and sets a cookie, so probing it would write a SQLite file per check.

## Running the live-engine tests

These apply generated DDL to a real Postgres and MySQL. They're skipped by default; the
skip message names the environment variable to set, so a silent skip can't be mistaken
for a pass.

```bash
.venv/bin/pip install -e ".[engines]"

export SCHEMAVCS_PG_URL="postgres://postgres@127.0.0.1:5432/schemavcs_test"
export SCHEMAVCS_MYSQL_URL="mysql://root@127.0.0.1:3306/schemavcs_test"
make test-engine
```

CI runs them against `postgres:16` and `mysql:8.0` service containers — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). MySQL is pinned to 8.0 on purpose:
`lower_case_table_names` can only be set at server initialization and the Linux default
differs from the macOS one.

<details>
<summary>Throwaway local servers, if you'd rather not use Docker</summary>

```bash
# Postgres — isolated data directory, non-standard port, nothing else touched
initdb -D /tmp/svcs-pg -U postgres --auth=trust
pg_ctl -D /tmp/svcs-pg -o "-p 15432 -k /tmp" -l /tmp/svcs-pg.log start
createdb -h 127.0.0.1 -p 15432 -U postgres schemavcs_test

# MySQL — note the short socket path; MySQL rejects paths over 103 characters
mysqld --initialize-insecure --datadir=/tmp/svcs-my
mysqld --datadir=/tmp/svcs-my --port=13306 --socket=/tmp/svcs.sock --mysqlx=0 &

export SCHEMAVCS_PG_URL="postgres://postgres@127.0.0.1:15432/schemavcs_test"
export SCHEMAVCS_MYSQL_URL="mysql://root@127.0.0.1:13306/schemavcs_test"
make test-engine

# tear down
pg_ctl -D /tmp/svcs-pg stop && mysqladmin --protocol=TCP -P 13306 -u root shutdown
```
</details>

## Layout

```
src/schemavcs/
  model/      canonical schema objects, types, immutable snapshots, the fixture DSL
  engine/     diff, merge, merge base, migration planning, the commit DAG
              -- dialect-neutral, enforced by tests/unit/test_architecture.py
  dialects/   Postgres and MySQL: parsing in, SQL out. The only dialect-aware code.
  storage/    durable implementations of the engine's Store contract
  web/        FastAPI + Jinja, no build step, no external assets.
              Decides nothing (D45); tokenised design system (D47).
tests/
  unit/       335 tests, no database
  web/        77 tests: the HTTP layer (cookies, forms, redirects, staleness,
              response headers) and the stylesheet's contrast contract
  engine/     generated DDL applied to real Postgres and MySQL
docs/         scope, and the full test plan behind the test IDs (M-01, E-40, ...)
```

The load-bearing rule is that `model/` and `engine/` never learn what a dialect is.
That's a test, not a comment — `test_architecture.py` fails the build if the word
"postgres" or "mysql" appears in either package's code. It has caught three real leaks
so far, one of which I'd have argued was fine.

## Reading the tests

Test names carry the IDs from [`docs/test-plan.md`](docs/test-plan.md). A few worth
opening first:

- `test_merge.py::test_M01_...` — a rename and a retype on the same column, merging
  clean. The product thesis in one test.
- `test_merge.py::test_M80_...` — two changes that are pairwise clean and jointly
  invalid. The reason global validation exists.
- `test_plan.py::test_E40_...` — a rename swap. Valid start, valid end, no valid order
  in between. Fails on every naive emitter.
- `test_merge_base.py::test_l06_...` — why the merge base is a computed LCA and not the
  branch point.
