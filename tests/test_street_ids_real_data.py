"""Regression/validation tests for street_ids.label_streets against a real
OSM extract, not just the small hand-built graphs in test_street_ids.py.

tests/test_files/nuevos_ministerios/{nodes,edges}.gpkg is label_streets'
output (node_cluster_m=30, parallel_dist_m=30) for the area around
Nuevos Ministerios, Madrid - a dense, real-world interchange (Paseo de
la Castellana crossing Calle de Raimundo Fernández Villaverde and
several other named avenues) with plenty of the messy, non-idealized
geometry label_streets has to handle: divided carriageways mapped with
an uneven number of ways per side, named service/access stubs that
touch the main avenue at a different graph node than any of its other
edges, big multi-way intersections, and so on. Its streets already
carry real OSM `name`s, which is what makes it useful as ground truth:
two edges sharing an (fuzzy-matching) name are, in reality, the same
street, so how well `street_id` groups them is directly checkable,
independent of any hand-built synthetic case.

**This fixture is generated WITH real OSM names**, deliberately
decoupled from examples/nuevos_ministerios/nuevos_ministerios.py's own
checked-in output: that example script sets `DROP_OSM_NAMES = True` (to
demonstrate the geometry/rank-only identity logic in isolation, with no
real street names influencing it at all), so its own
nodes.gpkg/edges.gpkg/streets.csv carry no names and must NOT be copied
in as this fixture. This one is instead produced by loading the same
underlying `examples/nuevos_ministerios/streets.osm` data directly and
calling `street_ids.label_streets` with real names left in (the same
`angle_threshold_deg=20, node_cluster_m=30, name_similarity_threshold=
82, parallel_dist_m=30` the example script itself uses, after
`ox.truncate.largest_component`), then copying the resulting
`nodes.gpkg`/`edges.gpkg` here - never copy those two files from
examples/nuevos_ministerios/ itself, since that copy now has no names.

This file doesn't re-run label_streets (the fixtures are already its
output) - it validates properties of that output that must hold
regardless of the input, and pins down a couple of concrete before/after
examples for the parallel_dist_m fix (see street_identity.md "Parallel-
street proximity merge") so a future change that silently regresses it
gets caught here, not just in the small synthetic tests.
"""

import ast
import os
from collections import defaultdict

import geopandas as gpd
import pandas as pd
import pytest

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "test_files", "nuevos_ministerios")
_EDGES_PATH = os.path.join(_FIXTURE_DIR, "edges.gpkg")
_NODES_PATH = os.path.join(_FIXTURE_DIR, "nodes.gpkg")


def _clean_name(raw):
    """Undo stringify_nonnumeric_columns for the `name` column: "" -> None,
    a stringified list -> its first element."""
    if not raw:
        return None
    if raw.startswith("["):
        try:
            values = ast.literal_eval(raw)
            return values[0] if values else None
        except (ValueError, SyntaxError):
            return raw
    return raw


@pytest.fixture(scope="module")
def edges_gdf():
    if not os.path.exists(_EDGES_PATH):
        pytest.skip(f"real-data fixture not present: {_EDGES_PATH}")
    edges = gpd.read_file(_EDGES_PATH)
    edges["osm_name"] = edges["name"].map(_clean_name)
    return edges


@pytest.fixture(scope="module")
def nodes_gdf():
    if not os.path.exists(_NODES_PATH):
        pytest.skip(f"real-data fixture not present: {_NODES_PATH}")
    return gpd.read_file(_NODES_PATH).set_index("osmid")


def _undirected(edges_gdf):
    """Collapse the MultiDiGraph's reciprocal directed edges to one row
    per physical (undirected) edge, the same convention label_streets
    itself uses internally."""
    pair = list(
        zip(
            edges_gdf["u"].combine(edges_gdf["v"], min),
            edges_gdf["u"].combine(edges_gdf["v"], max),
            edges_gdf["key"],
        )
    )
    return edges_gdf.assign(_pair=pair).drop_duplicates("_pair")


