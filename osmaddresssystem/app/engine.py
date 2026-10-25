"""The compute pipeline of the app (path numbering only).

``compute_network`` turns the base network, the community edits, the reference node and the names given so far into
the numbered, named network:

1. apply the community edits and read which streets already have a name (OSM, community, names assigned in the app),
2. plan the fewest further streets that need a name (``naming_plan``): every street ends up with a name or a derived
   name within ``max_levels`` derivations, a derived street is shorter than its main street, and the named streets
   form one network connected to the reference through named streets,
3. route to the reference through the named and to-be-named streets only (``path_numbering.node_distances`` with
   the backbone), then re-assign the street ids by distance, cutting streets where needed (``split_streets``;
   ``street_id_original`` always keeps the id before the cut),
4. derive the names of the sub-streets (``branches.assign_branch_names``): ``3-xyz``, and ``3-#512`` while the main
   street 512 is still to be named; naming it later turns these into ``3-<name>``,
5. check that every street is covered after the cutting; streets that are not get planned too and steps 3-4 repeat.

``compute_addresses`` then gives every surveyed house a house number (path numbering, letters when several houses share
a number), a complete address and the street signs of the intersections.
"""

from __future__ import annotations

import math
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer

from .. import branches, naming_plan, numbering, path_numbering, pipeline, street_editor, street_signs
from ..branches import is_named, placeholder
from . import roads as roads_mod
from .project import Project

_EMPTY_STREET_EDITS = "old_street_id,new_street_id,street_name,street_rank,is_drivable\n"


@dataclass
class BaseData:
    """The fixed inputs of a project.

    Attributes:
        nodes: Nodes of the labelled network, indexed by node id, projected, with ``x``/``y``/``node_id``.
        edges: Edges indexed by ``(u, v, key)``.
        streets: One row per street of the labelled network.
        street_edits: Path of the community street edits csv (a header-only file when there are none).
        edge_edits: Path of the community edge edits csv, or None.
        buildings: The houses to address (points or polygons, WGS84) or None.
        area: Polygon(s) of the area to process (WGS84) or None.
        points: Important points - squares, intersections, facilities ... (WGS84) or None.
    """

    nodes: gpd.GeoDataFrame
    edges: gpd.GeoDataFrame
    streets: pd.DataFrame
    street_edits: Path
    edge_edits: Path | None = None
    buildings: gpd.GeoDataFrame | None = None
    area: gpd.GeoDataFrame | None = None
    points: gpd.GeoDataFrame | None = None

    @property
    def crs(self):
        """Projected CRS of the network."""
        return self.nodes.crs


@dataclass
class NetworkState:
    """Result of :func:`compute_network`.

    Attributes:
        nodes: Nodes.
        edges: Edges with the new ``street_id``, ``street_id_original``, positions and branch names.
        streets: One row per street (new ids) with names, ``branch_*`` columns, ``to_be_named``, ``display_name``.
        streets_gdf: Streets with a line geometry in the numbering direction (projected).
        distance: Distance of every node to the reference through the backbone.
        reference: ``{"node", "x", "y"}`` of the reference node.
        plan: Streets still to be named, in priority order, with ``street_id_original``.
        mapping: Table ``street_id, street_id_original, street_part, street_name`` of every street id.
        cuts: Points where a street was cut (WGS84).
        stats: Headline numbers.
        notes: Remarks (planner fall-backs, uncovered streets ...).
        warnings: Things the community should look at (see :func:`build_warnings`).
    """

    nodes: gpd.GeoDataFrame
    edges: gpd.GeoDataFrame
    streets: pd.DataFrame
    streets_gdf: gpd.GeoDataFrame
    distance: pd.Series
    reference: dict
    plan: pd.DataFrame
    mapping: pd.DataFrame
    cuts: gpd.GeoDataFrame
    stats: dict
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    aoi: object = None
    extra: set = field(default_factory=set)        # streets added to the plan after the cutting: the starting point of the next computation (saves rounds)


@dataclass
class AddressState:
    """Result of :func:`compute_addresses`.

    Attributes:
        houses: Surveyed houses (WGS84 points) with ``address``, ``number``, ``street_id``, ``street``, ``side`` and
            ``status`` (``ok``, ``pending_name``, ``no_name`` or ``no_street``).
        signs: Intersections (WGS84 points) with ``labels`` (the street names on the sign boards).
        stats: Headline numbers.
        warnings: Things to look at (houses without a street, on a street without a name ...).
    """

    houses: gpd.GeoDataFrame
    signs: gpd.GeoDataFrame
    stats: dict
    warnings: list = field(default_factory=list)


def load_base(project: Project) -> BaseData:
    """Read the network and the input files of a project folder.

    Args:
        project: The project.

    Returns:
        The :class:`BaseData`.

    Raises:
        FileNotFoundError: If the network files are missing (run ``setup`` first).
    """
    n, i = project.network_dir, project.input_dir
    for f in ("nodes.gpkg", "edges.gpkg", "streets.csv"):
        if not (n / f).exists():
            raise FileNotFoundError(f"{n / f} is missing: run `python -m osmaddresssystem.app setup --project {project.root}`")
    nodes = gpd.read_file(n / "nodes.gpkg").set_index("osmid")
    edges = gpd.read_file(n / "edges.gpkg").set_index(["u", "v", "key"])
    streets = pd.read_csv(n / "streets.csv")
    se = i / "street_edits.csv"
    if not se.exists():
        cache = project.root / "computed"
        cache.mkdir(exist_ok=True)
        se = cache / "street_edits_empty.csv"
        se.write_text(_EMPTY_STREET_EDITS)
    ee = i / "edge_edits.csv"

    def opt(name: str):
        """Read an optional GeoPackage of the input folder in WGS84."""
        return gpd.read_file(i / name).to_crs(4326) if (i / name).exists() else None

    return BaseData(nodes, edges, streets, se, ee if ee.exists() else None, opt("buildings.gpkg"), opt("area.gpkg"), opt("points.gpkg"))


def _given(name) -> bool:
    """Whether a local street name is given (not blank and not just a number).

    Args:
        name: Cell value.

    Returns:
        True for a real name.
    """
    return isinstance(name, str) and bool(name.strip()) and not name.strip().isdigit()


def resolve_edit_names(base: BaseData, project: Project) -> tuple[Path, Path | None]:
    """Write the community edit files with their names resolved.

    Rules: a local (community) name always overrides the OSM name; a blank local name never removes one (the street keeps its
    existing name); when the street-level and the edge-level file disagree on a street id the street-level file wins (it is the
    more recent one when both carry the same date).

    Args:
        base: The (effective) base data.
        project: The project (folder for the resolved files).

    Returns:
        Paths of the street-level and (if any) edge-level edit files to use.
    """
    se = pd.read_csv(base.street_edits, dtype=str)
    ee = pd.read_csv(base.edge_edits, dtype=str) if base.edge_edits else None
    local: dict[str, str] = {}
    for df in ([ee] if ee is not None else []) + [se]:                      # later files overwrite earlier ones
        for sid, name in zip(df["new_street_id"], df["street_name"]):
            if _given(name) and isinstance(sid, str):
                local[sid.strip()] = name.strip()
    if not len(se) and ee is None:
        return base.street_edits, None
    existing = base.streets.assign(street_id=base.streets["street_id"].astype(str)).set_index("street_id")["street_name"]
    se = se.copy()
    se["street_name"] = [local.get(str(n).strip(), existing.get(str(o).strip())) for o, n in zip(se["old_street_id"], se["new_street_id"])]
    out = project.root / "computed"
    out.mkdir(exist_ok=True)
    p1 = out / "street_edits_resolved.csv"
    se.to_csv(p1, index=False)
    p2 = None
    if ee is not None:
        ee = ee.copy()
        cur = base.edges["street_name"]

        def current(u, v, k):
            """Existing street name of an edge, or None."""
            try:
                return cur.get((int(u), int(v), int(k)))
            except ValueError:
                return None

        ee["street_name"] = [local.get(str(n).strip(), current(u, v, k)) for u, v, k, n in zip(ee["u"], ee["v"], ee["key"], ee["new_street_id"])]
        p2 = out / "edge_edits_resolved.csv"
        ee.to_csv(p2, index=False)
    return p1, p2


def effective_base(base: BaseData, project: Project, write_cache: bool = True) -> tuple[BaseData, "roads_mod.RoadsResult | None"]:
    """The network with the editors' changes: deleted edges removed, missing roads added, street ids reassigned (see ``roads``).

    Args:
        base: The base data.
        project: The project (``roads``).
        write_cache: Write the rewritten edge edits to ``computed/`` (False for previews that must not touch the real result).

    Returns:
        ``(base, result)``: the base data to compute with and the integration result (None when the project has no drawn roads).
        Nothing is written into the base files; the edge edits that had to change are written to ``computed/``.
    """
    rd = project.roads
    removed_feats = set(rd.get("removed_features", []))
    imports = rd.get("imports", [])
    nodes, edges, streets = base.nodes, base.edges, base.streets
    if rd.get("removed"):
        edges, streets = roads_mod.drop_edges(edges, streets, rd["removed"])
    features = [{**f, "key": f"{imp['id']}:{f['id']}"} for imp in imports for f in imp["features"] if f"{imp['id']}:{f['id']}" not in removed_feats]
    edges = add_flag_columns(edges, project.input_dir / "streets.osm")                  # paved / drivable_info of the downloaded roads (None = unknown)
    ee = pd.read_csv(base.edge_edits, dtype=str).fillna("") if base.edge_edits else None
    res = None
    if features:
        res = roads_mod.integrate_roads(nodes, edges, streets, ee, features, rd.get("connections", {}), keep=set(rd.get("keep", [])))
        nodes, edges, streets = res.nodes, res.edges, res.streets
        if res.edge_edits is not None:
            ee = res.edge_edits
    assign = rd.get("edge_street") or {}
    if assign:
        edges, streets = roads_mod.assign_street_ids(edges, streets, assign)
        if ee is not None and len(ee):                       # the community edge edits must not undo the choice made here
            ee = ee[[roads_mod.edge_ref(u, v, k) not in assign for u, v, k in zip(ee["u"].astype(int), ee["v"].astype(int), ee["key"].astype(int))]]
    edges, streets = apply_attributes(edges, streets, {})             # the rank / drivable / paved of the editors are applied in compute_network, to the ids they were given for
    changed = bool(assign) or bool(res is not None and res.report["split_edges"])
    edits_path = base.edge_edits
    if write_cache and ee is not None and changed:
        cache = project.root / "computed"
        cache.mkdir(exist_ok=True)
        edits_path = cache / "edge_edits_effective.csv"
        ee.to_csv(edits_path, index=False)
    return BaseData(nodes, edges, streets, base.street_edits, edits_path, base.buildings, base.area, base.points), res


