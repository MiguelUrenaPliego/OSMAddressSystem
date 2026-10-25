"""Path numbering: number streets by shortest *network* distance to a reference point.

``numbering_mode="path"`` is like the radial mode, but "distance to the centre" is
the shortest distance along the road network, not the straight line:

1. **Node distances** (:func:`node_distances`). Every node gets its distance to the
   reference node, computed in stages:

   * Stage 1 - the *high-rank* network (drivable roads up to secondary, or up to
     the best road at the reference when that is less important). Routes try to stay
     on high-rank roads: stepping from a road onto a *less important* road is a
     "bad transition", unless the new road is at least as important as the best road
     at the reference node. Paths are compared by (number of bad transitions, length),
     so a short cut through minor roads never beats staying on the main road.
   * Stage 2 - lower-rank drivable roads only hang off the high-rank network: their
     nodes get ``distance of the junction + length``; high-rank distances are never
     changed by them.
   * Stage 3 - non-drivable edges (paths, footways) are used only to reach nodes that
     are not on any drivable road. They are never used to route between drivable nodes.
     Drivable nodes that cannot be reached otherwise (islands) are finally reached
     through any edge.

2. **Street splitting** (:func:`split_streets`). A street must *begin* at its node
   nearest to the reference (by network distance) and that node must be one of its two
   ends. When the nearest node is in the middle - more generally at every interior local
   *minimum* of the distance (the numbers fall and then rise again), but not at peaks - the
   street is cut there into separate street ids, as many times as needed. The first part keeps the original id, the others get new ids; every edge and
   street row keeps ``street_id_original`` and the pairs are returned as a mapping table.

3. **Numbering**. ``street_direction.assign_street_directionality(numbering_mode="path")``
   positions every node along its street as ``distance(node) - distance(start node)``, so
   numbering starts at 0 (house number 1 left / 2 right) at the beginning of each street id;
   house numbers use the usual 2 m step and branch roads the 20 m step.
"""

from __future__ import annotations

import heapq
import math
import re

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd

from .default_values import DEFAULT_HIGHWAY_RANKS, DEFAULT_PATH_HIGH_RANK_MAX, DEFAULT_RANK

_EPS = 1e-6


def highway_rank(value, highway_ranks: dict | None = None) -> int:
    """Street rank of an edge's ``highway`` value (0 most important).

    Args:
        value: A highway string, a list, or a stringified list/``"a; b"`` of several values.
        highway_ranks: Mapping highway -> rank; defaults to ``DEFAULT_HIGHWAY_RANKS``.

    Returns:
        The best (lowest) rank among the values; ``DEFAULT_RANK`` when none is known.
    """
    ranks = highway_ranks or DEFAULT_HIGHWAY_RANKS
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = re.findall(r"[a-z_]+", str(value))
    found = [ranks[p] for p in parts if p in ranks]
    return min(found) if found else DEFAULT_RANK


def _undirected_edges(edges_gdf: gpd.GeoDataFrame, highway_ranks: dict | None) -> pd.DataFrame:
    """One row per physical edge with rank, drivability and length.

    Args:
        edges_gdf: Edges indexed by ``(u, v, key)`` with ``highway``, ``length``, ``is_drivable`` and ``street_id``.
        highway_ranks: Rank mapping.

    Returns:
        Table ``u, v, key, length, rank, drivable, street`` without reverse duplicates.
    """
    e = edges_gdf.reset_index()
    pair = list(zip(np.minimum(e["u"], e["v"]), np.maximum(e["u"], e["v"]), e["key"]))
    e["_pair"] = pair
    e = e.drop_duplicates("_pair")
    out = pd.DataFrame({
        "u": e["u"].to_numpy(), "v": e["v"].to_numpy(), "key": e["key"].to_numpy(),
        "length": e["length"].to_numpy(dtype=float),
        "rank": [highway_rank(h, highway_ranks) for h in e["highway"]],
        "drivable": e["is_drivable"].fillna(True).astype(bool).to_numpy() if "is_drivable" in e else True,
        "street": e["street_id"].to_numpy() if "street_id" in e else np.nan,
    })
    return out


