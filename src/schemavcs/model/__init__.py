"""Canonical, dialect-neutral schema model.

The load-bearing invariant (D5): nothing in this package knows what Postgres or
MySQL is. Dialect knowledge lives only in `schemavcs.dialects`.
"""
from .dsl import SchemaBuilder, schema
from .evolve import IndexSpec, SchemaError, SnapshotEditor, index_columns, resolve_column_ids
from .objects import (Column, Constraint, ConstraintKind, Index, IndexColumn,
                      Table, new_id)
from .snapshot import Snapshot
from .types import DIALECT_GENERIC, ColumnType

__all__ = [
    "schema", "SchemaBuilder", "Snapshot", "SnapshotEditor", "SchemaError",
    "Column", "Constraint", "ConstraintKind", "Index", "IndexColumn", "Table",
    "IndexSpec", "index_columns", "resolve_column_ids",
    "ColumnType", "DIALECT_GENERIC", "new_id",
]
