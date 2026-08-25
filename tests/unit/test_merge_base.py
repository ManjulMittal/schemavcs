"""L-* : merge base / LCA. See docs/test-plan.md section 4.

This is the headline decision (D13) and the expectations here were WRONG in the first
draft of the docs: after merging main into feature, the new base is the tip main had at
merge time -- not the merge commit, and not the original branch point. A merge commit can
only be the LCA in cross-merge topologies (L-06c).

L-20 is the single most valuable test in the suite; L-21 proves it has teeth.
"""
import pytest

from schemavcs.engine import MultipleMergeBasesError, Repo, merge_base
from schemavcs.model import schema


def s(**cols):
    b = schema().table("t").col("id", "bigint", pk=True)
    for n, ty in cols.items():
        b = b.col(n, ty)
    return b.build()


def linear_repo():
    """main: G -> A -> B ; feature branches at A -> C"""
    r = Repo.init()
    a = r.commit("main", s(a="int"))
    r.branch("feature", "main")
    b = r.commit("main", s(a="int", b="int"))
    c = r.commit("feature", s(a="int", c="int"))
    return r, a, b, c


def test_l01_lca_of_a_commit_with_itself():
    r = Repo.init()
    a = r.commit("main", s(a="int"))
    assert merge_base(r, a.id, a.id) == a.id


def test_l02_ancestor_case():
    r = Repo.init()
    a = r.commit("main", s(a="int"))
    b = r.commit("main", s(a="int", b="int"))
    assert merge_base(r, b.id, a.id) == a.id


def test_l03_l04_simple_fork_returns_the_fork_point():
    r, a, b, c = linear_repo()
    assert merge_base(r, b.id, c.id) == a.id


def test_l05_fast_forward_is_detectable():
    r = Repo.init()
    a = r.commit("main", s(a="int"))
    r.branch("feature", "main")
    c = r.commit("feature", s(a="int", c="int"))
    # main is wholly contained in feature -> base == main's head -> fast-forward
    assert merge_base(r, a.id, c.id) == a.id


def test_l06_base_advances_past_the_branch_point_after_a_merge():
    """THE corrected expectation. main=G->A->B, feature=A->C, merge main into feature
    creating M(C,B); then D lands on main. LCA(D, M) is B -- the tip main had at merge
    time. Not M, and not the branch point A."""
    r, a, b, c = linear_repo()
    m = r.merge_commit("feature", ours=c.id, theirs=b.id,
                       snapshot=s(a="int", b="int", c="int"))
    d = r.commit("main", s(a="int", b="int", d="int"))

    got = merge_base(r, d.id, m.id)
    assert got == b.id, "base must advance to main's tip at merge time"
    assert got != a.id, "must not still be the original branch point"
    assert got != m.id, "the merge commit is not the base here"


def test_l06b_immediately_after_a_merge_the_base_is_the_source_head():
    r, a, b, c = linear_repo()
    m = r.merge_commit("feature", ours=c.id, theirs=b.id,
                       snapshot=s(a="int", b="int", c="int"))
    assert merge_base(r, b.id, m.id) == b.id, "so feature->main is a fast-forward"


def test_l06c_a_merge_commit_can_be_the_base_in_a_cross_merge():
    """Guards against over-generalising L-06: M *can* be the LCA, just not there."""
    r, a, b, c = linear_repo()
    m1 = r.merge_commit("feature", ours=c.id, theirs=b.id,
                        snapshot=s(a="int", b="int", c="int"))
    m2 = r.merge_commit("main", ours=b.id, theirs=m1.id,
                        snapshot=s(a="int", b="int", c="int"))
    d = r.commit("feature", s(a="int", b="int", c="int", d="int"))
    assert merge_base(r, m2.id, d.id) == m1.id


def test_l07_base_advances_again_on_a_second_merge():
    r, a, b, c = linear_repo()
    m1 = r.merge_commit("feature", ours=c.id, theirs=b.id,
                        snapshot=s(a="int", b="int", c="int"))
    b2 = r.commit("main", s(a="int", b="int", e="int"))
    m2 = r.merge_commit("feature", ours=m1.id, theirs=b2.id,
                        snapshot=s(a="int", b="int", c="int", e="int"))
    d = r.commit("main", s(a="int", b="int", e="int", f="int"))
    assert merge_base(r, d.id, m2.id) == b2.id


