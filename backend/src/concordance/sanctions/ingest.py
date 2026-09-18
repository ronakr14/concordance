"""Turning mapped rows into sanction records, and deciding which rows to refuse.

Two outcomes for a row that is not clean, and the line between them is whether
the problem makes the record *wrong* or merely *less complete*.

**Rejected** - the row is not a record: it has no name, a formula where data
belongs, more cells than the header, a number where a name belongs, a value too
long for its column, or a name with nothing else to match it by. Rejecting it
does not reject the file; the other rows are ingested and the rejections are
reported by row number.

**Accepted with a warning** - the row is a record with a bad value in it: an
exclusion date that will not parse, a state that is not two letters. The value
is dropped and the row kept, because silently losing a sanction over a date
typo is the worse failure for a compliance system - a missed exclusion is the
thing this whole application exists to prevent.

**Record keys.** When the mapping names a key column, that is the key. When it
does not, the key is derived from the row's identifying content, so the same
person on the same list keeps the same key from one month's file to the next -
which is what lets an unchanged row be recognised as unchanged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from concordance.sanctions.canonical import (
    BY_NAME,
    CORROBORATING_FIELDS,
    NAME_FIELDS,
    Kind,
)
from concordance.sanctions.workbook import Sheet, cell_text

#: Prefix on derived keys, so a reviewer can tell at a glance that nobody
#: assigned this key - the system did.
DERIVED_KEY_PREFIX = "K-"

#: Date layouts seen on exclusion lists, tried in order. Month-first before
#: day-first because every list this system targets is American.
DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y%m%d",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%Y/%m/%d",
)

#: What a spreadsheet-formula cell starts with. `-` and `+` are excluded: they
#: begin real surnames and addresses often enough to be refused on their own.
_FORMULA_PREFIXES = ("=", "@")


@dataclass
class RowIssue:
    row: int
    reason: str
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"row": self.row, "reason": self.reason}
        if self.field:
            out["field"] = self.field
        return out


@dataclass
class ParsedRow:
    """One accepted row, in canonical fields, with the original kept whole."""

    row: int
    values: dict[str, Any]
    raw: dict[str, Any]
    key_derived: bool = False

    @property
    def record_id(self) -> str:
        return str(self.values["record_id"])


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    rejected: list[RowIssue] = field(default_factory=list)
    warnings: list[RowIssue] = field(default_factory=list)

    def summary(self, sample: int = 50) -> dict[str, Any]:
        return {
            "accepted": len(self.rows),
            "rejected": len(self.rejected),
            "warnings": len(self.warnings),
            "rejected_rows": [r.as_dict() for r in self.rejected[:sample]],
            "warning_rows": [w.as_dict() for w in self.warnings[:sample]],
        }


class _RejectedRowError(Exception):
    def __init__(self, reason: str, field_name: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field_name


def parse_rows(sheet: Sheet, mapping: dict[str, str], *, source_authority: str) -> ParseResult:
    """Apply a validated mapping to every data row."""
    column = {name: sheet.headers.index(header) for name, header in mapping.items()}
    result = ParseResult()
    keys_seen: dict[str, int] = {}

    for number, cells in sheet.rows:
        try:
            parsed = _parse_row(number, cells, sheet, column, source_authority, result)
        except _RejectedRowError as exc:
            result.rejected.append(RowIssue(number, exc.reason, exc.field))
            continue

        first = keys_seen.get(parsed.record_id)
        if first is not None:
            reason = (
                f"exact duplicate of row {first}"
                if parsed.key_derived
                else f"record key {parsed.record_id!r} already used by row {first}"
            )
            result.rejected.append(RowIssue(number, reason, "record_id"))
            continue
        keys_seen[parsed.record_id] = number
        result.rows.append(parsed)
    return result


def _parse_row(
    number: int,
    cells: tuple[Any, ...],
    sheet: Sheet,
    column: dict[str, int],
    source_authority: str,
    result: ParseResult,
) -> ParsedRow:
    overflow = [c for c in cells[sheet.width :] if c is not None]
    if overflow:
        raise _RejectedRowError(f"row has {len(overflow)} cell(s) beyond the last header column")

    for cell in cells:
        if isinstance(cell, str) and cell.startswith(_FORMULA_PREFIXES):
            # Stored and later exported, a formula runs in whoever opens the
            # export. It is never a name or an identifier.
            raise _RejectedRowError("a cell holds a spreadsheet formula, not data")

    raw = {
        header: cell_text(cells[i]) if i < len(cells) else None
        for i, header in enumerate(sheet.headers)
    }
    values: dict[str, Any] = {}
    for name, index in column.items():
        cell = cells[index] if index < len(cells) else None
        value = _convert(name, cell, number, result)
        if value is not None:
            values[name] = value

    if not any(values.get(n) for n in NAME_FIELDS):
        raise _RejectedRowError("row has no last name or organization name", "last_name")
    if not any(values.get(n) for n in CORROBORATING_FIELDS):
        # A truncated row: a name and nothing to match it against. Matching on
        # a name alone is how two different people become one sanction.
        raise _RejectedRowError(
            "row has a name but no identifier, date, license or address to match it by"
        )

    values["source_authority"] = source_authority
    values["is_organization"] = bool(values.get("organization_name")) and not (
        values.get("last_name") or values.get("first_name")
    )
    derived = not values.get("record_id")
    if derived:
        values["record_id"] = derive_record_key(source_authority, values)
    return ParsedRow(row=number, values=values, raw=raw, key_derived=derived)


def _convert(name: str, cell: Any, row: int, result: ParseResult) -> Any:
    spec = BY_NAME[name]
    if cell is None:
        return None

    if spec.kind is Kind.NAME and isinstance(cell, (int, float)) and not isinstance(cell, bool):
        raise _RejectedRowError(f"{name} is a number, not a name", name)

    if spec.kind is Kind.DATE:
        parsed = parse_date(cell)
        if parsed is None and not _is_null_date(cell):
            result.warnings.append(RowIssue(row, f"{name} {cell!r} is not a date; left blank", name))
        return parsed

    if spec.kind is Kind.DOB:
        # Kept as written. A partial or malformed date of birth is evidence the
        # normalizer weighs, and a DATE column cannot hold `08-24-57`.
        text = cell.isoformat() if isinstance(cell, (date, datetime)) else cell_text(cell)
        return _fits(name, text, spec.max_length)

    if spec.kind is Kind.NPI:
        text = str(int(cell)) if isinstance(cell, (int, float)) and not isinstance(cell, bool) else cell_text(cell)
        return _fits(name, text, spec.max_length)

    if spec.kind is Kind.ZIP:
        if isinstance(cell, (int, float)) and not isinstance(cell, bool):
            # A ZIP typed as a number has lost its leading zero: 02134 -> 2134.
            digits = str(int(cell))
            text = digits.zfill(5) if len(digits) <= 5 else digits.zfill(9)
        else:
            text = cell_text(cell)
        return _fits(name, text, spec.max_length)

    text = cell_text(cell)
    if spec.kind is Kind.STATE:
        state = (text or "").strip().upper()
        if len(state) != 2 or not state.isalpha():
            result.warnings.append(
                RowIssue(row, f"{name} {text!r} is not a two-letter code; left blank", name)
            )
            return None
        return state
    return _fits(name, text, spec.max_length)


def _fits(name: str, text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > limit:
        # Truncating an identifier or a name makes a different record; the
        # row is refused and the uploader told which field.
        raise _RejectedRowError(f"{name} is {len(text)} characters; the limit is {limit}", name)
    return text


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(int(value))
    text = str(value).strip()
    if _is_null_date(text):
        return None
    for layout in DATE_FORMATS:
        try:
            return datetime.strptime(text, layout).date()
        except ValueError:
            continue
    return None


def _is_null_date(value: Any) -> bool:
    """LEIE writes `00000000` for "no reinstatement date". That is empty, not wrong."""
    text = str(value).strip()
    return not text or set(text) <= {"0", "/", "-"}


def derive_record_key(source_authority: str, values: dict[str, Any]) -> str:
    """A key from the fields that identify the sanction, stable across uploads.

    Name, NPI, date of birth, state and exclusion date: what distinguishes one
    exclusion from another on the same list. Address is left out on purpose -
    providers move, and a new address on next month's file is an update to the
    same sanction rather than a second one.
    """
    parts = [
        source_authority.strip().casefold(),
        _fold(values.get("last_name")),
        _fold(values.get("first_name")),
        _fold(values.get("organization_name")),
        _fold(values.get("npi")),
        _fold(values.get("dob")),
        _fold(values.get("state")),
        str(values.get("exclusion_date") or ""),
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{DERIVED_KEY_PREFIX}{digest[:20].upper()}"


def _fold(value: Any) -> str:
    return " ".join(str(value).casefold().split()) if value else ""


__all__ = [
    "DATE_FORMATS",
    "DERIVED_KEY_PREFIX",
    "ParseResult",
    "ParsedRow",
    "RowIssue",
    "derive_record_key",
    "parse_date",
    "parse_rows",
]
