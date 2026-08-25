"""Form input -> one editor call -> one commit message.

Every edit here goes through `SnapshotEditor`, which is the whole point: the editor
preserves object identity across a change, so renaming a column in the browser produces
`same id, new name` and stays mergeable. A UI that let you paste a replacement schema
would look more flexible and would quietly destroy every identity in it (D22) -- so this
is the only way the schema changes after the initial import.

Each handler returns the commit message, which doubles as the confirmation text. Writing
it here rather than in the route keeps "what happened" next to "what was done".
"""
from __future__ import annotations

from ..dialects import known_types, type_params


class EditError(Exception):
    """Bad or missing form input. Distinct from SchemaError, which means the *schema*
    rejected an otherwise well-formed request."""


def _req(form: dict, key: str) -> str:
    v = str(form.get(key, "")).strip()
    if not v:
        raise EditError(f"{key.replace('_', ' ')} is required")
    return v


def _opt(form: dict, key: str) -> str | None:
    v = str(form.get(key, "")).strip()
    return v or None


def _cols(form: dict, key: str = "columns") -> list[str]:
    raw = _req(form, key)
    cols = [c.strip() for c in raw.split(",") if c.strip()]
    if not cols:
        raise EditError("list at least one column")
    return cols


def _bool(form: dict, key: str) -> bool:
    return str(form.get(key, "")).lower() in ("1", "true", "on", "yes")


def _type(form: dict, dialect: str, key: str = "type") -> str:
    """A type the target engine can express, assembled from a picker and a size box.

    The canonical model is permissive on purpose -- it is dialect-neutral (D5) and
    carries more than any single engine (D6) -- so `somethign` parses happily and only
    fails when a server rejects the DDL. The editor is the first layer that knows which
    engine this schema targets, so it is the first that can say no (D48).

    The base name arrives from a `<select>`, because the set of valid names is fixed and
    a free text box invites a typo the tool then has to reject (D50). Only the size is
    typed, because that part genuinely is open: `varchar(255)` is not enumerable.
    """
    from ..model.types import ColumnType

    allowed = known_types(dialect)
    base = str(form.get(f"{key}_base", "")).strip()

    if base:
        params = str(form.get(f"{key}_params", "")).strip()
        if base not in allowed:
            raise EditError(
                f"{dialect} has no type called {base!r}. Available: {', '.join(allowed)}")
        return _with_params(base, params)

    # No picker in the payload: a caller using the plain `type` field directly.
    text = _req(form, key)
    try:
        parsed = ColumnType.parse(text)
    except ValueError:
        raise EditError(
            f"{text!r} is not a type. Write a name, optionally with a size: "
            f"varchar(255), decimal(10,2), timestamptz") from None

    if parsed.base not in allowed:
        raise EditError(
            f"{dialect} has no type called {parsed.base!r}. Available: "
            f"{', '.join(allowed)}")
    return text


def _with_params(base: str, params: str) -> str:
    """Attach a size to a base type, refusing sizes that mean nothing.

    Silently dropping `int(5)` would be worse than refusing it: the user asked for
    something specific and would get something else without being told.
    """
    accepts = type_params(base)
    if not params:
        return base
    if not accepts:
        raise EditError(
            f"{base} does not take a size — leave that box empty. Types that do: "
            f"varchar, char, decimal")

    parts = [p.strip() for p in params.replace("(", "").replace(")", "").split(",")
             if p.strip()]
    if not all(p.isdigit() for p in parts):
        raise EditError(f"a size has to be a number, not {params!r}")
    if len(parts) > len(accepts):
        raise EditError(
            f"{base} takes {len(accepts)} number(s) — {', '.join(accepts)} — "
            f"but got {len(parts)}")
    return f"{base}({','.join(parts)})"


# ------------------------------------------------------------------ tables
def add_table(e, f, dialect):
    name = _req(f, "name")
    e.add_table(name)
    return f"create table {name}"


def drop_table(e, f, dialect):
    name = _req(f, "table")
    e.drop_table(name)
    return f"drop table {name}"


def rename_table(e, f, dialect):
    old, new = _req(f, "table"), _req(f, "new_name")
    e.rename_table(old, new)
    return f"rename table {old} to {new}"


# ----------------------------------------------------------------- columns
def add_column(e, f, dialect):
    table, name, type_ = _req(f, "table"), _req(f, "name"), _type(f, dialect)
    e.add_col(table, name, type_, nullable=not _bool(f, "not_null"),
              default=_opt(f, "default"))
    return f"add column {table}.{name} {type_}"


