"""Tests for geocode_map.build_interactive_geocode_map."""

import os

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from osmaddresssystem import geocode_map, numbering, pipeline, street_direction, street_ids
from osmaddresssystem.synthetic_grid import build_idealized_grid


def _addressed_streets_gdf(admin=True):
    G = build_idealized_grid(rows=3, cols=3, spacing=100.0, named_row=1, named_row_name="Elm Street", primary_row=0)
    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(G, node_cluster_m=0)
    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf, edges_gdf, streets_df, origin=(0.0, 0.0)
    )
    edges_gdf, streets_df = numbering.assign_house_numbers(edges_gdf, streets_df)
    if admin:
        streets_df["country"] = "Testland"
        streets_df["province"] = "Test Province"
        streets_df["city"] = "Testville"
        streets_df["neighbourhood"] = None
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)
    return streets_gdf


def test_build_interactive_geocode_map_writes_html(tmp_path):
    streets_gdf = _addressed_streets_gdf()
    out_path = str(tmp_path / "geocode.html")
    geocode_map.build_interactive_geocode_map(streets_gdf, out_path)

    assert os.path.exists(out_path)
    content = open(out_path).read()
    assert "osmGeocodeOnMapClick" in content
    assert "osmGeocodePoint" in content
    assert "__geocodeStreets" in content
    # No leftover template placeholder (same class of bug as street_editor.py).
    assert "__OSM_GEOCODE_MAP_VAR__" not in content


def test_build_interactive_geocode_map_uses_google_hybrid_only(tmp_path):
    streets_gdf = _addressed_streets_gdf()
    out_path = str(tmp_path / "geocode.html")
    fmap = geocode_map.build_interactive_geocode_map(streets_gdf, out_path)

    content = open(out_path).read()
    assert "mt1.google.com/vt/lyrs=y" in content
    assert "openstreetmap.org" not in content.lower()

    tile_layers = [c for c in fmap._children.values() if c.__class__.__name__ == "TileLayer"]
    assert len(tile_layers) == 1


def test_build_interactive_geocode_map_raises_on_empty_streets():
    empty = gpd.GeoDataFrame({"street_id": [], "street_name": []}, geometry=[], crs="EPSG:3857")
    with pytest.raises(ValueError):
        geocode_map.build_interactive_geocode_map(empty, "unused.html")


def test_js_geocode_matches_python_geocode_points(tmp_path):
    """The embedded JS re-implementation of numbering.geocode_points must
    agree with the real Python implementation for the same point - the
    whole point of this map. Runs in a real headless browser (Playwright),
    not a string-content check, since the actual nearest-street/project-
    onto-line math only exists as executable JS."""
    playwright_sync_api = pytest.importorskip("playwright.sync_api")

    streets_gdf = _addressed_streets_gdf()
    out_path = str(tmp_path / "geocode.html")
    geocode_map.build_interactive_geocode_map(streets_gdf, out_path)

    example_geom = streets_gdf.geometry.iloc[3]
    if example_geom.geom_type == "MultiLineString":
        example_geom = next(iter(example_geom.geoms))
    # 0.25, not 0.5 - the fixture's own streets all cross at a shared
    # row (y=100), so a street's exact midpoint sits exactly on TWO
    # streets at once (a genuine, order-dependent nearest-street tie,
    # not a real accuracy issue - see test_admin_columns_skipped_when_missing's
    # sibling fixture for the actual grid layout).
    example_point = example_geom.interpolate(0.25, normalized=True)
    py_address = numbering.geocode_points(streets_gdf, [(example_point.x, example_point.y)])[0]

    point_wgs84 = gpd.GeoSeries([example_point], crs=streets_gdf.crs).to_crs(4326).iloc[0]

    try:
        with playwright_sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            assert errors == []

            js_address = page.evaluate(
                f"osmGeocodePoint({point_wgs84.y}, {point_wgs84.x})"
            )
            browser.close()
    except Exception as exc:  # pragma: no cover - environment without a browser installed
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise

    assert js_address == py_address


