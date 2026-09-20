"""
query_runner.py — bounded, validated SQL execution for the cockpit agent.

The agent supplies query strings; this module validates and runs them with
row-count enforcement. The caller manages the DuckDB connection lifecycle.
"""

import re
import uuid
from typing import Any

import pandas as pd

MAX_ROWS = 5_000
PREVIEW_ROWS = 50

# Blocklist: DDL, DML, extension loading, and filesystem readers the agent
# should never emit. A "SELECT only" prefix check catches the obvious cases;
# this list catches injection attempts inside subqueries or CTEs.
_BLOCKED = re.compile(
    r"""
    \b(?:
        CREATE | DROP   | INSERT | UPDATE | DELETE | TRUNCATE |
        ALTER  | ATTACH | DETACH | LOAD   | INSTALL |
        COPY   | EXPORT | IMPORT | PRAGMA | VACUUM  |
        CALL\s+(?:dbgen|load|install)
    )\b
    | read_csv\s*\(
    | read_json\s*\(
    | read_text\s*\(
    | glob\s*\(
    | \.\.[/\\]        # path traversal
    | /etc/
    | /proc/
    """,
    re.IGNORECASE | re.VERBOSE,
)


class QueryError(Exception):
    """Raised when a query fails validation."""


def validate_query(query: str) -> None:
    """
    Reject queries that are not read-only SELECT statements.
    Raises QueryError with a descriptive message.
    """
    stripped = query.strip().lstrip(";").strip()
    upper = stripped.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise QueryError(
            "Only SELECT (or WITH … SELECT) statements are permitted."
        )
    m = _BLOCKED.search(stripped)
    if m:
        raise QueryError(
            f"Query contains a disallowed keyword or pattern near '{m.group().strip()}'. "
            "Only read-only analytical queries are allowed."
        )


def run_bounded_query(
    query: str,
    con,
    max_rows: int = MAX_ROWS,
) -> tuple[str, pd.DataFrame, bool, list[dict[str, Any]]]:
    """
    Validate then execute a SQL query with an enforced row cap.

    Fetches max_rows + 1 rows so overflow can be detected without materialising
    the full result. Returns (result_id, df, overflow, columns).

    result_id — UUID string, unique per call, used by the caller's result registry.
    df        — DataFrame capped at max_rows rows.
    overflow  — True if the raw result exceeded max_rows.
    columns   — list of {name, type} dicts describing df's columns.

    Raises QueryError on validation failure, propagates DuckDB exceptions on
    execution failure (caller should catch and report them).
    """
    validate_query(query)

    # Wrap in a subquery so the row cap is always applied regardless of whether
    # the inner query already has ORDER BY or LIMIT clauses.
    bounded = f"SELECT * FROM (\n{query}\n) __cockpit_q LIMIT {max_rows + 1}"
    df: pd.DataFrame = con.execute(bounded).df()

    overflow = len(df) > max_rows
    if overflow:
        df = df.iloc[:max_rows].copy()

    result_id = str(uuid.uuid4())
    columns = [{"name": c, "type": str(df[c].dtype)} for c in df.columns]
    return result_id, df, overflow, columns
