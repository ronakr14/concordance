"""Lexicons the matching engine owns.

Deliberately independent of `synth/`'s reference tables. The generator's tables
describe the data it invents; these describe what the engine believes about
names, credentials and addresses in the real world. Coupling them would make a
test that "matching handles nicknames" prove only that the generator and the
matcher share a file - and would leave the engine with no nickname knowledge at
all on the day real data replaces the synthetic set.

Everything here is a frozen module-level constant. No I/O, no mutable state.
"""

from __future__ import annotations

from types import MappingProxyType

# --- names ---------------------------------------------------------------

# canonical -> the informal forms that should fold into it.
_NICKNAME_GROUPS: dict[str, tuple[str, ...]] = {
    "ROBERT": ("BOB", "BOBBY", "ROB", "ROBBIE", "BERT"),
    "WILLIAM": ("BILL", "BILLY", "WILL", "WILLIE", "WILLY", "LIAM"),
    "RICHARD": ("RICK", "RICKY", "DICK", "RICH", "RICHIE"),
    "MARGARET": ("PEGGY", "PEG", "MAGGIE", "MEG", "MARGE", "MARGIE", "GRETA"),
    "ELIZABETH": ("BETH", "LIZ", "LIZZY", "BETTY", "BETSY", "ELIZA", "LIZA", "LIBBY"),
    "JAMES": ("JIM", "JIMMY", "JAMIE", "JIMMIE"),
    "JOHN": ("JACK", "JOHNNY", "JOHNNIE", "JON"),
    "MICHAEL": ("MIKE", "MICKEY", "MICK", "MIKEY"),
    "CHARLES": ("CHUCK", "CHARLIE", "CHAS", "CHARLEY"),
    "JOSEPH": ("JOE", "JOEY", "JOSE"),
    "THOMAS": ("TOM", "TOMMY", "THOM"),
    "CHRISTOPHER": ("CHRIS", "TOPHER", "KIT"),
    "ANTHONY": ("TONY", "ANTON"),
    "NICHOLAS": ("NICK", "NICKY", "NICO"),
    "STEPHEN": ("STEVE", "STEVIE", "STEVEN"),
    "DANIEL": ("DAN", "DANNY", "DANE"),
    "MATTHEW": ("MATT", "MATTY"),
    "ANDREW": ("ANDY", "DREW"),
    "DAVID": ("DAVE", "DAVEY"),
    "DONALD": ("DON", "DONNY"),
    "RONALD": ("RON", "RONNIE"),
    "KENNETH": ("KEN", "KENNY"),
    "EDWARD": ("ED", "EDDIE", "TED", "TEDDY", "NED"),
    "LAWRENCE": ("LARRY", "LAURENCE", "LAURIE"),
    "GREGORY": ("GREG", "GREGG"),
    "JEFFREY": ("JEFF", "GEOFFREY"),
    "TIMOTHY": ("TIM", "TIMMY"),
    "BENJAMIN": ("BEN", "BENNY", "BENJI"),
    "SAMUEL": ("SAM", "SAMMY"),
    "ALEXANDER": ("ALEX", "AL", "SANDY", "XANDER"),
    "ZACHARY": ("ZACH", "ZACK", "ZAK"),
    "NATHANIEL": ("NATE", "NAT", "NATHAN"),
    "FREDERICK": ("FRED", "FREDDY", "FRITZ"),
    "PATRICK": ("PAT", "PADDY", "RICK"),
    "PATRICIA": ("PAT", "PATTY", "TRISH", "TRICIA"),
    "BARBARA": ("BARB", "BABS", "BARBIE"),
    "SUSAN": ("SUE", "SUSIE", "SUZY", "SUZANNE"),
    "DEBORAH": ("DEB", "DEBBIE", "DEBRA"),
    "KATHERINE": ("KATE", "KATIE", "KATHY", "KAY", "KIT", "CATHERINE", "KATHRYN"),
    "JENNIFER": ("JEN", "JENNY", "JENNIE"),
    "CYNTHIA": ("CINDY", "CYN"),
    "SANDRA": ("SANDY", "SANDIE"),
    "KIMBERLY": ("KIM", "KIMMY"),
    "REBECCA": ("BECKY", "BECCA", "REBEKAH"),
    "THERESA": ("TERRY", "TERRI", "TESS", "TERESA"),
    "CHRISTINA": ("CHRIS", "TINA", "CHRISTINE", "KRISTINA"),
    "VICTORIA": ("VICKY", "VICKI", "TORI"),
    "DANIELLE": ("DANI", "DANNI"),
    "SAMANTHA": ("SAM", "SAMMY"),
    "STEPHANIE": ("STEPH", "STEFF"),
    "MICHELLE": ("SHELLY", "MICHELE"),
    "PAMELA": ("PAM", "PAMMY"),
    "ANGELA": ("ANGIE", "ANGELIA"),
    "VERONICA": ("RONNIE", "VERO"),
    "JOSEPHINE": ("JOSIE", "JO"),
    "ELEANOR": ("ELLIE", "NORA"),
    "VIRGINIA": ("GINNY", "GINGER"),
    "DOROTHY": ("DOT", "DOTTIE", "DOLLY"),
    "MARGARITA": ("RITA", "MARGO"),
    "FRANCISCO": ("PACO", "FRANK", "CISCO"),
    "MARIA": ("MARY", "MARIE"),
    "JESUS": ("CHUY", "JESSE"),
    "ANTONIO": ("TONY", "ANTON"),
    "ALEJANDRO": ("ALEX", "ALEJO"),
}

