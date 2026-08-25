# Test Plan

A TDD specification. Every case below is written to be turned into a failing test before
the corresponding code exists. IDs are stable and referenced from commit messages.

Companion to [`scope.md`](scope.md) and [`../decisions.md`](../decisions.md). `D-nn`
references are to decisions in that file — those tests are regression guards for a
specific call, and if one starts failing, the decision is what to re-read.

---

## Testing philosophy — three opinions that shape everything below

**1. Do not assert on emitted SQL strings as the primary check.**

The obvious way to test an emitter is a golden file: "assert the output equals this SQL."
It's brittle in the worst way — it fails on whitespace and passes on semantically wrong
DDL. `ALTER TABLE users ALTER COLUMN a TYPE text` and the same statement with the wrong
table are one character apart.

Instead, the primary assertion is **round-trip semantic equality** (`R-*`): apply the
generated migration to a real engine seeded with the source schema, re-introspect, assert
the result equals the intended snapshot. That catches emitter bugs, ordering bugs, and
type-normalization bugs with one assertion, and it cannot pass on invalid DDL because
invalid DDL doesn't execute.

Golden SQL files exist, but for *human review* of readability — reviewed by eye, updated
freely, and never the thing that proves correctness.

**2. Every conflict case needs a convergent counter-case.**

A merge engine that flags everything as a conflict passes every conflict test. The
interesting failures are **false** conflicts: both branches renaming a column to the
*same* name, both dropping the same column, an already-merged branch merged again. Naive
implementations report conflicts on all three. So each of the five categories gets both
a positive and a convergent negative, and they're paired in the tables below.

**3. Test density belongs in the merge engine.**

It's pure functions over plain data — no I/O, no fixtures beyond a schema builder,
millisecond runtime. It's also where correctness actually lives. The frontend gets a
handful of journey tests (`X-*`) and no more; unit-testing React components would inflate
the count while catching nothing this product can get wrong.

---

## Test infrastructure

### The schema fixture DSL

This is the most important piece of test infrastructure, because ~150 cases below all
depend on constructing schemas readably. Designed first, deliberately.

```python
s = (schema(dialect="postgres")
     .table("users")
       .col("id", "bigint", pk=True)
       .col("email", "varchar(255)", nullable=False)
       .col("created_at", "timestamptz", default="now()")
       .index("idx_users_email", ["email"], unique=True)
     .table("orders")
       .col("id", "bigint", pk=True)
       .col("user_id", "bigint")
       .fk("fk_orders_user", ["user_id"], "users", ["id"], on_delete="cascade")
     .build())
```

Two requirements that are easy to get wrong and expensive to retrofit:

- **Identity must be addressable.** Tests need to say "the same column, renamed," which
  means grabbing a UUID: `s.col("users.email").id`. Without this, every merge test has to
  reconstruct identity by name and the tests can't distinguish rename from drop+add —
  the exact thing under test.
- **Mutation must be explicit and non-destructive.** `s.evolve().rename_col("users.email",
  "email_address").build()` returns a new snapshot preserving UUIDs. This is how branch
  divergence is expressed in every `M-*` test.

### The DAG fixture

`L-*` tests need commit graphs built by hand:

```python
dag = commits("""
    A -> B -> C          (main)
    B -> D -> E          (feature)
    C, E -> F            (merge into main)
""")
assert merge_base(dag.ref("main"), dag.ref("feature")) == dag["F"]
```

Text-defined graphs, because the merge-base bugs worth catching are topological and
unreadable when expressed as imperative commit calls.

### Real-engine fixtures

- Session-scoped Docker containers for Postgres and MySQL, started once per run
- **Each test gets its own schema/database namespace**, so the suite runs in parallel and
  a failure can't leak into the next test
- Introspection helper: live engine → canonical snapshot, which is itself covered by
  `R-01`/`R-02` before anything depends on it

### Property-based testing

Hypothesis, with a schema generator over the supported object types. Used for `R-90+`
(round-trip fuzzing) and `M-90+` (merge algebraic laws). Not used where a hand-written
case is clearer — generated cases are bad at expressing "a rename swap."

---

## 1. Canonical model & identity — `N-*`

