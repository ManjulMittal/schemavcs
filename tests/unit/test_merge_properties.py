"""Algebraic properties of merge (M-90..M-94), via Hypothesis.

The hand-written cases in test_merge.py each encode a scenario I thought of. These
check the laws that must hold for scenarios I did not -- in particular M-94, the
property that catches the ugliest class of merge bug: a change that was present on
exactly one branch and is silently absent from the result. Nobody notices a dropped
change until production disagrees with the schema.

M-94 deliberately verifies through the *diff engine* rather than by re-reading the
merge's own attribute walk. Checking an implementation against a restatement of
itself proves nothing; diff is independent code that reaches the same facts a
different way.
"""
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from schemavcs.engine import MergeStatus, diff, merge
from schemavcs.model import schema
from schemavcs.model.evolve import SchemaError

TABLES = ["t1", "t2"]
COLS = ["a", "b", "c", "d"]
TYPES = ["int", "bigint", "text", "varchar(50)", "boolean"]

# Generation is capped small on purpose: merge bugs are combinatorial, not scalar.
# Two tables and four columns already produce every interaction the algorithm has --
# adding a fiftieth column only makes shrinking slower and failures harder to read.
PROFILE = settings(max_examples=200, deadline=None,
                   suppress_health_check=[HealthCheck.filter_too_much])


@st.composite
def base_snapshots(draw):
    n_tables = draw(st.integers(1, 2))
    b = schema()
    for tname in TABLES[:n_tables]:
        b = b.table(tname).col("id", "bigint", pk=True)
        for cname in draw(st.lists(st.sampled_from(COLS), min_size=1, max_size=3,
                                   unique=True)):
            b = b.col(cname, draw(st.sampled_from(TYPES)),
                      nullable=draw(st.booleans()))
    return b.build()


@st.composite
def edits(draw, n=3):
    """A list of (op_name, args) applied defensively -- an op that cannot apply to a
    given snapshot is skipped rather than filtered out, which keeps the generator
    from starving on inputs where most ops are invalid."""
    ops = []
    for _ in range(draw(st.integers(0, n))):
        op = draw(st.sampled_from([
            "rename_col", "retype_col", "set_nullable", "set_default",
            "add_col", "drop_col", "add_index", "rename_table", "add_table",
            "drop_table",
        ]))
        ops.append((op, {
            "table": draw(st.sampled_from(TABLES)),
            "col": draw(st.sampled_from(COLS)),
            "new_name": draw(st.sampled_from(["x", "y", "z"])),
            "type": draw(st.sampled_from(TYPES)),
            "flag": draw(st.booleans()),
            "tag": draw(st.sampled_from(["p", "q"])),
        }))
    return ops


def apply(snap, ops):
    for op, a in ops:
        try:
            e = snap.evolve()
            if op == "rename_col":
                e.rename_col(f"{a['table']}.{a['col']}", a["new_name"])
            elif op == "retype_col":
                e.retype_col(f"{a['table']}.{a['col']}", a["type"])
            elif op == "set_nullable":
                e.set_nullable(f"{a['table']}.{a['col']}", a["flag"])
            elif op == "set_default":
                e.set_default(f"{a['table']}.{a['col']}", a["tag"])
            elif op == "add_col":
                e.add_col(a["table"], a["new_name"], a["type"])
            elif op == "drop_col":
                e.drop_col(f"{a['table']}.{a['col']}")
            elif op == "add_index":
                e.add_index(a["table"], f"idx_{a['tag']}", [a["col"]])
            elif op == "rename_table":
                e.rename_table(a["table"], f"r_{a['tag']}")
            elif op == "add_table":
                e.add_table(f"n_{a['tag']}")
            elif op == "drop_table":
                e.drop_table(a["table"])
            snap = e.build()
        except (SchemaError, KeyError, StopIteration):
            continue        # op does not apply to this snapshot; that is fine
    return snap


# ------------------------------------------------------------------- properties
@PROFILE
@given(base_snapshots())
def test_M90_merging_a_snapshot_with_itself_is_the_identity(base):
    r = merge(base, base, base)

    assert r.is_clean, (r.conflicts, r.violations)
    assert r.merged == base


