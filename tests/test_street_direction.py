"""Tests for street_direction.assign_street_directionality."""

import math

import networkx as nx
import osmnx as ox
from shapely.geometry import LineString

from osmaddresssystem import street_direction as sd
from osmaddresssystem import street_ids


def make_graph(edges, crs="EPSG:3857"):
    """edges: list of (u, v, (ux, uy), (vx, vy))."""
    G = nx.MultiDiGraph(crs=crs)
    for u, v, uxy, vxy in edges:
        G.add_node(u, x=uxy[0], y=uxy[1])
        G.add_node(v, x=vxy[0], y=vxy[1])
        fwd = LineString([uxy, vxy])
        bwd = LineString([vxy, uxy])
        G.add_edge(
            u, v, 0, highway="residential", name=None, geometry=fwd, length=fwd.length, osmid=1
        )
        G.add_edge(
            v, u, 0, highway="residential", name=None, geometry=bwd, length=bwd.length, osmid=1
        )
    return G


def label_and_direct(G, origin, transversal_road_pct=10.0, **kwargs):
    """Test helper: label and direct."""
    # node_cluster_m=0 disables street_ids' node-clustering feature: these
    # are small hand-built graphs testing directionality, not clustering,
    # and some of their coordinates are deliberately within the default
    # 15m cluster radius of each other (e.g. (0, 10) and (10, 0)).
    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(G, node_cluster_m=0)
    edges_gdf, streets_df = sd.assign_street_directionality(
        nodes_gdf,
        edges_gdf,
        streets_df,
        origin=origin,
        transversal_road_pct=transversal_road_pct,
        **kwargs,
    )
    return edges_gdf.reset_index(), streets_df


def test_radial_street_starts_at_the_end_nearest_origin():
    """A street running straight out from the origin: distance
    difference between its endpoints is ~100% of its length, so it's
    radial, and cumulative length must start at 0 at the near end."""
    G = make_graph(
        [
            (0, 1, (10, 0), (30, 0)),
            (1, 2, (30, 0), (60, 0)),
        ]
    )
    edges, streets = label_and_direct(G, origin=(0, 0))

    assert streets.loc[0, "road_type"] == "radial"
    assert streets.loc[0, "length_m"] == 50.0

    near_edge = edges.loc[(edges["u"] == 0) & (edges["v"] == 1)].iloc[0]
    assert near_edge["street_start_length_m"] == 0.0
    assert near_edge["street_end_length_m"] == 20.0

    # Both directed copies of the same physical segment carry the same
    # street_start_length_m/street_end_length_m bounds.
    reverse_near_edge = edges.loc[(edges["u"] == 1) & (edges["v"] == 0)].iloc[0]
    assert reverse_near_edge["street_start_length_m"] == 0.0
    assert reverse_near_edge["street_end_length_m"] == 20.0

    far_edge = edges.loc[(edges["u"] == 1) & (edges["v"] == 2)].iloc[0]
    assert far_edge["street_start_length_m"] == 20.0
    assert far_edge["street_end_length_m"] == 50.0


def test_transversal_street_numbered_clockwise():
    """A street connecting a point due north of the origin to a point
    due east: both endpoints are equidistant from the origin (0% ratio,
    so definitely transversal), and the clockwise sweep from north to
    east means numbering must start at the north point."""
    G = make_graph(
        [
            (0, 1, (0, 10), (10, 0)),  # node 0 = north, node 1 = east
        ]
    )
    edges, streets = label_and_direct(G, origin=(0, 0))

    assert streets.loc[0, "road_type"] == "transversal"

    north_to_east = edges.loc[(edges["u"] == 0) & (edges["v"] == 1)].iloc[0]
    assert north_to_east["street_start_length_m"] == 0.0

    # The reverse directed copy carries the same fixed bound (it's the
    # north node - node 0 - that's at the numbering start, not either
    # edge's own directed "u").
    east_to_north = edges.loc[(edges["u"] == 1) & (edges["v"] == 0)].iloc[0]
    assert east_to_north["street_start_length_m"] == 0.0


def test_transversal_threshold_is_configurable():
    """Two endpoints with a distance difference that's ~14% of the
    street's length: transversal at the default 10% threshold's
    complement (i.e. radial), transversal once the threshold is raised
    past ~14%."""
    # (0,0) to (100,0): dist to origin 0 vs 100, length 100 -> ratio 100%,
    # clearly radial regardless of threshold; use a shallow diagonal
    # instead so the ratio lands in a useful test range.
    G = make_graph(
        [
            (0, 1, (100, 0), (100, 20)),
        ]
    )
    # dist_a = 100, dist_b = sqrt(100**2+20**2) ~ 101.98, length ~ 20
    # ratio = |100 - 101.98| / 20 * 100 ~= 9.9%
    edges_strict, streets_strict = label_and_direct(G, origin=(0, 0), transversal_road_pct=5.0)
    assert streets_strict.loc[0, "road_type"] == "radial"

    edges_loose, streets_loose = label_and_direct(G, origin=(0, 0), transversal_road_pct=15.0)
    assert streets_loose.loc[0, "road_type"] == "transversal"