| ID | Case | Expectation |
|---|---|---|
| N-01 | New object gets a UUID | Non-null, unique within snapshot |
| N-02 | Rename preserves UUID | `id` unchanged, `name` changed (D1) |
| N-03 | Drop + re-add same name | Two distinct UUIDs — *not* a rename |
| N-04 | Snapshot equality ignores table declaration order | Tables are a set |
| N-05 | Snapshot equality ignores column declaration order | Column order is not semantically modeled |
| N-06 | Snapshot equality respects **index column order** | `[a,b]` ≠ `[b,a]` (D4) |
| N-07 | Snapshot equality respects FK column pairing order | `(a→x, b→y)` ≠ `(a→y, b→x)` |
| N-08 | Content hash is stable across serialization round-trip | Same schema → same hash |
| N-09 | Content hash differs on any attribute change | No hash collisions on single-attribute deltas |
| N-10 | Commits are immutable | Mutation attempt raises |

`N-05` and `N-06` together are the subtle pair: column order is *not* meaningful, index
column order *is*. Getting these backwards produces either spurious diffs or silently
broken indexes.

---

## 2. Parser / ingest — `P-*`

### Happy path

| ID | Case |
|---|---|
| P-01 | `CREATE TABLE` with every supported scalar type, both dialects |
| P-02 | Inline `PRIMARY KEY` on a column |
| P-03 | Table-level `PRIMARY KEY (a, b)` — composite, order preserved |
| P-04 | `FOREIGN KEY` with `ON DELETE` / `ON UPDATE` variants |
| P-05 | `UNIQUE` constraint, inline and table-level |
| P-06 | `CHECK` constraint with an expression |
| P-07 | `NOT NULL` and explicit `NULL` |
| P-08 | `DEFAULT` with a literal |
| P-09 | `DEFAULT` with a function call |
| P-10 | `CREATE INDEX`, `CREATE UNIQUE INDEX`, composite with order |
| P-11 | Multiple statements, with and without trailing semicolon |
| P-12 | `--` and `/* */` comments interleaved between statements |
| P-13 | Empty input → empty schema, **not** an error |

### Determinism & normalization — the spurious-diff guards

| ID | Case | Expectation |
|---|---|---|
| P-20 | Parse the same DDL twice | Byte-identical snapshots (modulo UUIDs) |
| P-21 | `varchar(255)` vs `character varying(255)` | Same canonical type |
| P-22 | `int` vs `integer` vs `int4` | Same canonical type |
| P-23 | `DEFAULT now()` vs `DEFAULT CURRENT_TIMESTAMP` vs `DEFAULT NOW()` | Same canonical default (D6) |
| P-24 | Whitespace, newline, and indentation variation | Identical snapshot |
| P-25 | Keyword case variation (`create table` / `CREATE TABLE`) | Identical snapshot |
| P-26 | Quoted vs unquoted identifiers where folding makes them equal | Identical snapshot |

**P-23 is the highest-floor test in the suite.** A diff tool that reports a change on an
untouched `created_at` column gets closed and never reopened.

### Dialect-specific traps

| ID | Case | Expectation |
|---|---|---|
| P-30 | MySQL `TINYINT(1)` | Canonical boolean, not a 1-byte int (D6) |
| P-30b | MySQL `BOOLEAN` / `BOOL` | Same canonical boolean as P-30 — they are synonyms |
| P-30c | MySQL `TINYINT(1) UNSIGNED` and `TINYINT(1) ZEROFILL` | **Not** boolean — from 8.0.19 the boolean assumption excludes these |
| P-30d | MySQL `TINYINT` with no display width | Integer, not boolean |
| P-31 | MySQL `` `backtick` `` quoting | Parsed as identifier |
| P-32 | Postgres `"double quote"` quoting | Parsed as identifier, case preserved |
| P-33 | Postgres unquoted `CREATE TABLE Users` | Folded to `users` |
| P-34 | MySQL unquoted `CREATE TABLE Users` | Case preserved as declared, but folding is server-dependent — see P-34b |
| P-34b | MySQL table-name folding under `lower_case_table_names` 0 / 1 / 2 | Canonical rule applied consistently regardless of server setting; test pins the value explicitly |
| P-34c | MySQL column names differing only in case (`Email` vs `email`) | **Duplicate** — MySQL column names are case-insensitive on every platform |
| P-34d | Postgres column names differing only in case, quoted | **Distinct** — documents the divergence from P-34c |
| P-35 | Postgres `SERIAL` | Canonical autoincrement, not a bare integer |
| P-36 | MySQL `AUTO_INCREMENT` | Same canonical autoincrement as P-35 |
| P-37 | `TEXT` in Postgres vs MySQL | Different canonical types — bound differs (D6) |
| P-38 | MySQL `UNSIGNED INT` | Canonical unsigned integer |
| P-39 | MySQL prefix index `KEY(url(64))` | Canonical prefix-index construct |
| P-40 | Postgres partial index `WHERE deleted_at IS NULL` | Canonical partial index |

