"""Model objects -> plain dicts for templates.

Deliberately a separate module. Templates that reach into `Constraint.column_ids` and
resolve them themselves would put reference-resolution logic -- the thing D30 says is
subtle -- into Jinja, where it cannot be tested. Everything id-shaped is resolved to a
name here, once, and templates only ever render strings.
"""
from __future__ import annotations

from ..engine.diff import ChangeKind
from ..engine.plan import Safety
from ..model.objects import ConstraintKind

#: Human wording for each change kind. The point of an identity-based diff is that it can
#: say "renamed", so the vocabulary is worth getting right rather than dumping enum names.
CHANGE_WORDS = {
    ChangeKind.CREATE_TABLE: ("created table", "add"),
    ChangeKind.DROP_TABLE: ("dropped table", "drop"),
    ChangeKind.RENAME_TABLE: ("renamed table", "rename"),
    ChangeKind.ADD_COLUMN: ("added column", "add"),
    ChangeKind.DROP_COLUMN: ("dropped column", "drop"),
    ChangeKind.RENAME_COLUMN: ("renamed column", "rename"),
    ChangeKind.ALTER_COLUMN: ("altered column", "alter"),
    ChangeKind.CREATE_INDEX: ("created index", "add"),
    ChangeKind.DROP_INDEX: ("dropped index", "drop"),
    ChangeKind.RENAME_INDEX: ("renamed index", "rename"),
    ChangeKind.ALTER_INDEX: ("altered index", "alter"),
    ChangeKind.ADD_CONSTRAINT: ("added constraint", "add"),
    ChangeKind.DROP_CONSTRAINT: ("dropped constraint", "drop"),
    ChangeKind.ALTER_CONSTRAINT: ("altered constraint", "alter"),
}

SAFETY_WORDS = {
    Safety.SAFE: ("safe", "runs without locking anything for long"),
    Safety.LOCK_HEAVY: ("lock heavy", "succeeds, but may hold a lock on a large table"),
    Safety.LOSSY: ("lossy", "data can be silently truncated or dropped"),
    Safety.UNSAFE: ("unsafe", "may fail outright on existing data"),
}


def render_type(t) -> str:
    return t.render() if hasattr(t, "render") else str(t)


def table_view(table, snapshot) -> dict:
    """One table, with every id already resolved to a name."""
    by_id = {c.id: c.name for c in table.columns}
    tables_by_id = {t.id: t.name for t in snapshot.tables}

    badges: dict[str, list[str]] = {c.name: [] for c in table.columns}
    constraints = []
    for c in table.constraints:
        cols = [by_id.get(i, "?") for i in c.column_ids]
        detail = ", ".join(cols)
        if c.kind is ConstraintKind.PRIMARY_KEY:
            for name in cols:
                badges.setdefault(name, []).append("PK")
        elif c.kind is ConstraintKind.UNIQUE:
            for name in cols:
                badges.setdefault(name, []).append("UQ")
        elif c.kind is ConstraintKind.FOREIGN_KEY:
            ref_t = tables_by_id.get(c.ref_table_id, "?")
            ref_table = snapshot.table_by_id(c.ref_table_id) if c.ref_table_id else None
            ref_cols = [
                (ref_table.column_by_id(i).name if ref_table and ref_table.column_by_id(i)
                 else "?")
                for i in c.ref_column_ids
            ]
            for name in cols:
                badges.setdefault(name, []).append("FK")
            detail = f"({', '.join(cols)}) -> {ref_t}({', '.join(ref_cols)})"
        elif c.kind is ConstraintKind.CHECK:
            detail = c.expression or ""
        constraints.append({"name": c.name, "kind": c.kind.value.replace("_", " "),
                            "detail": detail})

    indexes = [{
        "name": i.name,
        "unique": i.unique,
        "columns": ", ".join(
            by_id.get(ic.column_id, "?") + (" DESC" if ic.desc else "")
            + (f"({ic.prefix_length})" if ic.prefix_length else "")
            for ic in i.columns),
        "where": i.where,
    } for i in table.indexes]

    return {
        "name": table.name,
        "id": table.id,
        "columns": [{
            "name": c.name,
            "id": c.id,
            "type": render_type(c.type),
            # Split for the editor: the base is chosen from a list, the size is typed.
            "base_type": c.type.base,
            "type_params": ",".join(str(p) for p in c.type.params),
            "nullable": c.nullable,
            "default": c.default,
            "badges": badges.get(c.name, []),
        } for c in table.columns],
        "constraints": constraints,
        "indexes": indexes,
    }


def schema_view(snapshot) -> list[dict]:
    return [table_view(t, snapshot) for t in snapshot.tables]


def change_view(change) -> dict:
    word, tone = CHANGE_WORDS.get(change.kind, (change.kind.value, "alter"))
    name = change.name or change.table
    if change.before_name and change.before_name != change.name:
        subject = f"{change.before_name} -> {change.name}"
    else:
        subject = name
    scope = change.table if change.name else ""
    return {
        "word": word,
        "tone": tone,
        "scope": scope,
        "subject": subject,
        "deltas": [{"attribute": d.attribute,
                    "before": _show(d.before),
                    "after": _show(d.after)} for d in change.deltas],
    }


def operation_view(op) -> dict:
    word, why = SAFETY_WORDS[op.safety]
    return {
        "describe": op.describe(),
        "safety": op.safety.value,
        "safety_word": word,
        "safety_why": why,
        "requires_cast": op.requires_cast,
        "temp": op.temp,
    }


def conflict_view(c) -> dict:
    return {
        "key": c.key,
        "category": c.category.value.replace("_", "/"),
        "kind": c.object_kind,
        "path": c.path,
        "attribute": c.attribute,
        "message": c.message,
        "base": _show(c.base),
        "ours": _show(c.ours),
        "theirs": _show(c.theirs),
        "choosable": c.attribute is not None,
    }


def _show(v) -> str:
    if v is None:
        return "—"
    if v is True:
        return "yes"
    if v is False:
        return "no"
    if hasattr(v, "render"):
        return v.render()
    if isinstance(v, (tuple, list)):
        return ", ".join(_show(x) for x in v) or "—"
    return str(v)
