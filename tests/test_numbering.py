"""Tests for numbering.assign_house_numbers/add_admin_columns/geocode_points."""

import math
import os

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from osmaddresssystem import numbering, pipeline, street_direction, street_ids
from osmaddresssystem.synthetic_grid import build_idealized_grid


def _labeled_and_directed(G, origin=(0.0, 0.0), **kwargs):
    """Test helper: labeled and directed."""
    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(G, node_cluster_m=0)
    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf, edges_gdf, streets_df, origin=origin, **kwargs
    )
    return nodes_gdf, edges_gdf, streets_df


def _expected_house_number(distance_m, divisor_m, side):
    """Test helper: expected house number."""
    base = round(distance_m / divisor_m)
    wants_even = side == "right"
    if (base % 2 == 0) != wants_even:
        base += 1
    return max(2 if wants_even else 1, base)


def test_house_numbers_are_cumulative_length_divided_by_twenty_per_side():
    """Check house numbers are cumulative length divided by twenty per side."""
    G = build_idealized_grid(rows=3, cols=3, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    edges_gdf, streets_df = numbering.assign_house_numbers(edges_gdf, streets_df, number_divisor_m=20.0)

    numbered = edges_gdf[edges_gdf["street_start_length_m"].notna()]
    assert len(numbered) > 0
    for _, row in numbered.iterrows():
        for side in ("left", "right"):
            start = row[f"house_number_start_{side}"]
            end = row[f"house_number_end_{side}"]
            assert start == _expected_house_number(row["street_start_length_m"], 20.0, side)
            assert end == _expected_house_number(row["street_end_length_m"], 20.0, side)
            # Right is always even and >= 2, left always odd and >= 1.
            if side == "right":
                assert start % 2 == 0 and start >= 2
                assert end % 2 == 0 and end >= 2
            else:
                assert start % 2 == 1 and start >= 1
                assert end % 2 == 1 and end >= 1


def test_house_numbers_use_custom_divisor():
    """Check house numbers use custom divisor."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    edges_gdf, streets_df = numbering.assign_house_numbers(
        edges_gdf, streets_df, number_divisor_m=50.0
    )
    numbered = edges_gdf[edges_gdf["street_start_length_m"].notna()]
    row = numbered.iloc[0]
    assert row["house_number_start_right"] == _expected_house_number(
        row["street_start_length_m"], 50.0, "right"
    )
    assert row["house_number_start_left"] == _expected_house_number(
        row["street_start_length_m"], 50.0, "left"
    )


def test_house_number_from_distance_parity_and_minimums():
    """Right is always even (minimum 2), left always odd (minimum 1) -
    round(0 / divisor) would naively be 0, which is neither a valid even
    (right, minimum 2) nor odd (left, minimum 1) house number."""
    assert numbering._house_number_from_distance(0.0, 20.0, "right") == 2
    assert numbering._house_number_from_distance(0.0, 20.0, "left") == 1
    assert numbering._house_number_from_distance(9.9, 20.0, "right") == 2
    assert numbering._house_number_from_distance(9.9, 20.0, "left") == 1
    assert numbering._house_number_from_distance(10.1, 20.0, "right") == 2
    assert numbering._house_number_from_distance(10.1, 20.0, "left") == 1
    # 30/20 = 1.5, which Python's round() (banker's rounding) rounds to
    # 2 - already even, so "right" keeps it; "left" needs odd, so it
    # nudges to the next one up (3), not down.
    assert numbering._house_number_from_distance(30.0, 20.0, "right") == 2
    assert numbering._house_number_from_distance(30.0, 20.0, "left") == 3
    # 70/20 = 3.5, rounds to 4 (banker's rounding) - already even.
    assert numbering._house_number_from_distance(70.0, 20.0, "right") == 4
    assert numbering._house_number_from_distance(70.0, 20.0, "left") == 5

    with pytest.raises(ValueError):
        numbering._house_number_from_distance(10.0, 20.0, "up")


def test_geocode_points_house_number_never_zero_at_streets_own_start():
    """Check geocode points house number never zero at streets own start."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    edges_gdf, streets_df = numbering.assign_house_numbers(edges_gdf, streets_df)
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    # (0, 0) is the origin - the very start of the radial streets there.
    [address] = numbering.geocode_points(streets_gdf, [(0.0, 0.0)])
    house_number = int(address.split(",")[0])
    assert house_number >= 1


def test_house_numbers_are_none_where_position_is_none():
    """Check house numbers are none where position is none."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    edges_gdf.loc[edges_gdf.index[0], "street_start_length_m"] = None
    edges_gdf.loc[edges_gdf.index[0], "street_end_length_m"] = None

    edges_gdf, streets_df = numbering.assign_house_numbers(edges_gdf, streets_df)
    for side in ("left", "right"):
        assert edges_gdf.iloc[0][f"house_number_start_{side}"] is None
        assert edges_gdf.iloc[0][f"house_number_end_{side}"] is None


def test_add_admin_columns_fills_from_polygon_chain(monkeypatch):
    """Fake reverse_geocode_boundary so the test doesn't need the network.
    Polygons are ranked by their own area (largest first) and paired with
    admin_levels in the same coarsest-first order, so a much bigger
    "town" polygon (standing in for the country/province chain here)
    must land in a coarser column than a much smaller "neighbourhood"
    polygon, regardless of the dict's own key names."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    _, _, streets_df = street_ids.label_streets(G, node_cluster_m=0)

    def fake_reverse(lat, lon, zooms=(), **kwargs):
        """Test helper: fake reverse."""
        # Route the fake name by the exact single zoom add_admin_columns
        # asks for, matching _ADMIN_COLUMN_TO_ZOOM's own mapping.
        names = {3: "Cameroon", 5: "West", 10: "Bandja", 16: "Downtown"}
        zoom = zooms[0]
        return (str(zoom), zoom, names[zoom], Polygon()) if zoom in names else None

    monkeypatch.setattr(numbering.utils, "reverse_geocode_boundary", fake_reverse)

    # Much bigger than "neighbourhood_polygon" below, so it's ranked
    # first (coarsest) regardless of its own key name.
    town_polygon = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
    neighbourhood_polygon = Polygon([(0, 0), (10, 0), (10, 5), (0, 5)])

    result = numbering.add_admin_columns(
        streets_df,
        {"town": town_polygon, "neighbourhood": neighbourhood_polygon},
    )

    # Only two polygons given: they're paired with the first two
    # (coarsest) admin_levels, country and province.
    assert (result["country"] == "Cameroon").all()
    assert (result["province"] == "West").all()
    assert result["city"].isna().all()
    assert result["neighbourhood"].isna().all()


def test_add_admin_columns_ranks_by_area_not_dict_order(monkeypatch):
    """Even if the smaller polygon appears first in the dict, area
    ranking (not insertion order) decides which admin level it's paired
    with."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    _, _, streets_df = street_ids.label_streets(G, node_cluster_m=0)

    def fake_reverse(lat, lon, zooms=(), **kwargs):
        """Test helper: fake reverse."""
        names = {3: "Cameroon", 5: "West"}
        zoom = zooms[0]
        return (str(zoom), zoom, names[zoom], Polygon()) if zoom in names else None

    monkeypatch.setattr(numbering.utils, "reverse_geocode_boundary", fake_reverse)

    small_polygon = Polygon([(0, 0), (10, 0), (10, 5), (0, 5)])
    big_polygon = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])

    # "small" is inserted first, but must still be paired with the finer
    # level (province) since it's ranked by area, not dict order.
    result = numbering.add_admin_columns(streets_df, {"small": small_polygon, "big": big_polygon})
    assert (result["country"] == "Cameroon").all()
    assert (result["province"] == "West").all()


def test_geocode_points_projects_onto_nearest_street_and_formats_address():
    """Check geocode points projects onto nearest street and formats address."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    edges_gdf, streets_df = numbering.assign_house_numbers(edges_gdf, streets_df)
    streets_df["street_name"] = "Test Street"
    streets_df["country"] = "Cameroon"
    streets_df["province"] = "West"
    streets_df["city"] = "Bandja"
    streets_df["neighbourhood"] = None

    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    point = (5.0, 0.0)  # just off the bottom horizontal street
    [address] = numbering.geocode_points(streets_gdf, [point])

    assert "Test Street" in address
    assert "Cameroon" in address
    assert "West" in address
    assert "Bandja" in address
    # neighbourhood is None, must not appear as the literal string "None".
    assert "None" not in address


def test_side_of_line_left_and_right():
    """Facing east (line running west to east), north is to the LEFT,
    south is to the RIGHT - standard x=east/y=north convention."""
    from shapely.geometry import LineString

    line = LineString([(0, 0), (100, 0)])
    assert numbering._side_of_line(Point(50, 10), line) == "left"
    assert numbering._side_of_line(Point(50, -10), line) == "right"

    # Facing north (line running south to north), east is to the RIGHT,
    # west is to the LEFT.
    line_ns = LineString([(0, 0), (0, 100)])
    assert numbering._side_of_line(Point(10, 50), line_ns) == "right"
    assert numbering._side_of_line(Point(-10, 50), line_ns) == "left"


def test_geocode_points_uses_correct_side_for_house_number():
    """A point north of an east-running street gets the left (odd)
    number; a point south gets the right (even) number, both derived
    from the same distance-along, via geocode_points' own side
    detection - not something a caller has to figure out themselves."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    edges_gdf, streets_df = numbering.assign_house_numbers(edges_gdf, streets_df)
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    # The bottom horizontal street runs along y=0 from x=0. A point just
    # north (left, facing away from origin/east) vs. just south (right).
    [address_left] = numbering.geocode_points(streets_gdf, [(50.0, 5.0)])
    [address_right] = numbering.geocode_points(streets_gdf, [(50.0, -5.0)])

    left_number = int(address_left.split(",")[0])
    right_number = int(address_right.split(",")[0])
    assert left_number % 2 == 1
    assert right_number % 2 == 0


def test_geocode_point_convenience_wrapper_matches_geocode_points():
    """Check geocode point convenience wrapper matches geocode points."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    edges_gdf, streets_df = numbering.assign_house_numbers(edges_gdf, streets_df)
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    point = (5.0, 0.0)
    single = numbering.geocode_point(streets_gdf, point)
    [batch] = numbering.geocode_points(streets_gdf, [point])
    assert single == batch


def test_geocode_points_raises_on_empty_streets():
    """Check geocode points raises on empty streets."""
    empty = gpd.GeoDataFrame({"street_id": [], "street_name": []}, geometry=[], crs="EPSG:3857")
    with pytest.raises(ValueError):
        numbering.geocode_points(empty, [(0.0, 0.0)])


def test_letters_for_crowded_slots():
    """Items sharing a slot get A, B, ...; a lone item keeps its plain number."""
    from osmaddresssystem.numbering import add_letters, number_letter

    assert add_letters([3, 3, 5], ["a3", "a3", "a5"], [10.0, 2.0, 0.0]) == ["3B", "3A", "5"]
    assert [number_letter(i) for i in (0, 25, 26)] == ["A", "Z", "AA"]


def test_default_house_divisor_is_two_metres_and_numbers_start_at_1_and_2():
    """Houses are numbered every 2 m by default; the left side starts at 1 and the right at 2."""
    from osmaddresssystem.default_values import DEFAULT_BRANCH_NUMBER_DIVISOR_M, DEFAULT_NUMBER_DIVISOR_M

    assert DEFAULT_NUMBER_DIVISOR_M == 2.0 and DEFAULT_BRANCH_NUMBER_DIVISOR_M == 20.0
    assert numbering._house_number_from_distance(0.0, 2.0, "left") == 1
    assert numbering._house_number_from_distance(0.0, 2.0, "right") == 2