def reference_node(nodes_gdf: gpd.GeoDataFrame, origin: tuple[float, float]):
    """Node nearest (straight line) to the reference point.

    Args:
        nodes_gdf: Nodes indexed by node id with ``x``/``y`` columns (projected CRS).
        origin: ``(x, y)`` of the reference point in the same CRS.

    Returns:
        The node id.
    """
    d = np.hypot(nodes_gdf["x"].to_numpy() - origin[0], nodes_gdf["y"].to_numpy() - origin[1])
    return nodes_gdf.index[int(np.argmin(d))]


def node_distances(
    nodes_gdf: gpd.GeoDataFrame,
    edges_gdf: gpd.GeoDataFrame,
    origin: tuple[float, float],
    high_rank_max: int = DEFAULT_PATH_HIGH_RANK_MAX,
    highway_ranks: dict | None = None,
    backbone_streets=None,
) -> tuple[pd.Series, object]:
    """Distance to the reference node for every node, with the path-mode routing rules.

    See the module docstring for the stages and the transition rule. With ``backbone_streets`` the first
    stage is not the high-rank network but the **named backbone**: only the edges of those streets (the streets
    that have a name or are going to get one) are used to route to the reference, with plain shortest paths;
    every other street hangs off the backbone (stage 2) and footpaths follow (stage 3).

    Args:
        nodes_gdf: Nodes indexed by node id, with ``x``/``y`` (projected CRS).
        edges_gdf: Edges indexed by ``(u, v, key)`` with ``highway``, ``length`` and ``is_drivable``.
        origin: ``(x, y)`` reference point; the nearest node becomes the reference node.
        high_rank_max: Roads up to this rank form the high-rank network.
        highway_ranks: Rank mapping (default ``DEFAULT_HIGHWAY_RANKS``).
        backbone_streets: Street ids that form the routing backbone (named or to-be-named streets), or None
            for the rank-based high-rank network.

    Returns:
        ``(distance, reference)``: a Series indexed by node id (NaN for nodes that cannot
        be reached at all) and the reference node id.

    Raises:
        ValueError: If ``backbone_streets`` is given but no drivable edge of it touches the reference node.
    """
    ref = reference_node(nodes_gdf, origin)
    ed = _undirected_edges(edges_gdf, highway_ranks)
    bb = None if backbone_streets is None else {int(s) for s in backbone_streets}
    adj: dict = {}
    for u, v, length, rank, drv, st in zip(ed["u"], ed["v"], ed["length"], ed["rank"], ed["drivable"], ed["street"]):
        inb = bool(bb is not None and pd.notna(st) and int(st) in bb)
        adj.setdefault(u, []).append((v, length, rank, drv, inb))
        adj.setdefault(v, []).append((u, length, rank, drv, inb))

    drive_ranks = [x[2] for x in adj.get(ref, []) if x[3]]
    r_ref = min(drive_ranks) if drive_ranks else min([x[2] for x in adj.get(ref, [])] or [DEFAULT_RANK])
    cut = max(high_rank_max, r_ref)
    if bb is not None and not any(x[3] and x[4] for x in adj.get(ref, [])):
        raise ValueError("no drivable street of the backbone touches the reference node")

    dist: dict = {}
    # Stage 1: high-rank drivable network; lexicographic cost (bad transitions, length).
    best: dict = {}
    heap = [(0, 0.0, ref, None)]
    settled_nodes: set = set()
    while heap:
        bad, d, node, last = heapq.heappop(heap)
        if best.get((node, last), (math.inf, math.inf)) < (bad, d):
            continue
        if node not in settled_nodes:
            settled_nodes.add(node)
            dist[node] = d
        for nxt, length, rank, drv, inb in adj.get(node, []):
            if not drv or (rank > cut if bb is None else not inb):
                continue
            nb = bad + (1 if (bb is None and last is not None and rank > last and rank > r_ref) else 0)
            key = (nxt, rank)
            cand = (nb, d + length)
            if cand < best.get(key, (math.inf, math.inf)):
                best[key] = cand
                heapq.heappush(heap, (nb, d + length, nxt, rank))

    def extend(allowed) -> None:
        """Dijkstra seeded with all known distances, only filling unreached nodes."""
        h = [(d, n) for n, d in dist.items()]
        heapq.heapify(h)
        while h:
            d, node = heapq.heappop(h)
            if d > dist.get(node, math.inf) + _EPS:
                continue
            for nxt, length, rank, drv, inb in adj.get(node, []):
                if nxt in fixed or not allowed(rank, drv):
                    continue
                nd = d + length
                if nd < dist.get(nxt, math.inf) - _EPS:
                    dist[nxt] = nd
                    heapq.heappush(h, (nd, nxt))

    # Stage 2: lower-rank drivable roads hang off the high-rank network.
    fixed = set(dist)
    extend(lambda rank, drv: drv and (rank > cut if bb is None else True))
    # Stage 3: non-drivable edges only for nodes that are on no drivable road.
    drive_nodes = {n for n, nb in adj.items() if any(x[3] for x in nb)}
    fixed = drive_nodes
    extend(lambda rank, drv: not drv)
    # Stage 4: drivable islands that nothing above reached may use any edge.
    fixed = set(dist)
    extend(lambda rank, drv: True)

    return pd.Series({n: dist.get(n, np.nan) for n in nodes_gdf.index}, dtype=float), ref


