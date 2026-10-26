"""Tests for numbering_mode="path": network-distance routing, street splitting and numbering."""

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

from osmaddresssystem import numbering, path_numbering, street_direction


def _graph(coords: dict, edges: list):
    """Build nodes and edges GeoDataFrames from node coordinates and edge specs.

    Args:
        coords: Node id -> (x, y) in metres.
        edges: ``(u, v, highway, drivable, street_id)`` tuples (one directed row each, plus its reverse).

    Returns:
        ``(nodes_gdf, edges_gdf)`` indexed like the pipeline's.
    """
    nodes = pd.DataFrame({"x": [c[0] for c in coords.values()], "y": [c[1] for c in coords.values()]},
                         index=list(coords))
    rows = []
    for u, v, hw, drv, sid in edges:
        for a, b in ((u, v), (v, u)):
            g = LineString([coords[a], coords[b]])
            rows.append({"u": a, "v": b, "key": 0, "highway": hw, "is_drivable": drv, "street_id": sid,
                         "is_main_direction": True, "length": g.length, "geometry": g})
    return nodes, gpd.GeoDataFrame(rows, crs=32632).set_index(["u", "v", "key"])


def test_high_rank_roads_are_not_short_cut_through_minor_roads():
    """A residential short cut never shortens the distance of a node on the primary road."""
    coords = {"R": (0, 0), "A": (100, 0), "B": (200, 0), "X": (100, -60)}
    nodes, edges = _graph(coords, [("R", "A", "primary", True, 1), ("A", "B", "primary", True, 1),
                                   ("R", "X", "residential", True, 2), ("X", "B", "residential", True, 3)])
    d, ref = path_numbering.node_distances(nodes, edges, (0, 0))
    assert ref == "R" and d["B"] == 200.0          # high-rank distance, whatever the minor roads offer
    assert d["X"] == edges.loc[("R", "X", 0), "length"]


def test_minor_roads_hang_off_the_high_rank_network():
    """A node only on a minor road gets the distance of its junction plus the road length."""
    coords = {"R": (0, 0), "A": (100, 0), "B": (200, 0), "X": (100, -30)}
    nodes, edges = _graph(coords, [("R", "A", "primary", True, 1), ("A", "B", "primary", True, 1),
                                   ("A", "X", "residential", True, 2), ("R", "X", "residential", True, 3)])
    d, _ = path_numbering.node_distances(nodes, edges, (0, 0))
    # X is reached from R through the minor road (about 104 m) or from A (130 m): the minor road
    # network may start from any high-rank node, never altering the primary nodes.
    assert d["A"] == 100.0 and d["B"] == 200.0 and abs(d["X"] - np.hypot(100, 30)) < 1e-6


def test_non_drivable_edges_never_route_between_drivable_nodes():
    """A footway short cut is ignored for drivable nodes; footway-only nodes still get a distance."""
    coords = {"R": (0, 0), "A": (100, 0), "B": (200, 0), "Y": (0, 30)}
    nodes, edges = _graph(coords, [("R", "A", "primary", True, 1), ("A", "B", "primary", True, 1),
                                   ("R", "B", "footway", False, 2), ("R", "Y", "footway", False, 3)])
    d, _ = path_numbering.node_distances(nodes, edges, (0, 0))
    assert d["B"] == 200.0 and d["Y"] == 30.0


def test_direct_less_important_road_beats_a_detour_with_a_bad_transition():
    """From a primary reference, stepping primary -> secondary is a bad transition and is avoided."""
    coords = {"R": (0, 0), "A": (100, 0), "D": (100, 100)}
    nodes, edges = _graph(coords, [("R", "A", "primary", True, 1), ("A", "D", "secondary", True, 2),
                                   ("R", "D", "secondary", True, 3)])
    d, _ = path_numbering.node_distances(nodes, edges, (0, 0))
    assert abs(d["D"] - np.hypot(100, 100)) < 1e-6      # direct, 141 m, not 100 + 100 via A