def roads_warnings(report: dict, streets: pd.DataFrame) -> list[dict]:
    """Warnings about the drawn roads.

    Args:
        report: ``RoadsResult.report``.
        streets: Computed streets (to find the street ids of the drawn roads after the cutting).

    Returns:
        ``roads_dangling`` (ends not connected to anything) and ``roads_isolated`` (roads that do not reach the network).
    """
    def ids_of(feature_keys) -> list[int]:
        """Computed street ids of drawn-road features."""
        originals = {report["street_ids"][k] for k in feature_keys if k in report["street_ids"]}
        return [int(s) for s in streets.loc[streets["street_id_original"].isin(originals), "street_id"]]

    out = []
    dang = [n for n in report["nodes"] if n["key"] in set(report["dangling"])]
    if dang:
        keys = sorted({f for n in dang for f in n.get("features", [])})
        out.append(_warning("roads_dangling", "warning", f"{len(dang)} ends of the drawn roads are not connected",
                            "Click an end, then the road or node to connect it to. An unconnected end is a dead end.", ids_of(keys), count=len(dang),
                            nodes=[{"key": n["key"], "lat": n["lat"], "lon": n["lon"]} for n in dang]))
    if report["isolated"]:
        out.append(_warning("roads_isolated", "error", f"{len(report['isolated'])} drawn roads do not reach the street network",
                            "They get no number and no name until they are connected.", ids_of(report["isolated"]), count=len(report["isolated"])))
    return out


def disconnected_parts(base: BaseData) -> list[dict]:
    """The parts of the street network that are not connected to the main (largest) part, inside the area of interest.

    Args:
        base: The (effective) base data.

    Returns:
        One dict per smaller part, largest first: ``id``, ``edges`` (list of ``(u, v, key)``), ``street_ids``, ``length_m``.
    """
    idx = list(base.edges.index)
    parent: dict = {}

    def find(a):
        """Union-find root with path halving."""
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for u, v, _k in idx:
        parent[find(u)] = find(v)
    groups: dict = {}
    for i, (u, v, _k) in enumerate(idx):
        groups.setdefault(find(u), []).append(i)
    if len(groups) < 2:
        return []
    lengths = base.edges.geometry.length.to_numpy()
    main = max(groups.values(), key=lambda g: lengths[g].sum())
    aoi = aoi_polygon(base)
    aoi_p = None
    if aoi is not None:
        aoi_p = gpd.GeoSeries([aoi], crs=4326).to_crs(base.crs).iloc[0]
    out = []
    for g in sorted(groups.values(), key=lambda g: -lengths[g].sum()):
        if g is main:
            continue
        sub = base.edges.iloc[g]
        if aoi_p is not None and not sub.geometry.intersects(aoi_p).any():
            continue
        out.append({"id": len(out) + 1, "edges": [idx[i] for i in g], "street_ids": sorted({int(x) for x in sub["street_id"]}), "length_m": float(lengths[g].sum())})
    return out


def aoi_polygon(base: BaseData):
    """The area of interest of the project (WGS84), or None.

    It is the polygon(s) of ``input/area.gpkg`` whose ``kind`` contains ``aoi`` (for Fotouni: the surveyed buildings + 50 m); only it
    is shown on the map, and only the streets inside it need names.

    Args:
        base: The base data.

    Returns:
        A shapely geometry or None.
    """
    a = base.area
    if a is None or "kind" not in a.columns:
        return None
    sel = a[a["kind"].astype(str).str.contains("aoi", case=False)]
    return None if sel.empty else sel.to_crs(4326).geometry.union_all()


def _street_ids_inside(edges: gpd.GeoDataFrame, aoi_wgs84) -> set[int]:
    """Ids of the streets that have a road inside the area of interest.

    Args:
        edges: Edges (projected) with ``street_id``.
        aoi_wgs84: The area of interest.

    Returns:
        The street ids.
    """
    poly = gpd.GeoSeries([aoi_wgs84], crs=4326).to_crs(edges.crs).iloc[0]
    hit = edges.geometry.intersects(poly)
    return {int(x) for x in edges.loc[hit, "street_id"].dropna()}


_OSM_INFO: dict = {}

PAVED_SURFACES = {"asphalt", "paved", "concrete", "concrete:plates", "concrete:lanes", "paving_stones", "sett", "cobblestone", "metal", "bricks", "chipseal"}
UNPAVED_SURFACES = {"unpaved", "dirt", "gravel", "ground", "sand", "earth", "compacted", "mud", "grass", "laterite", "fine_gravel", "pebblestone", "clay",
                    "woodchips", "rock", "dirt/sand", "soil"}
OUTSIDE_SNAP_M = 500.0
"""A reference point outside the area is joined to the larger road network when a node is at most this near (metres)."""
SIGN_POI_MAX_M = 300.0
"""A reference point is put on the signs of the nearest intersection when that is at most this far (metres)."""
NO_NAME = "0"                                                 # stored as the name of a street whose OSM name was deleted; a number-only name never counts as a name
RANK_HIGHWAY = {0: "trunk", 1: "primary", 2: "secondary", 3: "tertiary", 4: "residential", 5: "footway"}
"""A representative OSM ``highway`` value of every street rank (used when the editors change the rank of a street)."""
RANK_LABELS = {0: "0 motorway / trunk", 1: "1 primary", 2: "2 secondary", 3: "3 tertiary", 4: "4 residential / unclassified / service", 5: "5 footway / path"}


def osm_ways_info(path) -> dict | None:
    """What the OSM file says about every way: whether it has a ``name`` tag and its ``surface``.

    Args:
        path: ``input/streets.osm``.

    Returns:
        ``{"named": set of way ids, "surface": {way id: surface tag}}`` (ids as text), or None when there is no file.
    """
    import xml.etree.ElementTree as ET

    path = Path(path)
    if not path.exists():
        return None
    key = (str(path), path.stat().st_mtime)
    if key not in _OSM_INFO:
        named, surface = set(), {}
        for _ev, el in ET.iterparse(path, events=("end",)):
            if el.tag == "way":
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                if tags.get("name"):
                    named.add(el.get("id"))
                if tags.get("surface"):
                    surface[el.get("id")] = tags["surface"]
                el.clear()
        _OSM_INFO.clear()
        _OSM_INFO[key] = {"named": named, "surface": surface}
    return _OSM_INFO[key]


def osm_named_ways(path) -> set[str] | None:
    """Ids of the OSM ways that have a ``name`` tag (see :func:`osm_ways_info`).

    Args:
        path: ``input/streets.osm``.

    Returns:
        A set of way ids (as text), or None when there is no file.
    """
    info = osm_ways_info(path)
    return None if info is None else info["named"]


def surface_paved(surface) -> bool | None:
    """Paved / unpaved by the OSM ``surface`` tag; None when the tag is missing or not known.

    Args:
        surface: The ``surface`` tag or None.

    Returns:
        True, False or None.
    """
    s = str(surface or "").strip().lower()
    return True if s in PAVED_SURFACES else False if s in UNPAVED_SURFACES else None


def add_flag_columns(edges: gpd.GeoDataFrame, osm_path) -> gpd.GeoDataFrame:
    """Give every edge its ``paved`` and ``drivable_info`` flags (nullable booleans: True / False / None = OSM does not say).

    ``paved`` comes from the OSM ``surface`` tag of the way, ``drivable_info`` from the ``highway`` class.

    Args:
        edges: Edges with ``osmid``, ``highway`` and ``is_drivable``.
        osm_path: ``input/streets.osm`` (surface tags).

    Returns:
        A copy with the two columns (kept when already there).
    """
    import re

    from ..default_values import DEFAULT_HIGHWAY_RANKS, DEFAULT_NON_DRIVABLE_HIGHWAYS

    edges = edges.copy()
    if "paved" not in edges.columns:
        info = osm_ways_info(osm_path)
        surf = info["surface"] if info else {}
        edges["paved"] = pd.array([surface_paved(next((surf[i] for i in re.findall(r"\d+", o) if i in surf), None))
                                  for o in edges["osmid"].astype(str)], dtype="boolean")
    if "drivable_info" not in edges.columns:
        known = set(DEFAULT_HIGHWAY_RANKS) | set(DEFAULT_NON_DRIVABLE_HIGHWAYS)
        edges["drivable_info"] = pd.array([bool(d) if any(t in known for t in re.findall(r"[a-z_]+", str(h))) else None
                                          for d, h in zip(edges["is_drivable"], edges["highway"])], dtype="boolean")
    return edges


def dominant_rank(edges: gpd.GeoDataFrame, streets: pd.DataFrame) -> pd.DataFrame:
    """One rank per street id: the rank that most of the street's length has (the lengths of all edges of the same rank are added).

    Drivable and paved may change from edge to edge, the rank of a street does not. A tie goes to the more important rank.

    Args:
        edges: Edges with ``street_id``, ``highway`` and ``length``.
        streets: Streets table.

    Returns:
        The streets table with ``street_rank`` (and ``highway`` of that rank when the table has it) set.
    """
    rk = pd.Series([path_numbering.highway_rank(h) for h in edges["highway"]], index=edges.index)
    df = pd.DataFrame({"sid": pd.to_numeric(edges["street_id"], errors="coerce"), "rank": rk.to_numpy(), "len": edges["length"].astype(float).to_numpy()}).dropna(subset=["sid"])
    tot = df.groupby(["sid", "rank"])["len"].sum().reset_index().sort_values(["sid", "len", "rank"], ascending=[True, False, True])
    best = tot.drop_duplicates("sid").set_index("sid")["rank"].astype(int).to_dict()
    streets = streets.copy()
    streets["street_rank"] = [best.get(int(i), r) for i, r in zip(streets["street_id"], streets["street_rank"] if "street_rank" in streets else [4] * len(streets))]
    if "highway" in streets.columns:
        streets["highway"] = [RANK_HIGHWAY[r] if int(i) in best else h for i, r, h in zip(streets["street_id"], streets["street_rank"], streets["highway"])]
    return streets