def _parse_pair(text) -> tuple[int, int, int] | None:
    """Parse a ``nearest_main_eid`` cell such as ``"(np.int64(1), np.int64(2), 0)"``.

    Args:
        text: Cell value.

    Returns:
        ``(min(u, v), max(u, v), key)`` or None.
    """
    nums = re.findall(r"\d+", str(text))
    if len(nums) < 3:
        return None
    u, v, k = int(nums[0]), int(nums[1]), int(nums[2])
    return min(u, v), max(u, v), k


NO_CUT_STREETS: set = set()
"""Street ids (before cutting) that must never be cut: the editors forced them to stay as one street (set by the app per computation)."""
CUT_AT_PEAKS = False
"""Streets are cut where the distance to the reference has an interior minimum, not at peaks (set True to cut there too)."""


SYNTHETIC_CONNECTOR_KEY = 900_000_000


def _trails(pairs: dict) -> list[list]:
    """Cut the roads of one street id into simple trails: paths that never pass a node twice, so no node has three roads of one id.

    The longest unused road starts a trail and it is extended at both ends by the straightest continuation among the roads that are
    left (never back to a node the trail has already visited); what remains starts further trails.

    Args:
        pairs: ``pair key -> (u, v, length, geometry)`` of the roads of one street id.

    Returns:
        Trails (lists of pair keys), longest first.
    """
    at: dict = {}
    for pk, (u, v, _l, _g) in pairs.items():
        at.setdefault(u, []).append(pk)
        at.setdefault(v, []).append(pk)

    def heading(pk, node):                                         # unit direction of the road when it leaves the node
        u, _v, _l, g = pairs[pk]
        c = list(g.coords)
        a, b = (c[0], c[1]) if node == u else (c[-1], c[-2])
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy) or 1.0
        return dx / n, dy / n

    def other(pk, node):
        u, v, _l, _g = pairs[pk]
        return v if node == u else u

    unused = set(pairs)
    trails = []
    while unused:
        first = max(unused, key=lambda pk: (pairs[pk][2], str(pk)))
        unused.discard(first)
        u0, v0 = pairs[first][0], pairs[first][1]
        trail, visited = [first], {u0, v0}
        for node, last in ((v0, first), (u0, first)):              # extend at the end node, then at the start node
            while True:
                hl = heading(last, node)
                best = None
                for pk in at[node]:
                    if pk not in unused or other(pk, node) in visited:
                        continue
                    h = heading(pk, node)
                    cos = hl[0] * h[0] + hl[1] * h[1]               # -1 = straight on
                    if best is None or (cos, str(pk)) < best[0]:
                        best = ((cos, str(pk)), pk)
                if best is None:
                    break
                pk = best[1]
                unused.discard(pk)
                trail.append(pk)
                node = other(pk, node)
                visited.add(node)
                last = pk
        trails.append(trail)
    return sorted(trails, key=lambda t: (-sum(pairs[pk][2] for pk in t), str(t[0])))


