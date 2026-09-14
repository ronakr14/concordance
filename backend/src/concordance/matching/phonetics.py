"""Double Metaphone.

Lawrence Philips' algorithm, implemented here rather than pulled from a
package: it is a blocking key, so it sits in the hot path of every run, and the
Postgres implementation at Stage 5 has to agree with it exactly. Owning the
code is what makes that guarantee checkable.

It returns *two* codes. Names that arrived through another language - Nguyen,
Schmidt, Gonzalez - have a defensible English reading and a defensible native
one, and a blocking key that commits to only one of them loses the pair.
Callers should index both and treat a hit on either as a candidate.
"""

from __future__ import annotations

from functools import lru_cache

VOWELS = frozenset("AEIOUY")

# Beginnings whose first letter is silent in English.
SILENT_INITIALS = ("GN", "KN", "PN", "WR", "PS")

SLAVO_GERMANIC_MARKERS = ("W", "K", "CZ", "WITZ")


class _Codes:
    """The primary and alternate keys under construction."""

    __slots__ = ("alternate", "primary")

    def __init__(self) -> None:
        self.primary: list[str] = []
        self.alternate: list[str] = []

    def add(self, primary: str, alternate: str | None = None) -> None:
        self.primary.append(primary)
        self.alternate.append(primary if alternate is None else alternate)

    def result(self, max_length: int) -> tuple[str, str]:
        p = "".join(self.primary)[:max_length]
        a = "".join(self.alternate)[:max_length]
        return p, a


def _is_slavo_germanic(word: str) -> bool:
    return any(marker in word for marker in SLAVO_GERMANIC_MARKERS)


def _at(word: str, pos: int, length: int = 1) -> str:
    if pos < 0 or pos >= len(word):
        return ""
    return word[pos : pos + length]


def _starts(word: str, pos: int, *options: str) -> bool:
    return any(word.startswith(opt, pos) for opt in options if opt)


# Surnames repeat heavily - a 50k provider file draws on a few thousand
# distinct ones - so the cache turns the hot path of index building into a
# dictionary lookup. The function is pure, so caching is free of risk.
@lru_cache(maxsize=65_536)
def double_metaphone(value: str, max_length: int = 4) -> tuple[str, str]:
    """Return the (primary, alternate) phonetic keys for ``value``.

    Both are empty strings for input with no alphabetic content.
    """
    word = "".join(ch for ch in value.upper() if ch.isalpha())
    if not word:
        return "", ""

    codes = _Codes()
    slavo = _is_slavo_germanic(word)
    length = len(word)
    last = length - 1
    pos = 0

    if _starts(word, 0, *SILENT_INITIALS):
        pos = 1
    if _at(word, 0) == "X":  # Xavier
        codes.add("S")
        pos = 1

    while pos < length and (len(codes.primary) < max_length or len(codes.alternate) < max_length):
        ch = word[pos]

        if ch in VOWELS:
            # Only an initial vowel is pronounced for our purposes.
            if pos == 0:
                codes.add("A")
            pos += 1

        elif ch == "B":
            codes.add("P")
            pos += 2 if _at(word, pos + 1) == "B" else 1

        elif ch == "Ç":
            codes.add("S")
            pos += 1

        elif ch == "C":
            pos = _handle_c(word, pos, codes)

        elif ch == "D":
            if _starts(word, pos, "DG"):
                if _at(word, pos + 2) in ("I", "E", "Y"):  # edge, ledger
                    codes.add("J")
                    pos += 3
                else:
                    codes.add("TK")
                    pos += 2
            elif _starts(word, pos, "DT", "DD"):
                codes.add("T")
                pos += 2
            else:
                codes.add("T")
                pos += 1

        elif ch == "F":
            codes.add("F")
            pos += 2 if _at(word, pos + 1) == "F" else 1

        elif ch == "G":
            pos = _handle_g(word, pos, slavo, codes)

        elif ch == "H":
            # Only pronounced between a vowel and either end or another vowel.
            if (pos == 0 or _at(word, pos - 1) in VOWELS) and _at(word, pos + 1) in VOWELS:
                codes.add("H")
                pos += 2
            else:
                pos += 1

        elif ch == "J":
            pos = _handle_j(word, pos, last, slavo, codes)

        elif ch == "K":
            codes.add("K")
            pos += 2 if _at(word, pos + 1) == "K" else 1

        elif ch == "L":
            if _at(word, pos + 1) == "L":
                if _is_spanish_ll(word, pos, length, last):
                    codes.add("L", "")
                else:
                    codes.add("L")
                pos += 2
            else:
                codes.add("L")
                pos += 1

        elif ch == "M":
            if (_starts(word, pos - 1, "UMB") and (pos + 1 == last or _starts(word, pos + 2, "ER"))) or _at(
                word, pos + 1
            ) == "M":
                pos += 2
            else:
                pos += 1
            codes.add("M")

        elif ch == "N":
            codes.add("N")
            pos += 2 if _at(word, pos + 1) == "N" else 1

        elif ch == "Ñ":
            codes.add("N")
            pos += 1

        elif ch == "P":
            if _at(word, pos + 1) == "H":
                codes.add("F")
                pos += 2
            else:
                codes.add("P")
                pos += 2 if _at(word, pos + 1) in ("P", "B") else 1

        elif ch == "Q":
            codes.add("K")
            pos += 2 if _at(word, pos + 1) == "Q" else 1

        elif ch == "R":
            # Final French -IER is silent in the primary reading.
            if pos == last and not slavo and _starts(word, pos - 2, "IE") and not _starts(word, pos - 4, "ME", "MA"):
                codes.add("", "R")
            else:
                codes.add("R")
            pos += 2 if _at(word, pos + 1) == "R" else 1

        elif ch == "S":
            pos = _handle_s(word, pos, last, codes)

        elif ch == "T":
            pos = _handle_t(word, pos, codes)

        elif ch == "V":
            codes.add("F")
            pos += 2 if _at(word, pos + 1) == "V" else 1

        elif ch == "W":
            pos = _handle_w(word, pos, last, codes)

        elif ch == "X":
            if not (pos == last and (_starts(word, pos - 3, "IAU", "EAU") or _starts(word, pos - 2, "AU", "OU"))):
                codes.add("KS")
            pos += 2 if _at(word, pos + 1) in ("C", "X") else 1

        elif ch == "Z":
            if _at(word, pos + 1) == "H":  # Zhao
                codes.add("J")
                pos += 2
            else:
                if _starts(word, pos + 1, "ZO", "ZI", "ZA") or (slavo and pos > 0 and _at(word, pos - 1) != "T"):
                    codes.add("S", "TS")
                else:
                    codes.add("S")
                pos += 2 if _at(word, pos + 1) == "Z" else 1

        else:
            pos += 1

    return codes.result(max_length)


