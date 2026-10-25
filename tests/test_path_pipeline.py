"""End-to-end tests of path numbering (and street-line orientation) on synthetic networks."""

import math

import networkx as nx
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from osmaddresssystem import numbering, path_numbering, pipeline, street_editor
from osmaddresssystem.synthetic_grid import build_idealized_grid


def _grid(rows=4, cols=5):
    """Build the test grid.

    Args:
        rows: Grid rows.
        cols: Grid columns.

    Returns:
        The synthetic grid graph (100 m spacing, one named row, one primary row).
    """
    return build_idealized_grid(rows=rows, cols=cols, spacing=100.0, named_row=1, named_row_name="Elm Street", primary_row=2)


def _run(mode, origin=(150.0, 150.0), **kw):
    """Run the whole pipeline on the grid.

    Args:
        mode: Numbering mode.
        origin: Reference point.
        **kw: Extra ``build_addressed_streets`` arguments.

    Returns:
        ``(nodes_gdf, edges_gdf, streets_gdf)``.
    """
    return pipeline.build_addressed_streets(_grid(), origin=origin, numbering_mode=mode, label_streets_kwargs={"node_cluster_m": 0}, **kw)


def _street_paths(edges_gdf):
    """Ordered node sequences of every street's main path.

    Args:
        edges_gdf: Edges with ``street_id``, ``is_main_direction`` and the street positions.

    Returns:
        Dict street id -> list of node ids from the street's start to its other end.
    """
    flat = edges_gdf.reset_index()
    flat = flat[flat["is_main_direction"] & flat["street_id"].notna() & flat["street_pos_u_m"].notna()]
    out = {}
    for sid, g in flat.groupby("street_id"):
        pos = {}
        G = nx.Graph()
        for r in g.itertuples():
            pos[r.u], pos[r.v] = r.street_pos_u_m, r.street_pos_v_m
            G.add_edge(r.u, r.v)
        if max(dict(G.degree()).values()) > 2 or not nx.is_connected(G):
            continue
        ends = [n for n, deg in G.degree() if deg == 1]
        if len(ends) != 2:
            continue
        start = min(pos, key=pos.get)
        out[sid] = nx.shortest_path(G, start, ends[0] if ends[1] == start else ends[1]), pos
    return out


def test_path_mode_streets_start_at_zero_at_an_end_nearest_the_reference():
    """Every street id begins at its end nearest to the reference, with position 0 there, and only grows."""
    nodes, edges, streets = _run("path")
    d, _ = path_numbering.node_distances(nodes, edges, (150.0, 150.0))
    paths = _street_paths(edges)
    assert len(paths) >= 5
    for sid, (seq, pos) in paths.items():
        assert pos[seq[0]] == 0.0
        assert d[seq[0]] <= min(d[n] for n in seq) + 1e-6          # nearest by network distance
        dist = [d[n] for n in seq]
        assert all(b >= a - 1e-6 for a, b in zip(dist, dist[1:])), f"street {sid} is not monotone: {dist}"
        assert all(abs(pos[n] - (d[n] - d[seq[0]])) < 1e-6 for n in seq)


def test_path_mode_id_mapping_is_consistent():
    """Original and new ids are always kept and agree between the edges and the streets table."""
    nodes, edges, streets = _run("path")
    assert streets["street_id"].is_unique
    assert set(edges["street_id"].dropna().astype(int)) <= set(streets["street_id"])
    assert set(streets["street_id_original"]) >= set(streets.loc[streets["street_part"] == 0, "street_id"])
    new = streets[streets["street_part"] > 0]
    assert set(new["street_id_original"]) <= set(streets.loc[streets["street_part"] == 0, "street_id"])
    pair = edges[["street_id", "street_id_original"]].dropna().drop_duplicates()
    assert (pair.groupby("street_id")["street_id_original"].nunique() == 1).all()
    # a new id copies the original street's attributes; a named street gets its part letter in parentheses
    for _, r in new.iterrows():
        orig = streets[streets["street_id"] == r["street_id_original"]].iloc[0]
        assert r["street_name_base"] == orig["street_name"] or (pd.isna(r["street_name_base"]) and pd.isna(orig["street_name"]))
        if isinstance(orig["street_name"], str) and orig["street_name"].strip():
            assert r["street_name"] == f"{orig['street_name']} ({chr(ord('A') + int(r['street_part']))})"
        else:
            assert pd.isna(r["street_name"]) or r["street_name"] == ""


def test_path_mode_numbers_start_at_1_and_2_on_every_street():
    """At the beginning of every street id the left house number is 1 and the right 2."""
    nodes, edges, streets = _run("path")
    first = edges[edges["street_start_length_m"] == 0.0]
    assert len(first) > 0
    assert (first["house_number_start_left"].astype(float) == 1).all()
    assert (first["house_number_start_right"].astype(float) == 2).all()


