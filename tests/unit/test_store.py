"""V-* : commit DAG, branches, optimistic concurrency. See docs/test-plan.md section 3."""
import pytest

from schemavcs.engine import Repo, StaleHeadError
from schemavcs.model import schema


def base():
    return schema().table("users").col("id", "bigint", pk=True).build()


def test_v01_init_creates_a_genesis_commit_with_an_empty_schema():
    r = Repo.init()
    head = r.head("main")
    assert head.parents == ()
    assert r.snapshot("main").tables == ()


def test_v02_commit_advances_head_and_records_parent():
    r = Repo.init()
    before = r.head("main")
    c = r.commit("main", base(), message="add users")
    assert r.head("main").id == c.id
    assert c.parents == (before.id,)
    assert c.message == "add users"


def test_v03_branch_points_at_the_same_commit_without_creating_one():
    r = Repo.init()
    r.commit("main", base())
    n = r.commit_count()
    r.branch("feature", "main")
    assert r.head("feature").id == r.head("main").id
    assert r.commit_count() == n, "branching must not create a commit"


def test_v04_commits_on_one_branch_do_not_move_another():
    r = Repo.init()
    r.commit("main", base())
    r.branch("feature", "main")
    main_head = r.head("main").id

    r.commit("feature", base().evolve().add_col("users", "email", "varchar(255)").build())

    assert r.head("main").id == main_head
    assert r.head("feature").id != main_head


def test_v05_history_walk_reaches_genesis():
    r = Repo.init()
    s = base()
    r.commit("main", s)
    r.commit("main", s.evolve().add_col("users", "a", "int").build())
    hist = r.history("main")
    assert len(hist) == 3
    assert hist[-1].parents == ()


def test_v06_committing_an_unchanged_schema_is_rejected_as_empty():
    r = Repo.init()
    s = base()
    r.commit("main", s)
    with pytest.raises(ValueError, match="(?i)no change"):
        r.commit("main", s)


def test_v07_reading_any_commit_is_a_single_lookup():
    """D2: snapshots are self-describing, so no replay is needed."""
    r = Repo.init()
    c = r.commit("main", base())
    assert r.snapshot_at(c.id) == base()


def test_commits_are_immutable():
    r = Repo.init()
    c = r.commit("main", base())
    with pytest.raises((AttributeError, TypeError)):
        c.message = "rewritten"


def test_unknown_branch_raises_clearly():
    r = Repo.init()
    with pytest.raises(KeyError, match="nope"):
        r.head("nope")


# --------------------------------------------------- optimistic concurrency (C-01)
def test_c01_concurrent_commit_with_a_stale_head_is_rejected():
    """The real concurrency failure mode: two people commit, the loser's work vanishes."""
    r = Repo.init()
    r.commit("main", base())
    stale = r.head("main").id

    r.commit("main", base().evolve().add_col("users", "a", "int").build())

    with pytest.raises(StaleHeadError) as e:
        r.commit("main", base().evolve().add_col("users", "b", "int").build(),
                 expected_head=stale)
    assert "main" in str(e.value)
    assert r.head("main").id in str(e.value), "message must name the current head"


def test_commit_with_a_correct_expected_head_succeeds():
    r = Repo.init()
    r.commit("main", base())
    h = r.head("main").id
    r.commit("main", base().evolve().add_col("users", "a", "int").build(), expected_head=h)


def test_c04_concurrent_commits_to_different_branches_both_succeed():
    r = Repo.init()
    r.commit("main", base())
    r.branch("f1", "main")
    r.branch("f2", "main")
    h = r.head("main").id
    r.commit("f1", base().evolve().add_col("users", "a", "int").build(), expected_head=h)
    r.commit("f2", base().evolve().add_col("users", "b", "int").build(), expected_head=h)
    assert r.head("f1").id != r.head("f2").id
