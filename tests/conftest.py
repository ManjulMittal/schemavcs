"""Shared fixtures.

`base_schema` is a fixture rather than a plain helper function on purpose. Calling a
helper twice in one test returns two independently-built snapshots with no shared
UUIDs, and since diff is identity-based (D1) that silently compares unrelated objects.
I made exactly that mistake in eight tests while building the diff engine.

A fixture is evaluated once per test, so `diff(base_schema, base_schema.evolve()...)`
is correct by construction rather than by remembering. Snapshots are immutable, so
sharing one across a test is safe.
"""
import pytest

from schemavcs.model import schema


@pytest.fixture
def base_schema():
    """A small realistic schema: users with a PK and a NOT NULL email."""
    return (schema()
            .table("users")
              .col("id", "bigint", pk=True)
              .col("email", "varchar(255)", nullable=False)
            .build())


@pytest.fixture
def two_tables():
    """users <- orders, for foreign-key and ordering scenarios."""
    return (schema()
            .table("users")
              .col("id", "bigint", pk=True)
            .table("orders")
              .col("id", "bigint", pk=True)
              .col("user_id", "bigint")
              .fk("fk_orders_user", ["user_id"], "users", ["id"], on_delete="cascade")
            .build())
