"""Postgres adapter."""
from __future__ import annotations

from sqlglot import exp

from ..model import ColumnType, IndexSpec
from .base import DialectAdapter

# SERIAL is a type in Postgres but a constraint in MySQL (D28 finding 3). The canonical
# model stores (int type, autoincrement=True); each adapter re-splits on emit.
_SERIAL = {
    exp.DataType.Type.SMALLSERIAL: "smallint",
    exp.DataType.Type.SERIAL: "int",
    exp.DataType.Type.BIGSERIAL: "bigint",
}

_TYPE_NAMES = {
    exp.DataType.Type.INT: "int",
    exp.DataType.Type.BIGINT: "bigint",
    exp.DataType.Type.SMALLINT: "smallint",
    exp.DataType.Type.VARCHAR: "varchar",
    exp.DataType.Type.CHAR: "char",
    exp.DataType.Type.TEXT: "text",
    exp.DataType.Type.BOOLEAN: "boolean",
    exp.DataType.Type.TIMESTAMPTZ: "timestamptz",
    exp.DataType.Type.TIMESTAMP: "timestamp",
    exp.DataType.Type.DATE: "date",
    exp.DataType.Type.DECIMAL: "decimal",
    exp.DataType.Type.DOUBLE: "double",
    exp.DataType.Type.FLOAT: "real",
    exp.DataType.Type.JSON: "json",
    exp.DataType.Type.JSONB: "jsonb",
    exp.DataType.Type.UUID: "uuid",
}


class PostgresAdapter(DialectAdapter):
    name = "postgres"
    sqlglot_dialect = "postgres"
    columns_case_sensitive = True   # quoted identifiers really are distinct here

    def fold_identifier(self, name: str) -> str:
        # Unquoted identifiers fold to lower case (P-33). sqlglot has already
        # discarded the quoting distinction by this point, so folding uniformly is
        # the honest approximation: it matches the overwhelmingly common case and
        # never invents a distinction that isn't there.
        return name.lower()

    def canonical_type(self, kind, flags: set[str]) -> tuple[ColumnType, bool]:
        t = kind.this
        if t in _SERIAL:
            return ColumnType(base=_SERIAL[t]), True
        base = _TYPE_NAMES.get(t)
        if base is None:
            base = str(t).split(".")[-1].lower()
        return ColumnType(base=base, params=_params(kind)), False

    def normalize_default(self, node) -> str | None:
        if node is None:
            return None
        # Postgres collapses now()/NOW()/CURRENT_TIMESTAMP to one node already,
        # so P-23 costs nothing here (D28 finding 2).
        if isinstance(node, exp.CurrentTimestamp):
            return "CURRENT_TIMESTAMP"
        if isinstance(node, exp.Boolean):
            return "true" if node.this else "false"
        if isinstance(node, exp.Literal):
            return node.this if node.is_string else str(node.this)
        return node.sql(dialect=self.sqlglot_dialect)

    def index_column(self, node, desc: bool) -> IndexSpec:
        return IndexSpec(column=self.fold_identifier(node.name), desc=desc)


def _params(kind) -> tuple[int, ...]:
    out = []
    for p in (kind.expressions or []):
        try:
            out.append(int(p.this.this))
        except (AttributeError, TypeError, ValueError):
            pass
    return tuple(out)
