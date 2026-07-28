"""Tests for street_editor.build_street_editor_map/apply_street_edits."""

import csv
import os

import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString

from osmaddresssystem import pipeline, street_direction, street_editor, street_ids
from osmaddresssystem.synthetic_grid import build_idealized_grid


def _labeled_and_directed(G, origin=(0.0, 0.0), **kwargs):
    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(G, node_cluster_m=0)
    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf, edges_gdf, streets_df, origin=origin, **kwargs
    )
    return nodes_gdf, edges_gdf, streets_df


def test_build_street_editor_map_writes_html(tmp_path):
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    assert os.path.exists(out_path)
    content = open(out_path).read()
    assert "osmStreetEditorExportCsv" in content
    assert "street_id" in content


def test_build_street_editor_map_uses_google_hybrid_only(tmp_path):
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    fmap = street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    content = open(out_path).read()
    assert "mt1.google.com/vt/lyrs=y" in content
    assert "openstreetmap.org" not in content.lower()

    map_children = list(fmap._children.values())
    tile_layer_children = [
        c for c in map_children if c.__class__.__name__ == "TileLayer"
    ]
    assert len(tile_layer_children) == 1
    layer = tile_layer_children[0]
    assert layer.overlay is False
    assert layer.control is False


def test_popup_html_shows_readonly_highway_and_rank():
    html = street_editor._popup_html(
        street_id=5, street_name="Main St", highway="primary", street_rank=1
    )
    assert "primary 1" in html
    # Read-only: the highway/rank text isn't inside an <input> value.
    import re

    input_values = re.findall(r'<input[^>]*value="([^"]*)"', html)
    assert "primary 1" not in input_values


def test_popup_html_handles_missing_highway_rank():
    html = street_editor._popup_html(street_id=5, street_name=None, highway=None, street_rank=None)
    assert "unknown" in html


def test_build_street_editor_map_raises_on_empty_streets(tmp_path):
    import geopandas as gpd

    empty = gpd.GeoDataFrame(
        {"street_id": [], "street_name": []}, geometry=[], crs="EPSG:3857"
    )
    with pytest.raises(ValueError):
        street_editor.build_street_editor_map(empty, empty, str(tmp_path / "out.html"))


def test_build_street_editor_map_includes_edge_editor_ui(tmp_path):
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    content = open(out_path).read()
    assert "osmEditorToggleMode" in content
    assert "osmEdgeEditorToggle" in content
    assert "osmEdgeEditorStreetIdBox" in content
    assert "edge_edits.csv" in content
    assert "__edgesData" in content

    # osmEditorSetMode must reference the real streets/edges FeatureGroup
    # and map Leaflet variable names, not a leftover, never-substituted
    # placeholder token - regression test for a bug where the mode
    # toggle button silently did nothing because window["{{...}}"]
    # never resolved (an f-string collapses "{{x}}" to "{x}" before the
    # later literal ".replace(\"{{x}}\", ...)" call ever runs, so the
    # replacement never matched anything).
    assert "__OSM_STREETS_GROUP_VAR__" not in content
    assert "__OSM_EDGES_GROUP_VAR__" not in content
    assert "__OSM_MAP_VAR__" not in content

    # Searchable dropdowns and the Reset/Create-new buttons, not the old
    # free-text-only boxes or the removed Show button.
    assert "osmEdgeEditorRenderDropdown" in content
    assert "osmEdgeEditorIdDropdown" in content
    assert "osmEdgeEditorNameDropdown" in content
    assert "osmEdgeEditorReset" in content
    assert "osmEdgeEditorCreateNew" in content
    assert "osmEdgeEditorCommitTarget" in content
    assert "osmEdgeEditorShow" not in content
    assert ">Show<" not in content
    # The old immediate-recolor-on-keystroke handler must be gone -
    # typing only filters the dropdown now, it never commits a target.
    assert "osmEdgeEditorOnTargetChange" not in content
    import re

    assert re.search(r'var streetsGroup = window\["feature_group_[0-9a-f]+"\]', content)
    assert re.search(r'var edgesGroup = window\["feature_group_[0-9a-f]+"\]', content)
    assert re.search(r'var map = window\["map_[0-9a-f]+"\]', content)