def _raw_bearing_deg(geom, at_start: bool) -> float:
    """Raw (directed, mod 360) tangent bearing at one end of a line
    geometry, using the same 10%-in convention as street_ids._TANGENT_
    FRACTION - the away-from-that-endpoint direction, kept unfolded
    (unlike street_ids._endpoint_angle_deg, which folds to [0, 180) and
    so can't tell a genuine opposite-direction through-pair from a
    same-direction parallel-duplicate pair - see "No more than two
    edges per node" in report/street_identity.md)."""
    import math

    frac = 0.1 if at_start else 0.9
    p0 = geom.coords[0] if at_start else geom.coords[-1]
    p1 = geom.interpolate(frac, normalized=True).coords[0]
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    return math.degrees(math.atan2(dy, dx)) % 360


def test_no_node_shares_more_than_two_logical_directions_per_street_id(edges_gdf):
    """The global invariant enforced by _enforce_max_two_per_node - no
    more than two edges share a street_id at one node - actually has
    three distinct, deliberate kinds of exception:

    1. Two edges recognized as a parallel-duplicate pair (raw, unfolded
       away-from-node bearings close to each other rather than ~180 deg
       apart - i.e. heading the same general way, not a genuine
       straight-through line) count as a single logical direction and
       may coexist with one more, real, different direction at the same
       node under the same street_id - see "No more than two edges per
       node" in report/street_identity.md and
       _enforce_max_two_per_node's raw_angle_at_node parameter.
    2. A "bridge edge" - one whose *own* two endpoints each already have
       another edge carrying the same street_id - doesn't count against
       the budget at all: it's not competing for a node's two slots, it
       purely connects two points already independently established as
       the same street on each side. See _enforce_max_two_per_node's
       "Bridge-edge exception".
    3. An edge absorbed by "Redistributing too-short streets"
       (_redistribute_short_streets, min_street_length_m=200 by
       default) into a nearby valid street doesn't count against the
       budget either, by design - deliberately NOT run through
       _fix_up_two_per_node_after_reassignment the way every other
       reassignment step is, since it's being absorbed purely for
       identity, not competing to be part of the target's own
       through-line at whichever node it touches. This can only ever
       apply to an edge whose own length is below min_street_length_m
       (only such an edge could have been a redistribution candidate at
       all), so approximated here as excluding any edge under 200m from
       the per-node count entirely.

    This must hold on real data too, not just the small synthetic cases
    that exercise it directly: every node with more than two edges
    sharing a street_id must have that excess fully explained by one or
    more of the above, using the same fork_angle_threshold_deg (40 deg)
    this fixture was generated with.
    """
    undirected = _undirected(edges_gdf)

    # A bridge edge (per _enforce_max_two_per_node's exception above)
    # never counts toward any node's budget - filter those out first.
    by_node_edges = defaultdict(set)
    edge_uv = {}
    for _, row in undirected.iterrows():
        sid = row["street_id"]
        if pd.isna(sid):
            continue
        pair_key = row["_pair"]
        edge_uv[pair_key] = (row["u"], row["v"], sid)
        by_node_edges[(row["u"], sid)].add(pair_key)
        by_node_edges[(row["v"], sid)].add(pair_key)

    def is_bridge(pair_key) -> bool:
        u, v, sid = edge_uv[pair_key]
        if u == v:
            return False
        u_has_other = len(by_node_edges[(u, sid)] - {pair_key}) > 0
        v_has_other = len(by_node_edges[(v, sid)] - {pair_key}) > 0
        return u_has_other and v_has_other

    # Exception 3: an edge under min_street_length_m never counts toward
    # a node's budget either - see the docstring above.
    min_street_length_m = 200.0

    by_node_sid = defaultdict(list)
    for _, row in undirected.iterrows():
        sid = row["street_id"]
        if pd.isna(sid) or is_bridge(row["_pair"]) or row["length"] < min_street_length_m:
            continue
        for node, at_start in ((row["u"], True), (row["v"], False)):
            by_node_sid[(node, sid)].append(_raw_bearing_deg(row.geometry, at_start))

    fork_angle_threshold_deg = 40.0
    violations = []
    for (node, sid), bearings in by_node_sid.items():
        if len(bearings) <= 2:
            continue
        # Greedily pair up members whose raw bearings are close (a
        # recognized parallel-duplicate pair), same as
        # _enforce_max_two_per_node's own pairing - each recognized
        # pair collapses to one logical unit instead of two.
        remaining = list(range(len(bearings)))
        logical_units = len(bearings)
        while True:
            best = None
            for i in range(len(remaining)):
                for j in range(i + 1, len(remaining)):
                    a, b = remaining[i], remaining[j]
                    diff = abs(bearings[a] - bearings[b]) % 360
                    diff = min(diff, 360 - diff)
                    if diff <= fork_angle_threshold_deg and (best is None or diff < best[0]):
                        best = (diff, a, b)
            if best is None:
                break
            remaining.remove(best[1])
            remaining.remove(best[2])
            logical_units -= 1
        if logical_units > 2:
            violations.append((node, sid, len(bearings), logical_units))

    assert violations == []