def test_l08_commits_after_the_merge_do_not_move_the_base():
    r, a, b, c = linear_repo()
    m = r.merge_commit("feature", ours=c.id, theirs=b.id,
                       snapshot=s(a="int", b="int", c="int"))
    r.commit("feature", s(a="int", b="int", c="int", x="int"))
    d = r.commit("main", s(a="int", b="int", y="int"))
    assert merge_base(r, d.id, r.head("feature").id) == b.id


def test_l09_l10_criss_cross_is_refused_not_approximated():
    """D14: a loud refusal on a rare topology beats a quiet miscalculation."""
    r = Repo.init()
    root = r.commit("main", s(a="int"))
    r.branch("feature", "main")
    b = r.commit("main", s(a="int", b="int"))
    c = r.commit("feature", s(a="int", c="int"))
    # each branch merges the other -> two incomparable candidate bases
    r.merge_commit("main", ours=b.id, theirs=c.id, snapshot=s(a="int", b="int", c="int"))
    r.merge_commit("feature", ours=c.id, theirs=b.id, snapshot=s(a="int", b="int", c="int"))

    with pytest.raises(MultipleMergeBasesError) as e:
        merge_base(r, r.head("main").id, r.head("feature").id)
    assert len(e.value.candidates) == 2
    assert {b.id, c.id} == set(e.value.candidates)
    assert "ambiguous" in str(e.value).lower()


def test_l11_deep_dag():
    r = Repo.init()
    prev = None
    for i in range(40):
        prev = r.commit("main", s(**{f"c{i}": "int"}))
    fork = prev
    r.branch("feature", "main")
    for i in range(10):
        r.commit("main", s(**{f"m{i}": "int"}))
    for i in range(10):
        r.commit("feature", s(**{f"f{i}": "int"}))
    assert merge_base(r, r.head("main").id, r.head("feature").id) == fork.id


def test_l12_wide_dag_pairwise():
    r = Repo.init()
    root = r.commit("main", s(a="int"))
    heads = []
    for i in range(8):
        r.branch(f"b{i}", "main")
        heads.append(r.commit(f"b{i}", s(**{f"x{i}": "int"})).id)
    for i in range(8):
        for j in range(i + 1, 8):
            assert merge_base(r, heads[i], heads[j]) == root.id


# ============================================ the regression that justifies D13
def test_l20_a_resolved_conflict_does_not_reappear_on_the_next_merge():
    """The single most valuable test in the suite.

    Resolve a conflict once, commit more on main, merge again -- the previously
    resolved conflict must be gone, because the base advanced past it.
    """
    r = Repo.init()
    start = r.commit("main", s(email="varchar(255)"))
    r.branch("feature", "main")

    # divergent retype of the same column -> a genuine conflict
    theirs = r.commit("main", s(email="text"))
    ours = r.commit("feature", s(email="varchar(500)"))

    b1 = merge_base(r, ours.id, theirs.id)
    assert b1 == start.id
    resolved = s(email="text")                      # human resolved it: took theirs
    m = r.merge_commit("feature", ours=ours.id, theirs=theirs.id, snapshot=resolved)

    # unrelated work continues on main
    theirs2 = r.commit("main", s(email="text", extra="int"))

    b2 = merge_base(r, m.id, theirs2.id)
    assert b2 == theirs.id, "base advanced to the already-merged main tip"
    # the base already carries the resolution, so email is no longer divergent
    assert r.snapshot_at(b2).col("t.email").type == r.snapshot_at(m.id).col("t.email").type


def test_l21_the_naive_stored_branch_point_implementation_fails_l20():
    """Unusual on purpose: asserts the WRONG implementation is wrong.

    Without this, L-20 could pass for reasons unrelated to merge-base correctness --
    it passes trivially on a fresh branch.
    """
    r = Repo.init()
    start = r.commit("main", s(email="varchar(255)"))
    r.branch("feature", "main")
    theirs = r.commit("main", s(email="text"))
    ours = r.commit("feature", s(email="varchar(500)"))
    m = r.merge_commit("feature", ours=ours.id, theirs=theirs.id, snapshot=s(email="text"))
    theirs2 = r.commit("main", s(email="text", extra="int"))

    naive = r.branch_point("feature")          # what a stored branch point would give
    correct = merge_base(r, m.id, theirs2.id)

    assert naive == start.id
    assert correct != naive, "if these agree, L-20 is not actually testing anything"
    # and the naive base still shows the column as divergent -- the conflict returns
    assert r.snapshot_at(naive).col("t.email").type != r.snapshot_at(m.id).col("t.email").type