def _cut_indices(d: list[float], at_peaks: bool = CUT_AT_PEAKS) -> list[int]:
    """Interior positions at which a street must be cut.

    A street must begin at its node nearest to the reference, that node must be an end, and the
    numbers must not fall and then rise again. So a street is cut at every interior local *minimum* of the
    distance along its path (a node nearer to the reference than both its neighbours, which includes an interior
    nearest node). A local maximum (a peak: the numbers rise and then fall) is **not** a place to cut by default
    (``CUT_AT_PEAKS``). Stretches of equal distance count as one node and are cut at their first node.

    Args:
        d: Distance to the reference of each node along the street's path.
        at_peaks: Also cut at interior local maxima (the earlier behaviour).

    Returns:
        Sorted interior indices (1 .. len(d) - 2).
    """
    runs = []                                   # (first index, last index, value) of equal-distance runs
    for i, v in enumerate(d):
        if runs and abs(v - runs[-1][2]) <= _EPS:
            runs[-1] = (runs[-1][0], i, runs[-1][2])
        else:
            runs.append((i, i, v))
    cuts = []
    for (a0, a1, av), (b0, b1, bv), (c0, c1, cv) in zip(runs, runs[1:], runs[2:]):
        if (bv < av and bv < cv) or (at_peaks and bv > av and bv > cv):
            cuts.append(b0)
    return sorted(cuts)