def alter_column(e, f, dialect):
    """One form, one commit, for everything about a single column.

    The editor used to expose rename / retype / nullability / default as four separate
    forms, each with its own column picker. That is four times the chrome for one mental
    action ("change this column"), and it puts the answer to "which column?" four
    screens-worth apart from the column itself (D49).

    Only fields that actually changed are applied, so the commit message describes the
    real edit rather than restating the whole column -- and a rename still reads as a
    rename, which is the behaviour this whole tool is about.
    """
    path = _req(f, "path")
    table, current_name = path.rsplit(".", 1)
    col = e.snapshot.col(path)
    if col is None:
        raise EditError(f"no such column: {path}")

    done = []

    new_type = str(f.get("type", "")).strip()
    if new_type and new_type != col.type.render():
        e.retype_col(path, _type(f, dialect))
        done.append(f"type to {new_type}")

    # An unchecked checkbox sends nothing, which is indistinguishable from "the form
    # never offered the field" -- so the form sends a hidden marker alongside it. Without
    # that, any caller omitting `nullable` would silently make the column NOT NULL.
    if _bool(f, "nullable_field") and (want_nullable := _bool(f, "nullable")) != col.nullable:
        e.set_nullable(path, want_nullable)
        done.append("allow nulls" if want_nullable else "forbid nulls")

    new_default = _opt(f, "default")
    if new_default != col.default:
        e.set_default(path, new_default)
        done.append(f"default to {new_default}" if new_default else "drop the default")

    # Renaming last: every edit above addresses the column by its current path, and
    # renaming first would invalidate all of them.
    new_name = str(f.get("name", "")).strip()
    if new_name and new_name != current_name:
        e.rename_col(path, new_name)
        done.insert(0, f"rename to {new_name}")

    if not done:
        raise EditError(f"nothing to change on {path}")
    return f"{table}.{current_name}: " + ", ".join(done)


def drop_column(e, f, dialect):
    path = _req(f, "path")
    e.drop_col(path)
    return f"drop column {path}"


def rename_column(e, f, dialect):
    path, new = _req(f, "path"), _req(f, "new_name")
    e.rename_col(path, new)
    return f"rename column {path} to {new}"


def retype_column(e, f, dialect):
    path, type_ = _req(f, "path"), _type(f, dialect)
    e.retype_col(path, type_)
    return f"retype column {path} to {type_}"


def set_nullable(e, f, dialect):
    path = _req(f, "path")
    nullable = _bool(f, "nullable")
    e.set_nullable(path, nullable)
    return f"{'allow' if nullable else 'forbid'} nulls on {path}"


def set_default(e, f, dialect):
    path = _req(f, "path")
    default = _opt(f, "default")
    e.set_default(path, default)
    return (f"set default on {path} to {default}" if default
            else f"drop default on {path}")


# ----------------------------------------------------------------- indexes
def add_index(e, f, dialect):
    table, name = _req(f, "table"), _req(f, "name")
    cols = _cols(f)
    e.add_index(table, name, cols, unique=_bool(f, "unique"), where=_opt(f, "where"))
    kind = "unique index" if _bool(f, "unique") else "index"
    return f"create {kind} {name} on {table} ({', '.join(cols)})"


def drop_index(e, f, dialect):
    path = _req(f, "path")
    e.drop_index(path)
    return f"drop index {path}"


# ------------------------------------------------------------- constraints
def add_unique(e, f, dialect):
    table, name = _req(f, "table"), _req(f, "name")
    cols = _cols(f)
    e.add_unique(table, name, cols)
    return f"add unique {name} on {table} ({', '.join(cols)})"


def add_check(e, f, dialect):
    table, name, expr = _req(f, "table"), _req(f, "name"), _req(f, "expression")
    # `columns` is optional but strongly worth supplying: it is what lets the engine
    # notice that dropping a column breaks a CHECK that reads it (M-85).
    cols = [c.strip() for c in str(f.get("columns", "")).split(",") if c.strip()]
    e.add_check(table, name, expr, columns=cols)
    return f"add check {name} on {table}"


def add_fk(e, f, dialect):
    table, name = _req(f, "table"), _req(f, "name")
    cols = _cols(f)
    ref_table, ref_cols = _req(f, "ref_table"), _cols(f, "ref_columns")
    if len(cols) != len(ref_cols):
        raise EditError(
            f"a foreign key pairs columns positionally: {len(cols)} local "
            f"column(s) but {len(ref_cols)} referenced")
    e.add_fk(table, name, cols, ref_table, ref_cols,
             on_delete=_opt(f, "on_delete"), on_update=_opt(f, "on_update"))
    return f"add foreign key {name}: {table}({', '.join(cols)}) -> {ref_table}"


def drop_constraint(e, f, dialect):
    path = _req(f, "path")
    e.drop_constraint(path)
    return f"drop constraint {path}"


#: Every handler takes `(editor, form, dialect)`. Only the two that accept a type name
#: use the dialect, but a uniform signature keeps the dispatcher in app.py free of
#: special cases -- and the alternative, inspecting each handler's parameters, is the
#: kind of cleverness that breaks silently when someone adds a handler.
#: Reachable through the combined `alter_column` form rather than a form of their own.
#: Kept as distinct operations because they are distinct *semantics* -- a rename is not
#: a retype, and the merge engine depends on that -- but a user changing a column thinks
#: of it as one action, so the UI presents one (D49).
SUBSUMED = ("rename_column", "retype_column", "set_nullable", "set_default")

EDITS = {
    "add_table": add_table, "drop_table": drop_table, "rename_table": rename_table,
    "add_column": add_column, "alter_column": alter_column, "drop_column": drop_column,
    "rename_column": rename_column, "retype_column": retype_column,
    "set_nullable": set_nullable, "set_default": set_default,
    "add_index": add_index, "drop_index": drop_index,
    "add_unique": add_unique, "add_check": add_check, "add_fk": add_fk,
    "drop_constraint": drop_constraint,
}