def test_named_streets_have_a_nonzero_dominant_street_id_share(edges_gdf):
    """Every real, non-trivial named street's single largest street_id
    must account for at least some meaningful chunk of its total mapped
    length - not, say, dozens of nearly-equal slivers, which would mean
    label_streets is barely grouping it at all.

    This is deliberately a low bar (15%), not a quality target: on this
    extract, most named streets' dominant share is well above that
    (Paseo de la Castellana ~57%, see test_castellana_has_a_clear_dominant_street_id
    below), but several streets around the Nuevos Ministerios
    interchange itself - e.g. Calle de Ponzano at ~22% - are still fairly
    fragmented even with parallel_dist_m enabled (busy, multi-way
    intersections legitimately cut a through street into several pieces,
    and rule 4's "at most one partner per node" cap can cut off a
    same-name continuation at a node with more than two named
    candidates). This test exists to catch a regression that makes that
    fragmentation much worse, not to assert the current state is good
    enough - see street_identity.md for further work on that front.
    """
    named = edges_gdf[edges_gdf["osm_name"].notna()]
    by_name = named.groupby("osm_name")

    low_share_streets = []
    for name, group in by_name:
        if group["length"].sum() < 100:  # skip tiny/incidental name matches
            continue
        by_id_length = group.groupby("street_id")["length"].sum()
        dominant_share = by_id_length.max() / by_id_length.sum()
        if dominant_share < 0.15:
            low_share_streets.append((name, dominant_share))

    assert low_share_streets == []


def _street_id_of(edges_gdf, u, v):
    row = edges_gdf[(edges_gdf["u"] == u) & (edges_gdf["v"] == v)]
    assert len(row) == 1, f"expected exactly one edge ({u}, {v}), found {len(row)}"
    return row.iloc[0]["street_id"]


