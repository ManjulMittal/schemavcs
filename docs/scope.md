# Scope & Architecture

Companion to [`../decisions.md`](../decisions.md). That file records *why* each call was
made; this one states *what* is being built and where the boundaries are.

---

## Problem framing

> Git can merge your migration files. It cannot tell you your schema is broken.

Two engineers branch off `main`. One renames `email` to `email_address`. The other widens
that column's type. Git either reports a textual conflict on adjacent lines — noise, since
the edits are compatible — or merges cleanly and produces a schema that is wrong. No
line-oriented VCS can know those two edits touch **the same column**, because its atom is
a line of text and the thing that actually changed is an object.

This builds a version control system whose atom is a **schema object**.

### Who it's for

A team of 3–15 engineers sharing one database, where schema conflicts are currently
resolved by Slack message and hope. Concretely, the recurring failure it targets: two
migrations authored in parallel, merged without complaint, discovered broken on deploy.

### What success looks like

A user can branch a schema, evolve it independently, see exactly what diverged in terms
of *objects and their attributes* rather than text, and merge back — with automatic
resolution where the changes are provably compatible, explicit conflicts where they
aren't, and an ordered migration that actually executes.

---

## In scope

**Object types under version control**

- Tables — create, drop, rename
- Columns — add, drop, rename, retype, nullability, defaults
- Constraints — primary key, foreign key, unique, check
- Indexes — including uniqueness and ordered column lists

**Dialects.** Postgres and MySQL. Ingest and emit for both; the core is neutral.

**Operations.** Branch, commit, diff, three-way merge, migration generation (forward
only — rollback was cut, D37).

**Where a schema comes from.** `CREATE TABLE` / `CREATE INDEX` statements, or the
structured editor. Nothing else. See the boundary below.

## Explicitly out of scope

Row data (per the brief). Views, triggers, stored procedures, partitions, row-level
security, sequences-as-objects, collation and charset. Rebase, cherry-pick, revert, tags.
Auth and permissions. Live production database introspection. Real-time collaboration.
Cross-dialect migration as a workflow. Rollback generation (D37).

These are boundaries, not gaps — each has a recorded reason in `decisions.md`.

### The load-bearing one: the schema originates here (D41)

The tool reads schema **definitions** and refuses schema **change scripts** — `ALTER`,
`DROP`, `RENAME`, `TRUNCATE`. So there is no import path for an existing `migrations/`
folder, and `pg_dump --schema-only` output does not load, since `pg_dump` emits every
primary key, foreign key and unique constraint as a separate `ALTER TABLE ONLY`.

Worth separating from the rest of this list, because it is the only boundary that
constrains *who can use the tool at all* rather than what the tool can express. The
reason is identity, not parsing: a history we did not witness cannot distinguish a
rename from a drop-plus-add, so importing one means guessing identity — which is the one
thing the whole design exists not to do.

Positioning that follows: **greenfield-first**. Design a schema here, evolve it here,
generate the migrations from here. Not an adoption path for a legacy database.

---

## Architecture

```
   DDL text ──[parser: pg | mysql]──┐
                                    │
                                    ├──> canonical schema model
   structured editor ───────────────┘     (dialect-neutral, UUID identity)
                                                    │
                                                    │  branch
                                                    │  diff
                                                    │  three-way merge
                                                    │  integrity validation
                                                    ▼
                                            merged snapshot
                                                    │
                        ┌───[emitter: pg]───────────┼──────────[emitter: mysql]───┐
                        ▼                                                         ▼
                ordered PG migration                                 ordered MySQL migration
                (transactional)                                      (stepwise, no-rollback
                                                                      markers)
```

**The load-bearing invariant.** The merge engine never learns what a dialect is. If
`if dialect == ...` appears inside diff, merge, or validation, the design has failed.
Dialect knowledge lives only in parsers and emitters.

### Layers

