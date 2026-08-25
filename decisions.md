# Decisions

## The brief

**The problem.** Teams branch application code without thinking and then, the moment two
people touch the database, fall back to coordinating by hand in Slack. This builds the
missing half: branch, diff, and merge for a *relational database schema* itself. It is for
a small team of backend engineers working in parallel branches against one database
(Postgres or MySQL), who want to know what diverged and get a migration out the other end.
Row data is out of scope — the artifact under version control is the schema.

**The hard part.** The obvious implementation diffs schemas *by name*, and it is wrong in a
way that costs you data. If one engineer renames `users.email` to `contact_email` and
another widens `users.email` to `text`, a name-keyed diff sees a drop plus an add on one
side and a modify on the other: it reports a false conflict, or silently drops the widening.
Text-diffing the DDL is worse — it cannot distinguish a rename from a drop-and-recreate at
all, and a merged file is not guaranteed to be a schema any database will accept. This
matters more than a bad merge in source code, because the output runs as a migration
against production data: a plausible-but-wrong answer is the expensive one. The fix is
identity — every object carries a stable id, and references are stored by id, never by name
(D1, D30). A rename becomes "same id, new name", an attribute like any other, and the
example above merges cleanly with both intents preserved.

**The slice.** One path, end to end: ingest DDL → branch → evolve both branches through a
structured editor → see a semantic diff → three-way merge with conflict resolution →
emit a migration for the target dialect. The failure case taken seriously is the *clean
merge that produces an invalid schema*: one branch adds a foreign key, the other drops the
table it points at. Neither edit conflicts with the other, and no per-attribute merge can
see the problem — so the merged result is validated globally and refused as a dangling
reference rather than committed.

**Why this framing.** Two narrower readings were available. One is a smarter text diff for
`.sql` files — quick to demo, but it never confronts the rename problem, which is the only
genuinely hard thing here. The other is a migration-file manager, a friendlier Flyway or
Alembic, which is mostly workflow plumbing around a solved core. Modelling the schema as
identified objects is more work and forces real trade-offs to be made in the open — what a
conflict *is*, what a merge may refuse, what a dialect cannot express — and those decisions
are what the rest of this document records.

---

A running log of the real calls made building this. Not a changelog — the reasoning,
the alternatives, and the things deliberately left out.