# Flattened: informal form -> canonical. Built once at import.
NICKNAME_TO_CANONICAL = MappingProxyType(
    {nick: canonical for canonical, nicks in _NICKNAME_GROUPS.items() for nick in nicks}
)

# Canonical forms are their own canonical form, which makes lookup total.
CANONICAL_NAMES = frozenset(_NICKNAME_GROUPS)

# Credentials and generational suffixes stripped from a personal name. A name
# field is not a credentials field, and whether "MD" was typed into it says
# nothing about identity.
CREDENTIAL_SUFFIXES = frozenset(
    {
        "MD", "DO", "DDS", "DMD", "DPM", "DC", "OD", "PHARMD", "PHD", "EDD", "PSYD",
        "RN", "LPN", "LVN", "NP", "APRN", "CRNA", "CNM", "PA", "PAC", "PA-C",
        "LCSW", "LMSW", "LPC", "LMFT", "PT", "DPT", "OT", "OTR", "SLP", "RD", "RPH",
        "FACS", "FACP", "FAAP", "FACC", "FACOG", "FAAFP", "MPH", "MBA", "MS", "MSN", "BSN",
    }
)

GENERATIONAL_SUFFIXES = frozenset({"JR", "SR", "II", "III", "IV", "V", "VI", "1ST", "2ND", "3RD"})

NAME_PREFIXES = frozenset({"DR", "MR", "MRS", "MS", "MISS", "PROF", "REV"})

# Particles that belong to the surname but are written apart.
SURNAME_PARTICLES = frozenset({"DE", "DEL", "DELA", "DELLA", "DI", "DA", "DOS", "LA", "LE", "VAN", "VON", "VANDER", "MAC", "MC", "ST", "SAINT"})

# --- addresses -----------------------------------------------------------

# Abbreviation -> expanded form. Normalization expands rather than abbreviates,
# because expansion is many-to-one: ST and STR both become STREET.
STREET_ABBREVIATIONS = MappingProxyType(
    {
        "ST": "STREET", "STR": "STREET", "STRT": "STREET",
        "AVE": "AVENUE", "AV": "AVENUE", "AVEN": "AVENUE",
        "RD": "ROAD", "DR": "DRIVE", "DRV": "DRIVE",
        "LN": "LANE", "BLVD": "BOULEVARD", "BLV": "BOULEVARD",
        "CT": "COURT", "CRT": "COURT", "PL": "PLACE", "PLZ": "PLAZA",
        "TER": "TERRACE", "TERR": "TERRACE", "CIR": "CIRCLE", "CIRC": "CIRCLE",
        "PKWY": "PARKWAY", "PKY": "PARKWAY", "PKWAY": "PARKWAY",
        "HWY": "HIGHWAY", "HGWY": "HIGHWAY", "EXPY": "EXPRESSWAY",
        "SQ": "SQUARE", "TRL": "TRAIL", "TRLS": "TRAILS", "WY": "WAY",
        "N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST",
        "NE": "NORTHEAST", "NW": "NORTHWEST", "SE": "SOUTHEAST", "SW": "SOUTHWEST",
        "MT": "MOUNT", "MTN": "MOUNTAIN", "FT": "FORT", "PT": "POINT",
    }
)

