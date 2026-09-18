"""Reading an uploaded workbook into a header and rows, and nothing more.

This module decides what the file *says*, not what it *means*: no mapping, no
parsing of dates or identifiers. That split is what lets the inspection phase
run on the same reading the commit phase ingests, so the analyst confirms a
mapping against exactly the columns that will be read.

Three things it refuses, each with a message the uploader can act on: a file
that is not a workbook, a workbook with no header row, and one with more rows
than the configured cap. The cap is checked while reading rather than after, so
a million-row file is refused at row cap+1 instead of after it has been held in
memory whole.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from openpyxl import load_workbook

#: Rows scanned for the header. Real exports put a title or a blank line above
#: it often enough that "row 1" is wrong; more than this is not a header.
HEADER_SCAN_ROWS = 10


class WorkbookError(ValueError):
    """The upload cannot be read as a sanction workbook."""


@dataclass
class Sheet:
    """One worksheet: its header, and its data rows as the file wrote them."""

    title: str
    headers: list[str]
    #: `(row_number, cells)`. The row number is the spreadsheet's own, 1-based,
    #: so an error can say "row 214" and the analyst can find it.
    rows: list[tuple[int, tuple[Any, ...]]] = field(default_factory=list)

    @property
    def width(self) -> int:
        return len(self.headers)


def read_workbook(data: bytes, *, max_rows: int) -> Sheet:
    """The first worksheet with a header row."""
    if not data:
        raise WorkbookError("the file is empty")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        # `.xls`, CSV renamed to `.xlsx`, a PDF: openpyxl's own error for all of
        # these is a zipfile traceback, which tells the uploader nothing.
        raise WorkbookError("the file is not an Excel .xlsx workbook")
    try:
        # `data_only=False`: formulas come back as their text. With `True` a
        # formula reads as its cached result, and a file written by anything
        # other than Excel has none - the formula would vanish rather than be
        # refused.
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except Exception as exc:
        raise WorkbookError(f"the workbook could not be opened: {type(exc).__name__}") from exc

    try:
        for sheet in book.worksheets:
            found = _read_sheet(sheet, max_rows=max_rows)
            if found is not None:
                return found
    finally:
        book.close()
    raise WorkbookError("no worksheet has a header row")


def _read_sheet(sheet: Any, *, max_rows: int) -> Sheet | None:
    rows = sheet.iter_rows(values_only=True)
    headers: list[str] | None = None
    header_row = 0
    for number, cells in enumerate(rows, start=1):
        if number > HEADER_SCAN_ROWS:
            return None
        labels = [_header(c) for c in cells]
        # A header is a row of at least two text labels. A title row above it
        # has one; a data row has numbers and dates in it.
        if sum(1 for label in labels if label) >= 2 and all(
            c is None or isinstance(c, str) for c in cells
        ):
            headers = _trim(labels)
            header_row = number
            break
    if headers is None:
        return None

    out = Sheet(title=str(sheet.title), headers=_dedupe(headers))
    for number, cells in enumerate(rows, start=header_row + 1):
        if all(_blank(c) for c in cells):
            continue
        if len(out.rows) >= max_rows:
            raise WorkbookError(f"the file has more than {max_rows:,} data rows")
        out.rows.append((number, tuple(_cell(c) for c in cells)))
    return out


def _header(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _trim(labels: list[str]) -> list[str]:
    """Drop trailing empty header cells, which read-only mode pads rows with."""
    while labels and not labels[-1]:
        labels.pop()
    return labels


def _dedupe(headers: list[str]) -> list[str]:
    """Blank and repeated headers get a position suffix, so each column has a name.

    A mapping is keyed by header text, and two columns called `Name` cannot both
    be addressed. `Name` and `Name (col 7)` can.
    """
    seen: set[str] = set()
    out: list[str] = []
    for position, header in enumerate(headers, start=1):
        label = header or f"Column {position}"
        if label in seen:
            label = f"{label} (col {position})"
        seen.add(label)
        out.append(label)
    return out


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _cell(value: Any) -> Any:
    """Normalise only what openpyxl itself varies: datetimes at midnight are dates."""
    if isinstance(value, datetime) and value.time() == datetime.min.time():
        return value.date()
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    text = getattr(value, "text", None)
    if isinstance(text, str):
        # An array or data-table formula object. Rendered as the formula it
        # is, so the ingester refuses it like any other.
        return text if text.startswith("=") else f"={text}"
    return value


def cell_text(value: Any) -> str | None:
    """A cell as display text - what inspection samples and `raw` store."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


__all__ = ["HEADER_SCAN_ROWS", "Sheet", "WorkbookError", "cell_text", "read_workbook"]
