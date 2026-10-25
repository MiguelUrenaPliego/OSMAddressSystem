from __future__ import annotations

import math

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd

from .default_values import (
    DEFAULT_DIRECTION_DEG,
    DEFAULT_NUMBERING_MODE,
    DEFAULT_PATH_HIGH_RANK_MAX,
    DEFAULT_TRANSVERSAL_ROAD_PCT,
)


def _bearing_deg(dx: float, dy: float) -> float:
    """Compass bearing in degrees, clockwise from north (0=N, 90=E).

    Args:
        dx: Displacement along the x (east) axis.
        dy: Displacement along the y (north) axis.

    Returns:
        The bearing in degrees, in [0, 360).
    """
    return math.degrees(math.atan2(dx, dy)) % 360


def _angle_diff_mod180(a: float, b: float) -> float:
    """Circular distance between two mod-180 (undirected) orientations,
    folded into [0, 90] — see report/street_identity.md's "Direction
    normalization" section for why the difference (not each angle) has
    to be folded.

    Args:
        a: First orientation, in degrees.
        b: Second orientation, in degrees.

    Returns:
        The folded circular distance, in [0, 90].
    """
    diff = abs(a - b) % 180
    return min(diff, 180 - diff)


def _farthest_pair(G: nx.MultiGraph, weight: str = "length") -> tuple[int, int]:
    """Approximate the two most-distant nodes in a connected graph via
    the standard double-sweep heuristic (exact on a tree/simple path,
    which is what a street's own edge-subgraph normally is - see
    report/street_identity.md "Street directionality").

    Args:
        G: A connected graph (typically one street's own edge-subgraph).
        weight: Edge attribute to use as distance weight.

    Returns:
        A tuple ``(a, b)`` of the two approximate farthest-apart node ids.
    """
    any_node = next(iter(G.nodes))
    dist_from_any = nx.single_source_dijkstra_path_length(G, any_node, weight=weight)
    a = max(dist_from_any, key=dist_from_any.get)
    dist_from_a = nx.single_source_dijkstra_path_length(G, a, weight=weight)
    b = max(dist_from_a, key=dist_from_a.get)
    return a, b


def detect_main_direction(edges_gdf: gpd.GeoDataFrame) -> float:
    """Autodetect a street network's dominant compass direction.

    Intended for grid-mode numbering (see ``assign_street_directionality``)
    when there's no natural fixed direction to hand in by hand. Builds a
    length-weighted histogram of each main-path edge's undirected bearing
    (folded mod 180, since a street has no direction sense) in 1-degree
    bins, and returns the bin with the most total length.

    Args:
        edges_gdf: Edge GeoDataFrame from ``street_ids.label_streets``,
            with ``is_main_direction`` and ``length`` columns and a
            ``geometry`` column of line geometries.

    Returns:
        The dominant bearing, in degrees, in [0, 180).

    Raises:
        ValueError: If ``edges_gdf`` has no ``is_main_direction=True``
            edges to measure.
    """
    main = edges_gdf[edges_gdf["is_main_direction"]]
    if main.empty:
        raise ValueError("edges_gdf has no is_main_direction=True edges to measure")

    bins = np.zeros(180)
    for geom, length in zip(main.geometry, main["length"]):
        p0 = geom.coords[0]
        p1 = geom.coords[-1]
        bearing = _bearing_deg(p1[0] - p0[0], p1[1] - p0[1]) % 180
        bin_idx = int(bearing) % 180
        bins[bin_idx] += length

    return float(np.argmax(bins))


