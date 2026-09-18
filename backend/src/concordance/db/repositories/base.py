"""What every repository shares: a session it does not own, and paging.

**Repositories accept a `Session`; they never open one.** A repository that
opens its own connection cannot participate in the caller's transaction, which
means a reconciliation run could write results and fail to write the run row
that explains them. The session is the unit of work and it belongs to whoever
started the work.

**No raw SQL outside this package** (and `assistant/`, which generates read-only
SQL by design and validates it separately). The rule is not aesthetic: it is
what keeps the audit and supersede semantics in one place, where they can be
enforced rather than remembered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

#: A list endpoint that forgets to pass a limit should not be able to ask for
#: the whole table.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results, plus what the caller needs to ask for the next."""

    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "has_more": self.has_more,
            "count": len(self.items),
        }


def clamp_limit(limit: int | None) -> int:
    if not limit or limit < 1:
        return DEFAULT_PAGE_SIZE
    return min(limit, MAX_PAGE_SIZE)


def paginate(
    session: Session, stmt: Select[Any], limit: int | None, offset: int = 0
) -> Page[Any]:
    """Run `stmt` for one page and count the rows it would have returned.

    Two queries rather than a window function: the count is over the same
    filtered statement with its ordering stripped, which Postgres plans better
    than a `count(*) OVER ()` carried through a sorted, limited result.
    """
    size = clamp_limit(limit)
    start = max(0, offset)
    counted = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int(session.scalar(counted) or 0)
    items = list(session.scalars(stmt.limit(size).offset(start)))
    return Page(items=items, total=total, limit=size, offset=start)


def paginate_rows(
    session: Session, stmt: Select[Any], limit: int | None, offset: int = 0
) -> Page[Any]:
    """`paginate` for a statement that selects several entities or columns.

    `paginate` reads the first column of each row, which is right for a single
    entity and drops everything else from a join. This returns the rows whole.
    """
    size = clamp_limit(limit)
    start = max(0, offset)
    counted = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int(session.scalar(counted) or 0)
    items = list(session.execute(stmt.limit(size).offset(start)).all())
    return Page(items=items, total=total, limit=size, offset=start)


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Page",
    "clamp_limit",
    "paginate",
    "paginate_rows",
]