# --------------------------------------------------------------------------
# per-letter handlers, split out to keep the main loop readable
# --------------------------------------------------------------------------


def _handle_c(word: str, pos: int, codes: _Codes) -> int:
    # Germanic -ACH- as in Bach, but not Bacher / Macher.
    if pos > 1 and word[pos - 2] not in VOWELS and _starts(word, pos - 1, "ACH") and _at(word, pos + 2) not in ("I",) and (
        _at(word, pos + 2) != "E" or _starts(word, pos - 2, "BACHER", "MACHER")
    ):
        codes.add("K")
        return pos + 2

    if pos == 0 and _starts(word, pos, "CAESAR"):
        codes.add("S")
        return pos + 2

    if _starts(word, pos, "CHIA"):  # chianti
        codes.add("K")
        return pos + 2

    if _starts(word, pos, "CH"):
        if pos > 0 and _starts(word, pos, "CHAE"):  # Michael
            codes.add("K", "X")
            return pos + 2
        greek = pos == 0 and (
            _starts(word, pos + 1, "HARAC", "HARIS") or _starts(word, pos + 1, "HOR", "HYM", "HIA", "HEM")
        )
        if greek and not _starts(word, 0, "CHORE"):
            codes.add("K")
            return pos + 2
        germanic = (
            _starts(word, 0, "VAN ", "VON ")
            or _starts(word, 0, "SCH")
            or _starts(word, pos - 2, "ORCHES", "ARCHIT", "ORCHID")
            or _at(word, pos + 2) in ("T", "S")
            or (
                (pos == 0 or _at(word, pos - 1) in ("A", "O", "U", "E"))
                and _at(word, pos + 2) in ("L", "R", "N", "M", "B", "H", "F", "V", "W", " ")
            )
        )
        if germanic:
            codes.add("K")
        elif pos > 0:
            codes.add("X", "K") if not _starts(word, 0, "MC") else codes.add("K")
        else:
            codes.add("X")
        return pos + 2

    if _starts(word, pos, "CZ") and not _starts(word, pos - 2, "WICZ"):
        codes.add("S", "X")
        return pos + 2

    if _starts(word, pos + 1, "CIA"):  # focaccia
        codes.add("X")
        return pos + 3

    if _starts(word, pos, "CC") and not (pos == 1 and word[0] == "M"):
        if _at(word, pos + 2) in ("I", "E", "H") and not _starts(word, pos + 2, "HU"):
            if (pos == 1 and word[0] == "A") or _starts(word, pos - 1, "UCCEE", "UCCES"):
                codes.add("KS")
            else:
                codes.add("X")
            return pos + 3
        codes.add("K")
        return pos + 2

    if _starts(word, pos, "CK", "CG", "CQ"):
        codes.add("K")
        return pos + 2

    if _starts(word, pos, "CI", "CE", "CY"):
        if _starts(word, pos, "CIO", "CIE", "CIA"):
            codes.add("S", "X")
        else:
            codes.add("S")
        return pos + 2

    codes.add("K")
    if _starts(word, pos + 1, " C", " Q", " G"):
        return pos + 3
    if _at(word, pos + 1) in ("C", "K", "Q") and not _starts(word, pos + 1, "CE", "CI"):
        return pos + 2
    return pos + 1


