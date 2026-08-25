"""MySQL adapter.

Two dialect quirks are handled here that would otherwise corrupt data silently, both
traced to sqlglot's `Anonymous` node -- its "I didn't recognise this" fallback:

  * prefix indexes  `KEY i (note(32))` parse as a function call, and re-emit with the
    identifier upper-cased (`NOTE(32)`), corrupting the column reference (D28 finding 1)
  * `now()` parses as Anonymous while `CURRENT_TIMESTAMP` parses as CurrentTimestamp,
    so default normalization is ours to do (D28 finding 2)

Anonymous is never passed through untouched.
"""
from __future__ import annotations

from sqlglot import exp

from ..model import ColumnType, IndexSpec
from .base import DialectAdapter

_TYPE_NAMES = {
    exp.DataType.Type.INT: "int",
    exp.DataType.Type.BIGINT: "bigint",
    exp.DataType.Type.SMALLINT: "smallint",
    exp.DataType.Type.TINYINT: "tinyint",
    exp.DataType.Type.MEDIUMINT: "mediumint",
    exp.DataType.Type.UINT: "int",
    exp.DataType.Type.UBIGINT: "bigint",
    exp.DataType.Type.USMALLINT: "smallint",
    exp.DataType.Type.UTINYINT: "tinyint",
    exp.DataType.Type.VARCHAR: "varchar",
    exp.DataType.Type.CHAR: "char",
    exp.DataType.Type.TEXT: "text",
    exp.DataType.Type.BOOLEAN: "boolean",
    exp.DataType.Type.DATETIME: "datetime",
    exp.DataType.Type.TIMESTAMP: "timestamp",
    exp.DataType.Type.DATE: "date",
    exp.DataType.Type.DECIMAL: "decimal",
    exp.DataType.Type.DOUBLE: "double",
    exp.DataType.Type.FLOAT: "float",
    exp.DataType.Type.JSON: "json",
}

_UNSIGNED = {
    exp.DataType.Type.UINT, exp.DataType.Type.UBIGINT,
    exp.DataType.Type.USMALLINT, exp.DataType.Type.UTINYINT,
}

# MySQL accepts BOOLEAN/BOOL as synonyms for TINYINT(1). From 8.0.19 only plain
# TINYINT(1) -- no UNSIGNED, no ZEROFILL -- carries the boolean assumption, so the
# variants must stay integers.
_TINYINT1_IS_BOOL = True


class MySQLAdapter(DialectAdapter):
    name = "mysql"
    sqlglot_dialect = "mysql"
    # Column names are case-insensitive on every platform, which is also what makes
    # the prefix-index reconstruction below sound.
    columns_case_sensitive = False

    def fold_identifier(self, name: str) -> str:
        return name

    def canonical_type(self, kind, flags: set[str]) -> tuple[ColumnType, bool]:
        t = kind.this
        params = _params(kind)
        unsigned = t in _UNSIGNED or _has_unsigned(kind)
        zerofill = "ZeroFillColumnConstraint" in flags or _has_zerofill(kind)

        if _TINYINT1_IS_BOOL and t == exp.DataType.Type.TINYINT \
                and params == (1,) and not unsigned and not zerofill:
            return ColumnType(base="boolean"), False

        base = _TYPE_NAMES.get(t) or str(t).split(".")[-1].lower()
        if base == "boolean":
            return ColumnType(base="boolean"), False
        # Display width is deprecated (8.0.17) and carries no schema meaning for
        # integers; keeping it would surface as a spurious diff.
        if base in {"int", "bigint", "smallint", "tinyint", "mediumint"}:
            params = ()
        return ColumnType(base=base, params=params, unsigned=unsigned), False

    def normalize_default(self, node) -> str | None:
        if node is None:
            return None
        if isinstance(node, exp.CurrentTimestamp):
            return "CURRENT_TIMESTAMP"
        # `now()` arrives as Anonymous -- normalize rather than pass through.
        if isinstance(node, exp.Anonymous) and node.name.lower() in {"now", "current_timestamp"}:
            return "CURRENT_TIMESTAMP"
        if isinstance(node, exp.Boolean):
            return "true" if node.this else "false"
        if isinstance(node, exp.Literal):
            return node.this if node.is_string else str(node.this)
        return node.sql(dialect=self.sqlglot_dialect)

    def index_column(self, node, desc: bool) -> IndexSpec:
        """Reconstruct a prefix index from sqlglot's function-call fallback.

        `note(32)` parses as Anonymous(this="note", expressions=[32]) and would
        re-emit as `NOTE(32)`. Recovering the real name by case-insensitive match is
        sound *specifically* because MySQL column names are case-insensitive, so
        `NOTE` and `note` cannot be two different columns. The same trick would be
        unsound under Postgres, which is why it lives in this adapter only.
        """
        if isinstance(node, exp.Anonymous):
            length = None
            args = node.expressions or []
            if len(args) == 1 and isinstance(args[0], exp.Literal):
                try:
                    length = int(args[0].this)
                except (TypeError, ValueError):
                    length = None
            return IndexSpec(column=node.name, desc=desc, prefix_length=length)
        return IndexSpec(column=node.name, desc=desc)


def _params(kind) -> tuple[int, ...]:
    out = []
    for p in (kind.expressions or []):
        try:
            out.append(int(p.this.this))
        except (AttributeError, TypeError, ValueError):
            pass
    return tuple(out)


def _has_unsigned(kind) -> bool:
    return _flag(kind, "UNSIGNED")


def _has_zerofill(kind) -> bool:
    return _flag(kind, "ZEROFILL")


def _flag(kind, word: str) -> bool:
    for v in kind.args.values():
        if isinstance(v, list):
            if any(word in str(x).upper() for x in v):
                return True
        elif v is not None and word in str(v).upper():
            return True
    return False