def test_apply_street_edits_with_edge_level_override(tmp_path):
    G = build_idealized_grid(rows=3, cols=3, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    ids = streets_df["street_id"].tolist()
    source_id, target_id = ids[0], ids[1]
    target_name = streets_df.loc[streets_df["street_id"] == target_id, "street_name"].iloc[0]

    # Pick one edge that currently belongs to source_id and reassign it,
    # at the edge level, to target_id - without touching source_id's
    # other edges via any street-level edit.
    source_edges = edges_gdf[edges_gdf["street_id"] == source_id].reset_index()
    u, v, key = source_edges.iloc[0][["u", "v", "key"]]

    streets_csv_path = tmp_path / "streets.csv"
    with open(streets_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_street_id", "new_street_id", "street_name"])
        # No real street-level edits - just the unedited passthrough.

    edge_edits_csv_path = tmp_path / "edge_edits.csv"
    with open(edge_edits_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["u", "v", "key", "new_street_id", "street_name"])
        writer.writerow([u, v, key, target_id, target_name])

    new_nodes, new_edges, new_streets = street_editor.apply_street_edits(
        nodes_gdf, edges_gdf, streets_df, str(streets_csv_path),
        edge_edits_csv_path=str(edge_edits_csv_path), origin=(0.0, 0.0),
    )

    moved_row = new_edges.loc[(u, v, key)]
    assert moved_row["street_id"] == target_id
    assert moved_row["street_name"] == target_name

    # A different physical edge (not this one or its reverse-direction
    # copy) that used to share source_id is untouched, proving the
    # reassignment was scoped to this one edge, not the whole street_id.
    edited_pair = frozenset((u, v))
    other_physical_edges = new_edges.reset_index()
    other_physical_edges = other_physical_edges[
        other_physical_edges.apply(lambda r: frozenset((r["u"], r["v"])), axis=1) != edited_pair
    ]
    assert (other_physical_edges["street_id"] == source_id).any()


def test_apply_street_edits_edge_level_unassign(tmp_path):
    G = build_idealized_grid(rows=3, cols=3, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    some_id = streets_df["street_id"].iloc[0]
    some_edges = edges_gdf[edges_gdf["street_id"] == some_id].reset_index()
    # Pick two genuinely different physical edges (different (u, v) pairs,
    # not just the reverse-direction copy of the same one - both directed
    # copies of an edited edge are meant to move together).
    distinct_pairs = some_edges[["u", "v"]].apply(lambda r: frozenset(r), axis=1)
    first_pair = distinct_pairs.iloc[0]
    other_idx = distinct_pairs[distinct_pairs != first_pair].index
    assert len(other_idx) > 0, "fixture needs a street with more than one physical edge"

    u, v, key = some_edges.iloc[0][["u", "v", "key"]]
    u2, v2, key2 = some_edges.loc[other_idx[0], ["u", "v", "key"]]

    streets_csv_path = tmp_path / "streets.csv"
    with open(streets_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_street_id", "new_street_id", "street_name"])

    edge_edits_csv_path = tmp_path / "edge_edits.csv"
    with open(edge_edits_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["u", "v", "key", "new_street_id", "street_name"])
        writer.writerow([u, v, key, "None", ""])

    new_nodes, new_edges, new_streets = street_editor.apply_street_edits(
        nodes_gdf, edges_gdf, streets_df, str(streets_csv_path),
        edge_edits_csv_path=str(edge_edits_csv_path), origin=(0.0, 0.0),
    )

    assert pd.isna(new_edges.loc[(u, v, key), "street_id"])
    # A different physical edge of that same street keeps its id.
    assert new_edges.loc[(u2, v2, key2), "street_id"] == some_id


def test_apply_street_edits_edge_level_new_street_gets_a_streets_row(tmp_path):
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    some_id = streets_df["street_id"].iloc[0]
    some_edges = edges_gdf[edges_gdf["street_id"] == some_id].reset_index()
    u, v, key = some_edges.iloc[0][["u", "v", "key"]]

    brand_new_id = str(int(streets_df["street_id"].max()) + 1000)

    streets_csv_path = tmp_path / "streets.csv"
    with open(streets_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_street_id", "new_street_id", "street_name"])

    edge_edits_csv_path = tmp_path / "edge_edits.csv"
    with open(edge_edits_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["u", "v", "key", "new_street_id", "street_name"])
        writer.writerow([u, v, key, brand_new_id, "Brand New Street"])

    new_nodes, new_edges, new_streets = street_editor.apply_street_edits(
        nodes_gdf, edges_gdf, streets_df, str(streets_csv_path),
        edge_edits_csv_path=str(edge_edits_csv_path), origin=(0.0, 0.0),
    )

    assert int(brand_new_id) in set(new_streets["street_id"])
    row = new_streets[new_streets["street_id"] == int(brand_new_id)].iloc[0]
    assert row["street_name"] == "Brand New Street"
    assert new_edges.loc[(u, v, key), "street_id"] == int(brand_new_id)


def test_apply_street_edits_renames_a_street(tmp_path):
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    some_id = streets_df["street_id"].iloc[0]

    csv_path = tmp_path / "edits.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_street_id", "new_street_id", "street_name"])
        writer.writerow([some_id, some_id, "Renamed Street"])

    new_nodes, new_edges, new_streets = street_editor.apply_street_edits(
        nodes_gdf, edges_gdf, streets_df, str(csv_path), origin=(0.0, 0.0)
    )

    matching = new_streets[new_streets["street_id"] == some_id]
    assert len(matching) == 1
    assert matching.iloc[0]["street_name"] == "Renamed Street"

    matching_edges = new_edges[new_edges["street_id"] == some_id]
    assert (matching_edges["street_name"] == "Renamed Street").all()

    # Numbering was recomputed: house numbers still present on the
    # renamed street's main-path edges.
    assert matching_edges.loc[matching_edges["is_main_direction"], "street_start_length_m"].notna().all()


def test_apply_street_edits_unassigns_street_when_new_id_is_none(tmp_path):
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    some_id = streets_df["street_id"].iloc[0]

    csv_path = tmp_path / "edits.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_street_id", "new_street_id", "street_name"])
        writer.writerow([some_id, "None", ""])

    new_nodes, new_edges, new_streets = street_editor.apply_street_edits(
        nodes_gdf, edges_gdf, streets_df, str(csv_path), origin=(0.0, 0.0)
    )

    assert some_id not in set(new_streets["street_id"])
    assert new_edges[new_edges["street_id"] == some_id].empty


def _add_edge(G, u, v, uxy, vxy, highway="residential", name=None, osmid=1, key=0):
    G.add_node(u, x=uxy[0], y=uxy[1])
    G.add_node(v, x=vxy[0], y=vxy[1])
    fwd = LineString([uxy, vxy])
    bwd = LineString([vxy, uxy])
    G.add_edge(u, v, key, highway=highway, name=name, geometry=fwd, length=fwd.length, osmid=osmid)
    G.add_edge(v, u, key, highway=highway, name=name, geometry=bwd, length=bwd.length, osmid=osmid)


def test_apply_street_edits_recomputes_main_path_across_a_hand_merged_intersection(tmp_path):
    """A hand-picked edge_edits.csv merging two edges into a BRAND-NEW
    street_id spanning a real clustered intersection (two distinct raw
    osmids 1m apart, well within the default node_cluster_m) must, after
    ``apply_street_edits``, have a correct, complete recomputed main
    path for that new street_id - not just a naive street_id/name
    pass-through.

    This is a regression test for the bug this task fixes: the OLD
    ``apply_street_edits`` fed the remapped edges_gdf straight into
    ``street_direction.assign_street_directionality`` without ever
    calling ``street_ids.recompute_main_paths``. That function only
    WALKS edges already flagged ``is_main_direction=True`` - it never
    sets that flag for a brand-new street_id, never synthesizes the
    connector edge needed to bridge the two distinct osmids at the
    intersection, and never reorients an edge stored backwards relative
    to the new path. So the OLD behavior would leave the new street_id's
    edges with ``is_main_direction=False`` (whatever it defaulted to
    for a street_id nothing ever set it for) and no synthetic connector
    at all - both directly contradicted by the assertions below.
    """
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node(10, x=0, y=0)
    G.add_node(11, x=1, y=0)  # 1m from node 10: within the default cluster
    _add_edge(G, 1, 10, (-100, 0), (0, 0), highway="residential", name="North Arm", osmid=101)
    _add_edge(G, 11, 2, (1, 0), (100, 0), highway="residential", name="South Arm", osmid=102)
    # An unrelated crossing street through the same clustered
    # intersection, so it isn't the only thing ever assigned a street_id
    # there.
    _add_edge(G, 10, 20, (0, 0), (0, 100), highway="residential", name="Cross St", osmid=103)
    _add_edge(G, 11, 21, (1, 0), (0, -100), highway="residential", name="Cross St", osmid=104)

    # Deliberately NOT ``_labeled_and_directed`` (hardcodes
    # ``node_cluster_m=0``) - this fixture needs the DEFAULT node
    # clustering so raw nodes 10/11 (1m apart) actually cluster into one
    # real intersection.
    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(G)
    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf, edges_gdf, streets_df, origin=(0.0, 0.0)
    )

    north_id = edges_gdf.loc[(1, 10, 0), "street_id"]
    south_id = edges_gdf.loc[(11, 2, 0), "street_id"]
    assert north_id != south_id  # separate streets before the hand-edit

    new_street_id = str(int(streets_df["street_id"].max()) + 1000)

    streets_csv_path = tmp_path / "streets.csv"
    with open(streets_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_street_id", "new_street_id", "street_name"])

    edge_edits_csv_path = tmp_path / "edge_edits.csv"
    with open(edge_edits_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["u", "v", "key", "new_street_id", "street_name"])
        writer.writerow([1, 10, 0, new_street_id, "Hand-Merged Ave"])
        writer.writerow([11, 2, 0, new_street_id, "Hand-Merged Ave"])

    new_nodes, new_edges, new_streets = street_editor.apply_street_edits(
        nodes_gdf, edges_gdf, streets_df, str(streets_csv_path),
        edge_edits_csv_path=str(edge_edits_csv_path), origin=(0.0, 0.0),
    )

    merged = new_edges[new_edges["street_id"] == int(new_street_id)]
    assert not merged.empty

    # The two hand-picked arms must be on the main path.
    assert bool(merged.loc[(1, 10, 0), "is_main_direction"]) is True
    assert bool(merged.loc[(11, 2, 0), "is_main_direction"]) is True

    # A brand-new synthetic connector bridging osmids 10 and 11 must
    # exist under this new street_id - the naive old behavior never
    # created one.
    synthetic = merged[merged.index.get_level_values("key") >= street_ids._SYNTHETIC_CONNECTOR_KEY_OFFSET]
    assert len(synthetic) == 1
    syn_u, syn_v, _syn_key = synthetic.index[0]
    assert {syn_u, syn_v} == {10, 11}
    assert bool(synthetic.iloc[0]["is_main_direction"]) is True

    # Numbering was recomputed on top of the freshly recomputed main
    # path - every main-path edge of the new street has a house number.
    assert merged.loc[merged["is_main_direction"], "street_start_length_m"].notna().all()


def test_edge_editor_map_works_in_a_real_browser(tmp_path):
    """Real-browser regression test for a class of bug string-content
    assertions can't catch: the edge click bindings used to be added via
    fmap.get_root().html.add_child(...), which renders EARLIER in the
    page than folium's own `var poly_line_xxx = L.polyline(...)`
    declarations (those live in the root Figure's own .script section,
    rendered later). That left every click binding throwing "poly_line_xxx
    is not defined" at load time, silently aborting the whole script tag
    and leaving window.__edgeLayersByKey permanently empty - so every
    edge was unclickable and osmEdgeEditorRestyleAll() had nothing to
    style. No amount of checking the HTML's own text content would catch
    this, since the broken and the fixed version contain the exact same
    source text - only actually running it in a browser does. Deferring
    the whole binding script to DOMContentLoaded fixed it.
    """
    playwright_sync_api = pytest.importorskip("playwright.sync_api")

    G = build_idealized_grid(rows=3, cols=3, spacing=100.0, named_row=1, named_row_name="Elm Street", primary_row=0)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    try:
        with playwright_sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            page.click("#osmEditorModeButton")
            page.wait_for_timeout(200)

            assert errors == [], f"JS errors on load/toggle: {errors}"

            layer_count = page.evaluate("Object.keys(window.__edgeLayersByKey).length")
            assert layer_count == len(edges_gdf)

            # Clicking a currently-colored (main-path) edge with no
            # target selected must select its own street as the target -
            # this only works at all if the click binding actually fired.
            first_key = page.evaluate("Object.keys(window.__edgeLayersByKey)[0]")
            page.evaluate(f"window.__edgeLayersByKey['{first_key}'].fire('click')")
            page.wait_for_timeout(100)
            target = page.evaluate("window.__osmEdgeEditorTarget")
            assert target is not None

            browser.close()
    except Exception as exc:  # pragma: no cover - environment without a browser installed
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_edge_editor_dropdown_opens_on_empty_focus_and_name_box_stays_empty(tmp_path):
    """The Street ID/name boxes must open their dropdown showing every
    known street on focus alone (no typing needed), and must never show
    a stale pre-filled name - regression test for both bugs at once."""
    playwright_sync_api = pytest.importorskip("playwright.sync_api")

    G = build_idealized_grid(rows=3, cols=3, spacing=100.0, named_row=1, named_row_name="Elm Street", primary_row=0)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    try:
        with playwright_sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)
            page.click("#osmEditorModeButton")
            page.wait_for_timeout(200)

            name_value = page.eval_on_selector("#osmEdgeEditorStreetNameBox", "el => el.value")
            assert name_value == ""

            page.click("#osmEdgeEditorStreetIdBox")
            page.wait_for_timeout(100)
            entries = page.eval_on_selector("#osmEdgeEditorIdDropdown", "el => el.children.length")
            assert entries == len(streets_df)

            browser.close()
    except Exception as exc:  # pragma: no cover - environment without a browser installed
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_edge_editor_reset_button_reverts_whole_street(tmp_path):
    """The Reset button reverts every edit made to the CURRENTLY
    SELECTED target street this session - both edges added to it and
    edges removed from it - back to their own original state, and
    leaves a brand-new street_id (via Create new street) with zero
    members again, matching its own real "original" (nonexistent)
    state."""
    playwright_sync_api = pytest.importorskip("playwright.sync_api")

    G = build_idealized_grid(rows=3, cols=3, spacing=100.0, named_row=1, named_row_name="Elm Street", primary_row=0)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    try:
        with playwright_sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)
            page.click("#osmEditorModeButton")
            page.wait_for_timeout(200)

            assert "osmEdgeEditorShow" not in page.content()

            # Select an existing street as the target, add one
            # black-dotted edge to it, then Reset - the added edge must
            # revert to black-dotted again.
            colored_key = page.evaluate("""
            () => {
                for (var k in window.__edgeLayersByKey) {
                    if (window.__edgeLayersByKey[k].options.weight === 7) return k;
                }
                return null;
            }
            """)
            page.evaluate(f"window.__edgeLayersByKey['{colored_key}'].fire('click')")
            page.wait_for_timeout(100)
            target_before = page.evaluate("window.__osmEdgeEditorTarget")
            assert target_before is not None

            dotted_key = page.evaluate("""
            () => {
                for (var k in window.__edgeLayersByKey) {
                    if (window.__edgeLayersByKey[k].options.weight === 4) return k;
                }
                return null;
            }
            """)
            page.evaluate(f"window.__edgeLayersByKey['{dotted_key}'].fire('click')")
            page.wait_for_timeout(100)
            added_weight = page.evaluate(f"window.__edgeLayersByKey['{dotted_key}'].options.weight")
            assert added_weight == 7

            page.click("button[onclick='osmEdgeEditorReset()']")
            page.wait_for_timeout(100)
            reverted_weight = page.evaluate(f"window.__edgeLayersByKey['{dotted_key}'].options.weight")
            assert reverted_weight == 4
            assert page.evaluate("Object.keys(window.__edgeEdits).length") == 0

            # Create a brand-new street, add an edge to it, then Reset -
            # it must go back to zero members (its own real "original").
            page.click("button[onclick=\"osmEdgeEditorCreateNew()\"]")
            page.wait_for_timeout(100)
            new_target = page.evaluate("window.__osmEdgeEditorTarget")
            assert new_target["name"] == ""

            some_key = page.evaluate("Object.keys(window.__edgeLayersByKey)[0]")
            page.evaluate(f"window.__edgeLayersByKey['{some_key}'].fire('click')")
            page.wait_for_timeout(100)
            assert page.evaluate("Object.keys(window.__edgeEdits).length") == 1

            page.click("button[onclick='osmEdgeEditorReset()']")
            page.wait_for_timeout(100)
            assert page.evaluate("Object.keys(window.__edgeEdits).length") == 0

            browser.close()
    except Exception as exc:  # pragma: no cover - environment without a browser installed
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise
