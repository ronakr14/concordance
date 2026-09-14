"""NPI validation and classification.

An NPI is ten digits whose last digit is a Luhn check digit computed over the
number prefixed with ``80840`` - the ISO 7812 issuer identifier the NPPES
assigns to health-care providers. Implementing the real checksum is cheap and
catches invalid identifiers a length check misses.

The classification is the point. `MISSING`, `SENTINEL` and `PLACEHOLDER_TEXT`
carry no information at all; `CHECKSUM_FAIL` and `MALFORMED` say the source
tried to give an identifier and failed; only `VALID` may drive a deterministic
match. Collapsing these into a boolean is what produces confident false
negatives, because "NPI disagrees" is strong evidence *against* a match and a
sentinel would supply it for free.
"""

from __future__ import annotations

import re
from enum import StrEnum

NPI_PREFIX = "80840"
NPI_RE = re.compile(r"^\d{10}$")
_SEPARATORS = re.compile(r"[\s\-._/]")

# Syntactically fine, semantically meaningless. Overridable per deployment.
DEFAULT_SENTINELS = frozenset(
    {
        "0000000000",
        "1111111111",
        "1234567890",
        "9999999999",
        "0123456789",
    }
)

DEFAULT_PLACEHOLDERS = frozenset(
    {
        "",
        "-",
        "--",
        "N/A",
        "NA",
        "N.A.",
        "NONE",
        "NULL",
        "NIL",
        "UNKNOWN",
        "UNK",
        "TBD",
        "PENDING",
        "NOT AVAILABLE",
        "NOT APPLICABLE",
        "NO NPI",
        "XXXXXXXXXX",
    }
)


class NpiStatus(StrEnum):
    """Exactly one of these applies to any NPI field value."""

    VALID = "VALID"
    MISSING = "MISSING"
    SENTINEL = "SENTINEL"
    PLACEHOLDER_TEXT = "PLACEHOLDER_TEXT"
    MALFORMED = "MALFORMED"
    CHECKSUM_FAIL = "CHECKSUM_FAIL"

    @property
    def is_informative(self) -> bool:
        """False where the value tells us nothing about identity."""
        return self in (NpiStatus.VALID, NpiStatus.CHECKSUM_FAIL, NpiStatus.MALFORMED)


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


def clean_npi(value: str | None) -> str:
    """Strip surrounding whitespace and embedded separators, upper-case."""
    if value is None:
        return ""
    return _SEPARATORS.sub("", str(value).strip()).upper()


def classify_npi(
    value: str | None,
    sentinels: frozenset[str] = DEFAULT_SENTINELS,
    placeholders: frozenset[str] = DEFAULT_PLACEHOLDERS,
) -> tuple[NpiStatus, str]:
    """Classify one NPI field value. Returns the status and the cleaned digits."""
    if value is None:
        return NpiStatus.MISSING, ""

    raw = str(value).strip().upper()
    if not raw:
        return NpiStatus.MISSING, ""
    if raw in placeholders or _SEPARATORS.sub(" ", raw).strip() in placeholders:
        return NpiStatus.PLACEHOLDER_TEXT, ""

    cleaned = clean_npi(value)
    if not cleaned:
        return NpiStatus.MISSING, ""
    if cleaned in placeholders:
        return NpiStatus.PLACEHOLDER_TEXT, ""
    if not cleaned.isdigit():
        return NpiStatus.MALFORMED, cleaned
    if cleaned in sentinels:
        return NpiStatus.SENTINEL, cleaned
    if not NPI_RE.match(cleaned):
        return NpiStatus.MALFORMED, cleaned
    if npi_check_digit(cleaned[:9]) != int(cleaned[9]):
        return NpiStatus.CHECKSUM_FAIL, cleaned
    return NpiStatus.VALID, cleaned


def is_valid_npi(value: str | None) -> bool:
    """True only for a ten-digit, non-sentinel, checksum-correct NPI."""
    return classify_npi(value)[0] is NpiStatus.VALID


def is_placeholder(value: str | None) -> bool:
    """True for blank, textual placeholders and known sentinel numbers."""
    status, _ = classify_npi(value)
    return status in (NpiStatus.MISSING, NpiStatus.PLACEHOLDER_TEXT, NpiStatus.SENTINEL)
