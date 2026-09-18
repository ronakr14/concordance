"""The upload pipeline without a database: reading, proposing, validating, parsing.

These pin down the rules that decide what becomes a sanction record - which
rows are refused, which values are dropped with a warning, how a key is derived
when the file has none - because a mistake in any of them is silent in the
database and loud in a compliance review.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from concordance.sanctions import mapping as mappings
from concordance.sanctions.ingest import derive_record_key, parse_date, parse_rows
from concordance.sanctions.workbook import WorkbookError, read_workbook
from concordance.synth.excel import DIALECTS, write_sanction_workbooks

pytestmark = pytest.mark.unit


def _xlsx(rows: list[list[Any]]) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


LEIE = DIALECTS["leie"]
HEADERS = [LEIE[f] for f in ("record_id", "last_name", "first_name", "npi", "dob", "state",
                             "zip", "exclusion_date", "reinstatement_date", "sanction_type")]
MAPPING = {f: LEIE[f] for f in ("record_id", "last_name", "first_name", "npi", "dob", "state",
                                "zip", "exclusion_date", "reinstatement_date", "sanction_type")}


def _sheet(*rows: list[Any]) -> Any:
    return read_workbook(_xlsx([HEADERS, *rows]), max_rows=1000)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def test_an_empty_upload_is_refused_with_a_reason() -> None:
    with pytest.raises(WorkbookError, match="empty"):
        read_workbook(b"", max_rows=10)


def test_a_file_that_is_not_xlsx_is_refused_by_name_not_by_traceback() -> None:
    with pytest.raises(WorkbookError, match=r"not an Excel \.xlsx"):
        read_workbook(b"LASTNAME,FIRSTNAME\nSMITH,JOHN\n", max_rows=10)


def test_the_header_is_found_below_a_title_row() -> None:
    sheet = read_workbook(
        _xlsx([["Exclusions as of September"], [], ["LASTNAME", "FIRSTNAME"], ["SMITH", "JOHN"]]),
        max_rows=10,
    )
    assert sheet.headers == ["LASTNAME", "FIRSTNAME"]
    assert sheet.rows == [(4, ("SMITH", "JOHN"))], "row numbers are the spreadsheet's own"


def test_a_workbook_without_a_header_row_is_refused() -> None:
    with pytest.raises(WorkbookError, match="header"):
        read_workbook(_xlsx([[1, 2, 3], [4, 5, 6]]), max_rows=10)


def test_the_row_cap_is_enforced_while_reading() -> None:
    rows = [["LASTNAME", "STATE"]] + [["SMITH", "TX"]] * 6
    with pytest.raises(WorkbookError, match="more than 5"):
        read_workbook(_xlsx(rows), max_rows=5)


def test_repeated_and_blank_headers_get_distinct_names() -> None:
    sheet = read_workbook(_xlsx([["Name", "Name", None, "State"], ["a", "b", "c", "TX"]]), max_rows=5)
    assert sheet.headers == ["Name", "Name (col 2)", "Column 3", "State"]


# --------------------------------------------------------------------------
# proposing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", sorted(DIALECTS))
def test_the_proposal_recovers_each_generator_dialect(dialect: str) -> None:
    """A regression guard, not an accuracy claim.

    The synonym list was written with these dialects in view, so a perfect
    score here says the vocabulary still covers them - not that an unseen
    source would score the same. Real headers are confirmed by the analyst.
    """
    truth = DIALECTS[dialect]
    proposal = mappings.propose([*truth.values(), "Source Notes"])
    wrong = {f: (proposal.mapping.get(f), h) for f, h in truth.items() if proposal.mapping.get(f) != h}
    assert not wrong
    assert "Source Notes" not in proposal.mapping.values()


def test_folding_expands_the_abbreviations_exclusion_lists_use() -> None:
    assert mappings.fold("prov_last_nm") == ("provider", "last", "name")
    assert mappings.fold("ADDRESS2") == ("address", "2")
    assert mappings.fold("License #") == ("license", "number")
    assert mappings.fold("Date of Birth") == ("date", "birth")


def test_a_stored_mapping_is_reused_only_when_every_column_is_present() -> None:
    stored = {"last_name": "LASTNAME", "npi": "NPI"}
    assert mappings.reuse(stored, ["LASTNAME", "NPI", "EXTRA"]) == stored
    assert mappings.reuse(stored, ["LASTNAME"]) is None


# --------------------------------------------------------------------------
# validating
# --------------------------------------------------------------------------


def test_validation_names_each_field_at_fault() -> None:
    errors = mappings.validate(
        {"first_name": "FIRSTNAME", "npi": "NOPE", "colour": "STATE", "state": "FIRSTNAME"},
        ["FIRSTNAME", "STATE"],
    )
    assert errors["mapping.npi"] == "column 'NOPE' is not in this file"
    assert errors["mapping.colour"] == "not a canonical field"
    assert "already mapped to first_name" in errors["mapping.state"]
    # No name mapped at all: both name fields are flagged, so the UI can
    # highlight either.
    assert "mapping.last_name" in errors and "mapping.organization_name" in errors


def test_an_organization_name_alone_satisfies_the_name_rule() -> None:
    assert mappings.validate({"organization_name": "BUSNAME"}, ["BUSNAME"]) == {}


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_a_clean_row_parses_into_canonical_fields() -> None:
    result = parse_rows(
        _sheet(["S1", "Smith", "John", 1234567893, "1970-01-02", "tx", 2134, "01/15/2020",
                "00000000", "1128a1"]),
        MAPPING,
        source_authority="OIG-LEIE",
    )
    assert not result.rejected and not result.warnings
    values = result.rows[0].values
    assert values["npi"] == "1234567893", "a numeric NPI cell becomes its digits"
    assert values["zip"] == "02134", "a ZIP typed as a number gets its leading zero back"
    assert values["state"] == "TX"
    assert values["exclusion_date"] == date(2020, 1, 15)
    assert "reinstatement_date" not in values, "LEIE's 00000000 means none, not a bad date"
    assert values["is_organization"] is False
    assert result.rows[0].raw["LASTNAME"] == "Smith", "the original row is kept"


def test_the_generators_malformed_rows_are_rejected_and_the_rest_accepted(tmp_path: Path) -> None:
    records = [
        {"source_authority": "OIG-LEIE", "record_id": f"S{i}", "last_name": f"Name{i}",
         "first_name": "Pat", "npi": "1234567893", "state": "TX", "dob": "1970-01-01"}
        for i in range(4)
    ]
    [path] = write_sanction_workbooks(records, [("OIG-LEIE", "leie")], tmp_path)
    sheet = read_workbook(path.read_bytes(), max_rows=100)
    mapping = mappings.propose(sheet.headers).mapping

    result = parse_rows(sheet, mapping, source_authority="OIG-LEIE")
    assert len(result.rows) == 4
    reasons = sorted(r.reason for r in result.rejected)
    assert len(reasons) == 3
    assert any("beyond the last header column" in r for r in reasons), "the over-long row"
    assert any("formula" in r for r in reasons), "the junk row"
    assert any("nothing" in r or "no identifier" in r for r in reasons), "the truncated row"


def test_a_number_where_a_name_belongs_rejects_the_row() -> None:
    result = parse_rows(_sheet(["S1", 12345, "John", None, None, "TX"]), MAPPING, source_authority="X")
    assert [r.field for r in result.rejected] == ["last_name"]


def test_a_value_too_long_for_its_column_rejects_rather_than_truncates() -> None:
    result = parse_rows(_sheet(["S1", "x" * 101, "John", None, None, "TX"]), MAPPING, source_authority="X")
    assert "limit is 100" in result.rejected[0].reason


def test_a_bad_date_or_state_is_dropped_with_a_warning_and_the_row_kept() -> None:
    result = parse_rows(
        _sheet(["S1", "Smith", "John", "1234567893", None, "Texas", None, "someday"]),
        MAPPING,
        source_authority="X",
    )
    assert len(result.rows) == 1, "a missed exclusion is worse than a missing date"
    assert {w.field for w in result.warnings} == {"state", "exclusion_date"}


def test_duplicate_keys_in_one_file_reject_the_later_row() -> None:
    result = parse_rows(
        _sheet(["S1", "Smith", "John", None, None, "TX"], ["S1", "Jones", "Ann", None, None, "CA"]),
        MAPPING,
        source_authority="X",
    )
    assert [r.row for r in result.rows] == [2]
    assert result.rejected[0].row == 3 and "already used by row 2" in result.rejected[0].reason


def test_an_organization_is_a_row_with_a_business_name_and_no_person() -> None:
    sheet = read_workbook(_xlsx([["BUSNAME", "LASTNAME", "STATE"], ["Acme Clinic", None, "TX"],
                                 ["Acme Clinic", "Smith", "TX"]]), max_rows=10)
    result = parse_rows(sheet, {"organization_name": "BUSNAME", "last_name": "LASTNAME",
                                "state": "STATE"}, source_authority="X")
    assert [r.values["is_organization"] for r in result.rows] == [True, False]


# --------------------------------------------------------------------------
# derived keys
# --------------------------------------------------------------------------


def test_a_derived_key_is_stable_and_ignores_address() -> None:
    base = {"last_name": "Smith", "first_name": "John", "npi": "1234567893", "state": "TX",
            "exclusion_date": date(2020, 1, 1), "address_line1": "1 Main St"}
    moved = {**base, "address_line1": "99 Elm Ave"}
    assert derive_record_key("OIG-LEIE", base) == derive_record_key("OIG-LEIE", moved)
    assert derive_record_key("OIG-LEIE", base).startswith("K-")
    assert derive_record_key("OIG-LEIE", base) != derive_record_key("SAM.gov", base)
    assert derive_record_key("OIG-LEIE", base) != derive_record_key(
        "OIG-LEIE", {**base, "exclusion_date": date(2021, 1, 1)}
    )


def test_a_file_without_a_key_column_gets_derived_keys_and_dedupes_exact_repeats() -> None:
    mapping = {k: v for k, v in MAPPING.items() if k != "record_id"}
    row = ["ignored", "Smith", "John", "1234567893", None, "TX"]
    result = parse_rows(_sheet(row, row), mapping, source_authority="X")
    assert len(result.rows) == 1 and result.rows[0].key_derived
    assert "exact duplicate of row 2" in result.rejected[0].reason


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2020-01-15", date(2020, 1, 15)), ("01/15/2020", date(2020, 1, 15)),
     ("20200115", date(2020, 1, 15)), ("Jan 15, 2020", date(2020, 1, 15)),
     ("00000000", None), ("not a date", None)],
)
def test_dates_in_the_layouts_exclusion_lists_use(text: str, expected: date | None) -> None:
    assert parse_date(text) == expected
