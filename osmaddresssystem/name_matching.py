"""Fuzzy matching for street names.

Two edges can carry different literal `name` strings for the same
real-world street: an abbreviation vs. the full form, an accented vs.
unaccented spelling, a route number in a different position/format, or
one edge named after the route ("Nationale 5") and another named after
the two towns it connects, with the route folded into a parenthetical
("Douala-Bafoussam (N5)"). `names_match` tries to recognize these as
the same street.

Two independent signals are combined:

1. **Route number.** Many highway names encode a route number, often
   next to a route-class marker (`N5`, `RN 5`, `Route Nationale 5`).
   If a route number can be extracted from both names, they match only
   if that number is the same. This is checked first and is decisive:
   a route number match overrides any string dissimilarity elsewhere
   in the name, and a route number mismatch overrides any string
   similarity elsewhere (two different numbered routes should never be
   folded together just because their surrounding text looks similar).
2. **Fuzzy string similarity**, used when a route number isn't found in
   both names. Names are normalized (accents stripped, case folded,
   punctuation collapsed to spaces) and compared with
   `rapidfuzz.fuzz.token_sort_ratio`, which is language-agnostic (it's
   an edit-distance measure over normalized tokens, not a dictionary),
   so it works the same way regardless of the language the street name
   is written in.
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

from .default_values import DEFAULT_NAME_SIMILARITY_THRESHOLD

# Route-class markers that can precede a route number: N5, RN5, RD12, R5,
# D5, A7, and the spelled-out "Route"/"Nationale"/"National"/"Highway".
_ROUTE_NUMBER_RE = re.compile(
    r"\b(?:RN|RD|N|R|D|A|route(?:\s+nationale)?|nationale|national|highway|hwy)\.?\s*-?\s*(\d{1,4})\b",
    re.IGNORECASE,
)


def _strip_accents(text: str) -> str:
    """Remove accents/diacritics from a string.

    Args:
        text: The string to strip accents from.

    Returns:
        The input with all combining diacritical marks removed.
    """
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


# Common street-suffix abbreviations, expanded before comparison so
# "Elm St" and "Elm Street" land on the same normalized form. Matched as
# whole words only, after punctuation has already been collapsed to
# spaces.
_ABBREVIATIONS = {
    "st": "street",
    "rd": "road",
    "ave": "avenue",
    "av": "avenue",
    "blvd": "boulevard",
    "dr": "drive",
    "ln": "lane",
    "hwy": "highway",
    "ft": "fort",
    "mt": "mount",
}


def _normalize(name: str) -> str:
    """Normalize a street name for fuzzy comparison.

    Strips accents, lowercases, collapses punctuation to spaces, and
    expands common street-suffix abbreviations (see ``_ABBREVIATIONS``).

    Args:
        name: The raw street name.

    Returns:
        The normalized, whitespace-joined form of ``name``.
    """
    name = _strip_accents(name).lower()
    name = re.sub(r"[^a-z0-9]+", " ", name)
    words = [_ABBREVIATIONS.get(w, w) for w in name.split()]
    return " ".join(words)


def extract_route_number(name: str) -> str | None:
    """Pull a route number (e.g. "5") out of a street name, if present.

    Only matches a number adjacent to a route-class marker (`N`, `RN`,
    `RD`, `R`, `D`, `A`, `Route`, `Nationale`, `National`, `Highway`), so
    an address like "5 Rue de la Paix" or "Elm Street" doesn't
    accidentally produce a false route number.

    Args:
        name: The street name to search.

    Returns:
        The matched route number as a string, or ``None`` if no
        route-class marker with an adjacent number was found.
    """
    match = _ROUTE_NUMBER_RE.search(name)
    return match.group(1) if match else None


def name_similarity(name_a: str, name_b: str) -> float:
    """Similarity between two street names, in [0, 100].

    Args:
        name_a: The first street name.
        name_b: The second street name.

    Returns:
        100 if both names encode the same route number, 0 if they
        encode different route numbers (a numbered-route mismatch always
        wins over string similarity), and a fuzzy string similarity score
        otherwise.
    """
    route_a = extract_route_number(name_a)
    route_b = extract_route_number(name_b)
    if route_a is not None and route_b is not None:
        return 100.0 if route_a == route_b else 0.0
    return fuzz.token_sort_ratio(_normalize(name_a), _normalize(name_b))


def names_match(
    name_a: str, name_b: str, threshold: float = DEFAULT_NAME_SIMILARITY_THRESHOLD
) -> bool:
    """Whether two street names should be treated as the same street.

    Args:
        name_a: The first street name.
        name_b: The second street name.
        threshold: Minimum ``name_similarity`` score (0-100) for a match.

    Returns:
        True if both names are non-empty and their similarity score is
        at least ``threshold``.
    """
    if not name_a or not name_b:
        return False
    return name_similarity(name_a, name_b) >= threshold
