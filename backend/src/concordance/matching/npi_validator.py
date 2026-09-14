"""NPI validation.

An NPI is ten digits whose last digit is a Luhn check digit computed over the
number prefixed with ``80840`` - the ISO 7812 issuer identifier the NPPES
assigns to health-care providers. Validating it properly is what lets the
engine tell "this NPI disagrees" (strong evidence against a match) from "this
NPI was never real" (no evidence either way), which is the distinction the
whole deterministic path turns on.

Stage 1 needs the generator half; Stage 2 builds the comparison levels on top.
"""

from __future__ import annotations

import re

NPI_PREFIX = "80840"
NPI_RE = re.compile(r"^\d{10}$")

# Values seen in real extracts that are syntactically fine and semantically
# meaningless. Treated as missing, never as disagreement.
SENTINEL_NPIS = frozenset(
    {
        "0000000000",
        "1111111111",
        "1234567890",
        "9999999999",
        "0123456789",
    }
)

PLACEHOLDER_TEXT = frozenset(
    {"", "-", "--", "n/a", "na", "none", "null", "unknown", "unk", "tbd", "pending", "not available"}
)


def luhn_check_digit(payload: str) -> int:
    """Check digit for ``payload`` (digits only), doubling from the right."""
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - (total % 10)) % 10


def npi_check_digit(first_nine: str) -> int:
    """Check digit for the first nine digits of an NPI."""
    if len(first_nine) != 9 or not first_nine.isdigit():
        raise ValueError(f"expected nine digits, got {first_nine!r}")
    return luhn_check_digit(NPI_PREFIX + first_nine)


def make_npi(first_nine: str) -> str:
    """Complete a nine-digit body into a checksum-valid NPI."""
    return first_nine + str(npi_check_digit(first_nine))


def is_placeholder(value: str | None) -> bool:
    """True for blank, textual placeholders and known sentinel numbers."""
    if value is None:
        return True
    v = value.strip().lower()
    if v in PLACEHOLDER_TEXT:
        return True
    return v in SENTINEL_NPIS


def is_valid_npi(value: str | None) -> bool:
    """True only for a ten-digit, non-sentinel, checksum-correct NPI."""
    if value is None:
        return False
    v = value.strip()
    if not NPI_RE.match(v) or v in SENTINEL_NPIS:
        return False
    return npi_check_digit(v[:9]) == int(v[9])
