"""The web application: branch, edit, diff, merge, and generate migrations.

Structure worth knowing before reading: **every mutation is a commit**. There is no
"unsaved schema" -- clicking "rename column" writes a commit on that branch, the same way
`git commit` does. That removes a whole category of state the UI would otherwise have to
own (drafts, dirty flags, discard-changes dialogs) and it makes the history panel real
rather than decorative.

The routes are thin on purpose. Anything that decides something -- what a diff means, how
a conflict is resolved, whether a migration is safe -- lives in `engine` and is tested
without a browser. This module's job is HTTP, and its own tests (tests/web) check the
things only HTTP can get wrong: redirects, cookies, form parsing, error rendering, and
that a stale merge token is refused.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..dialects import (EMITTERS, TYPE_PARAMS, UnrepresentableError, emit,
                        known_types)
from ..dialects.errors import DDLError
from ..engine import (MergeStatus, Repo, Resolution, Side, StaleConflictsError,
                      StaleHeadError, diff, merge_branches)
from ..engine.diff import UnalignedSnapshotsError
from ..engine.plan import UnacknowledgedRiskError, plan
from ..model import SchemaError
from . import tour, views, workspaces
from .edits import EDITS, EditError
from .security import SecurityHeaders, unhandled

HERE = Path(__file__).parent
COOKIE = "schemavcs_ws"

app = FastAPI(title="schemavcs")
app.add_middleware(SecurityHeaders)
app.add_exception_handler(Exception, unhandled)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


# --------------------------------------------------------------- helpers
def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("notice", request.query_params.get("notice"))
    ctx.setdefault("error", request.query_params.get("error"))
    # Every page inside a workspace carries the tour, so a reviewer who lands mid-flow
    # (or bookmarks a deep link) still has the narrative and can see where they are.
    if ctx.get("ws"):
        ctx.setdefault("tour", tour.steps(ctx["ws"]))
    return templates.TemplateResponse(request, template, ctx)


def back(ws: str, branch: str, *, notice: str = "", error: str = "",
         table: str = "") -> RedirectResponse:
    """Post/Redirect/Get, landing back where the edit was made.

    The redirect is what stops a reload from committing the same change twice. But a
    plain redirect also returns you to the top of the page, which is wrong: you were
    working on one table, several screens down, and the edit throws you away from it
    (D49). Naming the table keeps its editor open and scrolls to it, so the page comes
    back in the state you left it.
    """
    # The message is interpolated into a URL that becomes a Location header, so it is
    # percent-encoded here rather than relying on the framework to do it. A CR or LF
    # reaching a response header unencoded is header injection, and these messages
    # carry text from exceptions -- some of which are multi-line.
    params = []
    if notice:
        params.append(f"notice={quote(notice, safe='')}")
    elif error:
        params.append(f"error={quote(error, safe='')}")
    if table:
        params.append(f"open={quote(table, safe='')}")
    q = ("?" + "&".join(params)) if params else ""
    # The fragment is what the browser scrolls to; `open` is what re-expands the editor.
    anchor = f"#t-{quote(table, safe='')}" if table else ""
    return RedirectResponse(f"/w/{ws}/branch/{branch}{q}{anchor}", status_code=303)


def repo_or_redirect(ws: str):
    if not workspaces.exists(ws):
        return None
    return workspaces.open_repo(ws)


class Missing(Exception):
    """Workspace gone -- almost always a stale cookie after the data dir was cleared."""


def load(ws: str) -> Repo:
    repo = repo_or_redirect(ws)
    if repo is None:
        raise Missing(ws)
    return repo


@app.exception_handler(Missing)
def _missing(request: Request, exc: Missing):
    r = RedirectResponse("/", status_code=303)
    r.delete_cookie(COOKIE)          # otherwise every later request rediscovers it
    return r


# ------------------------------------------------------------- landing
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    """The front door is a working demo, not a form.

    The reviewer arrives from a link with no context. Asking them to compose a schema
    before anything happens spends their attention on data entry, and the interesting
    behaviour needs two divergent branches to exist -- so the app builds them and lands
    the visitor on the schema with the tour open (D46).
    """
    ws = request.cookies.get(COOKIE)
    if workspaces.exists(ws):
        return RedirectResponse(f"/w/{ws}/branch/main", status_code=303)
    ws = workspaces.create(demo=True)
    r = RedirectResponse(f"/w/{ws}/branch/main?welcome=1", status_code=303)
    _remember(r, ws)
    return r


def _remember(response, ws: str) -> None:
    # No expiry: the workspace is the visitor's only handle on their work, and a session
    # cookie would lose it when the browser closes.
    response.set_cookie(COOKIE, ws, max_age=60 * 60 * 24 * 30, httponly=True,
                        samesite="lax")


@app.get("/start", response_class=HTMLResponse)
def start(request: Request):
    """Bring your own schema. Secondary on purpose -- most visitors want the demo."""
    return render(request, "landing.html", sample=workspaces.SEED_DDL.strip(),
                  dialects=sorted(EMITTERS))


@app.post("/reset")
def reset(request: Request):
    """Start over with a fresh demo. A reviewer who has merged everything needs a way
    back to the beginning that is not 'clear your cookies'."""
    ws = workspaces.create(demo=True)
    r = RedirectResponse(f"/w/{ws}/branch/main?welcome=1", status_code=303)
    _remember(r, ws)
    return r


@app.post("/new")
def new_workspace(request: Request, ddl: str = Form(""), dialect: str = Form("postgres")):
    try:
        ws = workspaces.create(ddl=ddl, dialect=dialect)
    except DDLError as e:
        return render(request, "landing.html", sample=(ddl or workspaces.SEED_DDL).strip(),
                      dialects=sorted(EMITTERS), error=str(e))
    r = RedirectResponse(f"/w/{ws}/branch/main", status_code=303)
    _remember(r, ws)
    return r


# -------------------------------------------------------------- branch
@app.get("/w/{ws}", response_class=HTMLResponse)
def workspace_root(ws: str):
    return RedirectResponse(f"/w/{ws}/branch/main", status_code=303)


@app.get("/w/{ws}/branch/{branch}", response_class=HTMLResponse)
def branch_page(request: Request, ws: str, branch: str):
    repo = load(ws)
    if branch not in repo.branches():
        return RedirectResponse(f"/w/{ws}/branch/main", status_code=303)
    snapshot = repo.snapshot(branch)
    # "How does this branch differ from main?" is the first question a reviewer has on
    # any branch page, and answering it in the header saves them guessing which of the
    # five demo branches they are on.
    drift = None
    if branch != "main":
        drift = len(diff(repo.snapshot("main"), snapshot))
    return render(request, "branch.html", ws=ws, branch=branch, page="branch", drift=drift,
                  welcome=request.query_params.get("welcome"),
                  open_table=request.query_params.get("open"),
                  operations=tour.OPERATIONS, branches=repo.branches(),
                  types=known_types(snapshot.dialect), dialect=snapshot.dialect,
                  type_params=TYPE_PARAMS, tables=views.schema_view(snapshot),
                  history=[{"id": c.id[:8], "message": c.message, "merge": c.is_merge}
                           for c in repo.history(branch)],
                  table_names=[t.name for t in snapshot.tables])


@app.post("/w/{ws}/branch/{branch}/edit")
async def edit(request: Request, ws: str, branch: str):
    repo = load(ws)
    form = dict(await request.form())
    op = str(form.pop("op", ""))
    handler = EDITS.get(op)
    if handler is None:
        return back(ws, branch, error=f"unknown operation: {op}")

    # Which table the user was working on, so the page can come back to it. `add_table`
    # names its table in `name`, and landing on the table just created -- editor open,
    # ready for its first column -- is exactly where you want to be.
    focus = str(form.get("table") or form.get("name") or "").strip()
    if op in ("alter_column", "drop_column", "drop_index", "drop_constraint"):
        focus = str(form.get("path", "")).rsplit(".", 1)[0]
    if op == "drop_table":
        focus = ""

    try:
        snapshot = repo.snapshot(branch)
        editor = snapshot.evolve()
        message = handler(editor, form, snapshot.dialect)
        repo.commit(branch, editor.build(), message=message)
    except (SchemaError, EditError, ValueError) as e:
        # These three carry messages written for the person reading them -- "duplicate
        # column: users.email", "type is required". Anything else is a bug and goes to
        # the generic handler rather than into the page.
        return back(ws, branch, error=str(e), table=focus)
    if op == "rename_table":
        focus = str(form.get("new_name", "")).strip()
    return back(ws, branch, notice=message, table=focus)


@app.post("/w/{ws}/branches")
def create_branch(ws: str, name: str = Form(...), source: str = Form(...)):
    repo = load(ws)
    name = name.strip()
    if not name:
        return back(ws, source, error="a branch needs a name")
    if name in repo.branches():
        return back(ws, source, error=f"branch already exists: {name}")
    repo.branch(name, source)
    return back(ws, name, notice=f"branched {name} from {source}")


# ---------------------------------------------------------------- diff
@app.get("/w/{ws}/compare", response_class=HTMLResponse)
def compare(request: Request, ws: str, base: str = Query("main"), target: str = Query("main")):
    repo = load(ws)
    branches = repo.branches()
    ctx = dict(ws=ws, page="compare", branches=branches, base=base, target=target,
               changes=None)
    if base in branches and target in branches:
        try:
            d = diff(repo.snapshot(base), repo.snapshot(target))
            ctx["changes"] = [views.change_view(c) for c in d.changes]
        except UnalignedSnapshotsError as e:
            ctx["error"] = str(e)
    return render(request, "compare.html", **ctx)


# --------------------------------------------------------------- merge
@app.get("/w/{ws}/merge", response_class=HTMLResponse)
def merge_form(request: Request, ws: str, ours: str = Query("main"), theirs: str = Query("")):
    repo = load(ws)
    return render(request, "merge.html", ws=ws, page="merge", branches=repo.branches(),
                  ours=ours, theirs=theirs, result=None)


@app.post("/w/{ws}/merge", response_class=HTMLResponse)
async def do_merge(request: Request, ws: str):
    repo = load(ws)
    form = dict(await request.form())
    ours, theirs = str(form.get("ours", "")), str(form.get("theirs", ""))
    token = str(form.get("token", "")) or None

    # Resolution inputs arrive as `resolve:<conflict key>` so the key survives the round
    # trip opaquely -- the browser never has to understand what a conflict key means.
    resolutions = {}
    for field, value in form.items():
        if field.startswith("resolve:") and value in ("ours", "theirs"):
            key = field[len("resolve:"):]
            resolutions[key] = Resolution.ours() if value == "ours" else Resolution.theirs()

    ctx = dict(ws=ws, page="merge", branches=repo.branches(), ours=ours, theirs=theirs)
    try:
        outcome = merge_branches(repo, ours=ours, theirs=theirs,
                                 resolutions=resolutions or None, token=token)
    except StaleConflictsError as e:
        return render(request, "merge.html", result=None, **ctx,
                      error=f"{e} — the branches moved while you were deciding. "
                            f"Re-run the merge to see the current conflicts.")
    except KeyError:
        return render(request, "merge.html", result=None, **ctx,
                      error="pick two different branches that both still exist")
    except ValueError as e:
        return render(request, "merge.html", result=None, **ctx, error=str(e))

    return render(request, "merge.html", **ctx, result={
        "status": outcome.status.value,
        "clean": outcome.status in (MergeStatus.MERGED, MergeStatus.FAST_FORWARD,
                                    MergeStatus.UP_TO_DATE),
        "commit": outcome.commit.id[:8] if outcome.commit else None,
        "conflicts": [views.conflict_view(c) for c in outcome.conflicts],
        "violations": [{"invariant": v.invariant.replace("_", " "), "message": v.message}
                       for v in outcome.violations],
        "token": outcome.token,
        "resolved": {k: ("ours" if r.side is Side.OURS else "theirs")
                     for k, r in resolutions.items()},
    })


# ----------------------------------------------------------- migration
@app.get("/w/{ws}/migration", response_class=HTMLResponse)
def migration(request: Request, ws: str, deployed: str = Query("main"),
              target: str = Query("main"), dialect: str = Query("postgres"),
              acknowledge: bool = Query(False)):
    repo = load(ws)
    branches = repo.branches()
    ctx = dict(ws=ws, page="migration", branches=branches, deployed=deployed, target=target,
               dialect=dialect, dialects=sorted(EMITTERS), acknowledge=acknowledge,
               plan=None, sql=None, needs_ack=False)
    if deployed not in branches or target not in branches:
        return render(request, "migration.html", **ctx, error="pick two branches")
    try:
        # The acknowledgement is not a UI formality: the planner itself refuses to
        # return a destructive plan unless told to, so the warning screen below is
        # driven by the engine's own guard rather than by a second check in the view.
        p = plan(repo.snapshot(deployed), repo.snapshot(target),
                 acknowledge_lossy=acknowledge)
    except UnacknowledgedRiskError as e:
        ctx["needs_ack"] = True
        ctx["destructive"] = [o.describe() for o in e.operations]
        return render(request, "migration.html", **ctx)
    except UnalignedSnapshotsError as e:
        return render(request, "migration.html", **ctx, error=str(e))

    ctx["plan"] = [views.operation_view(o) for o in p]
    ctx["destructive"] = [o.describe() for o in p.destructive]
    ctx["worst"] = p.worst_safety.value
    try:
        ctx["sql"] = emit(p, dialect).text()
    except UnrepresentableError as e:
        ctx["error"] = str(e)
    return render(request, "migration.html", **ctx)
