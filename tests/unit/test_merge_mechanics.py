"""Branch-level merge mechanics (M-100..M-107).

The engine merges snapshots; this layer decides *which* snapshots. Three cheap cases
have to be recognized before any conflict computation happens -- merging a branch into
itself, merging one already merged, and a fast-forward -- because each of them would
otherwise produce a pointless merge commit and, worse, invite the user to resolve
conflicts that have no reason to exist.
"""
import pytest

from schemavcs.engine import (MergeStatus, Repo, Resolution, StaleConflictsError,
                              UnresolvedConflictsError, merge, merge_branches)
from schemavcs.model import schema


@pytest.fixture
def repo(base_schema):
    r = Repo.init()
    r.commit("main", base_schema, message="initial")
    return r


def test_M100_merging_a_branch_into_itself_is_a_noop(repo):
    r = merge_branches(repo, ours="main", theirs="main")

    assert r.status is MergeStatus.UP_TO_DATE
    assert r.commit is None


def test_M101_merging_an_already_merged_branch_is_a_noop(repo, base_schema):
    repo.branch("feature", "main")
    repo.commit("feature", base_schema.evolve().add_col("users", "a", "int").build())
    first = merge_branches(repo, ours="main", theirs="feature")
    assert first.status is MergeStatus.FAST_FORWARD

    again = merge_branches(repo, ours="main", theirs="feature")

    assert again.status is MergeStatus.UP_TO_DATE
    assert again.commit is None


def test_M102_fast_forward_when_the_target_is_an_ancestor(repo, base_schema):
    repo.branch("feature", "main")
    ahead = base_schema.evolve().add_col("users", "a", "int").build()
    tip = repo.commit("feature", ahead)

    r = merge_branches(repo, ours="main", theirs="feature")

    assert r.status is MergeStatus.FAST_FORWARD
    assert repo.head("main").id == tip.id, "main should just move, not gain a commit"
    assert len(repo.head("main").parents) == 1, "a fast-forward creates no merge commit"


def test_M103_a_real_merge_produces_a_commit_with_two_parents(repo, base_schema):
    repo.branch("feature", "main")
    ours_tip = repo.commit("main",
                           base_schema.evolve().add_col("users", "a", "int").build())
    theirs_tip = repo.commit("feature",
                             base_schema.evolve().add_col("users", "b", "int").build())

    r = merge_branches(repo, ours="main", theirs="feature")

    assert r.status is MergeStatus.MERGED
    assert r.commit.parents == (ours_tip.id, theirs_tip.id)
    assert r.commit.is_merge


def test_M104_a_conflict_free_merge_commits_without_a_resolution_step(repo,
                                                                     base_schema):
    repo.branch("feature", "main")
    repo.commit("main", base_schema.evolve().add_col("users", "a", "int").build())
    repo.commit("feature", base_schema.evolve().add_col("users", "b", "int").build())

    r = merge_branches(repo, ours="main", theirs="feature")

    assert r.status is MergeStatus.MERGED
    cols = {c.name for c in repo.snapshot("main").table("users").columns}
    assert cols == {"id", "email", "a", "b"}


def test_M105_partial_resolution_is_rejected(repo, base_schema):
    """All-or-nothing (D12). Applying half a resolution would produce a schema
    neither engineer designed, and there is no mid-merge state to resume from."""
    ours = base_schema.evolve().retype_col("users.email", "text") \
                              .set_default("users.id", "1").build()
    theirs = base_schema.evolve().retype_col("users.email", "varchar(9)") \
                                .set_default("users.id", "2").build()
    conflicts = merge(base_schema, ours, theirs).conflicts
    assert len(conflicts) == 2, "precondition: two independent conflicts"

    with pytest.raises(UnresolvedConflictsError) as e:
        merge(base_schema, ours, theirs,
              resolutions={conflicts[0].key: Resolution.ours()})

    assert conflicts[1].key in e.value.missing


def test_M106_resolution_against_a_stale_conflict_set_is_rejected(base_schema):
    ours = base_schema.evolve().retype_col("users.email", "text").build()
    theirs = base_schema.evolve().retype_col("users.email", "varchar(9)").build()
    stale = merge(base_schema, ours, theirs)

    # The user was shown one conflict, then `theirs` moved on and grew a second.
    theirs2 = theirs.evolve().set_default("users.id", "2").build()
    ours2 = ours.evolve().set_default("users.id", "1").build()

    with pytest.raises(StaleConflictsError):
        merge(base_schema, ours2, theirs2,
              resolutions={c.key: Resolution.ours() for c in stale.conflicts},
              token=stale.token)


