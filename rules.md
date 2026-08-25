# Project Rules

Reference document for this project round. Everything below is the brief as given —
scope, expectations, evaluation criteria, and required deliverables.

## Problem statement

### Version control for database schemas

Build **branch, diff, and merge for database schemas**.

A user should be able to:

- Branch a schema
- Evolve it independently — add, drop, rename, and retype columns; change
  constraints and indexes; create and drop tables
- See exactly what diverged
- Merge back

**Out of scope:** row data. The artifact under version control is the *schema itself*.

## Overview

This is an open-ended project round.

The problem statement is intentionally vague — how it is interpreted, scoped, and
built *is* the evaluation. There is no "right" answer. What matters is how you think,
what decisions you make, and whether you can turn ambiguity into something real.

Engineers are builders. That means the full picture counts — code, product instincts,
UX choices, documentation. Not just whether it works, but whether it's *insanely great*.

Treat it like a real work assignment, not an exam.

## Hard requirements

- [ ] A **working solution with a deployed URL** that can be tested. It **must be a
      web application**.
- [ ] A **`decisions.md`** at the root of the repo (see below). Not optional.
- [ ] A **GitHub repository** with the code.

Timebox: **5 days**.

## Evaluation criteria

| Criteria | What this means |
| --- | --- |
| **Problem framing** | How did you interpret the problem? Why did you scope it the way you did? What did you deliberately leave out and why? |
| **Product thinking** | Did you think about who this is for and what problem it actually solves? Or did you just write code? |
| **UX decisions** | Does the experience make sense? Is it intuitive? Visual polish is not judged — whether you thought about the person using this is. |
| **Code quality** | Is the code clean, well-organized, and something you'd be comfortable handing to a teammate? |
| **Tests** | Meaningful tests, not token coverage. Tests that actually catch real problems. |
| **Documentation** | Could someone set this up and understand your thinking without talking to you? |
| **Setup experience** | How easy is it to get running? |
| **Velocity** | Given 5 days, how much real progress did you make? |
| **Above and beyond** | Did you surprise us? Not with bells and whistles, but with depth. Did you solve a hard sub-problem most people would skip? |

## Going above and beyond

This is the criterion that separates a solid submission from a memorable one. A
working solution that ticks the boxes is the **baseline, not the ceiling**.

Going above and beyond is **not** bells and whistles — not extra pages, not a slicker
theme, not a longer feature list. It's **depth**: finding the hard part of the problem
most people would quietly skip, and actually solving it.

What that looks like:

- **Solve a hard sub-problem others avoid.** The messy edge case, the ambiguous input,
  the failure mode nobody wants to touch. Go at the genuinely difficult thing instead
  of routing around it.
- **Handle the real world, not the happy path.** Bad data, partial input, malformed
  documents, rate limits, timeouts, concurrent users. Degrade gracefully instead of
  falling over.
- **Show range.** A real product call, a real UX call, and a real infra call — each
  one holding up.
- **Build something you'd actually trust.** Observability, sensible error messages, a
  setup a stranger can run in one shot, tests that catch failures you'd actually hit.
- **Go deep on one thing exceptionally well** rather than shallow on ten. Depth beats
  breadth every time.
- **Make the end-to-end journey delightful.** First-run experience, empty state, the
  moment something goes wrong, the small touches that make someone smile. Imagine the
  actual person moving through the product and remove the friction at every step.

## `decisions.md` (required)

A `decisions.md` at the root of the repo. This is **not a changelog**. It's a running
log of the real calls made while building.

For each meaningful decision, capture:

- **The decision** — what you actually chose.
- **The alternatives** — what else you seriously considered.
- **The reasoning** — why you went that way, including the tradeoffs you accepted.
- **What you deliberately cut** — and why it was the right thing to leave out for now.

Keep it honest and specific.

> "Used Postgres because it's good" tells us nothing.
> "Used Postgres over a vector DB because the query patterns were relational and I
> didn't want to run two datastores for a 5-day build" tells us how you think.

This file is often more revealing than the code itself — it's where judgment under
ambiguity and time pressure shows.
