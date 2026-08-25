"""Ingest errors.

Design stance (D21): a malformed or unsupported input is rejected *wholly*, with every
problem reported at once and each carrying a 1-based line number. Silent partial import
is the worst outcome this product can produce -- a quietly dropped CHECK constraint makes
every subsequent diff, merge, and generated migration wrong with no signal to the user.
"""
from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_SUMMARY = (
    "supported: CREATE TABLE / CREATE INDEX, columns with types, NULL/NOT NULL, "
    "DEFAULT, PRIMARY KEY, FOREIGN KEY, UNIQUE, CHECK, and (unique) indexes"
)

#: Shown when the input is a *change script* rather than a schema definition. This is
#: the one rejection that reflects a deliberate product boundary (D41) rather than an
#: unimplemented construct, so it says so -- a user who reads "unsupported" assumes it
#: is coming soon and waits, where the truth is that they should import differently.
CHANGE_SCRIPT_HINT = (
    "this tool is the source of truth for the schema, so it reads schema "
    "DEFINITIONS, not change scripts (D41). Import the current state as CREATE TABLE "
    "statements -- or start the schema here and let the tool generate the migrations "
    "instead"
)


@dataclass(frozen=True)
class Problem:
    line: int
    message: str
    hint: str | None = None

    def render(self) -> str:
        s = f"line {self.line}: {self.message}"
        return f"{s}\n    {self.hint}" if self.hint else s


class DDLError(Exception):
    """Raised when input cannot be imported. Carries every problem found."""

    def __init__(self, problems: list[Problem]):
        self.problems = problems
        body = "\n".join(p.render() for p in problems)
        super().__init__(f"cannot import DDL ({len(problems)} problem(s)):\n{body}")
