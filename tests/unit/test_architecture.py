"""The load-bearing invariant from docs/scope.md, enforced rather than asserted in prose.

D5: the merge engine never learns what a dialect is. If `if dialect == "mysql"` appears
inside the model or the engine, the design has failed -- and it fails silently, which is
why this is a test and not a code comment.
"""
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "schemavcs"

# Package -> may it mention specific dialects?
NEUTRAL_PACKAGES = ["model", "engine"]

DIALECT_TOKENS = re.compile(r"\b(postgres|postgresql|mysql|mariadb|sqlite)\b", re.I)


def _code_lines(path: pathlib.Path):
    """Yield (lineno, line) for code only -- comments and docstrings are exempt.

    Prose is allowed to explain dialect behaviour; branching on it is not.
    """
    text = path.read_text()
    in_doc, quote = False, ""
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw
        if in_doc:
            if quote in line:
                in_doc = False
                line = line.split(quote, 1)[1]
            else:
                continue
        for q in ('"""', "'''"):
            if q in line:
                before, _, after = line.partition(q)
                if q in after:
                    line = before + after.split(q, 1)[1]
                else:
                    in_doc, quote = True, q
                    line = before
                break
        line = line.split("#", 1)[0]
        if line.strip():
            yield n, line


def test_neutral_packages_never_branch_on_dialect():
    offenders = []
    for pkg in NEUTRAL_PACKAGES:
        for path in (SRC / pkg).rglob("*.py"):
            for n, line in _code_lines(path):
                if DIALECT_TOKENS.search(line):
                    offenders.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert not offenders, (
        "dialect knowledge leaked into a neutral package (D5):\n  "
        + "\n  ".join(offenders)
    )


def test_neutral_packages_do_not_import_dialect_adapters():
    offenders = []
    for pkg in NEUTRAL_PACKAGES:
        for path in (SRC / pkg).rglob("*.py"):
            for n, line in _code_lines(path):
                if re.search(r"^\s*(from|import)\b.*dialects", line):
                    offenders.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert not offenders, "neutral package imported a dialect adapter:\n  " + "\n  ".join(offenders)


def test_neutral_packages_do_not_import_the_web_layer():
    """Same dependency-direction rule as the storage check, one layer up. The web app
    imports the engine; the engine must never import the web app. A violation would
    make the engine untestable without FastAPI installed -- and `make test` running
    without a browser stack is the reason the suite takes two seconds.
    """
    offenders = []
    for pkg in NEUTRAL_PACKAGES:
        for path in (SRC / pkg).rglob("*.py"):
            for n, line in _code_lines(path):
                if re.search(r"^\s*(from|import)\b.*\bweb\b", line):
                    offenders.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert not offenders, (
        "a neutral package imported the web layer, reversing the dependency:\n  "
        + "\n  ".join(offenders))


def test_neutral_packages_do_not_import_the_storage_layer():
    """Dependency direction. `engine` defines the `Store` contract; `storage`
    implements it. If the arrow ever reverses, the engine has acquired an opinion
    about databases -- which is what moving the datastore out was meant to prevent
    (D34), and the dialect-token check above cannot catch it because a module named
    `storage` mentions no database by name.
    """
    offenders = []
    for pkg in NEUTRAL_PACKAGES:
        for path in (SRC / pkg).rglob("*.py"):
            for n, line in _code_lines(path):
                if re.search(r"^\s*(from|import)\b.*\bstorage\b", line):
                    offenders.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert not offenders, (
        "a neutral package imported the storage layer, reversing the dependency:\n  "
        + "\n  ".join(offenders))
