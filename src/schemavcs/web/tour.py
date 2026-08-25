"""The guided path through the app.

A reviewer arrives at a URL with no context and no one to explain the demo. The tool's
central claim -- that a rename on one branch and a retype on the other merge cleanly --
is invisible unless somebody sets up two branches first, so the app sets them up and then
*tells the reviewer where to look* (D46).

The steps are a fixed narrative, not progress tracking. Marking them "done" would need
state the app does not have (a GET is not an event) and would be a lie about a page the
visitor merely loaded. Instead every step says what it shows and links straight to it
with the parameters already filled in, and the current one is highlighted.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The branches `seed_demo` creates. Named here so the narrative and the seeding cannot
#: drift apart silently -- a step that links to a branch nobody created is a dead end.
RENAMED, WIDENED = "rename-email", "widen-email"
NICK_A, NICK_B = "nickname-a", "nickname-b"


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    blurb: str
    href: str


def steps(ws: str) -> list[Step]:
    w = f"/w/{ws}"
    return [
        Step("schema", "The schema",
             "Two tables, versioned like source. Every table, column, index and "
             "constraint carries a stable id — that is what the rest of this depends on.",
             f"{w}/branch/main"),
        Step("branches", "Two engineers diverge",
             f"‘{RENAMED}’ renamed users.email. ‘{WIDENED}’ changed its type. "
             "The same column, edited two different ways.",
             f"{w}/branch/{RENAMED}"),
        Step("compare", "A diff that understands renames",
             "It reports a renamed column, not a deletion beside an addition — because "
             "the object on both sides has the same id.",
             f"{w}/compare?base=main&target={RENAMED}"),
        Step("merge", "Merge, and a real conflict",
             "The rename and the retype both survive: different attributes. Then try "
             f"‘{NICK_A}’ against ‘{NICK_B}’, which disagree about one attribute and stop.",
             f"{w}/merge?ours={RENAMED}&theirs={WIDENED}"),
        Step("migration", "Get the SQL",
             "The migration from what is deployed to what you want, per engine, with "
             "destructive steps withheld until acknowledged.",
             f"{w}/migration?deployed=main&target={RENAMED}"),
    ]


#: Every operation the structured editor exposes, grouped for display. This exists so the
#: answer to "what can this thing do?" is on the screen rather than inferred from which
#: buttons a reviewer happens to find (D46).
OPERATIONS = [
    ("Columns", ["add a column", "rename", "change type", "default", "NOT NULL",
                 "drop — all from one form per column"]),
    ("Indexes", ["create index", "unique index", "partial index (WHERE …)",
                 "drop index"]),
    ("Constraints", ["foreign key", "unique", "check", "drop constraint"]),
    ("Tables", ["create table", "rename table", "drop table"]),
    ("Version control", ["branch", "commit (every edit is one)",
                         "compare any two branches", "three-way merge",
                         "resolve conflicts", "generate migration SQL"]),
]