### Malformed input — `P-50+`

| ID | Case | Expectation |
|---|---|---|
| P-50 | Unbalanced parentheses | Error naming the line |
| P-51 | Truncated mid-statement | Error naming the line |
| P-52 | Garbage that isn't SQL at all | Error, no stack trace |
| P-53 | Duplicate table name in one input | Error naming both occurrences |
| P-54 | Duplicate column name in one table | Error |
| P-55 | FK referencing a table absent from input | Error naming the missing target |
| P-56 | Index referencing a nonexistent column | Error naming the column |
| P-57 | PK referencing a nonexistent column | Error |
| P-58 | Error messages carry a **1-based line number** matching the input | Asserted explicitly |

### Unsupported constructs — must reject, never skip — `P-60+`

| ID | Construct | Expectation |
|---|---|---|
| P-60 | `CREATE VIEW` | Specific rejection naming the construct |
| P-61 | `CREATE TRIGGER` | Specific rejection |
| P-62 | `CREATE FUNCTION` / `PROCEDURE` | Specific rejection |
| P-63 | `GENERATED ALWAYS AS` column | Specific rejection |
| P-64 | `PARTITION BY` | Specific rejection |
| P-65 | Row-level security / `POLICY` | Specific rejection |
| P-66 | `COLLATE` on a column | Specific rejection (D6 — deliberately unmodeled) |
| P-67 | A supported table adjacent to an unsupported construct | **Whole import rejected**, not partially applied |
| P-68 | Rejection message lists what *is* supported | Asserted |

**P-67 is the one that matters most in this block** (D21). Partial import is worse than
no import: the user gets a schema they believe is complete and every downstream diff and
migration is silently wrong.

---

## 3. Version store & DAG — `V-*`

| ID | Case | Expectation |
|---|---|---|
| V-01 | Init repo | Genesis commit, empty schema |
| V-02 | Commit advances branch head | New head, parent points to old |
| V-03 | Branch creates a second ref at the same commit | Both heads equal, no new commit |
| V-04 | Commit on a branch doesn't move the other | Isolation |
| V-05 | History walk from head reaches genesis | Linear case |
| V-06 | Committing an unchanged schema | Rejected as empty, or explicit no-op — asserted either way |
| V-07 | Reading any commit is a single lookup | Snapshot self-sufficiency (D2) |

---

## 4. Merge base / LCA — `L-*` — **the headline**

| ID | Topology | Expectation |
|---|---|---|
| L-01 | `LCA(A, A)` | `A` |
| L-02 | Linear: `A → B → C`, `LCA(C, A)` | `A` — ancestor case |
| L-03 | Simple fork off `B` | `B` |
| L-04 | Fork several commits deep on both sides | The fork point, not either head |
| L-05 | Fast-forward detectable: LCA equals one head | Flagged FF, three-way merge skipped |
| L-06 | **After `main` merged into `feature`, plus a new commit on `main`: `LCA(main, feature)`** | **The tip `main` had when it was merged in — advanced past the original branch point** (D13) |
| L-06b | Immediately after `main` merged into `feature`, before any new commit: `LCA(main, feature)` | `main`'s head — i.e. a fast-forward for `feature`→`main` |
| L-06c | Cross-merge: `main`→`feature`, then `feature`→`main` | A merge commit *can* be the LCA here — asserted separately so L-06 isn't over-generalized |
| L-07 | Two sequential merges in the same direction, then a new commit on the source | The source tip as of the **second** merge — the base advances again |
| L-08 | Merge in one direction, then commits on both sides, then LCA | Still the source tip as of that merge — commits after it on either side don't move the base |
| L-09 | Criss-cross: both branches merged each other | **Multiple LCAs detected, merge refused with explanation** (D14) |
| L-10 | Criss-cross refusal message names the ambiguity | Not a generic error |
| L-11 | Deep DAG, ~50 commits, several merges | Correct LCA; completes fast |
| L-12 | Wide DAG, 10 branches off one root | Correct pairwise LCAs |

