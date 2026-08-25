"""HTTP layer (W-01..W-24).

The engine is tested without a browser; these cover what only HTTP can get wrong --
cookies, redirects, form parsing, error rendering -- plus the two places where the web
layer is load-bearing for correctness: workspace isolation and merge-token staleness.
"""
import re

import pytest

from schemavcs.web import workspaces

from .helpers import (change_lines, conflict_keys, error, notice, sql, status,
                      token)


def branches(client, ws):
    return sorted(workspaces.open_repo(ws).branches())


def edit(client, ws, branch, **form):
    return client.post(f"/w/{ws}/branch/{branch}/edit", data=form, follow_redirects=True)


def branch(client, ws, name, source="main"):
    return client.post(f"/w/{ws}/branches", data={"name": name, "source": source},
                       follow_redirects=True)


# ------------------------------------------------------------- workspaces
def test_W01_the_front_door_is_a_working_demo_not_a_form(client):
    """A reviewer arrives from a link with no context. If the first screen is an empty
    textarea, the interesting behaviour is behind six clicks nobody will make (D46)."""
    r = client.get("/", follow_redirects=True)

    assert r.status_code == 200
    assert r.url.path.endswith("/branch/main")
    assert "schemavcs_ws" in client.cookies
    ws = client.cookies["schemavcs_ws"]

    # Two engineers' work already diverged, so there is something to merge on arrival.
    assert branches(client, ws) == ["main", "nickname-a", "nickname-b",
                                    "rename-email", "widen-email"]


def test_W01b_the_demo_lands_with_the_tour_and_the_operation_list_visible(client):
    r = client.get("/", follow_redirects=True)

    for phrase in ("Guided tour", "A diff that understands renames",
                   "Everything you can do", "change type", "three-way merge"):
        assert phrase in r.text, phrase
    assert "A rename is not a drop plus an add" in r.text, "the hero states the claim"


def test_W01e_every_response_carries_a_strict_content_security_policy(client, ws):
    """The app serves one stylesheet and one script from its own origin and nothing
    else, so the policy can be the strict version. Templates carry no inline styles or
    handlers to keep it that way -- this test is what notices when one creeps back in.
    """
    r = client.get(f"/w/{ws}/branch/main")

    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_W01f_templates_carry_no_inline_styles_or_handlers():
    """The CSP above is only strict if nothing needs `unsafe-inline`. Asserting on the
    templates catches the cause; asserting on the header catches the symptom."""
    import pathlib
    import re

    templates = pathlib.Path(__file__).resolve().parents[2] / "src/schemavcs/web/templates"
    offenders = []
    for path in templates.glob("*.html"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r'\sstyle="|\son[a-z]+="', line):
                offenders.append(f"{path.name}:{n}: {line.strip()[:70]}")

    assert not offenders, "inline style/handler would force unsafe-inline:\n  " + \
        "\n  ".join(offenders)


def test_W01c_bring_your_own_schema_is_still_reachable(client):
    r = client.get("/start")

    assert r.status_code == 200
    assert "CREATE TABLE users" in r.text


def test_W01d_start_over_replaces_the_workspace(client, ws):
    branch(client, ws, "mine")

    r = client.post("/reset", follow_redirects=True)

    assert client.cookies["schemavcs_ws"] != ws
    assert "mine" not in r.text


def test_W02_creating_a_workspace_sets_a_cookie_and_lands_on_main(client):
    r = client.post("/new", data={"ddl": "", "dialect": "postgres"})

    assert r.status_code == 200
    assert "schemavcs_ws" in client.cookies
    assert r.url.path.endswith("/branch/main")
    assert "users" in r.text and "orders" in r.text


@pytest.mark.parametrize("path", ["branch/main", "compare", "merge", "migration"])
def test_W02b_every_workspace_page_carries_the_tour(client, ws, path):
    """A reviewer who bookmarks or is sent a deep link still needs the narrative."""
    r = client.get(f"/w/{ws}/{path}")

    assert "Guided tour" in r.text
    assert "Two engineers diverge" in r.text