| Layer | Responsibility | Dialect-aware? |
|---|---|---|
| Parsers | DDL text → canonical model; rename inference on import | Yes |
| Canonical model | Object graph with stable UUIDs; type lattice | No |
| Version store | Commits as snapshots, branches, DAG | No |
| Merge engine | LCA, attribute-level three-way merge, conflict taxonomy, post-merge validation | No |
| Emitters | Merged snapshot → ordered, safety-classified DDL | Yes |

---

## The merge model

### Identity

Every object carries a UUID assigned at creation and preserved for life. A rename is
`same id, new name` — a one-line diff rule. Without this, renames are indistinguishable
from drop-plus-add, and every downstream consumer inherits that error.

### Attribute-level reconciliation

A column is a bag of scalars: `name`, `type`, `nullable`, `default`. Merge reconciles each
independently against the merge base. Rename on one branch plus retype on the other →
disjoint attributes → clean automatic merge. **This is the case the product exists for.**

Ordered column lists (index columns, PK columns, FK column pairs) are the exception:
treated as atomic values, never element-wise merged. Index column order determines whether
an index is usable, so silently interleaving two engineers' orderings would produce an
index neither designed and present it as success.

### Conflict taxonomy — a closed set

| # | Situation | Category | Scope |
|---|---|---|---|
| 1 | In base, deleted in ours, modified in theirs | delete/modify | pairwise |
| 2 | Absent in base, added in both, same name, different UUIDs | name collision | pairwise |
| 3 | Same object, same attribute, divergent values | attribute conflict | pairwise |
| 4 | Same object, *different* attributes changed | **auto-merge** | pairwise |
| 5 | Merged result violates an invariant | integrity violation | global |

A sixth category appearing during the build is evidence of a modeling error, not a reason
to add a case.

Category 5 runs on the merged snapshot and is load-bearing, not garnish. The proof: A
renames `email` → `contact`, B adds a new column named `contact`. Pairwise merge sees
**no conflict** — different UUIDs, disjoint attributes, nothing overlaps. Only global
validation catches the duplicate name. Invariants checked: every FK resolves to an
existing table and uniquely-constrained columns, every index references live columns, PK
columns are non-nullable, no duplicate names within a scope.

### Merge base

Computed as a lowest common ancestor over the commit DAG at merge time — **not** read
from a stored branch point.

The stored branch point is correct exactly once. After `main` has been merged into
`feature`, the correct base for the next merge has advanced to the commit `main`'s tip
pointed at when that merge happened — *not* the merge commit itself, and not the original
branch point. Use the stored branch point and every already-resolved conflict returns on
every subsequent merge, forever.

Worked through, since this is easy to get wrong: `main = A→B→C`, `feature` branches at `B`
to `D`, merge `main`→`feature` creates `M(D, C)`, then `E` lands on `main`. Common
ancestors of `E` and `M` are `{A, B, C}`; the lowest is `C`. A merge commit *can* be the
LCA once branches have merged each other in both directions, which is why the intuition
"the base becomes the merge commit" sounds right and isn't.

Multiple candidate LCAs (criss-cross histories, where two branches have merged each
other) are **detected and refused** with an explanation, not approximated. Recursive merge
is correct and out of reach in five days; picking a candidate arbitrarily would put a
subtly wrong result behind a confident UI.

---

## Migration generation

**The output is `diff(target_head, merged_result)`** — not a rendering of the merge. The
merge produces a schema; the migration is the difference between the target branch's
current state and that schema. Conflating them yields output that is wrong in a way that
looks right.

**Ordering.** Topological sort: tables before FKs referencing them, FKs dropped before
their tables, columns before indexes on them. Circular FKs (`users.org_id` ↔
`orgs.owner_id`) are broken by phasing into create-tables then add-constraints.

**Intermediate-state collisions.** Before emitting, check whether any step leaves the
schema with a duplicate name; if so, route through a temporary name. The canonical case is
a rename swap (`x` → `y`, `y` → `x`): start valid, end valid, and every naive emitter
fails partway through because the intermediate state isn't.

