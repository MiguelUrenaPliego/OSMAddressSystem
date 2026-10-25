"""Point-of-interest direction signs: "how do I get to the hospital?"

Takes a small JSON file of points/polygons of interest (a school, a
health centre, a whole town's own AOI polygon, ...) and, for every real
intersection within a distance-dependent buffer of each one, works out:

1. **Which street's own sign (if any) this POI belongs on.** Reuses
   ``street_signs.compute_intersections``' own choice of which street(s)
   get a sign at a given intersection (see that module's docstring: the
   lower-rank street(s), except at a "pure" intersection where every
   road gets one). ``routing.py`` finds the real routing-weighted-
   shortest route from the intersection to the POI; whichever street
   that route LEAVES the intersection on is the one the POI gets added
   to - if that street doesn't get a sign at this intersection at all
   (e.g. the route leaves via the dominant primary road at a
   primary-secondary intersection), the POI gets no sign here, full
   stop, exactly like a real signpost never bothers pointing you further
   down the road you're already on.

2. **How much detail that entry gets.** Each POI has its own
   ``sign_type`` (1 = name + icon + distance, 2 = name + icon, 3 = icon
   only - see the module-level type table); each road CATEGORY has its
   own cap (``DEFAULT_ROAD_MAX_SIGN_TYPE`` - primary caps at 1, everything
   else at 2, so only a main road ever gets a distance plate). The
   entry actually shown is ``max(poi.sign_type, road_cap)`` - the LESS
   detailed of the two always wins, since a bigger number here means
   less information.

``load_pois``/``PointOfInterest`` parse the JSON; ``merge_nearby_pois``
optionally collapses same-type POIs (e.g. a school mapped as several
separate OSM buildings) that sit close together into one, at their
combined centroid, so they don't each demand their own sign;
``compute_poi_signs`` does the routing and sign assignment. Rendering
lives in ``street_signs.build_street_signs_map`` (pass its
``poi_signs``/``pois`` arguments) - the POI-signs map is part of the
same street-signs map, not a separate one.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace

import geopandas as gpd
import pandas as pd
from shapely.geometry import shape

from . import routing
from .street_signs import rank_to_category

# Each road category's own cap on how detailed a POI entry can be on its
# sign - only a primary road ever gets the full name+icon+distance plate;
# every other category is capped at name+icon (no distance).
DEFAULT_ROAD_MAX_SIGN_TYPE = {
    "primary": 1,
    "secondary": 2,
    "tertiary": 2,
    "other": 2,
}

# 1 = name + icon + distance (km); 2 = name + icon; 3 = icon only.
_SIGN_TYPE_MIN, _SIGN_TYPE_MAX = 1, 3


@dataclass
class PointOfInterest:
    """A point of interest that can appear on direction signs.

    Attributes:
        name: Display name.
        description: Optional description.
        icon: Emoji or icon shown on the sign.
        sign_type: Sign style (1 name+distance, 2 name, 3 icon only).
        max_distance: Maximum routing distance in metres at which it is signed.
        node_rank: Minimum intersection rank at which it is signed.
        min_distance: Minimum distance in metres before it is signed.
        detail_distance: Distance below which the detailed sign is used.
        detail_node_rank: Intersection rank for the detailed sign.
        geometry: Shapely geometry in EPSG:4326.
    """
    name: str
    description: str | None
    icon: str
    sign_type: int
    max_distance: float
    node_rank: int
    min_distance: float = 0.0
    detail_distance: float | None = None
    detail_node_rank: int | None = None
    geometry: object = None  # shapely geometry, EPSG:4326

    @property
    def is_polygon(self) -> bool:
        """Whether the POI geometry is a polygon or multipolygon."""
        return self.geometry.geom_type in ("Polygon", "MultiPolygon")


def load_pois(json_path: str) -> list[PointOfInterest]:
    """Load POIs from a JSON file.

    Each entry: ``name``, ``description``, ``icon``, ``sign_type``
    (1-3), ``max_distance`` (m), ``node_rank``, and optionally
    ``min_distance`` (m, default 0), ``detail_distance`` (m),
    ``detail_node_rank``, plus a GeoJSON-style ``geometry`` (``Point``
    or ``Polygon``, EPSG:4326 lon/lat coordinates).
    """
    records = json.loads(open(json_path, encoding="utf-8").read())
    pois = []
    for r in records:
        pois.append(
            PointOfInterest(
                name=r["name"],
                description=r.get("description"),
                icon=r.get("icon", ""),
                sign_type=int(r["sign_type"]),
                max_distance=float(r["max_distance"]),
                min_distance=float(r.get("min_distance", 0.0)),
                node_rank=int(r["node_rank"]),
                detail_distance=(
                    float(r["detail_distance"]) if r.get("detail_distance") is not None else None
                ),
                detail_node_rank=(
                    int(r["detail_node_rank"]) if r.get("detail_node_rank") is not None else None
                ),
                geometry=shape(r["geometry"]),
            )
        )
    return pois


def merge_nearby_pois(pois: list[PointOfInterest], buffer_m: float = 100.0) -> list[PointOfInterest]:
    """Collapse same-type point POIs within ``buffer_m`` of each other into
    one, at their combined centroid.

    A single real amenity (a school compound, say) sometimes ends up as
    several separate OSM features close together (one per building) -
    each becoming its own ``PointOfInterest`` would otherwise demand a
    separate sign entry pointing at (almost) the same place. "Same type"
    is judged by ``icon`` (the field that actually distinguishes a
    school from a health centre from a chiefferie here). Merging is
    transitive (A-B-C close in a chain all merge into one), and only
    ever applies to point geometries - polygon POIs (e.g. a whole town's
    own AOI) are left untouched.

    Args:
        pois: The POIs to merge.
        buffer_m: Distance (m) within which two same-icon POIs merge.

    Returns:
        A new list: merged clusters (name = every source POI's own name,
        joined with " / "), plus every POI that didn't merge with
        anything, plus every polygon POI unchanged.
    """
    points = [p for p in pois if not p.is_polygon]
    polygons = [p for p in pois if p.is_polygon]
    if len(points) < 2:
        return pois

    geom_series = gpd.GeoSeries([p.geometry for p in points], crs=4326)
    proj = geom_series.to_crs(geom_series.estimate_utm_crs())

    parent = list(range(len(points)))

    def find(a: int) -> int:
        """Union-find lookup with path compression.

        Args:
            x: Element index.

        Returns:
            The representative (root) of ``x``'s set.
        """
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        """Merge the sets of two elements.

        Args:
            a: First element index.
            b: Second element index.
        """
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            if points[i].icon == points[j].icon and proj.iloc[i].distance(proj.iloc[j]) <= buffer_m:
                union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(points)):
        clusters[find(i)].append(i)

    merged = []
    for indices in clusters.values():
        if len(indices) == 1:
            merged.append(points[indices[0]])
            continue
        group = [points[i] for i in indices]
        centroid_proj = proj.iloc[indices].union_all().centroid
        centroid_wgs84 = gpd.GeoSeries([centroid_proj], crs=proj.crs).to_crs(4326).iloc[0]
        merged.append(
            replace(group[0], name=" / ".join(dict.fromkeys(g.name for g in group)), geometry=centroid_wgs84)
        )
    return merged + polygons


def polygon_boundary_target_nodes(
    polygon_proj, edges_gdf: gpd.GeoDataFrame
) -> list:
    """Every real node lying on an edge that crosses ``polygon_proj``'s
    own boundary - i.e. "the nearest node outside the polygon" for every
    road entering it, per the module docstring's "polygon of interest"
    rule.

    Args:
        polygon_proj: The POI polygon, already in the same projected
            CRS as ``edges_gdf``.
        edges_gdf: Edge table (u, v, key)-indexed, with geometry.

    Returns:
        A list of unique real node ids (``edges_gdf``'s own ``u``/``v``
        values).
    """
    boundary = polygon_proj.boundary
    crossing = edges_gdf[edges_gdf.geometry.intersects(boundary)]
    targets = set()
    for (u, v, _key), row in crossing.iterrows():
        targets.add(u)
        targets.add(v)
    return list(targets)


def point_target_nodes(point_proj, nodes_gdf: gpd.GeoDataFrame, k: int = 1) -> list:
    """The ``k`` nearest real nodes to ``point_proj`` (already projected)."""
    distances = nodes_gdf.geometry.distance(point_proj)
    return list(distances.nsmallest(k).index)


def _rank_threshold_for_distance(poi: PointOfInterest, distance_m: float) -> int:
    """Which node_rank threshold applies at this distance from the POI -
    the tighter ``detail_node_rank`` once inside ``detail_distance``, else
    the regular ``node_rank``."""
    if poi.detail_distance is not None and distance_m <= poi.detail_distance:
        return poi.detail_node_rank
    return poi.node_rank


def _select_candidate_intersections(
    intersections_gdf: gpd.GeoDataFrame, poi: PointOfInterest, geometry_proj
) -> pd.Series:
    """Real intersections eligible for this POI: within
    ``[min_distance, max_distance]`` of ``geometry_proj`` (a polygon's own
    shape - distance is 0 anywhere inside/on it - or a point), AND at
    least as important (``intersection_rank`` <=) as the rank threshold
    that applies at that particular distance.

    Returns:
        A Series of ``distance_m``, indexed like ``intersections_gdf``
        (only the eligible rows).
    """
    distance_m = intersections_gdf.geometry.distance(geometry_proj)
    in_range = (distance_m >= poi.min_distance) & (distance_m <= poi.max_distance)
    thresholds = distance_m.apply(lambda d: _rank_threshold_for_distance(poi, d))
    eligible = in_range & (intersections_gdf["intersection_rank"] <= thresholds)
    return distance_m[eligible]


def compute_poi_signs(
    intersections_gdf: gpd.GeoDataFrame,
    edges_gdf: gpd.GeoDataFrame,
    nodes_gdf: gpd.GeoDataFrame,
    streets_df: pd.DataFrame,
    pois: list[PointOfInterest],
    category_ranks: dict | None = None,
    category_factors: dict | None = None,
) -> dict:
    """Assign POI direction entries to (node_id, street_id) signs.

    Returns:
        ``{(node_id, street_id): [entry, ...]}`` where each entry is
        ``{"poi": PointOfInterest, "distance_m": float, "sign_type": int}``
        - ``sign_type`` already resolved via ``DEFAULT_ROAD_MAX_SIGN_TYPE``.
    """
    street_rank_map = streets_df.set_index("street_id")["street_rank"]
    street_category = {
        sid: rank_to_category(rank, category_ranks) for sid, rank in street_rank_map.items()
    }
    routing_graph = routing.build_routing_graph(edges_gdf, street_category, category_factors)

    sign_street_ids_by_node = {
        row["node_id"]: set(int(s) for s in row["sign_street_ids"].split(";") if s)
        for _, row in intersections_gdf.iterrows()
    }

    proj_crs = intersections_gdf.crs
    signs: dict = defaultdict(list)

    for poi in pois:
        geom_proj = gpd.GeoSeries([poi.geometry], crs=4326).to_crs(proj_crs).iloc[0]

        if poi.is_polygon:
            targets = polygon_boundary_target_nodes(geom_proj, edges_gdf)
        else:
            # Only the SINGLE truly nearest node - not e.g. the 3
            # nearest, which can include a node that's actually farther
            # from the POI than the candidate intersection itself (just
            # globally close network-wide). Treating those as equally
            # valid "entrances" let the routing-weighted argmin pick a
            # geometrically wrong, but road-class-cheaper, detour
            # instead of the real (if lower-category, more heavily
            # weighted) access road right there.
            targets = point_target_nodes(geom_proj, nodes_gdf, k=1)
        if not targets:
            continue

        candidate_distances = _select_candidate_intersections(intersections_gdf, poi, geom_proj)
        if candidate_distances.empty:
            continue
        candidate_node_ids = intersections_gdf.loc[candidate_distances.index, "node_id"].tolist()

        results = routing.nearest_targets_distance(routing_graph, candidate_node_ids, targets)
        for node_id, (real_distance_m, street_id) in results.items():
            if street_id is None:
                continue
            if street_id not in sign_street_ids_by_node.get(node_id, set()):
                continue  # route leaves via the road that gets no sign here
            category = street_category.get(street_id, "other")
            road_cap = DEFAULT_ROAD_MAX_SIGN_TYPE.get(category, _SIGN_TYPE_MAX)
            sign_type = max(poi.sign_type, road_cap)
            signs[(node_id, street_id)].append(
                {"poi": poi, "distance_m": real_distance_m, "sign_type": sign_type}
            )

    # No dedup across DIFFERENT node_ids: a POI legitimately gets its own
    # sign at every distinct intersection that qualifies for it (a
    # different approach/decision point each deserves its own pointer,
    # per the map-should-have-more-than-one-sign feedback below). Within
    # one node_id, a duplicate can't actually happen here in the first
    # place - ``routing.nearest_targets_distance`` above already returns
    # at most one (distance, street) result per source node_id, so this
    # loop can never append two entries for the same POI under the same
    # key. If that ever changes (e.g. a future multi-target-per-node
    # design), dedupe per (node_id, street_id) key here, keeping the
    # entry with the shortest remaining distance - never across node_ids.
    return dict(signs)
