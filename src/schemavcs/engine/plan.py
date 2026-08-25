"""Migration planning: a diff becomes an ordered, executable sequence of operations.

This module is where the genuinely hard part of the project lives, and it is
deliberately **dialect-neutral**. It decides *what* has to happen and *in what order*;
turning an operation into SQL text is the renderers' job (D35). The payoff is that
every interesting test here asserts on an operation sequence rather than on strings --
`E-25` (circular foreign keys) and `E-40` (rename swap) are questions about ordering,
and string matching would only obscure them.

Three problems are solved here that a naive emitter gets wrong.

**Ordering** (D17). Drop constraints before the columns they cover; create tables
before the keys referencing them. Handled by phasing rather than by a general
topological sort -- see `_PHASE`.

**Foreign-key cycles** (E-25). `users.org_id -> orgs.id` and `orgs.owner_id ->
users.id` have no valid linear order if each table is created complete. Separating
table creation from foreign-key addition dissolves the problem entirely: create every
table bare, then add every foreign key. No cycle detection, no phase assignment -- the
cycle simply cannot arise. This is the rare case where the right structure makes the
hard algorithm unnecessary.

**Intermediate-state collisions** (D16, E-40). Renaming `x` to `y` while renaming `y`
to `x` has a valid start state and a valid end state, and no valid order in between.
That one needs real cycle detection and a temporary name.

A note on names: DDL addresses objects by name, but names *change during the
migration*. An operation's name is therefore whatever is correct at its own position
in the sequence, which is why `_stamp_names` replays the plan to assign them rather
than reading them from either snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from ..model.objects import Constraint, ConstraintKind
from .diff import ChangeKind, diff

TEMP_PREFIX = "__schemavcs_tmp"


class OpKind(str, Enum):
    DROP_CONSTRAINT = "drop_constraint"
    DROP_INDEX = "drop_index"
    DROP_COLUMN = "drop_column"
    DROP_TABLE = "drop_table"
    CREATE_TABLE = "create_table"
    RENAME_TABLE = "rename_table"
    RENAME_COLUMN = "rename_column"
    ADD_COLUMN = "add_column"
    ALTER_COLUMN_TYPE = "alter_column_type"
    SET_NOT_NULL = "set_not_null"
    DROP_NOT_NULL = "drop_not_null"
    SET_DEFAULT = "set_default"
    DROP_DEFAULT = "drop_default"
    RENAME_INDEX = "rename_index"
    CREATE_INDEX = "create_index"
    ADD_CONSTRAINT = "add_constraint"


#: Execution phase per operation kind. Ordering by phase, not by a general dependency
#: sort, because the dependency structure of DDL is fixed and known: there is no schema
#: change whose correct ordering these ten buckets get wrong. A topological sort would
#: be more general, more code, and would still need these rules encoded as edges.
_PHASE = {
    OpKind.DROP_CONSTRAINT: 0,     # before the columns/tables they cover (E-21/E-24)
    OpKind.DROP_INDEX: 1,          # before the columns they cover (E-23)
    OpKind.DROP_COLUMN: 2,
    OpKind.DROP_TABLE: 3,
    OpKind.CREATE_TABLE: 4,        # bare: no foreign keys yet (E-25)
    OpKind.RENAME_TABLE: 5,
    OpKind.RENAME_COLUMN: 6,       # before ADD_COLUMN, so `a->b` plus a new `b`
    OpKind.ADD_COLUMN: 7,          # never collides (E-44)
    OpKind.ALTER_COLUMN_TYPE: 8,
    OpKind.SET_NOT_NULL: 8,
    OpKind.DROP_NOT_NULL: 8,
    OpKind.SET_DEFAULT: 8,
    OpKind.DROP_DEFAULT: 8,
    OpKind.RENAME_INDEX: 9,
    OpKind.CREATE_INDEX: 10,       # after the columns exist (E-22)
    OpKind.ADD_CONSTRAINT: 11,     # last, which is what breaks FK cycles
}


class Safety(str, Enum):
    """How much a caller should worry. Ordered by severity.

    Deliberately coarse. Predicting whether a given engine version rewrites a table
    for a given ALTER is a research project with version-specific answers (D19); four
    honest buckets beat a precise-looking number that is wrong on half of deployments.
    """
    SAFE = "safe"
    LOCK_HEAVY = "lock_heavy"      # succeeds, but may hold a lock on a large table
    LOSSY = "lossy"                # destroys data
    UNSAFE = "unsafe"              # may fail outright on a non-empty table


_SEVERITY = {Safety.SAFE: 0, Safety.LOCK_HEAVY: 1, Safety.LOSSY: 2, Safety.UNSAFE: 3}

#: Integer widths, for widen-vs-narrow classification.
_INT_RANK = {"smallint": 1, "int": 2, "bigint": 3}
_TEXTUAL = {"varchar", "char", "text"}
_NUMERIC = set(_INT_RANK) | {"decimal", "double", "real"}


@dataclass(frozen=True)
class Operation:
    """One DDL step. Names are execution-time names, resolved by `_stamp_names`."""

    kind: OpKind
    table: str
    name: str | None = None
    new_name: str | None = None
    safety: Safety = Safety.SAFE
    #: True when the engine cannot convert the old value to the new one implicitly.
    requires_cast: bool = False
    #: Set when this step exists only to route around a name collision (D16).
    temp: bool = False

    # ---- payload, already resolved to names so renderers never touch ids
    column: object = None
    index: object = None
    constraint: object = None
    columns: tuple = ()             # index/constraint column names, ordered
    #: Types of `columns`, positionally. Renderers need these: MySQL cannot index a
    #: TEXT column without a prefix length, and only the renderer knows that rule.
    column_types: tuple = ()
    ref_table: str | None = None
    ref_columns: tuple[str, ...] = ()
    #: For CREATE_TABLE: inline constraints (PK/UNIQUE/CHECK) with resolved names.
    inline: tuple = ()

    # ---- internal: identity, used for name replay
    table_id: str = ""
    object_id: str = ""

    @property
    def is_destructive(self) -> bool:
        return self.safety in (Safety.LOSSY, Safety.UNSAFE)

    def describe(self) -> str:
        if self.kind is OpKind.RENAME_TABLE:
            return f"{self.kind.value} {self.table} -> {self.new_name}"
        target = f"{self.table}.{self.name}" if self.name else self.table
        arrow = f" -> {self.new_name}" if self.new_name else ""
        return f"{self.kind.value} {target}{arrow}"


@dataclass(frozen=True)
class Plan:
    operations: tuple[Operation, ...] = ()

    def __len__(self) -> int:
        return len(self.operations)

    def __iter__(self):
        return iter(self.operations)

    @property
    def is_empty(self) -> bool:
        return not self.operations

    def kinds(self) -> list[OpKind]:
        return [o.kind for o in self.operations]

    def of_kind(self, *kinds: OpKind) -> tuple[Operation, ...]:
        return tuple(o for o in self.operations if o.kind in kinds)

    @property
    def worst_safety(self) -> Safety:
        return max((o.safety for o in self.operations),
                   key=lambda s: _SEVERITY[s], default=Safety.SAFE)

    @property
    def destructive(self) -> tuple[Operation, ...]:
        return tuple(o for o in self.operations if o.is_destructive)

    def index_of(self, kind: OpKind, name: str | None = None) -> int:
        for i, o in enumerate(self.operations):
            if o.kind is kind and (name is None or name in (o.name, o.new_name)):
                return i
        raise ValueError(f"no {kind.value} operation for {name!r}")


class UnacknowledgedRiskError(Exception):
    """A plan destroys data and nobody said that was acceptable (D19, E-71).

    Refusing by default is the whole point. A tool that silently emits
    `DROP COLUMN` because it was the mathematically correct diff is not one anyone
    should point at a production database.
    """

    def __init__(self, operations):
        self.operations = tuple(operations)
        lines = "\n  ".join(o.describe() for o in self.operations)
        super().__init__(
            f"this migration destroys data in {len(self.operations)} operation(s):\n  "
            f"{lines}\nRe-run with acknowledge_lossy=True to proceed."
        )


# ==================================================================== entry point
def plan(base, target, *, acknowledge_lossy: bool = False) -> Plan:
    """Ordered operations that take a database from `base` to `target`."""
    ops = _operations(base, target)
    ops = _order(ops)
    ops = _route_rename_cycles(ops, base, target)
    ops = _stamp_names(ops, base)

    p = Plan(tuple(ops))
    if p.destructive and not acknowledge_lossy:
        raise UnacknowledgedRiskError(p.destructive)
    return p


# ================================================================== change -> ops
def _operations(base, target) -> list[Operation]:
    d = diff(base, target)
    out: list[Operation] = []
    for c in d.changes:
        out.extend(_expand(c, base, target))
    return out


def _expand(c, base, target) -> list[Operation]:
    k = ChangeKind
    if c.kind is k.CREATE_TABLE:
        return _create_table(c.after, target)
    if c.kind is k.DROP_TABLE:
        return [Operation(OpKind.DROP_TABLE, c.table, safety=Safety.LOSSY,
                          table_id=c.object_id, object_id=c.object_id)]
    if c.kind is k.RENAME_TABLE:
        return [Operation(OpKind.RENAME_TABLE, c.before_name, name=c.before_name,
                          new_name=c.name,
                          table_id=c.object_id, object_id=c.object_id)]
    if c.kind is k.ADD_COLUMN:
        return [_add_column(c, target)]
    if c.kind is k.DROP_COLUMN:
        return [Operation(OpKind.DROP_COLUMN, c.table, name=c.name,
                          safety=Safety.LOSSY, column=c.before,
                          table_id=_table_id(target, base, c), object_id=c.object_id)]
    if c.kind in (k.RENAME_COLUMN, k.ALTER_COLUMN):
        return _alter_column(c, base, target)
    if c.kind is k.CREATE_INDEX:
        return [_create_index(c, target)]
    if c.kind is k.DROP_INDEX:
        return [Operation(OpKind.DROP_INDEX, c.table, name=c.name, index=c.before,
                          table_id=_table_id(target, base, c), object_id=c.object_id)]
    if c.kind is k.RENAME_INDEX:
        # Only the name moved? A rename. Anything else moved too, and an index has to
        # be dropped and recreated -- there is no ALTER INDEX for column lists.
        if len(c.deltas) == 1:
            return [Operation(OpKind.RENAME_INDEX, c.table, name=c.before_name,
                              new_name=c.name, index=c.after,
                              table_id=_table_id(target, base, c),
                              object_id=c.object_id)]
        return _recreate_index(c, base, target)
    if c.kind is k.ALTER_INDEX:
        return _recreate_index(c, base, target)
    if c.kind is k.ADD_CONSTRAINT:
        return [_add_constraint(c.after, c.table, target, c.object_id)]
    if c.kind is k.DROP_CONSTRAINT:
        return [Operation(OpKind.DROP_CONSTRAINT, c.table, name=c.name,
                          constraint=c.before,
                          table_id=_table_id(target, base, c), object_id=c.object_id)]
    if c.kind is k.ALTER_CONSTRAINT:
        # Constraints are immutable in every engine we target: drop and re-add.
        tid = _table_id(target, base, c)
        return [
            Operation(OpKind.DROP_CONSTRAINT, c.table, name=c.before.name,
                      constraint=c.before, table_id=tid, object_id=c.object_id),
            _add_constraint(c.after, c.table, target, c.object_id),
        ]
    raise AssertionError(f"unhandled change kind: {c.kind}")   # pragma: no cover


def _create_table(table, target) -> list[Operation]:
    """A new table, then its indexes, then its foreign keys -- separately.

    Foreign keys are never inlined. That single choice is what makes circular
    references (E-25), self-references (E-26) and n-way cycles (E-27) all work
    without any cycle detection: by the time any key is added, every table exists.
    """
    names, types = target.name_map(), _type_map(target)
    inline = tuple(_resolved_constraint(k, target)
                   for k in table.constraints
                   if k.kind is not ConstraintKind.FOREIGN_KEY)
    ops = [Operation(OpKind.CREATE_TABLE, table.name, column=None,
                     inline=inline, table_id=table.id, object_id=table.id,
                     columns=tuple(c.name for c in table.columns))]
    ops[0] = replace(ops[0], index=None, constraint=None, column=table.columns)
    for idx in table.indexes:
        ops.append(Operation(OpKind.CREATE_INDEX, table.name, name=idx.name,
                             index=idx, safety=Safety.LOCK_HEAVY,
                             columns=tuple(names.get(ic.column_id, "?")
                                           for ic in idx.columns),
                             column_types=tuple(types.get(ic.column_id)
                                                for ic in idx.columns),
                             table_id=table.id, object_id=idx.id))
    for k in table.constraints:
        if k.kind is ConstraintKind.FOREIGN_KEY:
            ops.append(_add_constraint(k, table.name, target, k.id))
    return ops


def _add_column(c, target) -> Operation:
    col = c.after
    if col.nullable or col.default is not None or col.autoincrement:
        # Adding a nullable column is metadata-only on both engines; adding one with
        # a default may rewrite the table on older versions, so it is lock-heavy
        # rather than safe (E-69).
        safety = Safety.SAFE if col.nullable and col.default is None \
            else Safety.LOCK_HEAVY
    else:
        # NOT NULL with no default fails on any table that already has rows (E-68).
        safety = Safety.UNSAFE
    return Operation(OpKind.ADD_COLUMN, c.table, name=col.name, column=col,
                     safety=safety, table_id=_table_of(target, c.object_id),
                     object_id=col.id)


def _alter_column(c, base, target) -> list[Operation]:
    """One change object can carry several independent attribute moves, and each is a
    separate DDL statement. The rename goes first so later statements address the new
    name -- which is also why `_stamp_names` replays rather than looks up."""
    tid = _table_of(target, c.object_id)
    ops = []
    by_attr = {d.attribute: d for d in c.deltas}

    if "name" in by_attr:
        ops.append(Operation(OpKind.RENAME_COLUMN, c.table,
                             name=by_attr["name"].before,
                             new_name=by_attr["name"].after,
                             table_id=tid, object_id=c.object_id))
    if "type" in by_attr:
        d = by_attr["type"]
        safety, cast = _classify_retype(d.before, d.after)
        ops.append(Operation(OpKind.ALTER_COLUMN_TYPE, c.table, name=c.name,
                             column=c.after, safety=safety, requires_cast=cast,
                             table_id=tid, object_id=c.object_id))
    if "nullable" in by_attr:
        to_null = by_attr["nullable"].after
        ops.append(Operation(
            OpKind.DROP_NOT_NULL if to_null else OpKind.SET_NOT_NULL,
            c.table, name=c.name, column=c.after,
            # Tightening can fail if existing rows hold NULL; loosening never can.
            safety=Safety.SAFE if to_null else Safety.UNSAFE,
            table_id=tid, object_id=c.object_id))
    if "default" in by_attr:
        after = by_attr["default"].after
        ops.append(Operation(
            OpKind.SET_DEFAULT if after is not None else OpKind.DROP_DEFAULT,
            c.table, name=c.name, column=c.after,
            table_id=tid, object_id=c.object_id))
    return ops


def _classify_retype(before, after) -> tuple[Safety, bool]:
    """Widening is safe, narrowing loses data, and anything unfamiliar is treated as
    lossy. Guessing 'probably fine' on an unrecognized pair is how a tool silently
    truncates a production column."""
    b, a = before.base, after.base
    if b == a:
        if before.params and after.params:
            widened = all(x <= y for x, y in zip(before.params, after.params))
            return (Safety.SAFE, False) if widened else (Safety.LOSSY, False)
        if before.params and not after.params:
            return Safety.SAFE, False          # varchar(50) -> text
        if not before.params and after.params:
            return Safety.LOSSY, False         # text -> varchar(50)
        return Safety.SAFE, False
    if b in _TEXTUAL and a in _TEXTUAL:
        # varchar(50) -> text always fits; text -> varchar(50) may truncate. Neither
        # needs a cast: every engine converts between string types implicitly.
        if not after.params:
            return Safety.SAFE, False
        if not before.params:
            return Safety.LOSSY, False
        widened = after.params[0] >= before.params[0]
        return (Safety.SAFE, False) if widened else (Safety.LOSSY, False)
    if b in _INT_RANK and a in _INT_RANK:
        return (Safety.SAFE, False) if _INT_RANK[a] >= _INT_RANK[b] \
            else (Safety.LOSSY, False)
    if b in _NUMERIC and a in _TEXTUAL:
        return Safety.SAFE, True               # every number has a text form (E-64)
    if b in _TEXTUAL and a in _NUMERIC:
        return Safety.LOSSY, True              # 'abc' has no number form (E-65)
    return Safety.LOSSY, True                  # unknown pair: assume the worst


def _create_index(c, target) -> Operation:
    names, types = target.name_map(), _type_map(target)
    return Operation(OpKind.CREATE_INDEX, c.table, name=c.name, index=c.after,
                     safety=Safety.LOCK_HEAVY,
                     columns=tuple(names.get(ic.column_id, "?")
                                   for ic in c.after.columns),
                     column_types=tuple(types.get(ic.column_id)
                                        for ic in c.after.columns),
                     table_id=_table_of(target, c.object_id), object_id=c.object_id)


def _recreate_index(c, base, target) -> list[Operation]:
    tid = _table_id(target, base, c)
    return [
        Operation(OpKind.DROP_INDEX, c.table, name=c.before.name, index=c.before,
                  table_id=tid, object_id=c.object_id),
        replace(_create_index(c, target), table_id=tid),
    ]


def _add_constraint(k, table_name, target, object_id) -> Operation:
    r = _resolved_constraint(k, target)
    return Operation(OpKind.ADD_CONSTRAINT, table_name, name=k.name, constraint=k,
                     columns=r.columns, ref_table=r.ref_table,
                     ref_columns=r.ref_columns, column_types=r.column_types,
                     safety=Safety.LOCK_HEAVY if k.kind is ConstraintKind.FOREIGN_KEY
                     else Safety.SAFE,
                     table_id=_table_of(target, object_id), object_id=k.id)


@dataclass(frozen=True)
class _ResolvedConstraint:
    constraint: Constraint
    columns: tuple[str, ...]
    ref_table: str | None
    ref_columns: tuple[str, ...]
    column_types: tuple = ()


def _resolved_constraint(k, target) -> _ResolvedConstraint:
    names = target.name_map()
    types = _type_map(target)
    return _ResolvedConstraint(
        constraint=k,
        columns=tuple(names.get(cid, "?") for cid in k.column_ids),
        ref_table=names.get(k.ref_table_id) if k.ref_table_id else None,
        ref_columns=tuple(names.get(cid, "?") for cid in k.ref_column_ids),
        column_types=tuple(types.get(cid) for cid in k.column_ids))


def _type_map(snap) -> dict:
    return {c.id: c.type for t in snap.tables for c in t.columns}


def _table_of(snap, object_id: str) -> str:
    """Which table owns this column/index/constraint id."""
    for t in snap.tables:
        ids = ({c.id for c in t.columns} | {i.id for i in t.indexes}
               | {k.id for k in t.constraints})
        if object_id in ids or t.id == object_id:
            return t.id
    return ""


def _table_id(primary, fallback, c) -> str:
    return _table_of(primary, c.object_id) or _table_of(fallback, c.object_id)


# ======================================================================= ordering
def _order(ops: list[Operation]) -> list[Operation]:
    """Stable sort by phase. Stability matters: two plans for the same diff must be
    byte-identical, or a reviewer cannot tell a real change from reordering."""
    return [o for _, o in sorted(enumerate(ops),
                                 key=lambda p: (_PHASE[p[1].kind], p[0]))]


# ============================================================ temp-name detours
def _route_rename_cycles(ops, base, target) -> list[Operation]:
    """Break rename cycles with a temporary name (D16, E-40).

    A rename swap has a valid start and a valid end and no valid order between them.
    The fix is to move one participant out of the way first. Cycles are detected per
    scope -- per table for columns and indexes, globally for tables -- because a
    collision is only a collision within a namespace.
    """
    reserved = _all_names(base) | _all_names(target)
    counter = [0]

    def temp_name() -> str:
        while True:
            counter[0] += 1
            candidate = f"{TEMP_PREFIX}_{counter[0]}"
            if candidate not in reserved:        # E-45: never shadow a real name
                reserved.add(candidate)
                return candidate

    out = list(ops)
    for kind in (OpKind.RENAME_TABLE, OpKind.RENAME_COLUMN, OpKind.RENAME_INDEX):
        scopes: dict[str, list[Operation]] = {}
        for o in out:
            if o.kind is kind:
                scopes.setdefault("" if kind is OpKind.RENAME_TABLE else o.table_id,
                                  []).append(o)
        for group in scopes.values():
            for cycle in _find_cycles(group):
                out = _break_cycle(out, cycle, temp_name())
    return out


def _find_cycles(renames: list[Operation]) -> list[list[Operation]]:
    """Cycles in the name graph `old -> new`.

    A chain (`a->b` where nothing renames to `a`) needs no help: ordering alone
    suffices. Only a closed cycle has no valid order, so only cycles get a temp name
    -- which is what E-46 pins down.
    """
    by_source = {o.name: o for o in renames}
    cycles, seen = [], set()
    for start in by_source:
        if start in seen:
            continue
        path, node = [], start
        while node in by_source and node not in path:
            path.append(node)
            node = by_source[node].new_name
        if node in path:
            cycle = path[path.index(node):]
            if len(cycle) > 1:
                cycles.append([by_source[n] for n in cycle])
            seen.update(cycle)
        else:
            seen.update(path)
    return cycles


def _break_cycle(ops, cycle, temp: str) -> list[Operation]:
    """Rewrite a rename cycle into an executable sequence via one temporary name.

    For a cycle `n1->n2, n2->n3, ..., nk->n1` the only orders that work move one
    participant out of the way and then close the loop *backwards*:

        n1 -> temp        (frees n1)
        nk -> n1          (n1 is now free)
        ...
        n2 -> n3
        temp -> n2        (n2 is now free)

    One extra statement, regardless of cycle length. Both detour statements are
    marked `temp` so the renderer -- and any human reading the migration -- can see
    which lines are machinery rather than intent.
    """
    first, rest = cycle[0], cycle[1:]
    replacement = ([replace(first, new_name=temp, temp=True)]
                   + list(reversed(rest))
                   + [replace(first, name=temp, new_name=first.new_name, temp=True)])

    cycle_ids = {id(o) for o in cycle}
    insert_at = next(i for i, o in enumerate(ops) if id(o) in cycle_ids)
    remaining = [o for o in ops if id(o) not in cycle_ids]
    return remaining[:insert_at] + replacement + remaining[insert_at:]


def _all_names(snap) -> set[str]:
    out = set()
    for t in snap.tables:
        out.add(t.name)
        out.update(c.name for c in t.columns)
        out.update(i.name for i in t.indexes)
        out.update(k.name for k in t.constraints)
    return out


# ================================================================== name stamping
def _stamp_names(ops, base) -> list[Operation]:
    """Replay the plan, giving every operation the name valid at its own position.

    Reading names from either snapshot would be wrong: after `RENAME TABLE users TO
    accounts`, a later statement must say `accounts`, and before it, `users`. Only a
    replay knows which side of the rename a given statement falls on.
    """
    live = {t.id: t.name for t in base.tables}
    out = []
    for o in ops:
        table = live.get(o.table_id, o.table)
        o = replace(o, table=table)
        if o.kind is OpKind.RENAME_TABLE:
            o = replace(o, table=live.get(o.table_id, o.name))
            live[o.table_id] = o.new_name
        elif o.kind is OpKind.CREATE_TABLE:
            live[o.table_id] = o.table
        out.append(o)
    return out
