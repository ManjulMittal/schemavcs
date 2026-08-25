"""Fixture/builder DSL.

Designed before the tests that use it, deliberately: ~150 cases in the test plan all
construct schemas, so readability here sets the readability of the whole suite.

Three requirements that are expensive to retrofit:
  * identity must be addressable       -> `s.col("users.email").id`
  * evolution must be non-destructive  -> `s.evolve()...build()` returns a new snapshot
  * callers speak NAMES, the model stores IDS (D30) -> resolution happens in `build()`,
    because a foreign key may reference a table declared later in the chain

    schema()
      .table("users")
        .col("id", "bigint", pk=True)
        .col("email", "varchar(255)", nullable=False)
        .index("idx_email", ["email"], unique=True)
      .table("orders")
        .col("user_id", "bigint")
        .fk("fk_user", ["user_id"], "users", ["id"], on_delete="cascade")
      .build()
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .evolve import SchemaError, index_columns, resolve_column_ids
from .objects import Column, Constraint, ConstraintKind, Index, IndexColumn, Table
from .types import DIALECT_GENERIC, ColumnType


@dataclass
class _PendingConstraint:
    name: str
    kind: ConstraintKind
    columns: tuple[str, ...] = ()
    ref_table: str | None = None
    ref_columns: tuple[str, ...] = ()
    on_delete: str | None = None
    on_update: str | None = None
    expression: str | None = None


@dataclass
class _PendingIndex:
    name: str
    columns: tuple
    unique: bool = False
    where: str | None = None


class SchemaBuilder:
    def __init__(self, dialect: str = DIALECT_GENERIC):
        self._dialect = dialect
        self._tables: list[Table] = []
        # Constraints and indexes are held by NAME until build(), so a foreign key can
        # reference a table that has not been declared yet.
        self._pending: dict[str, list] = {}

    # ------------------------------------------------------------- tables
    def table(self, name: str) -> "SchemaBuilder":
        self._tables.append(Table(name=name))
        self._pending.setdefault(name, [])
        return self

    @property
    def _current(self) -> Table:
        if not self._tables:
            raise SchemaError("call .table(...) before adding columns")
        return self._tables[-1]

    def _replace_current(self, t: Table) -> "SchemaBuilder":
        self._tables[-1] = t
        return self

    def _defer(self, item) -> "SchemaBuilder":
        self._pending[self._current.name].append(item)
        return self

    # ------------------------------------------------------------ columns
    def col(self, name: str, type_: str, *, nullable: bool = True,
            default: str | None = None, pk: bool = False,
            unique: bool = False, autoincrement: bool = False) -> "SchemaBuilder":
        """`pk=True` is sugar: it implies NOT NULL and adds a primary key constraint."""
        t = self._current
        col = Column(name=name, type=ColumnType.parse(type_),
                     nullable=False if pk else nullable,
                     default=default, autoincrement=autoincrement)
        self._replace_current(replace(t, columns=t.columns + (col,)))
        if pk:
            self.pk([name])
        if unique:
            self.unique(f"uq_{t.name}_{name}", [name])
        return self

    # -------------------------------------------------------- constraints
    def pk(self, columns, name: str | None = None) -> "SchemaBuilder":
        return self._defer(_PendingConstraint(
            name=name or f"pk_{self._current.name}",
            kind=ConstraintKind.PRIMARY_KEY, columns=tuple(columns)))

    def unique(self, name: str, columns) -> "SchemaBuilder":
        return self._defer(_PendingConstraint(
            name=name, kind=ConstraintKind.UNIQUE, columns=tuple(columns)))

    def check(self, name: str, expression: str, columns=()) -> "SchemaBuilder":
        return self._defer(_PendingConstraint(
            name=name, kind=ConstraintKind.CHECK, expression=expression,
            columns=tuple(columns)))

    def fk(self, name: str, columns, ref_table: str, ref_columns,
           *, on_delete: str | None = None,
           on_update: str | None = None) -> "SchemaBuilder":
        return self._defer(_PendingConstraint(
            name=name, kind=ConstraintKind.FOREIGN_KEY, columns=tuple(columns),
            ref_table=ref_table, ref_columns=tuple(ref_columns),
            on_delete=on_delete, on_update=on_update))

    # ------------------------------------------------------------ indexes
    def index(self, name: str, columns, *, unique: bool = False,
              where: str | None = None) -> "SchemaBuilder":
        return self._defer(_PendingIndex(name=name, columns=tuple(columns),
                                         unique=unique, where=where))

    # -------------------------------------------------------------- build
    def build(self):
        from .snapshot import Snapshot
        by_name = {t.name: t for t in self._tables}

        out = []
        for t in self._tables:
            constraints, indexes = [], []
            for item in self._pending.get(t.name, []):
                if isinstance(item, _PendingIndex):
                    indexes.append(Index(
                        name=item.name, columns=index_columns(t, item.columns),
                        unique=item.unique, where=item.where))
                    continue
                ref_table = by_name.get(item.ref_table) if item.ref_table else None
                if item.ref_table and ref_table is None:
                    raise SchemaError(
                        f"constraint {item.name!r} references unknown table "
                        f"{item.ref_table!r}")
                constraints.append(Constraint(
                    name=item.name, kind=item.kind,
                    column_ids=resolve_column_ids(t, item.columns),
                    ref_table_id=ref_table.id if ref_table else None,
                    ref_column_ids=(resolve_column_ids(ref_table, item.ref_columns)
                                    if ref_table else ()),
                    on_delete=item.on_delete, on_update=item.on_update,
                    expression=item.expression))
            out.append(replace(t, constraints=tuple(constraints), indexes=tuple(indexes)))
        return Snapshot(dialect=self._dialect, tables=tuple(out))


def schema(dialect: str = DIALECT_GENERIC) -> SchemaBuilder:
    return SchemaBuilder(dialect)
