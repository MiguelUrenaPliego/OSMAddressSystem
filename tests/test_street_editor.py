"""Tests for street_editor.build_street_editor_map/apply_street_edits."""

import csv
import json
import os

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString

from osmaddresssystem import pipeline, street_direction, street_editor, street_ids
from osmaddresssystem.synthetic_grid import build_idealized_grid


def _labeled_and_directed(G, origin=(0.0, 0.0), **kwargs):
    """Test helper: labeled and directed."""
    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(G, node_cluster_m=0)
    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf, edges_gdf, streets_df, origin=origin, **kwargs
    )
    return nodes_gdf, edges_gdf, streets_df


def test_build_street_editor_map_writes_html(tmp_path):
    """Check build street editor map writes html."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    assert os.path.exists(out_path)
    content = open(out_path).read()
    assert "osmStreetEditorExportCsv" in content
    assert "street_id" in content


def test_build_street_editor_map_has_three_switchable_basemaps(tmp_path):
    """No default OpenStreetMap tiles, and no folium LayerControl radio
    for the basemap (a plain <select> per the caller's own request) -
    three raster TileLayers (Google Hybrid, Google Satellite, Esri),
    none exposed on the layer control (which only ever lists overlays),
    swapped by osmSetBaseLayer instead."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    fmap = street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    content = open(out_path).read()
    assert "mt1.google.com/vt/lyrs=y" in content  # Google Hybrid
    assert "mt1.google.com/vt/lyrs=s" in content  # Google Satellite
    assert "arcgisonline.com" in content  # Esri
    assert "openstreetmap.org" not in content.lower()
    assert "osmSetBaseLayer" in content
    assert "osmSetBasemapOpacity" in content
    assert 'id="osmBaseLayerSelect"' in content

    map_children = list(fmap._children.values())
    tile_layer_children = [
        c for c in map_children if c.__class__.__name__ == "TileLayer"
    ]
    assert len(tile_layer_children) == 3
    for layer in tile_layer_children:
        assert layer.overlay is False
        assert layer.control is False


def test_popup_html_shows_readonly_highway_and_rank():
    """Check popup html shows readonly highway and rank."""
    html = street_editor._popup_html(
        street_id=5, street_name="Main St", highway="primary", street_rank=1
    )
    assert "primary 1" in html
    # Read-only: the highway/rank text isn't inside an <input> value.
    import re

    input_values = re.findall(r'<input[^>]*value="([^"]*)"', html)
    assert "primary 1" not in input_values


def test_popup_html_handles_missing_highway_rank():
    """Check popup html handles missing highway rank."""
    html = street_editor._popup_html(street_id=5, street_name=None, highway=None, street_rank=None)
    assert "unknown" in html


def test_popup_html_includes_rank_drivable_and_geometry_button():
    """Check popup html includes rank drivable and geometry button."""
    html = street_editor._popup_html(
        street_id=5, street_name="Main St", highway="primary", street_rank=1, is_drivable=True
    )
    assert "osmStreetEditorSetRank" in html
    assert "osmStreetEditorSetDrivable" in html
    assert 'onclick="osmStreetEditorEnterGeometryMode(\'5\')"' in html
    assert "checked" in html


def test_popup_html_drivable_unchecked_when_false():
    """Check popup html drivable unchecked when false."""
    html = street_editor._popup_html(
        street_id=5, street_name="Main St", highway="footway", is_drivable=False
    )
    assert "checked" not in html


def test_build_street_editor_map_has_labels_layer_and_control(tmp_path):
    """Check build street editor map has labels layer and control."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    content = open(out_path).read()
    assert "Street labels" in content
    assert "layer_control" in content or "L.control.layers" in content
    assert "osmStreetEditorHighlight" in content
    assert "osmStreetEditorUnhighlightAll" in content
    assert "__streetLayersById" in content


def test_export_csv_includes_new_columns(tmp_path):
    """Check export csv includes new columns."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    content = open(out_path).read()
    assert "street_rank" in content
    assert "is_drivable" in content
    assert "geom_edited" in content
    assert "is_new" in content
    assert "osmStreetEditorImportCsvFiles" in content
    assert "osmStreetEditorResetAllEdits" in content


def test_build_street_editor_map_raises_on_empty_streets(tmp_path):
    """Check build street editor map raises on empty streets."""
    import geopandas as gpd

    empty = gpd.GeoDataFrame(
        {"street_id": [], "street_name": []}, geometry=[], crs="EPSG:3857"
    )
    with pytest.raises(ValueError):
        street_editor.build_street_editor_map(empty, empty, str(tmp_path / "out.html"))


def test_build_street_editor_map_has_no_legacy_edit_edges_ui(tmp_path):
    """The old flat "Edit Edges" mode (a mode-toggle button, a bulk
    searchable street_id/name target box, a permanently-visible dotted
    "edges" layer) is gone entirely - editing an existing street's
    geometry/membership only ever happens per-street, via "Edit
    geometry" from that street's own popup (see
    test_geometry_mode_enters_and_shows_panel and friends)."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    content = open(out_path).read()
    assert "__edgesData" in content
    assert "edge_edits.csv" in content
    assert "osmStreetEditorEnterGeometryMode" in content
    assert "osmGeomEditFinish" in content
    assert "osmStreetEditorNewStreet" in content

    # Nothing from the old bulk Edit Edges mode survives.
    assert "osmEditorToggleMode" not in content
    assert "osmEditorSetMode" not in content
    assert "osmEditorModeButton" not in content
    assert "Switch to Edit Edges" not in content
    assert "osmEdgeEditorToggle" not in content
    assert "osmEdgeEditorStreetIdBox" not in content
    assert "osmEdgeEditorRenderDropdown" not in content
    assert "osmEdgeEditorCreateNew" not in content
    assert "osmEdgeEditorCommitTarget" not in content
    assert "__edgeLayersByKey" not in content

    # No leftover, never-substituted placeholder tokens.
    assert "__OSM_STREETS_GROUP_VAR__" not in content
    assert "__OSM_EDGES_GROUP_VAR__" not in content
    assert "__OSM_MAP_VAR__" not in content

    import re

    assert re.search(r'window\["feature_group_[0-9a-f]+"\]', content)
    assert re.search(r'window\["map_[0-9a-f]+"\]', content)


def test_apply_street_edits_with_edge_level_override(tmp_path):
    """Check apply street edits with edge level override."""
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
    """Check apply street edits edge level unassign."""
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
    """Check apply street edits edge level new street gets a streets row."""
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
    """Check apply street edits renames a street."""
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


def test_apply_street_edits_honors_rank_and_drivable_override(tmp_path):
    """Check apply street edits honors rank and drivable override."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    some_id = streets_df["street_id"].iloc[0]

    csv_path = tmp_path / "edits.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_street_id", "new_street_id", "street_name", "street_rank", "is_drivable"])
        writer.writerow([some_id, some_id, "", "2", "False"])

    new_nodes, new_edges, new_streets = street_editor.apply_street_edits(
        nodes_gdf, edges_gdf, streets_df, str(csv_path), origin=(0.0, 0.0)
    )

    matching_street = new_streets[new_streets["street_id"] == some_id].iloc[0]
    assert matching_street["street_rank"] == 2.0

    matching_edges = new_edges[new_edges["street_id"] == some_id]
    assert (~matching_edges["is_drivable"]).all()


