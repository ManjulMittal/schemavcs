"""Live-engine fixtures.

Skipped rather than failed when an engine is unreachable, so `make test` works on a
laptop with nothing installed -- but the skip names the missing environment variable,
because a silent skip is indistinguishable from a pass and that is how live suites
quietly stop running.
"""
import pytest

from .harness import (MYSQL_URL_ENV, PG_URL_ENV, MySQLEngine, PostgresEngine,
                      available)

BUILDERS = {"mysql": (MySQLEngine, MYSQL_URL_ENV),
            "postgres": (PostgresEngine, PG_URL_ENV)}


@pytest.fixture(scope="session", params=["postgres", "mysql"])
def engine(request):
    name = request.param
    urls = available()
    if name not in urls:
        pytest.skip(f"no live {name}: set {BUILDERS[name][1]} to run this")
    eng = BUILDERS[name][0](urls[name])
    yield eng
    eng.close()


@pytest.fixture
def db(engine):
    """A clean database per test. Live tests that share state are untraceable."""
    engine.reset()
    return engine
