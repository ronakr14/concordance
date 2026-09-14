"""The corruption engine.

One dial, `corruption_level` in [0.0, 0.9], scales every family at once; each
family also has its own rate and its own random stream, so a family can be
turned off or retuned without disturbing the others.

Corruption is applied to **both** sides. The source spec notes that roughly 90%
of real sanction records carry incorrect or missing identifiers, but a provider
master is not clean either - it has stale addresses, missing DOBs and
transposed licence numbers of its own. Corrupting only the sanction side would
produce an engine that is optimistic in exactly the way production punishes.

Two corruptions are sanction-side only, because a typed database column cannot
hold them: an alternate DOB *format*, and free-text placeholders in the NPI
field. Those arrive with a spreadsheet, not with a master file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from concordance.synth.dates import shift_years, with_day
from concordance.synth.reference import Reference, load_reference
from concordance.synth.rng import choice, cumulative, stream

Side = str  # "provider" | "sanction"
Record = dict[str, Any]
Change = dict[str, Any]
Profile = list[dict[str, Any]]

# Rate of each family at corruption_level = 1.0. The dial multiplies these.
FAMILY_RATES: dict[str, float] = {
    "name": 0.75,
    "npi": 0.85,
    "dob": 0.55,
    "address": 0.70,
    "license": 0.45,
}

# The provider master is dirty, but far less dirty than an inbound file.
PROVIDER_SIDE_SCALE = 0.30

KEYBOARD_NEIGHBOURS = {
    "a": "qwsz", "b": "vghn", "c": "xdfv", "d": "serfcx", "e": "wsdr",
    "f": "drtgvc", "g": "ftyhbv", "h": "gyujnb", "i": "ujko", "j": "huikmn",
    "k": "jiolm", "l": "kop", "m": "njk", "n": "bhjm", "o": "iklp",
    "p": "ol", "q": "wa", "r": "edft", "s": "awedxz", "t": "rfgy",
    "u": "yhji", "v": "cfgb", "w": "qase", "x": "zsdc", "y": "tghu",
    "z": "asx",
}

DIACRITICS = {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú", "n": "ñ", "c": "ç"}

USPS_ABBREVIATIONS = {
    "Street": "St", "Avenue": "Ave", "Road": "Rd", "Drive": "Dr", "Lane": "Ln",
    "Boulevard": "Blvd", "Court": "Ct", "Place": "Pl", "Terrace": "Ter",
    "Circle": "Cir", "Parkway": "Pkwy", "North": "N", "South": "S",
    "East": "E", "West": "W", "Suite": "Ste", "Apartment": "Apt",
}

SENTINEL_NPIS = ["0000000000", "9999999999", "1111111111", "1234567890"]
PLACEHOLDERS = ["UNKNOWN", "N/A", "NONE", "TBD", "-", "", "PENDING"]


@dataclass
class Op:
    """One corruption. ``sides`` limits where it can plausibly happen."""

    family: str
    name: str
    fn: Callable[[Record, np.random.Generator, Reference], dict[str, Any] | None]
    weight: float = 1.0
    sides: tuple[str, ...] = ("provider", "sanction")


# --------------------------------------------------------------------------
# name
# --------------------------------------------------------------------------


def _op_token_swap(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    if not (rec.get("first_name") and rec.get("last_name")):
        return None
    return {"first_name": rec["last_name"], "last_name": rec["first_name"]}


def _op_first_initial(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    first = rec.get("first_name")
    if not first or len(first) < 2:
        return None
    return {"first_name": first[0] + ("." if rng.random() < 0.5 else "")}


def _op_nickname(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    first = rec.get("first_name")
    if not first:
        return None
    options = ref.nickname_of.get(first)
    if options:
        return {"first_name": choice(rng, options)}
    # Works in reverse too: a file may carry the formal name for a nickname.
    canonical = ref.canonical_of.get(first)
    return {"first_name": canonical} if canonical else None


def _typo(word: str, rng: np.random.Generator) -> str:
    idx = int(rng.integers(0, len(word)))
    ch = word[idx].lower()
    neighbours = KEYBOARD_NEIGHBOURS.get(ch)
    if not neighbours:
        return word
    replacement = neighbours[int(rng.integers(0, len(neighbours)))]
    if word[idx].isupper():
        replacement = replacement.upper()
    return word[:idx] + replacement + word[idx + 1 :]


def _op_keyboard_typo(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    fields = [f for f in ("first_name", "last_name", "organization_name") if rec.get(f)]
    if not fields:
        return None
    target = choice(rng, fields)
    return {target: _typo(str(rec[target]), rng)}


def _op_diacritics(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    """One side keeps the accent the other side lost."""
    target = "organization_name" if rec.get("is_organization") else "last_name"
    value = rec.get(target)
    if not value:
        return None
    for i, ch in enumerate(value):
        if ch.lower() in DIACRITICS and rng.random() < 0.6:
            return {target: value[:i] + DIACRITICS[ch.lower()] + value[i + 1 :]}
    return None


def _op_credential_suffix(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    if rec.get("is_organization"):
        return None
    if rec.get("suffix"):
        return {"suffix": None}
    creds = ref.all_credentials()
    return {"suffix": choice(rng, creds)}


def _op_married_name(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    if rec.get("is_organization") or not rec.get("last_name"):
        return None
    new = ref.surnames.pick(rng)
    return {"last_name": new} if new != rec["last_name"] else None


def _op_hyphenation(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    last = rec.get("last_name")
    if not last:
        return None
    if "-" in last:
        head, _, tail = last.partition("-")
        return {"last_name": head if rng.random() < 0.5 else f"{head} {tail}"}
    other = ref.surnames.pick(rng)
    return {"last_name": f"{last}-{other}"}


# --------------------------------------------------------------------------
# npi
# --------------------------------------------------------------------------


def _op_npi_missing(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    return {"npi": None} if rec.get("npi") else None


def _op_npi_sentinel(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    return {"npi": choice(rng, SENTINEL_NPIS)}


def _op_npi_placeholder(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    return {"npi": choice(rng, PLACEHOLDERS)}


def _op_npi_checksum_fail(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    npi = rec.get("npi")
    if not npi or len(npi) != 10:
        return None
    last = int(npi[9])
    return {"npi": npi[:9] + str((last + int(rng.integers(1, 10))) % 10)}


def _op_npi_transpose(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    npi = rec.get("npi")
    if not npi or len(npi) != 10:
        return None
    i = int(rng.integers(0, 9))
    if npi[i] == npi[i + 1]:
        return None
    return {"npi": npi[:i] + npi[i + 1] + npi[i] + npi[i + 2 :]}


def _op_npi_wrong_length(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    npi = rec.get("npi")
    if not npi or len(npi) < 10:
        return None
    return {"npi": npi[:9] if rng.random() < 0.5 else npi + str(int(rng.integers(0, 10)))}


# --------------------------------------------------------------------------
# dob
# --------------------------------------------------------------------------


def _as_date(value: Any) -> date | None:
    return value if isinstance(value, date) else None


def _op_dob_missing(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    return {"dob": None} if rec.get("dob") else None


def _op_dob_off_by_one(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    d = _as_date(rec.get("dob"))
    if not d:
        return None
    if rng.random() < 0.5:
        return {"dob": shift_years(d, 1 if rng.random() < 0.5 else -1)}
    return {"dob": with_day(d, d.day + (1 if rng.random() < 0.5 else -1))}


def _op_dob_month_day_swap(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    d = _as_date(rec.get("dob"))
    if not d or d.day > 12 or d.day == d.month:
        return None
    return {"dob": date(d.year, d.day, d.month)}


def _op_dob_wrong_century(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    d = _as_date(rec.get("dob"))
    if not d:
        return None
    return {"dob": shift_years(d, 100 if d.year < 1926 else -100)}


def _op_dob_alt_format(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    """Spreadsheet-only: the date arrives as text in some local convention."""
    d = _as_date(rec.get("dob"))
    if not d:
        return None
    fmt = choice(rng, ["%m/%d/%Y", "%d-%b-%Y", "%Y%m%d", "%m-%d-%y", "%B %d, %Y"])
    return {"dob": d.strftime(fmt)}


# --------------------------------------------------------------------------
# address
# --------------------------------------------------------------------------


def _op_addr_abbreviate(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    line = rec.get("address_line1")
    if not line:
        return None
    words = [USPS_ABBREVIATIONS.get(w, w) for w in str(line).split()]
    new = " ".join(words)
    return {"address_line1": new} if new != line else None


def _op_addr_drop_unit(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    return {"address_line2": None} if rec.get("address_line2") else None


def _op_addr_zip4(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    z = rec.get("zip")
    if not z or len(str(z)) != 5:
        return None
    return {"zip": f"{z}-{int(rng.integers(1000, 9999))}"}


def _op_addr_wrong_zip(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    st = rec.get("state")
    if not st or st not in ref.cities_by_state:
        return None
    options = ref.cities_by_state[st]
    _, zip3 = options[int(rng.integers(0, len(options)))]
    return {"zip": f"{zip3}{int(rng.integers(0, 100)):02d}"}


def _op_addr_po_box(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    return {"address_line1": f"PO Box {int(rng.integers(10, 9999))}", "address_line2": None}


def _op_addr_missing(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    if not rec.get("address_line1"):
        return None
    return {"address_line1": None, "address_line2": None, "city": None, "zip": None}


# --------------------------------------------------------------------------
# license
# --------------------------------------------------------------------------


def _op_lic_missing(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    return {"license_number": None} if rec.get("license_number") else None


def _op_lic_wrong_state(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    current = rec.get("license_state")
    new = ref.states.pick(rng)
    return {"license_state": new} if new != current else None


def _op_lic_format(rec: Record, rng: np.random.Generator, ref: Reference) -> Change | None:
    lic = rec.get("license_number")
    if not lic:
        return None
    variant = str(lic).replace("-", "")
    if rng.random() < 0.5:
        variant = f"{rec.get('license_state', '')}{variant}"
    else:
        variant = variant.lstrip("0") or variant
    return {"license_number": variant} if variant != lic else None


OPS: list[Op] = [
    Op("name", "token_swap", _op_token_swap, 0.6),
    Op("name", "first_initial", _op_first_initial, 1.4),
    Op("name", "nickname", _op_nickname, 1.6),
    Op("name", "keyboard_typo", _op_keyboard_typo, 1.6),
    Op("name", "diacritic_loss", _op_diacritics, 0.8),
    Op("name", "credential_suffix", _op_credential_suffix, 1.5),
    Op("name", "married_name", _op_married_name, 0.7, sides=("sanction",)),
    Op("name", "hyphenation", _op_hyphenation, 0.7),
    Op("npi", "missing", _op_npi_missing, 2.2),
    Op("npi", "sentinel", _op_npi_sentinel, 1.0),
    Op("npi", "placeholder_text", _op_npi_placeholder, 1.0, sides=("sanction",)),
    Op("npi", "checksum_fail", _op_npi_checksum_fail, 1.3),
    Op("npi", "digit_transposition", _op_npi_transpose, 1.3),
    Op("npi", "wrong_length", _op_npi_wrong_length, 0.8),
    Op("dob", "missing", _op_dob_missing, 1.8),
    Op("dob", "off_by_one", _op_dob_off_by_one, 1.5),
    Op("dob", "month_day_swap", _op_dob_month_day_swap, 1.0),
    Op("dob", "wrong_century", _op_dob_wrong_century, 0.6),
    Op("dob", "alt_format", _op_dob_alt_format, 1.2, sides=("sanction",)),
    Op("address", "usps_abbreviation", _op_addr_abbreviate, 2.0),
    Op("address", "unit_dropped", _op_addr_drop_unit, 1.4),
    Op("address", "zip_plus_four", _op_addr_zip4, 1.0),
    Op("address", "wrong_zip", _op_addr_wrong_zip, 0.8),
    Op("address", "po_box", _op_addr_po_box, 0.7),
    Op("address", "missing", _op_addr_missing, 0.6),
    Op("license", "missing", _op_lic_missing, 1.6),
    Op("license", "wrong_state", _op_lic_wrong_state, 0.9),
    Op("license", "format_variation", _op_lic_format, 1.5),
]


def pick_op(rng: np.random.Generator, ops: list[Op], cum: np.ndarray) -> Op:
    """One weighted draw from a family's operations."""
    return ops[int(np.searchsorted(cum, rng.random(), side="right"))]


