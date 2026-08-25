"""Fixtures for the HTTP layer.

Each test gets its own data directory, so workspaces cannot leak between tests --
which matters here more than usual, because workspace isolation is itself one of the
things under test.
"""
import pytest
from fastapi.testclient import TestClient

from schemavcs.web import workspaces
from schemavcs.web.app import app


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(workspaces, "DATA_DIR", tmp_path / "ws")
    return tmp_path / "ws"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def ws(client):
    """A workspace seeded with the sample schema, with the cookie already set."""
    client.post("/new", data={"ddl": "", "dialect": "postgres"})
    return client.cookies["schemavcs_ws"]