### The regression that justifies the whole decision

| ID | Case | Expectation |
|---|---|---|
| **L-20** | Merge `main`→`feature` resolving one conflict. Commit on `main`. Merge `main`→`feature` again. | **The resolved conflict does not reappear.** |
| L-21 | Same, but asserted against a deliberately naive stored-branch-point implementation | The naive version *does* re-raise it — proves the test has teeth |

`L-20` is the single most valuable test in the suite. It's the behavior that separates a
merge tool people keep from one they abandon, and it passes trivially on a fresh branch —
which is exactly why it has to be written explicitly.

`L-21` is unusual and worth keeping: a test asserting that the *wrong* implementation
fails. Without it, `L-20` could pass for reasons unrelated to merge-base correctness.

---

## 5. Diff engine — `F-*`

### Happy path

| ID | Case | Reported as |
|---|---|---|
| F-01 | Add table | `create_table` |
| F-02 | Drop table | `drop_table` |
| F-03 | Add column | `add_column` |
| F-04 | Drop column | `drop_column` |
| F-05 | Change nullability | `alter_column{nullable}` |
| F-06 | Change default | `alter_column{default}` |
| F-07 | Add / drop index | `create_index` / `drop_index` |
| F-08 | Add / drop each constraint kind | Matching op |

### Rename handling — the reason identity exists

| ID | Case | Expectation |
|---|---|---|
| F-20 | Rename column | **`rename_column`**, not drop + add (D1) |
| F-21 | Rename table | `rename_table`, columns not re-reported |
| F-22 | Retype column | `alter_column{type}` |
| F-23 | **Rename + retype the same column** | **One change, two attribute deltas** |
| F-24 | Rename chain `a→b→c` across commits, diffed end to end | Single rename `a→c` (D2, free via identity) |
| F-25 | Rename swap `x→y`, `y→x` | Two renames, no drop/add |
| F-26 | Rename table *and* alter its columns | Both, columns attributed to the renamed table |
| F-27 | Drop `a`, add unrelated `b` | Drop + add — *not* inferred as a rename |

### Idempotency — the trust guards

| ID | Case | Expectation |
|---|---|---|
| F-40 | Diff a snapshot against itself | **Empty** |
| F-41 | Diff after a no-op round-trip through DDL | Empty |
| F-42 | Diff after reformatting the source DDL | Empty (leans on P-24) |
| F-43 | `varchar(255)` → `varchar(255)` spelled differently | Empty |
| F-44 | Reordered `CREATE TABLE` statements in input | Empty |
| F-45 | Reordered columns within a table | Empty (N-05) |
| F-46 | Reordered index columns | **Non-empty** — order is meaningful (N-06) |

---

## 6. Three-way merge — `M-*` — the five categories

Each category gets conflict cases *and* convergent counter-cases.

### Category 4 — auto-merge (the money cases)

| ID | Base → Ours / Theirs | Expectation |
|---|---|---|
| **M-01** | A renames `email`; B retypes `email` | **Clean merge, both applied** (D3) |
| M-02 | A changes nullability; B changes default, same column | Clean, both |
| M-03 | A adds column `x`; B adds column `y`, same table | Clean, both |
| M-04 | A adds table `t1`; B adds table `t2` | Clean, both |
| M-05 | A adds index `i1`; B adds index `i2`, same table | Clean, both |
| M-06 | A renames a table; B adds a column to it | Clean, both |
| M-07 | A renames an index; B changes its column list | Clean — different attributes (D4) |
| M-08 | A renames column `c`; B adds an index on `c` | Clean; index follows the rename |
| M-09 | A drops an unrelated column; B renames another | Clean, both |

`M-01` is the product thesis in one test.

### Category 3 — attribute conflict

| ID | Case | Expectation |
|---|---|---|
| M-20 | Both rename the same column, different names | Conflict |
| M-21 | Both retype the same column, differently | Conflict |
| M-22 | Both change the same default, differently | Conflict |
| M-23 | Both change nullability, differently | Conflict |
| M-24 | Conflict payload carries base / ours / theirs values | Asserted — the resolution UI needs all three |
| **M-25** | Both rename the same column to the **same** name | **Clean — convergent, not a conflict** |
| M-26 | Both retype to the same type | Clean |
| M-27 | Both set the same default via different spellings (`now()` / `CURRENT_TIMESTAMP`) | Clean — normalization (P-23) reaches into merge |

