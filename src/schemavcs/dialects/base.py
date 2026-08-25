"""Dialect adapter base: everything shared by the two ingest paths.

Dialect-specific behaviour is confined to the hooks at the bottom of this class --
`canonical_type`, `normalize_default`, `fold_identifier`, `columns_case_sensitive`. If a
new dialect needs anything beyond those, that's a signal the canonical model is missing a
concept, not a reason to add a branch upstream.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from ..model import (Column, ColumnType, Constraint, ConstraintKind, Index,
                     IndexColumn, IndexSpec, Snapshot, Table)
from .errors import CHANGE_SCRIPT_HINT, SUPPORTED_SUMMARY, DDLError, Problem

#: Statements that express a *change* to an existing schema. Rejected on purpose, not
#: for lack of a parser -- see D41. Matched as a prefix because sqlglot names some
#: nodes after the whole statement (`TruncateTable`, not `Truncate`).
_CHANGE_VERBS = ("ALTER", "DROP", "RENAME", "TRUNCATE")


def _change_verb(text: str) -> str | None:
    """The change verb `text` starts with, if any -- else None.

    Takes either a sqlglot node class name (`TruncateTable`) or raw SQL
    (`RENAME TABLE a TO b`), because unparseable statements arrive as opaque
    `Command` nodes and must reach the same message.
    """
    head = text.strip().upper()
    return next((v for v in _CHANGE_VERBS if head.startswith(v)), None)
from .split import Statement, split_statements

# Column constraints we understand. Anything else on a column is a rejection, so a
# construct we haven't modeled can never be silently dropped (D21).
_KNOWN_COL_CONSTRAINTS = (
    exp.NotNullColumnConstraint,
    exp.DefaultColumnConstraint,
    exp.PrimaryKeyColumnConstraint,
    exp.UniqueColumnConstraint,
    exp.AutoIncrementColumnConstraint,
    exp.GeneratedAsIdentityColumnConstraint,
    exp.ZeroFillColumnConstraint,
)

_REJECT_COL_CONSTRAINTS = {
    exp.ComputedColumnConstraint: ("generated column", "GENERATED ALWAYS AS is not supported"),
    exp.CollateColumnConstraint: ("collate", "COLLATE is not supported"),
    exp.CharacterSetColumnConstraint: ("character set", "per-column CHARACTER SET is not supported"),
}

# Table properties that are storage noise rather than schema, safe to ignore.
_IGNORABLE_PROPERTIES = (
    exp.EngineProperty, exp.CharacterSetProperty, exp.CollateProperty,
    exp.AutoIncrementProperty, exp.SchemaCommentProperty, exp.RowFormatProperty,
)


@dataclass
class _PendingConstraint:
    """A constraint as written: column NAMES, resolved to ids after every table is known."""
    name: str
    kind: ConstraintKind
    columns: tuple[str, ...] = ()
    ref_table: str | None = None
    ref_columns: tuple[str, ...] = ()
    on_delete: str | None = None
    on_update: str | None = None
    expression: str | None = None
    line: int = 0


@dataclass
class _PendingIndex:
    name: str
    columns: tuple = ()
    unique: bool = False
    where: str | None = None
    line: int = 0


@dataclass
class _Draft:
    """A table mid-parse: real columns, plus references still held by name."""
    name: str
    line: int
    columns: list = field(default_factory=list)
    constraints: list = field(default_factory=list)
    indexes: list = field(default_factory=list)


class DialectAdapter:
    name: str = "generic"
    sqlglot_dialect: str = ""

    # ------------------------------------------------------------ public
    def parse(self, sql: str) -> Snapshot:
        """Parse DDL into a canonical snapshot.

        Two phases, because references are ids (D30) but DDL is written in names, and a
        foreign key may legally reference a table declared later in the file:

          1. read every statement into drafts holding names
          2. resolve every name to an id, reporting each failure with its line
        """
        problems: list[Problem] = []
        drafts: dict[str, _Draft] = {}

        for stmt in split_statements(sql, self.sqlglot_dialect):
            try:
                node = sqlglot.parse_one(stmt.sql, dialect=self.sqlglot_dialect,
                                         error_level=sqlglot.ErrorLevel.RAISE)
            except Exception as e:
                problems.append(Problem(line=self._error_line(e, stmt),
                                        message=_first_line(e)))
                continue
            self._dispatch(node, stmt, drafts, problems)

        snapshot = self._resolve(drafts, problems)
        if problems:
            raise DDLError(sorted(problems, key=lambda p: (p.line, p.message)))
        return snapshot

    # ---------------------------------------------------------- resolve
    def _resolve(self, drafts: dict, problems: list[Problem]) -> Snapshot:
        tables = {d.name: Table(name=d.name, columns=tuple(d.columns)) for d in drafts.values()}

        out = []
        for draft in drafts.values():
            t = tables[draft.name]
            constraints, indexes = [], []
            seen_names: dict[str, int] = {}

            # Count primary keys up front so the error names the real problem. Two
            # inline PRIMARY KEYs also collide on the auto-generated name `pk_<table>`,
            # and reporting that instead would blame our naming scheme for the user's
            # invalid SQL.
            pk_lines = [c.line for c in draft.constraints
                        if c.kind is ConstraintKind.PRIMARY_KEY]
            if len(pk_lines) > 1:
                problems.append(Problem(
                    line=min(pk_lines),
                    message=f"table {draft.name!r} declares {len(pk_lines)} primary keys "
                            f"(lines {', '.join(str(l) for l in sorted(set(pk_lines)))}); "
                            "a table may have at most one",
                    hint="use a single composite PRIMARY KEY (a, b) if you meant both "
                         "columns"))

            for pc in draft.constraints:
                if pc.kind is ConstraintKind.PRIMARY_KEY and len(pk_lines) > 1:
                    continue   # already reported, precisely, above
                if pc.name in seen_names:
                    problems.append(Problem(
                        line=pc.line,
                        message=f"duplicate constraint name {pc.name!r} on {draft.name} "
                                f"(also declared on line {seen_names[pc.name]})"))
                    continue
                seen_names[pc.name] = pc.line
                col_ids = self._resolve_columns(t, pc.columns, pc, draft, problems,
                                                what="constraint")
                ref_table = tables.get(pc.ref_table) if pc.ref_table else None
                if pc.ref_table and ref_table is None:
                    problems.append(Problem(
                        line=pc.line,
                        message=f"foreign key {pc.name!r} on {draft.name} references "
                                f"unknown table {pc.ref_table!r}"))
                    continue
                ref_ids = (self._resolve_columns(ref_table, pc.ref_columns, pc, draft,
                                                 problems, what="foreign key target")
                           if ref_table else ())
                if col_ids is None or ref_ids is None:
                    continue
                constraints.append(Constraint(
                    name=pc.name, kind=pc.kind, column_ids=col_ids,
                    ref_table_id=ref_table.id if ref_table else None,
                    ref_column_ids=ref_ids, on_delete=pc.on_delete,
                    on_update=pc.on_update, expression=pc.expression))

            seen_index_names: dict[str, int] = {}
            for pi in draft.indexes:
                if pi.name in seen_index_names:
                    problems.append(Problem(
                        line=pi.line,
                        message=f"duplicate index name {pi.name!r} on {draft.name} "
                                f"(also declared on line {seen_index_names[pi.name]})"))
                    continue
                seen_index_names[pi.name] = pi.line
                cols = []
                bad = False
                for spec in pi.columns:
                    col = t.column(spec.column)
                    if col is None:
                        problems.append(Problem(
                            line=pi.line,
                            message=f"index {pi.name!r} on {draft.name} references "
                                    f"unknown column {spec.column!r}"))
                        bad = True
                        continue
                    cols.append(IndexColumn(column_id=col.id, desc=spec.desc,
                                            prefix_length=spec.prefix_length))
                if not bad:
                    indexes.append(Index(name=pi.name, columns=tuple(cols),
                                         unique=pi.unique, where=pi.where))

            out.append(Table(id=t.id, name=t.name, columns=t.columns,
                             constraints=tuple(constraints), indexes=tuple(indexes)))
        return Snapshot(dialect=self.name, tables=tuple(out))

    @staticmethod
    def _resolve_columns(table, names, pc, draft, problems, *, what: str):
        ids = []
        for n in names:
            col = table.column(n)
            if col is None:
                problems.append(Problem(
                    line=pc.line,
                    message=f"{what} {pc.name!r} on {draft.name} references unknown "
                            f"column {table.name}.{n!r}"))
                return None
            ids.append(col.id)
        return tuple(ids)

    # ---------------------------------------------------------- dispatch
    def _dispatch(self, node, stmt, drafts, problems):
        if isinstance(node, exp.Command):
            label = self._command_label(stmt.sql)
            verb = _change_verb(stmt.sql)
            problems.append(Problem(
                line=stmt.line,
                message=(f"{verb} describes a change to a schema, not a schema: "
                         f"{label}" if verb
                         else f"unsupported statement: {label}"),
                hint=CHANGE_SCRIPT_HINT if verb else SUPPORTED_SUMMARY))
            return

        if not isinstance(node, exp.Create):
            node_name = type(node).__name__.upper()
            verb = _change_verb(node_name)
            problems.append(Problem(
                line=stmt.line,
                message=(f"{verb} describes a change to a schema, not a schema; "
                         f"only CREATE TABLE and CREATE INDEX are read" if verb
                         else f"only CREATE TABLE and CREATE INDEX are supported, "
                              f"got {node_name}"),
                hint=CHANGE_SCRIPT_HINT if verb else SUPPORTED_SUMMARY))
            return

        kind = (node.args.get("kind") or "").upper()
        if kind == "TABLE":
            self._read_table(node, stmt, drafts, problems)
        elif kind == "INDEX":
            self._read_index(node, stmt, drafts, problems)
        else:
            problems.append(Problem(
                line=stmt.line,
                message=f"unsupported construct: CREATE {kind or 'UNKNOWN'}",
                hint=SUPPORTED_SUMMARY))

    # ------------------------------------------------------------- table
    def _read_table(self, node, stmt, drafts, problems):
        schema_expr = node.this
        if not isinstance(schema_expr, exp.Schema):
            problems.append(Problem(line=stmt.line,
                                    message="CREATE TABLE without a column list"))
            return

        table_node = schema_expr.this
        qualifier = _qualifier(table_node)
        if qualifier:
            bare = table_node.name
            problems.append(Problem(
                line=stmt.line,
                message=f"schema-qualified table name "
                        f"{_qualified_text(table_node)!r} is not supported",
                hint=f"this tool versions one schema at a time; accepting the "
                     f"qualifier would let {qualifier}.{bare} and any other "
                     f"X.{bare} collide into one object. Drop the "
                     f"{qualifier!r} prefix."))
            return
        name = self.fold_identifier(table_node.name)

        for prop in (node.args.get("properties").expressions
                     if node.args.get("properties") else []):
            if isinstance(prop, exp.PartitionedByProperty):
                problems.append(Problem(line=stmt.line,
                                        message="unsupported construct: PARTITION BY",
                                        hint=SUPPORTED_SUMMARY))
            elif not isinstance(prop, _IGNORABLE_PROPERTIES):
                problems.append(Problem(
                    line=stmt.line,
                    message=f"unsupported table option: {type(prop).__name__}",
                    hint=SUPPORTED_SUMMARY))

        if name in drafts:
            problems.append(Problem(line=stmt.line, message=f"duplicate table {name!r}"))
            return
        draft = _Draft(name=name, line=stmt.line)
        seen: dict[str, str] = {}

        for d in schema_expr.expressions:
            if isinstance(d, exp.ColumnDef):
                col = self._read_column(d, stmt, name, draft, problems)
                if col is None:
                    continue
                key = col.name if self.columns_case_sensitive else col.name.lower()
                if key in seen:
                    problems.append(Problem(
                        line=stmt.line,
                        message=f"duplicate column {name}.{col.name!r} "
                                f"(already declared as {seen[key]!r})",
                        hint=None if self.columns_case_sensitive else
                             "column names are case-insensitive in this dialect"))
                    continue
                seen[key] = col.name
                draft.columns.append(col)
            else:
                self._read_table_constraint(d, stmt, name, draft, problems)

        drafts[name] = draft

    def _read_column(self, d, stmt, table, draft, problems) -> Column | None:
        col_name = self.fold_identifier(d.name)
        kind = d.args.get("kind")
        if kind is None:
            problems.append(Problem(line=stmt.line,
                                    message=f"column {table}.{col_name!r} has no type"))
            return None

        nullable, default, autoinc = True, None, False
        # ZEROFILL arrives as a column *constraint*, not a type modifier, but it
        # changes how the type is canonicalised (it disqualifies TINYINT(1) from
        # being boolean), so flags must be collected before canonical_type runs.
        flags = {type(c.args.get("kind")).__name__ for c in d.constraints}
        for c in d.constraints:
            k = c.args.get("kind")
            for cls, (label, msg) in _REJECT_COL_CONSTRAINTS.items():
                if isinstance(k, cls):
                    problems.append(Problem(
                        line=stmt.line,
                        message=f"unsupported construct on {table}.{col_name}: {label} -- {msg}",
                        hint=SUPPORTED_SUMMARY))
                    return None
            if isinstance(k, exp.NotNullColumnConstraint):
                # The node is present for both `NOT NULL` and an explicit `NULL`;
                # `allow_null` distinguishes them.
                nullable = bool(k.args.get("allow_null"))
            elif isinstance(k, exp.DefaultColumnConstraint):
                default = self.normalize_default(k.this)
            elif isinstance(k, exp.PrimaryKeyColumnConstraint):
                nullable = False
                draft.constraints.append(_PendingConstraint(
                    name=f"pk_{table}", kind=ConstraintKind.PRIMARY_KEY,
                    columns=(col_name,), line=stmt.line))
            elif isinstance(k, exp.UniqueColumnConstraint):
                draft.constraints.append(_PendingConstraint(
                    name=f"uq_{table}_{col_name}", kind=ConstraintKind.UNIQUE,
                    columns=(col_name,), line=stmt.line))
            elif isinstance(k, (exp.AutoIncrementColumnConstraint,
                                exp.GeneratedAsIdentityColumnConstraint)):
                autoinc = True
            elif not isinstance(k, _KNOWN_COL_CONSTRAINTS):
                problems.append(Problem(
                    line=stmt.line,
                    message=f"unsupported column constraint on {table}.{col_name}: "
                            f"{type(k).__name__}",
                    hint=SUPPORTED_SUMMARY))
                return None

        ctype, type_autoinc = self.canonical_type(kind, flags)
        return Column(name=col_name, type=ctype, nullable=nullable, default=default,
                      autoincrement=autoinc or type_autoinc)

    def _read_table_constraint(self, d, stmt, table, draft, problems):
        # MySQL inline KEY/INDEX definitions land here as constraints, not indexes.
        if isinstance(d, exp.IndexColumnConstraint):
            draft.indexes.append(self._index_from_constraint(d, stmt.line))
            return
        if isinstance(d, exp.PrimaryKey):
            draft.constraints.append(_PendingConstraint(
                name=f"pk_{table}", kind=ConstraintKind.PRIMARY_KEY,
                columns=tuple(self.fold_identifier(_col_name(x)) for x in d.expressions),
                line=stmt.line))
            return
        if isinstance(d, exp.UniqueColumnConstraint):
            name = self.fold_identifier(d.args["this"].name) if d.args.get("this") else None
            cols = tuple(self.fold_identifier(_col_name(x)) for x in _unique_columns(d))
            draft.constraints.append(_PendingConstraint(
                name=name or f"uq_{table}_{'_'.join(cols)}",
                kind=ConstraintKind.UNIQUE, columns=cols, line=stmt.line))
            return
        if isinstance(d, exp.Constraint):
            self._read_named_constraint(d, stmt, table, draft, problems)
            return
        if isinstance(d, exp.ForeignKey):
            draft.constraints.append(self._foreign_key(d, table, None, stmt.line))
            return
        problems.append(Problem(
            line=stmt.line,
            message=f"unsupported table-level construct in {table}: {type(d).__name__}",
            hint=SUPPORTED_SUMMARY))

    def _read_named_constraint(self, d, stmt, table, draft, problems):
        cname = self.fold_identifier(d.this.name) if d.this else None
        for inner in d.expressions:
            if isinstance(inner, exp.UniqueColumnConstraint):
                cols = tuple(self.fold_identifier(_col_name(x)) for x in _unique_columns(inner))
                draft.constraints.append(_PendingConstraint(
                    name=cname, kind=ConstraintKind.UNIQUE, columns=cols, line=stmt.line))
            elif isinstance(inner, exp.CheckColumnConstraint):
                draft.constraints.append(_PendingConstraint(
                    name=cname, kind=ConstraintKind.CHECK, line=stmt.line,
                    expression=inner.this.sql(dialect=self.sqlglot_dialect)))
            elif isinstance(inner, exp.ForeignKey):
                draft.constraints.append(self._foreign_key(inner, table, cname, stmt.line))
            elif isinstance(inner, exp.PrimaryKey):
                draft.constraints.append(_PendingConstraint(
                    name=cname, kind=ConstraintKind.PRIMARY_KEY, line=stmt.line,
                    columns=tuple(self.fold_identifier(_col_name(x))
                                  for x in inner.expressions)))
            else:
                problems.append(Problem(
                    line=stmt.line,
                    message=f"unsupported constraint {cname!r} in {table}: "
                            f"{type(inner).__name__}",
                    hint=SUPPORTED_SUMMARY))

    def _foreign_key(self, d, table, name, line) -> _PendingConstraint:
        cols = tuple(self.fold_identifier(_col_name(x)) for x in d.expressions)
        ref = d.args.get("reference")
        ref_schema = ref.this if ref else None
        ref_table_node = ref_schema.this if ref_schema else None
        # A qualifier here would silently resolve to the wrong table, so keep the
        # qualifier in the name and let resolution fail loudly with it visible.
        ref_table = (_qualified_text(ref_table_node) if _qualifier(ref_table_node)
                     else self.fold_identifier(ref_table_node.name)) \
            if ref_table_node else None
        ref_cols = tuple(self.fold_identifier(_col_name(x))
                         for x in (ref_schema.expressions if ref_schema else []))
        on_delete, on_update = _referential_actions(ref)
        return _PendingConstraint(
            name=name or f"fk_{table}_{'_'.join(cols)}",
            kind=ConstraintKind.FOREIGN_KEY, columns=cols,
            ref_table=ref_table, ref_columns=ref_cols,
            on_delete=on_delete, on_update=on_update, line=line)

    # ------------------------------------------------------------- index
    def _read_index(self, node, stmt, drafts, problems):
        idx = node.this
        if not isinstance(idx, exp.Index):
            problems.append(Problem(line=stmt.line, message="unrecognised CREATE INDEX"))
            return
        table_node = idx.args["table"]
        table = (_qualified_text(table_node) if _qualifier(table_node)
                 else self.fold_identifier(table_node.name))
        if table not in drafts:
            problems.append(Problem(
                line=stmt.line,
                message=f"index {self.fold_identifier(idx.name)!r} references unknown "
                        f"table {table!r}"))
            return
        cols, where = self._index_columns(idx.args.get("params"))
        drafts[table].indexes.append(_PendingIndex(
            name=self.fold_identifier(idx.name), columns=cols,
            unique=bool(node.args.get("unique")), where=where, line=stmt.line))

    def _index_from_constraint(self, d, line) -> _PendingIndex:
        return _PendingIndex(
            name=self.fold_identifier(d.args["this"].name) if d.args.get("this") else "",
            columns=self._ordered_columns(d.args.get("expressions") or []),
            unique=False, line=line)

    def _index_columns(self, params) -> tuple[tuple[IndexColumn, ...], str | None]:
        if params is None:
            return (), None
        where = params.args.get("where")
        cols = self._ordered_columns(params.args.get("columns") or [])
        return cols, (where.sql(dialect=self.sqlglot_dialect) if where else None)

    def _ordered_columns(self, raw) -> tuple[IndexColumn, ...]:
        out = []
        for item in raw:
            desc = bool(item.args.get("desc")) if isinstance(item, exp.Ordered) else False
            inner = item.this if isinstance(item, exp.Ordered) else item
            out.append(self.index_column(inner, desc))
        return tuple(out)

    # --------------------------------------------------------- internals
    @staticmethod
    def _error_line(e: Exception, stmt: Statement) -> int:
        """sqlglot reports lines relative to the slice it was given; re-base them."""
        m = re.search(r"[Ll]ine (\d+)", str(e))
        return stmt.line + (int(m.group(1)) - 1) if m else stmt.line

    @staticmethod
    def _command_label(sql: str) -> str:
        words = re.findall(r"[A-Za-z_]+", sql)[:2]
        return " ".join(w.upper() for w in words) if words else "unknown"

    # -------------------------------------------------- dialect hooks
    columns_case_sensitive: bool = True

    def fold_identifier(self, name: str) -> str:
        raise NotImplementedError

    def canonical_type(self, kind, flags: set[str]) -> tuple[ColumnType, bool]:
        """-> (canonical type, implies_autoincrement).

        `flags` holds the sqlglot class names of the column's constraints, because
        some type-affecting modifiers (ZEROFILL) are parsed as constraints.
        """
        raise NotImplementedError

    def normalize_default(self, node) -> str | None:
        raise NotImplementedError

    def index_column(self, node, desc: bool) -> IndexSpec:
        """Return a NAME-based spec. Resolution to ids happens in _resolve()."""
        raise NotImplementedError


def _col_name(x) -> str:
    return x.name if hasattr(x, "name") else str(x)


def _unique_columns(d) -> list:
    inner = d.this
    if isinstance(inner, exp.Schema):
        return list(inner.expressions)
    if isinstance(inner, (exp.Tuple, exp.Paren)):
        return list(inner.expressions or [inner.this])
    return [inner] if inner is not None else []


def _qualifier(table_node) -> str | None:
    """The schema/database qualifier on a table reference, if any."""
    if table_node is None:
        return None
    db = table_node.args.get("db")
    catalog = table_node.args.get("catalog")
    for part in (catalog, db):
        if part is not None and getattr(part, "name", None):
            return part.name
    return None


def _qualified_text(table_node) -> str:
    q = _qualifier(table_node)
    return f"{q}.{table_node.name}" if q else table_node.name


def _referential_actions(ref) -> tuple[str | None, str | None]:
    """ON DELETE / ON UPDATE arrive as opaque option strings on the Reference node."""
    on_delete = on_update = None
    for opt in ((ref.args.get("options") or []) if ref else []):
        text = (opt if isinstance(opt, str) else opt.sql()).upper()
        if "ON DELETE" in text:
            on_delete = text.split("ON DELETE", 1)[1].strip().lower() or None
        elif "ON UPDATE" in text:
            on_update = text.split("ON UPDATE", 1)[1].strip().lower() or None
    return on_delete, on_update


def _first_line(e: Exception) -> str:
    return str(e).splitlines()[0]
