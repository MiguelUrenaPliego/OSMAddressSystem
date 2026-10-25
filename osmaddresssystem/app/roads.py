"""Missing roads: read a geofile that has only lines, generate its nodes and connect it to the street network.

A file of missing roads (KML, GeoJSON, GeoPackage, shapefile ...) only has edges. :func:`integrate_roads` makes it part of the
network the rest of the app works on:

1. **Nodes are generated.** The lines are noded against each other (crossings and touching ends become nodes; ends within
   ``node_tol_m`` of another line are snapped onto it) and every end point becomes a node.
2. **Connections.** A new node within ``auto_m`` (10 m) of an existing node is connected to it automatically, and so is an end that
   lies on an existing road (within ``touch_m``, 10 m: a T-junction, the road is split there). Anything else is
   left for the user, who connects a node by clicking an existing node, or a point of an existing road: that road is then split
   at the point (new node, both halves keep the road's street id) and the new node is connected to it. The suggestions
   (nearest node or road within ``suggest_m``) are reported.
3. **The geometry of the new edge is modified**: a connected end is moved onto the node it is connected to, so the lines
   really meet.
4. New streets (one per drawn line) get ids above all existing ones, the rank of their ``highway`` (default ``residential``)
   and, if the file has a name, that name.

The new edges are plain rows of the network tables (both directions), so street identities, main paths, routing, numbering
and naming work on them like on any other street. Nothing in the base files is changed: the integration is recomputed from
the stored drawn lines and connection choices every time (see ``engine.effective_base``).
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point
from shapely.ops import substring, unary_union

from ..path_numbering import highway_rank

HIGHWAY_DEFAULT = "residential"
RANK_HW = {0: "trunk", 1: "primary", 2: "secondary", 3: "tertiary", 4: "residential", 5: "footway"}
SYNTHETIC_KEY = 900_000_000
"""Edge keys from this value on are synthetic connectors of the street identity step and cannot be split."""
_KML = "{http://www.opengis.net/kml/2.2}"


def read_lines(path: Path) -> gpd.GeoDataFrame:
    """Read the lines of a geofile.

    KML is read directly (line placemarks and the lines inside multi-geometries); other formats through geopandas. Points and
    polygons are ignored, multi-lines are split, heights are dropped.

    Args:
        path: ``.kml``, ``.geojson``, ``.gpkg``, ``.shp`` (or anything geopandas reads).

    Returns:
        Lines in WGS84 with ``name`` and ``highway`` columns (None when the file has none).

    Raises:
        ValueError: If the file has no lines.
    """
    path = Path(path)
    rows = []
    if path.suffix.lower() == ".kml":
        for pm in ET.parse(path).getroot().iter(_KML + "Placemark"):
            nm = pm.find(_KML + "name")
            for ls in pm.iter(_KML + "LineString"):
                c = ls.find(_KML + "coordinates")
                if c is None or not c.text:
                    continue
                pts = [tuple(float(v) for v in tok.split(",")[:2]) for tok in c.text.split() if tok.count(",") >= 1]
                if len(pts) >= 2:
                    rows.append({"name": nm.text.strip() if nm is not None and nm.text else None, "highway": None, "geometry": LineString(pts)})
        gdf = gpd.GeoDataFrame(rows, columns=["name", "highway", "geometry"], geometry="geometry", crs=4326)
    else:
        raw = gpd.read_file(path)
        if raw.crs is None:
            raw = raw.set_crs(4326)
        raw = raw.to_crs(4326).explode(index_parts=False)
        raw = raw[raw.geometry.geom_type == "LineString"]
        name_col = next((c for c in raw.columns if c.lower() in ("name", "label", "street_name")), None)
        hw_col = next((c for c in raw.columns if c.lower() == "highway"), None)
        geoms = [shapely.force_2d(g) for g in raw.geometry]
        gdf = gpd.GeoDataFrame({"name": [(None if name_col is None or pd.isna(v) else str(v)) for v in (raw[name_col] if name_col else [None] * len(raw))],
                                "highway": [(None if hw_col is None or pd.isna(v) else str(v)) for v in (raw[hw_col] if hw_col else [None] * len(raw))]},
                               geometry=geoms, crs=4326)
    gdf = gdf[~gdf.geometry.is_empty].reset_index(drop=True)
    if gdf.empty:
        raise ValueError("the file contains no lines")
    return gdf


def features_from_lines(gdf: gpd.GeoDataFrame) -> list[dict]:
    """Make the saveable form of drawn lines.

    Args:
        gdf: Output of :func:`read_lines`.

    Returns:
        ``{"id", "coords": [[lon, lat], ...], "name", "highway"}`` per line (rounded to 7 decimals).
    """
    return [{"id": i, "coords": [[round(x, 7), round(y, 7)] for x, y in g.coords], "name": r["name"], "highway": r["highway"]}
            for i, (g, (_, r)) in enumerate(zip(gdf.geometry, gdf.iterrows()))]


def _node_key(x: float, y: float) -> str:
    """Deterministic key of a generated node from its position.

    Args:
        x: Projected x in metres.
        y: Projected y in metres.

    Returns:
        ``"<x cm>_<y cm>"``.
    """
    return f"{int(round(x * 100))}_{int(round(y * 100))}"


@dataclass
class NodedRoads:
    """Drawn lines after the generation of their nodes.

    Attributes:
        nodes: Node key -> ``(x, y)`` (projected metres).
        pieces: One dict per edge: ``u``/``v`` node keys, ``geom`` (projected), ``feature`` (``"<import>:<id>"``), ``name``, ``highway``.
        degree: Node key -> number of pieces that touch it.
    """

    nodes: dict = field(default_factory=dict)
    pieces: list = field(default_factory=list)
    degree: dict = field(default_factory=dict)


def node_lines(features: list[dict], crs, tol_m: float = 1.0) -> NodedRoads:
    """Generate the nodes of a set of drawn lines.

    Ends within ``tol_m`` of another drawn line are snapped onto it, all lines are split where they cross or touch, end points
    closer than ``tol_m`` are merged into one node.

    Args:
        features: Drawn lines (``coords`` in WGS84, plus ``name``, ``highway`` and a ``key`` such as ``"3:0"``).
        crs: Projected CRS of the network.
        tol_m: Snapping / merging tolerance in metres.

    Returns:
        The :class:`NodedRoads`.
    """
    tr = Transformer.from_crs(4326, crs, always_xy=True)
    lines, meta = [], []
    for f in features:
        xs, ys = tr.transform([c[0] for c in f["coords"]], [c[1] for c in f["coords"]])
        g = LineString(list(zip(xs, ys)))
        if g.length > 0:
            lines.append(g)
            meta.append(f)
    if not lines:
        return NodedRoads()
    snapped = []
    for i, g in enumerate(lines):
        others = [h for j, h in enumerate(lines) if j != i]
        snapped.append(shapely.snap(g, unary_union(others), tol_m) if others else g)
    noded = unary_union(snapped)
    parts = list(noded.geoms) if hasattr(noded, "geoms") else [noded]
    parts = [p for p in parts if p.geom_type == "LineString" and p.length > 0]

    ends = [(p.coords[0], p.coords[-1]) for p in parts]
    pts = np.array([c for e in ends for c in e])
    parent = list(range(len(pts)))

    def find(a: int) -> int:
        """Union-find root with path compression."""
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    order = np.argsort(pts[:, 0])
    for ai in range(len(order)):
        a = order[ai]
        for bi in range(ai + 1, len(order)):
            b = order[bi]
            if pts[b, 0] - pts[a, 0] > tol_m:
                break
            if np.hypot(*(pts[a] - pts[b])) <= tol_m:
                parent[find(a)] = find(b)
    groups: dict = {}
    for i in range(len(pts)):
        groups.setdefault(find(i), []).append(i)
    xy_of = {}
    for members in groups.values():
        cx, cy = pts[members].mean(axis=0)
        for m in members:
            xy_of[m] = (float(cx), float(cy))
    out = NodedRoads()
    for k, p in enumerate(parts):
        a, b = xy_of[2 * k], xy_of[2 * k + 1]
        ua, ub = _node_key(*a), _node_key(*b)
        if ua == ub:
            continue
        coords = [a] + list(p.coords)[1:-1] + [b]
        mid = p.interpolate(0.5, normalized=True)
        owner = next((meta[i] for i, g in enumerate(lines) if g.distance(mid) <= tol_m + 0.01), meta[0])
        out.nodes[ua], out.nodes[ub] = a, b
        out.pieces.append({"u": ua, "v": ub, "geom": LineString(coords), "feature": owner["key"], "name": owner.get("name"),
                           "highway": owner.get("highway")})
        out.degree[ua] = out.degree.get(ua, 0) + 1
        out.degree[ub] = out.degree.get(ub, 0) + 1
    return out


@dataclass
class RoadsResult:
    """The network with the drawn roads added.

    Attributes:
        nodes: Nodes (base + generated).
        edges: Edges (base, possibly with split roads, + new edges, both directions).
        streets: Streets (base + one per drawn line).
        edge_edits: The community edge edits, rewritten for roads that were split (None when there were none).
        report: ``{"nodes": [...], "dangling": [...], "isolated": [...], "street_ids": {...}, "split_edges": n, ...}``.
        new_edges: The new edges as lines (WGS84) with the street id and the connection state of their ends.
    """

    nodes: gpd.GeoDataFrame
    edges: gpd.GeoDataFrame
    streets: pd.DataFrame
    edge_edits: pd.DataFrame | None
    report: dict
    new_edges: gpd.GeoDataFrame


def edge_ref(u, v, key) -> str:
    """Stable reference of a road (both directions share it): ``"<small node>-<large node>-<key>"``.

    Args:
        u: One end node.
        v: The other end node.
        key: Edge key.

    Returns:
        The reference text.
    """
    a, b = sorted((int(u), int(v)))
    return f"{a}-{b}-{int(key)}"


def drop_edges(edges: gpd.GeoDataFrame, streets: pd.DataFrame, refs) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Remove roads (both directions) and the streets that are left without any edge.

    Args:
        edges: Edges indexed by ``(u, v, key)``.
        streets: Streets table.
        refs: Edge references (see :func:`edge_ref`).

    Returns:
        ``(edges, streets)`` without the removed roads.
    """
    refs = set(refs)
    if not refs:
        return edges, streets
    gone = [i for i in edges.index if edge_ref(*i) in refs]
    if not gone:
        return edges, streets
    edges = edges.drop(index=gone)
    alive = set(pd.to_numeric(edges["street_id"], errors="coerce").dropna().astype(int))
    return edges, streets[streets["street_id"].astype(int).isin(alive)].reset_index(drop=True)