`M-27` is the sleeper: a normalization bug that only produces a spurious *diff* is
annoying, but the same bug produces a spurious *conflict* here, which blocks work.

### Category 1 — delete/modify

| ID | Case | Expectation |
|---|---|---|
| M-40 | A drops column; B renames it | Conflict |
| M-41 | A drops column; B retypes it | Conflict |
| M-42 | A drops table; B adds a column to it | Conflict |
| M-43 | A drops table; B renames it | Conflict |
| M-44 | A drops index; B changes its columns | Conflict |
| M-45 | **Both drop the same column** | **Clean — convergent delete** |
| M-46 | Both drop the same table | Clean |
| M-47 | A drops column; B untouched | Clean drop |

### Category 2 — name collision

| ID | Case | Expectation |
|---|---|---|
| M-60 | Both add a column named `foo` to the same table, distinct UUIDs | Conflict |
| M-61 | Both add a table named `t`, distinct UUIDs | Conflict |
| M-62 | Both add `foo` with **identical definitions** | **Conflict** — distinct identities diverge on any future rename; documents the call |
| M-63 | Both add indexes with the same name | Conflict |
| M-64 | Same-named columns added to *different* tables | Clean — scoping |

### Category 5 — integrity violation (global)

| ID | Case | Expectation |
|---|---|---|
| **M-80** | A renames `email`→`contact`; B adds a new column `contact` | **Pairwise clean, global duplicate-name violation** (D11) |
| M-81 | A drops table `T`; B adds an FK referencing `T` | Dangling FK violation |
| M-82 | A drops column `c`; B adds an index on `c` | Index-on-missing-column violation |
| M-83 | A makes a column nullable; B adds it to the PK | Nullable-PK violation |
| M-84 | A drops a unique constraint; B adds an FK targeting those columns | FK-target-not-unique violation |
| M-85 | A drops column `c`; B adds a CHECK referencing `c` | Dangling reference violation |
| M-86 | A renames table `T`; B adds an FK to `T` by old name | Clean — FKs reference identity, not name |
| M-87 | Violation payload names the specific invariant and objects | Asserted — not "merge failed" |

**M-80 is the test that proves category 5 must exist.** Pairwise merge sees nothing: two
different UUIDs, disjoint attribute sets, zero overlap. Only validating the merged result
catches it. Without this pass the tool reports a clean merge and emits failing DDL.

`M-86` is its mirror image and guards against over-correcting: renames must *not* break
references, because references are by identity.

### Merge mechanics

| ID | Case | Expectation |
|---|---|---|
| M-100 | Merge a branch into itself | No-op |
| M-101 | Merge an already-merged branch, no new commits | No-op, empty diff |
| M-102 | Fast-forward when target is an ancestor | FF, no conflict computation |
| M-103 | Merge produces a commit with **two parents** | Asserted |
| M-104 | Merge with zero conflicts auto-commits | No resolution step |
| M-105 | Partial resolution submitted | Rejected — all-or-nothing (D12) |
| M-106 | Resolution against a stale conflict set | Rejected, re-merge required |
| M-107 | Resolution choosing a **third value** (neither ours nor theirs) | Accepted (D10 alt) |

### Algebraic properties — `M-90+` (Hypothesis)

| ID | Property |
|---|---|
| M-90 | `merge(X, X) == X` for generated `X` |
| M-91 | Merging disjoint changes is commutative in *result* (conflicts may differ in presentation) |
| M-92 | A conflict-free merge result always passes integrity validation |
| M-93 | Merge result is a valid snapshot — never an intermediate/partial state |
| M-94 | Auto-merge never loses a change present on exactly one side |

`M-94` is the one that catches the ugliest class of bug: a silently dropped change.

---

## 7. Emitters — `E-*`

### Happy path, both dialects

| ID | Case |
|---|---|
| E-01 | `CREATE TABLE` covering every supported construct |
| E-02 | `DROP TABLE` |
| E-03 | Add / drop column |
| E-04 | Rename column — PG `RENAME COLUMN` vs MySQL syntax |
| E-05 | Retype column — PG `ALTER COLUMN ... TYPE` vs MySQL `MODIFY COLUMN` |
| E-06 | Set / drop `NOT NULL` |
| E-07 | Set / drop default |
| E-08 | Add / drop each constraint kind |
| E-09 | Create / drop index |
| E-10 | Rename table |