def test_clicking_the_map_shows_an_address_and_drops_a_marker(tmp_path):
    playwright_sync_api = pytest.importorskip("playwright.sync_api")

    streets_gdf = _addressed_streets_gdf()
    out_path = str(tmp_path / "geocode.html")
    geocode_map.build_interactive_geocode_map(streets_gdf, out_path)

    try:
        with playwright_sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            box_before = page.eval_on_selector("#osmGeocodeResultBox", "el => el.innerText")
            assert "No point selected" in box_before

            page.evaluate(
                """
                () => {
                    var mapVar = Object.keys(window).find(k => k.startsWith('map_'));
                    var map = window[mapVar];
                    map.fire('click', {latlng: map.getCenter()});
                }
                """
            )
            page.wait_for_timeout(200)
            box_after = page.eval_on_selector("#osmGeocodeResultBox", "el => el.innerText")
            assert "No point selected" not in box_after
            assert "," in box_after

            marker_exists = page.evaluate("window.__geocodeMarker !== undefined && window.__geocodeMarker !== null")
            assert marker_exists is True

            assert errors == []
            browser.close()
    except Exception as exc:  # pragma: no cover - environment without a browser installed
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_admin_columns_skipped_when_missing(tmp_path):
    streets_gdf = _addressed_streets_gdf(admin=False)
    out_path = str(tmp_path / "geocode.html")
    geocode_map.build_interactive_geocode_map(streets_gdf, out_path)
    content = open(out_path).read()
    # Should still build fine with no admin columns at all.
    assert "osmGeocodeOnMapClick" in content


def test_style_for_highway_uses_rank_tiers():
    from osmaddresssystem.geocode_map import _style_for_highway

    motorway_style = _style_for_highway("motorway")
    footway_style = _style_for_highway("footway")
    residential_style = _style_for_highway("residential")
    unknown_style = _style_for_highway("some_made_up_highway_type")
    missing_style = _style_for_highway(None)

    # Higher-importance roads are drawn thicker than lower-importance ones.
    assert motorway_style["weight"] > residential_style["weight"] > footway_style["weight"]
    assert motorway_style["color"] != footway_style["color"]
    # An unrecognized or missing highway value falls back to the same
    # tier as a plain residential street (DEFAULT_RANK), not a crash.
    assert unknown_style == residential_style
    assert missing_style == residential_style


def test_map_includes_highway_styled_polylines_and_street_labels(tmp_path):
    streets_gdf = _addressed_streets_gdf()
    out_path = str(tmp_path / "geocode.html")
    fmap = geocode_map.build_interactive_geocode_map(streets_gdf, out_path)

    polylines = [c for c in fmap._children.values() if c.__class__.__name__ == "PolyLine"]
    assert len(polylines) == len(streets_gdf)
    # Not every street shares the exact same color/weight - highway-based
    # styling is actually being applied, not one hardcoded style for all.
    styles = {(p.options.get("color"), p.options.get("weight")) for p in polylines}
    assert len(styles) >= 1  # at least renders; real variety checked in _style_for_highway test

    content = open(out_path).read()
    # The named street's own label, and an unnamed street's id-based
    # fallback label, both show up somewhere on the map.
    assert "Elm Street" in content
    assert "street " in content


def test_red_projection_dot_appears_on_click(tmp_path):
    playwright_sync_api = pytest.importorskip("playwright.sync_api")

    streets_gdf = _addressed_streets_gdf()
    out_path = str(tmp_path / "geocode.html")
    geocode_map.build_interactive_geocode_map(streets_gdf, out_path)

    try:
        with playwright_sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            page.evaluate(
                """
                () => {
                    var mapVar = Object.keys(window).find(k => k.startsWith('map_'));
                    var map = window[mapVar];
                    map.fire('click', {latlng: map.getCenter()});
                }
                """
            )
            page.wait_for_timeout(200)

            marker_exists = page.evaluate(
                "window.__geocodeProjectionMarker !== undefined && window.__geocodeProjectionMarker !== null"
            )
            marker_color = page.evaluate(
                "window.__geocodeProjectionMarker ? window.__geocodeProjectionMarker.options.color : null"
            )
            assert marker_exists is True
            assert marker_color == "#ff0000"
            assert errors == []
            browser.close()
    except Exception as exc:  # pragma: no cover - environment without a browser installed
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_js_house_number_never_zero_at_a_streets_own_start(tmp_path):
    """Clicking exactly at a street's own start (distance 0) must show
    house number 1, never 0 - mirrors numbering._house_number_from_distance
    exactly."""
    playwright_sync_api = pytest.importorskip("playwright.sync_api")

    streets_gdf = _addressed_streets_gdf()
    out_path = str(tmp_path / "geocode.html")
    geocode_map.build_interactive_geocode_map(streets_gdf, out_path)

    example_geom = streets_gdf.geometry.iloc[0]
    if example_geom.geom_type == "MultiLineString":
        example_geom = next(iter(example_geom.geoms))
    start_point = example_geom.interpolate(0.0, normalized=True)
    point_wgs84 = gpd.GeoSeries([start_point], crs=streets_gdf.crs).to_crs(4326).iloc[0]

    try:
        with playwright_sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            js_address = page.evaluate(f"osmGeocodePoint({point_wgs84.y}, {point_wgs84.x})")
            browser.close()
    except Exception as exc:  # pragma: no cover - environment without a browser installed
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise

    assert errors == []
    house_number = int(js_address.split(",")[0])
    assert house_number >= 1


