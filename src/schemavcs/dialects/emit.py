"""Operation -> SQL. The dialect-specific half of migration generation.

The planner decided what happens and in what order (D35). Everything here is about
*text*, and about the places where two engines genuinely disagree. There are more of
those than the phrase "just generate SQL" suggests:

* **MySQL `MODIFY COLUMN` restates the whole column.** Emitting `MODIFY c BIGINT` to
  change a type silently drops that column's NOT NULL and DEFAULT, because MySQL
  treats the clause as a full redefinition. Postgres `ALTER COLUMN ... TYPE` changes
  only the type. Getting this wrong produces DDL that succeeds and quietly discards
  constraints -- the worst kind of bug.
* **Postgres needs `USING` for non-implicit casts**; MySQL converts or errors.
* **Transaction semantics differ**, which is why the two renderers disagree about
  wrapping at all (D18, E-80/E-81).
* **Indexes live in different namespaces.** `DROP INDEX name` in Postgres,
  `ALTER TABLE t DROP INDEX name` in MySQL.

Where a construct simply cannot be expressed, the emitter raises rather than dropping
it. Silently emitting a non-partial index for a partial one produces a database that
looks right and enforces something different.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..engine.plan import OpKind, Operation, Plan, Safety
from ..model.objects import ConstraintKind

NO_ROLLBACK = "NO ROLLBACK BEYOND THIS POINT -- the steps above cannot be undone"


class UnrepresentableError(Exception):
    """The target engine has no way to express this construct.

    Raised rather than approximated. An approximation that is not flagged is a lie,
    and flagging every approximation properly is its own feature (cut for now, D37) --
    so the honest interim behaviour is to refuse and say what and why.
    """

    def __init__(self, dialect: str, what: str, detail: str):
        self.dialect, self.what, self.detail = dialect, what, detail
        super().__init__(f"{dialect} cannot express {what}: {detail}")


@dataclass(frozen=True)
class Step:
    sql: str
    #: Rendered as a comment above the statement.
    note: str | None = None
    irreversible: bool = False


@dataclass(frozen=True)
class Script:
    dialect: str
    steps: tuple[Step, ...] = ()
    #: Whether the whole script runs inside one transaction. False is not a bug --
    #: see D18.
    transactional: bool = False

    def __len__(self) -> int:
        return len(self.steps)

    @property
    def statements(self) -> tuple[str, ...]:
        return tuple(s.sql for s in self.steps)

    def text(self) -> str:
        lines: list[str] = []
        if self.transactional:
            lines.append("BEGIN;")
        for s in self.steps:
            if s.note:
                lines.append(f"-- {s.note}")
            lines.append(s.sql)
        if self.transactional:
            lines.append("COMMIT;")
        return "\n".join(lines)


# ==================================================================== base emitter
class Emitter:
    name = ""
    quote_char = '"'
    transactional_ddl = True
    supports_partial_index = True
    supports_index_prefix = False
    supports_unsigned = False

    # ------------------------------------------------------------- public
    def emit(self, plan: Plan) -> Script:
        steps = [self._step(o) for o in plan.operations]
        steps = self._mark_irreversible(steps)
        return Script(dialect=self.name, steps=tuple(steps),
                      transactional=self.transactional_ddl)

    def _mark_irreversible(self, steps: list[Step]) -> list[Step]:
        """Annotate the FIRST irreversible step only.

        One marker at the boundary is actionable -- everything above it is recoverable,
        everything below is not. A marker on every destructive statement is noise that
        stops being read (E-83).
        """
        for i, s in enumerate(steps):
            if s.irreversible and not self.transactional_ddl:
                steps[i] = Step(s.sql, note=NO_ROLLBACK, irreversible=True)
                break
        return steps

    def q(self, ident: str) -> str:
        c = self.quote_char
        return f"{c}{ident.replace(c, c + c)}{c}"

    # ------------------------------------------------------------ dispatch
    def _step(self, o: Operation) -> Step:
        fn = getattr(self, f"_op_{o.kind.value}")
        note = "temporary name, to avoid a collision mid-migration" if o.temp else None
        return Step(fn(o), note=note,
                    irreversible=o.safety in (Safety.LOSSY, Safety.UNSAFE))

    # ------------------------------------------------------------- tables
    def _op_create_table(self, o) -> str:
        parts = [f"  {self.column_def(c)}" for c in (o.column or ())]
        parts += [f"  {self.constraint_def(r)}" for r in o.inline]
        body = ",\n".join(parts)
        return f"CREATE TABLE {self.q(o.table)} (\n{body}\n);"

    def _op_drop_table(self, o) -> str:
        return f"DROP TABLE {self.q(o.table)};"

    def _op_rename_table(self, o) -> str:
        return f"ALTER TABLE {self.q(o.table)} RENAME TO {self.q(o.new_name)};"

    # ------------------------------------------------------------ columns
    def _op_add_column(self, o) -> str:
        return (f"ALTER TABLE {self.q(o.table)} ADD COLUMN "
                f"{self.column_def(o.column)};")

    def _op_drop_column(self, o) -> str:
        return f"ALTER TABLE {self.q(o.table)} DROP COLUMN {self.q(o.name)};"

    def _op_rename_column(self, o) -> str:
        return (f"ALTER TABLE {self.q(o.table)} RENAME COLUMN {self.q(o.name)} "
                f"TO {self.q(o.new_name)};")

    # ------------------------------------------------------------ indexes
    def _op_create_index(self, o) -> str:
        idx = o.index
        if idx.where and not self.supports_partial_index:
            raise UnrepresentableError(
                self.name, "a partial index",
                f"index {idx.name!r} has a WHERE clause, which this engine has no "
                f"equivalent for; the index would silently cover every row")
        for ic in idx.columns:
            if ic.prefix_length and not self.supports_index_prefix:
                raise UnrepresentableError(
                    self.name, "an index prefix length",
                    f"index {idx.name!r} indexes only the first "
                    f"{ic.prefix_length} characters of a column")
        self.check_indexable(o.columns, o.column_types, idx.columns, idx.name)
        unique = "UNIQUE " if idx.unique else ""
        cols = ", ".join(self.index_column(name, ic)
                         for name, ic in zip(o.columns, idx.columns))
        where = f" WHERE {idx.where}" if idx.where else ""
        return (f"CREATE {unique}INDEX {self.q(idx.name)} ON {self.q(o.table)} "
                f"({cols}){where};")

    def index_column(self, name: str, ic) -> str:
        return f"{self.q(name)}{' DESC' if ic.desc else ''}"

    def check_indexable(self, names, types, index_columns, what: str) -> None:
        """Hook: may this engine index these columns at all? Default: yes."""

    # -------------------------------------------------------- constraints
    def _op_add_constraint(self, o) -> str:
        if o.constraint.kind in (ConstraintKind.UNIQUE, ConstraintKind.PRIMARY_KEY):
            self.check_indexable(o.columns, o.column_types, None, o.name)
        return (f"ALTER TABLE {self.q(o.table)} ADD "
                f"{self.constraint_def(_R(o))};")

    def _op_drop_constraint(self, o) -> str:
        return f"ALTER TABLE {self.q(o.table)} DROP CONSTRAINT {self.q(o.name)};"

    def constraint_def(self, r) -> str:
        k = r.constraint
        if k.kind in (ConstraintKind.UNIQUE, ConstraintKind.PRIMARY_KEY):
            self.check_indexable(r.columns, r.column_types, None, k.name)
        cols = ", ".join(self.q(c) for c in r.columns)
        head = f"CONSTRAINT {self.q(k.name)} "
        if k.kind is ConstraintKind.PRIMARY_KEY:
            return f"{head}PRIMARY KEY ({cols})"
        if k.kind is ConstraintKind.UNIQUE:
            return f"{head}UNIQUE ({cols})"
        if k.kind is ConstraintKind.CHECK:
            return f"{head}CHECK ({k.expression})"
        ref_cols = ", ".join(self.q(c) for c in r.ref_columns)
        out = (f"{head}FOREIGN KEY ({cols}) REFERENCES "
               f"{self.q(r.ref_table)} ({ref_cols})")
        if k.on_delete:
            out += f" ON DELETE {k.on_delete.upper()}"
        if k.on_update:
            out += f" ON UPDATE {k.on_update.upper()}"
        return out

    # ------------------------------------------------------- column text
    def column_def(self, col) -> str:
        parts = [self.q(col.name), self.type_sql(col)]
        if not col.nullable:
            parts.append("NOT NULL")
        # After NOT NULL, which is the conventional order in both engines' own dumps.
        parts += self.identity_clause(col)
        if col.default is not None:
            parts.append(f"DEFAULT {col.default}")
        return " ".join(parts)

    def identity_clause(self, col) -> list[str]:
        return []

    def type_sql(self, col) -> str:
        t = col.type
        if t.unsigned and not self.supports_unsigned:
            raise UnrepresentableError(
                self.name, "an unsigned integer",
                f"column {col.name!r} is {t.render()}; this engine has no unsigned "
                f"types, so the permitted range would change")
        if t.base not in self.TYPES:
            # Passing an unrecognised base through verbatim is the tempting default and
            # it is wrong (D39, D48): the model is deliberately permissive about type
            # names, so a typo like `somethign` survives all the way here and becomes
            # DDL the server rejects at 3am. The emitter is the layer that knows what a
            # given engine can actually express, so it is the layer that must refuse.
            raise UnrepresentableError(
                self.name, f"the type {t.base!r}",
                f"column {col.name!r} is declared {t.render()}, which this engine has no "
                f"type for. Supported: {', '.join(sorted(self.TYPES))}")
        base = self.TYPES[t.base]
        return base + (f"({','.join(str(p) for p in t.params)})" if t.params else "")

    TYPES: dict[str, str] = {}


class _R:
    """Adapts an `Operation` to the resolved-constraint shape `constraint_def` wants,
    so inline and standalone constraints render through one code path."""

    def __init__(self, o):
        self.constraint = o.constraint
        self.columns = o.columns
        self.ref_table = o.ref_table
        self.ref_columns = o.ref_columns
        self.column_types = o.column_types


# ======================================================================= Postgres
class PostgresEmitter(Emitter):
    """Postgres wraps everything in one transaction, which is the single biggest
    operational difference between the two engines (E-80)."""

    name = "postgres"
    quote_char = '"'
    transactional_ddl = True
    supports_partial_index = True
    supports_index_prefix = False
    supports_unsigned = False

    TYPES = {"int": "integer", "bigint": "bigint", "smallint": "smallint",
             "boolean": "boolean", "text": "text", "varchar": "varchar",
             "char": "char", "decimal": "numeric", "double": "double precision",
             "real": "real", "timestamp": "timestamp", "timestamptz": "timestamptz",
             "date": "date", "time": "time", "json": "json", "jsonb": "jsonb",
             "uuid": "uuid", "bytea": "bytea"}

    def identity_clause(self, col) -> list[str]:
        # Preferred over SERIAL: SERIAL is a macro that creates a sequence with its
        # own name and ownership rules, which makes it awkward to diff.
        return ["GENERATED BY DEFAULT AS IDENTITY"] if col.autoincrement else []

    def _op_alter_column_type(self, o) -> str:
        col = o.column
        using = ""
        if o.requires_cast:
            # Postgres refuses non-implicit casts without being told how. Omitting
            # this produces a migration that fails at run time, not at plan time.
            using = f" USING {self.q(col.name)}::{self.type_sql(col)}"
        return (f"ALTER TABLE {self.q(o.table)} ALTER COLUMN {self.q(col.name)} "
                f"TYPE {self.type_sql(col)}{using};")

    def _op_set_not_null(self, o) -> str:
        return (f"ALTER TABLE {self.q(o.table)} ALTER COLUMN {self.q(o.name)} "
                f"SET NOT NULL;")

    def _op_drop_not_null(self, o) -> str:
        return (f"ALTER TABLE {self.q(o.table)} ALTER COLUMN {self.q(o.name)} "
                f"DROP NOT NULL;")

    def _op_set_default(self, o) -> str:
        return (f"ALTER TABLE {self.q(o.table)} ALTER COLUMN {self.q(o.name)} "
                f"SET DEFAULT {o.column.default};")

    def _op_drop_default(self, o) -> str:
        return (f"ALTER TABLE {self.q(o.table)} ALTER COLUMN {self.q(o.name)} "
                f"DROP DEFAULT;")

    # Indexes are schema-level objects in Postgres, not table-level.
    def _op_drop_index(self, o) -> str:
        return f"DROP INDEX {self.q(o.name)};"

    def _op_rename_index(self, o) -> str:
        return f"ALTER INDEX {self.q(o.name)} RENAME TO {self.q(o.new_name)};"


# ========================================================================== MySQL
class MySQLEmitter(Emitter):
    """MySQL 8.0. Not wrapped in a transaction, because it cannot be (D18).

    Each DDL statement commits implicitly, so a failure halfway leaves the schema
    partially migrated with no way back. Emitting `BEGIN` here would be worse than
    useless -- it would imply a rollback guarantee the engine does not provide.
    """

    name = "mysql"
    quote_char = "`"
    transactional_ddl = False
    supports_partial_index = False
    supports_index_prefix = True
    supports_unsigned = True

    TYPES = {"int": "int", "bigint": "bigint", "smallint": "smallint",
             "boolean": "tinyint(1)", "text": "text", "varchar": "varchar",
             "char": "char", "decimal": "decimal", "double": "double",
             "real": "float", "timestamp": "datetime", "timestamptz": "timestamp",
             "date": "date", "time": "time", "json": "json", "blob": "blob"}

    #: Types this engine will not accept without a length. Postgres treats a bare
    #: `varchar` as unbounded; MySQL rejects the statement outright.
    NEEDS_LENGTH = ("varchar", "char")

    def type_sql(self, col) -> str:
        if col.type.base in self.NEEDS_LENGTH and not col.type.params:
            raise UnrepresentableError(
                self.name, f"an unbounded {col.type.base.upper()}",
                f"column {col.name!r} is {col.type.render()} with no length. This engine "
                f"has no unbounded {col.type.base.upper()}; give it a length like "
                f"{col.type.base}(255), or use TEXT")
        out = super().type_sql(col)
        return f"{out} unsigned" if col.type.unsigned else out

    def identity_clause(self, col) -> list[str]:
        return ["AUTO_INCREMENT"] if col.autoincrement else []

    def index_column(self, name: str, ic) -> str:
        prefix = f"({ic.prefix_length})" if ic.prefix_length else ""
        return f"{self.q(name)}{prefix}{' DESC' if ic.desc else ''}"

    #: MySQL stores these out of line, so it cannot index them without being told how
    #: many leading bytes to use. Found by applying generated DDL to a real server --
    #: no amount of string assertion would have surfaced it.
    UNBOUNDED = {"text", "blob", "json"}

    def check_indexable(self, names, types, index_columns, what: str) -> None:
        for i, (name, t) in enumerate(zip(names, types or ())):
            if t is None or t.base not in self.UNBOUNDED:
                continue
            prefixed = (index_columns is not None
                        and i < len(index_columns)
                        and index_columns[i].prefix_length)
            if prefixed:
                continue
            raise UnrepresentableError(
                self.name, f"an index on {t.base.upper()} column {name!r}",
                f"{what!r} covers {name} ({t.render()}), and this engine requires a "
                f"prefix length for out-of-line types. Give the index column an "
                f"explicit prefix length, or narrow the column to VARCHAR(n)")

    # `MODIFY COLUMN` is a full redefinition: any clause omitted is DISCARDED. So
    # every attribute change re-emits the complete definition, and all four of these
    # operations collapse into one statement shape.
    def _modify(self, o) -> str:
        return (f"ALTER TABLE {self.q(o.table)} MODIFY COLUMN "
                f"{self.column_def(o.column)};")

    _op_alter_column_type = _modify
    _op_set_not_null = _modify
    _op_drop_not_null = _modify

    def _op_set_default(self, o) -> str:
        return (f"ALTER TABLE {self.q(o.table)} ALTER COLUMN {self.q(o.name)} "
                f"SET DEFAULT {o.column.default};")

    def _op_drop_default(self, o) -> str:
        return (f"ALTER TABLE {self.q(o.table)} ALTER COLUMN {self.q(o.name)} "
                f"DROP DEFAULT;")

    def _op_drop_index(self, o) -> str:
        return f"ALTER TABLE {self.q(o.table)} DROP INDEX {self.q(o.name)};"

    def _op_rename_index(self, o) -> str:
        return (f"ALTER TABLE {self.q(o.table)} RENAME INDEX {self.q(o.name)} "
                f"TO {self.q(o.new_name)};")

    def _op_drop_constraint(self, o) -> str:
        # MySQL needs the constraint KIND to drop it; only 8.0.19+ accepts the
        # generic form for foreign keys.
        k = o.constraint
        if k is not None and k.kind is ConstraintKind.FOREIGN_KEY:
            return f"ALTER TABLE {self.q(o.table)} DROP FOREIGN KEY {self.q(o.name)};"
        if k is not None and k.kind is ConstraintKind.PRIMARY_KEY:
            return f"ALTER TABLE {self.q(o.table)} DROP PRIMARY KEY;"
        return f"ALTER TABLE {self.q(o.table)} DROP CONSTRAINT {self.q(o.name)};"


EMITTERS = {"postgres": PostgresEmitter, "mysql": MySQLEmitter}


def emit(plan, dialect: str) -> Script:
    """Render a plan as SQL for one engine."""
    try:
        cls = EMITTERS[dialect]
    except KeyError:
        raise ValueError(
            f"no emitter for dialect {dialect!r}; have {sorted(EMITTERS)}") from None
    return cls().emit(plan)