# Designators that introduce a secondary unit, split out of the street line.
UNIT_DESIGNATORS = frozenset(
    {"APT", "APARTMENT", "STE", "SUITE", "UNIT", "RM", "ROOM", "FL", "FLOOR", "BLDG", "BUILDING", "DEPT", "TRLR", "LOT", "SPC"}
)

PO_BOX_MARKERS = ("PO BOX", "P O BOX", "POBOX", "POST OFFICE BOX")

# --- organizations -------------------------------------------------------

# Legal-form suffixes, canonicalized to one token each.
CORPORATE_SUFFIXES = MappingProxyType(
    {
        "LLC": "LLC", "L.L.C.": "LLC", "LLC.": "LLC",
        "INC": "INC", "INC.": "INC", "INCORPORATED": "INC",
        "CORP": "CORP", "CORP.": "CORP", "CORPORATION": "CORP",
        "LTD": "LTD", "LIMITED": "LTD",
        "LLP": "LLP", "L.L.P.": "LLP", "LP": "LP",
        "PA": "PA", "P.A.": "PA", "PC": "PC", "P.C.": "PC",
        "PLLC": "PLLC", "P.L.L.C.": "PLLC", "PLC": "PLC",
        "CO": "CO", "COMPANY": "CO",
    }
)

# Words that carry no identifying weight in an organization name.
ORG_STOPWORDS = frozenset({"THE", "OF", "AND", "FOR", "AT", "IN", "A", "AN"})

# Expansions the engine knows. Acronyms in a sanction file are rarely defined.
ORG_ACRONYM_EXPANSIONS = MappingProxyType(
    {
        "HHS": "HOME HEALTH SERVICES",
        "SNF": "SKILLED NURSING FACILITY",
        "DME": "DURABLE MEDICAL EQUIPMENT",
        "ASC": "AMBULATORY SURGERY CENTER",
        "FQHC": "FEDERALLY QUALIFIED HEALTH CENTER",
        "PT": "PHYSICAL THERAPY",
        "OT": "OCCUPATIONAL THERAPY",
        "EMS": "EMERGENCY MEDICAL SERVICES",
        "ER": "EMERGENCY ROOM",
        "ICU": "INTENSIVE CARE UNIT",
        "MRI": "MAGNETIC RESONANCE IMAGING",
        "CMHC": "COMMUNITY MENTAL HEALTH CENTER",
    }
)

# --- geography -----------------------------------------------------------

US_STATES = MappingProxyType(
    {
        "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
        "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
        "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
        "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
        "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME",
        "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
        "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
        "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
        "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
        "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA", "PUERTO RICO": "PR",
        "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
        "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
        "VIRGINIA": "VA", "VIRGIN ISLANDS": "VI", "WASHINGTON": "WA",
        "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    }
)

STATE_CODES = frozenset(US_STATES.values())

# Misspellings and postal variants seen in real extracts.
STATE_ALIASES = MappingProxyType(
    {
        "CALIF": "CA", "CALIFORNA": "CA", "CAL": "CA",
        "PENN": "PA", "PENNA": "PA", "PENNSYLVANNIA": "PA",
        "MASS": "MA", "MASSACHUSSETTS": "MA", "MASSACHUSETS": "MA",
        "TEX": "TX", "TEXS": "TX",
        "FLA": "FL", "FLOR": "FL", "FLORDIA": "FL",
        "ILL": "IL", "ILLINIOS": "IL", "ILLINOISE": "IL",
        "MICH": "MI", "MINN": "MN", "MISS": "MS", "MO.": "MO",
        "N CAROLINA": "NC", "S CAROLINA": "SC", "N DAKOTA": "ND", "S DAKOTA": "SD",
        "N YORK": "NY", "NEWYORK": "NY", "N JERSEY": "NJ", "NEWJERSEY": "NJ",
        "W VIRGINIA": "WV", "WVA": "WV", "N HAMPSHIRE": "NH", "N MEXICO": "NM",
        "WASH": "WA", "WASH DC": "DC", "D.C.": "DC", "DC.": "DC",
        "ARIZ": "AZ", "ARK": "AR", "COLO": "CO", "CONN": "CT", "DEL": "DE",
        "GA.": "GA", "IND": "IN", "KAN": "KS", "KANS": "KS", "KEN": "KY",
        "NEB": "NE", "NEBR": "NE", "NEV": "NV", "OKLA": "OK", "ORE": "OR",
        "TENN": "TN", "VER": "VT", "VIRG": "VA", "WISC": "WI", "WYO": "WY",
    }
)
