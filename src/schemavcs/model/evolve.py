"""Non-destructive snapshot evolution.

Every M-* merge test builds two divergent branches from one base, so evolution must
never mutate the original. It must also preserve UUIDs across edits, or a rename
becomes indistinguishable from a drop-plus-add and the diff engine loses the one
property it exists to exploit (D1).

Because references are ids (D30), renaming is now genuinely free: an index over a
renamed column needs no updating at all, because it never stored the name. The
callers that DO need work are the ones taking column names as input -- add_index,
add_constraint -- which resolve names to ids here, once, at the edge.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .objects import (Column, Constraint, ConstraintKind, Index, IndexColumn,
                      Table, new_id)
from .types import ColumnType


@dataclass(frozen=True)
class IndexSpec:
    """Name-based index column description, for callers that build by name."""
    column: str
    desc: bool = False
    prefix_length: int | None = None


class SchemaError(Exception):
    """Raised on an edit that cannot apply -- unknown table, duplicate name, etc."""


class SnapshotEditor:
    """Fluent, copy-on-write editor. `.build()` returns a new Snapshot."""

    def __init__(self, snapshot):
        self._s = snapshot
        self._tables = list(snapshot.tables)

    # ---------------------------------------------------------- internals
    def _find(self, name: str) -> tuple[int, Table]:
        for i, t in enumerate(self._tables):
            if t.name == name:
                return i, t
        raise SchemaError(f"no such table: {name!r}")

    def _put(self, i: int, t: Table) -> "SnapshotEditor":
        self._tables[i] = t
        return self

    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        table, _, rest = path.rpartition(".")
        if not table:
            raise SchemaError(f"expected 'table.object', got {path!r}")
        return table, rest

    def _col_index(self, t: Table, name: str) -> int:
        for i, c in enumerate(t.columns):
            if c.name == name:
                return i
        raise SchemaError(f"no such column: {t.name}.{name}")

    # ------------------------------------------------------------- tables
    def add_table(self, name: str) -> "SnapshotEditor":
        if any(t.name == name for t in self._tables):
            raise SchemaError(f"duplicate table: {name!r}")
        self._tables.append(Table(name=name))
        return self

    def drop_table(self, name: str) -> "SnapshotEditor":
        i, _ = self._find(name)
        del self._tables[i]
        return self

    def rename_table(self, name: str, new_name: str) -> "SnapshotEditor":
        i, t = self._find(name)
        if any(x.name == new_name for x in self._tables):
            raise SchemaError(f"duplicate table: {new_name!r}")
        return self._put(i, replace(t, name=new_name))   # id preserved

    # ------------------------------------------------------------ columns
    def add_col(self, table: str, name: str, type_: str, *, nullable: bool = True,
                default: str | None = None, autoincrement: bool = False) -> "SnapshotEditor":
        i, t = self._find(table)
        if t.column(name):
            raise SchemaError(f"duplicate column: {table}.{name}")
        col = Column(name=name, type=ColumnType.parse(type_), nullable=nullable,
                     default=default, autoincrement=autoincrement)
        return self._put(i, replace(t, columns=t.columns + (col,)))

    def drop_col(self, path: str) -> "SnapshotEditor":
        table, name = self._split(path)
        i, t = self._find(table)
        j = self._col_index(t, name)
        cols = t.columns[:j] + t.columns[j + 1:]
        return self._put(i, replace(t, columns=cols))

    def _alter_col(self, path: str, **changes) -> "SnapshotEditor":
        table, name = self._split(path)
        i, t = self._find(table)
        j = self._col_index(t, name)
        col = replace(t.columns[j], **changes)           # id preserved by dataclass replace
        return self._put(i, replace(t, columns=t.columns[:j] + (col,) + t.columns[j + 1:]))

    def rename_col(self, path: str, new_name: str) -> "SnapshotEditor":
        table, _ = self._split(path)
        _, t = self._find(table)
        if t.column(new_name):
            raise SchemaError(f"duplicate column: {table}.{new_name}")
        return self._alter_col(path, name=new_name)

    def retype_col(self, path: str, type_: str) -> "SnapshotEditor":
        return self._alter_col(path, type=ColumnType.parse(type_))

    def set_nullable(self, path: str, nullable: bool) -> "SnapshotEditor":
        return self._alter_col(path, nullable=nullable)

    def set_default(self, path: str, default: str | None) -> "SnapshotEditor":
        return self._alter_col(path, default=default)

    # ------------------------------------------------- indexes / constraints
    def add_index(self, table: str, name: str, columns, *, unique: bool = False,
                  where: str | None = None) -> "SnapshotEditor":
        i, t = self._find(table)
        idx = Index(name=name, columns=index_columns(t, columns), unique=unique,
                    where=where)
        return self._put(i, replace(t, indexes=t.indexes + (idx,)))

    def drop_index(self, path: str) -> "SnapshotEditor":
        table, name = self._split(path)
        i, t = self._find(table)
        keep = tuple(x for x in t.indexes if x.name != name)
        if len(keep) == len(t.indexes):
            raise SchemaError(f"no such index: {path}")
        return self._put(i, replace(t, indexes=keep))

    def set_index_columns(self, path: str, columns) -> "SnapshotEditor":
        table, name = self._split(path)
        i, t = self._find(table)
        out = []
        found = False
        for x in t.indexes:
            if x.name == name:
                x, found = replace(x, columns=index_columns(t, columns)), True
            out.append(x)
        if not found:
            raise SchemaError(f"no such index: {path}")
        return self._put(i, replace(t, indexes=tuple(out)))

    def rename_index(self, path: str, new_name: str) -> "SnapshotEditor":
        table, name = self._split(path)
        i, t = self._find(table)
        out = tuple(replace(x, name=new_name) if x.name == name else x for x in t.indexes)
        return self._put(i, replace(t, indexes=out))

    def add_constraint(self, table: str, c: Constraint) -> "SnapshotEditor":
        """Add a pre-built constraint. Its column_ids must already be resolved."""
        i, t = self._find(table)
        if any(x.name == c.name for x in t.constraints):
            raise SchemaError(f"duplicate constraint: {table}.{c.name}")
        return self._put(i, replace(t, constraints=t.constraints + (c,)))

    def add_unique(self, table: str, name: str, columns) -> "SnapshotEditor":
        i, t = self._find(table)
        return self.add_constraint(table, Constraint(
            name=name, kind=ConstraintKind.UNIQUE,
            column_ids=resolve_column_ids(t, columns)))

    def add_check(self, table: str, name: str, expression: str,
                  columns=()) -> "SnapshotEditor":
        """`columns` records which columns the predicate reads, by id.

        Optional because we cannot parse arbitrary CHECK bodies, but worth recording
        when the caller knows: without it, dropping a column a CHECK depends on is
        undetectable, and the merge silently produces DDL the database rejects (M-85).
        """
        _, t = self._find(table)
        return self.add_constraint(table, Constraint(
            name=name, kind=ConstraintKind.CHECK, expression=expression,
            column_ids=resolve_column_ids(t, columns)))

    def add_fk(self, table: str, name: str, columns, ref_table: str, ref_columns,
               *, on_delete: str | None = None,
               on_update: str | None = None) -> "SnapshotEditor":
        _, t = self._find(table)
        _, target = self._find(ref_table)
        return self.add_constraint(table, Constraint(
            name=name, kind=ConstraintKind.FOREIGN_KEY,
            column_ids=resolve_column_ids(t, columns),
            ref_table_id=target.id,
            ref_column_ids=resolve_column_ids(target, ref_columns),
            on_delete=on_delete, on_update=on_update))

    def drop_constraint(self, path: str) -> "SnapshotEditor":
        table, name = self._split(path)
        i, t = self._find(table)
        keep = tuple(x for x in t.constraints if x.name != name)
        if len(keep) == len(t.constraints):
            raise SchemaError(f"no such constraint: {path}")
        return self._put(i, replace(t, constraints=keep))

    # -------------------------------------------------------------- build
    @property
    def snapshot(self):
        """The work in progress, readable mid-edit.

        A caller applying several changes at once needs to see the current state to
        decide what still needs doing -- "is this column already NOT NULL?" -- and the
        editor is copy-on-write, so handing out a snapshot costs nothing and cannot be
        used to mutate anything.
        """
        return self.build()

    def build(self):
        from .snapshot import Snapshot
        return Snapshot(dialect=self._s.dialect, tables=tuple(self._tables))


def resolve_column_ids(table: Table, names) -> tuple[str, ...]:
    """Column names -> ids, raising on anything the table does not have."""
    out = []
    for n in names:
        col = table.column(n)
        if col is None:
            raise SchemaError(f"no such column: {table.name}.{n}")
        out.append(col.id)
    return tuple(out)


def index_columns(table: Table, columns) -> tuple[IndexColumn, ...]:
    """Accepts names (`["a", "b"]`), `(name, desc)` pairs, `IndexSpec`s, or ready
    IndexColumn instances. Names are resolved to ids here so nothing downstream
    ever holds a name as a reference."""
    out = []
    for c in columns:
        if isinstance(c, IndexColumn):
            out.append(c)
            continue
        if isinstance(c, IndexSpec):
            name, desc, prefix = c.column, c.desc, c.prefix_length
        elif isinstance(c, tuple):
            name, desc, prefix = (list(c) + [False, None])[:3]
        else:
            name, desc, prefix = c, False, None
        col = table.column(name)
        if col is None:
            raise SchemaError(f"no such column: {table.name}.{name}")
        out.append(IndexColumn(column_id=col.id, desc=bool(desc), prefix_length=prefix))
    return tuple(out)


# Kept for the old call sites' shape; resolution now always needs the table.
_index_columns = index_columns