def test_cut_indices_split_only_at_interior_minima_not_at_peaks():
    """Interior local minima (also the nearest node) are cut points; a peak (numbers rise, then fall) is not."""
    cut = path_numbering._cut_indices
    assert cut([10, 5, 12, 3, 11]) == [1, 3]           # minima at 1 and 3; the peak at 2 is left alone
    assert cut([5, 3, 3, 6]) == [1]                    # a flat minimum is cut at its first node
    assert cut([1, 5, 5, 2]) == [] and cut([1, 9, 2]) == []     # peaks: no cut
    assert cut([1, 2, 3, 4]) == [] and cut([4, 3, 2, 1]) == [] and cut([1, 2, 2, 3]) == []
    assert cut([10, 5, 12, 3, 11], at_peaks=True) == [1, 2, 3]  # the earlier behaviour is still available


def _street_chain(dist_values):
    """A straight street of 100 m edges with the given node distances to the reference.

    Args:
        dist_values: Distance to the reference of each node, in order.

    Returns:
        ``(nodes_gdf, edges_gdf, streets_df, distance)``.
    """
    names = [f"n{i}" for i in range(len(dist_values))]
    coords = {n: (100.0 * i, 0.0) for i, n in enumerate(names)}
    nodes, edges = _graph(coords, [(a, b, "residential", True, 1) for a, b in zip(names, names[1:])])
    edges["nearest_main_eid"] = ""
    streets = pd.DataFrame({"street_id": [1], "street_name": ["xyz"], "street_rank": [4]})
    return nodes, edges, streets, pd.Series(dist_values, index=names, dtype=float)


def test_street_is_cut_at_interior_minima_and_ids_are_mapped():
    """A V-shaped street gets new ids, the first part keeps its id and the mapping lists all of them."""
    nodes, edges, streets, d = _street_chain([10, 5, 12, 3, 11])
    e2, s2, mapping = path_numbering.split_streets(edges, streets, d)
    ids = sorted(e2["street_id"].unique())
    assert len(ids) == 3 and ids[0] == 1
    assert set(e2["street_id_original"]) == {1} and set(s2["street_id_original"]) == {1}
    assert set(mapping["street_id"]) == set(ids) and (mapping["street_id_original"] == 1).all()
    assert (s2.set_index("street_id").loc[ids, "street_name_base"] == "xyz").all()
    assert sorted(s2.set_index("street_id").loc[ids, "street_name"]) == ["xyz", "xyz (B)", "xyz (C)"]
    # the cut nodes n1 and n3 are shared by two parts: each part ends at its own cut node
    part_of = {tuple(sorted(i[:2])): s for i, s in e2["street_id"].items()}
    assert part_of[("n1", "n2")] == part_of[("n2", "n3")]                   # the part between the two minima spans the peak
    assert len({part_of[("n0", "n1")], part_of[("n1", "n2")], part_of[("n3", "n4")]}) == 3


def test_monotonic_street_is_not_cut():
    """A street whose distance only grows keeps its id."""
    nodes, edges, streets, d = _street_chain([10, 20, 30, 40])
    e2, s2, mapping = path_numbering.split_streets(edges, streets, d)
    assert (e2["street_id"] == 1).all() and len(mapping) == 1


def test_numbering_starts_at_the_node_nearest_the_reference_and_house_numbers_start_at_1_and_2():
    """Positions are distances along the street from its start node (the end nearest the reference); the street starts at 0 (houses 1 / 2)."""
    nodes, edges, streets, d = _street_chain([300, 340, 400, 520])
    e2, s2 = street_direction.assign_street_directionality(
        nodes, edges, streets, origin=(0, 0), numbering_mode="path", node_distances=d)
    flat = e2.reset_index()
    first = flat[flat["street_start_length_m"] == 0].iloc[0]
    pos = {first["u"]: first["street_pos_u_m"], first["v"]: first["street_pos_v_m"]}
    assert min(pos.values()) == 0.0 and pos["n0"] == 0.0
    assert e2["street_start_length_m"].min() == 0.0
    assert abs(e2["street_end_length_m"].max() - 300.0) < 1e-9       # the 300 m of street, not the difference of the distances to the reference
    e3, _ = numbering.assign_house_numbers(e2, s2, number_divisor_m=2.0)
    top = e3[e3["street_start_length_m"] == 0].iloc[0]
    assert top["house_number_start_left"] == 1 and top["house_number_start_right"] == 2


