"""Query parameters every list endpoint shares.

The bounds match `db.repositories.base`: a page is at most 500 rows, so a client
cannot ask the API for a whole table by passing `limit=1000000`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Query

from concordance.api import schemas
from concordance.db.repositories.base import MAX_PAGE_SIZE

Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="Page size.")]
Offset = Annotated[int, Query(ge=0, description="Rows to skip.")]


def page_of[T](model: type[T], page: Any, items: list[T] | None = None) -> schemas.Page[T]:
    """Wrap a repository page in the API's envelope, validating each row."""
    rows = items if items is not None else [model.model_validate(r) for r in page.items]  # type: ignore[attr-defined]
    return schemas.Page[model](  # type: ignore[valid-type]
        items=rows, total=page.total, limit=page.limit, offset=page.offset
    )


__all__ = ["Limit", "Offset", "page_of"]