def apply_attributes(edges: gpd.GeoDataFrame, streets: pd.DataFrame, attrs: dict) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Apply the street rank / drivable / paved chosen by the editors and decide ``is_drivable`` for the street id algorithm.

    The algorithm needs a yes or no: an unknown drivability counts as drivable depending on the rank (rank 5, footway or path, is not
    drivable; the other ranks are).

    Args:
        edges: Edges with ``street_id``, ``highway``, ``paved`` and ``drivable_info`` (see :func:`add_flag_columns`).
        streets: Streets table.
        attrs: ``{street id: {"rank", "drivable", "paved"}}`` (only the keys that were set; ``None`` means unknown).

    Returns:
        ``(edges, streets)`` with the chosen values applied and ``is_drivable`` filled in.
    """
    edges, streets = edges.copy(), streets.copy()
    if "street_rank" not in streets.columns:
        streets["street_rank"] = [path_numbering.highway_rank(h) for h in streets["highway"]] if "highway" in streets.columns else 4
    sid = pd.to_numeric(edges["street_id"], errors="coerce")
    for key, a in (attrs or {}).items():
        oid = int(key)
        m = (sid == oid).to_numpy()
        sm = streets["street_id"].astype(int) == oid
        if a.get("rank") is not None:
            r = int(a["rank"])
            edges.loc[m, "highway"] = RANK_HIGHWAY.get(r, "residential")
            streets.loc[sm, "street_rank"] = r
            if "highway" in streets.columns:
                streets.loc[sm, "highway"] = RANK_HIGHWAY.get(r, "residential")
        if "drivable" in a:
            edges.loc[m, "drivable_info"] = pd.array([a["drivable"]] * int(m.sum()), dtype="boolean")
        if "paved" in a:
            edges.loc[m, "paved"] = pd.array([a["paved"]] * int(m.sum()), dtype="boolean")
    rank = [path_numbering.highway_rank(h) for h in edges["highway"]]
    edges["is_drivable"] = [bool(d) if pd.notna(d) else r < 5 for d, r in zip(edges["drivable_info"], rank)]       # unknown: by the rank
    return edges, dominant_rank(edges, streets)                                  # one rank per street: the one with the most length


def apply_part_attributes(edges: gpd.GeoDataFrame, streets: pd.DataFrame, part_attrs: dict) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Apply the rank / drivable / paved the editors chose for one part of a cut street (after the cutting, when the parts exist).

    Args:
        edges: Edges after the cutting (``street_id``, ``highway``, ``drivable_info``, ``paved``).
        streets: Streets after the cutting (``street_id``, ``street_id_original``, ``street_part``).
        part_attrs: ``{(original id, part): {"rank", "drivable", "paved"}}``.

    Returns:
        ``(edges, streets)`` with the values applied (and ``street_rank`` recomputed).
    """
    if not part_attrs:
        return edges, streets
    edges = edges.copy()
    key = {(int(o), int(p)): int(i) for i, o, p in zip(streets["street_id"], streets["street_id_original"], streets["street_part"])}
    sid = pd.to_numeric(edges["street_id"], errors="coerce")
    for k, a in part_attrs.items():
        if k not in key:
            continue
        m = (sid == key[k]).to_numpy()
        if a.get("rank") is not None:
            edges.loc[m, "highway"] = RANK_HIGHWAY.get(int(a["rank"]), "residential")
        if "drivable" in a:
            edges.loc[m, "drivable_info"] = pd.array([a["drivable"]] * int(m.sum()), dtype="boolean")
        if "paved" in a:
            edges.loc[m, "paved"] = pd.array([a["paved"]] * int(m.sum()), dtype="boolean")
    rank = [path_numbering.highway_rank(h) for h in edges["highway"]]
    edges["is_drivable"] = [bool(d) if pd.notna(d) else r < 5 for d, r in zip(edges["drivable_info"], rank)]
    return edges, dominant_rank(edges, streets)


def drop_inferred_part_names(edges: gpd.GeoDataFrame, streets: pd.DataFrame, named_ways, protected: set) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """A street (part) keeps the name of the network only if one of its roads has a name in OpenStreetMap (or was named by the community).

    The street identity step gives short unnamed roads the name of the street they line up with; once the street has been cut and
    restricted to the area, such a road can be all that is left of it, and then it should not carry that name: it is to be named or
    it derives a name from a neighbour.

    Args:
        edges: Edges after the cutting (``street_id``, ``street_id_original``, ``osmid``, ``name``).
        streets: Streets after the cutting.
        named_ways: OSM way ids that have a name tag (None: nothing is cleared).
        protected: Original street ids with a name given by the community (never cleared).

    Returns:
        ``(edges, streets)`` with the inferred names cleared.
    """
    if named_ways is None:
        return edges, streets
    import re

    own = []
    for o, nm in zip(edges["osmid"].astype(str), edges["name"]):
        own.append(any(i in named_ways for i in re.findall(r"\d+", o)) or (o.startswith("road") and is_named(nm)))   # drawn roads carry their own name
    sid = pd.to_numeric(edges["street_id"], errors="coerce")
    ok = set(sid[pd.Series(own, index=edges.index)].dropna().astype(int))
    orig = {int(a): int(b) for a, b in zip(streets["street_id"], streets["street_id_original"])} if "street_id_original" in streets else {}
    bad = [int(x) for x, nm in zip(streets["street_id"], streets["street_name"]) if is_named(nm) and int(x) not in ok and orig.get(int(x), int(x)) not in protected]
    if not bad:
        return edges, streets
    edges, streets = edges.copy(), streets.copy()
    streets.loc[streets["street_id"].astype(int).isin(bad), "street_name"] = ""
    if "street_name_base" in streets.columns:
        streets.loc[streets["street_id"].astype(int).isin(bad), "street_name_base"] = ""
    m = sid.isin(bad).to_numpy()
    for c in ("street_name", "name"):
        if c in edges.columns:
            edges.loc[m, c] = ""
    return edges, streets


def _redundant_planned(nodes1, edges2, streets2, chosen: set, order: dict, st, aoi, ref_node) -> set:
    """Streets of the list to name that are not needed: every street still gets a name or a derived name without them.

    Streets cut off later (or added in a later round) can end up on the list although other streets on the list give them a name, or
    only connect two named streets that are connected through derived streets anyway. A street is dropped when the streets that
    remain (named or to be named) still cover every street of the area within the allowed derivation levels **and** every named
    or to-be-named street is still connected to the reference through streets that have a name or a derived name.

    Args:
        nodes1: Nodes after the last pass.
        edges2: Edges after the last pass (street ids after the cutting).
        streets2: Streets after the last pass.
        chosen: Original ids of the streets to name by hand.
        order: Priority of the streets (a larger number is dropped first).
        st: The settings.
        aoi: The area of interest or None.
        ref_node: The reference node.

    Returns:
        The original ids that can be dropped.
    """
    parents = branches.branch_parents(nodes1, edges2, streets2)
    children = naming_plan._children(parents)
    deep_ok = branches.second_level_streets(nodes1, edges2, streets2, st.second_level_max_drive_m)
    adjacent, _nodes = branches.street_adjacency(nodes1, edges2, streets2)
    ids = streets2["street_id"].astype(int)
    orig = dict(zip(ids, streets2["street_id_original"].astype(int)))
    own = {int(i) for i, n in zip(ids, streets2["street_name"]) if is_named(n)}
    parts: dict = {}
    for i, o in orig.items():
        if o in chosen and i not in own:
            parts.setdefault(o, set()).add(i)
    e = edges2.reset_index()
    at_ref = {int(x) for x in e.loc[(e["u"] == ref_node) | (e["v"] == ref_node), "street_id"].dropna()}
    inside = _street_ids_inside(edges2, aoi) if aoi is not None else None
    targets = {s_ for s_ in parents if inside is None or s_ in inside}

    def stranded(roots: set) -> set:                   # roots that the reference cannot reach through streets with a name or a derived name
        covered = set(naming_plan._reach(children, roots, st.max_levels, deep_ok))
        seen, queue = set(), [s_ for s_ in at_ref if s_ in covered]
        seen.update(queue)
        while queue:
            for t in adjacent.get(queue.pop(), ()):
                if t in covered and t not in seen:
                    seen.add(t)
                    queue.append(t)
        return {r for r in roots if r not in seen}

    keep = set().union(*parts.values()) if parts else set()
    base_stranded = stranded(own | keep)
    drop: set = set()
    for o in sorted(parts, key=lambda x: (-order.get(x, 10**6), -x)):
        trial = keep - parts[o]
        if not targets <= set(naming_plan._reach(children, own | trial, st.max_levels, deep_ok)):
            continue
        if not stranded(own | trial) <= base_stranded:
            continue
        keep = trial
        drop.add(o)
    return drop


def restrict_to_aoi(base: BaseData, ref_node: int) -> BaseData:
    """Keep only the roads that touch the area of interest (whole edges), plus the shortest links needed to reach the reference.

    Roads outside the area must not influence the street ids (a street cut because of a route that leaves the area), so they are left
    out of the computation. A part of the area that would be cut off from the reference node is joined to it by the shortest path in
    the full network.

    Args:
        base: The base data (with the editors' changes).
        ref_node: The reference node.

    Returns:
        The base data with fewer edges (and the streets that still have an edge); unchanged without an area.
    """
    aoi = aoi_polygon(base)
    if aoi is None:
        return base
    import networkx as nx

    poly = gpd.GeoSeries([aoi], crs=4326).to_crs(base.edges.crs).iloc[0]
    inside = base.edges.geometry.intersects(poly).to_numpy()
    keep = set(base.edges.index[inside])
    full = nx.Graph()
    for (u, v, k), ln in zip(base.edges.index, base.edges["length"].astype(float)):
        if not full.has_edge(u, v) or ln < full[u][v]["length"]:
            full.add_edge(u, v, length=ln, key=(u, v, k))
    part = nx.Graph()
    for (u, v, k) in keep:
        part.add_edge(u, v)
    if ref_node not in part:
        part.add_node(ref_node)
    for comp in list(nx.connected_components(part)):
        if ref_node in comp or ref_node not in full:
            continue
        src = next(iter(comp))
        try:
            path = nx.shortest_path(full, src, ref_node, weight="length")
        except nx.NetworkXNoPath:
            continue
        for a, b in zip(path, path[1:]):
            u, v, k = full[a][b]["key"]
            keep.update({(u, v, k), (v, u, k)})
    mask = [i in keep for i in base.edges.index]
    edges = base.edges[mask]
    alive = set(pd.to_numeric(edges["street_id"], errors="coerce").dropna().astype(int))
    streets = base.streets[base.streets["street_id"].astype(int).isin(alive)].reset_index(drop=True)
    return BaseData(base.nodes, edges, streets, base.street_edits, base.edge_edits, base.buildings, base.area, base.points)