### Ordering — `E-20+`

| ID | Case | Expectation |
|---|---|---|
| E-20 | New table + FK referencing it | Table before FK (D17) |
| E-21 | Drop a table with an inbound FK | FK dropped first |
| E-22 | Add column + index on it | Column first |
| E-23 | Drop a column that appears in an index | Index dropped first |
| E-24 | Drop a column that appears in an FK | FK dropped first |
| E-25 | **Circular FK: `users.org_id` ↔ `orgs.owner_id`** | **Create both tables, then both FKs** — cycle broken by phasing |
| E-26 | Self-referencing FK (`employees.manager_id → employees.id`) | Table, then FK |
| E-27 | Three-table FK cycle | Phased correctly |
| E-28 | Rename a column that appears in an index | Index **not** spuriously recreated |
| E-29 | Deeply nested FK graph, ~50 tables | Topological sort completes, no recursion blowup |

### Intermediate-state collisions — `E-40+`

| ID | Case | Expectation |
|---|---|---|
| **E-40** | **Rename swap `x→y`, `y→x`** | **Routed via a temp name; executes** (D16) |
| E-41 | Three-way rename cycle `a→b→c→a` | Temp name; executes |
| E-42 | Table-name swap | Temp name; executes |
| E-43 | Rename `a→b` while dropping a pre-existing `b` | Drop before rename, no temp needed — minimality |
| E-44 | Rename `a→b` while adding a new `b` | Ordered so no collision occurs |
| E-45 | Temp names don't collide with existing identifiers | Asserted against an adversarial schema |
| E-46 | No temp name is emitted when none is needed | Guards against unconditional temp-routing |

`E-40` is the case a reviewer can try in fifteen seconds, and it fails on every naive
emitter — start state valid, end state valid, intermediate state invalid.

### Safety classification — `E-60+`

| ID | Operation | Class |
|---|---|---|
| E-60 | `varchar(50)` → `varchar(255)` | safe |
| E-61 | `varchar(255)` → `varchar(50)` | lossy |
| E-62 | `int` → `bigint` | safe |
| E-63 | `bigint` → `int` | lossy |
| E-64 | `int` → `text` | requires cast |
| E-65 | `text` → `int` | lossy, requires cast |
| E-66 | Drop column | lossy |
| E-67 | Add nullable column | safe |
| E-68 | Add `NOT NULL` column with no default | unsafe / lock-heavy |
| E-69 | Add `NOT NULL` column with a default | dialect- and version-dependent → lock-heavy |
| E-70 | Create index | lock-heavy |
| E-71 | Merge containing a lossy op without acknowledgment | **Rejected** (D19) |
| E-72 | Same merge with explicit acknowledgment | Proceeds |

### Engine honesty — `E-80+`

| ID | Case | Expectation |
|---|---|---|
| E-80 | Postgres plan | Wrapped in `BEGIN` / `COMMIT` |
| E-81 | MySQL plan | **Not** wrapped — stepwise (D18) |
| E-82 | MySQL plan with a destructive step | Carries a no-rollback marker at the right position |
| E-83 | MySQL marker position is the *first* irreversible step | Asserted |

### Representability — `E-100+`

| ID | Case | Expectation |
|---|---|---|
| E-100 | MySQL `UNSIGNED INT` → Postgres | `integer` + `CHECK (c >= 0)`, **flagged approximation** (D6) |
| E-101 | Postgres partial index → MySQL | Reported unrepresentable |
| E-102 | Postgres array type → MySQL | Reported unrepresentable |
| E-103 | MySQL prefix index → Postgres | **Approximation**: expression index on `LEFT(col, n)`, flagged — the planner only uses it for queries repeating that exact expression |
| E-104 | Postgres `TIMESTAMPTZ` → MySQL | Flagged approximation |
| E-105 | MySQL `TINYINT(1)` → Postgres | `boolean`, exact — not an approximation |
| E-106 | Report distinguishes *approximation* from *unrepresentable* | Asserted — different user decisions |
| E-107 | Same-dialect emit produces an empty representability report | No false warnings |

### Rollback — `E-120+`

| ID | Case | Expectation |
|---|---|---|
| E-120 | Every forward op has an inverse | Exhaustive over op kinds |
| E-121 | Inverse of drop-column restores the full definition | Type, nullability, default (D20) |
| E-122 | Inverse of a rename swap is a rename swap | Also needs temp names |
| E-123 | Inverse of a phased FK-cycle creation | Reverse phase order |
| E-124 | Forward-then-rollback restores the original snapshot | Verified live in `R-30` |

