"""Tests for street_ids.label_streets, mirroring the illustrations in
examples/tutorial/01_street_identity.ipynb and report/street_identity.md.
"""

import math

import networkx as nx
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString

from osmaddresssystem import street_ids
from osmaddresssystem.synthetic_grid import build_idealized_grid, build_irregular_grid


def make_graph(edges, crs="EPSG:3857"):
    """edges: list of (u, v, (ux, uy), (vx, vy), highway, name)."""
    G = nx.MultiDiGraph(crs=crs)
    for u, v, uxy, vxy, highway, name in edges:
        G.add_node(u, x=uxy[0], y=uxy[1])
        G.add_node(v, x=vxy[0], y=vxy[1])
        fwd = LineString([uxy, vxy])
        bwd = LineString([vxy, uxy])
        G.add_edge(u, v, 0, highway=highway, name=name, geometry=fwd, length=fwd.length, osmid=1)
        G.add_edge(v, u, 0, highway=highway, name=name, geometry=bwd, length=bwd.length, osmid=1)
    return G


def arm(angle_deg, length=100.0):
    rad = math.radians(angle_deg)
    return (length * math.cos(rad), length * math.sin(rad))


def street_id_of(edges_gdf, u, v):
    return edges_gdf.loc[(u, v, 0), "street_id"]


def test_named_street_is_left_as_is():
    """Rule 1: same name -> same street_id, regardless of geometry."""
    G = make_graph(
        [
            (0, 1, (0, 0), (100, 0), "residential", "Elm Street"),
            (1, 2, (100, 0), (200, 0), "residential", "Elm Street"),
            (2, 3, (200, 0), (300, 0), "residential", "Elm Street"),
        ]
    )
    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 1
    assert streets_df.loc[0, "street_name"] == "Elm Street"
    assert streets_df.loc[0, "highway"] == "residential"
    ids = {street_id_of(edges_gdf, u, v) for u, v in [(0, 1), (1, 2), (2, 3)]}
    assert len(ids) == 1


def test_streets_df_highway_takes_highest_rank_from_list():
    """streets_df's highway column must resolve a list-valued OSM
    highway tag to its single highest-rank (lowest rank number) member,
    not just carry the rank number or the raw list through."""
    G = make_graph(
        [
            (0, 1, (0, 0), (100, 0), ["residential", "primary", "service"], None),
        ]
    )
    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 1
    assert streets_df.loc[0, "highway"] == "primary"
    assert streets_df.loc[0, "street_rank"] == 1


def test_unnamed_street_name_is_real_none_not_nan():
    """A DataFrame column mixing Python None with real strings (e.g. one
    named street, one unnamed) can silently turn None into float NaN
    during construction. street_name for an unnamed street must stay
    exactly None, in both streets_df and edges_gdf, as specified."""
    G = make_graph(
        [
            (0, 1, (0, 0), (100, 0), "residential", "Elm Street"),
            (0, 2, (0, 0), (0, 100), "residential", None),
        ]
    )
    _, edges_gdf, streets_df = street_ids.label_streets(G)

    unnamed_row = streets_df.loc[streets_df["street_name"].isna()]
    assert len(unnamed_row) == 1
    assert unnamed_row["street_name"].iloc[0] is None

    assert edges_gdf.loc[(0, 2, 0), "street_name"] is None


def test_equal_rank_streets_grouped_by_direction():
    """Rules 2/3: east-west arms merge, north-south arms merge, but the
    two groups never merge with each other (90 deg apart)."""
    G = make_graph(
        [
            (1, 0, (-100, 0), (0, 0), "residential", None),  # west
            (0, 2, (0, 0), (100, 0), "residential", None),  # east
            (0, 3, (0, 0), (0, 100), "residential", None),  # north
            (0, 4, (0, 0), (0, -100), "residential", None),  # south
        ]
    )
    _, edges_gdf, streets_df = street_ids.label_streets(G, angle_threshold_deg=20)

    assert len(streets_df) == 2
    assert all(name is None for name in streets_df["street_name"])

    ew_id = street_id_of(edges_gdf, 1, 0)
    assert street_id_of(edges_gdf, 0, 2) == ew_id

    ns_id = street_id_of(edges_gdf, 0, 3)
    assert street_id_of(edges_gdf, 0, 4) == ns_id

    assert ew_id != ns_id


def test_lower_rank_street_is_cut_by_higher_rank_crossing():
    """Rule 2: a primary road keeps one id straight through a crossing.
    The lower-rank residential arms don't merge into the primary's id,
    and they don't merge with *each other* either: crossing a
    higher-rank street cuts them into two separate streets, one on each
    side."""
    G = make_graph(
        [
            (1, 0, (-100, 0), (0, 0), "primary", None),
            (0, 2, (0, 0), (100, 0), "primary", None),
            (0, 3, (0, 0), (0, 100), "residential", None),
            (0, 4, (0, 0), (0, -100), "residential", None),
        ]
    )
    # min_street_length_m=0: this test is specifically about rule 2's
    # rank-based cutting, not "Redistributing too-short streets" - left
    # at the library default, the short, unnamed, cut-off residential
    # arms would legitimately get redistributed right back into the
    # primary road they touch (rank-agnostic for that feature), which is
    # correct behavior for that separate feature but defeats what this
    # test checks. parallel_dist_m=0: the two residential arms are
    # collinear through node 0 and would otherwise legitimately get
    # merged by the parallel-proximity feature (same rank, unnamed,
    # aligned) - a separate concern from the rank-based cut this test
    # checks.
    _, edges_gdf, streets_df = street_ids.label_streets(
        G, angle_threshold_deg=20, min_street_length_m=0, parallel_dist_m=0
    )

    assert len(streets_df) == 3

    primary_id = street_id_of(edges_gdf, 1, 0)
    assert street_id_of(edges_gdf, 0, 2) == primary_id
    assert streets_df.set_index("street_id").loc[primary_id, "street_rank"] == 1

    north_id = street_id_of(edges_gdf, 0, 3)
    south_id = street_id_of(edges_gdf, 0, 4)
    assert north_id != south_id
    assert primary_id not in (north_id, south_id)


def test_direction_bin_groups_everything_within_tolerance_of_its_own_leader():
    """At a node_id, unnamed same-rank edges are grouped into FIXED
    direction bins (default 6 over [0, 180), i.e. 30 deg each - see
    ``num_node_directions`` and "No more than one direction per
    node_id"). At 0/5/12/70 deg, the fixed grid (rotated to the widest
    real gap between these angles, taken modulo the bin width) places
    0/5/12 in one bin and 70 in another.

    Merging within a bin also requires the FAR endpoint of each edge to
    stay within ``parallel_pass_touching_dist_m`` (point-to-line
    distance) of the other's own tracked branch reference line - since
    these arms are deliberately long (1000m, to keep their far endpoints
    well outside node_cluster_m and avoid the unrelated dual-carriageway
    auto-merge), even a 12 deg divergence corresponds to real physical
    separation (~208m) by the arms' own far ends, so
    ``parallel_pass_touching_dist_m`` is raised to accommodate it here -
    a real street network's own edges are far shorter, so this scale
    issue essentially never arises in practice.

    0/5/12 deg all still share ONE street_id (identity/continuity is
    still governed by the node's own direction bin, not
    angle_threshold_deg) - but turn-splitting then finds the 0/5/12 group's
    own main path (the 0 deg and 12 deg arms, tied on length, are its
    diameter) turns 12 deg at the shared node, over the default
    angle_threshold_deg (10) - so the 12 deg arm ends up split onto its
    own separate street_id, while the 5 deg arm (whose own turn against
    the chosen main path is only 5 deg, under the threshold) stays
    attached to the 0 deg arm's street_id as non-main furniture.
    """
    G = make_graph(
        [
            (0, 1, (0, 0), arm(0, length=1000.0), "residential", None),
            (0, 2, (0, 0), arm(5, length=1000.0), "residential", None),
            (0, 3, (0, 0), arm(12, length=1000.0), "residential", None),
            (0, 4, (0, 0), arm(70, length=1000.0), "residential", None),
        ]
    )
    _, edges_gdf, streets_df = street_ids.label_streets(
        G, min_street_length_m=0, parallel_dist_m=0, parallel_pass_touching_dist_m=250,
    )

    assert len(streets_df) == 3

    id_0 = street_id_of(edges_gdf, 0, 1)
    id_5 = street_id_of(edges_gdf, 0, 2)
    id_12 = street_id_of(edges_gdf, 0, 3)
    id_70 = street_id_of(edges_gdf, 0, 4)

    assert id_0 == id_5
    assert id_12 != id_0
    assert id_70 != id_0
    assert id_70 != id_12


def test_no_match_no_merge():
    """Rule 5: two arms 45 deg apart (outside the threshold) never merge."""
    G = make_graph(
        [
            (0, 1, (0, 0), arm(0), "residential", None),
            (0, 2, (0, 0), arm(45), "residential", None),
        ]
    )
    _, edges_gdf, streets_df = street_ids.label_streets(G, angle_threshold_deg=20)

    assert len(streets_df) == 2
    assert street_id_of(edges_gdf, 0, 1) != street_id_of(edges_gdf, 0, 2)


def test_num_node_directions_is_configurable():
    """Two arms 30 deg apart, sharing one node_id: merge with a coarser
    grid (num_node_directions=3, i.e. 60 deg bins - both arms fall in
    the same bin), don't merge with a finer one (num_node_directions=9,
    i.e. 20 deg bins - a 30 deg separation can't fit in one bin) - the
    per-node_id direction-binning granularity (report/street_identity.md
    "No more than one direction per node_id"), not the general
    angle_threshold_deg.

    Both arms share node 0 as their own "u" endpoint (neither touches the
    other at its own "v" end), so walking node1 -> node0 -> node2 through
    a coarse-bin merge is actually a near-reversal in real (directed)
    terms - both arms point the same general way OUT of node 0, so
    "continuing through" node 0 from one to the other means turning
    almost all the way back on yourself, a turn no realistic
    angle_threshold_deg would ever consider anything but sharp. That's a
    genuine, correctly-detected sharp turn for main-path purposes, not a
    bug - main_path_angle_threshold_deg is set to 180 (i.e. "never split") here
    purely so the newer main-path turn-split doesn't interfere with what
    this test is actually about (bin granularity at the identity-merge
    stage, before any main path/turn-split even exists)."""
    # Short arms (10m) so a 30 deg divergence's own far-point separation
    # (~5m) stays well within the default branch-drift distance - this
    # test is about bin granularity, not the drift check.
    G = make_graph(
        [
            (0, 1, (0, 0), arm(0, length=10.0), "residential", None),
            (0, 2, (0, 0), arm(30, length=10.0), "residential", None),
        ]
    )
    _, edges_fine, _ = street_ids.label_streets(
        G, num_node_directions=9, node_cluster_m=0, main_path_angle_threshold_deg=180,
    )
    assert street_id_of(edges_fine, 0, 1) != street_id_of(edges_fine, 0, 2)

    _, edges_coarse, _ = street_ids.label_streets(
        G, num_node_directions=3, node_cluster_m=0, main_path_angle_threshold_deg=180,
    )
    assert street_id_of(edges_coarse, 0, 1) == street_id_of(edges_coarse, 0, 2)