def test_profile_maps_line_position_to_numbering_position():
    """The street profile is the identity now: a point's numbering position is its distance along the street from the start."""
    nodes, edges, streets, d = _street_chain([300, 340, 400, 520])
    e2, s2 = street_direction.assign_street_directionality(
        nodes, edges, streets, origin=(0, 0), numbering_mode="path", node_distances=d)
    prof = s2.loc[0, "path_profile"]
    assert abs(numbering.profile_position(prof, 0.0) - 0.0) < 1e-9
    assert abs(numbering.profile_position(prof, 150.0) - 150.0) < 1e-9
    assert abs(numbering.profile_position(prof, 300.0) - 300.0) < 1e-9


def test_street_with_a_peak_is_not_cut_and_is_numbered_from_its_nearer_end():
    """A street whose middle is farther than both ends keeps one id; it starts at the end with the smaller distance and the numbers rise along it."""
    nodes, edges, streets, d = _street_chain([100, 150, 200, 160, 120])
    e2, s2, mapping = path_numbering.split_streets(edges, streets, d)
    assert sorted(e2["street_id"].unique()) == [1]                      # no cut at the maximum
    e3, _ = street_direction.assign_street_directionality(nodes, e2, s2, origin=(0, 0), numbering_mode="path", node_distances=d)
    flat = e3.reset_index().drop_duplicates(["street_start_length_m"]).sort_values("street_start_length_m")
    assert flat["street_start_length_m"].min() == 0.0 and abs(e3["street_end_length_m"].max() - 400.0) < 1e-9
    first = e3.reset_index()
    first = first[first["street_start_length_m"] == 0].iloc[0]
    assert "n0" in (first["u"], first["v"])                              # n0 (100 m) is nearer than n4 (120 m): numbering starts there
    assert (flat["street_start_length_m"].to_numpy()[1:] >= flat["street_end_length_m"].to_numpy()[:-1] - 1e-9).all()      # and only rises


def test_uniform_roads_give_plain_shortest_path_distances_on_random_graphs():
    """With every road the same rank and drivable, node distances equal networkx's shortest paths."""
    import networkx as nx

    rng = np.random.default_rng(7)
    for trial in range(12):
        n = int(rng.integers(6, 25))
        G = nx.gnp_random_graph(n, 0.25, seed=int(rng.integers(1e6)))
        G = G.subgraph(max(nx.connected_components(G), key=len)).copy()
        names = {i: f"v{i}" for i in G}
        coords = {names[i]: (float(rng.uniform(0, 500)), float(rng.uniform(0, 500))) for i in G}
        edges = [(names[a], names[b], "residential", True, 1) for a, b in G.edges]
        nodes, eg = _graph(coords, edges)
        ref_pt = coords[names[min(G)]]
        d, ref = path_numbering.node_distances(nodes, eg, ref_pt)
        H = nx.Graph()
        for (u, v, _k), r in eg.iterrows():
            H.add_edge(u, v, weight=r["length"])
        expected = nx.single_source_dijkstra_path_length(H, ref)
        for node, value in expected.items():
            assert abs(d[node] - value) < 1e-6, (trial, node)


def test_random_chains_are_cut_at_minima_so_every_part_has_its_nearest_node_at_an_end():
    """Whatever the distance profile, no part has an interior minimum: its nearest node is one of its ends."""
    rng = np.random.default_rng(11)
    for trial in range(60):
        k = int(rng.integers(3, 14))
        vals = list(np.round(rng.uniform(0, 500, size=k), 1))
        if trial % 3 == 0:                                   # sprinkle in flat stretches
            vals[1] = vals[0]
        nodes, edges, streets, d = _street_chain(vals)
        e2, s2, mapping = path_numbering.split_streets(edges, streets, d)
        assert set(e2["street_id_original"]) == {1} and len(mapping) == len(s2)
        covered = 0
        for sid, g in e2.reset_index().groupby("street_id"):
            idx = sorted({int(n[1:]) for n in list(g["u"]) + list(g["v"])})
            assert idx == list(range(idx[0], idx[-1] + 1))      # a contiguous piece of the chain
            seq = [vals[i] for i in idx]
            assert not any(seq[i] < seq[i - 1] - 1e-6 and seq[i] < seq[i + 1] - 1e-6 for i in range(1, len(seq) - 1)), (trial, seq)   # no interior minimum
            assert min(seq) in (seq[0], seq[-1])                # nearest node is one of its ends
            covered += len(idx) - 1
        assert covered == k - 1                                 # every edge belongs to exactly one part