@pytest.mark.parametrize("mode", ["radial", "path"])
def test_street_lines_run_in_the_numbering_direction(mode):
    """A point just left of a street's start gets an odd number, just right an even one."""
    nodes, edges, streets = _run(mode)
    checked = 0
    for _, row in streets.iterrows():
        line = row.geometry
        if line is None or line.geom_type != "LineString" or line.length < 40:
            continue
        p0, p1 = line.interpolate(0), line.interpolate(2.0)
        dx, dy = p1.x - p0.x, p1.y - p0.y
        n = math.hypot(dx, dy)
        dx, dy = dx / n, dy / n
        left = (p0.x + 3 * dx - dy, p0.y + 3 * dy + dx)
        right = (p0.x + 3 * dx + dy, p0.y + 3 * dy - dx)
        sub = streets[streets["street_id"] == row["street_id"]]
        a_left = numbering.geocode_points(sub, [left])[0].split(",")[0]
        a_right = numbering.geocode_points(sub, [right])[0].split(",")[0]
        assert int(a_left.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) % 2 == 1, (row["street_id"], a_left)
        assert int(a_right.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) % 2 == 0, (row["street_id"], a_right)
        assert int(a_left.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) <= 5 and int(a_right.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) <= 6
        checked += 1
    assert checked >= 5


def test_geocoding_uses_the_path_profile_not_the_line_length():
    """In path mode the house number follows the network distance profile of the street."""
    import json

    import geopandas as gpd

    profile = [[0.0, 0.0], [100.0, 40.0], [200.0, 160.0]]         # numbering grows slower, then faster
    street = gpd.GeoDataFrame(
        {"street_id": [1], "street_name": ["xyz"], "path_profile": [json.dumps(profile)]},
        geometry=[LineString([(0, 0), (200, 0)])], crs=32632)
    # (150, -2) is on the right of an eastward line, 150 m along: position = 40 + (160 - 40) * 0.5 = 100 m
    assert numbering.profile_position(profile, 150.0) == 100.0
    got = numbering.geocode_points(street, [(150.0, -2.0)])[0].split(",")[0]
    assert got == "50"                                              # round(100 / 2) = 50, even on the right
    plain = street.assign(path_profile=None)
    assert numbering.geocode_points(plain, [(150.0, -2.0)])[0].split(",")[0] == "76"   # 150 m / 2 = 75 -> even 76


def test_apply_street_edits_in_path_mode_keeps_the_mapping(tmp_path):
    """The editor re-run in path mode cuts the streets and keeps street_id_original."""
    nodes, edges, streets = _run("path", name_branches=False)
    # re-run from the radial labelling with a no-op edit file, now in path mode
    n0, e0, s0 = pipeline.build_addressed_streets(_grid(), origin=(150.0, 150.0), numbering_mode="radial",
                                                  label_streets_kwargs={"node_cluster_m": 0})
    csv = tmp_path / "streets.csv"
    s0.drop(columns="geometry").assign(old_street_id=s0["street_id"], new_street_id=s0["street_id"])[
        ["old_street_id", "new_street_id", "street_name", "street_rank"]].assign(is_drivable=None).to_csv(csv, index=False)
    n1, e1, s1 = street_editor.apply_street_edits(n0, e0, s0.drop(columns="geometry"), str(csv),
                                                  origin=(150.0, 150.0), numbering_mode="path")
    assert "street_id_original" in e1.columns and "street_id_original" in s1.columns
    assert s1["street_id"].is_unique and e1["street_pos_u_m"].notna().any()
    paths = _street_paths(e1)
    assert paths and all(pos[seq[0]] == 0.0 for seq, pos in paths.values())


def test_branch_names_work_in_path_mode():
    """Branch names are derived from the path-mode numbering and start from small numbers near the start."""
    nodes, edges, streets = _run("path", name_branches=True)
    named = streets[streets["branch_name"].notna()]
    assert len(named) > 0
    assert (named["branch_distance_m"] >= 0).all()
    assert named["branch_name"].str.match(r"^\d+[A-Z]*-").all()


def test_orient_along_numbering_flips_a_reversed_street_line_and_keeps_an_aligned_one():
    """The street line is flipped when its first point is not where the numbering starts."""
    import geopandas as gpd

    # one street of two edges, numbering starts at x = 200 (position 0) and ends at x = 0
    rows = [
        {"u": "c", "v": "b", "key": 0, "street_id": 1, "street_pos_u_m": 0.0, "street_pos_v_m": 100.0,
         "geometry": LineString([(200, 0), (100, 0)])},
        {"u": "b", "v": "a", "key": 0, "street_id": 1, "street_pos_u_m": 100.0, "street_pos_v_m": 200.0,
         "geometry": LineString([(100, 0), (0, 0)])},
    ]
    main = gpd.GeoDataFrame(rows, crs=32632).set_index(["u", "v", "key"])
    reversed_line = LineString([(0, 0), (100, 0), (200, 0)])           # a dissolved line running the other way
    aligned_line = LineString([(200, 0), (100, 0), (0, 0)])
    for line, first_x in ((reversed_line, 200.0), (aligned_line, 200.0)):
        dissolved = gpd.GeoDataFrame({"street_id": [1]}, geometry=[line], crs=32632)
        out = pipeline._orient_along_numbering(main, dissolved)
        assert list(out.iloc[0].coords)[0][0] == first_x
    # streets without positions (old edge tables) are left untouched
    dissolved = gpd.GeoDataFrame({"street_id": [1]}, geometry=[reversed_line], crs=32632)
    untouched = pipeline._orient_along_numbering(main.drop(columns=["street_pos_u_m", "street_pos_v_m"]), dissolved)
    assert list(untouched.iloc[0].coords)[0][0] == 0.0
