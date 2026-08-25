"""Statement splitting with accurate line tracking.

Naive `sql.split(";")` breaks on semicolons inside string literals and comments, and
loses the line numbers that P-58 requires. sqlglot's tokenizer already knows where
statement boundaries and literals are, so we split on its output instead of the raw text.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot.tokens import TokenType

from .errors import DDLError, Problem


@dataclass(frozen=True)
class Statement:
    sql: str
    line: int  # 1-based line in the original input where this statement starts


def split_statements(sql: str, dialect: str) -> list[Statement]:
    """Split into statements, each tagged with its starting line in the original input."""
    if not sql.strip():
        return []
    try:
        tokens = sqlglot.tokenize(sql, dialect=dialect)
    except Exception as e:  # tokenizer failures are still user-facing input errors
        raise DDLError([Problem(line=_guess_line(e), message=str(e).splitlines()[0])]) from e

    out: list[Statement] = []
    current: list = []
    for tok in tokens:
        if tok.token_type == TokenType.SEMICOLON:
            if current:
                out.append(_build(sql, current))
                current = []
        else:
            current.append(tok)
    if current:
        out.append(_build(sql, current))
    return out


def _build(sql: str, tokens: list) -> Statement:
    start, end = tokens[0].start, tokens[-1].end
    return Statement(sql=sql[start : end + 1], line=tokens[0].line)


def _guess_line(e: Exception) -> int:
    import re
    m = re.search(r"[Ll]ine (\d+)", str(e))
    return int(m.group(1)) if m else 1