def snap_reference(base: BaseData, lat: float, lon: float) -> dict:
    """Snap a clicked position to the nearest node of a drivable street.

    Args:
        base: The base data.
        lat: Latitude of the click.
        lon: Longitude of the click.

    Returns:
        ``{"node", "lat", "lon", "x", "y", "distance_m"}`` of the snapped node.

    Raises:
        ValueError: If the network has no drivable node.
    """
    x, y = Transformer.from_crs(4326, base.crs, always_xy=True).transform(lon, lat)
    drv = base.edges["is_drivable"].astype(str).str.lower().isin(["true", "1"]) if "is_drivable" in base.edges else None
    e = base.edges[drv] if drv is not None else base.edges
    ids = set(e.index.get_level_values("u")) | set(e.index.get_level_values("v"))
    cand = base.nodes[base.nodes.index.isin(ids)]
    if cand.empty:
        raise ValueError("the network has no drivable node")
    d = np.hypot(cand["x"].to_numpy() - x, cand["y"].to_numpy() - y)
    i = int(np.argmin(d))
    nx_, ny_ = float(cand["x"].iloc[i]), float(cand["y"].iloc[i])
    slon, slat = Transformer.from_crs(base.crs, 4326, always_xy=True).transform(nx_, ny_)
    return {"node": int(cand.index[i]), "lat": float(slat), "lon": float(slon), "x": nx_, "y": ny_, "distance_m": float(d[i])}


def _own_names(streets: pd.DataFrame, assigned: dict[int, str], key: str = "street_id") -> dict[int, str]:
    """Names of the streets that have a real name, with the community's assigned names on top.

    Args:
        streets: Streets table.
        assigned: Street id -> assigned name.
        key: Column the assigned ids refer to.

    Returns:
        ``street id -> name`` for every street with a real name.
    """
    out = {int(s): str(n).strip() for s, n in zip(streets["street_id"], streets["street_name"]) if is_named(n)}
    if key == "street_id":
        out.update({int(k): v for k, v in assigned.items() if int(k) in out or int(k) in set(streets["street_id"].astype(int))})
    return out


def _overlay_names(streets: pd.DataFrame, assigned: dict[int, str], first_part_only: bool = False) -> pd.DataFrame:
    """Put the assigned names on the streets (and on every part of a cut street, ``(B)``, ``(C)`` ..., unless ``first_part_only``).

    Args:
        streets: Streets after the cutting (with ``street_id_original`` and ``street_part``).
        assigned: Original street id -> name.
        first_part_only: Only the first part (the one that kept the id) gets the name; the other parts derive a name from the street
            they branch from (``3-xyz``) when they can.

    Returns:
        A copy with ``street_name`` overridden where the community assigned a name.
    """
    out = streets.copy()
    for i, (o, p) in enumerate(zip(out["street_id_original"], out["street_part"])):
        if int(o) in assigned and not (first_part_only and int(p) > 0):
            out.iloc[i, out.columns.get_loc("street_name")] = branches._part_name(assigned[int(o)], p)
    return out