Entries are roughly chronological. Where a later decision reversed an earlier one, the
reversal is recorded in place rather than edited away — see also [Reversals](#reversals).

---

## Contents

**Model and merge** — [D1](#d1--the-versioned-artifact-is-a-canonical-schema-model-with-stable-object-identity-not-ddl-text) object identity · [D2](#d2--commits-store-full-snapshots-not-operation-logs) snapshots · [D3](#d3--three-way-merge-operates-per-attribute-not-per-object)
per-attribute merge · [D4](#d4--ordered-column-lists-are-atomic-values-never-element-wise-merged) ordered lists · [D11](#d11--the-conflict-taxonomy-is-a-closed-set-of-five-categories) conflict taxonomy ·
[D13](#d13--merge-base-is-computed-as-a-lowest-common-ancestor-over-the-commit-dag-not-read-from-a-stored-branch-point) merge base · [D14](#d14--multiple-merge-bases-criss-cross-histories-are-detected-and-refused-not-approximated) criss-cross · [D15](#d15--the-emitted-migration-is-difftarget_head-merged_result-not-a-rendering-of-the-merge) what the migration is ·
[D27](#d27--column-ordering-within-a-table-is-not-semantically-modeled) column order · [D29](#d29--diff-requires-a-shared-identity-lineage-comparing-independent-snapshots-is-a-separate-operation) alignment · [D30](#d30--every-reference-between-objects-is-by-id-never-by-name) references are ids

**Dialects and types** — [D5](#d5--dialect-neutral-core-with-adapters-at-the-edges-reversal) neutral core · [D6](#d6--the-canonical-type-model-is-a-superset-with-per-dialect-representability-not-a-lowest-common-denominator) superset type model ·
[D7](#d7--parse-with-sqlglot-rather-than-hand-rolling-backend-is-therefore-python) sqlglot · [D28](#d28--sqlglot-day-1-gate-passed-with-four-things-we-now-own) spike findings · [D32](#d32--check-constraints-record-the-columns-they-read-by-id-when-the-caller-knows-them) CHECK references ·
[D33](#d33--foreign-key-targets-are-validated-for-uniqueness-at-merge-time) FK targets · [D39](#d39--constructs-a-target-engine-cannot-express-are-refused-never-approximated) refusals · [D48](#d48--the-dialect-layer-owns-the-type-vocabulary-and-refuses-what-it-cannot-express) type vocabulary

**Emit and migrate** — [D16](#d16--the-emitter-routes-through-temporary-names-when-an-intermediate-state-would-collide) temp names and ordering · [D18](#d18--mysql-migrations-are-emitted-as-individually-safe-steps-with-explicit-no-rollback-markers) MySQL steps and
safety classification · [D20](#d20--rollback-generation-is-in-scope-because-row-data-is-out-of-scope-later-reversed) rollback, in scope then cut · [D38](#d38--live-verification-is-execution-only-introspection-is-deferred) live
verification

**Ingest and scope** — [D21](#d21--ingest-rejects-unsupported-constructs-loudly-with-line-numbers) loud rejection · [D22](#d22--rename-inference-on-import-is-heuristic-plus-human-confirmation-never-silent) rename inference ·
[D24](#d24--structured-editor-scoped-to-exactly-the-operations-the-brief-enumerates) editor scope · [D26](#d26--what-is-deliberately-cut-consolidated) what is deliberately cut · [D41](#d41--the-tool-is-the-origin-of-the-schema-external-migration-histories-are-not-accepted) the tool is
the origin of the schema

**Storage and app** — [D8](#d8--live-migration-execution-lives-in-ci-not-in-the-product) CI verification · [D9](#d9--free-tier-deployment-one-container-ephemeral-disk-no-database-reversal) deployment · [D51](#d51--one-hosted-database-partitioned-by-workspace) hosted database · [D52](#d52--a-remote-database-makes-round-trip-count-a-design-constraint) latency ·
[D10](#d10--the-apps-datastore-has-nothing-to-do-with-the-dialects-it-versions) datastore · [D12](#d12--conflicts-are-resolved-atomically-on-one-screen-no-persisted-mid-merge-state) atomic conflict resolution · [D25](#d25--optimistic-concurrency-on-branch-heads) optimistic
concurrency · [D34](#d34--snapshots-are-persisted-as-opaque-json-blobs-not-normalized-into-tables) blob storage behind a `Store` seam

**Web and UI** — [D42](#d42--every-visitor-gets-an-isolated-workspace-and-nothing-is-authenticated) workspaces and the front door · [D43](#d43--the-merge-token-pins-the-conflicting-values-not-just-the-conflict-identities) merge token ·
[D44](#d44--every-edit-in-the-ui-is-a-commit-there-is-no-draft-state) commit per edit · [D45](#d45--the-web-layer-decides-nothing) the web layer decides nothing · [D47](#d47--light-tokenised-one-stylesheet-no-external-assets)
design system and editor ergonomics

Also: [Reversals](#reversals) · [Open risks](#open-risks)

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
means replaying from genesis. Snapshots are self-describing — reading any commit costs one
lookup — and identity (D1) already gives renames for free without needing the log. This is
git's tree model, for the same reason.

**Tradeoff accepted.** Storage is O(schema size) per commit rather than O(delta). At this
scale that's irrelevant, and content-addressing the snapshots recovers most of it anyway
if it ever matters.

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

**Decision.** The canonical model can express constructs that not every dialect supports.
Each dialect adapter declares what it can represent. Emitting something a target dialect
can't express produces a *report*, not a crash and not a silent substitution.

**Alternatives.** Restrict the canonical model to the intersection of all supported
dialects.

**Reasoning.** The intersection is close to useless. It excludes Postgres partial indexes,
arrays and `TIMESTAMPTZ`, and MySQL unsigned integers and prefix indexes — which is to say
it excludes most of what's actually in a production schema. A schema VCS that can't
represent your schema is not a schema VCS.

The interesting consequence is that "unrepresentable" becomes a first-class result. MySQL
`UNSIGNED INT` emitted to Postgres has no equivalent type; the honest output is the closest
substitution (`integer` plus `CHECK (col >= 0)`) *flagged as an approximation*, rather than
a silent downgrade.

This is also where the real difficulty of multi-dialect schema tooling lives, and it is
worth being concrete: MySQL accepts `BOOLEAN` but stores it as `TINYINT(1)`, which does not
constrain values to 0/1; `TEXT` is unbounded in Postgres but capped at 65,535 bytes in
MySQL; `DATETIME` / `TIMESTAMP` / `TIMESTAMPTZ` have materially different timezone
semantics per engine; and MySQL prefix indexes (`KEY(url(64))`) have no direct Postgres
equivalent — an expression index on `LEFT(col, n)` is the nearest functional one, and only
helps when a query repeats that exact expression. Deciding when two types are "the same
object with a changed attribute" versus "a different type" is a judgment call the diff
engine's correctness rests on.

**Cut.** Collation and charset modeling. Genuinely fiddly, and its main manifestation is
false diffs — which I avoid entirely by not modeling it rather than modeling it badly.

---

## D7 — Parse with `sqlglot` rather than hand-rolling; backend is therefore Python

**Decision.** DDL parsing and dialect-aware type extraction via `sqlglot`. The backend is
Python because of it.

**Alternatives.** (a) TypeScript end-to-end with `node-sql-parser` — one language, simpler
repo and deploy, lower parser fidelity. (b) Hand-write two DDL grammars.

**Reasoning.** Hand-rolling two DDL parsers in a five-day build means shipping two parsers
and no merge engine. That is the single most likely way this project fails, so the parser
must be a library, and the best multi-dialect option is Python. A language choice falling
out of a library choice is worth recording as a decision rather than letting it look like a
default.

Explicitly *not* a concern: that sqlglot hollows out the depth. It supplies parsed type
nodes. It does not do identity tracking, semantic diff, three-way merge, merge-base
computation, representability reporting or migration ordering — all of which stay
hand-built, as do the canonical type model and its normalization rules. I just don't also
write two SQL grammars.

**Tradeoff accepted.** Two languages in the repo, paid down with a single `make dev`.

**Risk gate.** Hour one is a spike against ugly real-world DDL from both engines (D28). If
sqlglot can't cleanly round-trip a `CREATE TABLE` with constraints, defaults and indexes, I
need to know before the model is built, not on day three.

---

## D8 — Live migration execution lives in CI, not in the product

**Decision.** The application generates migrations and never executes them. Verification
happens in CI as a round-trip property test: apply the generated plan to a throwaway schema
in a real engine, re-introspect it, assert the result equals the snapshot we intended.

**Alternatives.** Run real Postgres and MySQL alongside the app so users can apply a
migration and see it succeed.

**Reasoning.** The *value* of executing generated DDL is proving the emitter is correct,
and CI is where that proof belongs — it runs on every commit, against both engines, and it
catches emitter bugs, ordering bugs and type-normalization bugs in one assertion. Running
two database engines in production to serve a preview pane buys demo theatre and an
operational burden. This is the answer to "tests that catch real problems", and the
highest-value test in the repo.

**Downstream consequence.** Because nothing in the product needs a database engine, a
background worker or a long-running process, the app is deployable on a free serverless
tier. That wasn't the motivation, but it's why the constraint in D9 was satisfiable at all.

---

## D9 — Free-tier deployment: one container, ephemeral disk, no database (reversal)

> **Partly superseded by [D51](#d51--one-hosted-database-partitioned-by-workspace). The
> storage half of this decision no longer holds:** schemas now live in a hosted database
> and survive a restart. The reasoning below is left as written because it was sound on
> its own terms and the thing that changed was a requirement, not an argument — the app
> has to stay usable, with work intact, for a month of review rather than for one sitting.
> Everything here about the container, the cold start and the health check still stands.

**Decision.** A single Docker container on Render's free plan. No managed database, no
persistent volume, no serverless functions. `render.yaml` is committed so the deployment is
reproducible from the repo rather than from a sequence of dashboard clicks.

**What this reverses.** The original D9 chose serverless Python plus Turso (managed libSQL),
and rejected always-on container tiers *specifically because of cold start* — "a reviewer
clicks the link exactly once, and a 50-second blank page reads as broken". The target
chosen here has that cold start: Render suspends free services after 15 minutes idle and
takes about a minute to wake. The original reasoning was not wrong about the cost; it was
wrong about what the app needs.

**Reasoning.** Two facts inverted it.

The first is that this app cannot be serverless as built. `open_repo` is called per request
and the SQLite file *is* the state — that is what keeps the web layer stateless and lets two
workers behind one URL see the same repo. A serverless host with no durable filesystem
between requests breaks that on the second click, not at some scale limit. Making it
serverless means porting the `Store` seam to a network database first (D34), which is real
work justified by nothing except the host.

The second is that the durability the original decision was buying is not needed.
Workspaces are anonymous, per-visitor and throwaway by decision (D42). Losing them on a
redeploy costs nobody anything, and a returning visitor whose workspace is gone lands on a
fresh demo rather than an error. Free instances cannot mount a persistent disk, and that
turns out to be a constraint the design already satisfied for unrelated reasons.

So the trade became: pay a one-minute cold start, or port the storage layer to keep a
property nothing depends on. The cold start is cheaper, and unlike the port it can be
handled honestly — the README says the first load is slow and why. A reviewer told about a
free-tier spin-up reads it as a sensible constraint; one who is not told reads it as broken.

**Alternatives.** (a) Turso, per the original plan — free, no card, no disk needed, and the
`Store` seam exists precisely to allow it. Rejected for now as effort spent on infrastructure
rather than on the problem, but it is the one option that would also *prove* the seam is real
rather than decorative, which is a fair argument for doing it later. (b) A persistent volume:
effectively unavailable free — Render's free plan forbids disks outright, and Fly now
requires a card before a volume can be attached. (c) Free managed Postgres: Render's expires
30 days after creation, which is the wrong property for a link on a job application.

**What it costs, said plainly.** The first request after idle takes about a minute. Work is
lost when the container is recycled. Neither is mitigated by self-pinging, which Render
treats as abnormal traffic originating from the service and grounds for suspension — a dead
link is worse than a slow one.

**One thing the deployment forced into the code.** A `/healthz` route, because `/` is not
idempotent: it mints a workspace and sets a cookie (D46), so pointing a platform health
check at it would write a SQLite file per probe — a disk fill driven entirely by the
platform's own monitoring. `W-54` asserts probing it creates no workspace, and fails if the
route is pointed back at `/`.

**Unchanged from the original.** "All features usable" still holds because of D8: no feature
is gated behind infrastructure a free tier lacks.

**Cut.** Anything needing a persistent process: background jobs, websockets, scheduled work.

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
dangling foreign keys, indexes on dropped columns, nullable primary key columns, duplicate
names within a scope.

**Reasoning.** The largest scope risk in a merge engine is the conflict taxonomy growing
without bound. Declaring it closed makes that risk visible: if a sixth category shows up
during the build, that's evidence I mis-modeled something, not a licence to add a case.

Category 5 is not optional garnish, and one case proves it. Branch A renames `email` to
`contact`; branch B adds a new column named `contact`. Pairwise merge sees no conflict at
all — different UUIDs, disjoint attribute sets, nothing overlaps. Only validation of the
merged result catches the duplicate name. A tool without this pass reports a clean merge and
emits DDL that fails.

**D31 is folded in here.** It fixed how a category-2 conflict is *keyed*: on
`min(ours_id, theirs_id)`, with the other id carried as `other_id`, rather than on "ours" as
the first implementation did. Keying on "ours" means merging `A` into `B` and `B` into `A`
produce different resolution keys for the same conflict — and since resolutions are submitted
from a browser as opaque strings, that is a live bug the moment anyone merges in both
directions. Found by the `M-91` commutativity property, not by a hand-written test; I would
not have thought to check it. Conflicts are *pairs*, and anything derived from a pair has to
be symmetric or it is not really about the pair.

---

## D12 — Conflicts are resolved atomically on one screen; no persisted mid-merge state

**Decision.** A conflicted merge is presented in full and submitted as a single resolution.
There is no half-merged state stored server-side.

**Alternatives.** Model a git-style index — persist partial resolutions so a user can
resolve incrementally, leave, and return.

**Reasoning.** Persisting mid-merge state means modeling it, recovering it after a failed
request, garbage-collecting it when abandoned, and reconciling it when the target branch
moves underneath it. That is a meaningful chunk of a five-day budget spent on state
management rather than on merge correctness.

**Tradeoff accepted, and it's a real one.** A forty-conflict merge cannot be done in two
sittings. Documented in the UI rather than discovered.

**D23 is folded in here.** It contributed the component boundary: the diff view and the
conflict resolver are one component, not two. A single schema-delta rendering primitive,
parameterized by whether it has two inputs (diff) or three (merge resolution) — because a
conflict *is* a diff with an extra column and a choice attached. Two components is roughly
two days of frontend; one is roughly one, and the frontend is the largest single cost in
this build.

---

## D13 — Merge base is computed as a lowest common ancestor over the commit DAG, not read from a stored branch point

**Decision.** Compute the LCA of the two branch heads at merge time.

**Alternatives.** Record the commit a branch was created from and use it as the merge base
forever. This is what almost every implementation of this exercise will do.

**Reasoning.** The stored branch point is correct exactly once. After `main` has been merged
into `feature`, the correct base for the next merge has advanced to the commit `main`'s tip
pointed at when that merge happened. Keep using the original branch point and every conflict
already resolved comes back and asks to be resolved again, on every subsequent merge,
forever. That specific behavior is what makes people abandon a merge tool.

**Correction, recorded because it was wrong in the first draft of this file.** I originally
wrote that the new base *is the merge commit*. Derived properly: with `main = A→B→C`,
`feature` branched at `B` to `D`, merging `main`→`feature` creates `M(D, C)`. Add `E` on
`main`. Then `ancestors(E) = {A,B,C,E}` and `ancestors(M) = {A,B,C,D,M}`, so the lowest
common ancestor is **`C`** — the tip `main` had at merge time. Not `M`, and not the branch
point `B`. A merge commit *can* be the LCA in cross-merge topologies, which is what made the
wrong version sound plausible. The decision is unaffected — what matters is that the base
advances past the branch point — but the expected *value* was wrong, and it was wrong inside
a test expectation (`L-06`), which would have produced a day-2 failure with no way to tell
whether the test or the implementation was at fault.

This is the headline depth bet: invisible when right, silently corrosive when wrong,
algorithmically real but bounded, and *demonstrable* — merge, resolve a conflict, merge
again, and show the naive implementation re-litigating what this one doesn't.

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

**D17 is folded in here.** It supplies the other half of the same problem: an emitted
plan has to be *executable*, not merely correct as a set of changes — which means
dependency ordering as well as temp-name routing.

### D17 — Migration operations are topologically sorted, with foreign-key cycles broken by phasing

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

**Reasoning.** A failed MySQL migration leaves a half-migrated schema. Presenting a MySQL
plan as though it were atomic is an actively dangerous lie. This is a small amount of work,
and it's the difference between a tool that models reality and one that models Postgres and
hopes.

**Corrected after verification — I had the mechanism wrong.** The first draft said "MySQL DDL
is not transactional", which is outdated. MySQL 8.0 introduced *atomic DDL*: the data
dictionary lives in InnoDB and each individual DDL statement is genuinely all-or-nothing,
journalled through `mysql.innodb_ddl_log`. What remains true, and is the actual reason for
this decision, is that **DDL causes an implicit commit** — it cannot run inside
`START TRANSACTION … COMMIT`, and it ends any transaction already open. So the *statement* is
atomic and the *migration* is not: fail at step 5 of 9 and steps 1–4 are committed and
unrollbackable. That sharpens the design rather than changing it — the atomic unit is
precisely one statement, so the no-rollback marker is per-statement and exact. (InnoDB only.)

Postgres by contrast allows multi-statement DDL inside a transaction, with a few exceptions
(`CREATE INDEX CONCURRENTLY`, `CREATE DATABASE`) the emitter must keep outside the
`BEGIN`/`COMMIT` block.

**D19 is folded in here.** It is the second risk annotation the emitted plan carries: how
dangerous each individual operation is, alongside where the point of no return sits.

### D19 — Operation safety is classified; version-specific rewrite prediction is not

**Decision.** Every emitted operation is classified `safe`, `lossy`, or `lock-heavy`.
Lossy operations (narrowing a `varchar`, `int` → `smallint`, dropping a column) require
explicit acknowledgment before merge.

**Alternatives.** Full lock and table-rewrite prediction — "instant in Postgres 11+,
full table rewrite in Postgres 10."

**Reasoning.** Three-way classification is cheap, useful, and derivable from the type
lattice already needed for D6. Version-specific rewrite prediction is a per-engine,
per-version knowledge base — breadth in a depth costume, and it goes stale.

---

## D20 — Rollback generation is in scope, because row data is out of scope (later reversed)

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

**D37 is folded in here — and it reverses this decision.** Rollback generation was cut
outright; the argument above is left standing so the reversal has something to reverse.

### D37 — Rollback generation is cut (reversal of D20)

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
process, and so it deploys as one container with nothing attached to it.

**Serverless → one container (D9).** The original deployment plan picked serverless
specifically to dodge container cold starts, then chose the one shape this app cannot
take: the SQLite file *is* the request-to-request state. The property that plan was
buying — durable storage — turned out to be one nothing depends on, because workspaces
are throwaway by decision (D42). Traded a one-minute cold start, documented, for not
porting a storage layer to satisfy a host.

**Frontend cost.** I initially treated the merge engine as the expensive part. Inverted:
the merge engine is pure functions over plain data, fast to write and trivially testable;
the frontend is four non-trivial surfaces and it's where the schedule actually slips.
The single diff/resolver component (D23, folded into D12) exists because of
this correction.

---

## D29 — Diff requires a shared identity lineage; comparing independent snapshots is a separate operation

**Found while implementing the diff engine**, and it is a real gap in the original design
rather than a bug in the implementation.

**The problem.** `diff` matches objects by UUID (D1). Two snapshots descended from a common
ancestor share those UUIDs, so it works. Two *independently parsed* snapshots share none — so
diffing them reports every object as dropped and re-added. Useless output, and the first thing
a user does is paste their schema twice.

**Decision.** Re-import is an **align-then-diff** operation, and alignment is its own module
(`engine/align.py`) with a deliberately narrow contract. Alignment by name is deterministic:
same name in both snapshots, same object — no judgement, no configuration, no guessing.
Alignment across a *rename* is explicitly out of scope here, because the name is precisely the
evidence that disappeared; that is rename inference (D22), it is heuristic, it requires human
confirmation, and it belongs to the import path.

**Alternatives.** (a) Make `diff` fall back to name-matching when ids don't line up. (b) Have
`diff` infer renames itself when a name vanished and a similar one appeared.

**Reasoning.** Both put a guess inside the engine that everything else trusts absolutely. If
`diff` can invent a rename, the emitter generates `ALTER TABLE … RENAME COLUMN` where it should
generate a drop plus an add — silently converting a data-destroying change into a safe-looking
one, or the reverse. The whole value of identity-based diffing is that identity is
*authoritative*. Splitting it also puts the heuristic exactly where a human can see and correct
it, which is what D22 requires anyway.

**Made loud rather than documented.** I hit this mistake myself in eight tests while building
the diff engine, which is decent evidence a library user will too. So `diff()` now guards: if
two snapshots share table **names** but no object **identities**, it raises
`UnalignedSnapshotsError` naming `align_identity` as the fix. The discriminator is name overlap
*without* id overlap — a rename preserves ids, so there is no legitimate way for two related
snapshots to share a table name and share no identity, which makes this false-positive-free
rather than heuristic. It stays silent when either side is empty, because diffing genesis
against a first commit legitimately shares nothing.

**Consequence for the tests.** The idempotency block (`F-41`–`F-45`) is now written as
`diff(a, align_identity(a, b))`, which exercises the real user flow rather than comparing two
parse results in a way no user ever would. `test_align.py` pins the boundary with a test
asserting that a renamed column is **not** inferred as a rename. And the base schema is now a
pytest fixture rather than a helper function: a helper called twice in one test returns two
unrelated snapshots, whereas a fixture is evaluated once per test, so the mistake is no longer
expressible.

---

## D30 — Every reference between objects is by id, never by name

**Decision.** Indexes, constraints and foreign keys store the UUIDs of the columns and tables
they refer to. Names are resolved for display and for DDL emission, and are never stored as a
reference.

**Alternatives.** Store the referenced *name* — what both the model and the tests originally
did, and what every DDL text reads like.

**Reasoning.** If an index stored the name of its column, renaming the column would leave the
index pointing at a name that no longer exists: the snapshot becomes internally inconsistent,
and the integrity validator (D11, category 5) can no longer tell a genuinely dangling
reference from one that is merely stale after a rename. Name-keyed references inside an
identity-based model reintroduce, one level down, exactly the problem identity (D1) exists to
solve. With ids, renaming is genuinely free: an index over a renamed column needs no updating
at all, because it never stored the name.

**How it was found.** Adversarial probing, not the test plan — which is itself the lesson. The
suite was 141 tests green while every rename silently corrupted every index and constraint
referring to the renamed object, because the model and the tests both spoke names throughout.
`tests/unit/test_references.py` is the regression suite that would have caught it.

**What it costs.** Every boundary where names meet ids needs an explicit resolution step, and
there are four. Parsing is two phases — read each statement into a draft holding names, then
resolve every name to an id, reporting each failure with its line — because a foreign key may
legally reference a table declared later in the file. The builder DSL resolves in `build()`
for the same reason. `views.py` resolves everything id-shaped back to a name once, so
templates only ever render strings and reference resolution never leaks into Jinja where it
cannot be tested. And alignment (D29) cannot adopt an identity with a one-line swap: every
index and constraint pointing at the old id has to be rewritten, which is the bulk of that
module.

**Cut.** Nothing. Structural equality resolves references back to names before comparing —
a live engine cannot hand back our UUIDs — which is what the `names` mapping threaded through
`fingerprint()` is for.

---

## D28 — sqlglot day-1 gate: passed, with four things we now own

**Outcome.** Gate passed. Structured extraction of tables, columns, types, constraints and
indexes works for both dialects; `spike/sqlglot_spike.py` is kept in the repo as the record.
Four findings changed what we build.

**1. Prefix indexes are silently corrupted — this is the important one.** MySQL
`KEY i1 (note(32))` does not parse to a prefix-index construct. sqlglot has no such concept,
so it falls back to `Anonymous(this=note, expressions=[64])` — a *function call*.
Round-tripping it emits `` INDEX `i1` (`NOTE`(32)) ``: the column identifier is upper-cased
and re-quoted as a function name. `note` becomes `NOTE`. That is precisely the silent-misparse
failure D21 exists to prevent, and it would have shipped a corrupted column reference inside
an index definition.

The parser now detects `Anonymous` nodes in index column lists and reconstructs the prefix
index explicitly, matching the mangled name back to a real column **case-insensitively** —
safe because MySQL column names are case-insensitive on every platform, so `NOTE` and `note`
cannot be two different columns. Under Postgres, where quoted identifiers *are* case-sensitive,
the same reconstruction would be unsound, so this is a MySQL-adapter-only rule. `P-39`
accordingly becomes a **round-trip fidelity** test, not a "parses to something" test.

**Generalized.** `Anonymous` is sqlglot's "I didn't recognize this" node, so any `Anonymous`
appearing where the model expects a column reference or a known default is treated as a
rejection candidate rather than passed through. Silent pass-through of `Anonymous` is the
single most likely source of quiet corruption in the ingest path.

**2. Default normalization is free on Postgres, ours on MySQL.** Postgres collapses `now()`,
`NOW()` and `CURRENT_TIMESTAMP` into one node; MySQL maps `now()` to `Anonymous`. So the
highest-floor test in the plan is real work, on exactly one dialect, via the same mechanism as
finding 1.

**3. Autoincrement is structurally asymmetric.** Postgres `bigserial` is a distinct type;
MySQL `bigint AUTO_INCREMENT` is a type plus a constraint. The canonical model stores
`(BIGINT, autoincrement=True)` and each adapter re-splits on emit — confirming the D6 type
work is real rather than a library call.

**4. Index AST shape, confirmed.** Column order survives as a genuine ordered list, and
partial-index `WHERE` is captured, so `N-06`, D4 and `P-40` are implementable as specified.

**Rejection paths confirmed implementable.** `ParseError` carries line and column; `CREATE
VIEW`, `CREATE TRIGGER`, `GENERATED ALWAYS AS` and `PARTITION BY` all land on detectable
nodes, so `P-60`–`P-64` work as loud rejections (D21).

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

---

## D34 — Snapshots are persisted as opaque JSON blobs, not normalized into tables

**Decision.** The `commits` table holds `snapshot TEXT` — the output of `Snapshot.to_dict()`
as JSON. Tables, columns, constraints and indexes are *not* given relational tables of their
own.

**Alternatives.** Normalize properly: a `tables` table, a `columns` table, join rows back into
a snapshot on read. Or a hybrid, storing the blob but projecting a few columns out for
querying.

**Reasoning.** Nothing ever queries *inside* a snapshot. Every read is "give me the whole
schema at commit X", because diff, merge and emit all operate on complete snapshots by
construction (D2). Normalizing would mean modeling the schema model a second time, in SQL,
and then owning migrations for *that* schema too — a version control tool needing its own
schema migrations to change how it stores schemas is a joke I'd rather not have to explain.
`to_dict`/`from_dict` already round-trips losslessly and is property-tested against generated
inputs (M-93), so the risky half was already covered.

**What would change my mind.** A feature needing cross-commit queries — "which commit first
added this column", "find every branch where `users.email` is nullable". That is a real
product idea and would justify projecting a searchable index alongside the blob. Not in scope
for five days, and the blob does not preclude it later.

**Cut.** Relational normalization; cross-commit querying.

**D35 is folded in here.** It puts that blob behind a narrow `Store` seam that the whole
persistence suite runs against twice, and carries D36 with it.

### D35 — Durability sits behind a `Store` seam, and the whole suite runs against every implementation

**Decision.** `Repo` owns semantics (what a commit means, what makes a head stale, how history
is walked) and delegates durability to a narrow `Store` protocol. Two implementations,
`InMemoryStore` and `SqliteStore`, and the persistence suite is parametrized over both.

**Alternatives.** Use SQLite everywhere, including tests. Or keep the dicts and add a save/load
pair.

**Reasoning.** An abstraction exercised through one implementation is not abstract. Worse, here
the in-memory store is *exactly* the one that would pass the test that matters most while
hiding the bug — a dict cannot lose a race with itself, so optimistic concurrency (D25) would
look correct while being untested. Parametrizing is three lines and holds the real database to
the same contract. Running SQLite everywhere would have been simpler, but 250 tests paying
file-I/O cost for no signal is how a suite stops being run.

**Consequence worth stating.** Moving a branch head is now a single conditional
`UPDATE ... WHERE head = ?` — the entire concurrency story, enforced by the database rather
than by a check-then-write in Python that two requests can interleave through. D25 stopped
being a design note and became a guarantee.

**D36 is folded in here.** `SqliteStore` lives in a top-level `schemavcs.storage` package, not
in `engine/`, and an architecture test asserts the neutral packages never import it. That test
forbids dialect names in `engine/` and `sqlite` is on the list; it cannot distinguish "the
engine branches on SQLite quirks" (a design failure) from "the engine's own datastore happens
to be SQLite" (fine, per D10). An exemption would have been permanent and would have covered
every future line in the package, so the *next* genuine leak would have passed silently. Moving
the datastore out cost one directory and gave a better dependency direction. The general lesson
— when a guard rail fires on something you believe is fine, the cheap fix is to weaken the
guard rail and the right fix is usually to change the code.

**Cut.** Nothing.

---

## D38 — Live verification is execution-only; introspection is deferred

**Decision.** The live suite applies generated migrations to real Postgres and MySQL and
asserts they **execute**. It does not introspect the result and compare it to the intended
snapshot.

**Alternatives.** Build an introspector per engine (`information_schema` queries reconstructing
a full snapshot), as the test plan originally assumed. Or emit, parse the output back, and
compare.

**Reasoning.** An introspector is a third component comparable in size to the emitter, and
parse-back is unavailable because we cannot read `ALTER`. Execution-only gets most of the
value for a fraction of the cost, because the bugs that matter here are execution bugs: wrong
statement order, a rename onto an occupied name, a missing cast. Every one produces
syntactically valid SQL, so string assertions cannot catch them and only a real server can.

**Stated limitation, not glossed.** These tests prove the migration *runs*. They do not prove
it produced exactly the intended schema. A statement that succeeds while doing subtly the
wrong thing would pass.

**Vindicated immediately.** The first run against a real MySQL server failed on
`ADD CONSTRAINT uq_note UNIQUE (note)` — MySQL stores `TEXT` out of line and cannot index it
without a prefix length. The generated SQL was perfectly valid-looking; 326 unit tests were
green; no string assertion would ever have found it. The emitter now refuses with a message
naming the fix. That single finding paid for the whole tier.

**D40 is folded in here**, as a thin second tier: eight tests that check the riskiest
constructs *behaved*, not merely ran. An identity column generates distinct values, a partial
index keeps its predicate, a MySQL prefix index keeps its length, a cast works on a
*populated* table, UNIQUE/FK/CHECK are actually enforced, and a rename swap moves names rather
than data. A partial index emitted without its `WHERE` executes fine and covers every row; a
prefix index emitted without its length executes fine and indexes the whole column. Both were
confirmed by deliberately breaking the emitter: each mutation passes tier 1 and fails these.
A retype on an empty table proves nothing, so `text -> int` is tested with rows present, which
is where a missing `USING` clause bites. Eight chosen paths, not coverage — the honest framing
is "the riskiest constructs are checked for behaviour; the rest are checked for execution."

**Where it runs.** PostgreSQL 16.15 and MySQL 9.6.0 on isolated temporary data directories:
72 passed, 2 skipped, the skips being the two dialect-specific index tests, each skipped on the
engine that has no such construct.

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

---

## D41 — The tool is the origin of the schema; external migration histories are not accepted

**Decision.** A schema enters this tool as a **definition** — `CREATE TABLE` / `CREATE INDEX`
statements, or the structured editor — and is versioned here from that point on. Change
scripts are refused: `ALTER`, `DROP`, `RENAME`, `TRUNCATE`. There is no path that ingests an
existing `migrations/` folder or `pg_dump` output. This is a **product constraint**, not a
backlog item, and it is stated here so the boundary is a decision someone made rather than a
gap someone forgot.

**Alternatives seriously considered.** (1) Replay `ALTER` statements as a sequential fold, so
migration histories import. (2) Target `pg_dump` specifically — narrower, since the blocker
there is really just `ALTER TABLE ONLY ... ADD CONSTRAINT`. (3) Accept `CREATE TABLE` only,
and reject change scripts. ← chosen

**Reasoning — the parser was never the hard part.** The clause mapping is nearly free:
building the emitter produced an operation vocabulary (`ADD_COLUMN`, `RENAME_COLUMN`,
`ALTER_COLUMN_TYPE`, …) that every `ALTER` form maps onto directly, and sqlglot parses all of
them into structured nodes. The reason not to do this is not cost. **It is identity.**

This tool's value rests on every object owning a stable identity assigned once, at creation
(D1). Importing an external history breaks that in a way no amount of parsing fixes:

* A history that did `DROP COLUMN a; ADD COLUMN b` and one that did `RENAME COLUMN a TO b`
  produce **identical final schemas**. Replaying the statements recovers the difference; any
  history we did not witness — a squashed migration, a hand-edited dump, a schema changed
  outside the tool — does not. We would be *guessing* identity, which is precisely the
  rename-inference problem D22 already ruled cannot be done silently.
* Accepting external input means identities exist in two places and have to be reconciled.
  Drift detection, bidirectional sync and "whose identity wins" are three new problems, none
  of which the brief asks for, all harder than the one being solved.

**What it buys.** One unambiguous origin for every identity. The merge engine can trust that
two objects with the same id genuinely share a history, because there is no other way for an
id to come into existence. Every guarantee in the five-category conflict taxonomy depends on
that, and D29's `UnalignedSnapshotsError` exists to catch the case where it is violated.

**What it costs, stated without softening.** A user with an existing production database
cannot try this on their real schema without first reconstructing it as inline `CREATE TABLE`
statements. `pg_dump --schema-only` output does not work, because `pg_dump` emits every
primary key, foreign key and unique constraint as a separate `ALTER TABLE ONLY`. `mysqldump`
happens to work, since MySQL inlines constraints — an asymmetry that is awkward for a tool
claiming dialect neutrality, and worth naming rather than hoping nobody checks. So the honest
positioning is **greenfield-first**: design a schema here, evolve it here, let the tool emit
the migrations. It is not an adoption path for a legacy database.

**Consequence for the UX.** Refusing a change script gives a different message from refusing
an unimplemented construct, because they mean different things to the reader. "Unsupported"
tells a user the feature is coming and to wait; naming the boundary — *this tool reads schema
definitions, not change scripts; import the current state as `CREATE TABLE` statements, or
start the schema here and let the tool generate the migrations* — tells them to import
differently.

**When to revisit.** A real user with a real existing schema. Even then the answer is probably
**not** general `ALTER` replay — it is a one-time bootstrap importer that reconstructs a full
definition and assigns fresh identities, accepting that pre-import history is lost. A smaller
promise, and an honest one: identity starts when the tool starts.

**Cut.** `ALTER TABLE` ingest; migration-history import; `pg_dump` compatibility;
bidirectional sync with an external source of truth.

---

## D42 — Every visitor gets an isolated workspace, and nothing is authenticated

**Decision.** The app hands each visitor an opaque 16-hex-character workspace id in a cookie,
and each id owns one SQLite file. Branches, commits and merges live inside a workspace and
never cross one.

**Alternatives seriously considered.** (1) One global repo everyone shares — simplest to
build, but the demo URL is a shared machine, so the first reviewer's `feature` branch and the
second's are the same branch and two people evaluating at once silently corrupt each other's
story. (2) Real accounts: correct for a product, entirely beside the point being demonstrated,
and a day spent proving nothing about schema merging. (3) Cookie-scoped anonymous workspaces.
← chosen

**Reasoning.** The thing under evaluation is the merge engine, and the deployment exists so a
reviewer can drive it. Isolation is therefore a *correctness* requirement for the demo —
without it the tool appears to lose branches — while authentication is not a requirement at
all. Picking the cheapest mechanism that delivers isolation and stopping there is the whole
decision.

**Said plainly rather than implied:** the ids are unguessable, but this is not a security
boundary. Anyone holding a workspace id has that workspace. There is no login, no
authorization, no expiry, and nothing here should hold a schema anyone cares about. "It has
sessions" reads like "it has accounts", and it does not.

**What it costs.** No collaboration — the multi-engineer scenario the tool is *about* has to be
played by one person switching branches, not two people in two browsers. A real gap in the
demo, accepted because sharing a workspace means either auth or a URL anyone can guess.

**One thing it forced.** A workspace id arrives from a cookie and gets concatenated into a
filesystem path, so it is validated against `^[0-9a-f]{16}$` before it ever touches the disk
(W-08). Attacker-controlled input reaching `Path()` is the ordinary way this kind of shortcut
turns into a directory traversal.

**D46 is folded in here.** It decides what the front door actually opens onto — a workspace
that already contains the scenario, rather than an empty form.

### D46 — The deployed URL opens onto a working demo, not an empty form

**Decision.** `GET /` creates a workspace that already contains the scenario — a seeded schema
plus four branches holding two engineers' divergent work — and lands the visitor on it with a
five-step guided rail as the primary navigation. Bringing your own schema moved to `/start`,
one click away.

**Alternatives seriously considered.** (1) A landing page explaining the tool, with a "try it"
button — one click before anything happens, competing with a wall of text. (2) An empty
workspace with the sample DDL pre-filled in a textarea (what shipped first). (3) Land straight
in a pre-diverged demo. ← chosen

**Reasoning.** The delivery mechanism is a URL sent to someone with no context and no one to
explain it. That makes two things true at once: the reviewer will not read instructions, and
**the headline behaviour is invisible without setup**. The claim worth demonstrating is that a
rename on one branch and a retype on the other merge cleanly — which needs two branches, two
edits, and knowing to try that specific pair. Option 2 asks for roughly six correct clicks
before the first payoff, so the payoff does not happen. Seeding the divergence is not a
shortcut around building the feature: the branches are real commits made through the same
editor a visitor uses. It removes setup, not substance.

**The rail replaces the tab bar rather than joining it.** Two navigations covering the same
four pages is worse than either alone. Steps are numbered because the demo is a story with an
order, and each links to the exact comparison or merge worth looking at with the parameters
already filled in. Deliberately *not* progress tracking: marking steps "done" would need state
the app does not keep, and a GET is not an event.

**What it costs.** A returning visitor's cookie sends them back to their own workspace, so the
demo state diverges from the tour's narration as soon as they merge something — `Start over`
exists for that. And a reviewer who wants the ingest path has to notice `/start`, a real
demotion of a real feature, accepted because pasting DDL is the least interesting thing this
tool does.

**A bug this surfaced.** The "everything you can do" panel was generated from the backend's
capabilities, and four of them (`set default`, `set NOT NULL`, `add unique`, `add check`) had
no form in the editor. A capability list that over-promises is worse than none: the reviewer
goes looking and finds nothing. The forms now exist, and W-09b asserts the set of `op` values
rendered on the page equals the set the backend handles, so the two cannot drift again in
either direction.

---

## D43 — The merge token pins the conflicting *values*, not just the conflict identities

**Decision.** The token that guards a resolution hashes each conflict's key **and** its
base/ours/theirs values. A reversal of the narrower rule shipped with D12.

**How it was found.** Building the UI. Not by a test — the existing tests all passed. The
token existed to stop a user resolving conflicts that had since changed (M-106), but it
hashed only conflict *keys*, and a key is `attribute:column:<id>:type` — it says nothing
about the value. So: two branches disagree on `users.nickname`, `varchar(128)` vs `text`;
the user opens the page and clicks **take theirs**, looking at `text`; before they submit,
the other branch re-edits the *same attribute* to `varchar(200)`. The conflict set is
unchanged, so the key set is unchanged, so the token still matches — and `varchar(200)` is
applied as though the user had chosen it. The check was guarding the wrong thing. A user
is answering a question about **values**, so the values are what the token has to pin.

**Why it only appeared now.** With a Python API the base/ours/theirs values are right
there in the caller's hands and the token is belt-and-braces. A browser turns them into
text on a page that can go stale while someone thinks. The hazard is created by the
latency between *showing* a choice and *receiving* it, which is exactly what a UI
introduces and a library does not. Building the front end was the test.

**Cost.** The token now changes when a value changes, so a user is occasionally sent back
to re-decide a conflict that "looks the same". That is the correct trade: re-deciding is
an inconvenience, applying an unseen value is a wrong schema. Pinned by M-108, which fails
against the previous implementation.

---

## D44 — Every edit in the UI is a commit; there is no draft state

**Decision.** Clicking *rename column* writes a commit on that branch immediately. No save
button, no dirty buffer, no discard-changes dialog.

**Alternatives seriously considered.** (1) Edit into a working copy and commit explicitly —
familiar from git, honest about the distinction between a change and a recorded change.
(2) Autosave a draft, commit on demand. (3) Commit per edit. ← chosen

**Reasoning.** Option 1 means the app owns mutable per-branch state that is not a commit —
a second storage model, a second concurrency story, and a whole category of UI (unsaved
indicators, navigation guards, conflict-on-discard). All of it would exist to defer a write
that costs nothing here: a commit is a JSON snapshot, and the history panel is more useful
than a save button. It also makes the history real rather than decorative — a reviewer
clicking through the demo produces a genuine commit DAG, which is what the merge base is
computed over.

**What it costs.** Noisy history — nine commits for what a person would call one change, and
no way to squash them. A real product wants staging; this one wants the reviewer to see that
every edit is versioned.

**Consequence.** Post/Redirect/Get is load-bearing, not cosmetic: a reloaded POST would be a
real second commit (W-11).

---

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

---

## D48 — The dialect layer owns the type vocabulary, and refuses what it cannot express

**Decision.** A type name is validated against the target engine's vocabulary in two places:
the emitter refuses to render an unknown one, and the web editor rejects it at the moment it
is typed. The canonical model stays permissive.

**How it was found.** A reviewer typed `somethign` into the type box and the tool accepted it,
end to end — `ALTER TABLE "t" ALTER COLUMN "x" TYPE somethign USING "x"::somethign;`. DDL that
this tool generates happily and no server will run.

**Reasoning — the permissiveness is right, the passthrough was not.** `ColumnType.parse`
accepting any name is deliberate: the model is dialect-neutral (D5) and carries more than any
single engine (D6), so it cannot know that `jsonb` is fine and `somethign` is not. Tightening
it there would mean teaching the model about dialects, which is the one thing D5 forbids. The
bug was `TYPES.get(t.base, t.base)` in the emitter — a one-word fallback that turned "I don't
recognise this" into "emit it verbatim and hope". The emitter is precisely the layer that knows
what a given engine can express, so it is the layer that must refuse (D39). It now does, and
names the alternatives.

**Why validate twice.** The emitter is the correctness boundary; the editor is the usability
one. Catching it only at emission means a typo made on the schema page surfaces three screens
later as a failed migration, with nothing pointing at the column that caused it. Catching it
only in the editor would leave the API-level hole open. Different guarantees, so both exist.

**The case that matters more than typos.** `blob` on Postgres and `jsonb` on MySQL are not
mistakes — they are real types the *other* engine has. A schema authored for one and targeted
at the other hits this, and the message has to distinguish "no such type anywhere" from "not in
this engine". Pinned by E-91.

**Consequence for the UI.** The accepted vocabulary is now published on the page, so it is
discoverable by typing. Being told no is a poor way to learn what a tool accepts.

---

## D47 — Light, tokenised, one stylesheet, no external assets

**Decision.** A visual system driven by CSS custom properties, in a single hand-written
stylesheet with a 12-line script. No build step, no framework, no webfont request, no CDN.

**Reasoning — the content is code.** Schema definitions and generated SQL are the reason
anyone is on the page, so the semantic colours have to do work rather than decoration: added,
dropped, renamed, altered, and the four safety levels each get a hue. Because they are tokens
rather than per-page hex values, a diff tag and a migration safety pill cannot disagree about
what "destructive" looks like. That consistency is the UX payload; the palette is downstream
of it. The switch from dark to light was cheap for the same reason — though not a hue
rotation: a mint green that reads cleanly on near-black is illegible on white, so every
semantic colour was re-picked against the surface it actually sits on.

**A theme change is the visual edit that silently breaks readability**, so it is tested.
`tests/web/test_design.py` parses the tokens out of the real stylesheet and asserts each meets
its WCAG floor against the surface it is used on. The placeholder colour failed on the first
pass and was darkened.

One of those tests was wrong, and is worth recording. It asserted a luminance gap between
`--add` and `--drop`, reasoning that a red and green of similar brightness are confusable. But
contrast ratio measures lightness, not hue, and satisfying it would have distorted the palette
to serve a metric that was never the requirement. The real requirement is that **colour is
redundant** — every coloured thing also carries a word, which is what makes the diff readable
in greyscale and to a colour-blind reader. The test now pins that instead: the change-kind and
safety vocabularies must be exhaustive, since a kind with no wording would fall back to colour
alone.

**What the no-external-assets rule bought, unexpectedly.** Serving one stylesheet and one
script from our own origin and nothing else means the Content-Security-Policy can be the strict
version — `default-src 'none'`, no `'unsafe-inline'` anywhere. That required removing every
inline `style=` attribute and every inline handler from the templates, which is why the two
behaviours needing JavaScript hang off `data-navigate` and `data-confirm` in `static/app.js`.
Two tests keep it that way: one asserts the header is strict, the other greps the templates for
inline styles and handlers. The first catches the symptom, the second the cause — and the cause
is what regresses, because adding `style="margin:0"` to fix a spacing bug is a thing anyone
would do without thinking. (No external font for a related reason: a Google Fonts link is a
render-blocking request to a third party, and the system stack renders instantly everywhere.)

**Also tightened while in there.** Deliberate error text still goes to the page — `DDLError`
renders line numbers and a hint — but the routes had been catching `KeyError` and rendering
`str(e)`, which is an internal id or a driver path reaching a response. Unexpected exceptions
now go to a handler that logs the traceback server-side and returns a fixed sentence.

**What it costs.** One theme, chosen — no toggle and no `prefers-color-scheme`. Adding it back
is a second token block rather than a redesign, which is the point of tokenising, but every
pair in the contrast test would need checking twice. And a hand-written system means no
component library, so anything genuinely complex (a commit graph, a resizable diff) would be
real work rather than an import.

**D49 is folded in here.** It is the same kind of call made later against the same surface:
the schema editor's nine stacked forms collapse into one form per column, and every edit
returns to where it was made.

### D49 — Edits return to where they were made, and column editing is one form

**Decision.** Two changes to the schema editor, both reported from actual use: every edit
redirects back to the table it was made on (`?open=<table>#t-<table>`), and rename, retype,
default and nullability collapse from four forms with four column pickers into **one form per
column**, opened from that column's own row. Adding things is grouped into four tabs.

**The scroll problem was self-inflicted.** Post/Redirect/Get is correct and load-bearing
(D44): without it a reload re-commits. But a redirect returns you to the *top* of the page, and
with several tables the one you were working on is off-screen — creating a table being the
worst case, where the thing you just made is the thing you are thrown away from. The fix is
that the redirect knows the subject of the edit: a fragment scrolls the browser, and an `open=`
parameter re-expands the disclosure, since `<details>` state does not survive navigation.
Neither is enough alone. Three cases only look like edge cases until they are wrong: a
**rename** must anchor to the name that exists *after* the edit; a **drop** must anchor to
nothing; and a **rejected** edit must come back in place too, because losing your position is
worse on failure. W-38…W-42.

**The scattering was a modelling leak.** The editor had nine stacked forms per table because
the *engine* has nine operations. But `rename_column` and `set_nullable` are distinct
operations for a reason that matters to the merge engine and not at all to a person: someone
changing a column thinks "change this column", once. Four forms each re-asking "which column?"
is the data model showing through the UI. So the operations stay distinct underneath — a
rename is still a rename, which is the entire premise of this tool — and the UI presents one
form that diffs its input against the current column and applies only what changed.

**The checkbox trap, since it bites everyone once.** An unchecked checkbox submits nothing,
which is indistinguishable from a caller that never offered the field — so a form omitting
`nullable` would silently add `NOT NULL` to every column it touched. A hidden `nullable_field`
marker distinguishes "answered no" from "did not ask" (W-46).

**What it costs.** Grouping hides things behind a tab, so the four operations no longer visible
at a glance rely on the "Everything you can do" panel to be discoverable. And the parity test
needed loosening: four handlers now have no form of their own, so they are declared in a
`SUBSUMED` constant the test checks against, which keeps "unreachable" and "deliberately
combined" from being the same state.

**D50 is folded in here.** It replaces the free-text type box on that same form with a picker
plus a separate size field.

### D50 — Pick the type, type the size

**Decision.** The column type is chosen from a `<select>` of what the target engine can
express. Only the size is typed, in a separate box that disappears for types that do not take
one.

**Reasoning.** D48 made the tool reject unknown types, which was necessary but left the input
honest-but-hostile: a free text box that accepts anything and then says no. If the set of valid
answers is fixed and known, offering a text box is offering a way to be wrong. The size is the
part that genuinely is open — `varchar(255)`, `decimal(10,2)` are not enumerable, so they stay
typed. Splitting them is what makes the picker possible at all; a single `<select>` of complete
type strings would need an entry per length.

**A size that means nothing is refused, not dropped.** `int(5)` is not a narrower integer in
either engine, and silently discarding the `5` would give the user something other than what
they asked for without telling them (W-50).

**The picker is a UI affordance, not a check.** A `<select>` constrains a browser, not an HTTP
client, so the server still validates the base name against the engine's vocabulary; W-52 posts
`type_base=somethign` directly to prove it. And because the picker posts a base and a size on
*every* submit, the combined column editor must reassemble them into the string it started with
— otherwise renaming a column would also "retype" it to an identical type, polluting the diff
the whole tool exists to keep honest (W-53).

**Found while building it.** A bare `varchar` — legal and unbounded in Postgres — was emitted
verbatim for MySQL, which rejects the statement. Same family as D48: the neutral model can hold
it, one engine cannot express it, so the emitter refuses and says to give it a length or use
`TEXT` (E-92).

**What it costs.** Ordered column lists for indexes and foreign keys are still typed as
comma-separated names, and the same criticism applies. A `<select multiple>` does not preserve
order, and order is significant for both (D4) — so the honest fix is a reorderable picker,
which is real work. The server validates the names, so a typo is caught; just later than it
should be.

---

## D51 — One hosted database, partitioned by workspace

**Decision.** Every visitor's repo lives in one Turso (libSQL) database, with a
`workspace` column in both tables and in every statement. The store is one
implementation, not two: libSQL is a SQLite fork, so the same class talks to a local file
in development and to a hosted database in production.

**Alternatives.** (a) Keep one SQLite file per visitor and mount a persistent disk —
unavailable on the free tiers that need no credit card, and it ties the app to a
filesystem. (b) One hosted *database* per visitor, which Turso supports and which would
preserve the existing shape exactly — rejected because creating one means calling a
provisioning API on the landing page, so the front door acquires an external dependency
that can be slow, rate-limited or down. (c) Neon Postgres, which needs no new dependency
because `psycopg` is already vendored for the live-engine tests — rejected as the larger
port: different placeholders, different upsert syntax, different integrity errors, all in
the one file where a subtle change is most expensive.

**Reasoning.** The constraint that moved was the deployment lifetime. A demo that is
opened once can lose its state between sittings; a link sent to reviewers over a month
cannot. Free tiers cannot mount a disk, so durability has to come from a network
database — and the `Store` seam (D34) existed precisely so that this would be a change of
one file rather than a change of design.

Partitioning by column rather than by database is what keeps the front door free of an
external call. It costs an isolation property: separate files could not leak into one
another, and a `WHERE` clause can be forgotten. That is a real downgrade and it is
mitigated by testing rather than by care — `V-31` to `V-33` assert that two workspaces in
one database cannot see or move each other's branches, and `V-34` reads the source and
fails if any statement in the store omits `workspace`. A forgotten scope does not crash
and does not fail a single-workspace test; it quietly serves one visitor another's
schema, which is exactly the kind of failure that needs a test rather than a comment.

This does not make workspaces a security boundary, and D42 still applies: ids are
unguessable, nothing is authenticated, and the deployment is a demo.

**What it bought, beyond durability.** The store contract suite now runs against
`sqlite3` and `libsql` both, which is what D34 claimed the seam was for and had never
actually demonstrated. It paid for itself immediately, finding three differences that
would otherwise have been found in production:

- libSQL raises `ValueError`, not `IntegrityError`, on a constraint violation — so a
  duplicate branch name would have been a 500 instead of "branch already exists".
- Its cursors are not iterable, which `branch_names` relied on.
- **Its default connection opens an implicit transaction and never commits it.** Every
  write would have been discarded when the per-request connection closed. The app would
  have started, served pages, accepted edits and persisted nothing.

The third is the one that justifies the parameterisation on its own. Nothing in the
happy-path web tests would have caught it, because within a single request the data is
there; it disappears at close.

**Cut.** Retention. Anonymous workspaces accumulate and nothing expires them. At a few KB
per visitor against a 5GB free tier this is not a month-scale problem, and a cleanup pass
is scheduled work — the one thing D9 has no way to run.

---

## D52 — A remote database makes round-trip count a design constraint

**Decision.** Three changes, all forced by measurement rather than anticipated: reuse one
connection per worker thread, cache commits in-process, and assemble a new workspace in
memory before writing it.

**How it was found.** By pointing the deployed configuration at a real Turso database and
timing it. The first measurement was the landing page at **10.2 seconds**. Every store
operation is a network round trip -- about 140ms for a read and 325ms for a write from
where this was measured -- and the code was written against a local file where an
operation costs microseconds. Nothing was wrong with it; the cost model underneath it had
changed.

| | before | after |
|---|---|---|
| landing (seeds the demo) | 10.2s | 2.3s |
| branch page | 1.7s | 0.66s |
| compare | 1.2s | 0.51s |
| merge | — | 0.28s |

**What each change was worth, in order of being tried.**

*Connection reuse* was the obvious suspect and the wrong one. A connection cost 0.67s to
establish and the app opened two per request -- `exists()` then `open_repo()` -- so
pooling them looked like the answer. It moved the branch page from 1.7s to 1.4s and left
the landing page untouched. Worth keeping, but it disproved the theory: the cost was not
connections, it was the number of statements.

*Caching commits* was the real win, and it is free of risk because a commit is immutable:
its id is a UUID minted once, and snapshots are never rewritten (D2). A cache that cannot
go stale needs no invalidation. Branch page 1.4s to 0.66s. The cache is used only for
remote stores -- a local file is already fast, and every in-process database shares the
path `":memory:"`, so caching those would key two unrelated databases together.

*Building in memory, then publishing* fixed the landing page. Seeding the demo through the
normal commit path was 48 store operations, because every commit re-reads the branch head
and compare-and-swaps it. That coordination is meaningless for a workspace that does not
exist yet and has no concurrent writer, so the history is now assembled against an
in-memory store and written once: 48 operations became 12, all of them inserts.

**Alternatives that did not work.** libSQL *embedded replicas* -- a local file that syncs
from the primary, so reads are local -- are exactly the right shape for this and the
driver refuses to open one against a current Turso backend. `executemany` is not batched:
12 rows took 3.38s versus 3.90s as separate statements, so there is no round-trip saving
to have.

**Honest caveat.** These numbers were measured from a laptop to a database in
`ap-south-1`, and the deployed app will have a different distance to cover. The *counts*
improve at any latency; the *seconds* will not reproduce. What the numbers are good for is
the ordering of the three fixes, not their absolute values.

**Cut.** Reducing the branch page below five operations. It needs a request-scoped cache
for branch heads, which unlike commits are mutable, so it trades a clear correctness
argument for a fraction of a second.

---

## Open risks

| Risk | Mitigation |
|---|---|
| sqlglot round-trips `CREATE TABLE` but loses fidelity on constraints, defaults or index definitions | Day-one spike (D28), before the model is built — gated specifically on `P-01` + `P-23`, not "does sqlglot parse SQL". Failure means replanning the same day, not on day three. |
| Frontend overruns and compresses deploy/docs | Pre-committed cut order (below) so the decision isn't improvised under pressure. |
| Free-tier terms have changed | Verify day one. Architecture is vendor-independent. |
| Conflict taxonomy grows past five | Treated as evidence of a modeling error, investigated rather than patched. |
| MySQL `lower_case_table_names` differs between the dev machine (macOS default 2) and the CI container (Linux default 0), and can only be set at server init | Pin it explicitly in the Docker fixture; `P-34b` asserts canonical folding is applied regardless of the server setting. |

**Pre-committed cut order**, from the bottom: third dialect (already gone) → MySQL
*ingest*, keeping the MySQL emitter since that's what proves the core is neutral →
structured editor breadth → branch graph.

Never cut: identity-based diff, three-way attribute merge, post-merge integrity
validation, merge-base computation, rename inference, round-trip verification.