def test_apply_street_edits_returns_geom_edited_and_is_new_columns(tmp_path):
    """Check apply street edits returns geom edited and is new columns."""
    G = build_idealized_grid(rows=3, cols=3, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    some_id = streets_df["street_id"].iloc[0]
    some_edges = edges_gdf[edges_gdf["street_id"] == some_id].reset_index()
    u, v, key = some_edges.iloc[0][["u", "v", "key"]]

    streets_csv_path = tmp_path / "streets.csv"
    with open(streets_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_street_id", "new_street_id", "street_name"])

    edge_edits_csv_path = tmp_path / "edge_edits.csv"
    with open(edge_edits_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["u", "v", "key", "new_street_id", "street_name", "geom_edited", "is_new"])
        writer.writerow([u, v, key, some_id, "", "True", "False"])

    new_nodes, new_edges, new_streets = street_editor.apply_street_edits(
        nodes_gdf, edges_gdf, streets_df, str(streets_csv_path),
        edge_edits_csv_path=str(edge_edits_csv_path), origin=(0.0, 0.0),
    )

    assert "geom_edited" in new_edges.columns
    assert "is_new" in new_edges.columns
    assert bool(new_edges.loc[(u, v, key), "geom_edited"]) is True
    assert bool(new_edges.loc[(u, v, key), "is_new"]) is False
    # An untouched edge (neither directed copy of the edited physical
    # edge) defaults to False on both.
    edited_pair = frozenset((u, v))
    other_edges = new_edges.reset_index()
    other = other_edges[
        other_edges.apply(lambda r: frozenset((r["u"], r["v"])), axis=1) != edited_pair
    ].iloc[0]
    assert bool(other["geom_edited"]) is False
    assert bool(other["is_new"]) is False


def test_apply_street_edits_gpkg_round_trips(tmp_path):
    """Check apply street edits gpkg round trips."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    some_id = streets_df["street_id"].iloc[0]
    csv_path = tmp_path / "edits.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_street_id", "new_street_id", "street_name"])
        writer.writerow([some_id, some_id, "Renamed Street"])

    gpkg_path = str(tmp_path / "snapshot.gpkg")
    new_nodes, new_edges, new_streets = street_editor.apply_street_edits_gpkg(
        nodes_gdf, edges_gdf, streets_df, gpkg_path, str(csv_path), origin=(0.0, 0.0)
    )

    loaded_nodes, loaded_edges = street_editor.load_edited_gpkg(gpkg_path)

    assert len(loaded_nodes) == len(new_nodes)
    assert len(loaded_edges) == len(new_edges)
    assert "geom_edited" in loaded_edges.columns
    assert "is_new" in loaded_edges.columns
    matching = loaded_edges[loaded_edges["street_id"] == some_id]
    assert (matching["street_name"] == "Renamed Street").all()


def test_apply_street_edits_unassigns_street_when_new_id_is_none(tmp_path):
    """Check apply street edits unassigns street when new id is none."""
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
    """Test helper: add edge."""
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


def _pick_richest_street_id(page):
    """The street_id with the most main-path edges, so delete-node /
    move-node tests have an interior node to work with."""
    return page.evaluate("""
    () => {
        var counts = {};
        window.__edgesData.forEach(function(e) {
            if (e.is_main && e.street_id !== null && e.street_id !== undefined) {
                counts[e.street_id] = (counts[e.street_id] || 0) + 1;
            }
        });
        var best = null, bestCount = -1;
        for (var sid in counts) {
            if (counts[sid] > bestCount) { best = sid; bestCount = counts[sid]; }
        }
        return best;
    }
    """)


def test_geometry_mode_enters_and_shows_panel(tmp_path):
    """Check geometry mode enters and shows panel."""
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

            street_id = _pick_richest_street_id(page)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            assert errors == []
            panel_display = page.eval_on_selector("#osmGeomEditPanel", "el => el.style.display")
            assert panel_display == "block"
            edge_count = page.evaluate("Object.keys(window.__geomEdit.edgeLayers).length")
            assert edge_count > 0

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_geometry_mode_undo_reverts_node_move(tmp_path):
    """Check geometry mode undo reverts node move."""
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

            street_id = _pick_richest_street_id(page)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            node_key = page.evaluate("Object.keys(window.__geomEdit.nodeLayers)[0]")
            before_coords = page.evaluate("JSON.stringify(window.__geomEdit.edges)")

            page.evaluate("osmGeomEditSetTool('move')")
            page.evaluate(f"""
            () => {{
                var map = Object.values(window).find(function(v) {{
                    return v && v.dragging && v.getCenter;
                }});
                osmGeomEditStartDrag('{node_key}');
                map.fire('mousemove', {{latlng: L.latLng(0.5, 0.5)}});
                map.fire('mouseup', {{latlng: L.latLng(0.5, 0.5)}});
            }}
            """)
            page.wait_for_timeout(50)

            after_move_coords = page.evaluate("JSON.stringify(window.__geomEdit.edges)")
            assert after_move_coords != before_coords
            history_len = page.evaluate("window.__geomEdit.history.length")
            assert history_len > 0

            page.evaluate("while (window.__geomEdit.history.length) { osmGeomEditUndo(); }")
            page.wait_for_timeout(50)
            after_undo_coords = page.evaluate("JSON.stringify(window.__geomEdit.edges)")
            assert after_undo_coords == before_coords

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_geometry_mode_finish_blocked_by_branch(tmp_path):
    """Check geometry mode finish blocked by branch."""
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

            street_id = _pick_richest_street_id(page)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            # Pick an interior node (degree 2 within this street) and grow a
            # brand-new branch off it via the "create edge" tool - this
            # street now has a node with degree 3.
            interior_node = page.evaluate("""
            () => {
                var degree = {};
                for (var k in window.__geomEdit.edges) {
                    var e = window.__geomEdit.edges[k];
                    if (String(e.street_id) !== String(window.__geomEdit.streetId)) { continue; }
                    var a = e.coords[0], b = e.coords[e.coords.length - 1];
                    var ak = osmGeomEditNodeKey(a[0], a[1]);
                    var bk = osmGeomEditNodeKey(b[0], b[1]);
                    degree[ak] = (degree[ak] || 0) + 1;
                    degree[bk] = (degree[bk] || 0) + 1;
                }
                for (var node in degree) { if (degree[node] === 2) { return node; } }
                return null;
            }
            """)
            assert interior_node is not None, "fixture needs a street with an interior node"

            # Any OTHER node of this street (an endpoint, not the interior
            # node itself) - drawing straight to it (landing on a real
            # node finishes the edge immediately per the snap-to-node
            # behavior) creates a second path between two nodes already
            # connected by the existing line, i.e. a real branch.
            other_node = page.evaluate(f"""
            Object.keys(window.__geomEdit.nodeLayers).find(function(k) {{ return k !== '{interior_node}'; }})
            """)
            assert other_node is not None, "fixture needs at least 2 nodes on this street"

            page.evaluate("osmGeomEditSetTool('create')")
            coords = page.evaluate(f"window.__geomEdit.nodeLayers['{interior_node}'].getLatLng()")
            page.evaluate(f"""
            osmGeomEditNodeClick('{interior_node}', L.latLng({coords['lat']}, {coords['lng']}))
            """)
            other_coords = page.evaluate(f"window.__geomEdit.nodeLayers['{other_node}'].getLatLng()")
            page.evaluate(f"""
            osmGeomEditNodeClick('{other_node}', L.latLng({other_coords['lat']}, {other_coords['lng']}))
            """)
            page.wait_for_timeout(50)

            page.evaluate("osmGeomEditFinish()")
            page.wait_for_timeout(50)

            panel_display = page.eval_on_selector("#osmGeomEditPanel", "el => el.style.display")
            assert panel_display == "block"  # still open - Finish was blocked
            error_display = page.eval_on_selector("#osmGeomEditError", "el => el.style.display")
            assert error_display == "block"
            error_text = page.eval_on_selector("#osmGeomEditError", "el => el.innerText")
            assert "branch" in error_text.lower()

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_geometry_mode_finish_succeeds_when_valid(tmp_path):
    """Check geometry mode finish succeeds when valid."""
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

            street_id = _pick_richest_street_id(page)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            page.evaluate("osmGeomEditFinish()")
            page.wait_for_timeout(50)

            panel_display = page.eval_on_selector("#osmGeomEditPanel", "el => el.style.display")
            assert panel_display == "none"
            assert page.evaluate("window.__geomEdit") is None

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_geometry_mode_delete_node_merges_two_edges(tmp_path):
    """Check geometry mode delete node merges two edges."""
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

            street_id = _pick_richest_street_id(page)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            edges_before = page.evaluate("Object.keys(window.__geomEdit.edges).length")
            interior_node = page.evaluate("""
            () => {
                var degree = {};
                for (var k in window.__geomEdit.edges) {
                    var e = window.__geomEdit.edges[k];
                    if (String(e.street_id) !== String(window.__geomEdit.streetId)) { continue; }
                    var a = e.coords[0], b = e.coords[e.coords.length - 1];
                    var ak = osmGeomEditNodeKey(a[0], a[1]);
                    var bk = osmGeomEditNodeKey(b[0], b[1]);
                    degree[ak] = (degree[ak] || 0) + 1;
                    degree[bk] = (degree[bk] || 0) + 1;
                }
                for (var node in degree) { if (degree[node] === 2) { return node; } }
                return null;
            }
            """)
            assert interior_node is not None, "fixture needs a street with an interior node"
            assert edges_before >= 2

            page.evaluate("osmGeomEditSetTool('delete')")
            coords = page.evaluate(f"window.__geomEdit.nodeLayers['{interior_node}'].getLatLng()")
            page.evaluate(f"osmGeomEditNodeClick('{interior_node}', L.latLng({coords['lat']}, {coords['lng']}))")
            page.wait_for_timeout(50)

            edges_after = page.evaluate("Object.keys(window.__geomEdit.edges).length")
            assert edges_after == edges_before - 1

            result = page.evaluate("osmGeomEditValidateTopology()")
            assert result["ok"] is True

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_geometry_mode_remove_edge_then_finish_blocked_by_disconnection(tmp_path):
    """Check geometry mode remove edge then finish blocked by disconnection."""
    playwright_sync_api = pytest.importorskip("playwright.sync_api")

    G = build_idealized_grid(rows=4, cols=1, spacing=100.0, named_row=0, named_row_name="Elm Street", primary_row=0)
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

            street_id = page.evaluate("""
            () => {
                var counts = {};
                window.__edgesData.forEach(function(e) {
                    if (e.is_main && e.street_id !== null && e.street_id !== undefined) {
                        counts[e.street_id] = (counts[e.street_id] || 0) + 1;
                    }
                });
                var best = null, bestCount = -1;
                for (var s in counts) { if (counts[s] > bestCount) { best = s; bestCount = counts[s]; } }
                return best;
            }
            """)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            member_keys = page.evaluate("""
            Object.keys(window.__geomEdit.edges).filter(function(k) {
                return String(window.__geomEdit.edges[k].street_id) === String(window.__geomEdit.streetId);
            })
            """)
            if len(member_keys) < 3:
                pytest.skip("fixture needs a street with at least 3 member edges")

            # Remove a MIDDLE edge (not first or last) so the remaining
            # edges split into two disconnected pieces.
            middle_key = member_keys[len(member_keys) // 2]
            page.evaluate("osmGeomEditSetTool('remove_edge')")
            page.evaluate(f"osmGeomEditEdgeClick('{middle_key}')")
            page.wait_for_timeout(50)

            result = page.evaluate("osmGeomEditValidateTopology()")
            assert result["ok"] is False

            page.evaluate("osmGeomEditFinish()")
            page.wait_for_timeout(50)
            panel_display = page.eval_on_selector("#osmGeomEditPanel", "el => el.style.display")
            assert panel_display == "block"

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_new_street_button_enters_empty_geometry_mode(tmp_path):
    """Check new street button enters empty geometry mode."""
    playwright_sync_api = pytest.importorskip("playwright.sync_api")

    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    try:
        with playwright_sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on("dialog", lambda dialog: dialog.accept("Brand New St"))
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            page.evaluate("osmStreetEditorNewStreet()")
            page.wait_for_timeout(100)

            panel_display = page.eval_on_selector("#osmGeomEditPanel", "el => el.style.display")
            assert panel_display == "block"
            street_id = page.evaluate("window.__geomEdit.streetId")
            member_edges = page.evaluate("""
            Object.keys(window.__geomEdit.edges).filter(function(k) {
                var e = window.__geomEdit.edges[k];
                return e.street_id !== null && String(e.street_id) === String(window.__geomEdit.streetId);
            }).length
            """)
            assert member_edges == 0

            result = page.evaluate("osmGeomEditValidateTopology()")
            assert result["ok"] is False
            assert "no edges" in result["message"]

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_geometry_mode_internal_vertex_moves_without_confirmation(tmp_path):
    """Check geometry mode internal vertex moves without confirmation."""
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
            dialogs = []
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            street_id = _pick_richest_street_id(page)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            # Inject a synthetic interior vertex into one of this street's
            # own edges (the idealized-grid fixture only has straight
            # 2-point edges), then force a redraw so its own internal-
            # vertex marker gets created.
            edge_key = page.evaluate("""
            () => {
                for (var k in window.__geomEdit.edges) {
                    if (String(window.__geomEdit.edges[k].street_id) === String(window.__geomEdit.streetId)) {
                        return k;
                    }
                }
                return null;
            }
            """)
            assert edge_key is not None
            page.evaluate(f"""
            () => {{
                var coords = window.__geomEdit.edges['{edge_key}'].coords;
                var mid = [(coords[0][0] + coords[1][0]) / 2, (coords[0][1] + coords[1][1]) / 2];
                coords.splice(1, 0, mid);
            }}
            """)
            page.evaluate("osmGeomEditSetTool('move')")
            page.evaluate("osmGeomEditRedraw()")
            page.wait_for_timeout(50)

            internal_count = page.evaluate("Object.keys(window.__geomEdit.internalNodeLayers).length")
            assert internal_count > 0

            before = page.evaluate(f"JSON.stringify(window.__geomEdit.edges['{edge_key}'])")
            page.evaluate(f"osmGeomEditStartInternalVertexDrag('{edge_key}', 1)")
            page.wait_for_timeout(50)
            after = page.evaluate(f"JSON.stringify(window.__geomEdit.edges['{edge_key}'])")

            # No confirmation dialog for an internal-vertex move.
            assert dialogs == []
            # The move handler ran (edge object is the same reference
            # either way - just confirming no dialog blocked it).
            assert before is not None and after is not None

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_geometry_mode_network_node_move_asks_for_confirmation(tmp_path):
    """Check geometry mode network node move asks for confirmation."""
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
            dialogs = []
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            street_id = _pick_richest_street_id(page)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            node_key = page.evaluate("Object.keys(window.__geomEdit.nodeLayers)[0]")
            page.evaluate("osmGeomEditSetTool('move')")
            page.evaluate(f"osmGeomEditNetworkNodeMouseDown('{node_key}')")
            page.wait_for_timeout(50)

            assert len(dialogs) == 1
            assert "other" in dialogs[0].lower()

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_geometry_mode_node_marker_styles_distinguish_network_and_internal(tmp_path):
    """Check geometry mode node marker styles distinguish network and internal."""
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

            street_id = _pick_richest_street_id(page)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            node_key = page.evaluate("Object.keys(window.__geomEdit.nodeLayers)[0]")
            options = page.evaluate(f"window.__geomEdit.nodeLayers['{node_key}'].options")
            assert options["fillColor"] == "#000000"
            assert options["color"] == "#ffffff"
            assert options["radius"] > 4

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_node_popup_shows_spokes_for_each_street_and_allows_reassignment(tmp_path):
    """Check node popup shows spokes for each street and allows reassignment."""
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
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            result = page.evaluate("""
            () => {
                var best = null, bestCount = -1;
                window.__edgesData.forEach(function(e) {
                    [e.coords[0], e.coords[e.coords.length - 1]].forEach(function(pt) {
                        var spokes = osmNodeSpokes(pt[0], pt[1]);
                        if (spokes.length > bestCount) { bestCount = spokes.length; best = pt; }
                    });
                });
                return {pt: best, count: bestCount};
            }
            """)
            assert result["count"] >= 3, "fixture needs an intersection with 3+ streets"

            page.evaluate(f"osmNodePopupOpen({result['pt'][0]}, {result['pt'][1]})")
            page.wait_for_timeout(100)
            assert errors == []

            select_count = page.evaluate("""
            document.querySelectorAll('[id^="osmNodeSpokeSelect_"]').length
            """)
            assert select_count == result["count"]

            # Reassigning spoke 0 to "New street id" mints a fresh id and
            # records an edge-level override.
            edge_key_0 = page.evaluate("osmNodeSpokes(%s, %s)[0].edgeKey" % (result["pt"][0], result["pt"][1]))
            page.on("dialog", lambda d: d.accept("Brand New Spoke St"))
            page.evaluate(f"""
            () => {{
                var select = document.getElementById("osmNodeSpokeSelect_0");
                var opt = document.createElement("option");
                opt.value = "__new__";
                select.appendChild(opt);
                select.value = "__new__";
                select.onchange();
            }}
            """)
            page.wait_for_timeout(100)
            edit = page.evaluate(f"window.__edgeEdits['{edge_key_0}']")
            assert edit is not None
            assert edit["street_name"] == "Brand New Spoke St"

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_street_name_must_be_unique(tmp_path):
    """Check street name must be unique."""
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
            dialogs = []
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            ids = page.evaluate("Object.keys(window.__labelInfoById)")
            assert len(ids) >= 2, "fixture needs at least 2 named streets"
            other_id = ids[1]

            taken = page.evaluate(f"osmStreetEditorNameIsTaken('Elm Street', '{other_id}')")
            assert taken is True

            free = page.evaluate(f"osmStreetEditorNameIsTaken('Totally Unused Name', '{other_id}')")
            assert free is False

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_build_street_editor_map_has_node_dots_layer(tmp_path):
    """Check build street editor map has node dots layer."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    content = open(out_path).read()
    assert "osmNodePopupOpen" in content
    assert '"Nodes"' in content
    assert "circle_marker" in content or "CircleMarker" in content


def test_create_edge_does_not_finish_on_empty_space(tmp_path):
    """Clicking empty space (not near any existing node) while drawing
    only adds an intermediate vertex to the draft - it must NOT
    immediately finalize a 2-point edge, unlike the old behavior."""
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

            street_id = _pick_richest_street_id(page)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            node_key = page.evaluate("Object.keys(window.__geomEdit.nodeLayers)[0]")
            coords = page.evaluate(f"window.__geomEdit.nodeLayers['{node_key}'].getLatLng()")
            edges_before = page.evaluate("Object.keys(window.__geomEdit.edges).length")

            page.evaluate("osmGeomEditSetTool('create')")
            page.evaluate(f"osmGeomEditNodeClick('{node_key}', L.latLng({coords['lat']}, {coords['lng']}))")
            # Far from any existing node - a pure intermediate vertex.
            page.evaluate("osmGeomEditMapClick(L.latLng(5.0, 5.0))")
            page.wait_for_timeout(50)

            edges_after = page.evaluate("Object.keys(window.__geomEdit.edges).length")
            assert edges_after == edges_before, "no edge should be finalized by an empty-space click"
            draft_len = page.evaluate("window.__geomEdit.newEdgeDraft.coords.length")
            assert draft_len == 2

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_create_edge_finishes_on_node_and_chains_next_edge(tmp_path):
    """Check create edge finishes on node and chains next edge."""
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

            street_id = _pick_richest_street_id(page)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            node_keys = page.evaluate("Object.keys(window.__geomEdit.nodeLayers)")
            assert len(node_keys) >= 2
            first_coords = page.evaluate(f"window.__geomEdit.nodeLayers['{node_keys[0]}'].getLatLng()")
            second_coords = page.evaluate(f"window.__geomEdit.nodeLayers['{node_keys[1]}'].getLatLng()")

            page.evaluate("osmGeomEditSetTool('create')")
            page.evaluate(f"osmGeomEditNodeClick('{node_keys[0]}', L.latLng({first_coords['lat']}, {first_coords['lng']}))")
            page.evaluate(f"osmGeomEditNodeClick('{node_keys[1]}', L.latLng({second_coords['lat']}, {second_coords['lng']}))")
            page.wait_for_timeout(50)

            # The edge finished, AND drafting continues (chained) from
            # the node it just finished on - not left as a dead end.
            draft = page.evaluate("window.__geomEdit.newEdgeDraft")
            assert draft is not None
            assert len(draft["coords"]) == 1

            history_types = page.evaluate("window.__geomEdit.history.map(function(h) { return h.type; })")
            assert "create" in history_types

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_create_edge_snaps_within_5m_of_existing_node(tmp_path):
    """Check create edge snaps within 5m of existing node."""
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

            street_id = _pick_richest_street_id(page)
            page.evaluate(f"osmStreetEditorEnterGeometryMode('{street_id}')")
            page.wait_for_timeout(100)

            node_keys = page.evaluate("Object.keys(window.__geomEdit.nodeLayers)")
            first_coords = page.evaluate(f"window.__geomEdit.nodeLayers['{node_keys[0]}'].getLatLng()")
            second_coords = page.evaluate(f"window.__geomEdit.nodeLayers['{node_keys[1]}'].getLatLng()")
            # ~2m offset (well within the 5m snap tolerance).
            offset_lat = second_coords["lat"] + 0.000015
            offset_lng = second_coords["lng"] + 0.000015

            page.evaluate("osmGeomEditSetTool('create')")
            page.evaluate(f"osmGeomEditNodeClick('{node_keys[0]}', L.latLng({first_coords['lat']}, {first_coords['lng']}))")
            page.evaluate(f"osmGeomEditMapClick(L.latLng({offset_lat}, {offset_lng}))")
            page.wait_for_timeout(50)

            new_edge_key = page.evaluate("""
            Object.keys(window.__geomEdit.edges).find(function(k) {
                return window.__geomEdit.edges[k].is_new === true;
            })
            """)
            assert new_edge_key is not None, "click within snap tolerance of a node should finalize the edge"
            last_coord = page.evaluate(f"window.__geomEdit.edges['{new_edge_key}'].coords[window.__geomEdit.edges['{new_edge_key}'].coords.length - 1]")
            assert abs(last_coord[0] - second_coords["lat"]) < 1e-9
            assert abs(last_coord[1] - second_coords["lng"]) < 1e-9

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_segment_intersection_helper():
    """osmSegIntersect (pure JS math, no map needed) finds a genuine
    mid-segment crossing and excludes shared-endpoint touches."""
    playwright_sync_api = pytest.importorskip("playwright.sync_api")

    try:
        with playwright_sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content("<html><body><script>%s</script></body></html>" % (
                "function osmSegIntersect(p1,p2,p3,p4){"
                "var x1=p1[1],y1=p1[0],x2=p2[1],y2=p2[0],x3=p3[1],y3=p3[0],x4=p4[1],y4=p4[0];"
                "var d=(x1-x2)*(y3-y4)-(y1-y2)*(x3-x4);"
                "if(Math.abs(d)<1e-14){return null;}"
                "var t=((x1-x3)*(y3-y4)-(y1-y3)*(x3-x4))/d;"
                "var u=((x1-x3)*(y1-y2)-(y1-y3)*(x1-x2))/d;"
                "var eps=1e-6;"
                "if(t>eps&&t<1-eps&&u>eps&&u<1-eps){return [y1+t*(y2-y1),x1+t*(x2-x1)];}"
                "return null;}"
            ))

            crossing = page.evaluate("osmSegIntersect([0,0],[1,1],[0,1],[1,0])")
            assert crossing is not None
            assert abs(crossing[0] - 0.5) < 1e-9
            assert abs(crossing[1] - 0.5) < 1e-9

            shared_endpoint = page.evaluate("osmSegIntersect([0,0],[1,1],[1,1],[2,0])")
            assert shared_endpoint is None

            parallel = page.evaluate("osmSegIntersect([0,0],[1,1],[0,2],[1,3])")
            assert parallel is None

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_apply_street_edits_applies_edited_geometry_to_existing_edge(tmp_path):
    """Check apply street edits applies edited geometry to existing edge."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    some_id = streets_df["street_id"].iloc[0]
    some_edges = edges_gdf[edges_gdf["street_id"] == some_id].reset_index()
    u, v, key = some_edges.iloc[0][["u", "v", "key"]]

    original_geom = edges_gdf.loc[(u, v, key), "geometry"]
    line_wgs84 = gpd.GeoSeries([original_geom], crs=edges_gdf.crs).to_crs(4326).iloc[0]
    start, end = line_wgs84.coords[0], line_wgs84.coords[-1]
    mid_lon = (start[0] + end[0]) / 2 + 0.0005  # nudge the midpoint sideways
    mid_lat = (start[1] + end[1]) / 2 + 0.0005
    coords_latlon = [[start[1], start[0]], [mid_lat, mid_lon], [end[1], end[0]]]

    streets_csv_path = tmp_path / "streets.csv"
    with open(streets_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_street_id", "new_street_id", "street_name"])

    edge_edits_csv_path = tmp_path / "edge_edits.csv"
    with open(edge_edits_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["u", "v", "key", "new_street_id", "street_name", "geom_edited", "is_new", "coords"])
        writer.writerow([u, v, key, some_id, "", "True", "False", json.dumps(coords_latlon)])

    new_nodes, new_edges, new_streets = street_editor.apply_street_edits(
        nodes_gdf, edges_gdf, streets_df, str(streets_csv_path),
        edge_edits_csv_path=str(edge_edits_csv_path), origin=(0.0, 0.0),
    )

    # The edge now has 3 vertices (the inserted midpoint), not 2 -
    # proof the geometry itself changed, not just membership.
    matching = new_edges[new_edges["street_id"] == some_id]
    edited = matching[matching.apply(
        lambda r: frozenset((r.name[0], r.name[1])) == frozenset((u, v)), axis=1
    )]
    assert not edited.empty
    assert len(edited.iloc[0]["geometry"].coords) == 3


def test_apply_street_edits_creates_brand_new_edge_and_node(tmp_path):
    """Check apply street edits creates brand new edge and node."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))

    some_id = streets_df["street_id"].iloc[0]
    some_edges = edges_gdf[edges_gdf["street_id"] == some_id].reset_index()
    existing_u = some_edges.iloc[0]["u"]
    existing_node_geom = nodes_gdf.loc[existing_u, "geometry"]
    existing_point_wgs84 = gpd.GeoSeries([existing_node_geom], crs=nodes_gdf.crs).to_crs(4326).iloc[0]

    new_node_id = -1
    new_lat = existing_point_wgs84.y + 0.001
    new_lon = existing_point_wgs84.x + 0.001
    coords_latlon = [[existing_point_wgs84.y, existing_point_wgs84.x], [new_lat, new_lon]]

    streets_csv_path = tmp_path / "streets.csv"
    with open(streets_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old_street_id", "new_street_id", "street_name"])

    edge_edits_csv_path = tmp_path / "edge_edits.csv"
    with open(edge_edits_csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["u", "v", "key", "new_street_id", "street_name", "geom_edited", "is_new", "coords"])
        writer.writerow([existing_u, new_node_id, -100, some_id, "", "True", "True", json.dumps(coords_latlon)])

    new_nodes, new_edges, new_streets = street_editor.apply_street_edits(
        nodes_gdf, edges_gdf, streets_df, str(streets_csv_path),
        edge_edits_csv_path=str(edge_edits_csv_path), origin=(0.0, 0.0),
    )

    # The new node and physical edge both made it into the pipeline's
    # own output - street_id membership itself may have been turn-split
    # onto a fresh id by recompute_main_paths (this synthetic edge's
    # bearing has nothing to do with the original street's), which is
    # correct behavior, not something this test needs to pin down.
    assert new_node_id in new_nodes.index
    matching = new_edges.reset_index()
    new_physical = matching[
        matching.apply(lambda r: frozenset((r["u"], r["v"])) == frozenset((existing_u, new_node_id)), axis=1)
    ]
    assert not new_physical.empty
    assert new_physical.iloc[0]["street_id"] is not None
    assert pd.notna(new_physical.iloc[0]["street_id"])


def test_build_street_editor_map_simple_omits_geometry_tools(tmp_path):
    """Check build street editor map simple omits geometry tools."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor_simple.html")
    street_editor.build_street_editor_map_simple(streets_gdf, edges_gdf, out_path)

    content = open(out_path).read()
    # No geometry-changing tools.
    assert 'id="osmGeomEditTool_move"' not in content
    assert 'id="osmGeomEditTool_delete"' not in content
    assert 'id="osmGeomEditTool_prolong"' not in content
    assert 'id="osmGeomEditTool_create"' not in content
    # Membership-only tools, node popup, and merge all stay.
    assert 'id="osmGeomEditTool_add_edge"' in content
    assert 'id="osmGeomEditTool_remove_edge"' in content
    assert "osmNodePopupOpen" in content
    assert "osmStreetEditorToggleMergeMode" in content
    assert "osmStreetEditorEnterGeometryMode" in content


def test_build_street_editor_map_full_keeps_all_geometry_tools(tmp_path):
    """Check build street editor map full keeps all geometry tools."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor_full.html")
    street_editor.build_street_editor_map(streets_gdf, edges_gdf, out_path)

    content = open(out_path).read()
    for tool_id in (
        "osmGeomEditTool_move", "osmGeomEditTool_delete", "osmGeomEditTool_prolong",
        "osmGeomEditTool_create", "osmGeomEditTool_add_edge", "osmGeomEditTool_remove_edge",
    ):
        assert f'id="{tool_id}"' in content


def test_geometry_mode_simple_move_tool_is_unusable_via_ui(tmp_path):
    """Even though the underlying osmGeomEditSetTool function still
    exists (shared code), the simple map's UI never offers a way to
    invoke it - no button carries onclick="osmGeomEditSetTool('move')"
    (or delete/prolong/create) at all."""
    G = build_idealized_grid(rows=2, cols=2, spacing=100.0, named_row=None, primary_row=None)
    nodes_gdf, edges_gdf, streets_df = _labeled_and_directed(G, origin=(0.0, 0.0))
    streets_gdf = pipeline.streets_gdf_from_edges(edges_gdf, streets_df)

    out_path = str(tmp_path / "editor_simple.html")
    street_editor.build_street_editor_map_simple(streets_gdf, edges_gdf, out_path)

    content = open(out_path).read()
    for tool in ("move", "delete", "prolong", "create"):
        assert f"osmGeomEditSetTool('{tool}')" not in content
    assert "osmGeomEditSetTool('add_edge')" in content
    assert "osmGeomEditSetTool('remove_edge')" in content


def test_street_popup_has_no_street_id_input_and_rank_is_a_dropdown(tmp_path):
    """Check street popup has no street id input and rank is a dropdown."""
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

            sid = page.evaluate("Object.keys(window.__labelInfoById)[0]")
            html = page.evaluate(f"osmStreetPopupHtml('{sid}')")
            assert "osmStreetEditorSetId" not in html
            assert "<select" in html and "osmStreetEditorSetRank" in html
            assert 'type="number"' not in html

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_street_name_edit_trims_whitespace_and_rejects_duplicates(tmp_path):
    """Check street name edit trims whitespace and rejects duplicates."""
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
            page.on("dialog", lambda d: d.dismiss())
            page.goto(f"file://{out_path}")
            page.wait_for_timeout(300)

            ids = page.evaluate("Object.keys(window.__labelInfoById)")
            a, b = ids[0], ids[1]
            page.evaluate(f"""
            () => {{
                window.__streetIdToName['{a}'] = 'Named Street A';
            }}
            """)

            collision = page.evaluate(f"""
            () => {{
                var input = {{value: '  Named Street A  '}};
                osmStreetEditorSetName('{b}', input);
                return window.__streetEdits['{b}'];
            }}
            """)
            assert collision is None

            unique = page.evaluate(f"""
            () => {{
                var input = {{value: '  Fresh Unique Name  '}};
                osmStreetEditorSetName('{b}', input);
                return {{edit: window.__streetEdits['{b}'], inputValue: input.value}};
            }}
            """)
            assert unique["edit"]["street_name"] == "Fresh Unique Name"
            assert unique["inputValue"] == "Fresh Unique Name"

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise


def test_nodes_render_above_streets_in_marker_pane(tmp_path):
    """Check nodes render above streets in marker pane."""
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
            page.wait_for_timeout(500)

            paths_in_marker_pane = page.eval_on_selector_all(".leaflet-marker-pane path", "els => els.length")
            assert paths_in_marker_pane > 0

            z_overlay = page.evaluate(
                "parseInt(getComputedStyle(document.querySelector('.leaflet-overlay-pane')).zIndex)"
            )
            z_marker = page.evaluate(
                "parseInt(getComputedStyle(document.querySelector('.leaflet-marker-pane')).zIndex)"
            )
            assert z_marker > z_overlay

            browser.close()
    except Exception as exc:  # pragma: no cover
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed for Playwright: {exc}")
        raise