def _handle_g(word: str, pos: int, slavo: bool, codes: _Codes) -> int:
    if _at(word, pos + 1) == "H":
        if pos > 0 and word[pos - 1] not in VOWELS:
            codes.add("K")
            return pos + 2
        if pos == 0:
            codes.add("J" if _at(word, pos + 2) == "I" else "K")
            return pos + 2
        silent = (
            (pos > 1 and word[pos - 2] in ("B", "H", "D"))
            or (pos > 2 and word[pos - 3] in ("B", "H", "D"))
            or (pos > 3 and word[pos - 4] in ("B", "H"))
        )
        if silent:
            return pos + 2
        if pos > 2 and word[pos - 1] == "U" and word[pos - 3] in ("C", "G", "L", "R", "T"):
            codes.add("F")
        elif pos > 0 and word[pos - 1] != "I":
            codes.add("K")
        return pos + 2

    if _at(word, pos + 1) == "N":
        if pos == 1 and word[0] in VOWELS and not slavo:
            codes.add("KN", "N")
        elif not _starts(word, pos + 2, "EY") and _at(word, pos + 1) != "Y" and not slavo:
            codes.add("N", "KN")
        else:
            codes.add("KN")
        return pos + 2

    if _starts(word, pos + 1, "LI") and not slavo:  # tagliaro
        codes.add("KL", "L")
        return pos + 2

    if pos == 0 and (_at(word, pos + 1) == "Y" or _starts(word, pos + 1, "ES", "EP", "EB", "EL", "EY", "IB", "IL", "IN", "IE", "EI", "ER")):
        codes.add("K", "J")
        return pos + 2

    if (_starts(word, pos + 1, "ER") or _at(word, pos + 1) == "Y") and not _starts(word, 0, "DANGER", "RANGER", "MANGER") and (
        pos == 0 or word[pos - 1] not in ("E", "I")
    ) and not (pos > 0 and _starts(word, pos - 1, "RGY", "OGY")):
        codes.add("K", "J")
        return pos + 2

    if _at(word, pos + 1) in ("E", "I", "Y") or _starts(word, pos - 1, "AGGI", "OGGI"):
        if _starts(word, 0, "VAN ", "VON ") or _starts(word, 0, "SCH") or _starts(word, pos + 1, "ET"):
            codes.add("K")
        else:
            codes.add("J", "K")
        return pos + 2

    codes.add("K")
    return pos + 2 if _at(word, pos + 1) == "G" else pos + 1


def _handle_j(word: str, pos: int, last: int, slavo: bool, codes: _Codes) -> int:
    if _starts(word, pos, "JOSE") or _starts(word, 0, "SAN "):
        if (pos == 0 and _at(word, pos + 4) == " ") or _starts(word, 0, "SAN "):
            codes.add("H")
        else:
            codes.add("J", "H")
        return pos + 1

    if pos == 0:
        codes.add("J", "A")
    elif not slavo and _at(word, pos + 1) in VOWELS and pos > 0 and word[pos - 1] in ("A", "O"):
        codes.add("J", "H")
    elif pos == last:
        codes.add("J", "")
    elif _at(word, pos + 1) not in ("L", "T", "K", "S", "N", "M", "B", "Z") and (
        pos == 0 or word[pos - 1] not in ("S", "K", "L")
    ):
        codes.add("J")
    return pos + 2 if _at(word, pos + 1) == "J" else pos + 1


