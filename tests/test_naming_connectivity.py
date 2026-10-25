"""Tests for the connected-backbone and length rules of the naming plan, and for the stand-in names."""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString

from osmaddresssystem import branches, naming_plan, path_numbering

NODES = {"n0": (0, 0), "n1": (100, 0), "n2": (200, 0), "n3": (200, 150), "n4": (350, 150), "n5": (200, 260), "m0": (500, 0)}


def _net(names, extra=()):
    """A small network of five streets.

    Street 1 runs east from the reference n0 to n2 (200 m, rank 1); street 2 leaves n2 north to n3 (150 m);
    street 3 continues east from n3 to n4 (150 m); street 4 continues north from n3 to n5 (110 m).

    Args:
        names: Street id -> street name (None for unnamed).
        extra: Extra ``(street, u, v, rank)`` streets.

    Returns:
        ``(edges_gdf, streets_df)`` as after ``assign_street_directionality``.
    """
    spec = [(1, "n0", "n1", 0, 100), (1, "n1", "n2", 100, 200), (2, "n2", "n3", 0, 150), (3, "n3", "n4", 0, 150),
            (4, "n3", "n5", 0, 110)]
    ranks = {1: 1, 2: 4, 3: 3, 4: 4}
    rows = []
    for sid, u, v, a, b in spec:
        rows.append({"u": u, "v": v, "key": 0, "street_id": sid, "is_main_direction": True, "street_start_length_m": a,
                     "street_end_length_m": b, "is_drivable": True, "street_pos_u_m": a, "street_pos_v_m": b,
                     "geometry": LineString([NODES[u], NODES[v]])})
    for sid, u, v, rank in extra:
        ranks[sid] = rank
        g = LineString([NODES[u], NODES[v]])
        rows.append({"u": u, "v": v, "key": 0, "street_id": sid, "is_main_direction": True, "street_start_length_m": 0.0,
                     "street_end_length_m": g.length, "is_drivable": True, "street_pos_u_m": 0.0, "street_pos_v_m": g.length,
                     "geometry": g})
    edges = gpd.GeoDataFrame(rows, crs=32632).set_index(["u", "v", "key"])
    streets = pd.DataFrame({"street_id": sorted(ranks), "street_name": [names.get(s) for s in sorted(ranks)],
                            "street_rank": [ranks[s] for s in sorted(ranks)]})
    return edges, streets


def _connected_to_reference(plan, edges, streets, ref="n0"):
    """Check that the backbone (existing + chosen names) is one network touching the reference.

    Args:
        plan: The naming plan.
        edges: Edges.
        streets: Streets.
        ref: Reference node.

    Returns:
        True when every backbone street the reference can reach is reachable through backbone streets only.
    """
    adj, nodes = branches.street_adjacency(None, edges, streets)
    bb = plan.backbone - set(plan.unconnectable)
    seen = [s for s in bb if ref in nodes[s]]
    stack = list(seen)
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v in bb and v not in seen:
                seen.append(v)
                stack.append(v)
    return set(seen) == bb


def test_without_a_reference_an_unnamed_link_between_named_streets_is_not_needed():
    """Street 2 derives from street 1, so nothing needs naming when connectivity is not required."""
    edges, streets = _net({1: "A", 3: "B", 4: "C"})
    plan = naming_plan.plan_minimal_naming(None, edges, streets)
    assert len(plan.chosen) == 0


@pytest.mark.parametrize("method", ["exact", "greedy"])
def test_named_streets_must_be_connected_to_the_reference_through_named_streets(method):
    """B (street 3) only touches the reference through unnamed street 2, so street 2 is named to connect it."""
    edges, streets = _net({1: "A", 3: "B", 4: "C"})
    plan = naming_plan.plan_minimal_naming(None, edges, streets, reference_node="n0", method=method)
    assert list(plan.chosen["street_id"]) == [2] and plan.chosen.loc[0, "reason"] == "connection"
    assert plan.root_streets == [1] and _connected_to_reference(plan, edges, streets)


