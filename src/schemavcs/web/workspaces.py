"""One repo per visitor.

A deployed demo URL is a shared machine, and a single global repo would mean the first
reviewer's branches and the second reviewer's branches are the same branches. Every
visitor therefore gets an isolated workspace -- a cookie holding an opaque id, and one
SQLite file per id.

That is a product decision, not a security boundary: workspace ids are unguessable but
nothing here is authenticated, and the deployment is a demo, not a service holding
anyone's real schema. Said plainly rather than implied, because "it has sessions" reads
like "it has accounts" and it does not (D42).
"""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from ..dialects import parse_ddl
from ..engine import Repo
from ..storage import SqliteStore

DATA_DIR = Path(os.environ.get("SCHEMAVCS_DATA", "/tmp/schemavcs-workspaces"))

#: Workspace ids come back from a cookie, and a cookie is attacker-controlled input that
#: gets concatenated into a filesystem path. Validating the shape is what stops
#: `../../etc/passwd` from being a workspace name.
WORKSPACE_ID = re.compile(r"^[0-9a-f]{16}$")

SEED_DDL = """
CREATE TABLE users (
    id         bigint PRIMARY KEY,
    email      varchar(255) NOT NULL,
    nickname   varchar(64),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE orders (
    id       bigint PRIMARY KEY,
    user_id  bigint NOT NULL,
    total    numeric(10,2) NOT NULL,
    status   varchar(32) NOT NULL DEFAULT 'pending',
    CONSTRAINT orders_user_fk FOREIGN KEY (user_id) REFERENCES users (id)
);
CREATE INDEX orders_by_user ON orders (user_id);
"""


def new_id() -> str:
    return secrets.token_hex(8)


def is_valid(ws: str | None) -> bool:
    return bool(ws and WORKSPACE_ID.match(ws))


def path_for(ws: str) -> Path:
    if not is_valid(ws):
        raise ValueError(f"malformed workspace id: {ws!r}")
    return DATA_DIR / f"{ws}.db"


def exists(ws: str | None) -> bool:
    return is_valid(ws) and path_for(ws).exists()


def open_repo(ws: str) -> Repo:
    """Reopened per request. Cheap (SQLite), and it keeps the web layer stateless --
    two workers behind one URL see the same repo because the file is the state."""
    return Repo.open(SqliteStore(path_for(ws)))


#: The demo workspace arrives with two engineers' work already diverged. A reviewer who
#: has to build that state themselves before anything interesting happens will not: the
#: headline claim (a rename and a retype of the *same column* merge cleanly) needs two
#: branches to exist, and asking for six clicks before the first payoff is the surest way
#: to have the payoff never seen (D46).
DEMO_BRANCHES = [
    ("rename-email", [
        ("rename_col", ("users.email", "contact_email"),
         "clarify what the email column is for"),
        ("add_col", ("users", "verified_at", "timestamptz"),
         "track when an address was verified"),
    ]),
    ("widen-email", [
        ("retype_col", ("users.email", "text"), "email addresses outgrew varchar(255)"),
    ]),
    ("nickname-a", [
        ("retype_col", ("users.nickname", "varchar(128)"), "longer nicknames"),
    ]),
    ("nickname-b", [
        ("retype_col", ("users.nickname", "text"), "nicknames should be unbounded"),
    ]),
]


def seed_demo(repo: Repo) -> None:
    """Play the scenario the tour narrates, as real commits on real branches."""
    for name, steps in DEMO_BRANCHES:
        repo.branch(name, "main")
        for method, args, message in steps:
            editor = repo.snapshot(name).evolve()
            getattr(editor, method)(*args)
            repo.commit(name, editor.build(), message=message)


def create(ws: str | None = None, ddl: str | None = None, dialect: str = "postgres",
           *, demo: bool = False) -> str:
    """Create a workspace and seed it. Raises DDLError if the DDL will not parse --
    deliberately, so a bad paste never produces a half-built workspace."""
    snapshot = parse_ddl(ddl if ddl and ddl.strip() else SEED_DDL, dialect=dialect)
    ws = ws or new_id()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    repo = Repo.init("main", store=SqliteStore(path_for(ws)))
    repo.commit("main", snapshot, message="initial schema")
    if demo:
        seed_demo(repo)
    return ws