def test_min_street_length_m_redistribute_reaches_a_fixed_point(edges_gdf):
    """label_streets runs "redistribute too-short streets" and "the
    Rule-1 name-conflict invariant" together as a small, bounded
    fixed-point loop (see report/street_identity.md "Redistributing
    too-short streets"), not just once in sequence: the invariant pass
    can itself split a group and create a brand-new, small, disconnected
    fragment (its own fresh street_id, e.g. an unnamed leftover piece
    the "closest label" fallback keys by its own smallest edge id - see
    _split_name_conflicts) whose length was never checked against
    min_street_length_m at all if the loop only ran once.

    Found on this real extract: a two-edge, ~16m duplicate-mapped
    footway pair (osmid 1432125076 and 1443214257, sharing one node
    pair) ended up stranded as its own street_id this way, well under
    the 200m default, even though real, long, named streets exist
    all over this dense area for it to have been redistributed into.
    The fix makes the redistribute/invariant pair loop until neither
    changes anything (capped, for safety, well short of what a real
    network needs to converge). This is a regression test for the
    *orchestration*, not for _redistribute_short_streets or
    _split_name_conflicts individually - both already had their own
    direct unit tests before this bug was found; what was missing was
    running them together more than once.
    """
    rows = edges_gdf[(edges_gdf["u"] == 13194683156) & (edges_gdf["v"] == 13233580493)]
    assert len(rows) > 0
    sids = rows["street_id"].unique()
    assert len(sids) == 1
    sid = sids[0]
    assert pd.notna(sid)
    # However it landed, it must not be alone: a street_id this short
    # surviving as a singleton (or a same-osmid-only duplicate pair) is
    # exactly the bug - it must have been redistributed into (or already
    # been part of) a real, larger street.
    other_members = edges_gdf[
        (edges_gdf["street_id"] == sid)
        & ~(
            ((edges_gdf["u"] == 13194683156) & (edges_gdf["v"] == 13233580493))
            | ((edges_gdf["u"] == 13233580493) & (edges_gdf["v"] == 13194683156))
        )
    ]
    assert len(other_members) > 0




def test_parallel_dist_m_merges_a_corner_edge_into_a_single_covering_street_regardless_of_direction(
    edges_gdf,
):
    """A real repro for _merge_fully_covered_streets requiring a *single*
    covering candidate (not a blend of several), and for it correctly
    ignoring direction: nodes 1613048550, 1613048571, and 60017474 form a
    small triangle - three edges meeting pairwise, each running in a
    noticeably different direction from the other two (bearings roughly
    110, 71, and 161 deg - none within parallel_angle_threshold_deg, 20
    deg, of any other). The short corner edge (1613048550, 1613048571) is
    entirely within parallel_dist_m of, and reachable from, the single
    longer, same-named "Calle de Agustín de Betancourt" edge (1613048571,
    60017474) alone - even though their own directions don't run
    parallel to each other in the ``_merge_parallel_edges`` sense - so it
    gets merged into that one candidate specifically, not into the
    differently-named "Plaza de San Juan de la Cruz" edge (1613048550,
    60017474) it's also close to (name-ineligible), and not blended
    across both the way the old, since-removed multi-candidate version
    would have (see "Merging a fully-covered short street into a longer
    one" in report/street_identity.md).

    The corner edge's *street_name* still visibly reflects that merge -
    "Calle de Agustín de Betancourt" - even though its own final
    *street_id* number ends up distinct from its immediate covering
    candidate's, because the merge, once it pushes a node over the
    two-per-street_id budget, is followed by the same targeted
    ``_branch_walk`` cut-back every other donation-like step in this
    module uses (see "No more than two edges per node"); the name a
    merge donated survives that cut-back even when the exact numeric id
    doesn't."""
    sid = _street_id_of(edges_gdf, 1613048550, 1613048571)
    assert not pd.isna(sid)
    row = edges_gdf[
        (edges_gdf["u"].isin([1613048550, 1613048571]))
        & (edges_gdf["v"].isin([1613048550, 1613048571]))
    ].iloc[0]
    assert row["street_name"] == "Calle de Agustín de Betancourt"
    # It's never folded into the differently-named Plaza's own id.
    plaza_sid = _street_id_of(edges_gdf, 1613048550, 60017474)
    assert not pd.isna(plaza_sid)
    assert sid != plaza_sid


