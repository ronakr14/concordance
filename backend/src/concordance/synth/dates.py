"""Date arithmetic that survives leap years.

`date.replace(year=...)` raises on 29 February, which is exactly the kind of
one-in-1500 input a 50,000-row generator hits on every run.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date


def shift_years(d: date, years: int) -> date:
    """``d`` moved by ``years``, clamping 29 February to 28 February."""
    year = d.year + years
    day = min(d.day, monthrange(year, d.month)[1])
    return date(year, d.month, day)


def with_day(d: date, day: int) -> date:
    """``d`` with ``day``, clamped into the month."""
    return date(d.year, d.month, min(max(day, 1), monthrange(d.year, d.month)[1]))
