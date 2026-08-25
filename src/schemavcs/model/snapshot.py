"""A Snapshot is the complete schema state at one commit (D2).

Commits store snapshots rather than operation logs: reading any commit costs one
lookup, and identity (D1) already makes renames explicit without needing a log to
replay.

Because references between objects are ids rather than names (D30), a snapshot owns
two extra responsibilities: resolving ids back to names (for display, comparison, and
emission) and reporting references that resolve to nothing.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace

from .objects import (UNRESOLVED, Column, Constraint, ConstraintKind, Index,
                      IndexColumn, Table, new_id)
from .types import DIALECT_GENERIC, ColumnType


@dataclass(frozen=True)
class Snapshot:
    dialect: str = DIALECT_GENERIC
    tables: tuple[Table, ...] = ()

    # ------------------------------------------------------------ lookup
    def table(self, name: str) -> Table | None:
        return next((t for t in self.tables if t.name == name), None)

    def table_by_id(self, table_id: str) -> Table | None:
        return next((t for t in self.tables if t.id == table_id), None)

    def col(self, path: str) -> Column | None:
        """`col("users.email")` -- the addressable identity the fixture DSL needs."""
        table_name, _, col_name = path.rpartition(".")
        t = self.table(table_name)
        return t.column(col_name) if t else None

    def index(self, path: str) -> Index | None:
        table_name, _, idx = path.rpartition(".")
        t = self.table(table_name)
        return t.index(idx) if t else None

    def constraint(self, path: str) -> Constraint | None:
        table_name, _, name = path.rpartition(".")
        t = self.table(table_name)
        return t.constraint(name) if t else None

    # ----------------------------------------------------- name resolution
    def name_map(self) -> dict[str, str]:
        """id -> name for every table and column. The basis of all resolution."""
        out: dict[str, str] = {}
        for t in self.tables:
            out[t.id] = t.name
            for c in t.columns:
                out[c.id] = c.name
        return out

    def name_of(self, object_id: str | None) -> str | None:
        if object_id is None:
            return None
        return self.name_map().get(object_id, UNRESOLVED)

    def index_column_names(self, path: str) -> list[str]:
        """Resolved column names for an index, in order."""
        idx = self.index(path)
        if idx is None:
            return []
        names = self.name_map()
        return [names.get(c.column_id, UNRESOLVED) for c in idx.columns]

    def constraint_column_names(self, path: str) -> list[str]:
        c = self.constraint(path)
        if c is None:
            return []
        names = self.name_map()
        return [names.get(cid, UNRESOLVED) for cid in c.column_ids]

    def constraint_ref(self, path: str) -> tuple[str | None, list[str]]:
        """(referenced table name, referenced column names) for a foreign key."""
        c = self.constraint(path)
        if c is None or c.ref_table_id is None:
            return None, []
        names = self.name_map()
        return (names.get(c.ref_table_id, UNRESOLVED),
                [names.get(cid, UNRESOLVED) for cid in c.ref_column_ids])

    # -------------------------------------------------------- validation
    def dangling_references(self) -> list[str]:
        """Human-readable description of every reference that resolves to nothing.

        A dropped column leaves any index over it pointing at an id that no longer
        exists. That is a genuinely invalid schema, and now it is *detectable* --
        which it was not while references were names, because a stale name and a
        never-existed name look identical.
        """
        known = self.name_map()
        problems: list[str] = []
        for t in self.tables:
            own = {c.id for c in t.columns}
            for i in t.indexes:
                for ic in i.columns:
                    if ic.column_id not in own:
                        problems.append(
                            f"index {t.name}.{i.name} references a column that no "
                            f"longer exists on {t.name}")
            for c in t.constraints:
                for cid in c.column_ids:
                    if cid not in own:
                        problems.append(
                            f"constraint {t.name}.{c.name} references a column that no "
                            f"longer exists on {t.name}")
                if c.kind is ConstraintKind.FOREIGN_KEY:
                    target = self.table_by_id(c.ref_table_id) if c.ref_table_id else None
                    if target is None:
                        problems.append(
                            f"foreign key {t.name}.{c.name} references a table that no "
                            f"longer exists")
                    else:
                        tcols = {x.id for x in target.columns}
                        for cid in c.ref_column_ids:
                            if cid not in tcols:
                                problems.append(
                                    f"foreign key {t.name}.{c.name} references a column "
                                    f"that no longer exists on {target.name}")
        return problems

    # -------------------------------------------------- structural identity
    def fingerprint(self) -> tuple:
        names = self.name_map()
        return (self.dialect, tuple(sorted(t.fingerprint(names) for t in self.tables)))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Snapshot) and self.fingerprint() == other.fingerprint()

    def __hash__(self) -> int:
        return hash(self.fingerprint())

    def content_hash(self) -> str:
        """Stable across serialization; changes on any attribute change (N-08/N-09)."""
        return hashlib.sha256(
            json.dumps(self.fingerprint(), default=_json_default, sort_keys=True).encode()
        ).hexdigest()

    # ------------------------------------------------------------ evolve
    def evolve(self):
        from .evolve import SnapshotEditor
        return SnapshotEditor(self)

    # ----------------------------------------------------- serialization
    def to_dict(self) -> dict:
        return {
            "dialect": self.dialect,
            "tables": [
                {
                    "id": t.id, "name": t.name,
                    "columns": [
                        {"id": c.id, "name": c.name, "type": c.type.to_dict(),
                         "nullable": c.nullable, "default": c.default,
                         "autoincrement": c.autoincrement}
                        for c in t.columns
                    ],
                    "constraints": [
                        {"id": k.id, "name": k.name, "kind": k.kind.value,
                         "column_ids": list(k.column_ids),
                         "ref_table_id": k.ref_table_id,
                         "ref_column_ids": list(k.ref_column_ids),
                         "on_delete": k.on_delete, "on_update": k.on_update,
                         "expression": k.expression}
                        for k in t.constraints
                    ],
                    "indexes": [
                        {"id": i.id, "name": i.name, "unique": i.unique, "where": i.where,
                         "columns": [{"column_id": ic.column_id, "desc": ic.desc,
                                      "prefix_length": ic.prefix_length}
                                     for ic in i.columns]}
                        for i in t.indexes
                    ],
                }
                for t in self.tables
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Snapshot:
        return cls(
            dialect=d["dialect"],
            tables=tuple(
                Table(
                    id=t["id"], name=t["name"],
                    columns=tuple(
                        Column(id=c["id"], name=c["name"],
                               type=ColumnType.from_dict(c["type"]),
                               nullable=c["nullable"], default=c["default"],
                               autoincrement=c["autoincrement"])
                        for c in t["columns"]
                    ),
                    constraints=tuple(
                        Constraint(id=k["id"], name=k["name"],
                                   kind=ConstraintKind(k["kind"]),
                                   column_ids=tuple(k["column_ids"]),
                                   ref_table_id=k["ref_table_id"],
                                   ref_column_ids=tuple(k["ref_column_ids"]),
                                   on_delete=k["on_delete"], on_update=k["on_update"],
                                   expression=k["expression"])
                        for k in t["constraints"]
                    ),
                    indexes=tuple(
                        Index(id=i["id"], name=i["name"], unique=i["unique"],
                              where=i["where"],
                              columns=tuple(IndexColumn(**ic) for ic in i["columns"]))
                        for i in t["indexes"]
                    ),
                )
                for t in d["tables"]
            ),
        )


def _json_default(o):
    if isinstance(o, ColumnType):
        return o.to_dict()
    raise TypeError(f"not serializable: {type(o)}")