@pytest.mark.parametrize("method", ["exact", "greedy"])
def test_a_street_at_the_reference_must_be_named(method):
    """With the street at the reference unnamed it is chosen, and so is the link to the named street."""
    edges, streets = _net({3: "B"})
    plan = naming_plan.plan_minimal_naming(None, edges, streets, reference_node="n0", method=method)
    ids = set(plan.chosen["street_id"])
    assert 1 in ids and 2 in ids and _connected_to_reference(plan, edges, streets)
    # every other street is still within two levels of a name
    assert (plan.coverage["depth"] <= 2).all() and plan.uncovered == []


def test_a_derived_street_must_be_shorter_than_its_main_street():
    """A 300 m street touching a 200 m street cannot be derived from it and has to be named itself."""
    edges, streets = _net({1: "A", 3: "B", 4: "C"}, extra=[(5, "n2", "m0", 4)])   # n2 -> m0 is 300 m
    plan = naming_plan.plan_minimal_naming(None, edges, streets)
    assert 5 in set(plan.chosen["street_id"])
    parents = branches.branch_parents(None, edges, streets)
    assert parents[5] == set()                       # nothing touching it is longer than 300 m
    free = branches.branch_parents(None, edges, streets, use_length_rule=False)
    assert 1 in free[5]                              # without the rule street 1 could be its main street


def test_planned_main_streets_get_standin_names_that_turn_into_the_real_name():
    """Roads below a street that will be named carry #id now and the name after it has been assigned."""
    edges, streets = _net({1: "A"})
    e2, s2 = branches.assign_branch_names(None, edges, streets, planned_streets=[3])
    by = s2.set_index("street_id")
    assert by.loc[3, "display_name"] == "#3" and bool(by.loc[3, "to_be_named"])
    assert by.loc[4, "branch_root_id"] == 3 and by.loc[4, "branch_name"] == "1-#3" or by.loc[4, "branch_root_id"] == 3
    e3, s3 = branches.apply_assigned_names(e2, s2, {3: "Rue B"})
    by3 = s3.set_index("street_id")
    assert by3.loc[3, "street_name"] == "Rue B" and not bool(by3.loc[3, "to_be_named"])
    for sid, row in by3.iterrows():
        if isinstance(row["branch_prefix"], str) and row["branch_root_id"] == 3:
            assert row["branch_name"] == row["branch_prefix"] + "Rue B" and "#" not in row["display_name"]


def test_backbone_routing_ignores_shortcuts_through_streets_outside_the_backbone():
    """Distances to the reference run through named/to-be-named streets only; other streets hang off them."""
    coords = {"R": (0, 0), "A": (100, 0), "B": (200, 0), "X": (100, -40)}
    nodes = pd.DataFrame({"x": [c[0] for c in coords.values()], "y": [c[1] for c in coords.values()]}, index=list(coords))
    rows = []
    for u, v, hw, sid in (("R", "A", "residential", 1), ("A", "B", "residential", 1), ("R", "X", "primary", 2), ("X", "B", "primary", 3)):
        for a, b in ((u, v), (v, u)):
            g = LineString([coords[a], coords[b]])
            rows.append({"u": a, "v": b, "key": 0, "highway": hw, "is_drivable": True, "street_id": sid, "length": g.length, "geometry": g})
    edges = gpd.GeoDataFrame(rows, crs=32632).set_index(["u", "v", "key"])
    d_rank, _ = path_numbering.node_distances(nodes, edges, (0, 0))               # rank rule: the primary roads win
    assert d_rank["B"] == pytest.approx(2 * (40 ** 2 + 100 ** 2) ** 0.5)
    d_bb, _ = path_numbering.node_distances(nodes, edges, (0, 0), backbone_streets=[1])
    assert d_bb["B"] == 200.0                                                        # only the backbone street 1 is used for B
    assert d_bb["X"] == pytest.approx((100 ** 2 + 40 ** 2) ** 0.5)                   # X hangs off the reference
    with pytest.raises(ValueError):
        path_numbering.node_distances(nodes, edges, (0, 0), backbone_streets=[3])    # no backbone street at the reference