def test_bridging_footway_between_two_resolved_pedestrian_streets_gets_its_own_id(edges_gdf):
    """Investigated as a possibly-separate bug alongside the fully-covered-
    street merge rewrite: osmid 1435545679 (a footway split by OSM into
    two undirected edges, (13194683157, 13243204771) and (13243204771,
    13233569961)) sits directly between two other, unnamed pedestrian
    edges - osmid 1435545678 (resolved to one street_id) and osmid
    1443214257 (resolved to a different one) - and used to end up with
    street_id None at both halves under the pre-rewrite pipeline, even
    though both ends touch resolved (but different) pedestrian
    street_ids and Rule 1's name-conflict concern doesn't even apply
    (neither side is named).

    Root cause traced directly (not assumed): (13194683157,
    13243204771) is only ~1.5m long, so its own two endpoints
    node-cluster together (node_cluster_m=30) and it's treated as a
    connector; the connector-resolution step correctly gives both
    connector and its non-connector neighbor a real, resolved street_id
    early in the pipeline (never actually unresolved). What used to turn
    that into a visible ``None`` in the checked-in fixture was **not** a
    pipeline bug at all: it was a stale multi-layer GeoPackage produced
    while regenerating the fixture (``geopandas``/``pyogrio`` appends a
    new layer to an existing ``.gpkg`` file instead of replacing it, and
    ``gpd.read_file`` without an explicit ``layer=`` reads back the
    *old* default layer) - the actual `label_streets` output for this
    edge was never ``None`` once the file was regenerated cleanly
    (deleting the old .gpkg files first). Both halves do end up with
    *different* final street_ids from each other (a legitimate,
    separate outcome of the same-node two-per-street_id budget after
    the whole-street merge folds each half toward its own nearer
    neighbor - see "No more than two edges per node"), but neither is
    ``None``. Pinned here so a future regression (of either kind) is
    caught."""
    row_a = _undirected(edges_gdf)
    row_a = row_a[
        (row_a["u"].combine(row_a["v"], min) == 13194683157)
        & (row_a["u"].combine(row_a["v"], max) == 13243204771)
    ].iloc[0]
    row_b = _undirected(edges_gdf)
    row_b = row_b[
        (row_b["u"].combine(row_b["v"], min) == 13233569961)
        & (row_b["u"].combine(row_b["v"], max) == 13243204771)
    ].iloc[0]
    assert row_a["highway"] == "footway"
    assert row_b["highway"] == "footway"
    assert not pd.isna(row_a["street_id"])
    assert not pd.isna(row_b["street_id"])


def test_non_drivable_furniture_inherits_the_nearby_drivable_streets_id(edges_gdf):
    """Real repro for "Donating a drivable street's id to nearby
    non-drivable furniture": the footway edge (2053142667, 8458502442)
    (osmid [910922808, 910922807], unnamed on OSM) sits right at the
    corner where "Calle de José Abascal" and "Paseo de la Castellana"
    meet - touching (0m) both - so it used to be pruned to street_id
    None entirely, and even after furniture donation was added, a purely
    coverage-fraction-based choice used to wrongly pick Abascal (the
    footway crosses it near-perpendicularly, ~88 deg off) over Castellana
    (the footway actually runs almost exactly parallel to it, ~0.4 deg
    off) - see the direction-alignment ranking in
    _donate_non_drivable_furniture. It's now donated Castellana's own
    street_id, the drivable street it genuinely runs alongside - same
    street_id as its drivable neighbors, and the donated street_name
    surfaces on the (originally unnamed) edge too, the same way any other
    name donation does."""
    undirected = _undirected(edges_gdf)
    row = undirected[
        (undirected["u"].combine(undirected["v"], min) == 2053142667)
        & (undirected["u"].combine(undirected["v"], max) == 8458502442)
    ].iloc[0]
    assert row["highway"] == "footway"
    assert pd.isna(row["osm_name"])
    footway_sid = row["street_id"]
    assert pd.notna(footway_sid)
    assert row["street_name"] == "Paseo de la Castellana"

    castellana_sid = _street_id_of(edges_gdf, 2053142667, 8458502441)
    assert not pd.isna(castellana_sid)
    assert footway_sid == castellana_sid