@dataclass
class CorruptionEngine:
    """Applies corruption families to one record at a time.

    Each family owns a stream keyed by (seed, side, family), so the dial can be
    swept and families toggled without reshuffling the rest of the dataset.
    """

    level: float
    seed: int
    side: Side = "sanction"
    reference: Reference = field(default_factory=load_reference)
    family_rates: dict[str, float] = field(default_factory=lambda: dict(FAMILY_RATES))
    enabled: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.level <= 0.9:
            raise ValueError("corruption_level must lie in [0.0, 0.9]")
        self._streams = {
            fam: stream(self.seed, "corrupt", self.side, fam) for fam in self.family_rates
        }
        self._ops_by_family: dict[str, list[Op]] = {}
        for op in OPS:
            if self.side in op.sides:
                self._ops_by_family.setdefault(op.family, []).append(op)
        # Per-family CDFs, built once. The op choice happens hundreds of
        # thousands of times per run, and rebuilding a probability vector on
        # each call dominated the whole generator.
        self._op_cum = {
            fam: cumulative([op.weight for op in ops]) for fam, ops in self._ops_by_family.items()
        }

    def _scale(self) -> float:
        return self.level * (PROVIDER_SIDE_SCALE if self.side == "provider" else 1.0)

    def families(self) -> list[str]:
        if self.enabled is None:
            return list(self.family_rates)
        return [f for f in self.family_rates if f in self.enabled]

    def apply(self, record: Record) -> tuple[Record, Profile]:
        """Return a corrupted copy of ``record`` and the profile of what changed."""
        out = dict(record)
        profile: Profile = []
        if self.level <= 0.0:
            return out, profile

        for family in self.families():
            rng = self._streams[family]
            rate = min(0.95, self.family_rates[family] * self._scale())
            if rng.random() >= rate:
                continue
            ops = self._ops_by_family.get(family, [])
            if not ops:
                continue
            op = pick_op(rng, ops, self._op_cum[family])
            change = op.fn(out, rng, self.reference)
            if not change:
                continue
            before = {k: out.get(k) for k in change}
            out.update(change)
            profile.append(
                {
                    "family": family,
                    "op": op.name,
                    "side": self.side,
                    "before": {k: _jsonable(v) for k, v in before.items()},
                    "after": {k: _jsonable(v) for k, v in change.items()},
                }
            )
        return out, profile


def _jsonable(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


OP_BY_NAME: dict[str, Op] = {op.name: op for op in OPS}


def op_catalogue() -> list[dict[str, Any]]:
    """Every family and operation, for the scenario catalogue and the docs."""
    return [
        {"family": op.family, "op": op.name, "weight": op.weight, "sides": list(op.sides)}
        for op in OPS
    ]