def _is_spanish_ll(word: str, pos: int, length: int, last: int) -> bool:
    """-LLO / -LLA endings, where the second L is not sounded in English."""
    if pos == length - 3 and _starts(word, pos - 1, "ILLO", "ILLA", "ALLE"):
        return True
    tail_a = _starts(word, last - 1, "AS", "OS")
    tail_b = word[last] in ("A", "O")
    return bool(_starts(word, 0, "ALLE") and (tail_a or tail_b))


def _handle_s(word: str, pos: int, last: int, codes: _Codes) -> int:
    if _starts(word, pos - 1, "ISL", "YSL"):  # island, isle
        return pos + 1

    if pos == 0 and _starts(word, pos, "SUGAR"):
        codes.add("X", "S")
        return pos + 1

    if _starts(word, pos, "SH"):
        if _starts(word, pos + 1, "HEIM", "HOEK", "HOLM", "HOLZ"):
            codes.add("S")
        else:
            codes.add("X")
        return pos + 2

    if _starts(word, pos, "SIO", "SIA") or _starts(word, pos, "SIAN"):
        codes.add("S", "X")
        return pos + 3

    if (pos == 0 and _at(word, pos + 1) in ("M", "N", "L", "W")) or _at(word, pos + 1) == "Z":
        codes.add("S", "X")
        return pos + 2 if _at(word, pos + 1) == "Z" else pos + 1

    if _starts(word, pos, "SC"):
        if _at(word, pos + 2) == "H":
            if _starts(word, pos + 3, "OO", "ER", "EN", "UY", "ED", "EM"):
                if _starts(word, pos + 3, "ER", "EN"):
                    codes.add("X", "SK")
                else:
                    codes.add("SK")
                return pos + 3
            if pos == 0 and word[3] not in VOWELS and _at(word, 3) != "W":
                codes.add("X", "S")
            else:
                codes.add("X")
            return pos + 3
        if _at(word, pos + 2) in ("I", "E", "Y"):
            codes.add("S")
            return pos + 3
        codes.add("SK")
        return pos + 3

    if pos == last and _starts(word, pos - 2, "AI", "OI"):  # French, silent
        codes.add("", "S")
    else:
        codes.add("S")
    return pos + 2 if _at(word, pos + 1) in ("S", "Z") else pos + 1


def _handle_t(word: str, pos: int, codes: _Codes) -> int:
    if _starts(word, pos, "TION") or _starts(word, pos, "TIA", "TCH"):
        codes.add("X")
        return pos + 3

    if _starts(word, pos, "TH") or _starts(word, pos, "TTH"):
        if _starts(word, pos + 2, "OM", "AM") or _starts(word, 0, "VAN ", "VON ", "SCH"):
            codes.add("T")
        else:
            codes.add("0", "T")  # "0" is the conventional code for a soft TH
        return pos + 2

    codes.add("T")
    return pos + 2 if _at(word, pos + 1) in ("T", "D") else pos + 1


def _handle_w(word: str, pos: int, last: int, codes: _Codes) -> int:
    if _starts(word, pos, "WR"):
        codes.add("R")
        return pos + 2

    if pos == 0 and (_at(word, pos + 1) in VOWELS or _starts(word, pos, "WH")):
        codes.add("A", "F") if _at(word, pos + 1) in VOWELS else codes.add("A")
        return pos + 1

    if (
        (pos == last and pos > 0 and word[pos - 1] in VOWELS)
        or _starts(word, pos - 1, "EWSKI", "EWSKY", "OWSKI", "OWSKY")
        or _starts(word, 0, "SCH")
    ):
        codes.add("", "F")
        return pos + 1

    if _starts(word, pos, "WICZ", "WITZ"):
        codes.add("TS", "FX")
        return pos + 4

    return pos + 1


def phonetic_key(value: str) -> str:
    """The primary key alone - what a single-column index stores."""
    return double_metaphone(value)[0]


@lru_cache(maxsize=65_536)
def phonetic_keys(value: str) -> tuple[str, ...]:
    """Both keys, deduped and empty-stripped - what blocking indexes."""
    primary, alternate = double_metaphone(value)
    return tuple(dict.fromkeys(k for k in (primary, alternate) if k))