def _cut_points(edges: gpd.GeoDataFrame, nodes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Points where path numbering cut a street.

    Args:
        edges: Edges with ``street_id`` and ``street_id_original``.
        nodes: Nodes with a point geometry.

    Returns:
        Points (WGS84) at the nodes shared by two parts of one original street.
    """
    flat = edges.reset_index()
    flat = flat[flat["street_id"].notna()]
    ends = pd.concat([flat[["u", "street_id", "street_id_original"]].rename(columns={"u": "node"}),
                      flat[["v", "street_id", "street_id_original"]].rename(columns={"v": "node"})])
    parts = ends.groupby(["node", "street_id_original"])["street_id"].nunique()
    cut_nodes = list(parts[parts > 1].index.get_level_values("node").unique())
    geo = nodes.geometry
    return gpd.GeoDataFrame({"node": cut_nodes}, geometry=[geo[n] for n in cut_nodes], crs=nodes.crs).to_crs(4326)


def _n(n: int, noun: str) -> str:
    """Count with a correctly pluralised noun.

    Args:
        n: The count.
        noun: Singular noun.

    Returns:
        ``"1 street"`` / ``"3 streets"``.
    """
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _warning(code: str, severity: str, title: str, message: str, streets=None, count: int | None = None, **extra) -> dict:
    """One warning entry.

    Args:
        code: Machine-readable type (``named_split``, ``needs_name`` ...).
        severity: ``"error"`` or ``"warning"``.
        title: Short heading.
        message: Explanation and what to do.
        streets: Street ids (after cutting) to highlight on the map.
        count: Number of things concerned (defaults to the number of streets).
        **extra: Further fields (``houses`` ...).

    Returns:
        The warning dict.
    """
    ids = sorted({int(s) for s in ([] if streets is None else list(streets))})
    return {"code": code, "severity": severity, "title": title, "message": message, "streets": ids,
            "count": len(ids) if count is None else count, **extra}


def build_warnings(streets: pd.DataFrame, namable: set, unconnectable_originals: list, sliver_m: float = 150.0) -> list[dict]:
    """The warnings of a computed network.

    * ``named_split``: a street that has a name had to be cut into several ids (path numbering: a street must start at
      its end nearest to the reference); the parts carry ``(B)``, ``(C)`` ... after the name.
    * ``short_part``: a part of a cut street shorter than ``sliver_m`` (often a stub at a junction).
    * ``needs_name``: streets that still have to be named by hand - the minimum that lets every other street derive a name.
    * ``uncovered``: streets that cannot get a name at all (no named or nameable street to derive it from).
    * ``unconnected``: named streets that cannot reach the reference through drivable streets.
    * ``duplicate_name``: one name given to several different streets.

    Args:
        streets: Computed streets table.
        namable: Ids of the streets that can be named.
        unconnectable_originals: Original ids of named streets that cannot reach the reference.
        sliver_m: Parts shorter than this are reported.

    Returns:
        Warning dicts, errors first.
    """
    out: list[dict] = []
    s = streets.copy()
    s["has_own_name"] = s["street_name"].map(is_named)
    cut = s[s.groupby("street_id_original")["street_id"].transform("size") > 1]
    for o, g in cut.groupby("street_id_original"):
        named = bool(g["has_own_name"].any())
        if named:
            base = g.sort_values("street_part")["street_name"].iloc[0]
            out.append(_warning("named_split", "warning", f"Named street split: {base}",
                                f"Street {int(o)} was cut into {len(g)} parts: " + ", ".join(sorted(g["street_name"].fillna("").astype(str))) + ".", g["street_id"], original=int(o)))
        small = g[g["length_m"] < sliver_m]
        if named and len(small):
            out.append(_warning("short_part", "warning", f"Very short part of {g.sort_values('street_part')['street_name'].iloc[0]}",
                                f"{len(small)} part(s) shorter than {int(sliver_m)} m (a stub?). Consider naming or merging it.", small["street_id"]))
    planned = s[s["to_be_named"].astype(bool)]
    if len(planned):
        out.append(_warning("needs_name", "warning", _n(planned['street_id_original'].nunique(), "street") + " still need a name" if planned['street_id_original'].nunique() != 1 else "1 street still needs a name",
                            "Shown in red. Naming them lets the other streets derive their names (3-xyz).", planned["street_id"],
                            count=int(planned["street_id_original"].nunique())))
    unc = [int(r.street_id) for r in s.itertuples() if int(r.street_id) in namable and not r.has_own_name and not isinstance(r.branch_name, str)
           and not bool(r.to_be_named)]
    if unc:
        out.append(_warning("uncovered", "error", _n(len(unc), "street") + (" needs" if len(unc) == 1 else " need") + " a name of its own" if len(unc) == 1 else _n(len(unc), "street") + " need a name of their own",
                            "No street can give them a derived name: name them by hand.", unc))
    if unconnectable_originals:
        ids = s[s["street_id_original"].isin(unconnectable_originals)]["street_id"]
        out.append(_warning("unconnected", "warning", _n(len(unconnectable_originals), "named street") + " cannot reach the reference",
                            "Not connected to the reference point through the road network.", ids))
    base_name = {int(o): g.sort_values("street_part")["street_name"].iloc[0] for o, g in s[s["has_own_name"]].groupby("street_id_original")}
    by_name: dict = {}
    for o, nm in base_name.items():
        by_name.setdefault(str(nm).strip().lower(), []).append(o)
    for nm, origs in by_name.items():
        if len(origs) > 1:
            ids = s[s["street_id_original"].isin(origs)]["street_id"]
            out.append(_warning("duplicate_name", "warning", f"Name used for {len(origs)} streets: {base_name[origs[0]]}",
                                f"Streets {origs} share one name: give each its own, or merge them.", ids))
    order = {"error": 0, "warning": 1}
    return sorted(out, key=lambda w: (order.get(w["severity"], 2), w["code"]))


def compute_network(base: BaseData, project: Project, progress=None, roads_report: dict | None = None, hint_extra: set | None = None) -> NetworkState:
    """Run steps 1-5 of the module docstring.

    Args:
        base: The base data.
        project: The project (reference, names, settings).
        progress: Optional callable ``progress(step: str)`` called as the computation advances.
        roads_report: ``RoadsResult.report`` when ``base`` contains drawn roads; adds the warnings about them.
        hint_extra: ``NetworkState.extra`` of the previous computation: the extra streets to name found then are tried first, which
            saves the repeated rounds after an edit (the result is the same: rounds still continue while streets are uncovered).

    Returns:
        The :class:`NetworkState`.

    Raises:
        ValueError: If no reference node has been chosen.
    """
    if not project.reference:
        raise ValueError("choose the reference node first")
    st = project.settings
    say = progress or (lambda s: None)
    base = restrict_to_aoi(base, int(project.reference["node"]))
    street_edits, edge_edits = resolve_edit_names(base, project)
    base = BaseData(base.nodes, base.edges, base.streets, street_edits, edge_edits, base.buildings, base.area, base.points)
    ref_node = int(project.reference["node"])
    touched = set(base.edges.index.get_level_values("u")) | set(base.edges.index.get_level_values("v"))
    if ref_node not in base.nodes.index or ref_node not in touched:         # an edit removed the node: the reference stays on a node of the network
        ref_node = snap_reference(base, float(project.reference["lat"]), float(project.reference["lon"]))["node"]
        say("the reference node no longer exists: using the nearest node")
    ref_xy = (float(base.nodes.loc[ref_node, "x"]), float(base.nodes.loc[ref_node, "y"]))
    common = dict(edge_edits_csv_path=str(base.edge_edits) if base.edge_edits else None, origin=ref_xy)

    path_numbering.NO_CUT_STREETS = {int(x) for x in project.roads.get("no_cut", [])}
    say("applying the community edits")
    nodes0, edges0, streets0 = street_editor.apply_street_edits(
        base.nodes, base.edges, base.streets, str(base.street_edits), numbering_mode="radial", **common)
    all_attrs = project.roads.get("attrs") or {}
    attrs = {int(k): v for k, v in all_attrs.items() if ":" not in str(k)}                    # one street (all its parts)
    part_attrs = {tuple(int(x) for x in k.split(":")): v for k, v in all_attrs.items() if ":" in str(k)}   # one part of a cut street
    if attrs:
        # the editors set rank / drivable / paved on the street ids they see (the ids after the community edits): apply them there, and carry them
        # to the edges of the base network (which still have the ids from before the edits) that the routing below starts from
        edges0, streets0 = apply_attributes(edges0, streets0, attrs)
        tmp = base.edges.copy()
        post = edges0["street_id"].reindex(tmp.index)
        tmp["street_id"] = post.where(post.notna(), tmp["street_id"])
        tmp, _ = apply_attributes(tmp, base.streets, attrs)
        tmp["street_id"] = base.edges["street_id"]
        base = dataclasses.replace(base, edges=tmp)
    named_ways = osm_named_ways(project.input_dir / "streets.osm")
    protected = {int(k) for k in project.names}
    if base.street_edits and Path(str(base.street_edits)).exists():
        se = pd.read_csv(base.street_edits, dtype=str).fillna("")
        for c in ("old_street_id", "new_street_id"):
            if c in se.columns and "street_name" in se.columns:
                protected |= {int(x) for x, n in zip(se[c], se["street_name"]) if n.strip() and x.strip().isdigit()}
    edges0, streets0 = drop_inferred_part_names(edges0, streets0, named_ways, protected)
    assigned = {int(k): ('' if v == NO_NAME else v) for k, v in project.names.items()}   # NO_NAME: the street is deliberately left without a name
    streets0 = streets0.copy()
    for sid, name in assigned.items():
        streets0.loc[streets0["street_id"] == sid, "street_name"] = name
    named_ids = {int(s) for s, n in zip(streets0["street_id"], streets0["street_name"]) if is_named(n)}

    say("planning which streets still need a name")
    aoi = aoi_polygon(base)
    inside0 = _street_ids_inside(edges0, aoi) if aoi is not None else None             # only the streets of the area of interest need names
    plan = naming_plan.plan_minimal_naming(
        nodes0, edges0, streets0, max_levels=st.max_levels, targets=None if inside0 is None else sorted(inside0), method=st.plan_method, time_limit_s=st.time_limit_s,
        reference_node=ref_node if st.connect_backbone else None, second_level_max_drive_m=st.second_level_max_drive_m)
    planned = {int(s) for s in plan.chosen["street_id"]}
    first_order = {int(s): int(o) for s, o in zip(plan.chosen["street_id"], plan.chosen["order"])}
    reasons = {int(s): r for s, r in zip(plan.chosen["street_id"], plan.chosen["reason"])}
    notes = list(plan.notes)
    unconnectable = [int(s) for s in plan.unconnectable]
    extra: set = {int(x) for x in (hint_extra or ())} & {int(x) for x in streets0["street_id"]} - planned - named_ids

    def run(planned_: set, extra_: set, label: str) -> tuple:
        """One routing / cutting / name derivation pass for a set of streets that are to be named."""
        backbone = named_ids | planned_ | extra_
        say(f"routing through the named streets and assigning the ids ({label})")
        nodes1, edges1, streets1 = street_editor.apply_street_edits(
            base.nodes, base.edges, base.streets, str(base.street_edits), numbering_mode="path",
            backbone_streets=backbone, **common)
        edges1, streets1 = drop_inferred_part_names(edges1, streets1, named_ways, protected)
        edges1, streets1 = apply_part_attributes(edges1, streets1, part_attrs)          # what the editors chose for one part of a cut street
        streets1 = dominant_rank(edges1, streets1)                                     # the cut parts get the rank that has most of their length
        streets1 = _overlay_names(streets1, assigned, first_part_only=True)
        orig = streets1["street_id_original"].astype(int)
        part = streets1["street_part"].astype(int)
        own = streets1["street_name"].map(is_named)
        planned_parts = streets1.loc[orig.isin(planned_ | extra_) & ~own & (part == 0), "street_id"]
        say("deriving the sub-street names")
        kw = dict(number_divisor_m=st.branch_step_m, max_levels=st.max_levels, second_level_max_drive_m=st.second_level_max_drive_m)
        edges2, streets2 = branches.assign_branch_names(nodes1, edges1, streets1, planned_streets=planned_parts, **kw)
        # the other parts of a street to name (or named by the app) derive their names from the street they branch from; those that cannot
        # take the name of their street (``(B)``) or are to be named with it
        left = streets2[(streets2["street_part"].astype(int) > 0) & ~streets2["street_name"].map(is_named) & ~streets2["display_name"].map(lambda n: isinstance(n, str) and bool(n) and not n.startswith("#"))
                        & streets2["street_id_original"].astype(int).isin(set(assigned) | planned_ | extra_)]
        if len(left):
            more = left.loc[~left["street_id_original"].astype(int).isin(set(assigned)), "street_id"]
            for sid_, o_, p_ in zip(left["street_id"], left["street_id_original"], left["street_part"]):
                if int(o_) in assigned:
                    streets1.loc[streets1["street_id"] == sid_, "street_name"] = branches._part_name(assigned[int(o_)], p_)
            edges2, streets2 = branches.assign_branch_names(nodes1, edges1, streets1, planned_streets=pd.concat([planned_parts, more]), **kw)
        namable = set(branches.branch_parents(nodes1, edges2, streets2))
        has_name = streets2["display_name"].map(lambda n: isinstance(n, str) and bool(n))
        inside2 = _street_ids_inside(edges2, aoi) if aoi is not None else None
        unnamed = sorted(int(s) for s, h in zip(streets2["street_id"], has_name) if int(s) in namable and not h and (inside2 is None or int(s) in inside2))
        return nodes1, edges1, edges2, streets2, namable, unnamed

    for it in range(1, 5):
        nodes1, edges1, edges2, streets2, namable, unnamed = run(planned, extra, f"round {it}")
        if not unnamed:
            break
        say("some streets are still uncovered after the cutting; planning them too")
        tmp = streets2.copy()
        tmp["street_name"] = [n if is_named(n) else (placeholder(s) if tb else None)
                              for s, n, tb in zip(tmp["street_id"], tmp["street_name"], tmp["to_be_named"])]
        p2 = naming_plan.plan_minimal_naming(nodes1, edges2, tmp, max_levels=st.max_levels, targets=unnamed, method=st.plan_method,
                                             time_limit_s=st.time_limit_s, reference_node=ref_node if st.connect_backbone else None,
                                             second_level_max_drive_m=st.second_level_max_drive_m)
        by_id = streets2.set_index("street_id")["street_id_original"]
        new = {int(by_id[int(s)]) for s in p2.chosen["street_id"]} - planned - extra
        if not new:                                        # nothing can give them a name: they simply need a name of their own
            new = {int(by_id[int(s)]) for s in unnamed} - planned - extra
            if not new:
                notes.append(f"{len(unnamed)} streets cannot be covered: {unnamed[:10]}")
                break
            for o in new:
                reasons.setdefault(o, "own name needed")
        extra |= new
        for o in new:
            reasons.setdefault(o, "after-cutting")

    if not unnamed:                                      # a street that other planned or named streets would give a name to need not be named by hand
        drop = _redundant_planned(nodes1, edges2, streets2, planned | extra, first_order, st, aoi, ref_node)
        if drop:
            say(f"{len(drop)} streets will get derived names from the other streets to name: leaving them out of the list")
            trial = run(planned - drop, extra - drop, "final round")
            if not trial[5]:
                planned, extra = planned - drop, extra - drop
                nodes1, edges1, edges2, streets2, namable, unnamed = trial
    say("collecting the results")
    sg = pipeline.streets_gdf_from_edges(edges2, streets2)
    dist, _ = path_numbering.node_distances(nodes0, edges0, ref_xy, backbone_streets=named_ids | planned | extra)
    allp = planned | extra
    rows = []
    s2 = streets2.set_index("street_id")
    sg_idx = sg.set_index("street_id")
    for o in sorted(allp, key=lambda x: (first_order.get(x, 10**6), x)):
        parts = s2[s2["street_id_original"] == o]
        covers = int(streets2["branch_root_id"].isin(parts.index).sum())
        geom = shapely.union_all([sg_idx.geometry[i] for i in parts.index if i in sg_idx.index])
        c = gpd.GeoSeries([geom.interpolate(0.5, normalized=True) if geom.geom_type == "LineString" else geom.centroid],
                          crs=sg.crs).to_crs(4326).iloc[0]
        rows.append({"order": len(rows) + 1, "street_id_original": o, "street_ids": ";".join(str(i) for i in parts.index),
                     "street_rank": int(parts["street_rank"].min()), "highway": parts["highway"].iloc[0] if "highway" in parts else None,
                     "length_m": round(float(parts["length_m"].sum()), 1), "covers": covers,
                     "reason": reasons.get(o, "coverage"), "lat": round(c.y, 6), "lon": round(c.x, 6)})
    plan_df = pd.DataFrame(rows, columns=["order", "street_id_original", "street_ids", "street_rank", "highway", "length_m",
                                          "covers", "reason", "lat", "lon"])
    mapping = streets2[["street_id", "street_id_original", "street_part", "street_name"]].sort_values(["street_id_original", "street_part"])
    cuts = _cut_points(edges2, nodes1)
    own2 = streets2["street_name"].map(is_named)
    der = streets2["branch_name"].map(lambda n: isinstance(n, str))
    namable_need = namable if aoi is None else namable & _street_ids_inside(edges2, aoi)     # outside the area nothing is required
    stats = {
        "streets": int(len(streets2)), "original_ids": int(streets2["street_id_original"].nunique()), "cut_points": int(len(cuts)),
        "new_ids_from_cutting": int((streets2["street_part"] > 0).sum()),
        "named": int((own2).sum()), "named_by_community_in_app": int(len(assigned)), "derived": int((der & ~own2).sum()),
        "to_be_named": int(len(allp)), "namable": int(len(namable)),
        "uncovered": int(sum(1 for s, n, b, tb in zip(streets2["street_id"], streets2["street_name"], streets2["branch_name"], streets2["to_be_named"])
                             if int(s) in (namable if aoi is None else namable_need) and not is_named(n) and not isinstance(b, str) and not tb)),
        "rounds": it,
    }
    warnings = build_warnings(streets2, namable_need, unconnectable) + (roads_warnings(roads_report, streets2) if roads_report else [])
    parts = disconnected_parts(base)
    if parts:
        ids = [int(x) for x in streets2.loc[streets2["street_id_original"].isin({s_ for p_ in parts for s_ in p_["street_ids"]}), "street_id"]]
        warnings.append(_warning("disconnected", "error", f"The network is not connected: {len(parts)} separate part{'s' if len(parts) > 1 else ''} inside the area",
                                 "Streets of a part that is cut off from the main network cannot be numbered or named from it. Connect them (Edit streets tab, shown in magenta).",
                                 ids, count=len(parts)))
    warnings.sort(key=lambda w: ({"error": 0, "warning": 1}.get(w["severity"], 2), w["code"]))
    return NetworkState(nodes1, edges2, streets2, sg, dist, {"node": ref_node, "x": ref_xy[0], "y": ref_xy[1]}, plan_df, mapping, cuts, stats,
                        notes, warnings, aoi, set(extra))


def street_status(state: NetworkState, project: Project) -> pd.DataFrame:
    """Naming status of every street.

    Args:
        state: Computed network.
        project: The project (to tell community-assigned names from existing ones).

    Returns:
        A table indexed by street id with ``status`` (``named``, ``derived``, ``planned``, ``uncovered``, ``other``),
        ``source`` (``assigned``, ``existing``, ``derived``) and ``level``.
    """
    namable = set(branches.branch_parents(state.nodes, state.edges, state.streets))
    if state.aoi is not None:
        namable &= _street_ids_inside(state.edges, state.aoi)
    assigned = set(project.names)
    out = []
    for _, r in state.streets.iterrows():
        sid = int(r["street_id"])
        if is_named(r["street_name"]):
            src = "assigned" if int(r["street_id_original"]) in assigned else "existing"
            out.append((sid, "named", src, 0))
        elif isinstance(r["branch_name"], str):
            out.append((sid, "derived", "derived", int(r["branch_level"])))
        elif bool(r["to_be_named"]):
            out.append((sid, "planned", "planned", None))
        elif sid in namable:
            out.append((sid, "uncovered", None, None))
        else:
            out.append((sid, "other", None, None))
    return pd.DataFrame(out, columns=["street_id", "status", "source", "level"]).set_index("street_id")


def _tri(values, lengths) -> bool | None:
    """The flag of a street from the flags of its edges: True when any known edge is True (drivable) or most of the known length is.

    Args:
        values: Nullable booleans of the edges (or None).
        lengths: Length of every edge.

    Returns:
        True, False or None when no edge knows.
    """
    if values is None:
        return None
    known = values.notna().to_numpy()
    if not known.any():
        return None
    v = values.fillna(False).astype(bool).to_numpy()
    ln = lengths.to_numpy()
    return bool((v * ln * known).sum() >= 0.5 * (ln * known).sum())


def network_geojson(state: NetworkState, project: Project) -> dict:
    """The computed network as GeoJSON for the map.

    Args:
        state: Computed network.
        project: The project.

    Returns:
        ``{"streets": FeatureCollection, "cuts": FeatureCollection, "reference": {...}, "plan": [...], "stats": {...}}``.
    """
    status = street_status(state, project)
    aoi = state.aoi
    sg = state.streets_gdf.to_crs(4326)
    inside_lines: dict = {}
    inside_nodes: set = set()
    drawn_refs: set = set()
    if aoi is not None:                                              # edges are never cropped: an edge that touches the area is complete, with its outside node
        poly = gpd.GeoSeries([aoi], crs=4326).to_crs(state.edges.crs).iloc[0]
        ed = state.edges.reset_index()
        ed = ed[(ed["key"] < roads_mod.SYNTHETIC_KEY) & ed["street_id"].notna() & ed.geometry.intersects(poly)]
        inside_nodes = set(ed["u"].astype(int)) | set(ed["v"].astype(int))
        drawn_refs = {roads_mod.edge_ref(u, v, k) for u, v, k in zip(ed["u"], ed["v"], ed["key"])}
        ed4 = gpd.GeoSeries(ed.geometry.to_numpy(), crs=state.edges.crs).to_crs(4326)
        parts: dict = {}
        for sid_, g_ in zip(ed["street_id"].astype(int), ed4):
            parts.setdefault(sid_, []).append(g_)
        for sid_, lst in parts.items():
            m = shapely.ops.linemerge(shapely.MultiLineString(lst)) if len(lst) > 1 else lst[0]
            inside_lines[sid_] = shapely.set_precision(m, 1e-6)
    d = state.distance
    nodes_of: dict = {}
    for (u, v, _k), sid in state.edges["street_id"].dropna().items():
        nodes_of.setdefault(int(sid), set()).update((u, v))
    fl = state.edges.reset_index()
    fl = fl[(fl["key"] < roads_mod.SYNTHETIC_KEY) & fl["street_id"].notna()]
    fl = fl.assign(_l=fl.geometry.length)
    flags = {}
    for k, g in fl.groupby("street_id"):
        flags[int(k)] = (_tri(g["drivable_info"] if "drivable_info" in g else None, g["_l"]), _tri(g["paved"] if "paved" in g else None, g["_l"]))
    feats = []
    for _, r in sg.iterrows():
        sid = int(r["street_id"])
        dv = [d.get(n) for n in nodes_of.get(sid, ()) if pd.notna(d.get(n))]
        props = {"id": sid, "original": int(r["street_id_original"]), "part": int(r["street_part"]),
                 "name": r["street_name"] if is_named(r["street_name"]) else None,
                 "display": r["display_name"] if isinstance(r["display_name"], str) else None,
                 "status": status.loc[sid, "status"], "source": status.loc[sid, "source"],
                 "level": None if pd.isna(status.loc[sid, "level"]) else int(status.loc[sid, "level"]),
                 "rank": int(r["street_rank"]), "highway": r.get("highway"), "drivable": flags.get(sid, (None, None))[0], "paved": flags.get(sid, (None, None))[1], "length": round(float(r["length_m"]), 1),
                 "d0": round(min(dv), 1) if dv else None, "d1": round(max(dv), 1) if dv else None,
                 "parent": None if pd.isna(r["branch_parent_id"]) else int(r["branch_parent_id"]),
                 "root": None if pd.isna(r["branch_root_id"]) else int(r["branch_root_id"])}
        props = {k: (None if isinstance(v, float) and v != v else v) for k, v in props.items()}
        g = shapely.set_precision(r.geometry, 1e-6)
        if aoi is not None:                                          # the streets of the area are shown with all their edges that touch it, whole
            g = inside_lines.get(sid)
            if g is None:
                continue
        feats.append({"type": "Feature", "properties": props, "geometry": shapely.geometry.mapping(g)})
    cuts = [{"type": "Feature", "properties": {"node": int(r["node"])}, "geometry": shapely.geometry.mapping(r.geometry)}
            for _, r in state.cuts.iterrows() if aoi is None or int(r["node"]) in inside_nodes]
    rlon, rlat = Transformer.from_crs(state.nodes.crs, 4326, always_xy=True).transform(state.reference["x"], state.reference["y"])
    junctions = _junctions(state, status)
    if aoi is not None:
        kept = []
        for f in junctions["features"]:                                # only the roads that are drawn on the map are listed at a node
            if int(f["properties"]["node"]) not in inside_nodes:
                continue
            f["properties"]["spokes"] = [sp for sp in f["properties"]["spokes"] if sp["ref"] in drawn_refs]
            sp = f["properties"]["spokes"]
            if len(sp) >= 3 or len({x["id"] for x in sp}) >= 2:
                kept.append(f)
        junctions["features"] = kept
    return {"junctions": junctions, "streets": {"type": "FeatureCollection", "features": feats}, "cuts": {"type": "FeatureCollection", "features": cuts},
            "reference": {"lat": rlat, "lon": rlon, "node": state.reference["node"]},
            "plan": state.plan.to_dict("records"), "stats": state.stats, "notes": state.notes, "warnings": state.warnings}


def _junctions(state: NetworkState, status: pd.DataFrame) -> dict:
    """The nodes where roads meet, with one spoke per road leaving the node (for the node popup of the Street IDs tab).

    Args:
        state: Computed network.
        status: ``street_status`` table (index = street id).

    Returns:
        FeatureCollection of points (WGS84). ``properties``: ``node``, ``d`` (distance to the reference, or None) and ``spokes``
        = list of ``{"id", "original", "name", "status", "bearing", "length"}`` (bearing in degrees clockwise from north).
    """
    import math

    sg = state.streets_gdf
    disp = {int(r["street_id"]): (r["display_name"] if isinstance(r["display_name"], str) else None) for _, r in sg.iterrows()}
    orig = {int(r["street_id"]): int(r["street_id_original"]) for _, r in sg.iterrows()}
    spokes: dict = {}
    for (u, v, _k), r in state.edges.iterrows():
        sid = r.get("street_id")
        if pd.isna(sid) or r.geometry is None or r.geometry.is_empty:
            continue
        sid = int(sid)
        c = list(r.geometry.coords)
        if len(c) < 2:
            continue
        for node, (a, b) in ((u, (c[0], c[1])), (v, (c[-1], c[-2]))):
            bearing = (math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) + 360) % 360
            lst = spokes.setdefault(node, [])
            if any(o["ref"] == roads_mod.edge_ref(u, v, _k) for o in lst):
                continue                                      # the same road stored in both directions
            lst.append({"id": sid, "ref": roads_mod.edge_ref(u, v, _k), "original": orig.get(sid, sid), "name": disp.get(sid),
                                                "status": status.loc[sid, "status"] if sid in status.index else None,
                                                "bearing": round(bearing, 1), "length": round(float(r.geometry.length), 1)})
    pts = state.nodes.to_crs(4326)
    d = state.distance
    feats = []
    for node, sp in spokes.items():
        if len(sp) < 3 and len({s["id"] for s in sp}) < 2:
            continue
        g = pts.geometry.get(node)
        if g is None:
            continue
        dv = d.get(node)
        feats.append({"type": "Feature", "properties": {"node": int(node), "d": None if dv is None or pd.isna(dv) else round(float(dv), 1), "spokes": sp},
                      "geometry": {"type": "Point", "coordinates": [round(g.x, 7), round(g.y, 7)]}})
    return {"type": "FeatureCollection", "features": feats}


def _street_geometry(row) -> tuple:
    """The line to project on for a street row and its position profile.

    Args:
        row: A row of ``streets_gdf``.

    Returns:
        ``(geometry, profile)`` - for a street in several pieces the piece is chosen later and the profile is None.
    """
    geom = row.geometry
    return geom, (None if geom.geom_type == "MultiLineString" else row.get("path_profile"))


def _side_at(geom, along: float, point) -> str:
    """Side of a street, at a given position along it, on which a point lies.

    Args:
        geom: Street line in its numbering direction.
        along: Position along the line in metres.
        point: The point (a house).

    Returns:
        ``"left"`` or ``"right"`` looking in the direction of numbering.
    """
    a = geom.interpolate(max(along - 1.0, 0.0))
    b = geom.interpolate(min(along + 1.0, geom.length))
    here = geom.interpolate(along)
    tx, ty = b.x - a.x, b.y - a.y
    return "left" if tx * (point.y - here.y) - ty * (point.x - here.x) > 0 else "right"


def side_from_azimuth(geom, along: float, azimuth_deg: float, min_angle_deg: float = 20.0) -> str | None:
    """Side of a street on which a building lies, from the azimuth the surveyor pointed from the street to the door.

    The position of a building can be a few metres off, the direction to its door is much more reliable.

    Args:
        geom: Street line in its numbering direction (projected, x east, y north).
        along: Position along the line in metres where the surveyor stood.
        azimuth_deg: Bearing from the street to the door, degrees clockwise from north.
        min_angle_deg: When the azimuth is closer than this to the direction of the street it says nothing about the side.

    Returns:
        ``"left"`` / ``"right"`` looking in the direction of numbering, or None when the azimuth is missing or almost parallel to the street.
    """
    if azimuth_deg is None or azimuth_deg != azimuth_deg:
        return None
    a = geom.interpolate(max(along - 1.0, 0.0))
    b = geom.interpolate(min(along + 1.0, geom.length))
    tx, ty = b.x - a.x, b.y - a.y
    n = math.hypot(tx, ty)
    if n == 0:
        return None
    az = math.radians(float(azimuth_deg))
    vx, vy = math.sin(az), math.cos(az)
    cross = (tx * vy - ty * vx) / n
    if abs(cross) < math.sin(math.radians(min_angle_deg)):
        return None
    return "left" if cross > 0 else "right"


def snap_to_street(state: NetworkState, lat: float, lon: float, max_m: float = 60.0) -> dict | None:
    """Snap a clicked position to the nearest point of a street (to choose a house's road point).

    Args:
        state: The computed network.
        lat: Latitude of the click.
        lon: Longitude of the click.
        max_m: Largest distance from the click to a street.

    Returns:
        ``{"street_id", "street", "lat", "lon", "distance_m"}`` of the snapped point, or None when no street is that close.
    """
    sg = state.streets_gdf
    namable = set(branches.branch_parents(state.nodes, state.edges, state.streets))
    sg = sg[sg["street_id"].astype(int).isin(namable)].reset_index(drop=True)
    x, y = Transformer.from_crs(4326, sg.crs, always_xy=True).transform(lon, lat)
    p = shapely.geometry.Point(x, y)
    (idx_in, idx_tree), dist = sg.sindex.nearest([p], return_all=False, max_distance=max_m, return_distance=True)
    if len(idx_tree) == 0:
        return None
    row = sg.iloc[int(idx_tree[0])]
    q = shapely.ops.nearest_points(row.geometry, p)[0]
    slon, slat = Transformer.from_crs(sg.crs, 4326, always_xy=True).transform(q.x, q.y)
    disp = state.streets.set_index("street_id")["display_name"].get(int(row["street_id"]))
    return {"street_id": int(row["street_id"]), "street": disp if isinstance(disp, str) else None, "lat": float(slat), "lon": float(slon),
            "distance_m": float(dist[0])}


def reference_point_rows(base: BaseData, ranks: dict | None = None) -> gpd.GeoDataFrame:
    """The important points of the community that get an address: those inside the area of interest (all of them without one).

    Args:
        base: The base data (``points``, ``area``).
        ranks: Ranks chosen in the app (``{"POINT-n": rank}``), over the ``sign_rank`` column of the points file.

    Returns:
        WGS84 points with ``building_id`` (``POINT-<n>``, the position in ``input/points.gpkg``), ``use`` (= ``reference``), ``name``,
        ``info`` (what kind of point it is) and ``sign_rank`` (importance for the street signs: 1 landmark ... 3 local, 0 not on the signs).
        Empty when the project has no points.
    """
    cols = ["building_id", "use", "name", "info", "sign_rank", "geometry"]
    if base.points is None or base.points.empty:
        return gpd.GeoDataFrame({c: [] for c in cols}, geometry="geometry", crs=4326)
    p = base.points.to_crs(4326).reset_index(drop=True)
    geom = p.geometry.apply(lambda g: g if g.geom_type == "Point" else g.representative_point())
    info = []
    for _, r in p.iterrows():
        bits = [r.get("kind") if isinstance(r.get("kind"), str) else "", f"settlement {r['settlement']}" if isinstance(r.get("settlement"), str) and r["settlement"] else "",
                f"source {r['source']}" if isinstance(r.get("source"), str) and r["source"] else ""]
        info.append("; ".join(b for b in bits if b))
    out = gpd.GeoDataFrame({"building_id": [f"POINT-{i + 1}" for i in range(len(p))], "use": "reference",
                            "name": [n if isinstance(n, str) else "" for n in p.get("name", [""] * len(p))], "info": info,
                            "sign_rank": [int(v) if pd.notna(v) else 3 for v in (p["sign_rank"] if "sign_rank" in p.columns else [3] * len(p))]},
                           geometry=geom.to_numpy(), crs=4326)
    if ranks:
        out["sign_rank"] = [int(ranks.get(b, r)) for b, r in zip(out["building_id"], out["sign_rank"])]
    aoi = aoi_polygon(base)
    return out if aoi is None else out[out.geometry.within(aoi)].reset_index(drop=True)


def outside_point_rows(base: BaseData, ranks: dict | None = None) -> gpd.GeoDataFrame:
    """The reference points outside the area of interest that are on the street signs (``sign_rank`` above 0).

    Args:
        base: The base data.
        ranks: Ranks chosen in the app (``{"POINT-n": rank}``).

    Returns:
        WGS84 points like :func:`reference_point_rows` (all points, then only those outside the area with a rank above 0).
    """
    aoi = aoi_polygon(base)
    allp = reference_point_rows(BaseData(base.nodes, base.edges, base.streets, base.street_edits, base.edge_edits, base.buildings,
                                         None, base.points), ranks)                          # without the area: every point
    if aoi is None or allp.empty:
        return allp.iloc[0:0]
    out = allp[~allp.geometry.within(aoi)]
    return out[out["sign_rank"] > 0].reset_index(drop=True)


def add_outside_signs(base: BaseData, state: NetworkState, signs: gpd.GeoDataFrame, pois: list, ranks: dict | None) -> None:
    """Put the reference points outside the area on the signs along the way to them, with the distance on the larger road network.

    The route from the reference node to every outside point is the shortest one on the whole drivable network (not only the area); every
    intersection that has a sign and lies on it gets the point with the distance that is left from there.

    Args:
        base: The base data with all the roads (also outside the area).
        state: The computed network (reference node, node clusters).
        signs: The sign intersections (``node_id`` = node cluster).
        pois: The list of points per sign (changed in place).
        ranks: Ranks chosen in the app.
    """
    import networkx as nx

    pts = outside_point_rows(base, ranks)
    if pts.empty or not len(signs):
        return
    e = base.edges.reset_index()
    if "is_drivable" in e:
        e = e[e["is_drivable"].astype(bool)]
    G = nx.Graph()
    for u, v, ln in zip(e["u"], e["v"], e["length"].astype(float)):
        if not G.has_edge(u, v) or ln < G[u][v]["length"]:
            G.add_edge(u, v, length=ln)
    ref = int(state.reference["node"])
    if ref not in G:
        return
    cluster = state.nodes["node_id"].to_dict() if "node_id" in state.nodes.columns else {}
    sign_at = {int(n): i for i, n in enumerate(signs["node_id"])}
    ids = np.array(list(G.nodes))
    xy = base.nodes.loc[ids, ["x", "y"]].to_numpy()
    to_proj = Transformer.from_crs(4326, base.crs, always_xy=True)
    for bid_, nm_, rk_, g_ in zip(pts["building_id"], pts["name"], pts["sign_rank"], pts.geometry):
        x, y = to_proj.transform(g_.x, g_.y)
        d = np.hypot(xy[:, 0] - x, xy[:, 1] - y)
        j = int(d.argmin())
        if d[j] > OUTSIDE_SNAP_M:
            continue
        try:
            path = nx.shortest_path(G, ref, int(ids[j]), weight="length")
        except nx.NetworkXNoPath:
            continue
        cum = [0.0]
        for a, b in zip(path, path[1:]):
            cum.append(cum[-1] + G[a][b]["length"])
        total = cum[-1]
        for n, c in zip(path, cum):
            k = sign_at.get(int(cluster.get(n, -1)))
            if k is not None and total - c > 0:
                nxt = path[path.index(n) + 1]
                bx, by = base.nodes.loc[nxt, "x"] - base.nodes.loc[n, "x"], base.nodes.loc[nxt, "y"] - base.nodes.loc[n, "y"]
                pois[k].append({"id": bid_, "name": nm_, "rank": int(rk_), "distance_m": round(total - c, 0), "outside": True,
                                "bearing": round((math.degrees(math.atan2(bx, by)) + 360) % 360, 0)})


def compute_addresses(base: BaseData, state: NetworkState, project: Project) -> AddressState:
    """House numbers, complete addresses and street signs.

    Each house gets the road point used to number it: by default the nearest point of the nearest street within
    ``max_house_distance_m``; a point chosen by hand (``project.access``, any distance up to 60 m from a street) wins.
    From that point come the position along the street (the street's path-numbering profile) and the number; the side
    (odd on the left, even on the right) is the side of the street the *house* is on. Houses that share a street, side
    and number get a letter (``12A``, ``12B``). The address is ``"<number>, <street name>"`` with the own name, the derived
    name (``3-xyz``) or the stand-in ``#id`` while the street still has to be named (status ``pending_name``).

    Args:
        base: The base data (with ``buildings``: points or polygons; polygons are addressed at their centre).
        state: The computed network.
        project: The project (settings and chosen road points).

    Returns:
        The :class:`AddressState`.

    Raises:
        ValueError: If the project has no houses to address.
    """
    if base.buildings is None or base.buildings.empty:
        raise ValueError("this project has no houses to address (input/buildings.gpkg)")
    st = project.settings
    sg = state.streets_gdf.copy()
    namable = set(branches.branch_parents(state.nodes, state.edges, state.streets))
    sg = sg[sg["street_id"].astype(int).isin(namable)].reset_index(drop=True)
    refs = reference_point_rows(base, project.roads.get("point_rank"))                       # the important points inside the area are addressed like buildings
    allb = base.buildings.to_crs(4326)
    if len(refs):
        allb = gpd.GeoDataFrame(pd.concat([allb, refs], ignore_index=True), geometry="geometry", crs=4326)
    pts = allb.to_crs(state.nodes.crs).reset_index(drop=True)
    display_geom = pts.geometry.copy()                      # points or polygons, as surveyed
    pts["geometry"] = pts.geometry.centroid                 # a house is addressed at its centre
    for col in ("building_id", "address_id", "id"):
        if col in pts.columns:
            bid = [str(v) for v in pts[col]]
            break
    else:
        bid = [str(i) for i in range(len(pts))]
    to_proj = Transformer.from_crs(4326, state.nodes.crs, always_xy=True)
    manual = {i: project.access[b] for i, b in enumerate(bid) if b in project.access}

    (idx_in, idx_tree), dist = sg.sindex.nearest(pts.geometry, return_all=False, max_distance=st.max_house_distance_m, return_distance=True)
    n = len(pts)
    match = {int(i): (int(t), float(d)) for i, t, d in zip(idx_in, idx_tree, dist)}
    for i, a in manual.items():                             # a hand-chosen road point replaces the automatic match
        q = shapely.geometry.Point(*to_proj.transform(a["lon"], a["lat"]))
        (qi, qt), qd = sg.sindex.nearest([q], return_all=False, max_distance=60.0, return_distance=True)
        match.pop(i, None)
        if len(qt):
            match[i] = (int(qt[0]), float(qd[0]))
    azimuths = [float(v) if v is not None and v == v else float("nan") for v in pts["azimuth"]] if "azimuth" in pts.columns else None
    side_src = [None] * n
    number = [None] * n
    side = [None] * n
    along = [np.nan] * n
    sid_of = [None] * n
    dist_of = [np.nan] * n
    road_xy = [None] * n                                      # the point of the street the number is taken from
    for i, (t, dd) in match.items():
        row = sg.iloc[t]
        geom, profile = _street_geometry(row)
        house = pts.geometry.iloc[i]
        ref_pt = shapely.geometry.Point(*to_proj.transform(manual[i]["lon"], manual[i]["lat"])) if i in manual else house
        if geom.geom_type == "MultiLineString":
            geom = min(geom.geoms, key=lambda g: g.distance(ref_pt))
        a = geom.project(ref_pt)
        sd_loc = _side_at(geom, a, house) if i in manual else numbering._side_of_line(house, geom)
        sd_az = side_from_azimuth(geom, a, azimuths[i]) if azimuths is not None else None
        sd = sd_az or sd_loc                                  # the direction the surveyor pointed beats the (possibly shifted) position
        side_src[i] = "azimuth" if sd_az else "location"
        q = geom.interpolate(a)
        road_xy[i] = (q.x, q.y)
        pos = numbering.profile_position(profile, a) if isinstance(profile, str) and profile else a
        number[i] = numbering._house_number_from_distance(pos, st.house_step_m, sd)
        side[i], along[i], sid_of[i], dist_of[i] = sd, pos, int(row["street_id"]), float(house.distance(ref_pt))
    ok = [i for i in range(n) if number[i] is not None]
    labels = {}
    if ok:
        got = numbering.add_letters([number[i] for i in ok], [(sid_of[i], side[i], number[i]) for i in ok], [along[i] for i in ok])
        labels = dict(zip(ok, got))
    disp = state.streets.set_index("street_id")["display_name"].to_dict()
    address, status, street = [], [], []
    for i in range(n):
        if number[i] is None:
            address.append(None)
            status.append("no_street")
            street.append(None)
            continue
        name = disp.get(sid_of[i])
        if not isinstance(name, str) or not name:
            address.append(f"{labels[i]}, street {sid_of[i]}")
            status.append("no_name")
            street.append(None)
        else:
            address.append(f"{labels[i]}, {name}")
            status.append("pending_name" if "#" in name else "ok")
            street.append(name)
    houses = pts.copy()
    houses["geometry"] = display_geom
    houses = houses.to_crs(4326)
    houses["building_id"] = bid
    houses["address"], houses["number"], houses["street_id"], houses["street"] = address, [labels.get(i) for i in range(n)], sid_of, street
    houses["side"], houses["status"], houses["distance_m"] = side, status, dist_of
    houses["side_source"] = side_src
    back = Transformer.from_crs(state.nodes.crs, 4326, always_xy=True)
    rll = [back.transform(*xy) if xy else (None, None) for xy in road_xy]
    houses["road_lon"], houses["road_lat"] = [r[0] for r in rll], [r[1] for r in rll]
    houses["azimuth"] = pts["azimuth"].to_numpy() if "azimuth" in pts.columns else np.nan
    houses["source"] = ["manual" if i in manual else "auto" for i in range(n)]
    houses["access_lat"] = [manual[i]["lat"] if i in manual else None for i in range(n)]
    houses["access_lon"] = [manual[i]["lon"] if i in manual else None for i in range(n)]

    sig_streets = state.streets.copy()
    sig_streets["street_name"] = sig_streets["display_name"].where(sig_streets["display_name"].notna(), None)
    inter = street_signs.compute_intersections(state.nodes, state.edges, sig_streets).to_crs(4326)
    signs = gpd.GeoDataFrame({"node_id": inter["node_id"].astype(int), "labels": inter["sign_labels"], "label_ids": [[int(x) for x in str(s).split(";") if x] for s in inter["sign_street_ids"]], "rank": inter["intersection_rank"],
                              "type": inter["intersection_label"], "street_ids": inter["sign_street_ids"]}, geometry=inter.geometry, crs=4326)
    pois: list = [[] for _ in range(len(signs))]                      # the reference points on the signs: each goes to the nearest intersection
    far_points = 0
    if len(refs) and len(signs):
        rr = refs[refs["sign_rank"] > 0].to_crs(state.nodes.crs)
        sp = signs.to_crs(state.nodes.crs)
        for bid_, nm_, rk_, g_ in zip(rr["building_id"], rr["name"], rr["sign_rank"], rr.geometry):
            d_ = sp.geometry.distance(g_)
            j = int(d_.values.argmin())
            if d_.iloc[j] <= SIGN_POI_MAX_M:
                q_ = sp.geometry.iloc[j]
                pois[j].append({"id": bid_, "name": nm_, "rank": int(rk_), "distance_m": round(float(d_.iloc[j]), 0),
                                "bearing": round((math.degrees(math.atan2(g_.x - q_.x, g_.y - q_.y)) + 360) % 360, 0)})
            else:
                far_points += 1
    add_outside_signs(base, state, signs, pois, project.roads.get("point_rank"))
    pois = [sorted(x, key=lambda p_: (p_["rank"], p_.get("distance_m", 0), p_["name"])) for x in pois]
    signs["pois"] = pois
    jfeat = _junctions(state, street_status(state, project))["features"]
    cl = state.nodes["node_id"].to_dict() if "node_id" in state.nodes.columns else {}
    by_cluster: dict = {}
    for f in jfeat:
        by_cluster.setdefault(int(cl.get(f["properties"]["node"], -1)), []).extend(f["properties"]["spokes"])
    disp_ = state.streets.set_index("street_id")["display_name"].to_dict()
    signs["spokes"] = [[{"id": int(sp["id"]), "bearing": sp["bearing"], "name": disp_.get(int(sp["id"])) if isinstance(disp_.get(int(sp["id"])), str) else None}
                        for sp in by_cluster.get(int(n_), [])] for n_ in signs["node_id"]]
    counts = pd.Series(status).value_counts().to_dict()
    stats = {"houses": n, "reference_far_from_signs": far_points, "addressed": int(counts.get("ok", 0)), "pending_name": int(counts.get("pending_name", 0)),
             "no_name": int(counts.get("no_name", 0)), "no_street": int(counts.get("no_street", 0)), "signs": int(len(signs)),
             "manual_road_points": int(len(manual))}
    warns = []
    if stats["no_street"]:
        warns.append({"code": "no_street", "severity": "warning", "title": f"{stats['no_street']} houses are too far from any road",
                      "message": f"They are farther than {int(st.max_house_distance_m)} m from every drivable street, so they have no address. "
                                 "Click a house and choose the road point to use for its number (the nearest road may be missing in OSM), "
                                 "or raise the distance in the settings.",
                      "buildings": [bid[i] for i, s_ in enumerate(status) if s_ == "no_street"], "count": stats["no_street"], "streets": []})
    if stats["pending_name"]:
        warns.append({"code": "pending_name", "severity": "warning", "title": f"{stats['pending_name']} houses are on a street that is still to be named",
                      "message": "Their address shows #<street id> until the street gets a name; naming it (and computing again) fills it in.",
                      "buildings": [bid[i] for i, s_ in enumerate(status) if s_ == "pending_name"], "count": stats["pending_name"],
                      "streets": sorted({sid_of[i] for i, s_ in enumerate(status) if s_ == "pending_name"})})
    if stats["no_name"]:
        warns.append({"code": "no_name", "severity": "error", "title": f"{stats['no_name']} houses are on a street without any name",
                      "message": "The street has neither a name, a derived name nor a planned name: see the warnings of the Names tab.",
                      "buildings": [bid[i] for i, s_ in enumerate(status) if s_ == "no_name"], "count": stats["no_name"],
                      "streets": sorted({sid_of[i] for i, s_ in enumerate(status) if s_ == "no_name"})})
    return AddressState(houses, signs, stats, warns)