def test_W03_returning_with_a_cookie_goes_straight_back_to_the_schema(client, ws):
    r = client.get("/")
    assert r.url.path == f"/w/{ws}/branch/main"


def test_W04_bad_ddl_is_reported_and_no_workspace_is_created(client, data_dir):
    r = client.post("/new", data={"ddl": "CREATE TABLE (", "dialect": "postgres"})

    assert r.status_code == 200
    assert "cannot import DDL" in error(r.text)
    assert not data_dir.exists() or not list(data_dir.glob("*.db"))


def test_W05_a_change_script_is_refused_with_the_boundary_message(client):
    """The D41 boundary has to survive the trip through HTTP -- this is where a real
    user meets it, and a generic 500 would teach them nothing."""
    r = client.post("/new", data={"ddl": "ALTER TABLE users ADD COLUMN x int;",
                                  "dialect": "postgres"})

    assert "source of truth" in error(r.text)


def test_W06_a_stale_cookie_recovers_instead_of_500ing(client, ws, data_dir):
    """The deployed free tier restarts onto empty disk, so a cookie pointing at a
    workspace that no longer exists is an ordinary event, not an edge case."""
    workspaces.path_for(ws).unlink()

    r = client.get("/", follow_redirects=True)

    assert r.status_code == 200
    assert client.cookies["schemavcs_ws"] != ws
    assert r.url.path.endswith("/branch/main")


def test_W07_workspaces_are_isolated(client, ws):
    from fastapi.testclient import TestClient
    from schemavcs.web.app import app

    branch(client, ws, "mine")
    other = TestClient(app)
    other.post("/new", data={"ddl": "", "dialect": "postgres"})

    assert "mine" not in other.get("/", follow_redirects=True).text
    assert other.cookies["schemavcs_ws"] != ws


@pytest.mark.parametrize("bad", ["../../etc/passwd", "..", "x" * 40, "not-hex-at-all"])
def test_W08_a_malformed_workspace_id_never_reaches_the_filesystem(bad):
    """The id comes from a cookie, and a cookie is attacker-controlled input that gets
    concatenated into a path."""
    assert not workspaces.is_valid(bad)
    with pytest.raises(ValueError):
        workspaces.path_for(bad)


def test_W09_an_unknown_workspace_in_the_url_is_not_a_crash(client):
    r = client.get("/w/0123456789abcdef/branch/main", follow_redirects=True)

    assert r.status_code == 200
    assert r.url.path.endswith("/branch/main")
    assert client.cookies["schemavcs_ws"] != "0123456789abcdef"


# ------------------------------------------------------------------ edits
def test_W09b_every_operation_is_reachable_and_no_form_is_orphaned(client, ws):
    """Found by writing the operations list: it promised `set default` and `add check`,
    which the backend supported and the editor had no form for. A capability list that
    over-promises is worse than none -- the reviewer goes looking and finds nothing.

    Both directions matter. A handler with no route to it is a dead promise; a form
    posting an op nobody handles is a dead button. The four in `SUBSUMED` are reached
    through the combined column editor rather than a form of their own (D49), and naming
    them here is what keeps that an explicit decision instead of an oversight.
    """
    import re

    from schemavcs.web.edits import EDITS, SUBSUMED

    html = client.get(f"/w/{ws}/branch/main").text
    exposed = set(re.findall(r'name="op" value="(\w+)"', html))

    assert not exposed - set(EDITS), f"form posts an unhandled op: {exposed - set(EDITS)}"
    assert set(EDITS) - exposed == set(SUBSUMED), {
        "unreachable": sorted(set(EDITS) - exposed - set(SUBSUMED)),
        "declared subsumed but actually rendered": sorted(exposed & set(SUBSUMED)),
    }