def assign_street_directionality(
    nodes_gdf: gpd.GeoDataFrame,
    edges_gdf: gpd.GeoDataFrame,
    streets_df: pd.DataFrame,
    origin: tuple[float, float],
    transversal_road_pct: float = DEFAULT_TRANSVERSAL_ROAD_PCT,
    numbering_mode: str = DEFAULT_NUMBERING_MODE,
    direction_deg: float = DEFAULT_DIRECTION_DEG,
    node_distances: dict | pd.Series | None = None,
    path_high_rank_max: int = DEFAULT_PATH_HIGH_RANK_MAX,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Classify each street and give every edge a position along its
    street's own canonical numbering, using one of two
    numbering schemes (``numbering_mode``):

    - ``"radial"`` (default): classification/numbering relative to a
      center point, ``origin`` — see below. Suited to towns that grew
      outward from a center.
    - ``"path"``: positions are shortest *network* distances to the reference point
      ``origin`` (see ``path_numbering``): every node is positioned as its distance to the
      reference minus the distance of the street's start node, the node of the street nearest
      to the reference, so numbering starts at 0 at the beginning of every street id. Pass
      ``node_distances`` from ``path_numbering.node_distances`` (computed here when omitted);
      streets should already have been cut with ``path_numbering.split_streets``.
    - ``"grid"``: classification/numbering relative to a fixed compass
      direction, ``direction_deg`` (0 = north, clockwise, default
      north), and its perpendicular 90° clockwise from it (east, by
      default) — like a Manhattan-style avenues/streets grid, where
      numbering only cares about which way a street runs, not where it
      sits relative to any one point. ``origin`` is ignored in this
      mode.

    ``origin``/coordinates must be in the same projected CRS as
    ``nodes_gdf``/``edges_gdf`` (both already required to be projected
    by ``street_ids.label_streets`` for the same reason: planar
    distances and angles only mean what you'd expect in a projected
    CRS, not lat/lon degrees).

    For each street (the set of edges sharing a ``street_id`` — see
    ``street_ids.label_streets``), treated per connected component in
    case a named street is spatially split into unrelated pieces:

    1. Find its two endpoints. A street is normally a simple path (see
       the note in report/street_identity.md on why a street_id's
       edges can only form paths or closed loops, never a branching
       tree), so its endpoints are its two degree-1 nodes. A closed
       loop has none, so its two most-distant points (by the standard
       double-sweep heuristic) stand in for "endpoints" instead.
    2. Classify the street:
       - ``"radial"`` mode: measure both endpoints' distance to
         ``origin``. If that difference, as a percentage of the
         street's total length, is below ``transversal_road_pct``, the
         street runs roughly perpendicular to the radius from
         ``origin`` — call it "transversal". Otherwise it runs roughly
         toward/away from ``origin`` — "radial".
       - ``"grid"`` mode: measure the undirected bearing between the
         street's two endpoints (mod 180°, folded to the circular
         distance to ``direction_deg`` mod 180° vs. to
         ``direction_deg + 90`` mod 180°). Whichever is closer wins:
         "primary" (roughly parallel to ``direction_deg``) or "cross"
         (roughly parallel to the perpendicular).
    3. Pick the canonical numbering start:
       - radial mode, radial street: whichever endpoint is nearest
         ``origin`` (so the cumulative length increases moving away
         from ``origin``).
       - radial mode, transversal street: whichever endpoint comes
         first sweeping clockwise around ``origin`` (so the cumulative
         length increases moving clockwise).
       - grid mode, primary street: whichever endpoint has the smaller
         projection onto the ``direction_deg`` unit vector (so the
         cumulative length increases moving in that compass direction,
         e.g. climbing house numbers heading north).
       - grid mode, cross street: whichever endpoint has the smaller
         projection onto the ``direction_deg + 90`` (clockwise) unit
         vector (cumulative length increases moving in that
         perpendicular direction, e.g. heading east when
         ``direction_deg`` is north).
    4. Walk the street from that start (Dijkstra over the street's own
       edges only, weighted by length) to get every node's cumulative
       distance from the start.

    Adds to ``edges_gdf``:
      - ``street_start_length_m`` / ``street_end_length_m``: the
        physical segment's fixed cumulative-length bounds along its
        street (``street_start_length_m`` is always the smaller value,
        0 at the very first segment; ``street_end_length_m`` is always
        ``street_start_length_m`` + the edge's own length). These are
        properties of the *undirected* physical segment, not of either
        directed copy's own ``u``/``v`` order — osmnx's usual
        `MultiDiGraph` convention gives each physical edge two directed
        rows (``u -> v`` and ``v -> u``), and both get the same
        ``street_start_length_m``/``street_end_length_m`` values,
        for convenient lookup regardless of which directed copy a
        caller has in hand. They're always present and mean the same
        thing regardless of ``numbering_mode``.

    Adds to ``streets_df``:
      - ``road_type``: ``"radial"``/``"transversal"`` in radial mode,
        ``"primary"``/``"cross"`` in grid mode.
      - ``length_m``: total street length.

    A street with multiple disconnected components (e.g. a name reused
    for two unrelated pieces of road) gets classified/numbered per
    component independently; ``road_type``/``length_m`` in ``streets_df``
    then reflect whichever component was processed last for that
    ``street_id`` — a caveat worth checking for named streets before
    relying on those two summary columns, though `street_start_length_m`/
    `street_end_length_m` on the edges themselves are always correct
    per-component regardless.

    Args:
        nodes_gdf: Node GeoDataFrame from ``street_ids.label_streets``,
            in a projected CRS, with ``x``/``y`` columns.
        edges_gdf: Edge GeoDataFrame from ``street_ids.label_streets``
            (indexed by ``u``/``v``/``key``), with a ``street_id`` column.
        streets_df: One row per ``street_id``, from
            ``street_ids.label_streets``.
        origin: ``(x, y)`` center point in ``numbering_mode="radial"``
            mode, in the same projected CRS as ``nodes_gdf``/``edges_gdf``.
            Ignored in ``"grid"`` mode.
        transversal_road_pct: In radial mode, the maximum endpoint-to-
            origin distance difference (as a percentage of the street's
            total length) below which a street is classified
            "transversal" rather than "radial".
        numbering_mode: ``"radial"`` or ``"grid"`` — see above.
        direction_deg: In grid mode, the compass direction (0 = north,
            clockwise) that defines the "primary" axis; its perpendicular
            (``direction_deg + 90``) defines the "cross" axis. Ignored in
            radial mode.
        node_distances: Path mode only: distance to the reference of every node (a mapping or a
            Series indexed by node id). Computed with ``path_numbering.node_distances`` when
            omitted.
        path_high_rank_max: Path mode only: maximum street rank of the high-rank network (used when
            ``node_distances`` is computed here).

    Returns:
        A tuple ``(edges_gdf, streets_df)`` — copies of the inputs with
        the columns described above added. Besides ``street_start_length_m``/
        ``street_end_length_m`` every edge gets ``street_pos_u_m``/``street_pos_v_m``, the position
        of its own ``u`` and ``v`` node along the street's numbering (0 at the street's start), which
        gives the numbering direction of an edge regardless of its u/v order. In path mode
        ``streets_df`` also gets ``path_profile`` (JSON ``[[along-street length, position], ...]``,
        used to turn a point's position along the street line into its numbering position).

    Raises:
        ValueError: If ``numbering_mode`` is not ``"radial"``, ``"grid"`` or ``"path"``.
    """
    if numbering_mode not in ("radial", "grid", "path"):
        raise ValueError(f"numbering_mode must be 'radial', 'grid' or 'path', got {numbering_mode!r}")
    if numbering_mode == "path" and node_distances is None:
        from . import path_numbering

        node_distances, _ref = path_numbering.node_distances(
            nodes_gdf, edges_gdf, origin, high_rank_max=path_high_rank_max
        )

    origin_x, origin_y = origin
    node_xy = dict(zip(nodes_gdf.index, zip(nodes_gdf["x"], nodes_gdf["y"])))

    if numbering_mode == "grid":
        primary_theta = math.radians(direction_deg % 360)
        primary_axis = (math.sin(primary_theta), math.cos(primary_theta))
        cross_theta = math.radians((direction_deg + 90) % 360)
        cross_axis = (math.sin(cross_theta), math.cos(cross_theta))

        def _proj(node: int, axis: tuple[float, float]) -> float:
            """Scalar projection of ``node``'s (x, y) position onto ``axis``."""
            x, y = node_xy[node]
            return x * axis[0] + y * axis[1]

    edges_gdf = edges_gdf.copy()
    edges_reset = edges_gdf.reset_index()
    u_arr = edges_reset["u"].to_numpy()
    v_arr = edges_reset["v"].to_numpy()
    edges_reset["_pair"] = list(
        zip(np.minimum(u_arr, v_arr), np.maximum(u_arr, v_arr), edges_reset["key"])
    )
    undirected = edges_reset.drop_duplicates("_pair").set_index("_pair")

    net_deg: dict = {}                                    # roads per node in the whole network: 1 = a dead end, which no other route can reach
    for u_, v_, _k in undirected.index:
        net_deg[u_] = net_deg.get(u_, 0) + 1
        net_deg[v_] = net_deg.get(v_, 0) + 1
    start_length: dict = {}
    end_length: dict = {}
    node_pos: dict = {}
    street_total_length: dict = {}
    street_road_type: dict = {}
    street_profile: dict = {}
    dist_of = dict(node_distances) if node_distances is not None else {}

    for street_id, group in undirected.groupby("street_id"):
        # Rule 4: only walk/number a street_id's own main-path edges
        # (is_main_direction == True, set by street_ids.label_streets'
        # post-hoc longest-path pass - see report/street_identity.md
        # "Main-direction detection"). A non-main edge (a parallel
        # duplicate or a spur cut off elsewhere in that street_id, e.g.
        # by the dual-carriageway auto-merge) keeps its street_id/
        # street_name but is excluded from the numbered line itself -
        # it never enters the walked graph below, so it falls through
        # to the "no street_id" branch further down and gets null
        # position/direction fields, same null convention as an
        # unresolved connector.
        main_group = group[group["is_main_direction"]]

        G = nx.MultiGraph()
        for pair_key, row in main_group.iterrows():
            u, v, key = pair_key
            G.add_edge(u, v, key=key, length=row["length"], pair=pair_key)

        street_total_length[street_id] = group["length"].sum()

        if G.number_of_edges() == 0:
            # Every edge of this street_id was somehow non-main (should
            # not normally happen - label_streets always flags exactly
            # one main edge per non-empty street_id - but handled
            # defensively rather than crashing).
            continue

        for component_nodes in nx.connected_components(G):
            comp = G.subgraph(component_nodes)

            if len(comp.nodes) == 1:
                node_a = node_b = next(iter(comp.nodes))
            else:
                degrees = dict(comp.degree())
                endpoint_candidates = [n for n, d in degrees.items() if d == 1]
                if len(endpoint_candidates) == 2:
                    node_a, node_b = endpoint_candidates
                else:
                    node_a, node_b = _farthest_pair(comp)

            comp_length = sum(d["length"] for _, _, d in comp.edges(data=True))

            if numbering_mode == "path":
                dvals = {n: dist_of.get(n, float("nan")) for n in comp.nodes}
                if any(math.isnan(x) for x in dvals.values()):
                    continue  # nodes the reference cannot reach: not numbered
                ends = [node_a, node_b] if node_a != node_b and len(comp.nodes) > 1 and max(dict(comp.degree()).values()) <= 2 else None
                if ends is not None:                           # a street starts at one of its ends: the one with the smaller distance,
                    both_dead = all(net_deg.get(n, 0) == 1 for n in ends)      # but a dead end is only reached through the street itself
                    start_node = min(ends, key=lambda n: ((net_deg.get(n, 0) == 1 and not both_dead), dvals[n], str(n)))
                else:
                    start_node = min(dvals, key=lambda n: (dvals[n], str(n)))
                road_type = "path"
            elif numbering_mode == "grid":
                bearing_ab = _bearing_deg(
                    node_xy[node_b][0] - node_xy[node_a][0],
                    node_xy[node_b][1] - node_xy[node_a][1],
                ) % 180
                diff_to_primary = _angle_diff_mod180(bearing_ab, direction_deg % 180)
                diff_to_cross = _angle_diff_mod180(bearing_ab, (direction_deg + 90) % 180)
                is_primary = diff_to_primary <= diff_to_cross
                axis = primary_axis if is_primary else cross_axis
                start_node = node_a if _proj(node_a, axis) <= _proj(node_b, axis) else node_b
                road_type = "primary" if is_primary else "cross"
            else:
                dist_a = math.dist(node_xy[node_a], (origin_x, origin_y))
                dist_b = math.dist(node_xy[node_b], (origin_x, origin_y))
                ratio_pct = 0.0 if comp_length == 0 else abs(dist_a - dist_b) / comp_length * 100
                is_transversal = ratio_pct < transversal_road_pct

                if is_transversal:
                    bearing_a = _bearing_deg(
                        node_xy[node_a][0] - origin_x, node_xy[node_a][1] - origin_y
                    )
                    bearing_b = _bearing_deg(
                        node_xy[node_b][0] - origin_x, node_xy[node_b][1] - origin_y
                    )
                    start_node = node_a if (bearing_b - bearing_a) % 360 <= 180 else node_b
                else:
                    start_node = node_a if dist_a <= dist_b else node_b
                road_type = "transversal" if is_transversal else "radial"

            cumulative = nx.single_source_dijkstra_path_length(
                comp, start_node, weight="length"
            )
            if numbering_mode == "path":
                # position of a node = its distance along the street from the start node (the end with the smaller distance to the
                # reference): the numbers rise as one advances, whatever the distance to the reference does on the way
                position = {n: float(cumulative[n]) for n in comp.nodes}
                is_path = len(comp.nodes) > 1 and max(dict(comp.degree()).values()) <= 2 and comp.degree(start_node) == 1
                street_profile[street_id] = (
                    sorted((float(cumulative[n]), float(position[n])) for n in comp.nodes) if is_path else None
                )
            else:
                position = cumulative

            for u, v, key, data in comp.edges(keys=True, data=True):
                seg_len = data["length"]
                cu, cv = position[u], position[v]
                s_len = min(cu, cv)
                pair_key = data["pair"]
                start_length[pair_key] = s_len
                end_length[pair_key] = max(cu, cv) if numbering_mode == "path" else s_len + seg_len
                node_pos[pair_key] = {u: cu, v: cv}

            street_road_type[street_id] = road_type

    # Edges with no street_id at all (an unresolved connector, or a
    # non-drivable edge that street_ids.label_streets couldn't donate a
    # nearby drivable street's id to - e.g. its own real name didn't
    # fuzzy-match any reachable candidate's - see
    # report/street_identity.md "Donating a drivable street's id to
    # nearby non-drivable furniture") never appear in start_length/
    # end_length, since undirected.groupby("street_id") drops null keys.
    # They get no position either, for the same reason they get no
    # street_id: they aren't part of any street to have a position
    # along. Note this is now the *less* common outcome for a footway/
    # sidewalk running alongside a drivable road: most such edges are
    # donated the drivable street's id instead, and so are numbered
    # along with it like any other member.
    start_len_col = []
    end_len_col = []
    pos_u_col = []
    pos_v_col = []
    for u, v, key in zip(edges_reset["u"], edges_reset["v"], edges_reset["key"]):
        pair_key = (min(u, v), max(u, v), key)
        if pair_key not in start_length:
            start_len_col.append(None)
            end_len_col.append(None)
            pos_u_col.append(None)
            pos_v_col.append(None)
            continue
        start_len_col.append(start_length[pair_key])
        end_len_col.append(end_length[pair_key])
        pos_u_col.append(node_pos[pair_key][u])
        pos_v_col.append(node_pos[pair_key][v])

    edges_gdf["street_start_length_m"] = start_len_col
    edges_gdf["street_end_length_m"] = end_len_col
    edges_gdf["street_pos_u_m"] = pos_u_col
    edges_gdf["street_pos_v_m"] = pos_v_col

    streets_df = streets_df.copy()
    streets_df["road_type"] = streets_df["street_id"].map(street_road_type)
    streets_df["length_m"] = streets_df["street_id"].map(street_total_length)
    if numbering_mode == "path":
        import json

        streets_df["path_profile"] = streets_df["street_id"].map(
            lambda s: json.dumps(street_profile[s]) if street_profile.get(s) else None
        )

    return edges_gdf, streets_df