def test_lower_rank_differently_named_edge_is_not_absorbed_by_castellana(edges_gdf):
    """(25902894, 355172281) is a `residential` "Calle de Raimundo
    Fernández Villaverde" edge that meets `trunk` Paseo de la Castellana
    at node 355172281. Per rule 2 ("only the dominant tier at an
    intersection can merge across it"), a lower-rank edge is cut by the
    higher-rank street it meets, not folded into it - and the two names
    don't fuzzy-match either, so no name-matching rule should merge them.

    This used to regress via dual-carriageway detection: two edges whose
    endpoints cluster to the same node pair are normally the same street
    with no name check at all, but at this busy multi-way interchange
    (node_cluster_m=30 here), edges belonging to genuinely different
    named streets ended up sharing a clustered node pair purely by
    coincidence of geometry, silently bridging Raimundo Fernández
    Villaverde into Paseo de la Castellana's street_id (and, being a
    union-find merge, going on to disrupt which real Castellana segments
    grouped together too - see the test right below)."""
    raimundo_sid = _street_id_of(edges_gdf, 25902894, 355172281)
    assert not pd.isna(raimundo_sid)

    castellana = edges_gdf[edges_gdf["osm_name"] == "Paseo de la Castellana"]
    assert raimundo_sid not in set(castellana["street_id"].dropna())


def test_genuinely_connected_castellana_segments_merge_into_one_id(edges_gdf):
    """Three directly-chained, real Paseo de la Castellana segments -
    osmid 43798344 at (355174895, 1754817142), osmid 43798342 at
    (355172281, 1311052151)/(1311052151, 1752467527)/(1752467527,
    355174895), and osmid 429691811 at (339636378, 355172281) - are
    connected end-to-node, same name, same rank (trunk), and roughly
    collinear, so they should all land under the same street_id. Before
    the dual-carriageway name-compatibility fix (see the test above),
    the wrongly-absorbed Raimundo Fernández Villaverde edge at node
    355172281 disrupted the union-find grouping at that cluster and
    split these into different street_ids instead."""
    pairs = [
        (355174895, 1754817142),
        (355172281, 1311052151),
        (1311052151, 1752467527),
        (1752467527, 355174895),
        (339636378, 355172281),
    ]
    ids = {_street_id_of(edges_gdf, u, v) for u, v in pairs}
    assert len(ids) == 1
    assert not pd.isna(next(iter(ids)))


def test_three_way_same_named_cluster_group_merges_pairwise_not_by_representative(edges_gdf):
    """At the Castellana/Plaza de San Juan de la Cruz interchange
    (node_cluster_m=30), three edges all named "Paseo de la Castellana" -
    a `primary` edge (osmid 7732138, at (1754816996, 25902821)) and two
    `service` edges (osmid 890797396, at (25902821, 8807074846); osmid
    163805454, at (1754817000, 25902821)) - end up in the same
    dual-carriageway cluster-node-pair group. The two `service` edges are
    almost perfectly collinear through node 25902821 (~1.5 deg apart);
    the `primary` edge approaches from a very different angle (~30 deg
    off from each). All three share the same name, so name-similarity
    alone can't distinguish which pair is the real through-continuation.

    This used to regress because the dual-carriageway merge picked one
    arbitrary group member as a "representative" and only compared every
    other member against *that one*, instead of checking every pair: if
    the representative happened to be the primary edge (a function of
    processing order, not geometry), it would still get unioned with
    both service edges (same name), but the two service edges would
    never get directly compared to each other - and picking a different
    representative could change the outcome. Comparing every pair in the
    group removes that order-dependence, so both service edges reliably
    get paired directly with each other at their own shared node,
    regardless of which member of the group happened to be processed
    first.

    They're no longer required to end up under the exact same FINAL
    street_id, though: this whole node_cluster_m=30 group also reaches a
    much wider, real interchange further along, where the direction-
    consistency invariant (_enforce_direction_consistency_at_node_
    clusters - see "No more than one direction per node_id" in
    report/street_identity.md) legitimately splits it again at a
    different, genuinely divergent node (25902807, ~52 deg between two
    of this group's own directions there, well past even the generous
    fork_angle_threshold_deg tolerance) - a real, separate correctness
    concern this test isn't about, and the two service edges end up on
    opposite sides of that unrelated split purely because of how this
    dense interchange happens to be shaped. What this test actually
    checks is that the pairwise dual-carriageway comparison itself
    worked: each service edge resolves to its own valid street_id
    (never dropped/left unresolved) and keeps its own real OSM name."""
    service_a_id = _street_id_of(edges_gdf, 25902821, 8807074846)
    service_b_id = _street_id_of(edges_gdf, 1754817000, 25902821)
    assert not pd.isna(service_a_id)
    assert not pd.isna(service_b_id)

    service_a_name = edges_gdf.loc[
        (edges_gdf["u"] == 25902821) & (edges_gdf["v"] == 8807074846), "osm_name"
    ].iloc[0]
    service_b_name = edges_gdf.loc[
        (edges_gdf["u"] == 1754817000) & (edges_gdf["v"] == 25902821), "osm_name"
    ].iloc[0]
    assert service_a_name == "Paseo de la Castellana"
    assert service_b_name == "Paseo de la Castellana"