@pytest.mark.parametrize("rows,cols", [(4, 4), (3, 5)])
def test_idealized_grid_rows_and_columns(rows, cols):
    """Integration test on a rows x cols grid with one named row and one
    (interior) primary row:
      - every non-primary, non-named row is a single street
      - the named row is a single street under its own name
      - the primary row is a single street
      - each column is cut into two streets where it crosses the primary
        row (a lower-rank street doesn't merge across a higher-rank one),
        but is otherwise a single street on each side of the crossing
    """
    named_row, primary_row = 0, 1
    assert 0 < primary_row < rows - 1, "primary_row must be interior for this test"

    G = build_idealized_grid(
        rows=rows,
        cols=cols,
        named_row=named_row,
        named_row_name="Elm Street",
        primary_row=primary_row,
    )
    # min_street_length_m=0: with the library default, the short
    # below-primary column segment this test deliberately cuts by rank
    # would legitimately get reassigned by _redistribute_short_streets
    # into its own continuing above-primary column segment (same
    # direction, straight through) - a real, separate concern from what
    # this test is actually checking (the rank-based cut itself).
    # parallel_dist_m=0: same reason - the parallel-proximity feature
    # would otherwise also legitimately reconnect that same below/above
    # pair (same rank, unnamed, aligned, close near the crossing).
    _, edges_gdf, streets_df = street_ids.label_streets(
        G, angle_threshold_deg=20, min_street_length_m=0, parallel_dist_m=0
    )
    edges_reset = edges_gdf.reset_index()
    streets_by_id = streets_df.set_index("street_id")

    def id_of(u, v):
        row = edges_reset.loc[(edges_reset["u"] == u) & (edges_reset["v"] == v)]
        return row["street_id"].iloc[0]

    def node(r, c):
        return r * cols + c

    for r in range(rows):
        if r in (named_row, primary_row):
            continue
        row_ids = edges_reset.loc[
            (edges_reset["u"] // cols == r) & (edges_reset["v"] // cols == r), "street_id"
        ]
        assert row_ids.nunique() == 1
        assert streets_by_id.loc[row_ids.iloc[0], "street_name"] is None

    named_ids = edges_reset.loc[
        (edges_reset["u"] // cols == named_row) & (edges_reset["v"] // cols == named_row),
        "street_id",
    ]
    assert named_ids.nunique() == 1
    assert streets_by_id.loc[named_ids.iloc[0], "street_name"] == "Elm Street"

    primary_ids = edges_reset.loc[
        (edges_reset["u"] // cols == primary_row) & (edges_reset["v"] // cols == primary_row),
        "street_id",
    ]
    assert primary_ids.nunique() == 1
    assert streets_by_id.loc[primary_ids.iloc[0], "street_rank"] == 1

    for c in range(cols):
        below_ids = {id_of(node(r, c), node(r + 1, c)) for r in range(0, primary_row)}
        above_ids = {
            id_of(node(r, c), node(r + 1, c)) for r in range(primary_row, rows - 1)
        }
        assert len(below_ids) == 1
        assert len(above_ids) == 1
        assert below_ids != above_ids  # cut where the column crosses the primary row


def test_irregular_grid_wiggle_tolerated_kink_splits():
    """synthetic_grid.build_irregular_grid: rows zigzag +/-10
    deg segment to segment (under the 20 deg threshold) and should each
    stay one street; one row has a permanent 35 deg bend partway across
    (over the threshold) and should split into exactly two streets, not
    three - only the single transition *into* the bend exceeds the
    threshold, the segments after it wiggle around the new heading and
    stay merged with each other."""
    cols = 5
    named_row, primary_row, kink_row, kink_col = 0, 1, 3, 2

    G = build_irregular_grid(
        rows=4,
        cols=cols,
        named_row=named_row,
        named_row_name="Elm Street",
        primary_row=primary_row,
        wiggle_deg=10.0,
        kink_row=kink_row,
        kink_col=kink_col,
        kink_deg=35.0,
    )
    # min_street_length_m=0: this test is specifically about rule 3's
    # angle-based kink split, not "Redistributing too-short streets" -
    # left at the library default (200m), the short kink fragment would
    # legitimately get redistributed right back into the longer row it
    # directly touches at the kink node, which is correct behavior for
    # that separate feature but defeats what this test checks.
    # parallel_pass_touching_dist_m raised to accommodate this grid's own
    # 120m spacing: the branch-drift check (point-to-line distance) at
    # the default 15m would reject even the intentionally-tolerated +/-10
    # deg wiggle here (~41m of real separation at this edge length),
    # while a real street network's own edges are far shorter and rarely
    # trigger this scale mismatch.
    _, edges_gdf, streets_df = street_ids.label_streets(
        G, angle_threshold_deg=20, min_street_length_m=0, parallel_pass_touching_dist_m=45,
    )
    edges_reset = edges_gdf.reset_index()
    streets_by_id = streets_df.set_index("street_id")

    def row_ids(r):
        return edges_reset.loc[
            (edges_reset["u"] // cols == r) & (edges_reset["v"] // cols == r), "street_id"
        ]

    # Wiggly (but not kinked) rows stay a single street each.
    for r in (0, 1, 2):
        ids = row_ids(r)
        assert ids.nunique() == 1

    assert streets_by_id.loc[row_ids(named_row).iloc[0], "street_name"] == "Elm Street"
    assert streets_by_id.loc[row_ids(primary_row).iloc[0], "street_rank"] == 1

    # The kinked row splits into exactly two streets, at the kink column.
    kink_row_ids = row_ids(kink_row)
    assert kink_row_ids.nunique() == 2

    def id_of(c_from, c_to):
        u, v = kink_row * cols + c_from, kink_row * cols + c_to
        row = edges_reset.loc[(edges_reset["u"] == u) & (edges_reset["v"] == v)]
        return row["street_id"].iloc[0]

    # The kink segment itself (kink_col - 1 -> kink_col) already carries the
    # new heading, so the discontinuity sits between the segment before it
    # and the kink segment - "after" starts at the kink segment.
    before_bend_ids = {id_of(c, c + 1) for c in range(0, kink_col - 1)}
    after_bend_ids = {id_of(c, c + 1) for c in range(kink_col - 1, cols - 1)}
    assert len(before_bend_ids) == 1
    assert len(after_bend_ids) == 1
    assert before_bend_ids != after_bend_ids


def test_vectorized_rank_matches_scalar_including_list_valued_highway():
    """_vectorized_highway_rank_and_value must agree with the scalar
    _highway_rank/_highway_value_and_rank for plain strings, unknown
    values, and (the rare but real) list-valued OSM tags, all in the
    same column - and for a list, the chosen value must be the specific
    highway type with the highest rank (lowest rank number), not just
    the rank number itself."""
    import pandas as pd

    highway = pd.Series(["primary", "made_up_type", ["residential", "primary"], None])
    rank, value = street_ids._vectorized_highway_rank_and_value(
        highway, street_ids.DEFAULT_HIGHWAY_RANKS
    )
    expected_rank = [
        street_ids._highway_rank(h, street_ids.DEFAULT_HIGHWAY_RANKS) for h in highway
    ]
    expected_value = [
        street_ids._highway_value_and_rank(h, street_ids.DEFAULT_HIGHWAY_RANKS)[0]
        for h in highway
    ]
    assert rank.tolist() == expected_rank
    assert value.tolist() == expected_value
    # The list ["residential", "primary"] -> "primary" has the lower rank number.
    assert value.iloc[2] == "primary"


def test_vectorized_clean_name_matches_scalar_including_list_and_blank():
    """_vectorized_clean_name must agree with the scalar _clean_name for
    plain strings, None, blank/whitespace-only strings, and list-valued
    name tags, all in the same column, and must return real None (not
    NaN) for missing names."""
    import pandas as pd

    name = pd.Series(["Elm Street", None, "   ", ["", "Oak Ave"]])
    vectorized = street_ids._vectorized_clean_name(name)
    scalar = [street_ids._clean_name(n) for n in name]
    assert vectorized.tolist() == scalar
    assert vectorized.iloc[1] is None
    assert vectorized.iloc[2] is None


def test_streets_df_and_street_ids_are_ordered_by_rank():
    """streets.csv should list higher-priority streets (lower rank
    number) first, and street_id itself should follow that same order
    (not just be sorted incidentally) - ties broken deterministically."""
    G = build_idealized_grid(rows=4, cols=4, named_row=1, primary_row=2)
    _, edges_gdf, streets_df = street_ids.label_streets(G, angle_threshold_deg=20)

    # street_rank must be non-decreasing down the table.
    assert streets_df["street_rank"].is_monotonic_increasing

    # street_id must increase in lockstep with row order (1, 2, 3, ...),
    # i.e. id assignment itself follows the rank ordering.
    assert streets_df["street_id"].tolist() == list(range(1, len(streets_df) + 1))

    # The single primary (rank 2) street must be street_id 1, ahead of
    # every rank-5 residential street.
    primary_rows = streets_df.loc[streets_df["street_rank"] == 1]
    assert len(primary_rows) == 1
    assert primary_rows["street_id"].iloc[0] == 1

    # edges_gdf must use the same renumbered ids as streets_df.
    assert set(edges_gdf["street_id"].unique()) == set(streets_df["street_id"])


def test_streets_df_osmids_collects_and_flattens_all_ways_in_a_street():
    """streets.csv's osmids column must list every OSM way id that ended
    up under a street_id - flattening list-valued osmid (which osmnx
    produces when it merges parallel/duplicate ways) - so it can be used
    later to go tag those specific ways with a real name."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node(0, x=0, y=0)
    G.add_node(1, x=100, y=0)
    G.add_node(2, x=200, y=0)

    def add_edge(u, v, osmid):
        ux, uy = G.nodes[u]["x"], G.nodes[u]["y"]
        vx, vy = G.nodes[v]["x"], G.nodes[v]["y"]
        fwd = LineString([(ux, uy), (vx, vy)])
        bwd = LineString([(vx, vy), (ux, uy)])
        G.add_edge(
            u, v, 0, highway="residential", name=None, geometry=fwd, length=fwd.length, osmid=osmid
        )
        G.add_edge(
            v, u, 0, highway="residential", name=None, geometry=bwd, length=bwd.length, osmid=osmid
        )

    add_edge(0, 1, osmid=[101, 102])  # osmnx-style merged-ways list
    add_edge(1, 2, osmid=103)

    _, _, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 1
    assert streets_df.loc[0, "osmids"] == "101;102;103"


def _add_edge(G, u, v, uxy, vxy, highway="residential", name=None, osmid=1, key=0, **attrs):
    G.add_node(u, x=uxy[0], y=uxy[1])
    G.add_node(v, x=vxy[0], y=vxy[1])
    fwd = LineString([uxy, vxy])
    bwd = LineString([vxy, uxy])
    G.add_edge(
        u, v, key, highway=highway, name=name, geometry=fwd, length=fwd.length, osmid=osmid,
        **attrs,
    )
    G.add_edge(
        v, u, key, highway=highway, name=name, geometry=bwd, length=bwd.length, osmid=osmid,
        **attrs,
    )


def _oneway_edge(G, u, v, uxy, vxy, highway="residential", name=None, osmid=1, key=0):
    """Add a single directed edge only, as OSM represents a one-way way -
    used for divided-road/carriageway tests where the two directions are
    separate ways, not one reversible street."""
    G.add_node(u, x=uxy[0], y=uxy[1])
    G.add_node(v, x=vxy[0], y=vxy[1])
    line = LineString([uxy, vxy])
    G.add_edge(u, v, key, highway=highway, name=name, geometry=line, length=line.length, osmid=osmid)


def test_tangent_uses_10pct_fraction_not_the_adjacent_vertex():
    """A noisy first/last vertex right at a node (a stray dogleg a
    couple meters in, the kind real OSM digitizing produces) must not
    throw off the tangent enough to wrongly cut an otherwise-straight
    street. Edge A runs due east for 98m, then jogs sharply for its
    last 2m into the shared node - its raw adjacent-vertex tangent
    there would be ~26.6 deg off due east (over the default 20 deg
    angle_threshold_deg), but measuring 10% of the edge's own length in
    (see _TANGENT_FRACTION) averages past that jog and reads much
    closer to the edge's real overall direction, so it still merges
    with the perfectly straight, due-east edge B."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node(0, x=-100, y=0)
    G.add_node(1, x=0, y=0)
    G.add_node(2, x=100, y=0)
    edge_a = LineString([(-100, 0), (-2, -1), (0, 0)])
    edge_a_rev = LineString([(0, 0), (-2, -1), (-100, 0)])
    edge_b = LineString([(0, 0), (100, 0)])
    edge_b_rev = LineString([(100, 0), (0, 0)])
    G.add_edge(
        0, 1, 0, highway="residential", name=None, geometry=edge_a, length=edge_a.length, osmid=1
    )
    G.add_edge(
        1,
        0,
        0,
        highway="residential",
        name=None,
        geometry=edge_a_rev,
        length=edge_a_rev.length,
        osmid=1,
    )
    G.add_edge(
        1, 2, 0, highway="residential", name=None, geometry=edge_b, length=edge_b.length, osmid=2
    )
    G.add_edge(
        2,
        1,
        0,
        highway="residential",
        name=None,
        geometry=edge_b_rev,
        length=edge_b_rev.length,
        osmid=2,
    )

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 1
    assert street_id_of(edges_gdf, 0, 1) == street_id_of(edges_gdf, 1, 2)


def test_fuzzy_named_streets_merge_when_touching():
    """Two edges with different literal names for the same route
    ("Nationale 5" vs. "Douala-Bafoussam (N5)") get the same street_id
    once they touch at a shared node, thanks to fuzzy name matching."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (200, 0), name="Nationale 5")
    _add_edge(G, 1, 2, (200, 0), (400, 0), name="Douala-Bafoussam (N5)")

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 1
    ids = {street_id_of(edges_gdf, 0, 1), street_id_of(edges_gdf, 1, 2)}
    assert len(ids) == 1


def test_same_name_edges_that_never_touch_stay_separate():
    """Same (exact) name is not enough on its own any more: two
    completely disconnected edges named "Elm Street" must not share a
    street_id just because the string matches - they never touch at a
    node."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (200, 0), name="Elm Street")
    _add_edge(G, 10, 11, (10_000, 10_000), (10_200, 10_000), name="Elm Street")

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 2
    assert street_id_of(edges_gdf, 0, 1) != street_id_of(edges_gdf, 10, 11)


def test_three_same_name_arms_at_a_node_only_two_share_an_id():
    """A Y-fork where all three arms share the exact same name still
    must not put more than two edges under one street_id at that node -
    the third arm gets cut into its own id (best-match-wins, same as
    the unnamed rule)."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 1, 0, (-300, 0), (0, 0), name="Main Street")
    _add_edge(G, 0, 2, (0, 0), (300, 0), name="Main Street")
    _add_edge(G, 0, 3, (0, 0), (300, 300), name="Main Street")

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 2
    assert (streets_df["street_name"] == "Main Street").all()
    ids_at_node_0 = {
        street_id_of(edges_gdf, 1, 0),
        street_id_of(edges_gdf, 0, 2),
        street_id_of(edges_gdf, 0, 3),
    }
    assert len(ids_at_node_0) == 2


def test_named_match_prefers_straight_pair_over_insertion_order():
    """At a node with three edges sharing the exact same name (so every
    candidate pair scores identically on name similarity alone), the
    genuinely straight-through pair must win regardless of which order
    the edges happen to be added to the graph in - not whichever pair
    the matching loop happens to consider first.

    Edges are added in West, Spur, East order specifically so that a
    naive "first same-score pair wins" tie-break (no angle involved)
    would pair West with the 45-degree Spur before ever considering the
    true West/East straight continuation - reproducing the real bug this
    guards against (see test_street_ids_real_data.py's
    test_parallel_dist_m_merges_known_interchange_stubs, the Paseo de la
    Castellana case)."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 1, 0, (-500, 0), (0, 0), name="Main Street")  # West
    _add_edge(G, 0, 2, (0, 0), (300, 300), name="Main Street")  # Spur, ~45 deg
    _add_edge(G, 0, 3, (0, 0), (500, 0), name="Main Street")  # East, collinear with West

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 2
    assert street_id_of(edges_gdf, 1, 0) == street_id_of(edges_gdf, 0, 3)
    assert street_id_of(edges_gdf, 1, 0) != street_id_of(edges_gdf, 0, 2)


def test_dual_carriageway_auto_merges_via_node_cluster():
    """Two separately-mapped, opposite-direction one-way edges (a
    divided road's carriageways) whose endpoints are within
    node_cluster_m of each other in each direction are automatically
    the same street_id, no name or rank match needed - and exactly one
    is flagged is_main_direction=True."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node(1, x=0, y=0)
    G.add_node(2, x=1, y=0)  # 1m from node 1: within the default 15m cluster
    G.add_node(3, x=200, y=0)
    G.add_node(4, x=201, y=0)  # 1m from node 3

    fwd = LineString([(0, 0), (200, 0)])
    G.add_edge(
        1, 3, 0, highway="primary", name=None, geometry=fwd, length=fwd.length, osmid=201
    )
    bwd = LineString([(201, 0), (1, 0)])
    G.add_edge(
        4, 2, 0, highway="primary", name=None, geometry=bwd, length=bwd.length, osmid=202
    )

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 1
    assert set(streets_df.loc[0, "osmids"].split(";")) == {"201", "202"}
    main_flags = sorted(edges_gdf["is_main_direction"].tolist())
    assert main_flags == [False, True]


def test_dual_carriageway_not_merged_when_node_cluster_disabled():
    """The same layout as the dual-carriageway test above, but with
    node_cluster_m=0: clustering is off, so the two carriageways are
    two unrelated, unnamed edges and must get different street_ids."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node(1, x=0, y=0)
    G.add_node(2, x=1, y=0)
    G.add_node(3, x=200, y=0)
    G.add_node(4, x=201, y=0)

    fwd = LineString([(0, 0), (200, 0)])
    G.add_edge(
        1, 3, 0, highway="primary", name=None, geometry=fwd, length=fwd.length, osmid=201
    )
    bwd = LineString([(201, 0), (1, 0)])
    G.add_edge(
        4, 2, 0, highway="primary", name=None, geometry=bwd, length=bwd.length, osmid=202
    )

    # parallel_pass_touching_dist_m/parallel_pass_non_touching_dist_m=0:
    # the two carriageways (1m apart, unnamed, same rank, parallel) would
    # otherwise legitimately get joined by the final whole-street_id
    # parallel pass regardless of node_cluster_m - a separate concern
    # from what this test checks (node-cluster-based merging specifically).
    _, edges_gdf, streets_df = street_ids.label_streets(
        G, node_cluster_m=0, parallel_pass_touching_dist_m=0, parallel_pass_non_touching_dist_m=0
    )

    assert len(streets_df) == 2
    assert edges_gdf["is_main_direction"].all()


def _three_way_cluster_group_graph(order):
    """Three edges whose endpoints all cluster into the same two node
    clusters (a group of 3 for dual-carriageway detection): two edges
    named "Main Street" (which should merge with each other) and one
    named "Cross Street" (which should merge with neither, since its
    name doesn't fuzzy-match). `order` controls which edge is added to
    the graph first, to prove the result doesn't depend on that."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    # Cluster A: nodes 1, 2, 3, all within the default 15m cluster radius.
    G.add_node(1, x=0, y=0)
    G.add_node(2, x=1, y=0)
    G.add_node(3, x=2, y=0)
    # Cluster B: nodes 11, 12, 13, likewise.
    G.add_node(11, x=100, y=0)
    G.add_node(12, x=101, y=0)
    G.add_node(13, x=102, y=0)

    specs = {
        "cross": (1, 11, "Cross Street", 901),
        "main_a": (2, 12, "Main Street", 902),
        "main_b": (3, 13, "Main Street", 903),
    }
    for key in order:
        u, v, name, osmid = specs[key]
        geom = LineString([(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])])
        G.add_edge(u, v, 0, highway="residential", name=name, geometry=geom, length=geom.length, osmid=osmid)
    return G


@pytest.mark.parametrize(
    "order",
    [
        ["cross", "main_a", "main_b"],
        ["main_a", "main_b", "cross"],
        ["main_b", "main_a", "cross"],
        ["main_a", "cross", "main_b"],
    ],
)
def test_dual_carriageway_group_merge_is_order_independent(order):
    """A group of more than two edges sharing a dual-carriageway
    cluster-node-pair (see `_three_way_cluster_group_graph`) must merge
    by comparing every pair, not by picking one arbitrary "representative"
    member and only comparing others against it. An earlier version did
    the latter: if the representative happened to be the one edge whose
    name doesn't match the other two (a function of which edge is
    processed first, not geometry), the other two - which DO share a
    matching name and should merge - would never even be compared to
    each other, and the result would silently depend on input order.
    Regardless of which of the three edges is added to the graph first,
    the two "Main Street" edges must end up sharing a street_id and the
    "Cross Street" edge must not share it with either."""
    G = _three_way_cluster_group_graph(order)
    _, edges_gdf, _ = street_ids.label_streets(G)

    def sid_of(u, v):
        row = edges_gdf.loc[(u, v, 0)]
        return row["street_id"]

    main_a_sid = sid_of(2, 12)
    main_b_sid = sid_of(3, 13)
    cross_sid = sid_of(1, 11)

    assert not pd.isna(main_a_sid)
    assert not pd.isna(main_b_sid)
    assert main_a_sid == main_b_sid
    assert pd.isna(cross_sid) or cross_sid != main_a_sid


def _divided_avenue_uneven_carriageways(G):
    """One side of a divided avenue mapped as three chained ways (nodes 1-4
    along y=0), the other side mapped as a single long way 20m away (y=20)
    whose own endpoints (x=50, x=250) don't land near any node of the long
    side, only somewhere along its length - the case node-cluster-based
    dual-carriageway detection can't catch, since neither the short edge's
    endpoints nor any long-side node are within the default 15m cluster
    radius of each other. A short unrelated cross-street (a median
    crossover, in real-world terms) links node 2 to node 5, so the two
    sides are reachable from each other within a couple of hops - without
    it, the parallel-street merge's connectivity requirement would
    correctly refuse to merge two edges that merely happen to be close
    and parallel with no real relationship."""
    G.add_node(1, x=0, y=0)
    G.add_node(2, x=100, y=0)
    G.add_node(3, x=200, y=0)
    G.add_node(4, x=300, y=0)
    _oneway_edge(G, 1, 2, (0, 0), (100, 0), highway="primary", osmid=201)
    _oneway_edge(G, 2, 3, (100, 0), (200, 0), highway="primary", osmid=202)
    _oneway_edge(G, 3, 4, (200, 0), (300, 0), highway="primary", osmid=203)
    _oneway_edge(G, 5, 6, (250, 20), (50, 20), highway="primary", osmid=204)
    _oneway_edge(G, 2, 5, (100, 0), (250, 20), highway="residential", osmid=205)
    return G


def test_parallel_dist_m_disabled_by_default_leaves_uneven_carriageways_apart():
    """Without parallel_dist_m, the short offset carriageway from
    _divided_avenue_uneven_carriageways gets its own street_id: it shares
    no node cluster with the long side, so none of the existing rules
    connect it.

    min_street_length_m=0: this test is specifically about the
    parallel_dist_m-gated merge, not "Redistributing too-short streets" -
    left at the library default (200m), the short residential
    cross-street connector (osmid 205) would legitimately get
    redistributed into the primary avenue it touches (rank-agnostic for
    that feature), which is correct behavior for that separate feature
    but defeats what this test checks. parallel_pass_touching_dist_m/
    parallel_pass_non_touching_dist_m=0: this test is also specifically
    about parallel_dist_m being the gate, not the separate, always-on
    whole-street_id parallel pass, which would otherwise join the 20m-
    offset carriageways on its own regardless of parallel_dist_m."""
    G = _divided_avenue_uneven_carriageways(nx.MultiDiGraph(crs="EPSG:3857"))

    _, edges_gdf, streets_df = street_ids.label_streets(
        G,
        min_street_length_m=0,
        parallel_pass_touching_dist_m=0,
        parallel_pass_non_touching_dist_m=0,
    )

    assert len(streets_df) == 3  # long side, short side, and the cross-street connector
    long_id = street_id_of(edges_gdf, 1, 2)
    assert street_id_of(edges_gdf, 2, 3) == long_id
    assert street_id_of(edges_gdf, 3, 4) == long_id
    assert street_id_of(edges_gdf, 5, 6) != long_id


def _merge_case(
    name_a="Main Ave", name_b="Main Ave", rank_a=2, rank_b=2, connected=True, dist_m=30.0
):
    """Build the inputs for a direct _merge_fully_covered_streets call:
    a long street A (two 250m segments, sid 1) and a short street B (one
    100m segment, sid 2) running parallel 20m away, fully within B's own
    length of A's combined geometry."""
    geom_a1 = LineString([(0, 0), (250, 0)])
    geom_a2 = LineString([(250, 0), (500, 0)])
    geom_b = LineString([(100, 20), (200, 20)])
    eids = ["a1", "a2", "b"]
    u_arr = [1, 2, 10]
    v_arr = [2, 3, 11]
    geoms = [geom_a1, geom_a2, geom_b]
    lengths = [g.length for g in geoms]
    street_id_arr = np.array([1, 1, 2])
    name_arr = [name_a, name_a, name_b]
    rank_arr = [rank_a, rank_a, rank_b]
    adjacency = {1: {2}, 2: {1, 3}, 3: {2}, 10: {11}, 11: {10}}
    if connected:
        adjacency[2].add(10)
        adjacency[10].add(2)
    return street_ids._merge_fully_covered_streets(
        eids,
        u_arr,
        v_arr,
        street_id_arr,
        lengths,
        geoms,
        name_arr,
        rank_arr,
        adjacency,
        dist_m,
        8,
        82.0,
    )


def test_structure_type_column_resolves_tunnel_and_bridge_tags():
    """edges_gdf always carries a structure_type column: "tunnel" or
    "bridge" from OSM's separate tunnel/bridge tags, or None for a
    plain at-grade edge - regardless of parallel_dist_m."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _oneway_edge(G, 1, 2, (0, 0), (100, 0), highway="primary", osmid=1)
    G.edges[1, 2, 0]["tunnel"] = "yes"
    _oneway_edge(G, 3, 4, (0, 100), (100, 100), highway="primary", osmid=2)
    G.edges[3, 4, 0]["bridge"] = "viaduct"
    _oneway_edge(G, 5, 6, (0, 200), (100, 200), highway="primary", osmid=3)

    _, edges_gdf, _ = street_ids.label_streets(G)

    assert edges_gdf.loc[(1, 2, 0), "structure_type"] == "tunnel"
    assert edges_gdf.loc[(3, 4, 0), "structure_type"] == "bridge"
    assert edges_gdf.loc[(5, 6, 0), "structure_type"] is None


def test_private_access_edges_never_get_a_street_id():
    """An edge tagged access=private is excluded from every matching
    rule and always ends up with street_id=None, even when it directly
    touches, shares a name with, and is perfectly collinear with a real
    public street at both ends - a private driveway must never bridge
    two unrelated public street segments together just because it
    happens to connect them, and must never carry its own id either."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 1, 2, (0, 0), (100, 0), name="Main Street", osmid=1)
    _add_edge(G, 2, 3, (100, 0), (200, 0), name="Main Street", osmid=2, access="private")
    _add_edge(G, 3, 4, (200, 0), (300, 0), name="Main Street", osmid=3)

    _, edges_gdf, streets_df = street_ids.label_streets(G, parallel_dist_m=30.0)

    assert pd.isna(street_id_of(edges_gdf, 2, 3))
    # The two real segments on either side must not have been bridged
    # into one street_id through the private edge either.
    assert street_id_of(edges_gdf, 1, 2) != street_id_of(edges_gdf, 3, 4)


def test_private_access_values_argument_overrides_the_default():
    """private_access_values is overridable: a custom access value not
    in the default set (just "private") is treated the same way."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 1, 2, (0, 0), (100, 0), name="Main Street", osmid=1, access="customers")

    _, edges_gdf, _ = street_ids.label_streets(G, private_access_values={"customers"})

    assert pd.isna(street_id_of(edges_gdf, 1, 2))


def test_is_drivable_column_flags_highway_types():
    """edges_gdf always carries an is_drivable column (regardless of
    parallel_dist_m), True for a car-usable highway type and False for
    a pedestrian/cycle-only one - see DEFAULT_NON_DRIVABLE_HIGHWAYS."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _oneway_edge(G, 1, 2, (0, 0), (100, 0), highway="primary", osmid=1)
    _oneway_edge(G, 3, 4, (0, 100), (100, 100), highway="footway", osmid=2)

    _, edges_gdf, _ = street_ids.label_streets(G)

    assert bool(edges_gdf.loc[(1, 2, 0), "is_drivable"]) is True
    assert bool(edges_gdf.loc[(3, 4, 0), "is_drivable"]) is False


def test_non_drivable_edge_kept_when_disabled_by_default():
    """The same layout as above, but parallel_dist_m left at its default
    (0, disabled): the footway keeps its own street_id, since pruning
    reuses parallel_dist_m as its separation distance and is a no-op
    when that's 0."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _oneway_edge(G, 1, 2, (0, 0), (200, 0), highway="primary", osmid=1)
    _oneway_edge(G, 3, 4, (0, 20), (200, 20), highway="footway", osmid=2)

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert pd.notna(edges_gdf.loc[(3, 4, 0), "street_id"])
    assert len(streets_df) == 2


def test_non_drivable_edge_far_from_any_road_keeps_its_id():
    """A footway that mostly *isn't* near the drivable network (e.g. a
    park path) is a real, distinct street in its own right and keeps
    its street_id even with pruning enabled."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _oneway_edge(G, 1, 2, (0, 0), (200, 0), highway="primary", osmid=1)
    _oneway_edge(G, 3, 4, (0, 1000), (200, 1000), highway="footway", osmid=2)

    _, edges_gdf, streets_df = street_ids.label_streets(G, parallel_dist_m=30.0)

    assert pd.notna(edges_gdf.loc[(3, 4, 0), "street_id"])
    assert len(streets_df) == 2


def test_connector_edge_inherits_the_single_neighboring_street_id():
    """A short connector edge whose own endpoints cluster together (e.g.
    a slip road inside one intersection) doesn't get its own identity.
    It bridges whichever single street_id already touches that cluster."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (-200, 0), (0, 0), name="Main Street")
    _add_edge(G, 1, 2, (0, 0), (5, 0), name=None, highway="service")
    _add_edge(G, 2, 3, (5, 0), (205, 0), name="Main Street")

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    main_street_id = streets_df.loc[streets_df["street_name"] == "Main Street", "street_id"].iloc[
        0
    ]
    assert street_id_of(edges_gdf, 1, 2) == main_street_id


def test_connector_edge_prefers_higher_rank_neighbor():
    """When a connector's cluster touches more than one distinct
    street_id, it inherits the higher-priority (lower street_rank) one -
    here a primary arm over a residential arm."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (-200, 0), (0, 0), name=None, highway="primary")
    _add_edge(G, 1, 2, (0, 0), (5, 0), name=None, highway="service")
    _add_edge(G, 2, 3, (5, 0), (5, 200), name=None, highway="residential")

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    primary_id = streets_df.loc[streets_df["highway"] == "primary", "street_id"].iloc[0]
    assert street_id_of(edges_gdf, 1, 2) == primary_id


def test_isolated_connector_edge_gets_no_street_id():
    """A connector with no resolvable neighbor at all (nothing else
    touches its cluster) is left without a street_id rather than
    minted its own - it isn't really a street in its own right."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 10, 11, (0, 0), (5, 0), name=None, highway="service")

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 0
    assert edges_gdf["street_id"].isna().all()


def test_max_two_edges_per_node_splits_off_the_shortest_branch():
    """Three same-name arms all want the same street_id at one node
    (rule: same name, so no rank/angle constraint applies at all), but
    at most two edges may share a street_id at any node. The shortest
    branch (a short spur, not one of the two long collinear arms) is
    the one cut into a new street_id.

    min_street_length_m=0: this test is specifically about
    _enforce_max_two_per_node's own cutting behavior, not about
    "Redistributing too-short streets" - left at the library default
    (200m), the cut-off 20m spur (same name as the long street) would
    legitimately get redistributed right back into it afterward, which
    is correct behavior for that separate feature but not what this
    test checks."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 1, 0, (-300, 0), (0, 0), name="Main Street")  # long, west
    _add_edge(G, 0, 2, (0, 0), (300, 0), name="Main Street")  # long, east
    _add_edge(G, 0, 3, (0, 0), (0, 20), name="Main Street")  # short spur, north

    _, edges_gdf, streets_df = street_ids.label_streets(G, min_street_length_m=0)

    assert len(streets_df) == 2
    long_id_west = street_id_of(edges_gdf, 1, 0)
    long_id_east = street_id_of(edges_gdf, 0, 2)
    short_id = street_id_of(edges_gdf, 0, 3)
    assert long_id_west == long_id_east
    assert short_id != long_id_west


def test_name_similarity_threshold_is_configurable():
    """Raising name_similarity_threshold past a fuzzy pair's actual
    score must stop them from merging."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (200, 0), name="Elm Street")
    _add_edge(G, 1, 2, (200, 0), (400, 0), name="Elm Streat")  # typo

    _, edges_gdf, streets_df = street_ids.label_streets(G, name_similarity_threshold=100.0)

    assert len(streets_df) == 2
    assert street_id_of(edges_gdf, 0, 1) != street_id_of(edges_gdf, 1, 2)


def test_divided_avenue_stays_one_street_through_multiple_cross_streets():
    """A long divided avenue (two one-way carriageways, offset a few
    meters apart) crossed by three separate two-way residential streets
    must still collapse to a single street_id end to end: each
    carriageway merges with itself at every crossing (same rank, same
    direction, rule 3), and the two carriageways auto-merge with each
    other via node clustering at at least one of their matching
    segments. The three cross streets, being the non-dominant tier at
    every crossing, are each cut into their own id (rule 2), and don't
    merge with each other either."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    xs = [0, 200, 400, 600, 800]
    for i in range(4):
        _oneway_edge(
            G, f"A{i}", f"A{i + 1}", (xs[i], 0), (xs[i + 1], 0), highway="primary", osmid=100 + i
        )
    for i in range(4, 0, -1):
        _oneway_edge(
            G, f"B{i}", f"B{i - 1}", (xs[i], 8), (xs[i - 1], 8), highway="primary", osmid=200 + i
        )
    for i in (1, 2, 3):
        _add_edge(G, f"A{i}", f"N{i}", (xs[i], 0), (xs[i], 150), osmid=300 + i)
        _add_edge(G, f"A{i}", f"S{i}", (xs[i], 0), (xs[i], -150), osmid=400 + i)

    # min_street_length_m=0: this test is specifically about rule 2's
    # rank-based cutting of the cross streets, not "Redistributing
    # too-short streets" - left at the library default, the short,
    # unnamed, cut-off cross-street stubs would legitimately get
    # redistributed right back into the avenue they touch (rank-agnostic
    # for that feature), which is correct behavior for that separate
    # feature but defeats what this test checks. parallel_dist_m=0: each
    # crossing's north/south arms are collinear through the same node and
    # would otherwise legitimately get merged by the parallel-proximity
    # feature (same rank, unnamed, aligned) - a separate concern from the
    # rank-based cut this test checks.
    _, edges_gdf, streets_df = street_ids.label_streets(
        G, min_street_length_m=0, parallel_dist_m=0
    )

    avenue_rows = streets_df.loc[streets_df["highway"] == "primary"]
    assert len(avenue_rows) == 1
    assert set(avenue_rows["osmids"].iloc[0].split(";")) == {
        str(o) for o in list(range(100, 104)) + list(range(201, 205))
    }

    avenue_id = avenue_rows["street_id"].iloc[0]
    for i in range(4):
        assert street_id_of(edges_gdf, f"A{i}", f"A{i + 1}") == avenue_id
    for i in range(4, 0, -1):
        assert street_id_of(edges_gdf, f"B{i}", f"B{i - 1}") == avenue_id

    # 6 cross-street arms (3 crossings x north/south), each its own id,
    # none sharing an id with each other or with the avenue.
    cross_ids = {street_id_of(edges_gdf, f"A{i}", f"N{i}") for i in (1, 2, 3)}
    cross_ids |= {street_id_of(edges_gdf, f"A{i}", f"S{i}") for i in (1, 2, 3)}
    assert len(cross_ids) == 6
    assert avenue_id not in cross_ids


def test_curving_boulevard_merges_despite_large_total_turn():
    """A street built from many short segments, each turning only 10 deg
    from the last, stays ONE street_id start to end even though the
    total turn across all 8 segments is 80 deg - well past
    angle_threshold_deg (20 deg default). Every merge is checked
    PAIRWISE, against the bearing of the two specific edges actually
    being joined right now, never against a group's own first/original
    member - a long, gently curving real street is legitimately one
    street_id even when its overall bearing end to end has changed a
    lot, as long as each real physical step along it stays within
    tolerance. See "No drift" in _assign_external_street_ids_for_pass's
    own docstring and report/street_identity.md."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    x, y, heading = 0.0, 0.0, 0.0
    points = [(x, y)]
    for _ in range(8):
        heading += 10
        rad = math.radians(heading)
        x += 50 * math.cos(rad)
        y += 50 * math.sin(rad)
        points.append((x, y))
    for i in range(8):
        _add_edge(G, i, i + 1, points[i], points[i + 1], osmid=i)

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    ids = [street_id_of(edges_gdf, i, i + 1) for i in range(8)]
    assert len(set(ids)) == 1


def test_divided_avenue_with_side_street_joining_one_carriageway():
    """A residential side street T-joins directly onto one carriageway
    of a divided avenue (not at a crossing shared by both carriageways).
    The avenue still merges into one street_id via the usual
    dual-carriageway auto-merge, and the side street is cut from it at
    the real shared node (rule 2), same as an ordinary T-junction."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _oneway_edge(G, 1, 3, (0, 0), (400, 0), highway="primary", osmid=101)
    _oneway_edge(G, 4, 2, (403, 6), (3, 6), highway="primary", osmid=102)
    _add_edge(G, 3, 5, (400, 0), (400, 150), osmid=201)

    # min_street_length_m=0: this test is specifically about rule 2's
    # rank-based cutting of the side street, not "Redistributing
    # too-short streets" - left at the library default (200m), the
    # short, unnamed, cut-off side street would legitimately get
    # redistributed right back into the avenue it touches (rank-agnostic
    # for that feature), which is correct behavior for that separate
    # feature but defeats what this test checks.
    _, edges_gdf, streets_df = street_ids.label_streets(G, min_street_length_m=0)

    assert len(streets_df) == 2
    avenue_id = street_id_of(edges_gdf, 1, 3)
    assert street_id_of(edges_gdf, 4, 2) == avenue_id
    assert street_id_of(edges_gdf, 3, 5) != avenue_id


def test_small_roundabout_with_mixed_rank_approaches():
    """A small roundabout (four ring segments whose corners all cluster
    together) with four approaches of different rank: primary, secondary,
    residential, and service. Each ring segment is a connector (its own
    two endpoints share one node cluster), so none of them are matched
    by name or rank on their own - each is assigned by direction to
    whichever nearby approach it best continues (see "Internal connector
    edges" in report/street_identity.md), independently per ring
    segment. No two of the four (differently-ranked, differently-
    directed) approaches ever end up sharing a street_id with each
    other, regardless of which ring segment(s) they each pick up."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    r = 6.0
    corners = {"E": (r, 0), "N": (0, r), "W": (-r, 0), "S": (0, -r)}
    ring = ["E", "N", "W", "S"]
    for i in range(4):
        a, b = ring[i], ring[(i + 1) % 4]
        _oneway_edge(G, a, b, corners[a], corners[b], highway="service", osmid=900 + i)

    _add_edge(G, "N", "N_far", (0, r), (0, 200), highway="primary", osmid=1)
    _add_edge(G, "E", "E_far", (r, 0), (200, 0), highway="secondary", osmid=2)
    _add_edge(G, "S", "S_far", (0, -r), (0, -200), highway="residential", osmid=3)
    _add_edge(G, "W", "W_far", (-r, 0), (-200, 0), highway="service", osmid=4)

    # node_cluster_m=15 (the library's old default): this test's own
    # design ("four ring segments whose corners all cluster together")
    # needs opposite ring corners (12m apart, r=6) to cluster together,
    # which the current lower default no longer covers - pinned
    # explicitly so it keeps testing the same connector/two-per-node
    # interaction regardless of the library default. parallel_dist_m=0:
    # the tiny ring (service rank) and the same-rank "W" approach would
    # otherwise legitimately get merged by the parallel-proximity
    # feature (both service, unnamed, close/aligned near the shared
    # corner) - a separate concern from what this test checks.
    _, edges_gdf, streets_df = street_ids.label_streets(
        G, node_cluster_m=15, parallel_dist_m=0
    )

    approach_ids = {
        street_id_of(edges_gdf, "N", "N_far"),
        street_id_of(edges_gdf, "E", "E_far"),
        street_id_of(edges_gdf, "S", "S_far"),
        street_id_of(edges_gdf, "W", "W_far"),
    }
    assert len(approach_ids) == 4


def test_parallel_merge_rescues_a_busy_intersection_name_split():
    """Real-world reproduction (Calle de Fernández de la Hoz at a busy
    crossing in Madrid): three directly-chained, identically-named,
    same-rank edges that still end up with three different street_ids
    without the parallel-street merge, because a fourth, differently
    -named edge also touches the middle node and rule 4's "at most one
    partner per node" cap pairs it with one of the two real continuations
    instead. parallel_dist_m rescues this: the two edges it wrongly
    left apart are close (they touch), parallel (collinear), and
    connected (a shared node, zero hops), so they merge anyway."""
    G = nx.MultiDiGraph(crs="EPSG:32630")
    _add_edge(G, 1, 2, (0, 0), (0, 950), "residential", "Calle de Fernández de la Hoz", osmid=8029920)
    _add_edge(G, 2, 3, (0, 950), (1, 964), "residential", "Calle de Fernández de la Hoz", osmid=868615044)
    _add_edge(G, 3, 4, (1, 964), (5, 1026), "residential", "Calle de Fernández de la Hoz", osmid=8029921)
    # An unrelated named edge sharing node 2, so rule 4's greedy pairing
    # can only keep one of the two real continuations there.
    _add_edge(G, 2, 5, (0, 950), (100, 950), "residential", "Calle Santa Engracia", osmid=999)

    _, edges_gdf, streets_df = street_ids.label_streets(G, parallel_dist_m=30.0)

    id_a = street_id_of(edges_gdf, 1, 2)
    id_b = street_id_of(edges_gdf, 2, 3)
    id_c = street_id_of(edges_gdf, 3, 4)
    assert id_a == id_b == id_c


# ---------------------------------------------------------------------------
# Rule 1: final name-conflict invariant pass (_split_name_conflicts)
# ---------------------------------------------------------------------------


def test_main_direction_flags_longest_path_on_a_y_branch():
    """Direct exercise of _compute_main_direction (Rule 3) on a
    synthetic Y sharing one street_id: a long through-line (1->2->3)
    plus a short dead-end spur off node 2. Every edge on the long
    through-line must be flagged main; the short spur must not."""
    eids = [(1, 2, 0), (2, 3, 0), (2, 4, 0)]
    u_arr = [1, 2, 2]
    v_arr = [2, 3, 4]
    length_arr = [200.0, 200.0, 30.0]
    street_id_arr = [1, 1, 1]
    is_drivable_arr = [True, True, True]

    is_main, _main_length = street_ids._compute_main_direction(
        eids, u_arr, v_arr, length_arr, street_id_arr, is_drivable_arr
    )

    assert is_main[(1, 2, 0)] is True
    assert is_main[(2, 3, 0)] is True
    assert is_main[(2, 4, 0)] is False


def test_main_direction_picks_the_single_longest_path_across_components():
    """_compute_main_direction (Rule 3) directly, on a street_id whose
    own edges span two disconnected components (can happen after
    stripping/pruning elsewhere in label_streets): the single longest
    path must be picked across *all* components combined, not
    independently per component."""
    eids = [(1, 2, 0), (2, 3, 0), (10, 11, 0)]
    u_arr = [1, 2, 10]
    v_arr = [2, 3, 11]
    length_arr = [50.0, 60.0, 1000.0]
    street_id_arr = [1, 1, 1]
    is_drivable_arr = [True, True, True]

    is_main, _main_length = street_ids._compute_main_direction(
        eids, u_arr, v_arr, length_arr, street_id_arr, is_drivable_arr
    )

    assert is_main[(10, 11, 0)] is True
    assert is_main[(1, 2, 0)] is False
    assert is_main[(2, 3, 0)] is False


def test_main_direction_prefers_drivable_only_path_within_5_pct():
    """A street_id can mix drivable and non-drivable edges (a footway
    donated the drivable street's id - see "Donating a drivable street's
    id to nearby non-drivable furniture"). A straight drivable line
    1-2-3-4 (100m per edge, 300m total) plus a short non-drivable spur
    4-5 (10m) extends the network's own all-edges diameter to 310m
    (node 1 to node 5), but the drivable-only diameter (300m, node 1 to
    node 4) is well within 5% of that (300 >= 310 * 0.95 = 294.5), so
    the drivable-only path must be the one flagged main - the spur must
    not be, even though it's part of the same street_id and does sit on
    the network-wide diameter."""
    eids = [(1, 2, 0), (2, 3, 0), (3, 4, 0), (4, 5, 0)]
    u_arr = [1, 2, 3, 4]
    v_arr = [2, 3, 4, 5]
    length_arr = [100.0, 100.0, 100.0, 10.0]
    street_id_arr = [1, 1, 1, 1]
    is_drivable_arr = [True, True, True, False]

    is_main, _main_length = street_ids._compute_main_direction(
        eids, u_arr, v_arr, length_arr, street_id_arr, is_drivable_arr
    )

    assert is_main[(1, 2, 0)] is True
    assert is_main[(2, 3, 0)] is True
    assert is_main[(3, 4, 0)] is True
    assert is_main[(4, 5, 0)] is False


def test_main_direction_falls_back_to_all_edges_path_beyond_5_pct():
    """Same shape as the test above, but the non-drivable spur is long
    enough (50m) that the drivable-only diameter (300m) is more than 5%
    shorter than the all-edges diameter (350m: 300 < 350 * 0.95 =
    332.5) - the non-drivable edge must be bridging a real gap in this
    street_id's own length, so the all-edges path (including the spur)
    is kept as main instead of losing that much of the street."""
    eids = [(1, 2, 0), (2, 3, 0), (3, 4, 0), (4, 5, 0)]
    u_arr = [1, 2, 3, 4]
    v_arr = [2, 3, 4, 5]
    length_arr = [100.0, 100.0, 100.0, 50.0]
    street_id_arr = [1, 1, 1, 1]
    is_drivable_arr = [True, True, True, False]

    is_main, _main_length = street_ids._compute_main_direction(
        eids, u_arr, v_arr, length_arr, street_id_arr, is_drivable_arr
    )

    assert is_main[(1, 2, 0)] is True
    assert is_main[(2, 3, 0)] is True
    assert is_main[(3, 4, 0)] is True
    assert is_main[(4, 5, 0)] is True


def test_parking_aisle_edges_never_get_a_street_id():
    """An edge tagged service=parking_aisle (a parking lot's internal
    way, OSM's standard tag for that) is excluded from every matching
    rule and always ends up with street_id=None, mirroring
    test_private_access_edges_never_get_a_street_id exactly: even
    directly touching, sharing a name with, and being perfectly
    collinear with a real public street at both ends, a parking aisle
    must never bridge two unrelated public street segments together and
    must never carry its own id either."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 1, 2, (0, 0), (100, 0), highway="service", name="Main Street", osmid=1)
    _add_edge(
        G, 2, 3, (100, 0), (200, 0), highway="service", name="Main Street", osmid=2,
        service="parking_aisle",
    )
    _add_edge(G, 3, 4, (200, 0), (300, 0), highway="service", name="Main Street", osmid=3)

    _, edges_gdf, streets_df = street_ids.label_streets(G, parallel_dist_m=30.0)

    assert pd.isna(street_id_of(edges_gdf, 2, 3))
    assert street_id_of(edges_gdf, 1, 2) != street_id_of(edges_gdf, 3, 4)


def test_parking_service_values_argument_overrides_the_default():
    """parking_service_values is overridable: a custom service value not
    in the default set (just "parking_aisle") is treated the same way."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 1, 2, (0, 0), (100, 0), highway="service", osmid=1, service="driveway")

    _, edges_gdf, _ = street_ids.label_streets(G, parking_service_values={"driveway"})

    assert pd.isna(street_id_of(edges_gdf, 1, 2))


def test_parking_service_list_valued_tag_is_recognized():
    """OSM's service tag can be list-valued after osmnx merges parallel
    or duplicate ways - a list containing "parking_aisle" as any of its
    elements must still be recognized, the same list-handling convention
    used for access/tunnel/bridge elsewhere in this module."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(
        G, 1, 2, (0, 0), (100, 0), highway="service", osmid=1,
        service=["driveway", "parking_aisle"],
    )

    _, edges_gdf, _ = street_ids.label_streets(G)

    assert pd.isna(street_id_of(edges_gdf, 1, 2))


def test_two_pass_assignment_keeps_crossing_footway_street_id_continuous():
    """Requirement B bug repro: a footway that crosses a drivable primary
    road at a single point must keep one continuous street_id on both
    sides of the crossing, not get cut into two fragments there.

    Before the two-pass fix, every edge (drivable and non-drivable) ran
    through one interleaved matching pass together. At the shared
    crossing node, the primary road (rank 1) was the dominant tier and
    the footway (rank 5) was cut there by rule 2 - "a lower-rank edge is
    cut by any higher-rank street crossing it" - even though rule 2 was
    designed for two real streets crossing each other, not an unrelated
    footway crossing a road it has nothing to do with. That fragmented
    the footway into two separate street_ids at the one point it
    happened to cross a road, and (with parallel_dist_m enabled) each
    short fragment could then look mostly redundant with the nearby
    drivable network and get pruned away entirely - see "Two-pass
    assignment: drivable edges first, then non-drivable" in
    report/street_identity.md.

    After the fix, Pass 2 resolves the non-drivable subnetwork on its
    own, as if the primary road didn't exist - the footway's two halves
    are both rank 5 (the only, and therefore dominant, tier in that
    pass) and share the same direction at the crossing node, so rule 3
    merges them into one street_id, same as any other straight-through
    unnamed street. Confirmed by reasoning directly from the single-pass
    vs. two-pass mechanism above (this repo has no earlier commit of the
    single-pass code to run this same graph against for a literal
    before/after diff, since it isn't a git repository)."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 1, 2, (-100, 0), (0, 0), highway="primary", osmid=1)
    _add_edge(G, 2, 3, (0, 0), (100, 0), highway="primary", osmid=2)
    _add_edge(G, 4, 2, (0, -100), (0, 0), highway="footway", osmid=3)
    _add_edge(G, 2, 5, (0, 0), (0, 100), highway="footway", osmid=4)

    # parallel_dist_m=0: with it enabled, the footway's own crossing
    # point is close enough to the primary road to trigger one of this
    # module's proximity-based merge/donation features - a separate
    # concern from the two-pass continuity this test checks.
    _, edges_gdf, streets_df = street_ids.label_streets(
        G, angle_threshold_deg=20, parallel_dist_m=0
    )

    footway_south_id = street_id_of(edges_gdf, 4, 2)
    footway_north_id = street_id_of(edges_gdf, 2, 5)
    assert pd.notna(footway_south_id)
    assert footway_south_id == footway_north_id

    primary_id = street_id_of(edges_gdf, 1, 2)
    assert street_id_of(edges_gdf, 2, 3) == primary_id
    assert primary_id != footway_south_id


# --- min_street_length_m: redistributing too-short streets -----------------


def test_min_street_length_m_does_not_force_a_branching_merge():
    """A street_id whose own main path is under min_street_length_m is
    "too short" and gets a chance to be reassigned to a nearby valid
    street_id (see "Redistributing too-short streets" in
    report/street_identity.md) - but that reassignment can never be
    allowed to create a genuine branch (two incompatible directions
    meeting at one node_id under the same street_id, see "No more than
    one direction per node_id"). Here a connector directly touches a
    long, valid street but runs perpendicular to it: an earlier version
    of this fallback treated "touches it directly" as a strong enough
    signal to merge regardless of direction, which is exactly a branch.
    The branching invariant (``_split_branching_street_ids``), applied
    to the FINAL composition after every reassignment step, now splits
    any such forced merge back apart - the connector and the too-short
    street each end up on their own separate id instead, never forming
    a branch with the long street or with each other."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (300, 0), highway="residential", osmid=1)  # 300m: valid
    _add_edge(G, 10, 11, (300, 25), (340, 25), highway="residential", osmid=2)  # 40m: too short, 25m away
    _add_edge(G, 1, 10, (300, 0), (300, 25), highway="residential", osmid=3)  # perpendicular connector

    _, edges_gdf, streets_df = street_ids.label_streets(G, parallel_dist_m=30)

    long_id = street_id_of(edges_gdf, 0, 1)
    short_id = street_id_of(edges_gdf, 10, 11)
    connector_id = street_id_of(edges_gdf, 1, 10)
    assert pd.notna(long_id)
    # None of the three end up sharing a street_id with either of the
    # other two - no branch, anywhere.
    assert len({long_id, short_id, connector_id}) == 3


def test_min_street_length_m_nearest_fallback_disabled_when_parallel_dist_m_is_disabled():
    """The nearest-candidate fallback is gated by parallel_dist_m, not
    unbounded - see the docstring note in _redistribute_short_streets on
    why this has to match label_streets' own final connectivity
    invariant exactly. With the library default (parallel_dist_m=0,
    disabled), a too-short street with no touching candidate stays on
    its own separate street_id no matter how close a valid candidate
    is - only a literal touching match can redistribute it."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (300, 0), highway="residential", osmid=1)  # 300m: valid
    _add_edge(G, 10, 11, (300, 25), (340, 25), highway="residential", osmid=2)  # 40m: too short, 25m away

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    long_id = street_id_of(edges_gdf, 0, 1)
    short_id = street_id_of(edges_gdf, 10, 11)
    assert short_id != long_id
    assert len(streets_df) == 2


def test_min_street_length_m_prefers_a_touching_valid_street_over_a_nearer_one():
    """A too-short street's own criteria for picking a target: among
    eligible, valid-length candidates, one it actually TOUCHES (shares a
    node with) always wins over one it's merely close to, even if the
    touching candidate is shorter than the nearby-but-not-touching one.
    Only when NOTHING touches does distance decide at all.

    Here the too-short street shares a node with a 300m valid street A,
    and passes only ~20m from (but never touches, and never clusters
    with A's own node under the default 15m node_cluster_m) a longer,
    600m valid street C. Under a pure nearest-by-distance rule, C would
    win (it's both closer and longer) - but A must win instead, since
    sharing a node is the much stronger signal that it's actually the
    same real street continuing, not just an incidentally nearby one.

    The too-short street continues mostly straight out of A (~6 deg off,
    well within the default direction tolerance AND under the default
    angle_threshold_deg - so the newer main-path turn-split doesn't
    split it back apart right after this reassignment merges it) rather
    than turning a hard corner - a perpendicular touching edge would
    instead be kept apart from A by the direction-consistency invariant
    (see "No more than one direction per node_id" in
    report/street_identity.md), which isn't what this test is about."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (300, 0), highway="residential", osmid=1)  # A: 300m, valid
    _add_edge(G, 1, 2, (300, 0), (340, 4), highway="residential", osmid=2)  # too short, touches A at node 1
    _add_edge(G, 20, 21, (320, 0), (320, 600), highway="residential", osmid=3)  # C: 600m, valid, close but not touching

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    id_a = street_id_of(edges_gdf, 0, 1)
    id_short = street_id_of(edges_gdf, 1, 2)
    id_c = street_id_of(edges_gdf, 20, 21)
    assert id_short == id_a
    assert id_short != id_c


def test_min_street_length_m_skips_a_name_incompatible_nearer_candidate():
    """A too-short street's own real name must still gate eligibility the
    same way every other merge/donation site in this module does: the
    nearest candidate here is a closer, but differently-named, valid
    street ("Avenida Cercana") that doesn't fuzzy-match the too-short
    street's own name ("Calle Corta") - so it's skipped, and the
    redistribution instead reaches a second, still-within-parallel_dist_m,
    name-compatible (unnamed, so donation either way is allowed) valid
    candidate via the 90% rule instead of leaving the edge stranded."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    # Nearby (10m) but incompatible name - must be skipped.
    _add_edge(
        G, 0, 1, (0, 100), (300, 100), highway="residential", name="Avenida Cercana", osmid=1
    )
    # On the OTHER side of the too-short street (25m away, still within
    # parallel_dist_m of it, and its own entire length uniformly so -
    # satisfying the 90% rule), unnamed - eligible via the "exactly one
    # named" branch. Kept 35m from the incompatible candidate (beyond
    # parallel_dist_m) so the two candidates don't get merged with each
    # other by an earlier, unrelated parallel-proximity donation before
    # redistribution even runs.
    _add_edge(G, 100, 101, (0, 65), (300, 65), highway="residential", osmid=2)
    # The too-short street itself, between the two candidates above.
    _add_edge(G, 10, 11, (0, 90), (40, 90), highway="residential", name="Calle Corta", osmid=3)

    _, edges_gdf, streets_df = street_ids.label_streets(G, parallel_dist_m=30)

    short_id = street_id_of(edges_gdf, 10, 11)
    incompatible_id = street_id_of(edges_gdf, 0, 1)
    far_id = street_id_of(edges_gdf, 100, 101)
    assert short_id != incompatible_id
    assert short_id == far_id


def test_min_street_length_m_leaves_a_short_street_alone_when_nothing_valid_exists():
    """If literally no street_id anywhere meets min_street_length_m, a
    too-short street_id has nothing to redistribute into - it keeps its
    own edges rather than losing them or getting merged somewhere
    ineligible."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (40, 0), highway="residential", osmid=1)
    _add_edge(G, 10, 11, (1000, 1000), (1030, 1000), highway="residential", osmid=2)

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    id_a = street_id_of(edges_gdf, 0, 1)
    id_b = street_id_of(edges_gdf, 10, 11)
    assert pd.notna(id_a)
    assert pd.notna(id_b)
    assert id_a != id_b
    assert len(streets_df) == 2


def test_min_street_length_m_disabled_leaves_short_streets_alone():
    """min_street_length_m=0 disables the whole redistribution step, same
    as every other 0-disables-it default convention in this module -
    the short street here is left exactly as label_streets would have
    produced it before this feature existed."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (300, 0), highway="residential", osmid=1)
    _add_edge(G, 10, 11, (1000, 1000), (1040, 1000), highway="residential", osmid=2)

    _, edges_gdf, streets_df = street_ids.label_streets(G, min_street_length_m=0)

    long_id = street_id_of(edges_gdf, 0, 1)
    short_id = street_id_of(edges_gdf, 10, 11)
    assert short_id != long_id
    assert len(streets_df) == 2


def test_min_street_length_m_recomputes_is_main_direction_after_redistribution():
    """Once a too-short street's edge is folded into a valid street_id
    that doesn't touch it at any node, that valid street_id's own edges
    now span more than one connected component - and is_main_direction
    must be recomputed fresh for the whole, final street_id composition
    (_compute_main_direction picks the single longest-diameter path
    across *all* of a street_id's components, marking every edge in
    every other component False). The formerly-standalone short edge
    (trivially is_main_direction=True on its own, single-edge street_id)
    must end up False once it's folded into the far larger street, while
    the long edge stays True. The too-short edge runs the same direction
    as the target, offset a constant 25m for its entire own length (well
    within a 30m parallel_dist_m for its whole extent, satisfying the
    90% rule), and beyond the default 15m node_cluster_m so it isn't
    already touching - this is the "90% rule" redistribution path (see
    "Redistributing too-short streets" in report/street_identity.md),
    not a direction match at a shared node (they share no node at all)."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (250, 0), highway="primary", osmid=1)  # 250m: valid
    _add_edge(G, 10, 11, (0, 25), (40, 25), highway="residential", osmid=2)  # 40m: too short, 25m away throughout

    _, edges_gdf, streets_df = street_ids.label_streets(G, parallel_dist_m=30)

    long_id = street_id_of(edges_gdf, 0, 1)
    short_id = street_id_of(edges_gdf, 10, 11)
    assert short_id == long_id
    assert bool(edges_gdf.loc[(0, 1, 0), "is_main_direction"]) is True
    assert bool(edges_gdf.loc[(10, 11, 0), "is_main_direction"]) is False

    assert bool(edges_gdf.loc[(0, 1, 0), "is_main_direction"]) is True
    assert bool(edges_gdf.loc[(10, 11, 0), "is_main_direction"]) is False


def test_min_street_length_m_redistributed_edge_never_becomes_main_even_when_it_would_extend_the_diameter():
    """A too-short street redistributed into a valid target because it
    TOUCHES it (see test_min_street_length_m_prefers_a_touching_valid_
    street_over_a_nearer_one) must never be flagged is_main_direction,
    even when including it would make the target's own diameter longer
    than using only its native edges. Target A is a straight 300m line
    (nodes 0-1-2, 150m per edge); a too-short 50m edge touches A at node
    2 and extends past it to node 3. If the absorbed edge were eligible
    for the main path, the longest diameter would run node 0 to node 3
    (350m) - but it must never be chosen, so the main path stays A's own
    native node-0-to-node-2 line (300m), and the absorbed edge is
    False."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (150, 0), highway="residential", osmid=1)
    _add_edge(G, 1, 2, (150, 0), (300, 0), highway="residential", osmid=2)
    # A different highway rank (footway, not residential) so this edge is
    # cut from A by rule 2 during ordinary node-touching matching (it's
    # collinear, but not the dominant tier at node 2) instead of merging
    # in the "normal" way - the only thing that then attaches it to A is
    # this redistribution step itself.
    _add_edge(G, 2, 3, (300, 0), (350, 0), highway="footway", osmid=3)  # too short, touches at node 2

    # min_street_length_m=200 (the library's old default, still above the
    # 50m edge under test) rather than the current 50m default, which
    # this 50m-exactly edge would no longer be strictly under.
    # parallel_dist_m=0, parallel_pass_touching/non_touching_dist_m=0:
    # with any of these enabled, the footway would otherwise attach to A
    # earlier via a proximity-based merge/cross-pass donation (not
    # tracked as "redistributed", so not excluded from the main path) -
    # this test needs redistribution specifically to be the only thing
    # that attaches it.
    _, edges_gdf, streets_df = street_ids.label_streets(
        G,
        min_street_length_m=200,
        parallel_dist_m=0,
        parallel_pass_touching_dist_m=0,
        parallel_pass_non_touching_dist_m=0,
    )

    target_id = street_id_of(edges_gdf, 0, 1)
    assert street_id_of(edges_gdf, 1, 2) == target_id
    assert street_id_of(edges_gdf, 2, 3) == target_id

    assert bool(edges_gdf.loc[(0, 1, 0), "is_main_direction"]) is True
    assert bool(edges_gdf.loc[(1, 2, 0), "is_main_direction"]) is True
    assert bool(edges_gdf.loc[(2, 3, 0), "is_main_direction"]) is False


def test_parallel_pass_joins_touching_street_ids_within_15m_central_distance():
    """Two street_ids that TOUCH (share a raw node) but were cut apart by
    rule 2 (different rank: primary then a residential continuation) are
    still, at the whole-street_id level, obviously one street - the
    parallel pass joins them since their own central segments are well
    within the default 15m touching threshold (short edges, so their
    central parts sit close to the shared node itself) and their central
    bearings are identical (both due east).

    min_street_length_m=0: isolates the parallel pass from redistribute-
    too-short's own (rank-agnostic, ungated) touching branch, which would
    otherwise join these two edges anyway for an unrelated reason."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (10, 0), highway="primary", osmid=1)
    _add_edge(G, 1, 2, (10, 0), (20, 0), highway="residential", osmid=2)

    _, edges_gdf, streets_df = street_ids.label_streets(G, min_street_length_m=0)

    id_a = street_id_of(edges_gdf, 0, 1)
    id_b = street_id_of(edges_gdf, 1, 2)
    assert id_a == id_b
    assert len(streets_df) == 1


def test_parallel_pass_joins_non_touching_street_ids_within_5m_central_distance():
    """Two street_ids that share NO node at all, but run close and
    parallel (3m apart, well within the default 5m non-touching
    threshold), are joined by the parallel pass - different highway rank
    (so the earlier, same-rank-requiring parallel-proximity merge can't
    already have done this), parallel_dist_m=0 (disabling that earlier
    merge and redistribute's own nearest fallback entirely), and
    node_cluster_m=0 (disabling node clustering, which would otherwise
    cluster these two edges' nearby-but-distinct node pairs together and
    auto-merge them via dual-carriageway detection - an unrelated
    mechanism from what this test checks) isolate this to being the
    parallel pass's own doing."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (100, 0), highway="primary", osmid=1)
    _add_edge(G, 10, 11, (0, 3), (100, 3), highway="residential", osmid=2)

    _, edges_gdf, streets_df = street_ids.label_streets(
        G, parallel_dist_m=0, node_cluster_m=0
    )

    id_a = street_id_of(edges_gdf, 0, 1)
    id_b = street_id_of(edges_gdf, 10, 11)
    assert id_a == id_b
    assert len(streets_df) == 1


def test_parallel_pass_leaves_non_touching_street_ids_apart_beyond_5m():
    """Same as the join case above, but 6m apart - just beyond the
    default 5m non-touching threshold - so the parallel pass leaves them
    as two separate street_ids. node_cluster_m=0 for the same reason as
    the join case above."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (100, 0), highway="primary", osmid=1)
    _add_edge(G, 10, 11, (0, 6), (100, 6), highway="residential", osmid=2)

    _, edges_gdf, streets_df = street_ids.label_streets(
        G, parallel_dist_m=0, node_cluster_m=0
    )

    id_a = street_id_of(edges_gdf, 0, 1)
    id_b = street_id_of(edges_gdf, 10, 11)
    assert id_a != id_b
    assert len(streets_df) == 2


def test_loop_edge_prefers_same_osmid_similar_direction_over_better_rank():
    """Internal connector (loop) edges - whose own two endpoints cluster
    to the same node_id - are no longer assigned purely by the best-
    ranked candidate present at that node_id. Here two real, distinct
    nodes A (0,0) and B (5,0), 5m apart, cluster together (node_cluster_m
    =10); the connector edge between them runs due east. External edge 1
    touches the cluster at A's own raw osmid, also runs due east (same
    direction as the connector) but is the WORSE-ranked residential;
    external edge 2 touches at B's own raw osmid, runs due north
    (perpendicular - a poor direction match) but is the BETTER-ranked
    primary. The connector must go to edge 1's street_id: it shares the
    connector's own raw endpoint osmid (A) AND its direction is within
    angle_threshold_deg of the connector's own center bearing - criterion
    1, which wins outright regardless of rank. Under the old "just pick
    the best-ranked candidate present" behavior, this would have wrongly
    picked edge 2 instead."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    # Connector: A(0,0) -> B(5,0), due east.
    _add_edge(G, "A", "B", (0, 0), (5, 0), highway="service", osmid=1)
    # External 1 at A's own raw osmid, due east (matches connector's own
    # direction), worse rank (residential).
    _add_edge(G, "A", "ext1", (0, 0), (-100, 0), highway="residential", osmid=2)
    # External 2 at B's own raw osmid, due north (perpendicular), better
    # rank (primary).
    _add_edge(G, "B", "ext2", (5, 0), (5, 100), highway="primary", osmid=3)

    _, edges_gdf, streets_df = street_ids.label_streets(
        G,
        node_cluster_m=10,
        angle_threshold_deg=20,
        parallel_dist_m=0,
        parallel_pass_touching_dist_m=0,
        parallel_pass_non_touching_dist_m=0,
        min_street_length_m=0,
    )

    connector_id = street_id_of(edges_gdf, "A", "B")
    ext1_id = street_id_of(edges_gdf, "A", "ext1")
    ext2_id = street_id_of(edges_gdf, "B", "ext2")
    assert ext1_id != ext2_id
    assert connector_id == ext1_id
    assert connector_id != ext2_id


def test_loop_edge_falls_back_to_best_matching_direction_when_no_osmid_matches():
    """When no external edge at the connector's own raw endpoints has a
    "similar enough" direction (within angle_threshold_deg), the
    connector falls back to whichever candidate street_id present ANYWHERE
    at the node_id has the closest overall direction match - not simply
    the best-ranked one. Three real, distinct nodes A(0,0), B(5,0),
    C(2,5) all cluster together; the connector runs A->B, due east.
    External 1, at A's own raw osmid (a connector endpoint), runs due
    north-ish (85 deg off - a poor match, outside angle_threshold_deg)
    and is the BETTER-ranked primary. External 2, at C's own raw osmid
    (NOT a connector endpoint, so criterion 1 never even considers it),
    runs nearly due east (5 deg off - an excellent match) and is the
    WORSE-ranked residential. The connector must go to external 2's
    street_id: criterion 1 finds no qualifying same-osmid match (external
    1's direction is too far off to count, and external 2 doesn't share
    a raw osmid with the connector at all), so criterion 2's plain best-
    direction-anywhere-at-the-node comparison decides, and external 2's
    5 deg beats external 1's 85 deg despite the worse rank."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    # Connector: A(0,0) -> B(5,0), due east.
    _add_edge(G, "A", "B", (0, 0), (5, 0), highway="service", osmid=1)
    # External 1 at A's own raw osmid, 85 deg off (poor match), better
    # rank (primary).
    import math as _math
    dx1, dy1 = _math.cos(_math.radians(85)) * 100, _math.sin(_math.radians(85)) * 100
    _add_edge(G, "A", "ext1", (0, 0), (dx1, dy1), highway="primary", osmid=2)
    # External 2 at C (not a connector endpoint), 5 deg off (excellent
    # match), worse rank (residential).
    dx2, dy2 = 2 + _math.cos(_math.radians(5)) * 100, 5 + _math.sin(_math.radians(5)) * 100
    _add_edge(G, "C", "ext2", (2, 5), (dx2, dy2), highway="residential", osmid=3)

    _, edges_gdf, streets_df = street_ids.label_streets(
        G,
        node_cluster_m=10,
        angle_threshold_deg=20,
        parallel_dist_m=0,
        parallel_pass_touching_dist_m=0,
        parallel_pass_non_touching_dist_m=0,
        min_street_length_m=0,
    )

    connector_id = street_id_of(edges_gdf, "A", "B")
    ext1_id = street_id_of(edges_gdf, "A", "ext1")
    ext2_id = street_id_of(edges_gdf, "C", "ext2")
    assert ext1_id != ext2_id
    assert connector_id == ext2_id
    assert connector_id != ext1_id


def test_parallel_grouping_joins_edges_of_very_different_length():
    """A short edge running alongside only PART of a much longer one is
    still a genuine parallel pair, even though its own endpoints fall
    well past where the longer edge's own bounded geometry ends -
    _edges_are_truly_parallel measures against the other edge's own
    INFINITE line, not its bounded geometry. Regression test: two real
    edges (a long primary avenue segment and a short, nearby tertiary
    stub) reported as failing to merge despite being visibly parallel
    and close together."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    # Long primary edge: (0, 0) -> (500, 0), 500m.
    _add_edge(G, "P0", "P1", (0, 0), (500, 0), highway="primary", osmid=1)
    # Short tertiary stub running alongside only its middle portion,
    # offset 3m away (well within the 5m non-touching default) and NOT
    # sharing any node with the primary edge.
    _add_edge(G, "T0", "T1", (150, 3), (250, 3), highway="tertiary", osmid=2)

    _, edges_gdf, _ = street_ids.label_streets(G, node_cluster_m=0, min_street_length_m=0)

    assert street_id_of(edges_gdf, "P0", "P1") == street_id_of(edges_gdf, "T0", "T1")


def test_non_drivable_edge_parallel_to_a_drivable_street_is_donated_its_street_id():
    """A non-drivable edge (footway) running alongside a drivable street
    is still the same street for addressing purposes: the non-drivable
    pass donates it the drivable street's own street_id instead of
    minting a fresh one. Regression test: a real footway reported as
    getting its own distinct street_id despite running right alongside
    an already-resolved drivable street."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, "D0", "D1", (0, 0), (200, 0), highway="primary", osmid=1)
    # Footway offset 2m away, touching neither of the drivable edge's own
    # nodes.
    _add_edge(G, "F0", "F1", (0, 2), (200, 2), highway="footway", osmid=2)

    _, edges_gdf, streets_df = street_ids.label_streets(
        G, node_cluster_m=0, min_street_length_m=0
    )

    drivable_id = street_id_of(edges_gdf, "D0", "D1")
    footway_id = street_id_of(edges_gdf, "F0", "F1")
    assert footway_id == drivable_id
    # No separate street_id was minted for the footway.
    assert len(streets_df) == 1


def test_connectivity_invariant_splits_non_parallel_non_touching_fragments():
    """Two short, unrelated fragments that each independently land on the
    same street_id as a longer target (e.g. both absorbed via the
    nearest-by-distance redistribution fallback, which doesn't check
    direction) must NOT be treated as connected to each other just
    because they're both near the target - only genuine parallelism (or
    literal touching) counts as "connected" for the connectivity
    invariant. Regression test: two real, unrelated tunnel/footway edges
    reported as sharing a street_id despite neither touching nor running
    parallel to each other (or to whatever they were merged via)."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    # Long valid target street.
    _add_edge(G, "M0", "M1", (0, 0), (0, 300), highway="residential", osmid=1)
    # Fragment A: short, near the target's middle, running parallel to it
    # (a genuine dual-carriageway-style offset) - should end up merged.
    _add_edge(G, "A0", "A1", (2, 100), (2, 130), highway="residential", osmid=2)
    # Fragment B: short, near fragment A (well within parallel_dist_m of
    # it), but running perpendicular to both the target and fragment A -
    # not a genuine parallel pair with either.
    _add_edge(G, "B0", "B1", (2, 130), (20, 130), highway="residential", osmid=3)

    _, edges_gdf, streets_df = street_ids.label_streets(
        G,
        node_cluster_m=0,
        min_street_length_m=150,
        parallel_dist_m=10,
    )

    target_id = street_id_of(edges_gdf, "M0", "M1")
    a_id = street_id_of(edges_gdf, "A0", "A1")
    b_id = street_id_of(edges_gdf, "B0", "B1")
    assert a_id == target_id
    assert b_id != target_id
    assert b_id != a_id


def test_parallel_grouping_does_not_merge_collinear_edges_that_never_overlap():
    """Two edges sharing a bearing and a small perpendicular offset from
    each other's own INFINITE line are NOT a genuine parallel pair if
    they never actually run alongside each other - only checking
    perpendicular distance to an infinite line, with no bound on how far
    apart the two edges are ALONG that line, would wrongly merge two
    unrelated streets that merely happen to sit on the same bearing.
    Regression test: a real living_street edge reported as sharing a
    street_id with an unrelated residential street it never runs near,
    only shares an approximate bearing with."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    # Edge A: (0, 0) -> (100, 0).
    _add_edge(G, "A0", "A1", (0, 0), (100, 0), highway="residential", osmid=1)
    # Edge B: far away along the same line/bearing (starts 1000m past
    # A's own end), offset only 2m perpendicular - collinear-ish, but
    # the two edges never actually run alongside each other.
    _add_edge(G, "B0", "B1", (1100, 2), (1200, 2), highway="residential", osmid=2)

    _, edges_gdf, _ = street_ids.label_streets(G, node_cluster_m=0, min_street_length_m=0)

    assert street_id_of(edges_gdf, "A0", "A1") != street_id_of(edges_gdf, "B0", "B1")


def test_node_binning_rejects_an_edge_that_curves_away_past_the_shared_node():
    """Two edges sharing a node with a close LOCAL tangent there (within
    ``direction_tolerance_deg``) must NOT be joined if one of them then
    curves away well past that tolerance before reaching its own far
    endpoint - the local tangent (measured only ``_TANGENT_FRACTION``
    into the edge) can look aligned even though the edge, as a whole,
    heads somewhere completely different. Regression test: a real
    residential edge reported as sharing a street_id with another edge
    it only matched at the shared node itself, before visibly curving
    away from it."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node("N", x=0, y=0)
    G.add_node("A1", x=100, y=0)
    G.add_node("B1", x=100, y=80)
    # A: straight east, bearing 0 deg throughout.
    a_geom = LineString([(0, 0), (100, 0)])
    G.add_edge(
        "N", "A1", 0, highway="residential", name=None, geometry=a_geom, length=a_geom.length, osmid=1
    )
    G.add_edge(
        "A1", "N", 0, highway="residential", name=None,
        geometry=LineString([(100, 0), (0, 0)]), length=a_geom.length, osmid=1,
    )
    # B: starts heading almost due east too (local tangent near 0 deg at
    # N), but bends hard after 10m and its own far endpoint ends up at a
    # bearing (from N) of ~39 deg - well past direction_tolerance_deg.
    b_geom = LineString([(0, 0), (10, 0), (100, 80)])
    G.add_edge(
        "N", "B1", 0, highway="residential", name=None, geometry=b_geom, length=b_geom.length, osmid=2
    )
    G.add_edge(
        "B1", "N", 0, highway="residential", name=None,
        geometry=LineString([(100, 80), (10, 0), (0, 0)]), length=b_geom.length, osmid=2,
    )

    _, edges_gdf, _ = street_ids.label_streets(
        G, node_cluster_m=0, node_direction_tolerance_deg=30, min_street_length_m=0
    )

    assert street_id_of(edges_gdf, "N", "A1") != street_id_of(edges_gdf, "N", "B1")


def test_non_drivable_edge_mostly_covered_by_drivable_network_is_donated_its_street_id():
    """A non-drivable edge whose own length runs almost entirely (>=90%
    by default) near the drivable network, even without touching it at
    a shared node and even if it isn't a strict
    ``_edges_are_truly_parallel`` match (a slight weave), is directly
    donated the nearby drivable street_id with the largest total nearby
    length - coverage-based donation, distinct from (and running before)
    the stricter parallel-grouping-based donation."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    # Drivable primary edge: (0, 0) -> (200, 0).
    _add_edge(G, "D0", "D1", (0, 0), (200, 0), highway="primary", osmid=1)
    # Footway weaving slightly (offset 1-4m, never a clean parallel
    # line), covering nearly its whole own length near the drivable edge.
    footway_geom = LineString([(0, 3), (50, 1), (100, 4), (150, 2), (198, 3)])
    G.add_node("F0", x=0, y=3)
    G.add_node("F1", x=198, y=3)
    G.add_edge(
        "F0", "F1", 0, highway="footway", name=None, geometry=footway_geom,
        length=footway_geom.length, osmid=2,
    )
    G.add_edge(
        "F1", "F0", 0, highway="footway", name=None,
        geometry=LineString(list(footway_geom.coords)[::-1]),
        length=footway_geom.length, osmid=2,
    )

    _, edges_gdf, streets_df = street_ids.label_streets(
        G, node_cluster_m=0, min_street_length_m=0, parallel_pass_touching_dist_m=5,
        parallel_pass_non_touching_dist_m=5,
    )

    drivable_id = street_id_of(edges_gdf, "D0", "D1")
    footway_id = street_id_of(edges_gdf, "F0", "F1")
    assert footway_id == drivable_id
    assert len(streets_df) == 1


def test_coverage_donation_weighs_by_actual_overlap_not_candidate_own_length():
    """Coverage-based donation must pick the drivable street_id the
    non-drivable edge actually runs alongside for MOST of its own
    length, not whichever candidate street_id happens to have the
    largest total length of its OWN edges nearby - a long street that
    only brushes past a small corner of the non-drivable edge must not
    outvote a short street the non-drivable edge runs alongside for its
    entire length. Regression test: a real footway running directly
    alongside two short residential edges got donated a distant,
    unrelated, much longer street_id instead, because that street's own
    edges happened to sum to more raw length among the nearby
    candidates."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    # Footway: (0, 0) -> (50, 0).
    _add_edge(G, "F0", "F1", (0, 0), (50, 0), highway="footway", osmid=1)
    # Short street X: runs directly alongside the footway's own entire
    # length, 3m offset.
    _add_edge(G, "X0", "X1", (0, 3), (50, 3), highway="residential", osmid=2)
    # Long street Y: only its own tail end (40-50) is anywhere near the
    # footway - the other 490m of it (50-540) sticks out far away, never
    # actually running alongside the footway at all.
    _add_edge(G, "Y0", "Y1", (40, 3), (540, 3), highway="residential", osmid=3)

    _, edges_gdf, _ = street_ids.label_streets(
        G,
        node_cluster_m=0,
        min_street_length_m=0,
        parallel_pass_touching_dist_m=5,
        parallel_pass_non_touching_dist_m=5,
    )

    footway_id = street_id_of(edges_gdf, "F0", "F1")
    x_id = street_id_of(edges_gdf, "X0", "X1")
    y_id = street_id_of(edges_gdf, "Y0", "Y1")
    assert footway_id == x_id
    assert footway_id != y_id


def test_coverage_donation_splits_a_non_drivable_street_left_disconnected_by_it():
    """The non-drivable pipeline runs in this order: (1) the non-drivable
    pass assigns its own street_ids normally, entirely on its own; (2)
    coverage-based donation then reassigns any edge close enough to the
    drivable network to that drivable street_id instead, even overriding
    the street_id from step 1; (3) the connectivity invariant then splits
    whatever's left of a non-drivable street_id if donating its own
    "bridge" middle away has left its own remaining edges disconnected
    from each other.

    Here a single, continuous footway (one street_id from step 1) has
    only its own MIDDLE segment close enough to a drivable street to be
    donated away in step 2 - leaving the footway's own two end segments
    each still connected to their own local neighbor, but no longer
    connected to EACH OTHER (the middle segment that used to bridge them
    is gone). Step 3 must split those two remaining halves into two
    separate street_ids, not silently leave them sharing one id despite
    no longer being one connected piece."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    # Drivable street only in the middle, x=[40,60], y=0.
    _add_edge(G, "D0", "D1", (40, 0), (60, 0), highway="primary", osmid=1)
    # Footway spanning x=[0,100], y=3, split into 5 touching segments,
    # same bearing throughout - ONE non-drivable street_id after step 1.
    _add_edge(G, "N0", "N1", (0, 3), (20, 3), highway="footway", osmid=2)
    _add_edge(G, "N1", "N2", (20, 3), (40, 3), highway="footway", osmid=3)
    _add_edge(G, "N2", "N3", (40, 3), (60, 3), highway="footway", osmid=4)  # near drivable
    _add_edge(G, "N3", "N4", (60, 3), (80, 3), highway="footway", osmid=5)
    _add_edge(G, "N4", "N5", (80, 3), (100, 3), highway="footway", osmid=6)

    _, edges_gdf, streets_df = street_ids.label_streets(
        G,
        node_cluster_m=0,
        min_street_length_m=0,
        parallel_pass_touching_dist_m=5,
        parallel_pass_non_touching_dist_m=5,
    )

    drivable_id = street_id_of(edges_gdf, "D0", "D1")
    middle_id = street_id_of(edges_gdf, "N2", "N3")
    left_id = street_id_of(edges_gdf, "N0", "N1")
    assert left_id == street_id_of(edges_gdf, "N1", "N2")
    right_id = street_id_of(edges_gdf, "N3", "N4")
    assert right_id == street_id_of(edges_gdf, "N4", "N5")

    # The middle segment was donated to the drivable street.
    assert middle_id == drivable_id
    # The two remaining halves are no longer connected to each other and
    # must not share a street_id, with each other or with the drivable
    # street's own id.
    assert left_id != right_id
    assert left_id != drivable_id
    assert right_id != drivable_id
    assert len(streets_df) == 3


def test_non_drivable_donation_uses_the_looser_touching_distance_bound():
    """Coverage-based donation (the 90% rule) uses
    ``parallel_pass_touching_dist_m`` - the LOOSER bound - not the
    tighter ``parallel_pass_non_touching_dist_m``: a sidewalk running
    alongside a wide, multi-lane road is legitimately offset more than
    the tight "two separate streets happen to be near" bound, the same
    reason the looser touching bound exists at all (dual-carriageway-
    style offsets). Regression test: a real footway running the length
    of a wide primary road was only donated 23% coverage at the tight
    bound (real offset ~12-14m) but 100% at the looser one."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, "D0", "D1", (0, 0), (200, 0), highway="primary", osmid=1)
    # Footway offset 12m - beyond a tight (5m) non-touching bound, but
    # within a looser (15m) touching bound.
    _add_edge(G, "F0", "F1", (0, 12), (200, 12), highway="footway", osmid=2)

    _, edges_gdf, streets_df = street_ids.label_streets(
        G,
        node_cluster_m=0,
        min_street_length_m=0,
        parallel_pass_touching_dist_m=15,
        parallel_pass_non_touching_dist_m=5,
    )

    drivable_id = street_id_of(edges_gdf, "D0", "D1")
    footway_id = street_id_of(edges_gdf, "F0", "F1")
    assert footway_id == drivable_id
    assert len(streets_df) == 1




def test_real_internal_connector_edge_is_never_main_and_keeps_its_own_geometry():
    """SPEC CHANGE (confirmed): a REAL internal (loop/connector) edge -
    one that existed in the original graph with both raw endpoints
    clustering to the SAME node_id (see "Node clustering") - no longer
    participates in main-path computation at all, is never forced main,
    and its own (possibly curved) OSM geometry is never replaced. This
    replaces the old behavior (still exercised by the OLD version of
    this test, before this change) where every internal edge with a
    street_id was unconditionally forced onto the main path and
    straightened. Under the new design, the physical distance a real
    internal edge used to contribute is instead carried by a brand-new
    SYNTHETIC straight connector edge the main path itself builds where
    it actually needs one (see
    test_main_path_gets_a_new_synthetic_connector_at_a_real_intersection)
    - this real internal edge is simply unrelated furniture at the same
    intersection, and stays exactly as OSM mapped it."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node(1, x=0, y=0)
    G.add_node(2, x=1, y=0)  # 1m from node 1: within the default 15m cluster
    G.add_node(3, x=200, y=0)
    G.add_node(4, x=201, y=0)  # 1m from node 3

    fwd = LineString([(0, 0), (200, 0)])
    G.add_edge(1, 3, 0, highway="primary", name=None, geometry=fwd, length=fwd.length, osmid=201)
    bwd = LineString([(201, 0), (1, 0)])
    G.add_edge(4, 2, 0, highway="primary", name=None, geometry=bwd, length=bwd.length, osmid=202)

    # Internal connector (a median-crossing link at the same real
    # intersection as nodes 1/2) - deliberately curved, to prove its own
    # geometry is left untouched.
    connector_geom = LineString([(0, 0), (0.5, 2), (1, 0)])
    G.add_edge(
        1, 2, 0, highway="primary", name=None, geometry=connector_geom,
        length=connector_geom.length, osmid=999,
    )

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 1
    connector_row = edges_gdf.loc[(1, 2, 0)]
    assert pd.notna(connector_row["street_id"])
    assert bool(connector_row["is_main_direction"]) is False
    # Geometry and length are the ORIGINAL OSM ones, untouched.
    assert list(connector_row["geometry"].coords) == list(connector_geom.coords)
    assert connector_row["length"] == pytest.approx(connector_geom.length)


def test_sharp_turn_on_a_main_path_splits_the_street_id():
    """Same OSM name/highway merges two edges into one street_id at the
    identity-assignment stage regardless of geometry (Rule 1) - but a
    real physical right-angle turn on that street_id's own main path is
    still a different physical through-line for NUMBERING purposes, so
    it's split into two street_ids at the turn node (see "Split a
    street_id at a sharp turn on its own main path"). The split-off piece
    reuses the original's own name/rank/highway in streets_df - it's
    still physically the same road, just cut in two for numbering."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (100, 0), highway="residential", name="Calle Recta", osmid=1)
    _add_edge(G, 1, 2, (100, 0), (100, 100), highway="residential", name="Calle Recta", osmid=2)

    _, edges_gdf, streets_df = street_ids.label_streets(G, node_cluster_m=0)

    id_ab = street_id_of(edges_gdf, 0, 1)
    id_bc = street_id_of(edges_gdf, 1, 2)
    assert pd.notna(id_ab) and pd.notna(id_bc)
    assert id_ab != id_bc
    assert len(streets_df) == 2

    row_ab = streets_df.loc[streets_df["street_id"] == id_ab].iloc[0]
    row_bc = streets_df.loc[streets_df["street_id"] == id_bc].iloc[0]
    assert row_ab["street_name"] == row_bc["street_name"] == "Calle Recta"
    assert row_ab["highway"] == row_bc["highway"] == "residential"


def test_non_main_spur_follows_its_nearest_main_edge_across_a_turn_split():
    """When a sharp turn splits a street_id's own main path in two (see
    the turn-splitting test above), every non-main edge of the ORIGINAL
    street_id (a parallel duplicate, spur, or unresolved connector)
    follows whichever piece of the split it actually sits nearest to
    (``nearest_main_eid``), rather than arbitrarily staying with whatever
    piece happened to keep the original street_id.

    Here a same-named PARALLEL DUPLICATE of the second (post-turn) leg
    (offset 3m, well within the default 5m non-touching parallel bound)
    merges into the same street_id as both legs at the identity stage
    (Rule 1's name match plus parallel grouping), but is never itself the
    main path (the A-B-C through-line is longer). It must end up on the
    second leg's own split-off street_id, since that's the specific
    main-path edge it actually runs alongside - not the first leg's."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (100, 0), highway="residential", name="Calle Recta", osmid=1)
    _add_edge(G, 1, 2, (100, 0), (100, 100), highway="residential", name="Calle Recta", osmid=2)
    _add_edge(G, 3, 4, (103, 0), (103, 100), highway="residential", name="Calle Recta", osmid=3)

    _, edges_gdf, streets_df = street_ids.label_streets(G, node_cluster_m=0)

    id_ab = street_id_of(edges_gdf, 0, 1)
    id_bc = street_id_of(edges_gdf, 1, 2)
    id_dup = street_id_of(edges_gdf, 3, 4)
    assert id_ab != id_bc

    dup_row = edges_gdf.loc[(3, 4, 0)]
    assert not dup_row["is_main_direction"]
    assert dup_row["nearest_main_eid"] == (1, 2, 0)
    # The duplicate's own street_id must have followed whichever leg its
    # nearest main-path edge is on - the second (post-turn) leg, since
    # that's the one it actually runs alongside.
    assert id_dup == id_bc


def test_main_path_gets_a_new_synthetic_connector_at_a_real_intersection():
    """A street_id's own main path walking through a real intersection
    mapped as several close-but-distinct raw osmids (see "Node
    clustering") gets a brand-new SYNTHETIC straight connector edge
    bridging the two specific osmids the main path actually touches
    there - it is NOT the pre-existing real internal edge repurposed:
    that real internal edge is left completely alone (its own street_id
    still resolved the normal way, its own OSM geometry untouched, never
    main)."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node(1, x=0, y=0)
    G.add_node(2, x=1, y=0)  # 1m from node 1: within the default 15m cluster
    G.add_node(3, x=-200, y=0)
    G.add_node(4, x=201, y=0)

    west = LineString([(-200, 0), (0, 0)])
    G.add_edge(3, 1, 0, highway="primary", name="Main Ave", geometry=west, length=west.length, osmid=101)
    east = LineString([(1, 0), (201, 0)])
    G.add_edge(2, 4, 0, highway="primary", name="Main Ave", geometry=east, length=east.length, osmid=102)

    # A real (pre-existing) internal connector at the same intersection -
    # deliberately curved and unnamed, so it's easy to tell apart from the
    # brand-new synthetic one.
    real_internal_geom = LineString([(0, 0), (0.5, 3), (1, 0)])
    G.add_edge(
        1, 2, 0, highway="primary", name=None, geometry=real_internal_geom,
        length=real_internal_geom.length, osmid=999,
    )

    _, edges_gdf, streets_df = street_ids.label_streets(G)

    assert len(streets_df) == 1
    # Look up the west/east edges by their own real osmid rather than
    # (u, v) - main-path reorientation (see "Physical edge reorientation")
    # may have flipped either/both of their stored u/v depending on which
    # end of the path the walk happened to start from, which is an
    # internal, arbitrary-but-deterministic implementation detail this
    # test shouldn't depend on.
    west_row = edges_gdf[edges_gdf["osmid"] == 101].iloc[0]
    east_row = edges_gdf[edges_gdf["osmid"] == 102].iloc[0]
    sid = west_row["street_id"]
    assert pd.notna(sid)
    assert east_row["street_id"] == sid

    # The real internal edge is untouched: still resolved to a street_id,
    # never main, own curved geometry kept exactly as OSM mapped it.
    real_row = edges_gdf.loc[(1, 2, 0)]
    assert pd.notna(real_row["street_id"])
    assert bool(real_row["is_main_direction"]) is False
    assert list(real_row["geometry"].coords) == list(real_internal_geom.coords)

    # A brand-new synthetic connector bridges osmid 1 <-> osmid 2 with a
    # straight line, is main, and shares the same street_id - found by its
    # own synthetic `key` (far above any real osmnx key) rather than a
    # specific (u, v) order, for the same reorientation-direction reason
    # as above.
    synthetic_rows = edges_gdf[edges_gdf.index.get_level_values("key") >= 900_000_000]
    assert len(synthetic_rows) == 1
    syn_row = synthetic_rows.iloc[0]
    syn_u, syn_v, _syn_key = synthetic_rows.index[0]
    assert {syn_u, syn_v} == {1, 2}
    coords = list(syn_row["geometry"].coords)
    assert coords in ([(0.0, 0.0), (1.0, 0.0)], [(1.0, 0.0), (0.0, 0.0)])
    assert syn_row["length"] == pytest.approx(1.0)
    assert bool(syn_row["is_main_direction"]) is True
    assert syn_row["street_id"] == sid


def test_physical_reorientation_flips_u_v_and_geometry_to_match_the_walk():
    """When a street_id's own main path needs to walk a one-way edge
    "backwards" relative to however it happened to be stored (its own
    stored `u` is the node the walk actually arrives AT, not departs
    FROM), the edge's own stored `u`/`v` and its geometry's own
    coordinate order are both physically flipped to match the walk
    direction - see "Physical edge reorientation" in
    report/street_identity.md."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node(10, x=0, y=0)
    G.add_node(20, x=100, y=0)
    G.add_node(30, x=200, y=0)

    # Stored backwards: u=20 (B), v=10 (A), even though the main path
    # walks A -> B -> C.
    backwards_geom = LineString([(100, 0), (0, 0)])
    G.add_edge(
        20, 10, 0, highway="residential", name="Test St",
        geometry=backwards_geom, length=backwards_geom.length, osmid=1,
    )
    forward_geom = LineString([(100, 0), (200, 0)])
    G.add_edge(
        20, 30, 0, highway="residential", name="Test St",
        geometry=forward_geom, length=forward_geom.length, osmid=2,
    )

    _, edges_gdf, _ = street_ids.label_streets(G)

    # The physically-backwards edge no longer exists under its OLD
    # (20, 10, 0) index - it's been reoriented to (10, 20, 0), with its
    # geometry's own coordinate order reversed to match.
    assert (20, 10, 0) not in edges_gdf.index
    reoriented_row = edges_gdf.loc[(10, 20, 0)]
    assert list(reoriented_row["geometry"].coords) == [(0.0, 0.0), (100.0, 0.0)]
    assert bool(reoriented_row["is_main_direction"]) is True

    # The already-forward edge is untouched.
    forward_row = edges_gdf.loc[(20, 30, 0)]
    assert list(forward_row["geometry"].coords) == [(100.0, 0.0), (200.0, 0.0)]


def test_main_path_invariant_validator_accepts_valid_and_rejects_broken_paths():
    """Direct unit test of ``_validate_main_path_invariant`` (see "Full
    main-path connectivity validation" in report/street_identity.md): a
    genuinely valid main path (external edges plus a synthetic connector
    sharing one node_id but two different osmids) passes without error;
    an artificially broken one (the same osmid touched by three edges at
    once) raises a clear ``ValueError``, proving the invariant is
    actually enforced rather than always trivially satisfied."""
    eids = np.array([(1, 2, 0), (2, 3, 0), (3, 4, 0)], dtype=object)
    # A moderately complex, VALID path: edge A (osmid 1 -> osmid 2,
    # node_id 100 -> node_id 200), a synthetic connector (osmid 2 ->
    # osmid 3, both node_id 200 - the shared-node_id, distinct-osmid
    # case a synthetic connector is allowed to repeat), then edge B
    # (osmid 3 -> osmid 4, node_id 200 -> node_id 300).
    u_arr = np.array([1, 2, 3])
    v_arr = np.array([2, 3, 4])
    cluster_u = np.array([100, 200, 200])
    cluster_v = np.array([200, 200, 300])
    street_id_arr = np.array([1, 1, 1])
    is_main_arr = np.array([True, True, True])

    street_ids._validate_main_path_invariant(
        eids, u_arr, v_arr, cluster_u, cluster_v, street_id_arr, is_main_arr
    )

    # Now break it: osmid 2 touched by a THIRD edge too (a real bug this
    # invariant must catch, not silently accept).
    bad_eids = np.array([(1, 2, 0), (2, 3, 0), (3, 4, 0), (2, 5, 1)], dtype=object)
    bad_u = np.array([1, 2, 3, 2])
    bad_v = np.array([2, 3, 4, 5])
    bad_cluster_u = np.array([100, 200, 200, 200])
    bad_cluster_v = np.array([200, 200, 300, 400])
    bad_sid = np.array([1, 1, 1, 1])
    bad_is_main = np.array([True, True, True, True])

    with pytest.raises(ValueError):
        street_ids._validate_main_path_invariant(
            bad_eids, bad_u, bad_v, bad_cluster_u, bad_cluster_v, bad_sid, bad_is_main
        )


def test_name_match_overrides_rank_at_a_higher_rank_crossing():
    """The single most important rule (per report/street_identity.md
    "Name match overrides rank"): two edges sharing a (fuzzy-matching)
    name must end up on the same street_id even when a THIRD, unrelated,
    higher-rank (unnamed) edge is also present at the same node_id/
    direction bin - name compatibility overrides rule 2's normal
    "a lower-rank street is cut by a higher-rank crossing" behavior."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node(0, x=0, y=0)
    G.add_node(1, x=-100, y=0)
    G.add_node(2, x=100, y=0)
    G.add_node(3, x=0, y=100)

    # Same name, different (worse) ranks, both heading the same direction
    # through node 0.
    named_a = LineString([(-100, 0), (0, 0)])
    G.add_edge(1, 0, 0, highway="tertiary", name="Elm Street", geometry=named_a, length=named_a.length, osmid=1)
    named_b = LineString([(0, 0), (100, 0)])
    G.add_edge(0, 2, 0, highway="residential", name="Elm Street", geometry=named_b, length=named_b.length, osmid=2)

    # A third, unnamed, much-higher-rank (motorway) edge at the same
    # node, running a different direction - establishes a best_rank far
    # better than either named edge, which would normally cut them apart.
    unrelated = LineString([(0, 0), (0, 100)])
    G.add_edge(0, 3, 0, highway="motorway", name=None, geometry=unrelated, length=unrelated.length, osmid=3)

    _, edges_gdf, streets_df = street_ids.label_streets(G, angle_threshold_deg=20)

    # Looked up by osmid, not (u, v) - these are one-way edges, and main-
    # path reorientation (see "Physical edge reorientation") may have
    # flipped whichever one the walk happens to traverse "backwards",
    # which this test shouldn't depend on.
    id_a = edges_gdf[edges_gdf["osmid"] == 1].iloc[0]["street_id"]
    id_b = edges_gdf[edges_gdf["osmid"] == 2].iloc[0]["street_id"]
    id_c = edges_gdf[edges_gdf["osmid"] == 3].iloc[0]["street_id"]
    assert id_a == id_b
    assert id_c != id_a


def test_disconnected_components_cascade_reassignment():
    """The connectivity invariant's own components pass
    (``_split_disconnected_components``) transitively merges a chain of
    same-street_id components that are each within ``parallel_dist_m`` of
    the next, even if the two ends of the chain are themselves far apart
    - so a fragment reassigned by proximity to one part of a split-off
    group pulls in any other still-same-street_id fragment that's near
    IT specifically, cascading down the chain. Fixture: A and B stay
    close together (merge/stay on the original street_id); C is far from
    A/B but sits right next to D, and D (a "quarter" fragment) is also
    far from A/B - C and D end up merged together onto a NEW street_id,
    distinct from A/B's."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    # A and B: literally touch (share node "A1"/"B0", the same real node),
    # one street_id, kept. This test calls
    # ``_split_disconnected_components`` directly (not through the full
    # ``label_streets`` pipeline), so "touching" here means literal raw
    # node identity - no separate clustering step runs.
    G.add_node("A0", x=0, y=0)
    G.add_node("A1", x=0, y=20)
    G.add_edge("A0", "A1", 0, highway="residential", name=None,
               geometry=LineString([(0, 0), (0, 20)]), length=20.0, osmid=1)
    G.add_node("B1", x=0, y=40)
    G.add_edge("A1", "B1", 0, highway="residential", name=None,
               geometry=LineString([(0, 20), (0, 40)]), length=20.0, osmid=2)

    # C: far from A/B (300m away), but parallel and near D.
    G.add_node("C0", x=300, y=0)
    G.add_node("C1", x=300, y=20)
    G.add_edge("C0", "C1", 0, highway="residential", name=None,
               geometry=LineString([(300, 0), (300, 20)]), length=20.0, osmid=3)

    # D: far from A/B too, but within parallel_dist_m of C (5m away,
    # running the same direction).
    G.add_node("D0", x=305, y=0)
    G.add_node("D1", x=305, y=20)
    G.add_edge("D0", "D1", 0, highway="residential", name=None,
               geometry=LineString([(305, 0), (305, 20)]), length=20.0, osmid=4)

    # All four start life resolved to the SAME street_id by construction
    # of _split_disconnected_components's own test entry point - call it
    # directly rather than threading this through the full label_streets
    # pipeline (which would give A/B/C/D four different ids from the
    # start, defeating the point of this fixture).
    _, edges_gdf = __import__("osmnx").graph_to_gdfs(G)
    edges_reset = edges_gdf.reset_index()
    eids = np.empty(len(edges_reset), dtype=object)
    for _k, (u, v, key) in enumerate(zip(edges_reset["u"], edges_reset["v"], edges_reset["key"])):
        eids[_k] = (u, v, key)
    cluster_u = edges_reset["u"].to_numpy()
    cluster_v = edges_reset["v"].to_numpy()
    geometries = edges_reset["geometry"].to_numpy()
    street_id_arr = np.array([1, 1, 1, 1])

    out, next_id = street_ids._split_disconnected_components(
        eids, cluster_u, cluster_v, geometries, street_id_arr, parallel_dist_m=10.0, next_id=2,
    )

    idx = {tuple(e): i for i, e in enumerate(eids)}
    a_id = out[idx[("A0", "A1", 0)]]
    b_id = out[idx[("A1", "B1", 0)]]
    c_id = out[idx[("C0", "C1", 0)]]
    d_id = out[idx[("D0", "D1", 0)]]

    assert a_id == b_id == 1
    assert c_id == d_id


# --- recompute_main_paths ---------------------------------------------


def _hand_merged_fixture():
    """Two residential arms (north/south) meeting a primary crossing at
    a real intersection mapped as two distinct raw nodes (10, 11) 2m
    apart - well within the default node_cluster_m, so they cluster to
    one node_id, but are genuinely different raw osmids. Rule 2 (a
    lower-rank street is cut by a higher-rank crossing) keeps the north
    and south arms on two separate street_ids after ``label_streets``,
    exactly like ``test_lower_rank_street_is_cut_by_higher_rank_crossing``
    - this is the "before" state a user then hand-merges in the browser.

    Returns ``(nodes_gdf, edges_gdf, north_id, south_id)`` - the label_streets
    output, plus the two (still separate) arm ids the caller merges.
    """
    G = make_graph(
        [
            (1, 10, (-100, 0), (0, 0), "primary", None),
            (11, 2, (2, 0), (100, 0), "primary", None),
            (10, 3, (0, 0), (0, 100), "residential", None),
            (11, 4, (2, 0), (0, -100), "residential", None),
        ]
    )
    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(
        G, angle_threshold_deg=20, min_street_length_m=0, parallel_dist_m=0
    )
    north_id = street_id_of(edges_gdf, 10, 3)
    south_id = street_id_of(edges_gdf, 11, 4)
    assert north_id != south_id
    return nodes_gdf, edges_gdf, north_id, south_id


def test_recompute_main_paths_bridges_a_hand_merge_across_an_intersection():
    """Hand-merging two previously-separate street_ids that meet at a
    real multi-osmid intersection (node_id shared by raw nodes 10/11)
    must, after ``recompute_main_paths``: (a) not raise (the main-path
    invariant holds), (b) synthesize a brand-new connector edge bridging
    the two specific osmids (10, 11) the merged path needs, (c) end up
    with a connected, correctly-ordered ``is_main_direction=True`` main
    path for the merged street_id."""
    nodes_gdf, edges_gdf, north_id, south_id = _hand_merged_fixture()

    edges_gdf = edges_gdf.copy()
    edges_gdf.loc[edges_gdf["street_id"] == south_id, "street_id"] = north_id
    edges_gdf.loc[edges_gdf["street_id"] == north_id, "street_name"] = "Merged St"

    edges2, streets2 = street_ids.recompute_main_paths(nodes_gdf, edges_gdf)

    merged_row = streets2.loc[streets2["street_id"] == north_id]
    assert len(merged_row) == 1
    assert merged_row.iloc[0]["street_name"] == "Merged St"

    # The primary crossing (still its own separate street_id) ALSO passes
    # through the same clustered intersection and gets its own synthetic
    # connector - only the one belonging to the newly merged street_id
    # is this test's concern.
    synthetic = edges2[edges2.index.get_level_values("key") >= street_ids._SYNTHETIC_CONNECTOR_KEY_OFFSET]
    merged_synthetic = synthetic[synthetic["street_id"] == north_id]
    assert len(merged_synthetic) == 1
    syn_u, syn_v, _syn_key = merged_synthetic.index[0]
    assert {syn_u, syn_v} == {10, 11}
    assert bool(merged_synthetic.iloc[0]["is_main_direction"]) is True

    merged_edges = edges2[edges2["street_id"] == north_id]
    assert merged_edges["is_main_direction"].all()
    # North arm + south arm, each as two directed rows (the underlying
    # MultiDiGraph carries both directions per undirected edge), plus one
    # new synthetic connector row.
    assert len(merged_edges) == 5


def test_recompute_main_paths_reorients_a_backwards_edge():
    """A hand-edit that requires walking an edge backwards relative to
    its stored u/v gets that edge's own u/v and geometry coordinate
    order physically flipped, mirroring
    ``test_physical_reorientation_flips_u_v_and_geometry_to_match_the_walk``."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node(10, x=0, y=0)
    G.add_node(20, x=100, y=0)
    G.add_node(30, x=200, y=0)

    backwards_geom = LineString([(100, 0), (0, 0)])
    G.add_edge(
        20, 10, 0, highway="residential", name="Test St",
        geometry=backwards_geom, length=backwards_geom.length, osmid=1,
    )
    forward_geom = LineString([(100, 0), (200, 0)])
    G.add_edge(
        20, 30, 0, highway="residential", name="Test St",
        geometry=forward_geom, length=forward_geom.length, osmid=2,
    )

    nodes_gdf, edges_gdf, _ = street_ids.label_streets(G)

    edges2, _streets2 = street_ids.recompute_main_paths(nodes_gdf, edges_gdf)

    assert (20, 10, 0) not in edges2.index
    reoriented_row = edges2.loc[(10, 20, 0)]
    assert list(reoriented_row["geometry"].coords) == [(0.0, 0.0), (100.0, 0.0)]
    assert bool(reoriented_row["is_main_direction"]) is True

    forward_row = edges2.loc[(20, 30, 0)]
    assert list(forward_row["geometry"].coords) == [(100.0, 0.0), (200.0, 0.0)]


def test_recompute_main_paths_splits_on_a_sharp_turn_without_id_collision():
    """A hand-edit that merges two edges (sharing an OSM name, so
    ``label_streets`` itself would already have merged them at the
    identity stage) into one street_id with a turn sharper than
    ``main_path_angle_threshold_deg`` gets split into two ids by
    ``recompute_main_paths``, with the new id starting strictly above
    the max EXISTING numeric street_id - never colliding with one."""
    # BC deliberately turns sharper than a right angle relative to AB's
    # own direction of travel (AB heads due east; BC's own initial
    # tangent bears ~96 deg from due east, past the "keeps going
    # straight-ish" side into "sharper than 90" - see
    # ``_raw_turn_deviation_deg``) - a plain 90-degree corner does NOT
    # split (the default threshold is "sharper than a right angle", not
    # "at least"), so a real corner sharper than that is used here.
    G = nx.MultiDiGraph(crs="EPSG:3857")
    _add_edge(G, 0, 1, (0, 0), (100, 0), highway="residential", name="Street A", osmid=1)
    _add_edge(G, 1, 2, (100, 0), (90, 90), highway="residential", name="Street B", osmid=2)
    # An unrelated second street_id with a deliberately large numeric id,
    # so the fixture actually exercises "max existing numeric id + 1"
    # rather than happening to work via a small coincidental default.
    _add_edge(G, 100, 101, (500, 0), (600, 0), highway="residential", name="Other St", osmid=3)

    nodes_gdf, edges_gdf, _streets_df = street_ids.label_streets(G, node_cluster_m=0)

    # AB and BC have different names/highways so label_streets itself
    # keeps them on two separate street_ids - the merge below simulates
    # a user hand-picking both into one street_id via the edge editor,
    # something label_streets's own identity-assignment would never do
    # on its own for this geometry.
    id_a = street_id_of(edges_gdf, 0, 1)
    id_b = street_id_of(edges_gdf, 1, 2)
    assert id_a != id_b
    other_id = street_id_of(edges_gdf, 100, 101)
    big_id = 5000
    edges_gdf = edges_gdf.copy()
    edges_gdf.loc[edges_gdf["street_id"] == other_id, "street_id"] = big_id
    edges_gdf.loc[edges_gdf["street_id"] == id_b, "street_id"] = id_a
    edges_gdf.loc[edges_gdf["street_id"] == id_a, "street_name"] = "Merged St"
    max_existing = max(id_a, big_id)

    edges2, streets2 = street_ids.recompute_main_paths(nodes_gdf, edges_gdf)

    id_ab = street_id_of(edges2, 0, 1)
    id_bc = street_id_of(edges2, 1, 2)
    assert id_ab != id_bc
    new_id = id_bc if id_bc != id_a else id_ab
    assert new_id > max_existing


def test_recompute_main_paths_is_idempotent():
    """Re-running ``recompute_main_paths`` on its own already-correct
    output (main path already final, no membership changes) is a stable
    fixed point: no new splits, no reorientation, same connectors."""
    nodes_gdf, edges_gdf, north_id, south_id = _hand_merged_fixture()
    edges_gdf = edges_gdf.copy()
    edges_gdf.loc[edges_gdf["street_id"] == south_id, "street_id"] = north_id
    edges_gdf.loc[edges_gdf["street_id"] == north_id, "street_name"] = "Merged St"

    edges2, streets2 = street_ids.recompute_main_paths(nodes_gdf, edges_gdf)
    edges3, streets3 = street_ids.recompute_main_paths(nodes_gdf, edges2)

    pd.testing.assert_frame_equal(
        edges2.sort_index(), edges3.sort_index(), check_like=True
    )
    pd.testing.assert_frame_equal(
        streets2.sort_values("street_id").reset_index(drop=True),
        streets3.sort_values("street_id").reset_index(drop=True),
        check_like=True,
    )
