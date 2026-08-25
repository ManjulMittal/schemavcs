"""Dialect adapters -- the ONLY place in the codebase that knows what an engine is (D5).

`tests/unit/test_architecture.py` enforces that boundary: if a dialect name appears in
`schemavcs.model` or `schemavcs.engine`, the build fails.
"""
from __future__ import annotations

from ..model import Snapshot
from .errors import DDLError, Problem
from .mysql import MySQLAdapter
from .postgres import PostgresAdapter

ADAPTERS = {a.name: a for a in (PostgresAdapter(), MySQLAdapter())}


def adapter_for(dialect: str):
    try:
        return ADAPTERS[dialect]
    except KeyError:
        raise ValueError(
            f"unknown dialect {dialect!r}; supported: {', '.join(sorted(ADAPTERS))}"
        ) from None


def parse_ddl(sql: str, dialect: str) -> Snapshot:
    """Parse DDL into a canonical snapshot, or raise DDLError listing every problem."""
    return adapter_for(dialect).parse(sql)


#: Base types that take numeric parameters, and what those parameters mean. Used to ask
#: for a size in the right shape -- and to refuse one where it is meaningless, since
#: `int(5)` is not a narrower integer in any engine this targets.
TYPE_PARAMS: dict[str, tuple[str, ...]] = {
    "varchar": ("length",),
    "char": ("length",),
    "decimal": ("precision", "scale"),
    "time": ("precision",),
    "timestamp": ("precision",),
    "timestamptz": ("precision",),
}


def type_params(base: str) -> tuple[str, ...]:
    """The parameters `base` accepts, if any."""
    return TYPE_PARAMS.get(base, ())


def known_types(dialect: str) -> tuple[str, ...]:
    """Canonical type names a given engine can express, for validation and for display.

    Lives here rather than in the model because it is exactly the dialect knowledge the
    model is forbidden to hold (D5). The UI uses it to reject a typo at the moment it is
    typed instead of when the migration is generated.
    """
    return tuple(sorted(EMITTERS[dialect].TYPES))


__all__ = ["parse_ddl", "adapter_for", "ADAPTERS", "DDLError", "Problem",
           "known_types", "type_params", "TYPE_PARAMS"]

from .emit import (EMITTERS, MySQLEmitter, PostgresEmitter, Script, Step,
                   UnrepresentableError, emit)
