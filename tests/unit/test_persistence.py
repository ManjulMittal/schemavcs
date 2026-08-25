"""Store contract, run against EVERY implementation (V-20..V-27).

Parametrized deliberately. An abstraction exercised through one implementation is not
abstract -- and here the in-memory store is precisely the one that would pass the tests
that matter while hiding a real bug, because a dict cannot lose a race with itself.
Running both means the SQLite path is held to the same contract rather than trusted.

The durability tests are the ones that need a real database, so they are marked and
skipped for the in-memory store rather than silently asserting nothing.
"""
import pytest

from schemavcs.engine import (InMemoryStore, MergeStatus, Repo, StaleHeadError,
                              merge_branches)
from schemavcs.model import schema
from schemavcs.storage import SqliteStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    """Both stores, and for SQLite a real file -- because a file is what can be
    reopened, and reopening is the whole point of the exercise."""
    if request.param == "memory":
        yield InMemoryStore()
    else:
        s = SqliteStore(tmp_path / "repo.db")
        yield s
        s.close()


@pytest.fixture
def durable(store, tmp_path):
    if isinstance(store, InMemoryStore):
        pytest.skip("in-memory store cannot outlive the process, by definition")
    return store


# ------------------------------------------------------------------ the contract
def test_V20_a_committed_snapshot_reads_back_identically(store, base_schema):
    r = Repo.init("main", store=store)
    r.commit("main", base_schema, message="initial")

    assert r.snapshot("main") == base_schema
    assert r.snapshot("main").content_hash() == base_schema.content_hash()


def test_V21_references_survive_the_round_trip(store, two_tables):
    """The part most likely to break silently. Column and table references are ids
    (D30), so a serialization bug would produce a snapshot whose foreign keys point
    at nothing -- and `==` alone would not necessarily catch it."""
    r = Repo.init("main", store=store)
    r.commit("main", two_tables)

    loaded = r.snapshot("main")
    assert loaded.dangling_references() == []
    assert loaded.constraint_ref("orders.fk_orders_user") == ("users", ["id"])


def test_V22_a_lost_race_is_refused_not_silently_applied(store, base_schema):
    """Two writers, one branch. The second must be told, not merged over."""
    r = Repo.init("main", store=store)
    first = r.commit("main", base_schema)

    a = base_schema.evolve().add_col("users", "a", "int").build()
    r.commit("main", a, expected_head=first.id)

    b = base_schema.evolve().add_col("users", "b", "int").build()
    with pytest.raises(StaleHeadError) as e:
        r.commit("main", b, expected_head=first.id)

    assert e.value.expected == first.id
    assert "were not applied" in str(e.value)


def test_V22b_compare_and_set_is_the_store_primitive(store, base_schema):
    """Asserted at the store level too, because this is the one operation where a
    check-then-write in Python would leave a window for two requests to interleave."""
    r = Repo.init("main", store=store)
    head = r.head("main").id
    other = r.commit("main", base_schema).id

    assert store.compare_and_set_head("main", expected=head, new=head) is False, \
        "stale expectation must not win"
    assert store.compare_and_set_head("main", expected=other, new=head) is True


def test_V23_creating_a_branch_twice_is_refused(store):
    r = Repo.init("main", store=store)
    r.branch("feature", "main")

    with pytest.raises(ValueError, match="already exists"):
        r.branch("feature", "main")


def test_V24_unknown_names_raise_rather_than_returning_none(store):
    r = Repo.init("main", store=store)

    with pytest.raises(KeyError, match="no such branch"):
        r.head("nope")
    with pytest.raises(KeyError, match="no such commit"):
        r.snapshot_at("00000000-0000-0000-0000-000000000000")


def test_V25_a_merge_commits_two_parents_and_they_read_back(store, base_schema):
    r = Repo.init("main", store=store)
    r.commit("main", base_schema)
    r.branch("feature", "main")
    ours = r.commit("main", base_schema.evolve().add_col("users", "a", "int").build())
    theirs = r.commit("feature",
                      base_schema.evolve().add_col("users", "b", "int").build())

    result = merge_branches(r, ours="main", theirs="feature")

    assert result.status is MergeStatus.MERGED
    reloaded = r.head("main")
    assert reloaded.parents == (ours.id, theirs.id), "parent ORDER must survive"
    assert reloaded.is_merge


def test_V26_history_is_walkable_through_the_store(store, base_schema):
    r = Repo.init("main", store=store)
    r.commit("main", base_schema, message="one")
    r.commit("main", base_schema.evolve().add_col("users", "a", "int").build(),
             message="two")

    messages = [c.message for c in r.history("main")]

    assert messages == ["two", "one", "genesis"]


def test_V27_branch_point_is_recorded_but_is_not_the_merge_base(store, base_schema):
    r = Repo.init("main", store=store)
    at = r.commit("main", base_schema).id
    r.branch("feature", "main")

    assert r.branch_point("feature") == at


# ------------------------------------------------------------------ durability
def test_V28_a_repo_survives_the_process_that_created_it(durable, base_schema,
                                                         tmp_path):
    """The point of the whole exercise. Written, closed, reopened by a fresh store
    object with no shared state -- which is what a serverless cold start looks like."""
    r = Repo.init("main", store=durable)
    r.commit("main", base_schema, message="initial")
    r.branch("feature", "main")
    r.commit("feature", base_schema.evolve().add_col("users", "a", "int").build(),
             message="on feature")
    durable.close()

    with SqliteStore(durable.path) as reopened:
        r2 = Repo.open(reopened)
        assert r2.branches() == ["feature", "main"]
        assert r2.snapshot("main") == base_schema
        assert {c.name for c in r2.snapshot("feature").table("users").columns} == \
            {"id", "email", "a"}
        assert [c.message for c in r2.history("feature")] == \
            ["on feature", "initial", "genesis"]


def test_V29_a_merge_across_a_reopen_uses_the_persisted_dag(durable, base_schema):
    """The merge base is computed from the DAG (D13). If parent links did not survive
    persistence, the LCA would be wrong and the merge would silently re-present
    changes that were already merged."""
    r = Repo.init("main", store=durable)
    r.commit("main", base_schema)
    r.branch("feature", "main")
    r.commit("main", base_schema.evolve().add_col("users", "a", "int").build())
    r.commit("feature", base_schema.evolve().add_col("users", "b", "int").build())
    durable.close()

    with SqliteStore(durable.path) as reopened:
        r2 = Repo.open(reopened)
        result = merge_branches(r2, ours="main", theirs="feature")

    assert result.status is MergeStatus.MERGED
    assert {c.name for c in result.commit.snapshot.table("users").columns} == \
        {"id", "email", "a", "b"}


def test_V30_a_conflicted_merge_writes_nothing(durable, base_schema):
    """A failed merge must leave no trace in the database, not a half-written commit."""
    r = Repo.init("main", store=durable)
    r.commit("main", base_schema)
    r.branch("feature", "main")
    r.commit("main", base_schema.evolve().retype_col("users.email", "text").build())
    r.commit("feature",
             base_schema.evolve().retype_col("users.email", "varchar(9)").build())
    before_head, before_count = r.head("main").id, r.commit_count()

    result = merge_branches(r, ours="main", theirs="feature")

    assert result.status is MergeStatus.CONFLICTED
    assert r.head("main").id == before_head
    assert r.commit_count() == before_count, "no orphan commit may be left behind"