---

## 8. Round-trip verification — `R-*` — the integration backbone

The shape, run against both engines:

```
seed real engine with snapshot A
  → apply generated migration(A → B)
  → introspect
  → assert == B
```

| ID | Case |
|---|---|
| R-01 | Introspection of a hand-built schema matches the intended snapshot (bootstraps the helper) |
| R-02 | Introspect → emit → apply to a fresh database → introspect | Fixed point |
| R-10 | Every `E-01`–`E-10` happy-path case, applied for real |
| R-11 | Every ordering case `E-20`–`E-29` |
| R-12 | **Every collision case `E-40`–`E-46`** — this is where naive emitters die |
| R-13 | Cross-dialect emit of the same canonical schema, both engines |
| R-20 | Full merge → migration → apply → introspect == merged snapshot |
| R-21 | The `M-80` scenario, once resolved, produces an executable migration |
| R-30 | Apply forward, then rollback → introspect == original snapshot |
| R-31 | Rollback of a phased FK-cycle migration executes |
| R-90 | **Hypothesis:** generated schema pairs round-trip |
| R-91 | Hypothesis: generated pairs round-trip *and* roll back |

`R-12` and `R-90` are the two that earn their runtime. Fuzzed round-tripping finds
emitter bugs no hand-written case will, because it explores type and constraint
combinations nobody thinks to write down.

---

## 9. Rename inference on import — `I-*`

| ID | Case | Expectation |
|---|---|---|
| I-01 | Name gone, new name present, same type & position | High-confidence rename proposed |
| I-02 | Name gone, new name present, different type & position | Proposed as drop + add |
| I-03 | **Rename + retype together** | Low-confidence rename, **explicitly marked a guess** (D22) |
| I-04 | Two dropped, two added, ambiguous pairing | Both proposals surfaced, user disambiguates |
| I-05 | Re-import an unchanged schema | **No renames proposed, empty diff** — idempotency |
| I-06 | User accepts a proposal | Identity preserved, diff shows rename |
| I-07 | User rejects a proposal | Drop + add, new UUID |
| I-08 | Nothing inferred without confirmation | No silent rename ever applied |
| I-09 | Table-level rename inference | Same rules |
| I-10 | Rename inferred where a *different* column already holds the new name | Detected as a collision, not a rename |
| I-11 | Confidence scores are ordered sensibly across I-01/I-02/I-03 | Asserted relatively, not on absolute values |

`I-05` is the one that protects trust: a user pasting the same schema twice and seeing
phantom renames will not paste a third time.

---

## 10. API, concurrency, degradation — `C-*`

| ID | Case | Expectation |
|---|---|---|
| C-01 | Two commits to one branch from the same expected head | Second rejected, `409`, actionable message (D25) |
| C-02 | Rejection names the branch and current head | Asserted |
| C-03 | Merge submitted after the target branch moved | Rejected, re-merge required |
| C-04 | Concurrent commits to *different* branches | Both succeed |
| C-05 | Concurrent merges into the same target | One wins, other rejected cleanly |
| C-06 | Failed request leaves no partial state | No orphaned commits (D12) |
| C-07 | Malformed API payload | `400` with field-level detail, no stack trace |
| C-08 | Unknown branch / commit ref | `404`, not a 500 |
| C-09 | Store unavailable | Degrades with a clear error, no data loss |

## 11. Scale — `S-*`

| ID | Case | Expectation |
|---|---|---|
| S-01 | 500 tables / 5,000 columns — diff | Completes within budget |
| S-02 | Same — three-way merge | Completes within budget |
| S-03 | Same — integrity validation | No accidental O(n²) over FKs |
| S-04 | 1,000-change diff via API | Returns; documented UI limit (D26) |
| S-05 | 200-commit DAG — LCA | Completes fast |
| S-06 | 50-table FK graph — topological sort | No recursion-depth failure |

These are guard-rails against accidental complexity blowups, not performance
optimization targets. Failing one means an algorithm is quadratic where it should be
linear.

---

## 12. End-to-end journeys — `X-*`