def assign_street_ids(edges: gpd.GeoDataFrame, streets: pd.DataFrame, mapping: dict) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Give roads another street id (the editors' choice at a node: merge a street into another, or split a branch off).

    Args:
        edges: Edges indexed by ``(u, v, key)`` with ``street_id``.
        streets: Streets table.
        mapping: Edge reference (see :func:`edge_ref`) -> the street id the road now belongs to. A new id gets a copy of the street row
            it came from (without a name); a street left without edges disappears.

    Returns:
        ``(edges, streets)`` with the new membership.
    """
    if not mapping:
        return edges, streets
    edges = edges.copy()
    ids = [int(mapping[edge_ref(*i)]) if edge_ref(*i) in mapping else None for i in edges.index]
    old = edges["street_id"].copy()
    new_rows = []
    for new_id in sorted({i for i in ids if i is not None} - {int(x) for x in streets["street_id"]}):
        src = next(int(old.iloc[k]) for k, i in enumerate(ids) if i == new_id)
        row = streets[streets["street_id"].astype(int) == src].iloc[0].copy()
        row["street_id"] = new_id
        for c in ("street_name", "street_name_base"):
            if c in row.index:
                row[c] = ""
        new_rows.append(row)
    if new_rows:
        streets = pd.concat([streets, pd.DataFrame(new_rows)], ignore_index=True)
    edges["street_id"] = [i if i is not None else o for i, o in zip(ids, old)]
    if "street_name" in edges.columns:                               # the edges follow the name of the street they joined
        names = {int(r["street_id"]): r["street_name"] for _, r in streets.iterrows()}
        edges["street_name"] = [names.get(i, o) if i is not None else o for i, o in zip(ids, edges["street_name"])]
    alive = set(pd.to_numeric(edges["street_id"], errors="coerce").dropna().astype(int))
    return edges, streets[streets["street_id"].astype(int).isin(alive)].reset_index(drop=True)


def _find_duplicates(edges: gpd.GeoDataFrame, pieces: list, final_id: dict, base_ids: set, crs, keep: set, ratio: float = 0.10,
                     max_edges: int = 8, max_apart_m: float = 50.0) -> list[dict]:
    """Existing roads that a new road replaces: same end nodes, about the same length.

    A new piece whose two ends are connected to existing nodes duplicates the existing roads that join those nodes when their total
    length is within ``ratio`` (10 %) of the new length (and they follow it within ``max_apart_m``, which keeps a different road of
    similar length from being taken for it).

    Args:
        edges: Existing edges (before the new ones are added).
        pieces: Noded pieces of the new roads.
        final_id: Node key -> network node id.
        base_ids: Ids of the nodes that existed before.
        crs: Projected CRS of the edges.
        keep: References of existing roads that must stay.
        ratio: Length tolerance.
        max_edges: Longest chain of existing edges compared with one new piece.
        max_apart_m: Largest Hausdorff distance between the new piece and the existing chain.

    Returns:
        ``{"refs": [...], "piece": index, "feature", "new_length", "old_length"}`` per duplicate found.
    """
    und = edges[[u < v for u, v, _ in edges.index]]
    und = und[[k < SYNTHETIC_KEY for _, _, k in und.index]]
    G = nx.Graph()
    direct: dict = {}                                  # every road between two nodes (parallel roads included)
    for (u, v, k), ln in zip(und.index, und["length"].astype(float) if "length" in und else und.geometry.length):
        direct.setdefault(frozenset((u, v)), []).append({"length": float(ln), "ref": edge_ref(u, v, k), "key": (u, v, k)})
        if not G.has_edge(u, v) or ln < G[u][v]["length"]:
            G.add_edge(u, v, length=float(ln), ref=edge_ref(u, v, k), key=(u, v, k))
    out, claimed = [], set()
    # a drawn line that crosses other lines is cut into pieces; compare it as a whole too (its two far ends)
    whole: dict = {}
    for i, p in enumerate(pieces):
        whole.setdefault(p["feature"], []).append(i)
    candidates = []
    for feat, idx in whole.items():
        if len(idx) < 2:
            continue
        deg: dict = {}
        for i in idx:
            for k in (pieces[i]["u"], pieces[i]["v"]):
                deg[k] = deg.get(k, 0) + 1
        ends = [k for k, d in deg.items() if d == 1]
        if len(ends) == 2:
            candidates.append({"feature": feat, "u": final_id[ends[0]], "v": final_id[ends[1]], "geom": unary_union([pieces[i]["geom"] for i in idx]),
                               "length": sum(float(pieces[i]["geom"].length) for i in idx), "idx": idx})
    singles = [{"feature": p["feature"], "u": final_id[p["u"]], "v": final_id[p["v"]], "geom": p["geom"], "length": float(p["geom"].length), "idx": [i]}
               for i, p in enumerate(pieces)]
    for c in candidates + singles:
        u, v = c["u"], c["v"]
        if u == v or u not in base_ids or v not in base_ids or u not in G or v not in G:
            continue
        if any(i in d["idx"] for d in out for i in c["idx"]):          # already matched (as a whole line, or as one of its pieces)
            continue
        new_len, p = c["length"], c
        ed = None
        for opt in sorted(direct.get(frozenset((u, v)), []), key=lambda e: abs(e["length"] - new_len)):      # a road straight between the two nodes
            if (1 - ratio) * new_len <= opt["length"] <= (1 + ratio) * new_len:
                ed = [opt]
                break
        if ed is None:                                                                                     # else the chain of roads between them
            try:
                path = nx.shortest_path(G, u, v, weight="length")
            except nx.NetworkXNoPath:
                continue
            if len(path) - 1 > max_edges:
                continue
            ed = [G[a][b] for a, b in zip(path, path[1:])]
        old_len = sum(e["length"] for e in ed)
        if not (1 - ratio) * new_len <= old_len <= (1 + ratio) * new_len:
            continue
        chain = unary_union([und.loc[e["key"], "geometry"] for e in ed])
        if chain.hausdorff_distance(p["geom"]) > max_apart_m:
            continue
        refs = [e["ref"] for e in ed if e["ref"] not in keep and e["ref"] not in claimed]
        claimed.update(refs)
        out.append({"refs": refs, "all_refs": [e["ref"] for e in ed], "idx": c["idx"], "feature": p["feature"], "new_length": new_len, "old_length": old_len,
                    "geoms": [und.loc[e["key"], "geometry"] for e in ed], "kept": [e["ref"] for e in ed if e["ref"] in keep]})
    return out


def _split_edge(edges: gpd.GeoDataFrame, ukey: tuple, point: Point, new_id: int, crs) -> tuple[gpd.GeoDataFrame, tuple | None]:
    """Split one road (both directed rows) at the point of it nearest to ``point``.

    Args:
        edges: Edge table.
        ukey: ``(u, v, key)`` of one directed row of the road.
        point: Projected point near the road.
        new_id: Node id of the new node.
        crs: Network CRS.

    Returns:
        ``(edges, info)`` with ``info = (px, py, [(old, new1, new2), ...])`` or ``(edges, None)`` when the road cannot be split.
    """
    u, v, k = ukey
    if k >= SYNTHETIC_KEY or ukey not in edges.index:
        return edges, None
    g = edges.loc[ukey, "geometry"]
    d = g.project(point)
    if d < 0.5 or d > g.length - 0.5:
        return edges, None
    pt = g.interpolate(d)
    first, second = substring(g, 0, d), substring(g, d, g.length)
    rows, mapping = [], []
    for (a, b, key) in ((u, v, k), (v, u, k)):
        if (a, b, key) not in edges.index:
            continue
        r = edges.loc[(a, b, key)]
        geom = r["geometry"]
        if (a, b) == (u, v):
            p1, p2 = first, second
        else:
            p1, p2 = LineString(list(second.coords)[::-1]), LineString(list(first.coords)[::-1])
        for (x, y), geo in (((a, new_id), p1), ((new_id, b), p2)):
            nr = r.copy()
            nr["geometry"], nr["length"], nr["is_main_direction"], nr["nearest_main_eid"] = geo, geo.length, False, ""
            rows.append(((x, y, key), nr))
        mapping.append(((a, b, key), (a, new_id, key), (new_id, b, key)))
    out = edges.drop(index=[m[0] for m in mapping])
    add = gpd.GeoDataFrame([r for _, r in rows], index=pd.MultiIndex.from_tuples([i for i, _ in rows], names=["u", "v", "key"]), crs=crs)
    return pd.concat([out, add]), (pt.x, pt.y, mapping)


def _rewrite_edge_edits(edits: pd.DataFrame, mapping: list) -> tuple[pd.DataFrame, int]:
    """Point the community edge edits of a split road at the two new pieces.

    Args:
        edits: Edge-edit table (``u, v, key, ...``).
        mapping: ``(old, piece1, piece2)`` triples of ``(u, v, key)``.

    Returns:
        ``(edits, dropped_geometry_edits)``: geometry edits of a split road cannot be kept and lose their coordinates.
    """
    lost = 0
    rows = []
    old_map = {m[0]: m for m in mapping}
    for _, r in edits.iterrows():
        key = (int(r["u"]), int(r["v"]), int(r["key"]))
        if key in old_map:
            for piece in old_map[key][1:]:
                nr = r.copy()
                nr["u"], nr["v"], nr["key"] = str(piece[0]), str(piece[1]), str(piece[2])
                if str(r.get("geom_edited")) == "True" or str(r.get("is_new")) == "True":
                    nr["coords"], nr["geom_edited"], nr["is_new"] = "", "False", "False"
                    lost += 1
                rows.append(nr)
        else:
            rows.append(r)
    return pd.DataFrame(rows, columns=edits.columns), lost


def integrate_roads(nodes: gpd.GeoDataFrame, edges: gpd.GeoDataFrame, streets: pd.DataFrame, edge_edits: pd.DataFrame | None,
                    features: list[dict], connections: dict | None = None, auto_m: float = 10.0, suggest_m: float = 25.0,
                    node_tol_m: float = 1.0, first_street_id: int | None = None, touch_m: float = 10.0, keep: set | None = None) -> RoadsResult:
    """Add drawn roads to the network (see the module docstring).

    Args:
        nodes: Base nodes, indexed by node id, projected, with ``x``, ``y``, ``node_id``.
        edges: Base edges indexed by ``(u, v, key)``.
        streets: Base streets table.
        edge_edits: Community edge edits, or None.
        features: Drawn lines; every feature needs ``key`` (``"<import>:<id>"``), ``coords``, ``name``, ``highway``.
        connections: Connection choices by node key: ``{"type": "node", "node": id}``, ``{"type": "edge", "u", "v", "key", "lat", "lon"}``
            or ``{"type": "none"}`` (do not connect).
        auto_m: New nodes within this distance of an existing node are connected to it automatically.
        suggest_m: Search radius for the suggestions reported for nodes that stay unconnected.
        node_tol_m: Tolerance for generating the nodes of the drawn lines.
        first_street_id: First street id given to the new streets (default: above every existing id).
        touch_m: A new end lying on an existing road within this distance is connected to it (the road is split).
        keep: References of existing roads that must stay although a new road duplicates them (default: none, the new road wins).

    Returns:
        The :class:`RoadsResult`.
    """
    connections = connections or {}
    crs = edges.crs
    tr_in = Transformer.from_crs(4326, crs, always_xy=True)
    tr_out = Transformer.from_crs(crs, 4326, always_xy=True)
    noded = node_lines(features, crs, node_tol_m)
    connections = dict(connections)
    for f in features:                                     # a road drawn by hand knows how its two ends were snapped
        for end, tgt in ((0, f.get("start")), (-1, f.get("end"))):
            if not tgt or not f.get("coords"):
                continue
            ex, ey = tr_in.transform(*f["coords"][end])
            near = min(noded.nodes, key=lambda kk: math.hypot(noded.nodes[kk][0] - ex, noded.nodes[kk][1] - ey))
            if math.hypot(noded.nodes[near][0] - ex, noded.nodes[near][1] - ey) <= 2.0:
                connections.setdefault(near, tgt)
    nodes = nodes.copy()
    edges = edges.copy()
    base_ids = np.array(nodes.index)
    base_xy = np.c_[nodes["x"].to_numpy(), nodes["y"].to_numpy()]
    next_node = int(max(base_ids.max(), 0)) + 1
    max_sid = int(streets["street_id"].max())
    if edge_edits is not None and len(edge_edits) and "new_street_id" in edge_edits:
        max_sid = max(max_sid, int(pd.to_numeric(edge_edits["new_street_id"], errors="coerce").max() or 0))
    sid0 = first_street_id if first_street_id is not None else max_sid + 1

    final_id: dict = {}
    final_xy: dict = {}
    status: dict = {}
    lost_geom = 0
    split_count = 0
    ancestors: dict = {}                 # original (u, v, key) -> current piece keys (a road may be split more than once)

    def nearest_node(x: float, y: float):
        """Nearest base node as ``(id, distance, x, y)``."""
        d = np.hypot(base_xy[:, 0] - x, base_xy[:, 1] - y)
        i = int(np.argmin(d))
        return base_ids[i], float(d[i]), base_xy[i, 0], base_xy[i, 1]

    existing = {"edges": edges}                            # the roads that were there before the drawn ones were added

    def nearest_edge(x: float, y: float):
        """Nearest existing road (both directions share a geometry) as ``(key, distance)``."""
        table = existing["edges"]
        undirected = table[[u < v for u, v, _ in table.index]]
        best = None
        for idx, g in zip(undirected.index, undirected.geometry):
            dd = g.distance(Point(x, y))
            if best is None or dd < best[1]:
                best = (idx, dd)
        return best

    keys = sorted(noded.nodes)
    for kk in keys:                                        # 1. nodes: manual choice first, then automatic
        x, y = noded.nodes[kk]
        c = connections.get(kk)
        if c and c.get("type") == "none":
            status[kk] = {"state": "free", "manual": True}
        elif c and c.get("type") == "node" and int(c["node"]) in set(base_ids):
            nid = int(c["node"])
            final_id[kk], final_xy[kk] = nid, (float(nodes.loc[nid, "x"]), float(nodes.loc[nid, "y"]))
            status[kk] = {"state": "connected", "to": "node", "node": nid, "manual": True}
        elif c and c.get("type") == "edge":
            status[kk] = {"state": "pending_edge", "target": c}
        else:
            nid, dist, nx_, ny_ = nearest_node(x, y)
            ed = nearest_edge(x, y) if dist > auto_m else None
            if dist <= auto_m:
                final_id[kk], final_xy[kk] = int(nid), (float(nx_), float(ny_))
                status[kk] = {"state": "connected", "to": "node", "node": int(nid), "manual": False, "distance_m": dist}
            elif ed is not None and ed[1] <= touch_m:
                q = shapely.ops.nearest_points(edges.loc[ed[0], "geometry"], Point(x, y))[0]
                qlon, qlat = tr_out.transform(q.x, q.y)
                status[kk] = {"state": "pending_edge", "manual": False, "target": {"type": "edge", "u": int(ed[0][0]), "v": int(ed[0][1]),
                                                                                    "key": int(ed[0][2]), "lat": qlat, "lon": qlon}}
            else:
                status[kk] = {"state": "free", "manual": False}
    for kk in keys:                                        # 2. manual road connections: split the road
        if status[kk]["state"] != "pending_edge":
            continue
        c = status[kk]["target"]
        px, py = tr_in.transform(float(c["lon"]), float(c["lat"]))
        orig = (int(c["u"]), int(c["v"]), int(c["key"]))
        pieces = [p for p in ancestors.get(orig, [orig]) if p in edges.index or (p[1], p[0], p[2]) in edges.index]
        best, bd = None, None
        for p in pieces:
            key = p if p in edges.index else (p[1], p[0], p[2])
            dd = edges.loc[key, "geometry"].distance(Point(px, py))
            if bd is None or dd < bd:
                best, bd = key, dd
        if best is None:
            status[kk] = {"state": "free", "manual": True, "error": "that road no longer exists"}
            continue
        nid = next_node
        edges, info = _split_edge(edges, best, Point(px, py), nid, crs)
        if info is None:                                   # too close to an end: use that end node instead
            node, dist, nx_, ny_ = nearest_node(px, py)
            final_id[kk], final_xy[kk] = int(node), (float(nx_), float(ny_))
            status[kk] = {"state": "connected", "to": "node", "node": int(node), "manual": True}
            continue
        next_node += 1
        split_count += 1
        sx, sy, mapping = info
        nodes.loc[nid, ["x", "y", "node_id"]] = [sx, sy, nid]
        nodes.loc[nid, "geometry"] = Point(sx, sy)
        base_ids = np.append(base_ids, nid)
        base_xy = np.vstack([base_xy, [sx, sy]])
        ancestors[orig] = [m for old, a, b in mapping for m in (a, b)] + [p for p in ancestors.get(orig, []) if p not in {m[0] for m in mapping}]
        if edge_edits is not None and len(edge_edits):
            edge_edits, lost = _rewrite_edge_edits(edge_edits, mapping)
            lost_geom += lost
        final_id[kk], final_xy[kk] = nid, (sx, sy)
        status[kk] = {"state": "connected", "to": "edge", "node": nid, "manual": bool(connections.get(kk)), "edge": list(orig)}
    for kk in keys:                                        # 3. nodes that stay free get new ids
        if kk not in final_id:
            final_id[kk], final_xy[kk] = next_node, noded.nodes[kk]
            nodes.loc[next_node, ["x", "y", "node_id"]] = [noded.nodes[kk][0], noded.nodes[kk][1], next_node]
            nodes.loc[next_node, "geometry"] = Point(*noded.nodes[kk])
            next_node += 1
    nodes["node_id"] = nodes["node_id"].astype("int64")

    dups = _find_duplicates(edges, noded.pieces, final_id, {int(i) for i in base_ids}, crs, set(keep or ()))   # 3b. the new road wins over duplicates
    gone_refs = {r for d in dups for r in d["refs"]}
    if gone_refs:
        edges, streets = drop_edges(edges, streets, gone_refs)
    replaced = [{"refs": d["all_refs"], "removed": d["refs"], "kept": d["kept"], "feature": d["feature"], "new_length": round(d["new_length"], 1),
                 "old_length": round(d["old_length"], 1),
                 "geometry": shapely.geometry.mapping(shapely.set_precision(gpd.GeoSeries([unary_union(d["geoms"])], crs=crs).to_crs(4326).iloc[0], 1e-6))}
                for d in dups]
    feats = sorted({p["feature"] for p in noded.pieces})   # 4. streets and edges
    meta_of = {f["key"]: f for f in features}
    known = {int(x) for x in streets["street_id"]}
    join_of = {f: int(meta_of[f]["join"]) for f in feats if meta_of[f].get("join") is not None and int(meta_of[f]["join"]) in known}   # prolongs an existing street
    sid_of, n_new = {}, 0
    for f in feats:
        if f in join_of:
            sid_of[f] = join_of[f]
        else:
            sid_of[f], n_new = sid0 + n_new, n_new + 1
    srow = streets.set_index(streets["street_id"].astype(int))
    rows, new_lines, lens = [], [], {f: 0.0 for f in feats}
    taken = set(edges.index)
    for p in noded.pieces:
        u, v = final_id[p["u"]], final_id[p["v"]]
        coords = list(p["geom"].coords)
        coords[0], coords[-1] = final_xy[p["u"]], final_xy[p["v"]]            # the new edge is moved onto the node it connects to
        coords = [c for i, c in enumerate(coords) if i == 0 or c != coords[i - 1]]
        if u == v or len(coords) < 2:
            continue
        g = LineString(coords)
        if g.length < 0.5:
            continue
        k = 0
        while (u, v, k) in taken or (v, u, k) in taken:
            k += 1
        taken.update({(u, v, k), (v, u, k)})
        joined = join_of.get(p["feature"])
        mrank = meta_of[p["feature"]].get("rank")
        hw = p["highway"] or (RANK_HW.get(int(mrank)) if mrank is not None else None) or meta_of[p["feature"]].get("highway") or (srow.loc[joined, "highway"] if joined is not None and "highway" in srow.columns else None) or HIGHWAY_DEFAULT
        nm = p["name"] or (str(srow.loc[joined, "street_name"]) if joined is not None and isinstance(srow.loc[joined, "street_name"], str) else "")
        lens[p["feature"]] += g.length
        for (a, b, geo, rev) in ((u, v, g, "False"), (v, u, LineString(coords[::-1]), "True")):
            rows.append(((a, b, k), {"osmid": f"road{p['feature']}", "highway": hw, "name": nm, "oneway": "False", "ref": "", "reversed": rev,
                                     "length": geo.length, "width": "", "service": "", "bridge": "", "is_drivable": True, "structure_type": "", "paved": meta_of[p["feature"]].get("paved"), "drivable_info": meta_of[p["feature"]].get("drivable"),
                                     "street_id": sid_of[p["feature"]], "street_name": nm, "is_main_direction": False, "nearest_main_eid": "",
                                     "street_start_length_m": np.nan, "street_end_length_m": np.nan, "house_number_start_left": "",
                                     "house_number_start_right": "", "house_number_end_left": "", "house_number_end_right": "", "geometry": geo}))
        new_lines.append({"street_id": sid_of[p["feature"]], "u": u, "v": v, "geometry": g, "u_state": status[p["u"]]["state"],
                          "v_state": status[p["v"]]["state"], "feature": p["feature"], "name": nm or None})
    existing_edges = edges.copy()                          # after the splits, before the new edges
    if rows:
        add = gpd.GeoDataFrame([r for _, r in rows], index=pd.MultiIndex.from_tuples([i for i, _ in rows], names=["u", "v", "key"]), crs=crs)
        for col in edges.columns:
            if col not in add.columns:
                add[col] = None
        edges = pd.concat([edges, add[edges.columns]])
    admin = {c: streets[c].iloc[0] for c in ("country", "province", "city") if c in streets.columns}
    srows = []
    for f in feats:
        if f in join_of:
            continue                                       # the street already exists: the new road is part of it
        m = meta_of[f]
        hw = m.get("highway") or HIGHWAY_DEFAULT
        srows.append({"street_id": sid_of[f], "street_name": m.get("name") or np.nan, "street_rank": highway_rank(hw), "highway": hw,
                      "osmids": f"road{f}", "road_type": np.nan, "length_m": lens[f], **admin})
    streets = pd.concat([streets, pd.DataFrame(srows)], ignore_index=True) if srows else streets

    G = nx.Graph()                                         # 5. report: which new nodes dangle, which roads are isolated
    base_nodes_set = set(int(i) for i in nodes.index) - set(final_id[k] for k in keys if status[k]["state"] != "connected")
    for (a, b, _k) in edges.index:
        G.add_edge(int(a), int(b))
    comps = {n: i for i, comp in enumerate(nx.connected_components(G)) for n in comp}
    base_comp = {comps[int(n)] for n in set(nodes.index) if int(n) in comps and int(n) not in {final_id[k] for k in keys if status[k]["state"] == "free"}}
    isolated = sorted({p["feature"] for p in noded.pieces
                       if comps.get(final_id[p["u"]]) not in base_comp or comps.get(final_id[p["v"]]) not in base_comp})
    node_rep = []
    for kk in keys:
        x, y = noded.nodes[kk]
        lon, lat = tr_out.transform(*final_xy[kk])
        st = status[kk]
        rec = {"key": kk, "lat": lat, "lon": lon, "degree": noded.degree.get(kk, 0), **st,
               "features": sorted({p["feature"] for p in noded.pieces if kk in (p["u"], p["v"])})}
        rec.pop("target", None)
        if st["state"] == "free" and noded.degree.get(kk, 0) == 1:
            nid, dist, nx_, ny_ = nearest_node(x, y)
            existing["edges"] = existing_edges
            ed = nearest_edge(x, y)
            sug = None
            if dist <= suggest_m and (ed is None or dist <= ed[1]):
                sug = {"type": "node", "node": int(nid), "distance_m": round(dist, 1)}
            elif ed is not None and ed[1] <= suggest_m:
                u_, v_, k_ = ed[0]
                q = shapely.ops.nearest_points(existing_edges.loc[ed[0], "geometry"], Point(x, y))[0]
                qlon, qlat = tr_out.transform(q.x, q.y)
                sug = {"type": "edge", "u": int(u_), "v": int(v_), "key": int(k_), "lat": qlat, "lon": qlon, "distance_m": round(ed[1], 1)}
            rec["suggestion"] = sug
        node_rep.append(rec)
    dangling = [r["key"] for r in node_rep if r["state"] in ("free", "pending_edge") and r["degree"] == 1]
    report = {"features": len(feats), "pieces": len(noded.pieces), "nodes": node_rep, "dangling": dangling, "isolated": isolated,
              "street_ids": sid_of, "split_edges": split_count, "auto_connected": sum(1 for r in node_rep if r["state"] == "connected" and not r.get("manual")),
              "geometry_edits_lost": lost_geom, "replaced": replaced}
    new_edges = gpd.GeoDataFrame(new_lines, geometry="geometry", crs=crs).to_crs(4326) if new_lines else gpd.GeoDataFrame(
        {"street_id": [], "geometry": []}, geometry="geometry", crs=4326)
    return RoadsResult(nodes, edges, streets, edge_edits, report, new_edges)


def snap_click(nodes: gpd.GeoDataFrame, edges: gpd.GeoDataFrame, lat: float, lon: float, node_m: float = 10.0, max_m: float = 60.0) -> dict | None:
    """Turn a click into a connection target: an existing node, else a point of an existing road.

    Args:
        nodes: Network nodes (projected, with ``x``/``y``).
        edges: Network edges.
        lat: Latitude of the click.
        lon: Longitude of the click.
        node_m: A node within this distance of the click wins.
        max_m: Largest distance from the click to a road.

    Returns:
        ``{"type": "node", "node", "lat", "lon", "distance_m"}`` or ``{"type": "edge", "u", "v", "key", "lat", "lon", "distance_m"}``;
        None when nothing is within ``max_m``.
    """
    crs = edges.crs
    x, y = Transformer.from_crs(4326, crs, always_xy=True).transform(lon, lat)
    back = Transformer.from_crs(crs, 4326, always_xy=True)
    d = np.hypot(nodes["x"].to_numpy() - x, nodes["y"].to_numpy() - y)
    i = int(np.argmin(d))
    if d[i] <= node_m:
        nlon, nlat = back.transform(float(nodes["x"].iloc[i]), float(nodes["y"].iloc[i]))
        return {"type": "node", "node": int(nodes.index[i]), "lat": nlat, "lon": nlon, "distance_m": float(d[i])}
    p = Point(x, y)
    best = None
    for idx, g in zip(edges.index, edges.geometry):
        if idx[0] < idx[1] and idx[2] < SYNTHETIC_KEY:
            dd = g.distance(p)
            if best is None or dd < best[1]:
                best = (idx, dd)
    if best is None or best[1] > max_m:
        return None
    q = shapely.ops.nearest_points(edges.loc[best[0], "geometry"], p)[0]
    qlon, qlat = back.transform(q.x, q.y)
    return {"type": "edge", "u": int(best[0][0]), "v": int(best[0][1]), "key": int(best[0][2]), "lat": qlat, "lon": qlon, "distance_m": float(best[1])}