def split_streets(
    edges_gdf: gpd.GeoDataFrame,
    streets_df: pd.DataFrame,
    distance: pd.Series,
    suffix_split_names: bool = True,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame, pd.DataFrame]:
    """Cut streets so each one begins at its end nearest to the reference.

    Each street's main path is cut at every interior local extremum of the node distance (a
    minimum, which includes the nearest node when it is not an end, or a peak). The first part keeps the street id,
    further parts get new ids (``max id + 1`` ...). Edges that are not on the main path follow
    their nearest main edge. The original id is always kept in ``street_id_original``.

    Args:
        edges_gdf: Edges indexed by ``(u, v, key)`` with ``street_id``, ``is_main_direction`` and
            ``nearest_main_eid``.
        streets_df: One row per street.
        distance: Node distance to the reference (:func:`node_distances`).
        suffix_split_names: A named street cut into several parts has the same name on every part, so
            numbers would repeat under one name. When True (default) the parts after the first get a
            letter in parentheses, ``(B)``, ``(C)`` ..., e.g. ``"Rue X (B)"``; the first part keeps the plain
            name and unnamed streets are left alone. The unsuffixed name stays in ``street_name_base``.

    Returns:
        ``(edges_gdf, streets_df, mapping)``: copies with ``street_id`` updated and
        ``street_id_original`` added (plus ``street_part``, 0 for the part that kept the id), and a
        table ``street_id, street_id_original, street_part, n_edges`` listing every street id.
    """
    edges = edges_gdf.copy()
    idx = edges.index.to_frame(index=False)
    pair = list(zip(np.minimum(idx["u"], idx["v"]), np.maximum(idx["u"], idx["v"]), idx["key"]))
    sid = edges["street_id"].to_numpy()
    main = edges["is_main_direction"].fillna(False).astype(bool).to_numpy()
    length = edges["length"].to_numpy(dtype=float)
    new_sid = pd.Series(edges["street_id"].astype("Int64").to_numpy(), dtype="Int64")
    part_of = pd.Series(0, index=range(len(edges)))
    next_id = int(np.nanmax(edges["street_id"].astype(float))) + 1
    pair_assign: dict = {}   # (pair, original street) -> (new street id, part)

    net_deg: dict = {}
    for (a_, b_, _k) in set(pair):
        net_deg[a_] = net_deg.get(a_, 0) + 1
        net_deg[b_] = net_deg.get(b_, 0) + 1
    key_of = idx["key"].to_numpy()
    real = key_of < SYNTHETIC_CONNECTOR_KEY                              # connectors of the street identity step follow their nearest main edge
    by_street: dict = {}
    for i in range(len(edges)):
        if real[i] and pd.notna(sid[i]):
            by_street.setdefault(int(sid[i]), {}).setdefault(pair[i], (idx["u"][i], idx["v"][i], length[i], edges.geometry.iloc[i]))
    part_count: dict = {}
    for s, pairs in by_street.items():
        trails = _trails(pairs)                                           # a street id is a simple path: branches become streets of their own
        for t, trail in enumerate(trails):
            if t == 0:
                base = s
            else:
                base, next_id = next_id, next_id + 1
            part_no = part_count.get(s, 0)
            part_count[s] = part_no + 1
            G = nx.MultiGraph()
            for pk in trail:
                u0, v0, ln, _g = pairs[pk]
                G.add_edge(u0, v0, key=pk[2], length=ln, pair=pk)
            deg = dict(G.degree())
            seq = None
            if len(G.nodes) >= 3 and max(deg.values()) <= 2 and not all(x == 2 for x in deg.values()):
                start = next(n for n, x in deg.items() if x == 1)
                seq, prev = [start], None
                while True:
                    nxt = [n for n in G.neighbors(seq[-1]) if n != prev]
                    if not nxt:
                        break
                    prev = seq[-1]
                    seq.append(nxt[0])
            cuts = []
            if seq is not None and s not in NO_CUT_STREETS:                  # forced streets are not cut at minima, but stay simple paths
                d = [float(distance.get(n, np.nan)) for n in seq]
                if not any(math.isnan(x) for x in d):
                    lo, hi = 0, len(seq)                         # a dead end is reached only through the street: it cannot make the street fall and rise
                    if len(seq) > 3 and net_deg.get(seq[0], 0) == 1 and net_deg.get(seq[-1], 0) != 1:
                        lo = 1
                    elif len(seq) > 3 and net_deg.get(seq[-1], 0) == 1 and net_deg.get(seq[0], 0) != 1:
                        hi = len(seq) - 1
                    cuts = [c + lo for c in _cut_indices(d[lo:hi])]
            if seq is None or not cuts:
                for pk in trail:
                    pair_assign[pk] = (base, part_no)
                continue
            bounds = [0] + cuts + [len(seq) - 1]
            for k_, (a_, b_) in enumerate(zip(bounds, bounds[1:])):
                if k_ == 0:
                    new, pn = base, part_no
                else:
                    new, next_id = next_id, next_id + 1
                    pn = part_count[s]
                    part_count[s] += 1
                for j in range(a_, b_):
                    u1, v1 = seq[j], seq[j + 1]
                    for k in G[u1][v1]:
                        pair_assign[(min(u1, v1), max(u1, v1), k)] = (new, pn)

    make_main = np.zeros(len(edges), dtype=bool)                       # every road of a trail is on the main path of its street now
    nearest = edges["nearest_main_eid"].map(_parse_pair) if "nearest_main_eid" in edges else pd.Series([None] * len(edges))
    for i in range(len(edges)):
        if pd.isna(sid[i]):
            continue
        hit = pair_assign.get(pair[i]) if real[i] else None
        if hit is None and not real[i]:
            tgt = nearest.iloc[i]
            hit = pair_assign.get(tgt) if tgt is not None else None
        if hit is not None:
            new_sid.iloc[i], part_of.iloc[i] = hit[0], hit[1]
            if real[i]:
                make_main[i] = True
    edges["street_id_original"] = edges["street_id"]
    edges["is_main_direction"] = (edges["is_main_direction"].fillna(False).astype(bool).to_numpy() | make_main)
    edges["street_id"] = new_sid.to_numpy()
    edges["street_part"] = part_of.to_numpy()

    streets = streets_df.copy()
    streets["street_id_original"] = streets["street_id"]
    streets["street_part"] = 0
    extra = []
    info = edges[edges["street_part"] > 0][["street_id", "street_id_original", "street_part"]].drop_duplicates()
    base = streets.set_index("street_id")
    for _, r in info.iterrows():
        row = base.loc[r["street_id_original"]].copy()
        row["street_id"] = r["street_id"]
        row["street_id_original"] = r["street_id_original"]
        row["street_part"] = r["street_part"]
        extra.append(row)
    if extra:
        streets = pd.concat([streets, pd.DataFrame(extra)[streets.columns]], ignore_index=True)
    streets["street_id"] = streets["street_id"].astype(int)
    streets["street_name_base"] = streets["street_name"]
    if suffix_split_names:
        from .branches import is_named
        from .numbering import number_letter

        def _suffixed(name, part):
            """Name with its part letter, e.g. ``Rue X (B)`` (part 0 keeps the plain name)."""
            return f"{str(name).strip()} ({number_letter(int(part))})" if int(part) > 0 and is_named(name) else name

        streets["street_name"] = [_suffixed(n, p) for n, p in zip(streets["street_name"], streets["street_part"])]
        if "street_name" in edges.columns:
            by_id = streets.set_index("street_id")["street_name"].to_dict()
            moved = edges["street_part"] > 0
            edges.loc[moved, "street_name"] = [by_id.get(int(s), n) for s, n in zip(edges.loc[moved, "street_id"], edges.loc[moved, "street_name"])]
    mapping = (edges.groupby(["street_id", "street_id_original", "street_part"]).size()
               .rename("n_edges").reset_index().sort_values(["street_id_original", "street_part"]))
    return edges, streets, mapping
