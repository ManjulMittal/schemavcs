"""Canonical type model.

Dialect-neutral by construction (D5): nothing here knows what Postgres or MySQL is.
Adapters map into and out of these values.

The canonical form deliberately carries more than any single engine supports (D6) --
`unsigned` has no Postgres equivalent, and the intersection of both engines would
exclude most of a real production schema.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Tag for a snapshot not bound to any engine -- hand-built fixtures, and the
#: merge engine's intermediate results. Diff and merge never read this field;
#: emitters reject it, because emitting DDL requires knowing the target engine.
DIALECT_GENERIC = "generic"

# Spellings that mean the same canonical type. Resolves P-21/P-22 (varchar vs
# `character varying`, int vs integer vs int4) so they never surface as a diff.
_ALIASES = {
    "integer": "int", "int4": "int", "int8": "bigint", "int2": "smallint",
    "character varying": "varchar", "character": "char",
    "bool": "boolean",
    "double precision": "double", "float8": "double", "float4": "real",
    "numeric": "decimal",
    "timestamp with time zone": "timestamptz",
    "timestamp without time zone": "timestamp",
}

_PARAM_RE = re.compile(r"^\s*([a-z_ ]+?)\s*(?:\(\s*([\d,\s]+)\s*\))?\s*$", re.I)


@dataclass(frozen=True, order=True)
class ColumnType:
    """A canonical column type: base name, numeric params, and unsignedness.

    `params` is a tuple so `varchar(255)` and `decimal(10,2)` are both expressible
    and both hashable.
    """

    base: str
    params: tuple[int, ...] = ()
    unsigned: bool = False

    @classmethod
    def parse(cls, text: str, *, unsigned: bool = False) -> ColumnType:
        raw = text.strip().lower()
        if raw.endswith(" unsigned"):
            raw, unsigned = raw[: -len(" unsigned")].strip(), True

        m = _PARAM_RE.match(raw)
        if not m:
            raise ValueError(f"unparseable type: {text!r}")

        base = " ".join(m.group(1).split())
        base = _ALIASES.get(base, base)
        params = tuple(int(p) for p in m.group(2).replace(" ", "").split(",")) if m.group(2) else ()
        return cls(base=base, params=params, unsigned=unsigned)

    def render(self) -> str:
        """Canonical text form. Not dialect SQL -- emitters own that."""
        s = self.base
        if self.params:
            s += "(" + ",".join(str(p) for p in self.params) + ")"
        if self.unsigned:
            s += " unsigned"
        return s

    def to_dict(self) -> dict:
        return {"base": self.base, "params": list(self.params), "unsigned": self.unsigned}

    @classmethod
    def from_dict(cls, d: dict) -> ColumnType:
        return cls(base=d["base"], params=tuple(d["params"]), unsigned=d["unsigned"])

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.render()