def test_closed_loop_does_not_crash_and_stays_internally_consistent():
    """A many-sided polygon approximating a circle around the origin
    (each turn well under both the merge threshold and the main-path
    turn-split threshold, so it collapses to one street_id forming a
    genuine closed loop - see report/street_identity.md "Street
    directionality" for the documented seam-edge approximation this
    exercises)."""
    n, radius = 40, 200.0
    G = nx.MultiDiGraph(crs="EPSG:3857")
    for i in range(n):
        angle = 2 * math.pi * i / n
        G.add_node(i, x=radius * math.cos(angle), y=radius * math.sin(angle))

    def add_edge(u, v):
        """Test helper: add edge."""
        ux, uy = G.nodes[u]["x"], G.nodes[u]["y"]
        vx, vy = G.nodes[v]["x"], G.nodes[v]["y"]
        fwd = LineString([(ux, uy), (vx, vy)])
        bwd = LineString([(vx, vy), (ux, uy)])
        G.add_edge(
            u, v, 0, highway="residential", name=None, geometry=fwd, length=fwd.length, osmid=1
        )
        G.add_edge(
            v, u, 0, highway="residential", name=None, geometry=bwd, length=bwd.length, osmid=1
        )

    for i in range(n):
        add_edge(i, (i + 1) % n)

    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(G)
    assert len(streets_df) == 1  # every 9 deg turn is under the 10 deg default angle threshold

    edges_gdf, streets_df = sd.assign_street_directionality(
        nodes_gdf, edges_gdf, streets_df, origin=(0, 0)
    )
    edges = edges_gdf.reset_index()

    assert streets_df.loc[0, "road_type"] == "transversal"

    # Rule 4: only the ring's is_main_direction=True edges are walked/
    # numbered - street_ids.label_streets' Rule 3 pass flags the ring's
    # weighted-diameter path (the longest shortest-path between any two
    # of its own nodes), which on a closed ring necessarily excludes
    # exactly one "seam" edge, so that edge gets null position fields
    # instead of a real position, while every other ring edge gets a
    # real one.
    main_edges = edges.loc[edges["is_main_direction"]]
    non_main_edges = edges.loc[~edges["is_main_direction"]]
    assert len(non_main_edges) > 0
    assert non_main_edges["street_start_length_m"].isna().all()
    assert non_main_edges["street_end_length_m"].isna().all()

    # Every main edge: start < end (or ==, for the zero-length degenerate
    # case, which shouldn't occur here), and end - start == edge length.
    for _, row in main_edges.iterrows():
        assert row["street_start_length_m"] <= row["street_end_length_m"]
        assert math.isclose(
            row["street_end_length_m"] - row["street_start_length_m"],
            row["length"],
            rel_tol=1e-9,
        )

    # Exactly one node (the chosen start) is at cumulative length 0. Both
    # segments touching it report street_start_length_m == 0 (it's a
    # fixed per-segment bound, the same for both directed copies), so we
    # can't read the near node off a 'u'/'v' column directly. Instead,
    # independently re-derive each node's distance from an arbitrary
    # reference node by walking the same is_main_direction-only graph
    # (a simple path here, since exactly one seam edge is excluded), the
    # same way the production code itself walks it, and take - for each
    # zero-length-start row - whichever of its two endpoints is nearer
    # that reference. Since these rows all sit right at the true start,
    # this consistently singles out the same physical node regardless of
    # which reference node or orientation was used.
    main_graph = nx.Graph()
    for _, row in main_edges.iterrows():
        main_graph.add_edge(row["u"], row["v"], length=row["length"])
    ref_node = next(iter(main_graph.nodes))
    dist_from_ref = nx.single_source_dijkstra_path_length(main_graph, ref_node, weight="length")

    zero_rows = main_edges.loc[main_edges["street_start_length_m"] == 0.0]
    near_nodes = {
        row["u"] if dist_from_ref[row["u"]] <= dist_from_ref[row["v"]] else row["v"]
        for _, row in zero_rows.iterrows()
    }
    assert len(near_nodes) == 1


def test_grid_mode_north_south_street_is_primary_and_starts_at_south_end():
    """A due-north/south street with default direction_deg=0 (north)
    is 'primary', and must number from its southern (smaller-y) end
    upward, since cumulative length increases heading north."""
    G = make_graph(
        [
            (0, 1, (0, 10), (0, 40)),  # node 0 = south, node 1 = north
        ]
    )
    edges, streets = label_and_direct(G, origin=(0, 0), numbering_mode="grid")

    assert streets.loc[0, "road_type"] == "primary"

    south_to_north = edges.loc[(edges["u"] == 0) & (edges["v"] == 1)].iloc[0]
    assert south_to_north["street_start_length_m"] == 0.0
    assert south_to_north["street_end_length_m"] == 30.0

    # The reverse directed copy carries the same fixed bounds.
    north_to_south = edges.loc[(edges["u"] == 1) & (edges["v"] == 0)].iloc[0]
    assert north_to_south["street_start_length_m"] == 0.0
    assert north_to_south["street_end_length_m"] == 30.0


