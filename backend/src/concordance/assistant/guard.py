"""The guard: what the model wrote, parsed and refused before anything runs it.

A model that writes SQL is a model that can be talked into writing any SQL, so
nothing here trusts the text. It is parsed with `sqlglot`, inspected as a tree,
and **regenerated from that tree** - the string that reaches the database is
the one this module printed, never the one the model produced. Anything the
parser did not fully understand cannot ride along in a comment or a trailing
clause, because it is not in the tree that gets printed.

The rules, each of which refuses rather than sanitizes:

1. One statement. Not "the first statement" - a second one is a refusal.
2. A `SELECT` (or a set operation of them, or a `WITH` ending in one). No
   `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP`, `ALTER`, `GRANT`, `COPY`,
   `CALL`, `SET`, transaction control, or anything sqlglot parsed as an opaque
   command.
3. No comments and no statement separators in the text at all, checked before
   parsing: `--`, `/*`, `*/` and every `;` but a single trailing one.
4. Every table named is one of the whitelisted views, or a CTE defined in the
   same query. `pg_catalog`, `information_schema` and any schema qualifier are
   refused outright.
5. No function from the denylist - the file, sleep, dblink and settings family
   - and no `pg_*` function at all.
6. No `SELECT ... INTO`, no locking clause, no placeholder or bind parameter.
7. A `LIMIT` is enforced: missing, it is added; larger than the cap, it is
   lowered.

The read-only role the query then runs as has been granted `SELECT` on exactly
those views and nothing else, so every rule here has a second enforcement in
the database. That is the point: a parser bug should cost a refusal, not a
table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from concordance.assistant.views import ALLOWED_VIEWS

DIALECT = "postgres"
#: Rows a question may return without asking. The page paginates nothing;
#: a bigger answer is a worse answer.
DEFAULT_LIMIT = 200
MAX_LIMIT = 1_000
#: Longer than any honest analyst question turns into.
MAX_SQL_CHARS = 4_000

#: Node types that are not a read. Checked by class, so a new spelling of
#: `DELETE` in a future sqlglot is still a `exp.Delete`.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    node
    for node in (
        getattr(exp, name, None)
        for name in (
            "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
            "AlterTable", "TruncateTable", "Grant", "Revoke", "Copy", "Command",
            "Set", "SetItem", "Transaction", "Commit", "Rollback", "Use",
            "Into", "Lock", "LockingProperty", "Placeholder", "Parameter", "Call", "Execute",
            "Analyze", "Attach", "Detach", "Refresh", "Export", "Put", "Kill",
        )
    )
    if isinstance(node, type) and issubclass(node, exp.Expression)
)

#: Functions that read files, sleep, reach the network or touch settings.
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "pg_sleep", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
        "lo_import", "lo_export", "dblink", "dblink_exec", "dblink_connect",
        "set_config", "current_setting", "pg_terminate_backend", "pg_cancel_backend",
        "pg_reload_conf", "query_to_xml", "query_to_xml_and_xmlschema", "xmlserialize",
        "pg_read_server_files", "pg_write_server_files", "pg_execute_server_program",
        "current_user", "session_user", "system_user", "version", "current_version",
        "current_database", "current_schema", "current_catalog", "inet_server_addr",
        "inet_client_addr", "pg_backend_pid", "txid_current", "pg_current_logfile",
    }
)

_COMMENT = re.compile(r"--|/\*|\*/")


class SqlRejectedError(ValueError):
    """The generated SQL broke a rule. `reason` is shown to the person who asked."""

    def __init__(self, reason: str, *, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True, slots=True)
class SafeQuery:
    """SQL that passed every rule, as this module regenerated it."""

    sql: str
    tables: frozenset[str]
    limit: int
    notes: tuple[str, ...] = field(default=())


def check(
    sql: str,
    *,
    allowed: frozenset[str] = ALLOWED_VIEWS,
    default_limit: int = DEFAULT_LIMIT,
    max_limit: int = MAX_LIMIT,
) -> SafeQuery:
    """Parse, refuse or regenerate. Raises `SqlRejectedError` with a readable reason."""
    text = (sql or "").strip()
    if not text:
        raise SqlRejectedError("the model returned no SQL", code="empty")
    if len(text) > MAX_SQL_CHARS:
        raise SqlRejectedError(
            f"the SQL is {len(text)} characters; the limit is {MAX_SQL_CHARS}", code="too_long"
        )
    if _COMMENT.search(text):
        raise SqlRejectedError(
            "SQL comments are not allowed: they are how a second instruction hides",
            code="comment",
        )
    if ";" in text.rstrip().rstrip(";"):
        raise SqlRejectedError("only one statement may be run, and it may not contain ';'", code="multiple")

    try:
        statements = [s for s in sqlglot.parse(text, read=DIALECT) if s is not None]
    except Exception as exc:  # sqlglot raises several types; all mean the same here
        raise SqlRejectedError(f"the SQL did not parse: {exc}", code="unparsable") from exc
    if len(statements) != 1:
        raise SqlRejectedError(
            f"{len(statements)} statements were generated; exactly one is allowed", code="multiple"
        )
    tree = statements[0]

    if not isinstance(tree, exp.Select | exp.SetOperation | exp.Subquery):
        raise SqlRejectedError(
            f"only SELECT is allowed here, and this is {type(tree).__name__.upper()}",
            code="not_a_select",
        )

    cte_names = {
        cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE) if cte.alias_or_name
    }
    tables: set[str] = set()
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SqlRejectedError(
                f"{type(node).__name__.upper()} is not allowed: the assistant may only read",
                code="forbidden_statement",
            )
        if isinstance(node, exp.Table):
            tables.add(_table_name(node, allowed, cte_names))
        if isinstance(node, exp.Func):
            _check_function(node)

    named = {t for t in tables if t not in cte_names}
    if not named:
        raise SqlRejectedError(
            "the query reads no whitelisted view; the assistant can only answer from "
            f"{', '.join(sorted(allowed))}",
            code="no_table",
        )

    notes: list[str] = []
    limit = _limit_of(tree)
    if limit is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(default_limit)))
        limit = default_limit
        notes.append(f"a LIMIT of {default_limit} was added")
    elif limit > max_limit:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(max_limit)))
        notes.append(f"the LIMIT was lowered from {limit} to {max_limit}")
        limit = max_limit

    return SafeQuery(
        sql=tree.sql(dialect=DIALECT),
        tables=frozenset(named),
        limit=limit,
        notes=tuple(notes),
    )


def _table_name(node: exp.Table, allowed: frozenset[str], cte_names: set[str]) -> str:
    name = (node.name or "").lower()
    schema = (node.db or "").lower()
    if schema and schema != "public":
        raise SqlRejectedError(
            f"{schema}.{name} is out of bounds; the assistant reads only "
            f"{', '.join(sorted(allowed))}",
            code="forbidden_table",
        )
    if name not in allowed and name not in cte_names:
        raise SqlRejectedError(
            f"{name or 'that table'} is not one of the views the assistant may read "
            f"({', '.join(sorted(allowed))})",
            code="forbidden_table",
        )
    return name


def _check_function(node: exp.Func) -> None:
    name = (node.sql_name() or "").lower()
    if isinstance(node, exp.Anonymous):
        name = str(node.this or "").lower()
    if name in _FORBIDDEN_FUNCTIONS or name.startswith(("pg_", "dblink")):
        raise SqlRejectedError(f"the function {name}() is not allowed here", code="forbidden_function")


def _limit_of(tree: exp.Expression) -> int | None:
    limit = tree.args.get("limit")
    if limit is None:
        return None
    value = limit.expression if isinstance(limit, exp.Limit) else limit
    try:
        return int(value.name)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SqlRejectedError("the LIMIT must be a plain number", code="bad_limit") from exc


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "SafeQuery", "SqlRejectedError", "check"]