def test_W10_an_edit_is_a_commit(client, ws):
    before = workspaces.open_repo(ws).commit_count()

    r = edit(client, ws, "main", op="add_column", table="users", name="phone",
             type="varchar(32)")

    assert "add column users.phone" in notice(r.text)
    assert workspaces.open_repo(ws).commit_count() == before + 1
    assert workspaces.open_repo(ws).snapshot("main").col("users.phone") is not None


def test_W11_editing_redirects_so_a_reload_does_not_commit_twice(client, ws):
    """Post/Redirect/Get is not cosmetic here: with commit-per-edit, a resubmitted POST
    is a real second commit, and for `add_column` it is a duplicate-name error the user
    never asked for."""
    r = client.post(f"/w/{ws}/branch/main/edit",
                    data={"op": "add_column", "table": "users", "name": "phone",
                          "type": "varchar(32)"}, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/w/{ws}/branch/main?notice=")

    count = workspaces.open_repo(ws).commit_count()
    client.get(r.headers["location"])          # the reload
    assert workspaces.open_repo(ws).commit_count() == count


def test_W12_a_rejected_edit_reports_why_and_commits_nothing(client, ws):
    before = workspaces.open_repo(ws).commit_count()

    r = edit(client, ws, "main", op="add_column", table="users", name="email",
             type="int")

    assert "duplicate column: users.email" in error(r.text)
    assert workspaces.open_repo(ws).commit_count() == before


def test_W13_missing_form_input_is_a_message_not_a_stack_trace(client, ws):
    r = edit(client, ws, "main", op="add_column", table="users", name="", type="int")

    assert "name is required" in error(r.text)


def test_W14_an_unknown_operation_is_refused(client, ws):
    r = edit(client, ws, "main", op="drop_database", table="users")

    assert "unknown operation" in error(r.text)


def test_W15_a_foreign_key_with_mismatched_column_counts_explains_itself(client, ws):
    r = edit(client, ws, "main", op="add_fk", table="orders", name="fk2",
             columns="user_id,status", ref_table="users", ref_columns="id")

    assert "pairs columns positionally" in error(r.text)


def test_W16_branching_switches_to_the_new_branch(client, ws):
    r = branch(client, ws, "feature")

    assert "branched feature from main" in notice(r.text)
    assert r.url.path == f"/w/{ws}/branch/feature"
    assert branches(client, ws) == ["feature", "main"]


def test_W17_a_duplicate_branch_name_is_refused(client, ws):
    branch(client, ws, "feature")

    assert "branch already exists" in error(branch(client, ws, "feature").text)


# ------------------------------------------------------------------- diff
def test_W18_the_diff_says_renamed_rather_than_dropped_and_added(client, ws):
    branch(client, ws, "rename")
    edit(client, ws, "rename", op="rename_column", path="users.email",
         new_name="contact_email")

    r = client.get(f"/w/{ws}/compare?base=main&target=rename")

    assert change_lines(r.text) == ["renamed column users.email -&gt; contact_email"]


def test_W19_identical_branches_diff_to_nothing(client, ws):
    branch(client, ws, "idle")

    r = client.get(f"/w/{ws}/compare?base=main&target=idle")

    assert "No differences" in r.text


# ------------------------------------------------------------------ merge
def _diverge(client, ws):
    """The headline case: a rename on one branch, a retype on the other."""
    branch(client, ws, "rename")
    edit(client, ws, "rename", op="rename_column", path="users.email",
         new_name="contact_email")
    edit(client, ws, "main", op="retype_column", path="users.email", type="text")


def test_W20_a_rename_and_a_retype_of_the_same_column_merge_cleanly(client, ws):
    _diverge(client, ws)

    r = client.post(f"/w/{ws}/merge", data={"ours": "main", "theirs": "rename"})

    assert status(r.text) == "merged"
    col = workspaces.open_repo(ws).snapshot("main").col("users.contact_email")
    assert col is not None and col.type.render() == "text"


def test_W21_a_real_conflict_shows_all_three_sides_and_writes_nothing(client, ws):
    for b, t in (("a", "varchar(128)"), ("b", "text")):
        branch(client, ws, b, "main")
        edit(client, ws, b, op="retype_column", path="users.nickname", type=t)
    head = workspaces.open_repo(ws).head("a").id

    r = client.post(f"/w/{ws}/merge", data={"ours": "a", "theirs": "b"})

    assert status(r.text) == "1 conflict"
    for side in ("varchar(64)", "varchar(128)", "text"):    # base, ours, theirs
        assert side in r.text
    assert workspaces.open_repo(ws).head("a").id == head, "the branch must not move"


def test_W22_choosing_a_side_completes_the_merge(client, ws):
    for b, t in (("a", "varchar(128)"), ("b", "text")):
        branch(client, ws, b, "main")
        edit(client, ws, b, op="retype_column", path="users.nickname", type=t)
    shown = client.post(f"/w/{ws}/merge", data={"ours": "a", "theirs": "b"})
    key = conflict_keys(shown.text)[0]

    r = client.post(f"/w/{ws}/merge", data={
        "ours": "a", "theirs": "b", "token": token(shown.text),
        f"resolve:{key}": "theirs"})

    assert status(r.text) == "merged"
    assert workspaces.open_repo(ws).snapshot("a").col("users.nickname").type.render() == "text"


def test_W23_a_decision_made_about_stale_values_is_refused(client, ws):
    """The reason the token exists, exercised through the form it protects: the user
    picks "theirs" while looking at `text`, and `theirs` becomes something else before
    they submit."""
    for b, t in (("a", "varchar(128)"), ("b", "text")):
        branch(client, ws, b, "main")
        edit(client, ws, b, op="retype_column", path="users.nickname", type=t)
    shown = client.post(f"/w/{ws}/merge", data={"ours": "a", "theirs": "b"})
    key, tok = conflict_keys(shown.text)[0], token(shown.text)

    edit(client, ws, "b", op="retype_column", path="users.nickname", type="varchar(200)")
    r = client.post(f"/w/{ws}/merge", data={
        "ours": "a", "theirs": "b", "token": tok, f"resolve:{key}": "theirs"})

    assert "branches moved while you were deciding" in error(r.text)
    assert workspaces.open_repo(ws).snapshot("a").col("users.nickname").type.render() \
        == "varchar(128)", "nothing may be written"


def test_W24_an_integrity_violation_is_reported_as_unresolvable(client, ws):
    """No object was touched by both sides, so there is nothing to pick between --
    the page has to say that rather than offering a meaningless choice."""
    branch(client, ws, "drop-users", "main")
    edit(client, ws, "drop-users", op="drop_constraint", path="orders.orders_user_fk")
    edit(client, ws, "drop-users", op="drop_table", table="users")
    branch(client, ws, "sessions", "main")
    edit(client, ws, "sessions", op="add_table", name="sessions")
    edit(client, ws, "sessions", op="add_column", table="sessions", name="user_id",
         type="bigint", not_null="on")
    edit(client, ws, "sessions", op="add_fk", table="sessions", name="sessions_user_fk",
         columns="user_id", ref_table="users", ref_columns="id")

    r = client.post(f"/w/{ws}/merge", data={"ours": "sessions", "theirs": "drop-users"})

    assert status(r.text) == "invalid"
    assert "no longer exists" in r.text
    assert "resolve:" not in r.text, "an invalid merge offers no sides to pick"


# -------------------------------------------------------------- migration
def test_W25_the_migration_is_deployed_to_target_not_a_rendering_of_the_merge(client, ws):
    _diverge(client, ws)
    client.post(f"/w/{ws}/merge", data={"ours": "main", "theirs": "rename"})

    r = client.get(f"/w/{ws}/migration?deployed=rename&target=main&dialect=postgres")

    assert 'ALTER TABLE "users" ALTER COLUMN "contact_email" TYPE text' in sql(r.text)


@pytest.mark.parametrize("dialect,expected", [
    ("postgres", '"users" RENAME COLUMN "email" TO "contact_email"'),
    ("mysql", "`users` RENAME COLUMN `email` TO `contact_email`"),
])
def test_W26_one_plan_renders_per_engine(client, ws, dialect, expected):
    branch(client, ws, "rename")
    edit(client, ws, "rename", op="rename_column", path="users.email",
         new_name="contact_email")

    r = client.get(f"/w/{ws}/migration?deployed=main&target=rename&dialect={dialect}")

    assert expected in sql(r.text)


def test_W27_a_destructive_migration_withholds_the_sql_until_acknowledged(client, ws):
    branch(client, ws, "slim")
    edit(client, ws, "slim", op="drop_column", path="users.nickname")

    r = client.get(f"/w/{ws}/migration?deployed=main&target=slim&dialect=postgres")

    assert sql(r.text) is None, "SQL must not be handed over unasked"
    assert "destroys data" in r.text
    assert "drop_column users.nickname" in r.text

    ack = client.get(f"/w/{ws}/migration?deployed=main&target=slim&dialect=postgres"
                     "&acknowledge=true")
    assert 'DROP COLUMN "nickname"' in sql(ack.text)


def test_W28_an_empty_migration_says_so_rather_than_showing_empty_sql(client, ws):
    branch(client, ws, "idle")

    r = client.get(f"/w/{ws}/migration?deployed=main&target=idle&dialect=postgres")

    assert sql(r.text) is None
    assert "Nothing to do" in r.text


# ------------------------------------------------------------- types (D48)
def test_W33_a_type_the_engine_does_not_have_is_refused_at_the_point_of_typing(client, ws):
    """Found by a reviewer typing `somethign` into the type box and being allowed to.

    The canonical model accepts any name, so nothing complained until the generated SQL
    reached a server. The editor knows which engine the schema targets, so it is the
    first layer that can answer.
    """
    before = workspaces.open_repo(ws).commit_count()

    r = edit(client, ws, "main", op="add_column", table="users", name="oops",
             type="somethign")

    msg = error(r.text)
    assert "no type called 'somethign'" in msg
    assert "varchar" in msg, "say what IS available"
    assert workspaces.open_repo(ws).commit_count() == before, "nothing may be committed"


def test_W34_a_retype_to_a_nonexistent_type_is_refused_too(client, ws):
    r = edit(client, ws, "main", op="retype_column", path="users.email", type="strng")

    assert "no type called 'strng'" in error(r.text)


def test_W35_a_malformed_type_gets_a_different_message_than_an_unknown_one(client, ws):
    """`varchar(` is a syntax problem; `somethign` is a vocabulary problem. Telling a
    user to check the spelling when the parentheses are wrong sends them the wrong way."""
    r = edit(client, ws, "main", op="add_column", table="users", name="x",
             type="varchar(")

    msg = error(r.text)
    assert "is not a type" in msg and "no type called" not in msg


def test_W36_the_type_is_chosen_from_a_list_not_typed(client, ws):
    """The set of valid base names is fixed and known, so a free text box only invites
    a mistake the tool then has to reject (D50). The size stays typed, because
    `varchar(255)` is not enumerable."""
    html = client.get(f"/w/{ws}/branch/main").text

    assert '<select name="type_base"' in html
    assert 'list="types"' not in html and "<datalist" not in html
    for t in ("varchar", "timestamptz", "jsonb", "decimal"):
        assert f'<option value="{t}"' in html
    assert 'name="type_params"' in html, "the size is still an open field"


def test_W36b_the_current_type_is_preselected_when_editing_a_column(client, ws):
    """A picker that opens on the wrong value is worse than a text box: submitting the
    form unchanged would silently retype the column."""
    html = client.get(f"/w/{ws}/branch/main").text

    assert '<option value="varchar" selected>' in html      # users.email is varchar(255)
    assert 'name="type_params" value="255"' in html


# ------------------------------------------------- staying in place (D49)
def test_W38_an_edit_returns_to_the_table_it_was_made_on(client, ws):
    """Reported from use: creating a table threw the page back to the top, away from
    the thing just created. The redirect has to name where the work was happening."""
    r = client.post(f"/w/{ws}/branch/main/edit",
                    data={"op": "add_column", "table": "orders", "name": "note",
                          "type": "text"}, follow_redirects=False)

    location = r.headers["location"]
    assert location.endswith("#t-orders"), "the browser needs a fragment to scroll to"
    assert "open=orders" in location, "and the editor has to still be open when it lands"


def test_W39_creating_a_table_lands_on_that_table_ready_to_use(client, ws):
    """The one case where the old behaviour was most obviously wrong: you create a
    table, and the page returns you to the top with the new empty table off-screen."""
    r = client.post(f"/w/{ws}/branch/main/edit",
                    data={"op": "add_table", "name": "sessions"},
                    follow_redirects=False)

    assert r.headers["location"].endswith("#t-sessions")

    page = client.get(r.headers["location"].split("#")[0]).text
    assert 'id="t-sessions"' in page
    assert re.search(r'<details class="editor" open>\s*<summary>Add to <code>sessions',
                     page), "the new table's editor should be open, not collapsed"


def test_W40_renaming_a_table_follows_it_to_its_new_name(client, ws):
    """The anchor has to be the name that exists *after* the edit, or it points at
    nothing."""
    r = client.post(f"/w/{ws}/branch/main/edit",
                    data={"op": "rename_table", "table": "orders",
                          "new_name": "purchases"}, follow_redirects=False)

    assert r.headers["location"].endswith("#t-purchases")


def test_W41_dropping_a_table_does_not_anchor_to_the_hole_it_left(client, ws):
    r = client.post(f"/w/{ws}/branch/main/edit",
                    data={"op": "drop_table", "table": "orders"},
                    follow_redirects=False)

    assert "#t-" not in r.headers["location"]


def test_W42_a_rejected_edit_also_comes_back_in_place(client, ws):
    """Losing your position is worse on failure than on success -- you have to find the
    form again to correct it."""
    r = client.post(f"/w/{ws}/branch/main/edit",
                    data={"op": "add_column", "table": "users", "name": "email",
                          "type": "int"}, follow_redirects=False)

    assert "error=" in r.headers["location"]
    assert r.headers["location"].endswith("#t-users")


# ------------------------------------------- one form per column (D49)
def test_W43_one_form_changes_everything_about_a_column_in_one_commit(client, ws):
    before = workspaces.open_repo(ws).commit_count()

    r = edit(client, ws, "main", op="alter_column", path="users.nickname",
             name="handle", type="varchar(200)", default="anon", nullable_field="1")

    assert "rename to handle" in notice(r.text)
    assert workspaces.open_repo(ws).commit_count() == before + 1, "one action, one commit"

    col = workspaces.open_repo(ws).snapshot("main").col("users.handle")
    assert (col.type.render(), col.default, col.nullable) == ("varchar(200)", "anon", False)


def test_W44_only_the_fields_that_changed_are_touched(client, ws):
    """The form posts every field every time, so the handler has to diff against the
    current column -- otherwise the commit message describes edits that did not happen,
    and a no-op submit still writes a commit."""
    r = edit(client, ws, "main", op="alter_column", path="users.nickname",
             name="nickname", type="varchar(64)", default="", nullable_field="1",
             nullable="on")

    assert "nothing to change" in error(r.text)


def test_W45_the_message_names_each_change_that_was_applied(client, ws):
    r = edit(client, ws, "main", op="alter_column", path="users.nickname",
             type="text", name="nickname", nullable_field="1", nullable="on")

    assert notice(r.text) == "users.nickname: type to text"


def test_W46_an_omitted_nullable_field_does_not_silently_add_not_null(client, ws):
    """An unchecked checkbox sends nothing, which is indistinguishable from a caller
    that never offered the field. Without the hidden marker, every such call would
    quietly make the column NOT NULL."""
    assert workspaces.open_repo(ws).snapshot("main").col("users.nickname").nullable

    edit(client, ws, "main", op="alter_column", path="users.nickname", type="text")

    assert workspaces.open_repo(ws).snapshot("main").col("users.nickname").nullable


def test_W47_the_editor_groups_operations_instead_of_stacking_them(client, ws):
    """Reported from use: the options were 'way too scattered'. Nine stacked forms per
    table became four labelled groups, one visible at a time -- and with no inline
    script, since the CSP forbids it."""
    html = client.get(f"/w/{ws}/branch/main").text

    for label in ("Column", "Index", "Constraint", "Table"):
        assert f'class="tab" for=' in html and f">{label}</label>" in html, label
    assert html.count('class="tabpane') >= 4
    assert "onclick" not in html and "<script>" not in html


# ------------------------------------------------ the type picker (D50)
def test_W48_a_size_is_attached_to_the_chosen_type(client, ws):
    edit(client, ws, "main", op="add_column", table="users", name="phone",
         type_base="varchar", type_params="32")

    assert workspaces.open_repo(ws).snapshot("main").col("users.phone").type.render() \
        == "varchar(32)"


def test_W49_a_two_part_size_works(client, ws):
    edit(client, ws, "main", op="add_column", table="users", name="score",
         type_base="decimal", type_params="10,2")

    assert workspaces.open_repo(ws).snapshot("main").col("users.score").type.render() \
        == "decimal(10,2)"


def test_W50_a_size_on_a_type_that_has_none_is_refused_not_dropped(client, ws):
    """Silently discarding it would be worse than refusing: the user asked for something
    specific and would get something else without being told."""
    r = edit(client, ws, "main", op="add_column", table="users", name="n",
             type_base="int", type_params="5")

    assert "does not take a size" in error(r.text)
    assert workspaces.open_repo(ws).snapshot("main").col("users.n") is None


@pytest.mark.parametrize("params,expected", [
    ("abc", "has to be a number"),
    ("1,2,3", "takes 2 number(s)"),
])
def test_W51_a_malformed_size_says_what_is_wrong_with_it(client, ws, params, expected):
    r = edit(client, ws, "main", op="add_column", table="users", name="n",
             type_base="decimal", type_params=params)

    assert expected in error(r.text)


def test_W52_the_picker_cannot_be_used_to_smuggle_an_unknown_type(client, ws):
    """A <select> constrains a browser, not an HTTP client. The server still checks."""
    r = edit(client, ws, "main", op="add_column", table="users", name="n",
             type_base="somethign", type_params="")

    assert "no type called 'somethign'" in error(r.text)


def test_W53_editing_a_column_without_touching_its_type_leaves_it_alone(client, ws):
    """The picker posts a base and a size on every submit, so the handler has to
    reassemble them into the same string it started with -- otherwise every edit to a
    column's name would also 'retype' it to an identical type and log a false change."""
    r = edit(client, ws, "main", op="alter_column", path="users.email",
             name="contact", type_base="varchar", type_params="255",
             nullable_field="1")

    assert notice(r.text) == "users.email: rename to contact"
    assert workspaces.open_repo(ws).snapshot("main").col("users.contact").type.render() \
        == "varchar(255)"


def test_W54_the_health_check_is_cheap_and_has_no_side_effects(client, data_dir):
    """The reason /healthz exists rather than pointing the platform at "/".

    "/" mints a workspace and sets a cookie on every request, so using it as a health
    check would write a SQLite file per probe -- on a free instance with no persistent
    disk, that is a slow disk-fill driven entirely by the platform's own monitoring.
    """
    before = sorted(data_dir.glob("*.db")) if data_dir.exists() else []

    for _ in range(5):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.text == "ok"

    after = sorted(data_dir.glob("*.db")) if data_dir.exists() else []
    assert after == before
    assert "schemavcs_ws" not in client.cookies


def test_W55_the_health_check_says_nothing_about_the_process(client):
    """It is unauthenticated, so version, paths and environment stay out of it."""
    body = client.get("/healthz").text

    assert body == "ok"
