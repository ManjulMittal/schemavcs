"""Durable implementations of the engine's `Store` contract.

This package exists because of the architecture test. `engine` may not mention a
specific database -- and that rule is enforced by a regex over the source, which
cannot tell the difference between "the engine branches on SQLite dialect quirks"
(a design failure) and "the engine's datastore happens to be SQLite" (fine, D10).

Rather than weaken the rule with an exemption, the datastore moved out. The engine
defines the contract; picking a database is somebody else's job. That is the correct
dependency direction anyway: swapping SQLite for anything else touches only this
package, and the parametrized persistence suite proves it.
"""
from .sqlite_store import SqliteStore

__all__ = ["SqliteStore"]
