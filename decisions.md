# Decisions

A running log of the real calls made building this. Not a changelog — the reasoning,
the alternatives, and the things deliberately left out.

Entries are roughly chronological. Where a later decision reversed an earlier one, the
reversal is recorded in place rather than edited away — see also [Reversals](#reversals).

---

## D1 — The versioned artifact is a canonical schema model with stable object identity, not DDL text

**Decision.** Every schema object — table, column, constraint, index — carries a UUID
assigned at creation and preserved for its lifetime. The unit of version control is
this object graph, not SQL text.

**Alternatives.** (a) Version DDL files as text in a git-like store, diff with a line
differ. (b) Key objects by name, diff by name comparison.

**Reasoning.** Both alternatives make renames *unrepresentable*. Under name-keyed
diffing, renaming `email` to `email_address` is indistinguishable from dropping one
column and adding another — so the diff reports a data-destroying operation where a
safe one occurred, and every downstream consumer inherits that error. With identity,
a rename is `same id, new name`, which is a one-line diff rule. Text diffing is worse
still: it reports formatting churn as change and cannot merge two edits to the same
`CREATE TABLE` block without inventing syntax.

The whole product thesis depends on this: git can merge migration files, but it cannot
know that a rename on one branch and a retype on the other touch the *same column*.

**Cut.** Preserving byte-exact original DDL formatting. Once text is parsed to a model
and re-emitted, formatting is normalized. Round-tripping a user's exact whitespace and
comment placement is a separate problem with no bearing on merge correctness.

---

## D2 — Commits store full snapshots, not operation logs

**Decision.** A commit is a complete schema snapshot plus parent pointer(s). Diffs are
derived by comparing two snapshots.

**Alternatives.** An append-only log of typed operations (`RenameColumn`,
`AddConstraint`, …), with state derived by replay.

**Reasoning.** The op-log is seductive because renames are explicit and merges look like
operational transform. But replay ordering becomes pathological fast: operations that
commute in one history don't in another, and reconstructing state at an arbitrary commit
means replaying from genesis. Snapshots are self-describing — reading any commit costs
one lookup — and identity (D1) already gives renames for free without needing the log.
This is git's tree model, for the same reason.

**Tradeoff accepted.** Storage is O(schema size) per commit rather than O(delta).
For schemas of the size this targets, that's irrelevant; content-addressing the
snapshots recovers most of it anyway if it ever matters.

**Cut.** Delta compression / packfiles. Premature at this scale.

---

## D3 — Three-way merge operates per *attribute*, not per object

**Decision.** A column is a bag of scalar attributes (`name`, `type`, `nullable`,
`default`). Merge reconciles each attribute independently against the merge base.

**Alternatives.** Object-level merge: if both sides touched a column at all, conflict.

**Reasoning.** This is the case the product exists to handle. Branch A renames a column,
branch B widens its type — disjoint attributes, so it auto-merges cleanly and correctly.
Object-level merge would flag it as a conflict and require a human to hand-reconcile
something a machine can prove is safe. Every existing tool gets this wrong because they
don't have D1.

**Cut.** Nothing here — this is the core.

---

## D4 — Ordered column lists are atomic values, never element-wise merged

**Decision.** Indexes, primary keys, and foreign keys own an *ordered list* of column
references. If both branches modified that list, it is a conflict. No element-wise
reconciliation.

**Alternatives.** Merge the lists element-wise, the way D3 merges scalars — union the
additions, preserve relative order.

**Reasoning.** Index column *order* determines whether the index is usable for a given
query. If A prepends `tenant_id` and B appends `created_at`, element-wise merging
produces a composite index with an ordering neither engineer designed — a silent
performance regression presented as a successful automatic merge. That is precisely the
failure a schema VCS exists to prevent. Refusing to be clever here is the correct call:
predictable and occasionally annoying beats subtle and wrong.

**Cut.** Element-wise list merge. Explicitly, and it's documented in the UI, not
just here.

---

## D5 — Dialect-neutral core with adapters at the edges (reversal)

**Decision.** The canonical model and the entire merge engine are dialect-agnostic.
Dialect knowledge exists only at two boundaries: ingest (parsing) and emit (DDL
generation). Ship Postgres and MySQL.

**Alternatives.** Postgres only, which is what I originally argued for — on the grounds
that a dialect abstraction layer is breadth dressed up as depth.

**Reasoning for the reversal.** I was wrong about the direction of the cost. With a
single dialect, Postgres assumptions leak into the supposedly neutral model and nobody
finds out, because there is no second implementation to violate them. Building the
second emitter is what *forces* the core to actually be neutral. The abstraction is
cheaper to maintain when it's exercised than when it's hypothetical.

The hard constraint this creates: `if (dialect === ...)` appearing anywhere inside the
merge engine means the design has failed. That's the invariant, and it's testable.

**Cut.** A third dialect. Two implementations prove the abstraction; a third proves
nothing new and costs a day. Also cut: cross-dialect *migration* as a product workflow
("point at MySQL, land on Postgres") — that's a data migration tool, a different build
entirely. Cross-dialect emit exists as a capability that falls out of the neutral core,
with an explicit representability report, and is not presented as a supported migration
path.

---

## D6 — The canonical type model is a superset with per-dialect representability, not a lowest common denominator

**Decision.** The canonical model can express constructs that not every dialect
supports. Each dialect adapter declares what it can represent. Emitting something a
target dialect can't express produces a *report*, not a crash and not a silent
substitution.

**Alternatives.** Restrict the canonical model to the intersection of all supported
dialects.

**Reasoning.** The intersection is close to useless. It excludes Postgres partial
indexes, arrays, and `TIMESTAMPTZ`, and MySQL unsigned integers and prefix indexes —
which is to say it excludes most of what's actually in a production schema. A schema
VCS that can't represent your schema is not a schema VCS.

The interesting consequence is that "unrepresentable" becomes a first-class result.
MySQL `UNSIGNED INT` emitted to Postgres has no equivalent type; the honest output is
the closest substitution (`integer` plus `CHECK (col >= 0)`) *flagged as an
approximation*, rather than a silent downgrade.

This is also where the real difficulty of multi-dialect schema tooling lives, and it's
worth being concrete about why: MySQL accepts `BOOLEAN` but stores it as `TINYINT(1)`,
which does not constrain values to 0/1, `TEXT` is
unbounded in Postgres but capped at 65,535 bytes in MySQL, `DATETIME` / `TIMESTAMP` /
`TIMESTAMPTZ` have materially different timezone semantics per engine, and MySQL prefix
indexes (`KEY(url(64))`) have no Postgres equivalent at all. Deciding when two types are
"the same object with a changed attribute" versus "a different type" is a judgment call
the diff engine's correctness rests on.

**Cut.** Collation and charset modeling. Genuinely fiddly, and its main manifestation
is false diffs — which I avoid entirely by not modeling it rather than modeling it badly.

---

## D7 — Parse with `sqlglot` rather than hand-rolling; backend is therefore Python

**Decision.** DDL parsing and dialect-aware type extraction via `sqlglot`. The backend
is Python because of it.

**Alternatives.** (a) TypeScript end-to-end with `node-sql-parser` — one language,
simpler repo and deploy, lower parser fidelity. (b) Hand-write two DDL grammars.

**Reasoning.** Hand-rolling two DDL parsers in a five-day build means shipping two
parsers and no merge engine. That is the single most likely way this project fails, so
the parser must be a library, and the best multi-dialect option is Python. A language
choice falling out of a library choice is worth recording as a decision rather than
letting it look like a default.

Explicitly *not* a concern: that using sqlglot hollows out the depth. It supplies parsed
type nodes. It does not do identity tracking, semantic diff, three-way merge, merge-base
computation, representability reporting, or migration ordering — all of which stay
hand-built. The canonical type model and its normalization rules are still mine to
design; I just don't also write two SQL grammars.

**Tradeoff accepted.** Two languages in the repo (Python API, JS frontend), paid down
with a single `make dev`.

**Risk gate.** Hour one is a spike against ugly real-world DDL from both engines. If
sqlglot can't cleanly round-trip a `CREATE TABLE` with constraints, defaults, and
indexes, I need to know before the model is built, not on day three.

---

## D8 — Live migration execution lives in CI, not in the product

**Decision.** The deployed application generates migrations and never executes them.
Verification happens in CI as a round-trip property test: apply the generated plan to a
throwaway schema in a real engine, re-introspect it, assert the result equals the
snapshot we intended.

**Alternatives.** Run real Postgres and MySQL alongside the deployed app so users can
apply a migration and see it succeed.

**Reasoning.** The *value* of executing generated DDL is proving the emitter is correct,
and CI is where that proof belongs — it runs on every commit, against both engines, and
it catches emitter bugs, ordering bugs, and type-normalization bugs in one assertion.
Running two database engines in production to serve a preview pane buys demo theatre and
an operational burden.

This test is the answer to "tests that catch real problems." It is the highest-value
test in the repo.

**Downstream consequence (see D9).** Because nothing in the product needs a database
engine, a background worker, or a long-running process, the app became deployable on a
free serverless tier. That wasn't the motivation, but it's why the constraint in D9 was
satisfiable at all.

---

## D9 — Free-tier deployment: serverless Python + managed SQLite (Turso)

**Decision.** Serverless Python for the app and API, Turso (managed libSQL) for
persistence, GitHub Actions with `postgres` and `mysql` service containers for the
round-trip tests. Locally: a SQLite file, plus Docker Compose only if you want to run
the round-trip suite.

**Constraint.** Deployment must be free, with all features usable — not a trimmed demo.

**Alternatives.** (a) A free always-on container tier plus free managed Postgres. (b)
SQLite on a persistent volume. (c) SQLite file on a serverless host.

**Reasoning.** Diff and merge are millisecond-scale pure computation over JSON, so
serverless fits genuinely rather than as a compromise. The deciding factor against the
always-on container tiers is cold start: tens of seconds of container spin-up versus a
few seconds for a serverless function. (Both figures are estimates, not measured — the
ordering is what the decision rests on, and it should be sanity-checked once deployed.) A reviewer clicks the link exactly once, and a
50-second blank page reads as broken. Option (c) fails outright — serverless hosts have
no durable filesystem, so a local SQLite file doesn't survive an invocation. Turso keeps
SQLite semantics while being reachable from serverless, so local and deployed run the
same engine and there's no dev/prod datastore drift.

"All features usable" holds because of D8: there is no feature gated behind
infrastructure the free tier lacks.

**Honest caveat.** Free-tier terms churn constantly and I'm working from prior knowledge,
not today's pricing pages. Confirming current limits is a day-one task. Nothing in the
architecture depends on either specific vendor.

**Cut.** Anything needing a persistent process: background jobs, websockets, scheduled
work.

---

## D10 — The app's datastore has nothing to do with the dialects it versions

**Decision.** Recording this explicitly because it's a natural point of confusion, and
resolving it collapsed most of the deployment problem.

Schema snapshots are JSON documents. A snapshot of a MySQL schema is a document with a
`dialect: "mysql"` field and type nodes normalized against MySQL's rules. Storing it
requires no MySQL engine — the same way storing a `.sql` file on a filesystem doesn't
require the filesystem to parse SQL.

The *only* place real database engines appear anywhere in this system is the D8
round-trip test.

---

## D11 — The conflict taxonomy is a closed set of five categories

**Decision.**

| # | Situation | Category |
|---|---|---|
| 1 | Exists in base, deleted in ours, modified in theirs | delete/modify |
| 2 | Absent in base, added in both, same name, different UUIDs | name collision |
| 3 | Same object, same attribute, divergent values | attribute conflict |
| 4 | Same object, *different* attributes changed | **auto-merge** |
| 5 | Merged result violates an invariant | integrity violation |

Categories 1–4 are pairwise. Category 5 is global, computed on the merged snapshot:
dangling foreign keys, indexes on dropped columns, nullable primary key columns,
duplicate names within a scope.

**Reasoning.** The largest scope risk in a merge engine is the conflict taxonomy growing
without bound. Declaring it closed makes that risk visible: if a sixth category shows up
during the build, that's evidence I mis-modeled something, not a licence to add a case.

Category 5 is not optional garnish, and one case proves it. Branch A renames `email` to
`contact`; branch B adds a new column named `contact`. Pairwise merge sees no conflict at
all — different UUIDs, disjoint attribute sets, nothing overlaps. Only validation of the
merged result catches the duplicate name. A tool without this pass reports a clean merge
and emits DDL that fails.

---

## D12 — Conflicts are resolved atomically on one screen; no persisted mid-merge state

**Decision.** A conflicted merge is presented in full and submitted as a single
resolution. There is no half-merged state stored server-side.

**Alternatives.** Model a git-style index — persist partial resolutions so a user can
resolve incrementally, leave, and return.

**Reasoning.** Persisting mid-merge state means modeling it, recovering it after a failed
request, garbage-collecting it when abandoned, and reconciling it when the target branch
moves underneath it. That's a meaningful chunk of a five-day budget spent on state
management rather than on merge correctness.

**Tradeoff accepted, and it's a real one.** A forty-conflict merge cannot be done in two
sittings. Documented in the UI rather than discovered.

---

## D13 — Merge base is computed as a lowest common ancestor over the commit DAG, not read from a stored branch point

**Decision.** Compute the LCA of the two branch heads at merge time.

**Alternatives.** Record the commit a branch was created from, and use it as the merge
base forever. This is what almost every implementation of this exercise will do.

**Reasoning.** The stored branch point is correct exactly once. After `main` has been
merged into `feature`, the correct base for the next merge has advanced to the commit
`main`'s tip pointed at when that merge happened. Keep using the original branch point and
every conflict already resolved comes back and asks to be resolved again, on every
subsequent merge, forever. That specific behavior is what makes people abandon a merge
tool.

**Correction, recorded because it was wrong in the first draft of this file.** I originally
wrote that the new base *is the merge commit*. Derived properly: with `main = A→B→C`,
`feature` branched at `B` to `D`, merging `main`→`feature` creates `M(D, C)`. Add `E` on
`main`. Then `ancestors(E) = {A,B,C,E}` and `ancestors(M) = {A,B,C,D,M}`, so the common set
is `{A,B,C}` and the lowest is **`C`** — the tip `main` had at merge time. Not `M`, and not
the branch point `B`. A merge commit *can* be the LCA in cross-merge topologies (`main`→
`feature` followed by `feature`→`main`), which is what made the wrong version sound
plausible. The decision is unaffected — what matters is that the base advances past the
branch point — but the expected *value* was wrong, and it was wrong inside a test
expectation (`L-06`), which would have produced a day-2 failure with no way to tell whether
the test or the implementation was at fault.

This is the headline depth bet. It's invisible when right, silently corrosive when
wrong, algorithmically real but bounded — a DAG walk, on the order of a hundred lines —
and it is *demonstrable*: merge, resolve a conflict, merge again, and show the naive
implementation re-litigating what this one doesn't.

**Cut.** See D14.

---

## D14 — Multiple merge bases (criss-cross histories) are detected and refused, not approximated

**Decision.** When two branches have previously merged each other, the DAG can yield
multiple candidate LCAs. Detect this and refuse the merge with an explicit explanation.

**Alternatives.** (a) Recursive merge — merge the candidate bases into a virtual base,
as git does. (b) Pick one candidate arbitrarily and proceed.

**Reasoning.** Recursive merge is correct and out of reach in five days. Option (b) is
the real hazard: it produces a plausible result that is subtly wrong, behind a UI that
looks confident. For a tool whose entire value proposition is trustworthy merges, a
loud refusal on a rare topology is strictly better than a quiet miscalculation.

---

## D15 — The emitted migration is `diff(target_head, merged_result)`, not a rendering of the merge

**Decision.** Recording this because it's the subtlest trap in the design.

The merge produces a *schema*. The migration a user needs to run against the target
database is the difference between the target branch's current state and that merged
result — which is not the same as the set of changes the merge reconciled. Conflating the
two produces output that is wrong in a way that looks right, and it's a one-line
distinction once noticed.

---

## D16 — The emitter routes through temporary names when an intermediate state would collide

**Decision.** Before emitting, check whether any step leaves the schema in a state with
a duplicate name. If so, route through a temporary name.

**Reasoning.** Two columns exchanging names is the canonical case: `x` → `y` and
`y` → `x`. Start state is valid, end state is valid, and every naive emitter produces DDL
that fails partway through on a duplicate-name error because the *intermediate* state is
invalid. The correct handling is unambiguous and mechanical, which is exactly why it's a
good bet — it breaks loudly when skipped, and a reviewer can try it in fifteen seconds.

---

## D17 — Migration operations are topologically sorted, with foreign-key cycles broken by phasing

**Decision.** Order operations by dependency: tables before foreign keys that reference
them, foreign keys dropped before the tables they reference, columns created before
indexes on them. When two tables reference each other, break the cycle by splitting into
a create-tables phase and an add-constraints phase.

**Reasoning.** An unordered migration is a text file, not a migration. It looks correct
on screen and fails on execution — and the failure surfaces at the worst possible moment.
Circular foreign keys are the case that forces real phasing rather than a plain sort, and
they're common in practice (`users.org_id` ↔ `orgs.owner_id`).

The D8 round-trip harness verifies ordering automatically, so this stays honest for free.

---

## D18 — MySQL migrations are emitted as individually-safe steps with explicit no-rollback markers

**Decision.** Postgres wraps DDL in a transaction; MySQL cannot. Rather than pretend the two
engines behave alike, MySQL plans are emitted as a sequence of individually-safe steps,
annotated with the point past which failure cannot be rolled back.

**Reasoning.** A failed MySQL migration leaves a half-migrated schema. Presenting a
MySQL plan as though it were atomic is an actively dangerous lie. This is a small amount
of work and it's the difference between a tool that models reality and one that models
Postgres and hopes.

**Corrected after verification — I had the mechanism wrong.** The first draft said "MySQL
DDL is not transactional," which is outdated. MySQL 8.0 introduced *atomic DDL*: the data
dictionary lives in InnoDB and each individual DDL statement is genuinely all-or-nothing,
journalled through `mysql.innodb_ddl_log`. What remains true, and is the actual reason for
this decision, is that **DDL causes an implicit commit** — a DDL statement cannot run
inside `START TRANSACTION … COMMIT`, and it ends any transaction already open. So the
*statement* is atomic and the *migration* is not: fail at step 5 of 9 and steps 1–4 are
committed and unrollbackable.

This sharpens the design rather than changing it. The atomic unit is precisely one
statement, so the no-rollback marker is per-statement and exact, not a hand-wave. Also
worth noting the atomicity guarantee is InnoDB-only.

Postgres by contrast does allow multi-statement DDL inside a transaction, with a few
exceptions (`CREATE INDEX CONCURRENTLY`, `CREATE DATABASE`) that the emitter must not
place inside the `BEGIN`/`COMMIT` block.

---

## D19 — Operation safety is classified; version-specific rewrite prediction is not

**Decision.** Every emitted operation is classified `safe`, `lossy`, or `lock-heavy`.
Lossy operations (narrowing a `varchar`, `int` → `smallint`, dropping a column) require
explicit acknowledgment before merge.

**Alternatives.** Full lock and table-rewrite prediction — "instant in Postgres 11+,
full table rewrite in Postgres 10."

**Reasoning.** Three-way classification is cheap, useful, and derivable from the type
lattice already needed for D6. Version-specific rewrite prediction is a per-engine,
per-version knowledge base — breadth in a depth costume, and it goes stale.

---

## D20 — Rollback generation is in scope, because row data is out of scope

**Decision.** Generate down-migrations alongside forward migrations.

**Reasoning.** Worth recording because the usual objection doesn't apply here. Reversing
a column drop is normally impossible — the data is gone. But the brief puts row data out
of scope, so the artifact under version control is purely structural, and a dropped
column is fully recoverable *as schema*. A constraint from the problem statement made a
normally-intractable feature tractable. Taking it.

**Scoped honestly, corrected from the first draft.** "Nearly free" was too strong. A
rollback is always *expressible* at the schema level, but it is not always *executable*
against a populated table: re-adding a dropped `NOT NULL` column with no default fails
where rows exist. Row data being out of scope for *versioning* doesn't make the target
database empty. So down-migrations are generated and classified for executability, and the
ones that cannot run against a non-empty table are flagged rather than presented as a
working undo.

---

## D21 — Ingest rejects unsupported constructs loudly, with line numbers

**Decision.** A documented DDL subset. Anything outside it produces a specific,
line-numbered error — `line 14: GENERATED ALWAYS AS is not supported; supported column
features are …` — rather than being skipped.

**Alternatives.** Ignore unrecognized tokens and parse what's understood.

**Reasoning.** Silent misparse is the worst possible outcome in this product. If a
`CHECK` constraint is quietly dropped on import, every subsequent diff, merge, and
generated migration is wrong, and the user has no way to know. Refusing to import is
recoverable; importing something subtly incorrect is not.

**Cut.** `pg_dump` / `mysqldump` ingest. Genuinely valuable and genuinely tempting, but
real dump files bring `SET` statements, `COPY` blocks, grants, ownership, extensions, and
search-path manipulation — an unbounded surface with no natural stopping point.

---

## D22 — Rename inference on import is heuristic plus human confirmation, never silent

**Decision.** Pasted DDL has no object identity, so identity must be reconstructed by
matching against the previous snapshot. Name matching handles the easy cases; where a
name disappeared and a new one appeared, the tool proposes a confidence-scored rename and
asks the user to confirm.

**Alternatives.** (a) Treat every unmatched name as drop + add. (b) Auto-accept the
highest-confidence guess.

**Reasoning.** This is the genuinely ambiguous input in the problem, and the consequences
of the two readings diverge sharply — a rename preserves history and is safe, a drop+add
destroys both. Option (a) throws away the product's core value on exactly the input users
will paste. Option (b) makes an unrecoverable guess on the user's behalf.

The hardest sub-case is rename *and* retype in the same commit, where neither name nor
type is available to anchor on. Handling: propose based on ordinal position and remaining
attributes, at low confidence, clearly marked as a guess.

---

## D23 — The diff view and the conflict resolver are one component

**Decision.** A single schema-delta rendering primitive, parameterized by whether it has
two inputs (diff) or three (merge resolution).

**Reasoning.** A conflict *is* a diff with an extra column and a choice attached. Two
components is roughly two days of frontend; one component is roughly one. Given that the
frontend is the largest single cost in this build and the merge engine is the cheapest,
this boundary is worth getting right on day one.

---

## D24 — Structured editor scoped to exactly the operations the brief enumerates

**Decision.** Add / drop / rename / retype column, change constraints and indexes,
create / drop table. Each as an explicit typed operation. Nothing else.

**Alternatives.** A general-purpose schema designer — visual canvas, drag-and-drop,
relationship drawing.

**Reasoning.** Typed operations are what make identity (D1) free on the editing path,
which is the whole reason to have an editor rather than only SQL paste. Beyond that
enumerated set, additional editor surface is UI work that teaches the evaluation nothing
about schema version control. The brief lists the operations; implementing precisely
those and stopping is a scope decision, not an omission.

---

## D25 — Optimistic concurrency on branch heads

**Decision.** Committing performs a compare-and-swap on the branch's head commit ID. A
stale head is rejected with a clear "this branch moved" message.

**Reasoning.** Cheap, and it's the actual concurrency failure mode for this product —
two people committing to one branch, where the loser's work vanishes silently. Real-time
collaborative editing is out of scope (D26); not silently losing a commit is not.

---

## D26 — What is deliberately cut, consolidated

Beyond the cuts recorded inline above:

| Cut | Why |
|---|---|
| Row data | Out of scope per the brief. The artifact is the schema. |
| Rebase, cherry-pick, revert, tags | Merge is the interesting primitive; rebase is the same three-way machinery pointed sideways. One done well beats three done thinly. |
| Auth, users, permissions | Solved problem, zero evaluation signal. Single shared workspace. |
| Views, triggers, stored procedures, partitions, RLS, sequences-as-objects | Documented boundary, not a silent gap. Tables, columns, PK/FK/unique/check, indexes, defaults, nullability. |
| Live production database introspection | Same capability as paste-DDL import, with credential and network risk attached. |
| Real-time collaborative editing | Needs a persistent process, which D9 rules out, and it's orthogonal to merge correctness. |
| 500-table diff performance | Merge is pure computation and will hold; the *diff view* would need virtualization. Honest documented limit beats half-built windowing on day four. |
| Branch graph visualization | Flat branch list. Pretty, not load-bearing. |

---

## D27 — Column ordering within a table is not semantically modeled

**Decision.** Two snapshots that differ only in the order columns were declared are equal.
Column order is not an attribute, is not diffed, and is not merged.

**Alternatives.** Model ordinal position as a column attribute, diff it, and emit MySQL
`AFTER` clauses to reposition.

**Reasoning.** Recording this because it was implied by the test plan (`N-05`, `F-45`)
before it was ever decided, which is exactly the kind of gap worth catching before
development rather than after.

Column order has no logical meaning in either engine — it affects `SELECT *` output and
nothing a query depends on. Modeling it would mean every insertion of a column in the
middle of a table produces a cascade of position deltas for every column after it,
generating enormous diffs for a change with no semantic content, and giving two branches a
guaranteed conflict any time both add a column to the same table. That last consequence is
disqualifying: it would break `M-03`, one of the core auto-merge cases.

**Tradeoff accepted.** Postgres cannot reorder columns at all without rewriting the table,
so this is free there. MySQL *can* (`ALTER TABLE … AFTER`), so a MySQL user who deliberately
positions a column will see that intent silently dropped on round-trip. Documented, and
paired with the contrasting call in D4 — index column order *is* modeled, because there it
changes behaviour.

---

## Reversals

Recording these because the reasoning that changed is more informative than the
conclusion.

**Postgres-only → multi-dialect (D5).** I argued a dialect abstraction was breadth
masquerading as depth. I had the cost backwards: a single implementation lets dialect
assumptions leak into the "neutral" core undetected, and the second emitter is the thing
that enforces the boundary. The abstraction is cheaper exercised than hypothetical.

**Live execution in the product → CI-only (D8).** Originally scoped as a product feature
so users could apply a migration and watch it succeed. Realized the value is *emitter
correctness*, which belongs in a test that runs on every commit, not in a preview pane
backed by two production database engines. This cut then turned out to be what made free
deployment (D9) possible at all — the app needs no engine, no worker, no persistent
process.

**Frontend cost.** I initially treated the merge engine as the expensive part. Inverted:
the merge engine is pure functions over plain data, fast to write and trivially testable;
the frontend is four non-trivial surfaces and it's where the schedule actually slips.
D23 exists because of this correction.

---

## D29 — Diff requires a shared identity lineage; comparing independent snapshots is a separate operation

**Found while implementing the diff engine**, and it is a real gap in the original
design rather than a bug in the implementation.

**The problem.** `diff` matches objects by UUID (D1). Two snapshots descended from a
common ancestor share those UUIDs, so it works. Two *independently parsed* snapshots
share none — so diffing them reports every object as dropped and re-added. Useless
output, and the first thing a user does is paste their schema twice.

**Decision.** Re-import is an **align-then-diff** operation, and alignment is its own
module (`engine/align.py`) with a deliberately narrow contract:

- **Alignment by name is deterministic.** Same name in both snapshots → same object. No
  judgement, no configuration, no guessing.
- **Alignment across a rename is explicitly out of scope here.** The name is precisely
  the evidence that disappeared, so it can only be a guess. That is rename inference
  (D22), it is heuristic, it requires human confirmation, and it belongs to the import
  path.

**Alternatives.** (a) Make `diff` fall back to name-matching when ids don't line up.
(b) Have `diff` infer renames itself when a name vanished and a similar one appeared.

**Reasoning.** Both alternatives put a guess inside the engine that everything else
trusts absolutely. If `diff` can invent a rename, then the emitter generates
`ALTER TABLE … RENAME COLUMN` where it should generate a drop plus an add — silently
converting a data-destroying change into a safe-looking one, or the reverse. The whole
value of identity-based diffing is that identity is *authoritative*; an engine that
sometimes guesses identity has neither property.

Splitting it also puts the heuristic exactly where a human can see and correct it, which
is what D22 requires anyway.

**Consequence for the tests.** The idempotency block (`F-41`–`F-45`) is now written as
`diff(a, align_identity(a, b))`, which is a *better* test than what was specified: it
exercises the real user flow ("I re-pasted my schema, what changed?") rather than
comparing two parse results in a way no user ever would.

`test_align.py` pins the boundary with a test asserting that a renamed column is **not**
inferred as a rename — the failure this decision exists to prevent.

### Made loud rather than documented

I hit this mistake myself in eight tests while building the diff engine, which is decent
evidence a library user will too. So instead of only writing it down, `diff()` now
guards: if two snapshots share table **names** but no object **identities**, it raises
`UnalignedSnapshotsError` naming `align_identity` as the fix.

The discriminator is name overlap *without* id overlap. A rename preserves ids, so there
is no legitimate way for two related snapshots to share a table name and share no
identity — which makes this a false-positive-free check rather than a heuristic. It stays
deliberately silent when either side is empty, because diffing genesis against a first
commit legitimately shares nothing.

The test-side fix is structural too: the base schema is now a pytest fixture
(`tests/conftest.py`) rather than a helper function, and the helper is deleted. A helper
called twice in one test returns two unrelated snapshots; a fixture is evaluated once per
test, so the mistake is no longer expressible. That matters more going forward than
behind — every one of the ~60 remaining merge tests needs two branches diverging from one
shared base.

---

## Verification log

Every load-bearing external claim in these documents, checked against primary sources
before development started. Recorded so the difference between *verified* and *assumed*
stays visible — several claims below were wrong in the first draft.

### Verified correct

| Claim | Status | Detail |
|---|---|---|
| sqlglot parses PG + MySQL DDL | ✅ | Dependency-free, 30+ dialects, handles `CREATE`/`ALTER`/`DROP TABLE`, and detects unbalanced parens and reserved-word misuse — which `P-50`/`P-51` depend on |
| Turso free tier is genuinely free | ✅ | 100 databases, 5 GB storage, 500M monthly row reads, 10M writes, no credit card. One secondary source claimed scale-to-zero deprecation with a persistent compute charge; not present on the official pricing page — re-check at deploy time |
| PGlite is real Postgres in WASM | ✅ | Not a Linux VM — Postgres compiled via Emscripten in single-user mode, 3 MB gzipped, in-memory or IndexedDB. Single-process, so no concurrency, which is fine for verification |
| Postgres allows multi-statement DDL in a transaction | ✅ | With exceptions (`CREATE INDEX CONCURRENTLY`, `CREATE DATABASE`) the emitter must keep outside `BEGIN`/`COMMIT` |
| GitHub Actions service containers are free for public repos | ✅ | The `R-*` harness has no cost |

### Corrected

| Claim as first written | Reality | Impact |
|---|---|---|
| "After merging main→feature, the merge base becomes the merge commit" | The base becomes the **tip of the merged-in branch at merge time**. A merge commit can only be the LCA in cross-merge topologies. | **The worst of these.** It was wrong inside a test expectation (`L-06`) on the headline decision. Fixed in D13, `scope.md`, and split into `L-06`/`L-06b`/`L-06c` |
| "MySQL DDL is not transactional" | MySQL 8.0 has **atomic DDL** — each statement is all-or-nothing via `mysql.innodb_ddl_log`. The real constraint is that DDL forces an **implicit commit**, so a multi-statement migration still can't roll back. InnoDB only. | Conclusion survives, mechanism was wrong. Actually sharpens D18: the atomic unit is exactly one statement, so the no-rollback marker is precise |
| "MySQL prefix indexes have no Postgres equivalent at all" | Postgres expression index on `LEFT(col, n)` is a functional equivalent, with the caveat that the planner only uses it when a query repeats that exact expression. | `E-103` flips from *unrepresentable* to *approximation with a behavioural caveat* |
| "MySQL has no `BOOLEAN`" | MySQL accepts `BOOLEAN`/`BOOL` as synonyms for `TINYINT(1)`. The real subtleties: `TINYINT(1)` doesn't constrain values to 0/1, display width was deprecated in 8.0.17, and from 8.0.19 only `TINYINT(1)` without `UNSIGNED`/`ZEROFILL` carries the boolean assumption. | Added `P-30b`–`P-30d` |
| "MySQL unquoted identifiers preserve case" | **Column** names are case-insensitive on every platform. **Table** names depend on `lower_case_table_names` — default 0 on Unix, 1 on Windows, **2 on macOS**, and it can only be set at server initialization. | Added `P-34b`–`P-34d`. Also a CI landmine: the macOS default differs from the Linux container default, so the test harness must pin the value explicitly |
| "Rollback is nearly free because row data is out of scope" | Expressible ≠ executable. Re-adding a dropped `NOT NULL` column with no default fails against a populated table, and the target database has rows even though we don't version them. | D20 softened; down-migrations now classified for executability |
| "1–3s serverless cold start vs 30–60s container" | Unmeasured estimate stated as fact. | Relabelled as an estimate. The *ordering* is what the decision rests on. Also newly noted: Vercel Hobby is 60s function timeout, 4 CPU-hours/month, and **non-commercial use only** |

### Gap found, not an error

Column ordering within a table was assumed by the test plan (`N-05`, `F-45`) before it was
ever decided. Now recorded as D27, and the reasoning turned out to matter: modeling column
position would give two branches a guaranteed conflict whenever both add a column to the
same table, which would break `M-03`.

### Still unverified — do not treat as fact

- sqlglot's DDL **fidelity** on the specific constructs in scope. Library support for
  `CREATE TABLE` is confirmed; whether constraints, defaults, and index definitions
  round-trip cleanly is exactly what the day-1 spike (`P-01`, `P-23`) exists to answer.
- Turso's scale-to-zero / persistent-charge question above.
- That Vercel Hobby's 4 CPU-hours/month is ample. Almost certainly true for millisecond
  requests, but unmeasured.
- The estimate that LCA is "on the order of a hundred lines." A guess, stated twice as if
  it were a measurement.

---

## D28 — sqlglot day-1 gate: passed, with four things we now own

**Outcome.** Gate passed. Structured extraction of tables, columns, types, constraints,
and indexes works for both dialects. `spike/sqlglot_spike.py` is kept in the repo as the
record. Four findings changed what we build.

### 1. Prefix indexes are silently corrupted — this is the important one

MySQL `KEY i1 (note(32))` does not parse to a prefix-index construct. sqlglot has no such
concept, so it falls back to `Anonymous(this=note, expressions=[64])` — a **function
call**. Round-tripping it emits `` INDEX `i1` (`NOTE`(32)) ``: the column identifier is
**upper-cased and re-quoted as a function name**. `note` becomes `NOTE`.

That is precisely the silent-misparse failure D21 exists to prevent, and it would have
shipped a corrupted column reference inside an index definition.

**Decision.** The parser detects `Anonymous` nodes in index column lists and reconstructs
the prefix index explicitly, matching the mangled name back to a real column on the table
**case-insensitively**.

The case-insensitive match is safe for a non-obvious reason that fell out of the earlier
verification pass: MySQL column names are case-insensitive on every platform, so `NOTE`
and `note` cannot be two different columns. The correction that came out of checking a
docs claim turned out to license the fix for an unrelated bug. Under Postgres, where
quoted identifiers *are* case-sensitive, the same reconstruction would be unsound — so
this is a MySQL-adapter-only rule, which is where it belongs.

`P-39` accordingly becomes a **round-trip fidelity** test (`note(32)` in → `note(32)`
out, lowercase preserved), not merely a "parses to something" test.

**Generalized.** `Anonymous` is sqlglot's "I didn't recognize this" node. Any `Anonymous`
appearing where the model expects a column reference or a known default is treated as a
rejection candidate rather than passed through. Silent pass-through of `Anonymous` is the
single most likely source of quiet corruption in the ingest path.

### 2. Default normalization is free on Postgres, ours on MySQL

Postgres collapses `now()`, `NOW()`, and `CURRENT_TIMESTAMP` to one `CurrentTimestamp`
node — `P-23` passes for free there. MySQL does **not**: `CURRENT_TIMESTAMP` →
`CurrentTimestamp`, but `now()` → `Anonymous`. So the highest-floor test in the plan is
real work, on exactly one of the two dialects, and it's the same `Anonymous` mechanism as
finding 1.

### 3. Autoincrement is structurally asymmetric between dialects

Postgres `bigserial` → a distinct type `DType.BIGSERIAL` with no constraints. MySQL
`bigint AUTO_INCREMENT` → `DType.BIGINT` plus an `AutoIncrementColumnConstraint`. Same
concept, two different shapes — one encoded in the type, one in a constraint.

Canonical model stores `(BIGINT, autoincrement=True)` and each adapter re-splits on emit.
Confirms the D6 type work is real rather than a library call.

### 4. Index AST shape, confirmed

`Create.this` is an `Index`; `Index.params` is `IndexParameters` holding `columns` as an
ordered list of `Ordered` nodes (each with a `desc` flag), plus `where` for partial
indexes. `unique` sits on the outer `Create`, not on the `Index`.

Column order is preserved as a genuine list, so `N-06` and D4 are implementable as
specified. Partial-index `WHERE` is captured, so `P-40` holds.

### Rejection paths confirmed implementable

`ParseError` carries line and column (`P-58` holds). `CREATE VIEW` → `kind='VIEW'`;
`CREATE TRIGGER` → falls back to a `Command` node; `GENERATED ALWAYS AS` →
`ComputedColumnConstraint` on the column; `PARTITION BY` → `PartitionedByProperty`. All
four are detectable, so `P-60`–`P-64` are all implementable as loud rejections.

### Environment note

The local `~/.config/pip/pip.conf` sets a global `extra-index-url` pointing at a private
AlphaSense CodeArtifact repository, which prompts for credentials and fails
non-interactively. Installs here run with `PIP_CONFIG_FILE=/dev/null`. This is machine
config, not project config — but the `Makefile` pins `--index-url https://pypi.org/simple`
so a stranger's setup can't be broken by whatever their own pip config happens to say.

---

## D31 — Name-collision conflicts are keyed on the lower object id, not on "ours"

**Decision.** A category-2 conflict (both branches independently added an object with
the same name) is identified by `min(ours_id, theirs_id)`, with the other id carried as
`other_id`. The payload still labels which side is which; only the *key* is anchored.

**Alternatives.** Key it on the "ours" object, which is what the first implementation
did and what reads most naturally. Or synthesize a composite key from both ids.

**Reasoning.** Found by the M-91 commutativity property, not by a hand-written test —
I would not have thought to check it. Keying on "ours" means merging `A` into `B` and
`B` into `A` produce *different resolution keys for the same conflict*. Since
resolutions are submitted from a browser as opaque strings, that is a live bug the
moment anyone merges in both directions: a stored or retried resolution silently fails
to match. A composite key would also be stable but is twice as long for no gain, and
`other_id` already carries the pair.

The wider point: this is the second time an algebraic property caught something the
scenario tests could not. Conflicts are *pairs*, and anything derived from a pair has
to be symmetric or it is not really about the pair.

**Cut.** Nothing.

---

## D32 — CHECK constraints record the columns they read, by id, when the caller knows them

**Decision.** `Constraint.column_ids` is populated for CHECK constraints where the
caller can supply it (the structured editor always can; the SQL parser generally
cannot, since it would mean parsing arbitrary predicate expressions).

**Alternatives.** Leave CHECK bodies opaque strings. Or parse the expression to
extract column references, and refuse CHECKs we cannot parse.

**Reasoning.** Without recorded references, dropping a column that a CHECK depends on
is *undetectable* — the merge reports clean and emits DDL the database rejects (M-85).
That is exactly the class of failure the category-5 validation pass exists to prevent,
so leaving one input to it blind defeats the pass. Extracting references from arbitrary
SQL expressions is a real sub-problem I am deliberately not opening on this timeline;
refusing unparseable CHECKs would be worse than accepting them with reduced safety,
because CHECK bodies are exactly where dialect-specific function calls live.

**Accepted tradeoff, stated plainly.** A CHECK imported from pasted DDL has no recorded
column references, so `M-85` protection does not apply to it. This is a real asymmetry
between editor-authored and imported schemas, and it is the honest state of the tool
rather than something the tests paper over.

**Cut.** Expression parsing for column extraction.

---

## D33 — Foreign-key targets are validated for uniqueness at merge time

**Decision.** The category-5 pass rejects a merged result where a foreign key
references columns that no primary key, unique constraint, or unique index covers.

**Alternatives.** Let the database catch it when the migration runs.

**Reasoning.** "Let the database catch it" means the failure surfaces halfway through a
migration, after earlier statements have already applied — which on MySQL is not
rollback-able (D18). The check is a set comparison over constraints we already model,
so the cost is a few lines against a failure mode that is expensive and confusing in
production. This is the same argument as D11: validate the result, because the inputs
were individually fine.

Note the check compares the FK's target column set against *exact* unique column sets.
A composite unique on `(a, b)` does not license an FK on `(a)` alone, which matches
what engines actually enforce.

**Cut.** Prefix-matching against composite unique constraints (correctly, since engines
don't allow it either).


## D34 — Snapshots are persisted as opaque JSON blobs, not normalized into tables

**Decision.** The `commits` table holds `snapshot TEXT` — the output of
`Snapshot.to_dict()` as JSON. Tables, columns, constraints and indexes are *not* given
relational tables of their own.

**Alternatives.** Normalize properly: a `tables` table, a `columns` table, join rows
back into a snapshot on read. Or a hybrid, storing the blob but also projecting a few
columns out for querying.

**Reasoning.** Nothing ever queries *inside* a snapshot. Every single read is "give me
the whole schema at commit X" — because diff, merge and emit all operate on complete
snapshots by construction (D2). Normalizing would mean modeling the schema model a
second time, in SQL, and then owning migrations for *that* schema too: a version
control tool needing its own schema migrations to change how it stores schemas is a
joke I'd rather not have to explain. `to_dict`/`from_dict` already round-trips
losslessly and is property-tested against generated inputs (M-93), so the risky half
was already covered.

**What would change my mind.** A feature needing cross-commit queries — "which commit
first added this column", "find every branch where `users.email` is nullable". That is
a real product idea, and it would justify projecting a searchable index alongside the
blob. It is not in scope for five days, and the blob does not preclude adding it later.

**Cut.** Relational normalization; cross-commit querying.

---

## D35 — Durability sits behind a `Store` seam, and the whole suite runs against every implementation

**Decision.** `Repo` owns semantics (what a commit means, what makes a head stale, how
history is walked) and delegates durability to a narrow `Store` protocol. Two
implementations: `InMemoryStore` and `SqliteStore`. The persistence suite is
parametrized over both.

**Alternatives.** Just use SQLite everywhere, including tests. Or keep the dicts and
add a save/load pair.

**Reasoning.** An abstraction exercised through one implementation is not abstract.
Worse, here the in-memory store is *exactly* the one that would pass the test that
matters most while hiding the bug — a dict cannot lose a race with itself, so
optimistic concurrency (D25) would look correct while being untested. Parametrizing is
three lines and it holds the real database to the same contract.

Running SQLite everywhere would have been simpler, but 250 tests paying file-I/O cost
for no signal is how a suite stops being run.

**Consequence worth stating.** Moving a branch head is now a single conditional
`UPDATE ... WHERE head = ?`. That is the entire concurrency story, and it is enforced
by the database rather than by a check-then-write in Python that two requests can
interleave through. D25 stopped being a design note and became a guarantee.

**Cut.** Nothing.

---

## D36 — The datastore lives outside `engine/`, because the architecture test said so

**Decision.** `SqliteStore` lives in a new top-level `schemavcs.storage` package, not
in `engine/`. A new architecture test asserts the neutral packages never import it.

**Alternatives.** Put it in `engine/` and add an exemption to the architecture test for
the word "sqlite".

**Reasoning.** This is the second time the architecture test has caught something real,
and this time it caught something I would have argued was fine. The test forbids
dialect names in `engine/` via a regex over source, and `sqlite` is on that list. It
cannot distinguish "the engine branches on SQLite quirks" (a design failure) from "the
engine's own datastore happens to be SQLite" (fine, per D10).

I could have added the exemption. But the exemption would have been permanent, and it
would have applied to every future line in the package — so the *next* genuine leak
would pass silently. Moving the datastore out instead cost one directory and produced a
better dependency direction: the engine declares the contract, the storage layer picks
a database, and swapping SQLite for anything else touches exactly one package.

The general lesson: when a guard rail fires on something you believe is fine, the cheap
fix is to weaken the guard rail and the right fix is usually to change the code.

**Cut.** Nothing.


## D37 — Rollback generation is cut (reversal of D20)

**Decision.** No inverse-operation generation. `E-120`–`E-124` and `R-30`/`R-31` are
dropped, as is representability reporting (`E-100`–`E-107`).

**Reasoning.** D20 argued rollback was nearly free because row data is out of scope,
and that argument was too clever. The forward path alone needs ordering, cycle
phasing, temp-name routing and safety classification; every one of those has an inverse
that needs its own ordering rules — `E-122` (the inverse of a rename swap is another
rename swap, also needing temp names) and `E-123` (reverse the phase order of a cycled
FK creation) are not free, they are the same problem again.

Weighed against a **hard requirement** — a deployed web application — that is the wrong
place to spend a day. Cutting it is the honest call rather than half-building it.

**What survives.** Safety classification stays (`E-60`–`E-72`), and it is the part that
actually protects a user: knowing an operation is lossy *before* running it is worth
more than being able to undo it after. And on MySQL rollback is partly a fiction
anyway — DDL commits implicitly, which is why the emitter marks the no-rollback
boundary instead of pretending (D18).

**Cut.** Rollback; representability reporting.

---

## D38 — Live verification is execution-only; introspection is deferred

**Decision.** The live suite applies generated migrations to real Postgres and MySQL
and asserts they **execute**. It does not introspect the result and compare it to the
intended snapshot.

**Alternatives.** Build an introspector per engine (`information_schema` queries
reconstructing a full snapshot) as the test plan originally assumed. Or emit, parse the
output back, and compare.

**Reasoning.** An introspector is a third component comparable in size to the emitter,
and parse-back is unavailable because we cannot read `ALTER`. Execution-only gets most
of the value for a fraction of the cost, because the bugs that matter here are
execution bugs: wrong statement order, a rename onto an occupied name, a missing cast.
Every one of those produces syntactically valid SQL, so string assertions cannot catch
them and only a real server can.

**Stated limitation, not glossed.** These tests prove the migration *runs*. They do not
prove it produced exactly the intended schema. A statement that succeeds while doing
subtly the wrong thing would pass.

**Vindicated immediately.** The first run against a real MySQL server failed on
`ADD CONSTRAINT uq_note UNIQUE (note)` — MySQL stores `TEXT` out of line and cannot
index it without a prefix length. The generated SQL was perfectly valid-looking; 326
unit tests were green; no string assertion would ever have found it. The emitter now
refuses with a message naming the fix. That single finding paid for the whole tier.

**Cut.** Per-engine introspection; snapshot-equality round-tripping.

---

## D39 — Constructs a target engine cannot express are refused, never approximated

**Decision.** `UnrepresentableError` on a partial index for MySQL, an unsigned column
for Postgres, a MySQL prefix index for Postgres, and an unbounded-type index for MySQL.
Raised at emit time, before the database is touched.

**Alternatives.** Approximate and flag it — `UNSIGNED INT` becomes `integer` plus
`CHECK (c >= 0)`, a prefix index becomes an expression index on `LEFT(col, n)`. That was
D6's plan and it is still the right long-term answer.

**Reasoning.** An approximation that is *flagged properly* is a feature; an
approximation that is merely emitted is a lie. Doing the flagging properly is its own
piece of work (the `E-100` block, cut per D37), and in the meantime silently emitting a
non-partial index for a partial one produces a database that looks correct and enforces
something different — the worst available outcome. Refusing is a smaller promise, and
one the tool can keep.

Each refusal names what cannot be expressed, why it matters, and a way forward
("give the index column an explicit prefix length, or narrow the column to VARCHAR(n)").
A refusal without a route onward is just a wall.

**Cut.** Approximation with representability reporting — deferred, not abandoned.


## D40 — A thin semantic tier sits on top of execution-only verification

**Decision.** Eight targeted tests assert that a generated migration did the right
thing, not merely that it ran: an identity column generates distinct values, a partial
index keeps its predicate, a MySQL prefix index keeps its length, a cast works on a
*populated* table, UNIQUE/FK/CHECK are actually enforced, and a rename swap moves names
rather than data.

**Alternatives.** Stay purely execution-only (D38). Or build the full per-engine
introspector the test plan originally assumed.

**Reasoning.** D38's limitation was real: a statement can succeed while doing subtly
the wrong thing. Rather than accept that wholesale or build a whole introspector, these
eight probe the specific constructs where "executed successfully" and "behaves
correctly" can plausibly diverge — and each one would otherwise produce a database that
*looks* right and enforces something different.

Two examples of what tier 1 alone would have missed:

* A partial index emitted without its `WHERE` clause executes fine and covers every
  row.
* A prefix index emitted without its length executes fine and indexes the whole
  column, changing both its size and its behaviour.

Both were confirmed by deliberately breaking the emitter: each mutation passes tier 1
and fails these tests.

Retyping deserves its own note. A retype on an empty table proves nothing — the engine
has no rows to convert — so `text -> int` is tested with data present, which is where a
missing `USING` clause actually bites.

**What this is not.** Not general introspection, and not a claim that the emitted schema
equals the intended snapshot. Eight chosen paths, not coverage. The honest framing is
"the riskiest constructs are checked for behaviour; the rest are checked for
execution."

**Cut.** Full per-engine introspection and snapshot-equality round-tripping remain
deferred.

---

## Verification log — live engines

Run against **PostgreSQL 16.15** and **MySQL 9.6.0**, both on isolated temporary data
directories. **72 passed, 2 skipped** (the two skips are the dialect-specific index
tests, each skipped on the engine that has no such construct).

Two findings from actually running the SQL, neither of which any string assertion would
have produced:

| Finding | Consequence |
|---|---|
| MySQL cannot index a `TEXT`/`BLOB`/`JSON` column without a prefix length | The emitter produced valid-looking `ADD CONSTRAINT ... UNIQUE (note)` that the server rejected. It now refuses at emit time, naming the fix. |
| A retype tested only on an empty table proves nothing | The `USING`-clause path is now exercised with rows present. |

Postgres-specific paths probed individually before being folded into the suite:
identity generation, `ALTER INDEX ... RENAME`, partial-index predicates, `text -> int`
with data, DESC index columns, and composite foreign keys. All correct.


## D41 — The tool is the origin of the schema; external migration histories are not accepted

**Decision.** A schema enters this tool as a **definition** — `CREATE TABLE` /
`CREATE INDEX` statements, or the structured editor — and is versioned here from that
point on. Change scripts are refused: `ALTER`, `DROP`, `RENAME`, `TRUNCATE`. There is no
path that ingests an existing `migrations/` folder or `pg_dump` output.

This is a **product constraint**, not a backlog item. It is stated here so the boundary
is a decision someone made rather than a gap someone forgot.

**Alternatives seriously considered.**
1. Replay `ALTER` statements as a sequential fold, so migration histories import.
2. Target `pg_dump` specifically — narrower, since the blocker there is really just
   `ALTER TABLE ONLY ... ADD CONSTRAINT`.
3. Accept `CREATE TABLE` only, and reject change scripts. ← chosen

**Reasoning — the parser was never the hard part.**

The clause mapping is now nearly free: building the emitter produced an operation
vocabulary (`ADD_COLUMN`, `RENAME_COLUMN`, `ALTER_COLUMN_TYPE`, `DROP_CONSTRAINT`, …)
that every `ALTER` form maps onto directly, and sqlglot parses all of them into
structured nodes. The reason not to do this is not cost.

**It is identity.** This tool's entire value rests on every object owning a stable
identity assigned once, at creation (D1). A rename is `same id, new name`, and that is
what makes the headline merge case work. Importing an external history breaks that in a
way no amount of parsing fixes:

* A migration history that did `DROP COLUMN a; ADD COLUMN b` and one that did
  `RENAME COLUMN a TO b` produce **identical final schemas**. Replaying the statements
  recovers the difference; but any history we did not witness — a squashed migration, a
  hand-edited dump, a schema changed outside the tool — does not. We would be *guessing*
  identity, which is precisely the rename-inference problem D22 already ruled cannot be
  done silently.
* Accepting external input means identities exist in two places and have to be
  reconciled. Drift detection, bidirectional sync, and "whose identity wins" are three
  new problems, none of which the brief asks for, all of which are harder than the one
  being solved.

**What it buys.** One unambiguous origin for every identity. The merge engine can trust
that two objects with the same id genuinely share a history, because there is no other
way for an id to come into existence. Every guarantee in the five-category conflict
taxonomy depends on that, and `D29`'s `UnalignedSnapshotsError` exists precisely to
catch the case where it is violated.

**What it costs, stated without softening.** A user with an existing production database
cannot try this on their real schema without first reconstructing it as inline
`CREATE TABLE` statements. `pg_dump --schema-only` output does not work, because
`pg_dump` emits every primary key, foreign key and unique constraint as a separate
`ALTER TABLE ONLY`. `mysqldump` happens to work, since MySQL inlines constraints — an
asymmetry that is awkward for a tool claiming dialect neutrality, and worth naming
rather than hoping nobody checks.

So the honest positioning is **greenfield-first**: design a schema here, evolve it here,
let the tool emit the migrations. It is not an adoption path for a legacy database.

**Consequence for the UX.** Refusing a change script now gives a different message from
refusing an unimplemented construct, because they mean different things to the reader:

```
line 1: ALTER describes a change to a schema, not a schema;
        only CREATE TABLE and CREATE INDEX are read
    this tool is the source of truth for the schema, so it reads schema DEFINITIONS,
    not change scripts (D41). Import the current state as CREATE TABLE statements --
    or start the schema here and let the tool generate the migrations instead
```

A user who reads "unsupported" assumes it is coming and waits. A user who reads this
knows to import differently. That distinction is the whole reason to write the boundary
down.

**When to revisit.** A real user with a real existing schema. And even then the answer is
probably **not** general `ALTER` replay — it is a one-time bootstrap importer that
reconstructs a full definition and assigns fresh identities, accepting that pre-import
history is lost. That is a smaller promise and an honest one: identity starts when the
tool starts.

**Cut.** `ALTER TABLE` ingest; migration-history import; `pg_dump` compatibility;
bidirectional sync with an external source of truth.


## D42 — Every visitor gets an isolated workspace, and nothing is authenticated

**Decision.** The deployed app hands each visitor an opaque 16-hex-character workspace
id in a cookie, and each id owns one SQLite file. Branches, commits, and merges live
inside a workspace and never cross one.

**Alternatives seriously considered.**
1. One global repo everyone shares. Simplest to build, and the demo URL is a shared
   machine — the first reviewer's `feature` branch and the second's are the same branch.
   Two people evaluating at once would silently corrupt each other's story.
2. Real accounts: sign-up, sessions, per-user repos. Correct for a product, and entirely
   beside the point being demonstrated. Auth is a solved problem that would eat a day
   and prove nothing about schema merging.
3. Cookie-scoped anonymous workspaces. ← chosen

**Reasoning.** The thing under evaluation is the merge engine, and the deployment exists
so a reviewer can drive it. Isolation is therefore a *correctness* requirement for the
demo — without it the tool appears to lose branches — while authentication is not a
requirement at all. Picking the cheapest mechanism that delivers isolation and stopping
there is the whole decision.

**Said plainly rather than implied:** the ids are unguessable, but this is not a security
boundary. Anyone holding a workspace id has that workspace. There is no login, no
authorization, no expiry, and nothing here should hold a schema anyone cares about. "It
has sessions" reads like "it has accounts", and it does not.

**What it costs.** No collaboration — the multi-engineer scenario the tool is *about* has
to be played by one person switching branches, not two people in two browsers. That is a
real gap in the demo, accepted because sharing a workspace means either auth or a URL
anyone can guess.

**One thing it forced.** A workspace id arrives from a cookie and gets concatenated into
a filesystem path, so it is validated against `^[0-9a-f]{16}$` before it ever touches
the disk (W-08). Attacker-controlled input reaching `Path()` is the ordinary way this
kind of shortcut turns into a directory traversal.


## D43 — The merge token pins the conflicting *values*, not just the conflict identities

**Decision.** The token that guards a resolution hashes each conflict's key **and** its
base/ours/theirs values. Reversal of the narrower rule shipped with D12.

**How it was found.** Building the UI. Not by a test — the existing tests all passed.

The token existed to stop a user resolving conflicts that had since changed (M-106). But
it hashed only conflict *keys*, and a key is `attribute:column:<id>:type` — it says
nothing about the value. So:

* Two branches disagree on `users.nickname`: `varchar(128)` vs `text`.
* The user opens the conflict page and clicks **take theirs**, looking at `text`.
* Before they submit, the other branch re-edits the *same attribute* to `varchar(200)`.
* The conflict set is unchanged, so the key set is unchanged, so the token still
  matches — and `varchar(200)` is applied as though the user had chosen it.

The check was guarding the wrong thing. A user is answering a question about **values**,
so the values are what the token has to pin.

**Why it only appeared now.** With a Python API the base/ours/theirs values are right
there in the caller's hands; the token is belt-and-braces. A browser turns them into text
on a page that can go stale while someone thinks. The hazard is created by the latency
between *showing* a choice and *receiving* it, which is exactly what a UI introduces and
a library does not. Building the front end was the test.

**Cost.** The token now changes when a value changes, so a user is occasionally sent back
to re-decide a conflict that "looks the same". That is the correct trade: re-deciding is
an inconvenience, applying an unseen value is a wrong schema.

Pinned by M-108, which fails against the previous implementation.


## D44 — Every edit in the UI is a commit; there is no draft state

**Decision.** Clicking *rename column* writes a commit on that branch immediately. There
is no save button, no dirty buffer, no discard-changes dialog.

**Alternatives seriously considered.**
1. Edit into a working copy, commit explicitly. Familiar from git, and honest about the
   distinction between a change and a recorded change.
2. Autosave a draft, commit on demand.
3. Commit per edit. ← chosen

**Reasoning.** Option 1 means the app owns mutable per-branch state that is not a
commit — which is a second storage model, a second concurrency story, and a whole
category of UI (unsaved indicators, navigation guards, conflict-on-discard). All of it
would exist to defer a write that costs nothing here: a commit is a JSON snapshot, and
the history panel is more useful than a save button.

It also makes the history real rather than decorative. A reviewer clicking through the
demo produces a genuine commit DAG, which is what the merge base is computed over.

**What it costs.** Noisy history — nine commits for what a person would call one change,
and no way to squash them. A real product wants staging. This one wants the reviewer to
see that every edit is versioned.

**Consequence.** Post/Redirect/Get is load-bearing, not cosmetic: a reloaded POST would
be a real second commit (W-11).


## D45 — The web layer decides nothing

**Decision.** Routes parse HTTP and render; every judgement — what a diff means, whether
a merge conflicts, whether a migration is safe — happens in `engine`. The architecture
test now enforces the direction: `model` and `engine` may not import `web`.

**Reasoning.** The temptation in a take-home is to put the interesting logic where the
demo is. The cost shows up immediately: anything decided in a route needs a browser to
test, and `make test` stops being the two-second loop that gets run on every save.

The acknowledgement flow is the clearest instance. The obvious implementation is for the
view to check `plan.destructive` and hide the SQL. Instead the route calls `plan()`
*without* acknowledgement and renders the warning from the `UnacknowledgedRiskError` it
raises — so the screen is driven by the engine's own refusal (D19) rather than by a
second, independently-wrong copy of the rule in a template.

The web tests (W-01…W-28) cover only what HTTP can get wrong: cookies, redirects, form
parsing, error rendering, workspace isolation, and stale tokens.


## D46 — The deployed URL opens onto a working demo, not an empty form

**Decision.** `GET /` creates a workspace that already contains the scenario — a seeded
schema plus four branches holding two engineers' divergent work — and lands the visitor
on it with a five-step guided rail as the primary navigation. Bringing your own schema
moved to `/start`, one click away.

**Alternatives seriously considered.**
1. Landing page explaining the tool, with a "try it" button. One click before anything
   happens, and the click competes with a wall of text.
2. Empty workspace with the sample DDL pre-filled in a textarea (what shipped first).
3. Land straight in a pre-diverged demo. ← chosen

**Reasoning.** The delivery mechanism is a URL sent to someone with no context and no
one to explain it. That makes two things true at once: the reviewer will not read
instructions, and **the headline behaviour is invisible without setup**. The claim worth
demonstrating is that a rename on one branch and a retype on the other merge cleanly —
which requires two branches, two edits, and knowing to try that specific pair. Option 2
asks for roughly six correct clicks before the first payoff, so the payoff does not
happen.

Seeding the divergence is not a shortcut around building the feature; the branches are
real commits made through the same editor a visitor uses. It removes setup, not substance.

**The rail replaces the tab bar rather than joining it.** Two navigations covering the
same four pages is worse than either alone. The steps are numbered because the demo is a
story with an order, and each one links to the exact comparison or merge worth looking at
with the parameters already filled in — `nickname-a` against `nickname-b` conflicts, and
being told so beats discovering it by trying pairs.

**Deliberately not progress tracking.** Marking steps "done" would need state the app
does not keep, and a GET is not an event: it would claim a page was understood because it
was loaded. The rail says what each step *shows* and highlights where you are.

**What it costs.** A returning visitor's cookie sends them back to their own workspace,
so the demo state is per-visitor and diverges from the tour's narration as soon as they
merge something. `Start over` exists for that. And a reviewer who wants to see the
ingest path has to notice `/start`, which is a real demotion of a real feature —
accepted because pasting DDL is the least interesting thing this tool does.

**A bug this surfaced.** Writing the "everything you can do" panel — added because "what
operations are available?" should be answerable from the screen — produced a list from
the backend's capabilities, and four of them (`set default`, `set NOT NULL`,
`add unique`, `add check`) had no form in the editor. A capability list that
over-promises is worse than none: the reviewer goes looking and finds nothing. The forms
now exist, and W-09b asserts the set of `op` values rendered on the page equals the set
the backend handles, so the two cannot drift again in either direction.


## D47 — Light, tokenised, one stylesheet, no external assets

**Decision.** A dark-first visual system driven by CSS custom properties, in a single
hand-written stylesheet with a 12-line script. No build step, no framework, no webfont
request, no CDN.

**Reasoning — the content is code.** Schema definitions and generated SQL are the reason
anyone is on the page. The semantic colours have to do work rather than decoration:
added, dropped, renamed, altered, and the four safety levels each get a hue, and because
they are tokens rather than per-page hex values, a diff tag and a migration safety pill
cannot disagree about what "destructive" looks like. That consistency is the actual UX
payload; the palette is downstream of it.

**Light, after building it dark first.** The switch was cheap because everything was a
token — but it is not a hue rotation. A mint green that reads cleanly on near-black is
illegible on white, so every semantic colour was re-picked for contrast against the
surface it actually sits on. The generated SQL keeps a near-black block: it is the
artifact the user came for, and giving it the highest contrast on the page makes it where
the eye lands.

**A theme change is the visual edit that silently breaks readability**, so it is now
tested. `tests/web/test_design.py` parses the tokens out of the real stylesheet and
asserts each one meets its WCAG floor against the surface it is used on — 4.5:1 for
text, 3:1 for incidental things like placeholders. The placeholder colour failed that
check on the first pass and was darkened.

One of those tests was wrong and is worth recording. It asserted a luminance gap between
`--add` and `--drop`, reasoning that a red and green of similar brightness are
confusable. But contrast ratio measures lightness, not hue, and satisfying it would have
distorted the palette to serve a metric that was never the requirement. The real
requirement is that **colour is redundant** — every coloured thing also carries a word,
which is what makes the diff readable in greyscale and to a colour-blind reader. The test
now pins that instead: the change-kind and safety vocabularies must be exhaustive, since
a kind with no wording would fall back to colour alone.

**No external font, deliberately.** A Google Fonts link is a render-blocking request to
a third party. On a cold free-tier instance that is a reviewer looking at an unstyled
page while `fonts.gstatic.com` resolves — the worst possible first impression, for a
typeface nobody asked about. The system stack renders instantly everywhere.

**What the no-external-assets rule bought, unexpectedly.** Serving one stylesheet and
one script from our own origin and nothing else means the Content-Security-Policy can be
the strict version — `default-src 'none'` with no `'unsafe-inline'` anywhere — rather
than the usual compromise. That required removing every inline `style=` attribute and
every inline `onchange`/`onsubmit` handler from the templates, which is why the two
behaviours needing JavaScript now hang off `data-navigate` and `data-confirm` and live in
`static/app.js`.

Two tests keep it that way: one asserts the header is strict, the other greps the
templates for inline styles and handlers. The first catches the symptom, the second
catches the cause — and the cause is what regresses, because adding `style="margin:0"`
to fix a spacing bug is a thing anyone would do without thinking.

**Also tightened while in there.** Error text is a deliberate part of this product —
`DDLError` renders line numbers and a hint, `SchemaError` explains that a column name is
taken — so those keep going to the page. But the routes had been catching `KeyError` and
rendering `str(e)`, which is a different thing: an internal id or a driver path reaching a
response. Unexpected exceptions now go to a handler that logs the traceback server-side
and returns a fixed sentence.

**What it costs.** One theme, chosen — no toggle, and no `prefers-color-scheme` support,
so a reader who wants dark does not get it. Adding it back is a second token block rather
than a redesign, which is the point of tokenising, but it is not free: every pair in the
contrast test would need checking twice. And a hand-written system means no component
library to lean on, so anything genuinely complex (a commit graph, a resizable diff)
would be real work rather than an import.


## D48 — The dialect layer owns the type vocabulary, and refuses what it cannot express

**Decision.** A type name is validated against the target engine's vocabulary in two
places: the emitter refuses to render an unknown one, and the web editor rejects it at
the moment it is typed. The canonical model stays permissive.

**How it was found.** A reviewer typed `somethign` into the type box and the tool
accepted it. End to end:

```
model accepts: somethign
ALTER TABLE "t" ALTER COLUMN "x" TYPE somethign USING "x"::somethign;
```

DDL that this tool generates happily and no server will run.

**Reasoning — the permissiveness is right, the passthrough was not.** `ColumnType.parse`
accepting any name is deliberate: the model is dialect-neutral (D5) and carries more than
any single engine (D6), so it cannot know that `jsonb` is fine and `somethign` is not.
Tightening it there would mean teaching the model about dialects, which is the one thing
D5 forbids.

The bug was `TYPES.get(t.base, t.base)` in the emitter — a one-word fallback that turned
"I don't recognise this" into "emit it verbatim and hope". The emitter is precisely the
layer that knows what a given engine can express, so it is the layer that must refuse
(D39). It now does, and names the alternatives.

**Why validate twice.** The emitter is the correctness boundary; the editor is the
usability one. Catching it only at emission means a typo made on the schema page surfaces
three screens later as a failed migration, with nothing pointing at the column that
caused it. Catching it only in the editor would leave the API-level hole open. They are
different guarantees, so both exist.

**The case that matters more than typos.** `blob` on Postgres and `jsonb` on MySQL are
not mistakes — they are real types the *other* engine has. A schema authored for one and
targeted at the other hits this, and the message has to distinguish "no such type
anywhere" from "not in this engine". Pinned by E-91.

**Consequence for the UI.** The accepted vocabulary is now published on the page — a
`<datalist>` on every type input, so it is discoverable by typing, plus the list written
out under the editor. Being told no is a poor way to learn what a tool accepts.


## D49 — Edits return to where they were made, and column editing is one form

**Decision.** Two changes to the schema editor, both reported from actual use:

1. Every edit redirects back to the table it was made on — `?open=<table>#t-<table>` —
   so the editor is still expanded and the browser scrolls to it.
2. Rename, retype, default and nullability collapsed from four forms with four column
   pickers into **one form per column**, opened from that column's own row. Adding
   things is grouped into four tabs: Column, Index, Constraint, Table.

**The scroll problem was self-inflicted.** Post/Redirect/Get is correct and load-bearing
(D44): without it a reload re-commits. But a redirect returns you to the *top* of the
page, and with several tables the one you were working on is off-screen. Creating a table
was the worst case — the thing you just made is the thing you are thrown away from.

The fix is that the redirect knows the subject of the edit. A fragment scrolls the
browser; an `open=` parameter re-expands the disclosure, since HTML `<details>` state does
not survive navigation. Both are needed and neither is enough alone.

Three cases only look like edge cases until they are wrong: a **rename** must anchor to
the name that exists *after* the edit or it points at nothing; a **drop** must anchor to
nothing at all; and a **rejected** edit must come back in place too — losing your
position is worse on failure, because you then have to find the form again to correct it.
W-38…W-42.

**The scattering was a modelling leak.** The editor had nine stacked forms per table
because the *engine* has nine operations. But `rename_column` and `set_nullable` are
distinct operations for a reason that matters to the merge engine and not at all to a
person: someone changing a column thinks "change this column", once. Four forms, each
re-asking "which column?", is the data model showing through the UI.

So the operations stay distinct underneath — a rename is still a rename, which is the
entire premise of this tool — and the UI presents one form that diffs its input against
the current column and applies only what actually changed. One action, one commit, and a
message naming the real edits: `users.nickname: rename to handle, type to varchar(200)`.

**The checkbox trap, since it bites everyone once.** An unchecked checkbox submits
nothing, which is indistinguishable from a caller that never offered the field — so a
form omitting `nullable` would silently add `NOT NULL` to every column it touched. A
hidden `nullable_field` marker distinguishes "answered no" from "did not ask" (W-46).

**Tabs without JavaScript.** The CSP forbids inline script (D47), and a tab strip does
not need a framework: radio inputs plus sibling selectors. Class-based rather than
id-based, because the ids carry a table uuid and CSS cannot be written per instance.

**What it costs.** Grouping hides things behind a tab, so the four operations no longer
visible at a glance rely on the "Everything you can do" panel to be discoverable — the
reason that panel exists. And the parity test needed loosening: four handlers now have no
form of their own, so they are declared in a `SUBSUMED` constant the test checks against,
which keeps "unreachable" and "deliberately combined" from being the same state.


## Open risks

| Risk | Mitigation |
|---|---|
| sqlglot can't cleanly round-trip real-world `CREATE TABLE` with constraints and indexes | Day-one spike, before the model is built. Failure means replanning the same day, not on day three. |
| Frontend overruns and compresses deploy/docs | Pre-committed cut order (below) so the decision isn't improvised under pressure. |
| Free-tier terms have changed | Verify day one. Architecture is vendor-independent. |
| Conflict taxonomy grows past five | Treated as evidence of a modeling error, investigated rather than patched. |
| MySQL `lower_case_table_names` differs between the dev machine (macOS default 2) and the CI container (Linux default 0), and can only be set at server init | Pin it explicitly in the Docker fixture; `P-34b` asserts canonical folding is applied regardless of the server setting. |
| sqlglot round-trips `CREATE TABLE` but loses fidelity on constraints, defaults, or index definitions | The day-1 gate is specifically `P-01` + `P-23`, not "does sqlglot parse SQL". |

**Pre-committed cut order**, from the bottom: third dialect (already gone) → MySQL
*ingest*, keeping the MySQL emitter since that's what proves the core is neutral →
structured editor breadth → branch graph.

Never cut: identity-based diff, three-way attribute merge, post-merge integrity
validation, merge-base computation, rename inference, round-trip verification.
