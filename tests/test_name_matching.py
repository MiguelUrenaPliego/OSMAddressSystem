"""Tests for name_matching.names_match / name_similarity."""

from osmaddresssystem.name_matching import extract_route_number, name_similarity, names_match


def test_identical_names_match():
    assert names_match("Elm Street", "Elm Street")


def test_case_and_accent_insensitive():
    assert names_match("Boulevard de la Liberte", "Boulevard de la Liberté")
    assert names_match("RUE DE LA PAIX", "rue de la paix")


def test_common_abbreviations_match():
    assert names_match("Elm Street", "Elm St")
    assert names_match("Avenue Kennedy", "Av. Kennedy")


def test_route_number_matches_despite_different_wording():
    """The example from the spec: a route named after its number and the
    same route named after the towns it connects, with the number in a
    parenthetical."""
    assert names_match("Nationale 5", "Douala-Bafoussam (N5)")
    assert extract_route_number("Nationale 5") == "5"
    assert extract_route_number("Douala-Bafoussam (N5)") == "5"


def test_different_route_numbers_never_match():
    """A route-number mismatch overrides any surrounding string
    similarity - N5 and N6 must never be folded together."""
    assert not names_match("Nationale 5", "Nationale 6")
    assert not names_match("Route Nationale 5", "Route Nationale 6")


def test_route_number_only_recognized_next_to_a_route_marker():
    """A bare number elsewhere in a name (a house-number-style street
    name, say) isn't a route number, so it must fall back to plain
    string similarity instead of a route-number comparison."""
    assert extract_route_number("Elm Street") is None
    assert extract_route_number("5th Avenue") is None


def test_unrelated_names_do_not_match():
    assert not names_match("Rue de la Paix", "Boulevard Central")


def test_empty_or_none_names_never_match():
    assert not names_match("", "Elm Street")
    assert not names_match("Elm Street", "")
    assert not names_match(None, "Elm Street")


def test_threshold_is_configurable():
    score = name_similarity("Elm Street", "Elm Streat")  # one-letter typo
    assert 0 < score < 100
    assert names_match("Elm Street", "Elm Streat", threshold=score - 1)
    assert not names_match("Elm Street", "Elm Streat", threshold=score + 1)