**Safety classification.** Every operation is `safe`, `lossy`, or `lock-heavy`. Lossy
operations — narrowing a `varchar`, `int` → `smallint`, dropping a column — require
explicit acknowledgment before merge.

**Engine honesty.** Postgres plans are transactional. MySQL DDL is not, so MySQL plans are
emitted as individually-safe steps annotated with the point past which failure cannot be
rolled back. Presenting a MySQL plan as atomic would be an actively dangerous lie.

**Rollback.** Down-migrations are generated. Normally intractable — reversing a column drop
loses data — but row data is out of scope, so the artifact is purely structural and a
dropped column is fully recoverable *as schema*. A constraint from the brief made a
normally-hard feature tractable.

Bounded honestly, though: expressible is not the same as executable. Re-adding a dropped
`NOT NULL` column with no default fails against a table that has rows, and row data being
out of scope for *versioning* doesn't make the target database empty. Down-migrations are
therefore classified for executability, and the ones that can't run against a populated
table are flagged rather than presented as a working undo.

---

## Type system

The canonical type model is a **superset** with per-dialect representability declarations,
not the intersection of what all engines support. The intersection excludes Postgres
partial indexes, arrays, and `TIMESTAMPTZ`, and MySQL unsigned integers and prefix
indexes — most of a real production schema.

"Unrepresentable" is therefore a first-class result. MySQL `UNSIGNED INT` emitted to
Postgres becomes `integer` plus `CHECK (col >= 0)`, **flagged as an approximation** rather
than silently downgraded.

Where the real difficulty lives:

| Case | Why it's hard |
|---|---|
| MySQL accepts `BOOLEAN` but stores `TINYINT(1)`, which doesn't constrain values to 0/1 | Round-trip must not silently retype the column. Display width was deprecated in 8.0.17; from 8.0.19 only `TINYINT(1)` with no `UNSIGNED`/`ZEROFILL` carries the boolean assumption |
| `TEXT` | Unbounded in Postgres, capped at 65,535 bytes in MySQL. Same keyword, different type. |
| `DATETIME` / `TIMESTAMP` / `TIMESTAMPTZ` | Materially different timezone semantics per engine |
| MySQL prefix indexes `KEY(url(64))` | Postgres has *an* equivalent — an expression index on `LEFT(url, 64)` — but the planner only uses it when the query repeats that exact expression, so it's an approximation with a behavioural caveat, not a clean translation |
| `varchar(255)` → `varchar(300)` | Safe widen or a change? Needs a partial order, not equality |
| `DEFAULT now()` vs `CURRENT_TIMESTAMP` vs `NOW()` | Must normalize *expressions*, not just literals |

That last row is the least glamorous item in the build and has the highest floor: a diff
tool that reports spurious changes gets closed and never reopened. Re-importing an
unchanged schema must produce an empty diff.

---

## Ingest

Two paths.

**Structured editor** — exactly the operations the brief enumerates (add/drop/rename/
retype column, constraints, indexes, create/drop table), each an explicit typed
operation. Identity is preserved for free. No visual canvas, no drag-and-drop.

**Paste DDL** — identity must be *reconstructed* by matching against the previous
snapshot. Name matching handles the easy cases. Where a name disappeared and a new one
appeared, the tool proposes a confidence-scored rename and asks for confirmation, because
the two readings diverge sharply: a rename preserves history and is safe, a drop-plus-add
destroys both. Hardest sub-case is rename *and* retype in one commit, where neither name
nor type anchors the match — proposed at low confidence from ordinal position, clearly
marked as a guess.

**Unsupported constructs are rejected loudly, with line numbers.** Silent misparse is the
worst possible outcome here: a quietly dropped `CHECK` constraint makes every subsequent
diff, merge, and migration wrong with no signal to the user. Refusing an import is
recoverable; importing something subtly incorrect is not.

