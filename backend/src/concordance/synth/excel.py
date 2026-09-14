"""Excel export with deliberately non-canonical headers.

PLAN 11.1 fixed the upload path as generic plus a confirmed column mapping, so
the generator must never emit the canonical field names - otherwise the mapping
UI is exercised for the first time on the day real data arrives. Each source
authority writes its own dialect, and the same authority always writes the same
one, which is what makes "reuse the mapping for this source" testable.

A handful of rows are malformed on purpose. Upload validation at Stage 7 has to
reject them without rejecting the file.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import Workbook

# canonical field -> header, per dialect. Deliberately inconsistent between
# dialects: different names, different order, different casing conventions.
DIALECTS: dict[str, dict[str, str]] = {
    "leie": {
        "last_name": "LASTNAME",
        "first_name": "FIRSTNAME",
        "middle_name": "MIDNAME",
        "suffix": "BUSNAME_SUFFIX",
        "organization_name": "BUSNAME",
        "dob": "DOB",
        "npi": "NPI",
        "address_line1": "ADDRESS",
        "address_line2": "ADDRESS2",
        "city": "CITY",
        "state": "STATE",
        "zip": "ZIP",
        "specialty": "SPECIALTY",
        "license_number": "LICENSE",
        "license_state": "LICENSE_STATE",
        "sanction_type": "EXCLTYPE",
        "exclusion_date": "EXCLDATE",
        "reinstatement_date": "REINDATE",
        "record_id": "RECKEY",
        "ein": "EIN",
    },
    "sam": {
        "last_name": "Individual Last Name",
        "first_name": "Individual First Name",
        "middle_name": "Individual Middle Name",
        "suffix": "Name Suffix",
        "organization_name": "Entity Legal Name",
        "dba_name": "Doing Business As",
        "dob": "Date of Birth",
        "npi": "Provider Identifier",
        "address_line1": "Address Line 1",
        "address_line2": "Address Line 2",
        "city": "City Name",
        "state": "State / Province",
        "zip": "Postal Code",
        "specialty": "Provider Type",
        "license_number": "License #",
        "license_state": "License Issuing State",
        "sanction_type": "Action Type",
        "exclusion_date": "Action Effective Date",
        "reinstatement_date": "Action Termination Date",
        "record_id": "Record Key",
        "ein": "Taxpayer ID",
    },
    "state": {
        "last_name": "prov_last_nm",
        "first_name": "prov_first_nm",
        "middle_name": "prov_mid_init",
        "suffix": "prov_sfx",
        "organization_name": "facility_nm",
        "dob": "birth_dt",
        "npi": "npi_num",
        "address_line1": "svc_addr",
        "address_line2": "svc_addr_2",
        "city": "svc_city",
        "state": "svc_st",
        "zip": "svc_zip",
        "specialty": "prov_type_cd",
        "license_number": "lic_num",
        "license_state": "lic_st",
        "sanction_type": "term_reason",
        "exclusion_date": "term_eff_dt",
        "reinstatement_date": "reinstate_dt",
        "record_id": "case_num",
        "ein": "tax_id",
    },
    "board": {
        "last_name": "Licensee Surname",
        "first_name": "Licensee Given Name",
        "middle_name": "Middle",
        "suffix": "Credentials",
        "organization_name": "Practice Name",
        "dob": "Birthdate",
        "npi": "National Provider ID",
        "address_line1": "Practice Address",
        "address_line2": "Unit",
        "city": "Town",
        "state": "St",
        "zip": "Zip Code",
        "specialty": "Area of Practice",
        "license_number": "License Number",
        "license_state": "Issued By",
        "sanction_type": "Disciplinary Action",
        "exclusion_date": "Effective",
        "reinstatement_date": "Ends",
        "record_id": "Docket",
        "ein": "FEIN",
    },
}


def _cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return "Y" if value else "N"
    if isinstance(value, date):
        return value.isoformat()
    return value


def _malformed_rows(headers: list[str], count: int = 3) -> list[list[Any]]:
    """Rows an importer must reject: short, over-long, and junk-typed."""
    rows: list[list[Any]] = []
    if count >= 1:
        rows.append(["TRUNCATED-ROW", "only two cells"])
    if count >= 2:
        rows.append([f"EXTRA-{i}" for i in range(len(headers) + 3)])
    if count >= 3:
        junk: list[Any] = [None] * len(headers)
        junk[0] = "=SUM(A1:A9)"  # a formula where an id belongs
        if len(junk) > 1:
            junk[1] = 12345  # a number where a name belongs
        rows.append(junk)
    return rows


def write_sanction_workbooks(
    records: list[dict[str, Any]],
    authorities: list[tuple[str, str]],
    out_dir: Path,
) -> list[Path]:
    """One workbook per source authority, each in that authority's dialect."""
    out_dir.mkdir(parents=True, exist_ok=True)
    by_authority: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        by_authority.setdefault(str(rec.get("source_authority")), []).append(rec)

    written: list[Path] = []
    for authority, dialect_name in authorities:
        rows = by_authority.get(authority, [])
        if not rows:
            continue
        mapping = DIALECTS[dialect_name]
        wb = Workbook()
        ws = wb.active
        assert ws is not None  # a new Workbook always has one sheet
        ws.title = "Exclusions"
        fields = list(mapping)
        headers = [mapping[f] for f in fields]
        # The source column the mapping will not claim: preserved in `raw`.
        headers.append("Source Notes")
        ws.append(headers)

        for rec in rows:
            ws.append([_cell(rec.get(f)) for f in fields] + [f"imported from {authority}"])

        for row in _malformed_rows(headers):
            ws.append(row)

        slug = authority.lower().replace(" ", "_").replace("/", "_").replace(".", "")
        path = out_dir / f"sanctions_{slug}.xlsx"
        wb.save(path)
        written.append(path)
    return written