Deliberately few (philosophy #3). These test the *journey*, not components.

| ID | Journey |
|---|---|
| X-01 | Landing page loads the seeded demo with its pre-staged conflict visible |
| X-02 | Empty state: create a repo, see the guidance for an empty schema |
| X-03 | Paste DDL → confirm an inferred rename → commit → see the rename in the diff |
| X-04 | Paste malformed DDL → see a line-numbered error → correct it → succeed |
| X-05 | Branch → structured edit → diff against parent → merge cleanly |
| X-06 | Branch both ways → merge → resolve conflicts → preview migration → merge |
| X-07 | **Merge, resolve, commit to main, merge again → previously resolved conflict absent** (the `L-20` journey) |
| X-08 | Lossy operation surfaces an acknowledgment gate before merge |
| X-09 | Switch target dialect → see the representability report |
| X-10 | Copy the generated migration SQL |

`X-07` is `L-20` at the UI level. Worth duplicating because the wiring between a correct
engine and the screen is its own opportunity for failure.

---

## TDD build order

Written strictly in this sequence, tests before implementation. Each block is red before
it's green.

| Order | Tests | Why here |
|---|---|---|
| 0 | Fixture DSL + DAG builder | Everything depends on them. Build against `N-01`–`N-03`. |
| 1 | `N-*` | Model shape settles before anything consumes it |
| 2 | `P-01`–`P-13`, then `P-20`–`P-26` | Happy path, then normalization. **Day-1 sqlglot gate lives here** — if `P-01` or `P-23` can't be made green, replan the same day. |
| 3 | `P-30`–`P-40`, `P-50`–`P-68` | Dialect traps and rejection |
| 4 | `V-*` | Store primitives |
| 5 | `L-*`, ending with **`L-20`/`L-21`** | Merge base before merge. The headline. |
| 6 | `F-*` | Diff, with `F-40`–`F-46` idempotency as the gate |
| 7 | `M-*` by category: 4 → 3 → 1 → 2 → 5 | **Auto-merge first** (M-01), integrity last (M-80). Convergent counter-cases written *with* their conflict pairs, never after. |
| 8 | `M-90`–`M-94` | Properties once the shape is stable |
| 9 | `E-01`–`E-10`, then `R-01`/`R-02` | Emit, then immediately stand up round-tripping — nothing after this lands unverified |
| 10 | `E-20`–`E-29` + `R-11` | Ordering, verified live |
| 11 | `E-40`–`E-46` + `R-12` | Collisions, verified live |
| 12 | `E-60`–`E-83` | Classification and engine honesty |
| 13 | `E-100`–`E-107` | Representability |
| 14 | `E-120`–`E-124` + `R-30`/`R-31` | Rollback |
| 15 | `I-*` | Inference, once diff is trustworthy |
| 16 | `C-*` | API surface |
| 17 | `R-90`/`R-91`, `S-*` | Fuzz and scale — run in CI, not on every local save |
| 18 | `X-*` | Journeys last |

Two ordering choices worth stating: **`L-*` precedes `M-*`** because a merge built on the
wrong base is untestable in a meaningful way; and **round-tripping is stood up at step 9,
not at the end**, so every emitter case from that point forward is verified against a real
engine as it's written rather than in a day-5 scramble.

---

## The ten tests that matter most

If the suite had to shrink to ten, these:

| ID | What it protects |
|---|---|
| L-20 | Resolved conflicts stay resolved — the headline behavior |
| M-01 | Rename + retype auto-merges — the product thesis |
| M-80 | Post-merge integrity is load-bearing |
| E-40 | Rename swaps produce executable DDL |
| E-25 | Circular FKs produce executable DDL |
| R-90 | Fuzzed round-tripping — finds what nobody writes down |
| P-23 | Default normalization — no spurious diffs |
| F-40 | Self-diff is empty — no spurious diffs |
| P-67 | Unsupported input rejects wholly, never partially |
| M-25 | Convergent renames aren't false conflicts |

Six of the ten are guards against being *wrong while looking right* — silent misparse,
spurious diffs, false conflicts, plausible-but-failing DDL. That's the failure mode this
product has to earn trust against.

---

## Coverage stance

No coverage percentage target. The merge engine and emitters should approach full branch
coverage naturally because the case list above is close to exhaustive over their
behavior. Serialization glue, HTTP plumbing, and React components are covered
incidentally by `C-*` and `X-*` and are not chased.

Mutation testing on the merge engine and emitters only — those are the modules where a
surviving mutant means a real hole, and they're pure enough for it to run fast.
