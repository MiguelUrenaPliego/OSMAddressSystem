"""Downloadable tables of the computed network: streets, edges, nodes, buildings, street signs and an OSM file with the changes.

Every geometry is in WGS84 (EPSG:4326). Flags that say how a street differs from the OpenStreetMap download use these words:

* **osm** - the geometry is exactly the one that was downloaded (every edge of the street was in OSM and was not changed);
* **geometry_modified** - the street is made of OSM roads whose geometry changed (a road split to connect a new road, a community edit);
* **new** - the street, or edge, does not exist in OSM (drawn by the community and added in the Edit streets tab).

Names: ``osm_name`` is True when OSM has a name for the street, ``modified_osm_name`` when the community gave it another name,
``derived_name`` when the name is derived from a main street (``3-xyz``), ``derived_rank`` is 0 for an own name, 1 for ``3-xyz``,
2 for ``5-7-xyz`` ... and empty while the street has no name.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from xml.sax.saxutils import quoteattr

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import linemerge

from . import engine, roads as roads_mod
from ..branches import is_named
from ..path_numbering import highway_rank

GEOM_TOL_M = 0.5
"""A geometry closer than this (Hausdorff distance, metres) to the downloaded one counts as unchanged."""


def _text(v) -> str:
    """A cell as plain text.

    Args:
        v: Any value.

    Returns:
        ``""`` for missing values, else the string.
    """
    return v if isinstance(v, str) else ""


def _osm_names(base) -> tuple[dict, dict]:
    """OSM names of the downloaded roads.

    Args:
        base: Base data (the downloaded network, before any editor change).

    Returns:
        ``(by osm way id, by edge)``: the name tag of every way and of every ``(u, v, key)``.
    """
    by_way: dict = {}
    by_edge: dict = {}
    for (u, v, k), nm, oid in zip(base.edges.index, base.edges["name"], base.edges["osmid"]):
        if isinstance(nm, str) and nm.strip():
            by_edge[(u, v, k)] = nm.strip()
            for w in re.findall(r"\d+", str(oid)):
                by_way.setdefault(w, Counter())[nm.strip()] += 1
    return {w: c.most_common(1)[0][0] for w, c in by_way.items()}, by_edge


def edge_table(state, base) -> gpd.GeoDataFrame:
    """One row per real edge (a road between two nodes) of the computed network, with the flags against the OSM download.

    Args:
        state: Computed network (``engine.NetworkState``).
        base: The downloaded network (``engine.BaseData`` before the editors' changes).

    Returns:
        WGS84 lines with ``street_id``, ``street_id_original``, ``u``, ``v``, ``key``, ``osmid``, ``highway``, ``drivable``, ``osm``,
        ``geometry_modified``, ``new``, ``geom_edited``, ``street_dist_u_m`` / ``street_dist_v_m`` (distance along the street from the start of the street - its end nearest the
        reference - to the first node ``u`` and to the last node ``v`` of the edge; ``u`` is always the node nearer the start, so the numbering rises
        from ``u`` to ``v``), ``dist_u_m`` / ``dist_v_m`` (distance along the road network to the reference point of ``u`` and ``v``; it says
        nothing about the numbering), ``length_m`` (street columns are added by :func:`street_table`).
    """
    e = state.edges.reset_index()
    e = e[e["key"] < roads_mod.SYNTHETIC_KEY]
    e = e.assign(_k=[(min(u, v), max(u, v), k) for u, v, k in zip(e["u"], e["v"], e["key"])]).drop_duplicates("_k")
    orig = base.edges
    osm, mod, new = [], [], []
    for u, v, k, g, oid in zip(e["u"], e["v"], e["key"], e["geometry"], e["osmid"]):
        is_new = str(oid).startswith("road")
        same = False
        for idx in ((u, v, k), (v, u, k)):
            if idx in orig.index:
                same = orig.loc[idx, "geometry"].hausdorff_distance(g) <= GEOM_TOL_M
                break
        osm.append(bool(same) and not is_new)
        new.append(is_new)
        mod.append(not same and not is_new)
    dist = state.distance
    pu = pd.to_numeric(e["street_pos_u_m"], errors="coerce").to_numpy(float) if "street_pos_u_m" in e else np.full(len(e), np.nan)
    pv = pd.to_numeric(e["street_pos_v_m"], errors="coerce").to_numpy(float) if "street_pos_v_m" in e else np.full(len(e), np.nan)
    flip = np.nan_to_num(pu) > np.nan_to_num(pv)                  # the first node is the one nearer to the start of the street
    nu = np.where(flip, e["v"].to_numpy(), e["u"].to_numpy()).astype("int64")
    nv = np.where(flip, e["u"].to_numpy(), e["v"].to_numpy()).astype("int64")
    su, sv = np.where(flip, pv, pu), np.where(flip, pu, pv)
    geoms = [LineString(list(g.coords)[::-1]) if f else g for g, f in zip(e["geometry"], flip)]
    d_u = [None if n not in dist.index or pd.isna(dist.get(n)) else round(float(dist.get(n)), 1) for n in nu]
    d_v = [None if n not in dist.index or pd.isna(dist.get(n)) else round(float(dist.get(n)), 1) for n in nv]
    out = gpd.GeoDataFrame({"street_id": e["street_id"].astype("Int64"), "street_id_original": e["street_id_original"].astype("Int64"),
                            "street_part": e["street_part"].astype("Int64"), "u": nu, "v": nv,
                            "street_dist_u_m": np.round(su, 1), "street_dist_v_m": np.round(sv, 1),
                            "key": e["key"].astype("int64"), "osmid": e["osmid"].astype(str), "highway": e["highway"],
                            "drivable": e["drivable_info"].astype("boolean") if "drivable_info" in e else e["is_drivable"].astype("boolean"),
                            "paved": e["paved"].astype("boolean") if "paved" in e else pd.array([None] * len(e), dtype="boolean"), "osm": osm, "geometry_modified": mod, "new": new,
                            "geom_edited": e["geom_edited"].astype(bool) if "geom_edited" in e else False,
                            "dist_u_m": d_u, "dist_v_m": d_v, "length_m": [round(g.length, 2) for g in geoms]}, geometry=geoms, crs=state.edges.crs)
    return out.to_crs(4326)


def street_table(state, project, base, edges: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """One row per street id with its flags, name and geometry.

    Args:
        state: Computed network.
        project: The project (to tell community names from OSM names).
        base: The downloaded network.
        edges: Output of :func:`edge_table`.

    Returns:
        WGS84 lines with ``street_id``, ``original_id``, ``part``, ``rank``, ``osm``, ``geometry_modified``, ``new``, ``drivable``,
        ``osm_name``, ``modified_osm_name``, ``derived_name``, ``derived_rank``, ``name_source``, ``street_name``, ``own_name``, ``osm_name_text``.
    """
    way_name, edge_name = _osm_names(base)
    status = engine.street_status(state, project)
    sg = state.streets_gdf.to_crs(4326).set_index("street_id")
    by_street = {sid: g for sid, g in edges.groupby("street_id")}
    rows = []
    for sid, r in sg.iterrows():
        sid = int(sid)
        eg = by_street.get(sid)
        n_edges = 0 if eg is None else len(eg)
        osm_txt = ""
        if eg is not None:
            names = Counter()
            for u, v, k, oid, nw in zip(eg["u"], eg["v"], eg["key"], eg["osmid"], eg["new"]):
                if nw:
                    continue
                nm = edge_name.get((u, v, k)) or edge_name.get((v, u, k)) or next((way_name[w] for w in re.findall(r"\d+", oid) if w in way_name), "")
                if nm:
                    names[nm] += 1
            osm_txt = names.most_common(1)[0][0] if names else ""
        own = r["street_name"] if is_named(r["street_name"]) else ""
        if own and int(r["street_part"]) > 0:
            own = re.sub(r" \([A-Z]+\)$", "", own)                   # the part letter is not part of the street's name
        st = status.loc[sid]
        derived = st["status"] == "derived"
        geom = r.geometry
        if geom.geom_type == "MultiLineString":
            merged = linemerge(geom)
            geom = merged if merged.geom_type == "LineString" else geom
        rows.append({"street_id": sid, "original_id": int(r["street_id_original"]), "part": int(r["street_part"]), "rank": int(r["street_rank"]),
                     "osm": bool(n_edges) and bool(eg["osm"].all()), "geometry_modified": bool(n_edges) and bool(eg["geometry_modified"].any()),
                     "new": bool(n_edges) and bool(eg["new"].all()), "drivable": engine._tri(eg["drivable"], eg["length_m"]) if n_edges else None,
                     "paved": engine._tri(eg["paved"], eg["length_m"]) if n_edges else None,
                     "osm_name": bool(osm_txt), "modified_osm_name": bool(osm_txt) and bool(own) and own != osm_txt,
                     "derived_name": derived,
                     "derived_rank": 0 if st["status"] == "named" else (int(st["level"]) if derived and pd.notna(st["level"]) else None),
                     "name_source": ("derived" if derived else "none" if not own else "osm" if own == osm_txt else "community"),
                     "street_name": _text(r["display_name"]) if not _text(r["display_name"]).startswith("#") else "",
                     "own_name": own, "osm_name_text": osm_txt, "geometry": geom})
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)
    out["derived_rank"] = out["derived_rank"].astype("Int64")
    for c in ("drivable", "paved"):
        out[c] = out[c].astype("boolean")                                  # True / False / empty when OSM does not say
    return out


def with_street_columns(edges: gpd.GeoDataFrame, streets: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add the street-level columns (rank, names, flags) to every edge row.

    Args:
        edges: Output of :func:`edge_table`.
        streets: Output of :func:`street_table`.

    Returns:
        The edge table with ``rank``, ``osm_name``, ``modified_osm_name``, ``derived_name``, ``derived_rank``, ``name_source``,
        ``street_name`` (one row per edge, so several rows per street id).
    """
    cols = ["rank", "osm_name", "modified_osm_name", "derived_name", "derived_rank", "name_source", "street_name"]
    s = streets.set_index("street_id")[cols]
    out = edges.join(s, on="street_id")
    front = ["street_id", "rank", "u", "v", "street_dist_u_m", "street_dist_v_m", "dist_u_m", "dist_v_m", "key", "osm", "geometry_modified", "new", "drivable", "paved"] + cols[1:]
    return out[front + [c for c in out.columns if c not in front]]


def node_table(state, base, edges: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """The nodes of the computed network (real edges only).

    Args:
        state: Computed network.
        base: The downloaded network.
        edges: Output of :func:`edge_table`.

    Returns:
        WGS84 points with ``node_id`` (the OSM node id for downloaded nodes), ``x`` (lon), ``y`` (lat), ``osm`` (the node is in the
        download at the same place), ``streets`` (street ids that meet there, ``;``-joined), ``degree``, ``distance_m`` to the reference.
    """
    sid_at: dict = {}
    deg: Counter = Counter()
    for u, v, sid in zip(edges["u"], edges["v"], edges["street_id"]):
        for n in (u, v):
            deg[n] += 1
            if pd.notna(sid):
                sid_at.setdefault(n, set()).add(int(sid))
    nodes = state.nodes.loc[[n for n in deg if n in state.nodes.index]]
    inb = base.nodes
    same = [n in inb.index and Point(inb.loc[n, "x"], inb.loc[n, "y"]).distance(Point(r.x, r.y)) <= GEOM_TOL_M for n, r in zip(nodes.index, nodes.itertuples())]
    pts = nodes.to_crs(4326)
    dist = state.distance
    return gpd.GeoDataFrame({"node_id": nodes.index.astype("int64"), "x": pts.geometry.x.round(7), "y": pts.geometry.y.round(7), "osm": same,
                             "streets": [";".join(str(s) for s in sorted(sid_at.get(n, ()))) for n in nodes.index], "degree": [deg[n] for n in nodes.index],
                             "distance_m": [None if n not in dist.index or pd.isna(dist.get(n)) else round(float(dist.get(n)), 1) for n in nodes.index]},
                            geometry=pts.geometry.to_numpy(), crs=4326)


def reference_table(state, project) -> gpd.GeoDataFrame:
    """The reference point the distances and the numbering start from.

    Args:
        state: Computed network.
        project: The project (reference name and who set it).

    Returns:
        One WGS84 point with ``node_id``, ``x`` (lon), ``y`` (lat), ``name`` and ``set_by`` (``default`` or ``editor``).
    """
    r = project.reference or {}
    nid = int(r.get("node", state.reference["node"]))
    g = state.nodes.loc[[nid]].to_crs(4326).geometry.iloc[0]
    return gpd.GeoDataFrame({"node_id": [nid], "x": [round(g.x, 7)], "y": [round(g.y, 7)], "name": [r.get("name") or "reference point"],
                             "set_by": ["default" if r.get("by") == "default" else "editor"]}, geometry=[g], crs=4326)


def buildings_table(addresses, points: gpd.GeoDataFrame | None = None) -> gpd.GeoDataFrame:
    """All buildings with their assigned address, use, residents and services, plus the important points of the community.

    The important points (squares, intersections, facilities, OSM places ...) come last with ``use = "reference"`` and an ``info`` text
    that says what kind of point they are; they have no address.

    Args:
        addresses: Computed addresses (``engine.AddressState``).
        points: The important points of the project (``BaseData.points``) or None.

    Returns:
        WGS84 points with ``building_id``, ``x`` (lon), ``y`` (lat), ``use`` (residential, office, commercial, industrial, public,
        unknown, reference), ``residents``, ``name``, ``service``, ``workers``, ``info``, ``address``, ``number``, ``street_id``,
        ``street``, ``side``, ``side_source`` (``azimuth`` = from the direction the surveyor pointed, else ``location``), ``status``, ``source`` (``auto`` / ``manual`` road point).
    """
    h = addresses.houses
    pts = h.geometry.apply(lambda g: g if g.geom_type == "Point" else g.representative_point())
    cols = {"building_id": h["building_id"], "x": pts.x.round(7), "y": pts.y.round(7)}
    for c, default in (("use", "unknown"), ("residents", None), ("name", ""), ("service", ""), ("workers", None)):
        cols[c] = h[c] if c in h else default
    cols["info"] = (h["info"].fillna("") if "info" in h else "") if "info" in h else (h["review"].fillna("") if "review" in h else "")
    if "info" in h and "review" in h:
        cols["info"] = h["info"].fillna("").where(h["info"].fillna("") != "", h["review"].fillna(""))
    for c in ("address", "number", "street_id", "street", "side", "side_source", "status", "source"):
        cols[c] = h[c]
    out = gpd.GeoDataFrame(cols, geometry=pts.to_numpy(), crs=4326)
    done = set(h["building_id"])                                  # points inside the area were addressed with the buildings
    if points is not None and len(points):
        p = points.to_crs(4326)
        pp = p.geometry.apply(lambda g: g if g.geom_type == "Point" else g.representative_point())
        info = []
        for _, r in p.iterrows():
            bits = [_text(r.get("kind")), f"settlement {r['settlement']}" if _text(r.get("settlement")) else "", f"source {r['source']}" if _text(r.get("source")) else ""]
            info.append("; ".join(b for b in bits if b))
        ids = [f"POINT-{i + 1}" for i in range(len(p))]
        keep = [i not in done for i in ids]
        p, pp, info, ids = p[keep], pp[keep], [x for x, k in zip(info, keep) if k], [x for x, k in zip(ids, keep) if k]
        ref = gpd.GeoDataFrame({"building_id": ids, "x": pp.x.round(7), "y": pp.y.round(7), "use": "reference",
                                "name": [_text(n) for n in p.get("name", [""] * len(p))], "service": "", "info": info}, geometry=pp.to_numpy(), crs=4326)
        out = pd.concat([out, ref], ignore_index=True)
    for c in ("residents", "workers"):
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("Int64")
    return gpd.GeoDataFrame(out, geometry="geometry", crs=4326)


def signs_table(addresses, state) -> gpd.GeoDataFrame:
    """Every sign board: one row per street on a sign at an intersection.

    Args:
        addresses: Computed addresses.
        state: Computed network (street ranks).

    Returns:
        WGS84 points with ``node_id``, ``rank`` (rank of the intersection), ``type`` (kind of intersection / sign; ``reference`` for a reference point on the sign), ``street_id``,
        ``street_rank``, ``poi_rank`` (rank of the reference point), ``distance_m`` (distance to a reference point outside the area along the larger road network), ``text`` (the text on the sign), ``x`` (lon), ``y`` (lat).
    """
    rank = state.streets.set_index("street_id")["street_rank"]
    rows = []
    for r in addresses.signs.itertuples():
        ids = [int(x) for x in str(getattr(r, "street_ids", "") or "").split(";") if x]
        for i, text in enumerate(r.labels):
            sid = ids[i] if i < len(ids) else None
            rows.append({"node_id": int(r.node_id), "rank": int(r.rank), "type": getattr(r, "type", None), "street_id": sid,
                         "street_rank": None if sid is None or sid not in rank.index else int(rank[sid]), "poi_rank": None, "distance_m": None, "text": text,
                         "x": round(r.geometry.x, 7), "y": round(r.geometry.y, 7), "geometry": r.geometry})
        for poi in (getattr(r, "pois", None) or []):                 # the reference points that are shown on this sign
            rows.append({"node_id": int(r.node_id), "rank": int(r.rank), "type": "reference", "street_id": None, "street_rank": None, "poi_rank": int(poi["rank"]), "distance_m": poi.get("distance_m"),
                         "text": poi["name"], "x": round(r.geometry.x, 7), "y": round(r.geometry.y, 7), "geometry": r.geometry})
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)
    return out


def to_csv(gdf: gpd.GeoDataFrame) -> str:
    """CSV text of a table with the geometry as WKT in the last column.

    Args:
        gdf: A table.

    Returns:
        CSV text.
    """
    df = pd.DataFrame(gdf.drop(columns="geometry"))
    df["geometry"] = [shapely.to_wkt(g, rounding_precision=7) for g in gdf.geometry]
    return df.to_csv(index=False)


# ------------------------------------------------------------------------------------------------------------------------- OSM file
def read_osm(path) -> tuple[dict, dict]:
    """The nodes and ways of an OSM XML file.

    Args:
        path: ``.osm`` file.

    Returns:
        ``(nodes, ways)``: ``{id: (lat, lon, version)}`` and ``{id: {"version", "refs", "tags"}}``.
    """
    nodes, ways = {}, {}
    for _ev, el in ET.iterparse(path, events=("end",)):
        if el.tag == "node":
            nodes[int(el.get("id"))] = (float(el.get("lat")), float(el.get("lon")), el.get("version") or "1")
        elif el.tag == "way":
            ways[int(el.get("id"))] = {"version": el.get("version") or "1", "refs": [int(n.get("ref")) for n in el.findall("nd")],
                                       "tags": {t.get("k"): t.get("v") for t in el.findall("tag")}}
        if el.tag in ("node", "way", "relation"):
            el.clear()
    return nodes, ways


def _paths(edges: list[tuple]) -> list[list[tuple]]:
    """Cut a set of edges into simple paths (they stop at junctions and dead ends).

    Args:
        edges: ``(u, v, coords)`` with ``coords`` from ``u`` to ``v``.

    Returns:
        Paths as lists of ``(from_node, to_node, coords)`` oriented along the path.
    """
    adj: dict = {}
    for i, (u, v, _c) in enumerate(edges):
        adj.setdefault(u, []).append(i)
        adj.setdefault(v, []).append(i)
    used, out = set(), []

    def walk(start_node, first):
        path, node, i = [], start_node, first
        while True:
            used.add(i)
            u, v, c = edges[i]
            path.append((node, v if u == node else u, c if u == node else c[::-1]))
            node = path[-1][1]
            nxt = [j for j in adj[node] if j not in used]
            if len(adj[node]) != 2 or not nxt:
                return path
            i = nxt[0]

    ends = [n for n, es in adj.items() if len(es) != 2]
    for n in ends + list(adj):
        for i in adj[n]:
            if i not in used:
                out.append(walk(n, i))
    return out


class OsmBuilder:
    """Collects the new nodes and the new / modified ways of an OSM change file."""

    def __init__(self, osm_nodes: dict, osm_ways: dict):
        """Index the downloaded nodes by position.

        Args:
            osm_nodes: Output of :func:`read_osm` (nodes).
            osm_ways: Output of :func:`read_osm` (ways).
        """
        self.nodes, self.ways = osm_nodes, osm_ways
        self.at = {(round(la, 6), round(lo, 6)): i for i, (la, lo, _v) in osm_nodes.items()}
        self.new_nodes: dict = {}
        self.next_id = -1
        self.used_existing: set = set()
        self.out_ways: list[dict] = []

    def ref(self, node_id: int | None, lon: float, lat: float) -> int:
        """The OSM id to use for a point: the downloaded node when the network node is one, else one at the same place, else a new node.

        Args:
            node_id: Network node id (an OSM node id for downloaded nodes), or None for a vertex.
            lon: Longitude.
            lat: Latitude.

        Returns:
            A positive (existing) or negative (new) node id.
        """
        if node_id is not None and int(node_id) in self.nodes:
            self.used_existing.add(int(node_id))
            return int(node_id)
        key = (round(lat, 6), round(lon, 6))
        if key in self.at:
            self.used_existing.add(self.at[key])
            return self.at[key]
        if key not in self.new_nodes:
            self.new_nodes[key] = (self.next_id, lat, lon)
            self.next_id -= 1
        return self.new_nodes[key][0]

    def path_refs(self, path: list[tuple]) -> list[int]:
        """Node references of a path of edges (end nodes by network id, the vertices in between by position).

        Args:
            path: Output element of :func:`_paths`.

        Returns:
            The OSM node ids along the path.
        """
        refs: list[int] = []
        for a, b, c in path:
            seq = [self.ref(a, *c[0])] + [self.ref(None, *p) for p in c[1:-1]] + [self.ref(b, *c[-1])]
            refs += seq if not refs else seq[1:]
        return refs


def osm_changes(state, base, project, edges: gpd.GeoDataFrame, streets: gpd.GeoDataFrame, osm_path) -> tuple[str, dict]:
    """The new streets and the modified OSM roads as a JOSM-style ``.osm`` file (open it in JOSM, check it and upload).

    * **New ways** for the roads drawn by the community (one way per path of the drawn line), new nodes with negative ids; where a new road
      joins the network it uses the existing OSM node, or a new node inserted into the existing road (which is modified).
    * **Modified ways** (``action="modify"``, with their OSM version): a way whose name changed (community name, never the derived
      ``3-xyz``) or whose geometry changed (node inserted, vertex moved, part deleted). Names are only added or changed, never removed.

    Args:
        state: Computed network.
        base: The downloaded network.
        project: The project.
        edges: Output of :func:`edge_table`.
        streets: Output of :func:`street_table`.
        osm_path: The OSM file the network was made from (``input/streets.osm``).

    Returns:
        ``(xml text, stats)`` with ``new_ways``, ``modified_ways``, ``renamed_ways``, ``new_nodes``, ``skipped_ways`` (changed ways that could not be rebuilt).
    """
    nodes, ways = read_osm(osm_path)
    b = OsmBuilder(nodes, ways)
    own_name = streets.set_index("street_id")["own_name"]
    rows = [(int(r.u), int(r.v), [(x, y) for x, y in r.geometry.coords], r) for r in edges.itertuples()]
    new_ways, mod_ways, skipped, renamed = [], [], 0, 0

    groups: dict = {}                                               # 1. new roads, one group per drawn line
    for u, v, c, r in rows:
        if r.new:
            groups.setdefault(r.osmid, []).append((u, v, c, r))
    for oid, items in groups.items():
        for path in _paths([(u, v, c) for u, v, c, _ in items]):
            first = next(r for u, v, c, r in items if {u, v} == {path[0][0], path[0][1]})
            nm = own_name.get(int(first.street_id), "") if pd.notna(first.street_id) else ""
            tags = {"highway": first.highway or "residential"}
            if nm:
                tags["name"] = nm
            if pd.notna(first.paved):
                tags["surface"] = "paved" if first.paved else "unpaved"
            new_ways.append({"id": None, "refs": b.path_refs(path), "tags": tags})

    by_way: dict = {}                                               # 2. downloaded roads: changed names or geometry
    for u, v, c, r in rows:
        if r.new:
            continue
        ids = re.findall(r"\d+", r.osmid)
        if len(ids) == 1 and int(ids[0]) in ways:
            by_way.setdefault(int(ids[0]), []).append((u, v, c, r))
        elif ids:
            for w in ids:
                if int(w) in ways:
                    by_way.setdefault(int(w), [])                   # merged edge: only the name can be handled
    for wid, items in by_way.items():
        w = ways[wid]
        tags, refs = dict(w["tags"]), None
        names = Counter(own_name.get(int(r.street_id), "") for *_x, r in items if pd.notna(r.street_id))
        nm = next((n for n, _c in names.most_common() if n), "")
        name_changed = bool(nm) and nm != w["tags"].get("name", "")
        if name_changed:
            tags["name"] = nm
        if items and any(r.geometry_modified for *_x, r in items) and w["refs"][0] != w["refs"][-1]:
            paths = _paths([(u, v, c) for u, v, c, _ in items])
            if len(paths) == 1:
                refs = b.path_refs(paths[0])
                if refs[0] != w["refs"][0] and refs[-1] == w["refs"][0] or (refs[0] != w["refs"][0] and refs[0] == w["refs"][-1]):
                    refs = refs[::-1]
                if refs == w["refs"]:
                    refs = None
            else:
                skipped += 1
        tag_changed = False
        if items:                                                          # rank and surface: compared with the OSM tags, so a change that was undone is no change
            rank_len: dict = {}
            known = {True: 0.0, False: 0.0}
            for *_x, r in items:
                rank_len[highway_rank(r.highway)] = rank_len.get(highway_rank(r.highway), 0.0) + float(r.length_m)
                if pd.notna(r.paved):
                    known[bool(r.paved)] += float(r.length_m)
            final_rank = min(rank_len, key=lambda k: (-rank_len[k], k))
            if highway_rank(w["tags"].get("highway", "")) != final_rank:
                tags["highway"] = engine.RANK_HIGHWAY[final_rank]
                tag_changed = True
            if known[True] + known[False] > 0:
                final_paved = known[True] >= known[False]
                if final_paved != engine.surface_paved(w["tags"].get("surface")):
                    tags["surface"] = "paved" if final_paved else "unpaved"
                    tag_changed = True
        if name_changed or refs is not None or tag_changed:
            mod_ways.append({"id": wid, "version": w["version"], "refs": refs or w["refs"], "tags": tags})
            renamed += int(name_changed)

    for w in mod_ways:
        b.used_existing.update(r for r in w["refs"] if r in nodes)
    xml = [f"<?xml version='1.0' encoding='UTF-8'?>", "<osm version='0.6' generator='community address app' upload='false'>"]
    for nid in sorted(b.used_existing):
        la, lo, ver = nodes[nid]
        xml.append(f"  <node id='{nid}' version='{ver}' visible='true' lat='{la}' lon='{lo}' />")
    for _k, (nid, la, lo) in sorted(b.new_nodes.items(), key=lambda kv: -kv[1][0]):
        xml.append(f"  <node id='{nid}' action='modify' visible='true' lat='{la}' lon='{lo}' />")
    wid = -1
    for w in new_ways:
        xml.append(f"  <way id='{wid}' action='modify' visible='true'>")
        xml += [f"    <nd ref='{r}' />" for r in w["refs"]] + [f"    <tag k={quoteattr(k)} v={quoteattr(v)} />" for k, v in w["tags"].items()]
        xml.append("  </way>")
        wid -= 1
    for w in mod_ways:
        xml.append(f"  <way id='{w['id']}' action='modify' version='{w['version']}' visible='true'>")
        xml += [f"    <nd ref='{r}' />" for r in w["refs"]] + [f"    <tag k={quoteattr(k)} v={quoteattr(v)} />" for k, v in w["tags"].items()]
        xml.append("  </way>")
    xml.append("</osm>")
    return "\n".join(xml), {"new_ways": len(new_ways), "modified_ways": len(mod_ways), "renamed_ways": renamed, "new_nodes": len(b.new_nodes),
                            "skipped_ways": skipped}