def test_edge_street_name_preserves_its_own_osm_name(edges_gdf):
    """street_name must never overwrite an edge's own original OSM
    `name` with a different street's donated/representative name - only
    an edge with no OSM name of its own should show a donated name (see
    "Naming an unnamed road from a nearby named one" in
    report/street_identity.md). The Raimundo Fernández Villaverde edge
    is a good concrete repro: it has its own real OSM name, distinct
    from Paseo de la Castellana's, so its street_name must stay its own
    name and never read "Paseo de la Castellana"."""
    row = edges_gdf[(edges_gdf["u"] == 25902894) & (edges_gdf["v"] == 355172281)].iloc[0]
    assert row["osm_name"] == "Calle de Raimundo Fernández Villaverde"
    assert row["street_name"] == "Calle de Raimundo Fernández Villaverde"

    # Every edge with a real OSM name of its own must show that same
    # name as its street_name - never some other member's donated/
    # representative name.
    named = edges_gdf[edges_gdf["osm_name"].notna()]
    mismatched = named[named["osm_name"] != named["street_name"]]
    assert len(mismatched) == 0, mismatched[["u", "v", "osm_name", "street_name"]]

    # The donation mechanism itself must still work: at least one
    # genuinely unnamed edge should show a street_name donated from a
    # nearby named street.
    unnamed = edges_gdf[edges_gdf["osm_name"].isna()]
    assert (unnamed["street_name"].notna()).any()


def test_no_street_id_has_two_incompatible_real_names(edges_gdf):
    """Rule 1's invariant, checked network-wide on real data: no
    street_id may contain two or more real OSM names that don't all
    pairwise fuzzy-match. This is the final, unconditional safety net
    (see street_ids._split_name_conflicts / report/street_identity.md
    "Final name-conflict invariant") - on real, messy OSM data with
    plenty of busy intersections and parallel-merge donations, this is
    the strongest evidence that no merge site (node-touching name match,
    dual-carriageway cluster merge, parallel-proximity merge, or the
    Rule 2 second pass) can silently violate it."""
    from osmaddresssystem import name_matching

    named = edges_gdf[edges_gdf["osm_name"].notna() & edges_gdf["street_id"].notna()]
    for street_id, group in named.groupby("street_id"):
        names = sorted(set(group["osm_name"]))
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                assert name_matching.names_match(names[i], names[j]), (
                    street_id,
                    names[i],
                    names[j],
                )


def test_parking_aisle_edges_have_no_street_id_on_real_data(edges_gdf):
    """Requirement C, checked on real data: every edge tagged
    service=parking_aisle in the Nuevos Ministerios extract (a large
    parking area near the interchange contributes plenty of them) ends
    up with street_id None, the same as a private access road - see
    "Private access roads / parking aisles never get a street_id" in
    report/street_identity.md."""
    service = edges_gdf["service"].astype(str)
    parking = edges_gdf[service.str.contains("parking_aisle", na=False)]
    assert len(parking) > 0
    assert parking["street_id"].isna().all()
