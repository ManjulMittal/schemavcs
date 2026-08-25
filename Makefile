# Public PyPI is pinned explicitly: a private extra-index-url in a developer's
# global pip.conf will otherwise hijack resolution and prompt for credentials.
PIP := PIP_CONFIG_FILE=/dev/null .venv/bin/pip install --index-url https://pypi.org/simple

.PHONY: dev install test test-unit test-engine serve demo cover spike clean

dev:
	python3 -m venv .venv
	$(PIP) -q --upgrade pip
	$(PIP) -q -e ".[dev]"
	@echo "ready: make test"

# Same as `dev` but assumes the interpreter is already the one to use (CI).
install:
	python3 -m venv .venv
	$(PIP) -q --upgrade pip
	$(PIP) -q -e ".[dev,engines]"

# The default: everything that needs no database. Runs in ~2 seconds, which is the
# only reason a suite this size actually gets run on every save.
test:
	.venv/bin/pytest tests/unit tests/web -m "not engine"

test-unit: test

# Generated DDL applied to real engines. Skips with a message naming the missing
# environment variable rather than failing, so a fresh clone still runs `make test`.
test-engine:
	.venv/bin/pytest tests/engine

# A narrated end-to-end walkthrough -- the way to see the engine work without a UI.
# Set SCHEMAVCS_PG_URL to also apply the generated migration to a real server.
demo:
	.venv/bin/python demo.py

# The web app. SCHEMAVCS_DATA decides where workspaces live; the default is a temp
# directory, which is fine locally and wrong in production -- see the Dockerfile.
serve:
	.venv/bin/uvicorn schemavcs.web.app:app --reload --port 8000

cover:
	.venv/bin/pytest tests/unit tests/web -m "not engine" --cov=schemavcs --cov-report=term-missing

spike:
	.venv/bin/python spike/sqlglot_spike.py

clean:
	rm -rf .venv .pytest_cache **/__pycache__ *.egg-info src/*.egg-info