---

## Testing strategy

**The round-trip property test is the highest-value test in the repo.** Apply a generated
migration to a throwaway schema in a real engine, re-introspect it, assert the result
equals the snapshot we intended. One assertion catches emitter bugs, ordering bugs, and
type-normalization bugs, and it runs against both Postgres and MySQL in CI.

Beyond that:

- Merge engine as table-driven tests over the five-category taxonomy — pure functions, no
  I/O, so this is both the cheapest and most valuable unit surface
- LCA correctness on hand-built DAGs, including the merge-twice regression: resolve a
  conflict, merge again, assert it does not reappear
- Rename-swap emission: assert the generated DDL executes, which it doesn't without temp
  names
- FK cycle phasing: assert a mutually-referencing pair produces an executable plan
- Idempotent re-import: import an unchanged schema, assert an empty diff
- Malformed and unsupported DDL: assert specific line-numbered errors, never a silent skip

---

## Deployment

| Layer | Choice | Note |
|---|---|---|
| App + API | Serverless Python | sqlglot is dependency-free and small; requests are millisecond-scale compute. Cold start is seconds rather than tens of seconds — estimated, not measured. Vercel Hobby: 60s function timeout, 1M invocations, 4 CPU-hours/month, non-commercial use only. |
| Store | Turso (managed libSQL) | Free, persistent, reachable from serverless. Same SQLite engine locally, so no dev/prod drift. |
| Verification | GitHub Actions + `postgres`/`mysql` service containers | The only place real engines run. |
| Local | SQLite file; Docker Compose only for round-trip tests | The app itself needs no services to run locally. |

All features are usable on the free tier because nothing in the product requires a
database engine, a background worker, or a persistent process — a consequence of moving
live execution into CI.

---

## Five-day plan

| Day | Work | Risk |
|---|---|---|
| 1 | sqlglot spike (gate) → canonical model, UUID identity, commit/branch/DAG primitives, storage | Spike is the gate. Failure means replanning the same day. |
| 2 | Diff engine, LCA, three-way merge, conflict taxonomy, post-merge validation, tests | **Lowest risk** — pure functions, no I/O |
| 3 | Postgres + MySQL emitters, topological ordering, temp-name routing, safety classification, round-trip harness | Medium — ordering edge cases |
| 4 | Frontend: editor, branch list, shared delta component | **Highest risk. This is where it slips.** |
| 5 | Deploy, seeded demo, empty states, error paths, README | Compresses badly if day 4 overruns |

Day 2 being both the cheapest day and the crown jewels is the good news. Day 4 is the
problem, which is why the cut order is pre-committed rather than improvised.

**Pre-committed cut order**, from the bottom: third dialect (already gone) → MySQL
*ingest* (keeping the emitter, which is what proves the core is neutral) → structured
editor breadth → branch graph visualization.

**Never cut:** identity-based diff, three-way attribute merge, post-merge integrity
validation, merge-base computation, rename inference, round-trip verification.

---

## First-run experience

The deployed URL opens onto a **seeded repo that already has two branches with a
pre-staged conflict** — including a rename swap and a divergent rename — so the
interesting behavior is visible in ten seconds without typing any DDL.

This isn't depth, but it determines whether a reviewer ever reaches the depth. Roughly two
hours of work for the highest leverage in the build.

---

## Stretch, above the cut line but below the core

**PGlite** — real Postgres compiled to WebAssembly — would let generated Postgres
migrations execute for real, in the browser, against a genuine engine, with zero
infrastructure. "Here's your merge, and here it is actually running."

It degrades honestly and asymmetrically: Postgres gets live in-browser verification,
MySQL gets CI verification only, and the UI says so rather than implying parity.

Gated on day 3 landing clean. It is exactly the sort of clever thing that could consume
day 4, and a coherent merge engine with a comprehensible UI matters more than a party
trick.
