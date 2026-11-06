"""Normalization used only for COMPARING records. Nothing here is written back to the CRM.

Addresses: lowercase, strip punctuation, and convert directions and street types to the
USPS standard short forms (Publication 28), so "1120 West Main Street" and "1120 W Main St"
compare as equal. Only long forms are shortened, so "St" (Saint) in "45 St Lawrence Dr"
stays consistent on both sides.
"""
import re

DIRECTIONS = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}
STREET_TYPES = {
    "street": "st", "road": "rd", "avenue": "ave", "av": "ave", "boulevard": "blvd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy",
    "highway": "hwy", "circle": "cir", "terrace": "ter", "square": "sq", "trail": "trl",
    "pk": "pike", "pke": "pike",
}
WORD_MAP = {**DIRECTIONS, **STREET_TYPES}


def street_key(street):
    """'1120 West Main Street' -> '1120 w main st'."""
    words = re.sub(r"[^a-z0-9 ]", " ", (street or "").lower()).split()
    return " ".join(WORD_MAP.get(w, w) for w in words)


def is_po_box(street):
    return bool(re.match(r"^\s*p\.?\s*o\.?\s*box\b", street or "", re.I))


def zip5(zip_code):
    return re.sub(r"\D", "", zip_code or "")[:5]


def place_key(city, state):
    return (re.sub(r"[^a-z ]", "", (city or "").lower()).strip(), (state or "").strip().upper())


def name_key(name):
    """Loose form of a name for 'is this really a different name?' checks."""
    n = (name or "").lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return " ".join(n.split())


def phone_digits(phone):
    return re.sub(r"\D", "", phone or "")[-10:]


def person_key(name):
    return " ".join((name or "").lower().split())
