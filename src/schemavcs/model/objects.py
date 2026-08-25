"""Schema objects.

Every object carries a stable UUID assigned at creation and preserved for life (D1),
and -- critically -- **every reference between objects is by that UUID, never by name**
(D30).

That second property is easy to skip and expensive to retrofit. If an index stored the
*name* of its column, renaming the column would leave the index pointing at a name that
no longer exists: the snapshot becomes internally inconsistent, and the integrity
validator can no longer tell a genuinely dangling reference from one that is merely stale
after a rename. Name-keyed references inside an identity-based model reintroduce, one
level down, exactly the problem identity exists to solve.

Names are resolved for display and for DDL emission, never stored as references.

Equality and hashing are *structural* and exclude identity, so two independently built
identical schemas compare equal -- which is what round-trip verification (R-02) needs,
since a live engine cannot hand back our UUIDs. Because references are ids, structural
comparison has to resolve them back to names first; that is what the `names` mapping
threaded through `fingerprint()` is for.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from .types import ColumnType

#: Placeholder used in a fingerprint when a reference cannot be resolved. A dangling
#: reference is a real (invalid) state -- see Snapshot.validate() -- so it must be
#: representable rather than crash comparison.
UNRESOLVED = "<unresolved>"


def new_id() -> str:
    return str(uuid.uuid4())


class ConstraintKind(str, Enum):
    PRIMARY_KEY = "primary_key"
    FOREIGN_KEY = "foreign_key"
    UNIQUE = "unique"
    CHECK = "check"


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType
    nullable: bool = True
    default: str | None = None       # canonical form; parsers normalize into it
    autoincrement: bool = False      # SERIAL / AUTO_INCREMENT unified (D28 finding 3)
    id: str = field(default_factory=new_id, compare=False)

    def fingerprint(self, names=None) -> tuple:
        return (self.name, self.type, self.nullable, self.default, self.autoincrement)


def _total(fingerprint: tuple) -> str:
    """A total order over fingerprints that cannot raise.

    Sorting the tuples directly compares them element-wise, and several fields are
    optional -- a column `default`, an index `where`, a constraint `on_delete`. Two
    entries that tie on everything before one of those then compare `None` against a
    string, which is a TypeError. Sorting is only here to make the comparison
    order-insensitive, so any deterministic total order does the job; the tuples
    themselves are still what gets compared for equality.

    This matters more than a sort helper usually would, because `fingerprint` backs
    `Snapshot.__eq__` and `__hash__` -- and those are reachable with an *invalid*
    snapshot in hand. A merge may legitimately produce one and report the violations
    (D11), so equality has to survive a schema that is malformed rather than crash on
    it. Found by a property test, on CI, on an example this laptop never generated.
    """
    return repr(fingerprint)


@dataclass(frozen=True)
class IndexColumn:
    """One entry in an index's ordered column list, referenced by column id.

    `prefix_length` exists because MySQL prefix indexes are a real construct that
    sqlglot mangles into a function call (D28 finding 1); modeling it explicitly is
    what lets the adapter round-trip it instead of corrupting the column name.
    """

    column_id: str
    desc: bool = False
    prefix_length: int | None = None

    def fingerprint(self, names) -> tuple:
        return (names.get(self.column_id, UNRESOLVED), self.desc, self.prefix_length)


@dataclass(frozen=True)
class Index:
    name: str
    columns: tuple[IndexColumn, ...] = ()   # ORDERED - order is significant (N-06, D4)
    unique: bool = False
    where: str | None = None                # partial index predicate
    id: str = field(default_factory=new_id, compare=False)

    def fingerprint(self, names) -> tuple:
        return (self.name, tuple(c.fingerprint(names) for c in self.columns),
                self.unique, self.where)


@dataclass(frozen=True)
class Constraint:
    name: str
    kind: ConstraintKind
    column_ids: tuple[str, ...] = ()          # ORDERED (N-07)
    ref_table_id: str | None = None
    ref_column_ids: tuple[str, ...] = ()      # ORDERED, paired with `column_ids`
    on_delete: str | None = None
    on_update: str | None = None
    expression: str | None = None             # CHECK body
    id: str = field(default_factory=new_id, compare=False)

    def fingerprint(self, names) -> tuple:
        return (
            self.name, self.kind.value,
            tuple(names.get(c, UNRESOLVED) for c in self.column_ids),
            names.get(self.ref_table_id, UNRESOLVED) if self.ref_table_id else None,
            tuple(names.get(c, UNRESOLVED) for c in self.ref_column_ids),
            self.on_delete, self.on_update, self.expression,
        )


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    indexes: tuple[Index, ...] = ()
    id: str = field(default_factory=new_id, compare=False)

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    def column_by_id(self, column_id: str) -> Column | None:
        return next((c for c in self.columns if c.id == column_id), None)

    def index(self, name: str) -> Index | None:
        return next((i for i in self.indexes if i.name == name), None)

    def constraint(self, name: str) -> Constraint | None:
        return next((c for c in self.constraints if c.name == name), None)

    def fingerprint(self, names) -> tuple:
        # Columns are order-insensitive (D27); the ordered column lists inside indexes
        # and constraints are not.
        return (
            self.name,
            tuple(sorted((c.fingerprint(names) for c in self.columns), key=_total)),
            tuple(sorted((c.fingerprint(names) for c in self.constraints), key=_total)),
            tuple(sorted((i.fingerprint(names) for i in self.indexes), key=_total)),
        )