@PROFILE
@given(base_snapshots(), edits())
def test_M90b_merging_an_unchanged_side_yields_the_changed_side(base, ops):
    ours = apply(base, ops)
    assume(ours != base)

    r = merge(base, ours, base)

    assert r.status is not MergeStatus.CONFLICTED, r.conflicts
    if r.status is MergeStatus.CLEAN:
        assert r.merged == ours, "a side that changed nothing must contribute nothing"


@PROFILE
@given(base_snapshots(), edits(), edits())
def test_M91_merge_is_commutative_in_its_result(base, ops_a, ops_b):
    """Conflicts may be *presented* differently depending on which side is 'ours' --
    the labels swap. The merged schema must not depend on argument order, or two
    engineers merging the same pair of branches get different databases.
    """
    ours, theirs = apply(base, ops_a), apply(base, ops_b)

    fwd = merge(base, ours, theirs)
    rev = merge(base, theirs, ours)

    assert fwd.status is rev.status
    if fwd.status is not MergeStatus.CONFLICTED:
        assert fwd.merged == rev.merged
    else:
        assert ({(c.category, c.object_id, c.attribute) for c in fwd.conflicts} ==
                {(c.category, c.object_id, c.attribute) for c in rev.conflicts})


@PROFILE
@given(base_snapshots(), edits(), edits())
def test_M92_a_clean_result_is_never_secretly_invalid(base, ops_a, ops_b):
    """CLEAN is a claim about the whole schema, not just about conflict count. If it
    can be returned alongside a dangling reference or a duplicate name, the status is
    a lie and every downstream consumer inherits it."""
    r = merge(base, apply(base, ops_a), apply(base, ops_b))
    assume(r.status is MergeStatus.CLEAN)

    assert r.merged.dangling_references() == []
    for t in r.merged.tables:
        names = [c.name for c in t.columns]
        assert len(names) == len(set(names)), f"duplicate column in {t.name}"
    tnames = [t.name for t in r.merged.tables]
    assert len(tnames) == len(set(tnames))


@PROFILE
@given(base_snapshots(), edits(), edits())
def test_M93_the_result_is_always_a_whole_serializable_snapshot(base, ops_a, ops_b):
    """Never an intermediate state. Round-tripping through serialization is the
    check that matters, because that is how a result reaches storage and the UI."""
    from schemavcs.model import Snapshot

    r = merge(base, apply(base, ops_a), apply(base, ops_b))
    if r.merged is None:
        assert r.status is MergeStatus.CONFLICTED
        return

    assert Snapshot.from_dict(r.merged.to_dict()) == r.merged
    assert r.merged.dialect == base.dialect


@PROFILE
@given(base_snapshots(), edits(), edits())
def test_M94_a_clean_merge_never_loses_a_one_sided_change(base, ops_a, ops_b):
    """The property that catches silently dropped changes.

    Verified through diff, not through merge's own attribute walk: every object the
    diff engine reports as changed on either branch must also be reported as changed
    between the base and the merged result.
    """
    ours, theirs = apply(base, ops_a), apply(base, ops_b)
    r = merge(base, ours, theirs)
    assume(r.status is MergeStatus.CLEAN)

    def touched(target):
        """Keyed on (object, attribute), NOT on change kind.

        Kind is not stable across a merge and must not be: a column that was renamed
        on one branch and retyped on the other is reported as ALTER_COLUMN by the
        branch that retyped it and RENAME_COLUMN in the merged result. Keying on kind
        makes that correct merge look like a lost change.
        """
        out = set()
        for c in diff(base, target).changes:
            if c.deltas:
                out |= {(c.object_id, d.attribute) for d in c.deltas}
            else:
                out.add((c.object_id, c.kind.value))
        return out

    expected = touched(ours) | touched(theirs)
    actual = touched(r.merged)
    lost = expected - actual
    assert not lost, f"merge dropped {len(lost)} change(s): {sorted(map(str, lost))}"