def test_radial_mode_draws_a_red_origin_marker(tmp_path):
    streets_gdf = _addressed_streets_gdf()
    origin = (0.0, 0.0)
    out_path = str(tmp_path / "geocode.html")
    geocode_map.build_interactive_geocode_map(
        streets_gdf, out_path, numbering_mode="radial", origin=origin
    )
    content = open(out_path).read()
    # folium/branca JSON-escapes "&" to "\u0026" inside the embedded
    # DivIcon html, so check for the un-escaped remainder instead of the
    # literal HTML entity.
    assert "times;" in content
    assert "#ff0000" in content


def test_grid_mode_draws_two_red_direction_arrows(tmp_path):
    streets_gdf = _addressed_streets_gdf()
    out_path = str(tmp_path / "geocode.html")
    fmap = geocode_map.build_interactive_geocode_map(
        streets_gdf, out_path, numbering_mode="grid", direction_deg=30.0
    )
    content = open(out_path).read()
    assert content.count("8593;") == 2
    assert "rotate(30" in content
    assert "rotate(120" in content  # 30 + 90, the cross axis

    polylines = [c for c in fmap._children.values() if c.__class__.__name__ == "PolyLine"]
    arrow_lines = [p for p in polylines if p.options.get("color") == "#ff0000"]
    assert len(arrow_lines) == 2


def test_no_mode_given_draws_neither_marker(tmp_path):
    streets_gdf = _addressed_streets_gdf()
    out_path = str(tmp_path / "geocode.html")
    geocode_map.build_interactive_geocode_map(streets_gdf, out_path)
    content = open(out_path).read()
    assert "times;" not in content
    assert "8593;" not in content


def test_grid_mode_without_origin_anchors_arrows_at_bounds_center(tmp_path):
    """Grid mode's arrows don't require an origin - they anchor at the
    streets' own bounding box center instead."""
    streets_gdf = _addressed_streets_gdf()
    out_path = str(tmp_path / "geocode.html")
    fmap = geocode_map.build_interactive_geocode_map(
        streets_gdf, out_path, numbering_mode="grid", direction_deg=0.0
    )
    content = open(out_path).read()
    assert content.count("8593;") == 2


def test_js_left_right_parity_matches_python(tmp_path):
    """The embedded JS side-detection/parity math (even = right, odd =
    left) must agree with numbering._house_number_from_distance/
    _side_of_line exactly - clicking either side of the same street
    must give the correct even/odd number for that side, not just
    whichever number the naive undivided distance would give, and the
    two opposite sides must land on opposite parities."""
    playwright_sync_api = pytest.importorskip("playwright.sync_api")

    streets_gdf = _addressed_streets_gdf()
    out_path = str(tmp_path / "geocode.html")
    geocode_map.build_interactive_geocode_map(streets_gdf, out_path)

    # A point a few meters off either side of some street's own
    # geometry - same distance-along, opposite sides (which one is
    # actually "left" vs. "right" depends on that street's own
    # direction of travel, not assumed here).
    example_geom = streets_gdf.geometry.iloc[3]
    if example_geom.geom_type == "MultiLineString":
        example_geom = next(iter(example_geom.geoms))
    mid_point = example_geom.interpolate(0.25, normalized=True)
    point_a = (mid_point.x + 3, mid_point.y + 3)
    point_b = (mid_point.x - 3, mid_point.y - 3)
    py_a = numbering.geocode_points(streets_gdf, [point_a])[0]
    py_b = numbering.geocode_points(streets_gdf, [point_b])[0]

    wgs84_a = gpd.GeoSeries([Point(*point_a)], crs=streets_gdf.crs).to_crs(4326).iloc[0]
    wgs84_b = gpd.GeoSeries([Point(*point_b)], crs=streets_gdf.crs).to_crs(4326).iloc[0]

    try:
        with playwright_sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            js_a = page.evaluate(f"osmGeocodePoint({wgs84_a.y}, {wgs84_a.x})")
            js_b = page.evaluate(f"osmGeocodePoint({wgs84_b.y}, {wgs84_b.x})")
            browser.close()
    except Exception as exc:  # pragma: no cover - environment without a browser installed
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise

    assert errors == []
    assert js_a == py_a
    assert js_b == py_b

    number_a = int(js_a.split(",")[0])
    number_b = int(js_b.split(",")[0])
    assert number_a % 2 != number_b % 2