def test_M107_a_resolution_may_choose_a_third_value(base_schema):
    """Neither `text` nor `varchar(100)` -- the right answer is often a value only a
    human knows. Forcing a binary ours/theirs choice would make the tool lie."""
    from schemavcs.model.types import ColumnType

    ours = base_schema.evolve().retype_col("users.email", "text").build()
    theirs = base_schema.evolve().retype_col("users.email", "varchar(100)").build()
    c = merge(base_schema, ours, theirs).conflicts[0]

    r = merge(base_schema, ours, theirs,
              resolutions={c.key: Resolution.with_value(ColumnType.parse("varchar(320)"))})

    assert r.is_clean, (r.conflicts, r.violations)
    assert str(r.merged.col("users.email").type) == "varchar(320)"


def test_a_resolved_merge_still_gets_validated(base_schema):
    """Resolution answers conflicts; it does not exempt the result from category 5.
    A user resolving a rename conflict *towards* a name that already exists must
    still be stopped."""
    base = base_schema.evolve().add_col("users", "taken", "int").build()
    ours = base.evolve().rename_col("users.email", "a").build()
    theirs = base.evolve().rename_col("users.email", "b").build()
    c = merge(base, ours, theirs).conflicts[0]

    r = merge(base, ours, theirs,
              resolutions={c.key: Resolution.with_value("taken")})

    assert r.status is MergeStatus.INVALID
    assert r.violations[0].invariant == "duplicate_column_name"


def test_conflicted_branch_merge_does_not_move_the_branch(repo, base_schema):
    repo.branch("feature", "main")
    repo.commit("main", base_schema.evolve().retype_col("users.email", "text").build())
    repo.commit("feature",
                base_schema.evolve().retype_col("users.email", "varchar(9)").build())
    before = repo.head("main").id

    r = merge_branches(repo, ours="main", theirs="feature")

    assert r.status is MergeStatus.CONFLICTED
    assert r.commit is None
    assert repo.head("main").id == before, "a failed merge must leave the branch alone"


def test_merge_base_is_the_lca_not_the_branch_point(repo, base_schema):
    """The headline (D13) reaching into merge: after an earlier merge, the base for
    the next one is the tip that was merged in -- not where the branch was created.
    Using the branch point would re-present already-resolved changes as new."""
    repo.branch("feature", "main")
    a = base_schema.evolve().add_col("users", "a", "int").build()
    repo.commit("feature", a)
    merge_branches(repo, ours="main", theirs="feature")      # fast-forward

    b = a.evolve().add_col("users", "b", "int").build()
    repo.commit("feature", b)
    c = a.evolve().add_col("users", "c", "int").build()
    repo.commit("main", c)

    r = merge_branches(repo, ours="main", theirs="feature")

    assert r.status is MergeStatus.MERGED
    cols = {x.name for x in repo.snapshot("main").table("users").columns}
    assert cols == {"id", "email", "a", "b", "c"}


def test_M108_the_token_pins_the_conflicting_values_not_just_their_identity(base_schema):
    """The hole M-106 leaves open, found by building the UI (D43).

    M-106 moves a branch so a *new* conflict appears, and the changed conflict set is
    caught. But a conflict key is `attribute:column:<id>:type` -- it says nothing about
    the value. If the other branch re-edits the same attribute, the key set is
    identical and the old token still matches, so "take theirs" chosen while looking at
    `text` silently applies `varchar(200)` instead.

    The user is answering a question about values, so the values have to be in the token.
    """
    ours = base_schema.evolve().retype_col("users.email", "text").build()
    theirs = base_schema.evolve().retype_col("users.email", "varchar(9)").build()
    shown = merge(base_schema, ours, theirs)
    assert len(shown.conflicts) == 1

    # `theirs` changes its mind about the same attribute: same conflict, new value.
    theirs2 = base_schema.evolve().retype_col("users.email", "varchar(200)").build()
    assert [c.key for c in merge(base_schema, ours, theirs2).conflicts] == \
           [c.key for c in shown.conflicts], "precondition: the key set is unchanged"

    with pytest.raises(StaleConflictsError):
        merge(base_schema, ours, theirs2,
              resolutions={c.key: Resolution.theirs() for c in shown.conflicts},
              token=shown.token)