def test_parts_of_a_split_named_street_get_a_letter_in_parentheses():
    """Parts after the first get (B), (C) ... on the streets and on their edges; the first part keeps the name."""
    nodes, edges, streets, d = _street_chain([10, 5, 12, 3, 11])
    edges["street_name"] = "xyz"
    e2, s2, mapping = path_numbering.split_streets(edges, streets, d)
    by_id = s2.set_index("street_id")
    assert by_id.loc[1, "street_name"] == "xyz"                      # the part that kept the id
    part = by_id["street_part"]
    for sid, name in by_id["street_name"].items():
        letter = chr(ord("A") + int(part[sid]))
        assert name == ("xyz" if part[sid] == 0 else f"xyz ({letter})")
    # the edges carry the same name as their street
    assert (e2["street_name"] == e2["street_id"].map(by_id["street_name"])).all()
    assert len(set(by_id["street_name"])) == len(by_id)              # every part is distinguishable by name


def test_unnamed_streets_and_the_opt_out_are_left_alone():
    """Unnamed streets get no suffix, and suffix_split_names=False keeps the plain name everywhere."""
    nodes, edges, streets, d = _street_chain([10, 5, 12, 3, 11])
    unnamed = streets.assign(street_name=[None])
    _, s2, _ = path_numbering.split_streets(edges, unnamed, d)
    assert s2["street_name"].isna().all()
    _, s3, _ = path_numbering.split_streets(edges, streets, d, suffix_split_names=False)
    assert (s3["street_name"] == "xyz").all()


def test_branch_names_inherit_the_part_suffix():
    """A road leaving a suffixed part is named after the suffixed name."""
    from osmaddresssystem import branches

    G = _grid_with_peak_street()
    assert any(n.endswith("(B)") for n in G["branch_name"].dropna())


def _grid_with_peak_street():
    """Pipeline run on a grid, named so that a split named street has branch roads.

    Returns:
        The streets table of a path-mode run with branch names.
    """
    from osmaddresssystem import pipeline
    from osmaddresssystem.synthetic_grid import build_idealized_grid

    G = build_idealized_grid(rows=5, cols=6, spacing=100.0, named_row=2, named_row_name="Elm Street", primary_row=None)
    # reference near a corner, so the long named row has an interior nearest node only after cutting
    _, edges, streets = pipeline.build_addressed_streets(
        G, origin=(250.0, 450.0), numbering_mode="path", label_streets_kwargs={"node_cluster_m": 0}, name_branches=True)
    return streets


def test_a_street_never_has_three_roads_at_one_node():
    """A street id is a simple path: at a T junction of one id the straight roads stay together and the side road becomes a street of its own."""
    from shapely.geometry import LineString

    pairs = {("a", "b", 0): ("a", "b", 100.0, LineString([(0, 0), (100, 0)])),
             ("b", "c", 0): ("b", "c", 120.0, LineString([(100, 0), (220, 0)])),
             ("b", "d", 0): ("b", "d", 50.0, LineString([(100, 0), (100, 50)]))}
    trails = path_numbering._trails(pairs)
    assert len(trails) == 2 and set(trails[0]) == {("a", "b", 0), ("b", "c", 0)} and trails[1] == [("b", "d", 0)]     # straight on stays, the stub leaves
    coords = {"a": (0, 0), "b": (100, 0), "c": (220, 0), "d": (100, 50)}
    nodes, edges = _graph(coords, [("a", "b", "residential", True, 1), ("b", "c", "residential", True, 1), ("b", "d", "residential", True, 1)])
    edges["nearest_main_eid"] = ""
    streets = pd.DataFrame({"street_id": [1], "street_name": [None], "street_rank": [4]})
    d = pd.Series({"a": 10.0, "b": 110.0, "c": 230.0, "d": 160.0})
    e2, s2, mapping = path_numbering.split_streets(edges, streets, d)
    flat = e2.reset_index()
    for sid, g in flat.groupby("street_id"):
        import collections
        deg = collections.Counter(list(g["u"]) + list(g["v"]))
        assert max(deg.values()) <= 4 and all(v <= 2 for v in collections.Counter(list(g[g["u"] < g["v"]]["u"]) + list(g[g["u"] < g["v"]]["v"])).values())
    assert len(set(e2["street_id"])) == 2 and set(e2["street_id_original"]) == {1, 2}      # the stub is a street of its own, not a part of street 1