def test_grid_mode_east_west_street_is_cross_and_starts_at_west_end():
    """A due-east/west street is 'cross' (perpendicular to north), and
    must number from its western (smaller-x) end, since the cross axis
    (east, 90 deg clockwise from north) increases heading east."""
    G = make_graph(
        [
            (0, 1, (10, 0), (40, 0)),  # node 0 = west, node 1 = east
        ]
    )
    edges, streets = label_and_direct(G, origin=(0, 0), numbering_mode="grid")

    assert streets.loc[0, "road_type"] == "cross"

    west_to_east = edges.loc[(edges["u"] == 0) & (edges["v"] == 1)].iloc[0]
    assert west_to_east["street_start_length_m"] == 0.0
    assert west_to_east["street_end_length_m"] == 30.0


def test_grid_mode_direction_deg_rotates_the_axes():
    """With direction_deg=90 (east as 'primary'), a due-east/west street
    becomes primary instead of cross, and a north/south street becomes
    cross instead of primary - direction_deg genuinely rotates which
    axis is which, it isn't just a label swap."""
    ew_graph = make_graph([(0, 1, (10, 0), (40, 0))])
    _, ew_streets = label_and_direct(
        ew_graph, origin=(0, 0), numbering_mode="grid", direction_deg=90.0
    )
    assert ew_streets.loc[0, "road_type"] == "primary"

    ns_graph = make_graph([(0, 1, (0, 10), (0, 40))])
    _, ns_streets = label_and_direct(
        ns_graph, origin=(0, 0), numbering_mode="grid", direction_deg=90.0
    )
    assert ns_streets.loc[0, "road_type"] == "cross"


def test_grid_mode_picks_nearest_of_the_two_perpendicular_axes():
    """A street running roughly NNE (20 deg off north) should still be
    classified 'primary' against a north direction_deg, since 20 deg is
    closer to the primary axis (0/180) than to the cross axis (90/270)."""
    G = make_graph(
        [
            (0, 1, (0, 0), (math.sin(math.radians(20)) * 30, math.cos(math.radians(20)) * 30)),
        ]
    )
    _, streets = label_and_direct(G, origin=(0, 0), numbering_mode="grid")
    assert streets.loc[0, "road_type"] == "primary"


def test_rule4_non_main_edge_gets_null_position_fields():
    """Rule 4: numbering only walks a street_id's is_main_direction=True
    edges. A dual-carriageway pair (two separately-mapped, opposite-
    direction one-way carriageways auto-merged into one street_id by
    street_ids.label_streets, node_cluster_m enabled) gives one edge
    is_main_direction=True and the other False. After
    assign_street_directionality, the main edge must get real
    street_start_length_m/street_end_length_m, and the
    non-main edge must get null (NaN/None) for both, while both
    keep their street_id/street_name."""
    G = nx.MultiDiGraph(crs="EPSG:3857")
    G.add_node(1, x=0, y=0)
    G.add_node(2, x=1, y=0)  # within the default 15m node cluster of 1
    G.add_node(3, x=200, y=0)
    G.add_node(4, x=201, y=0)  # within the default 15m node cluster of 3

    fwd = LineString([(0, 0), (200, 0)])
    G.add_edge(1, 3, 0, highway="primary", name=None, geometry=fwd, length=fwd.length, osmid=201)
    bwd = LineString([(201, 0), (1, 0)])
    G.add_edge(4, 2, 0, highway="primary", name=None, geometry=bwd, length=bwd.length, osmid=202)

    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(G)
    assert len(streets_df) == 1

    edges_gdf, streets_df = sd.assign_street_directionality(
        nodes_gdf, edges_gdf, streets_df, origin=(0, 0)
    )
    edges = edges_gdf.reset_index()

    main_rows = edges.loc[edges["is_main_direction"]]
    non_main_rows = edges.loc[~edges["is_main_direction"]]
    assert len(main_rows) > 0
    assert len(non_main_rows) > 0

    # Both keep their street_id/street_name.
    assert main_rows["street_id"].notna().all()
    assert non_main_rows["street_id"].notna().all()
    assert (main_rows["street_id"] == non_main_rows["street_id"].iloc[0]).all()

    # Only the main edges get real position fields.
    assert main_rows["street_start_length_m"].notna().all()
    assert main_rows["street_end_length_m"].notna().all()
    assert non_main_rows["street_start_length_m"].isna().all()
    assert non_main_rows["street_end_length_m"].isna().all()
