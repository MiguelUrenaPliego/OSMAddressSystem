"""Ties street identity, directionality, and numbering into one call.

``street_ids.label_streets`` -> ``street_direction.assign_street_directionality``
(auto-detecting a grid direction when needed) -> ``numbering.assign_house_numbers``
-> ``numbering.add_admin_columns`` is the full sequence a caller needs to
go from a raw street graph to an addressed one. Both the initial
per-subregion generation step and ``street_editor.apply_street_edits``'s
re-run after a manual edit need this exact sequence, so it lives here
once instead of being duplicated in both places.
"""

from __future__ import annotations

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import Point, Polygon, MultiPolygon
from shapely.ops import linemerge

from . import branches, numbering, osm, path_numbering, street_direction, street_ids, utils
from .default_values import (
    DEFAULT_BRANCH_NUMBER_DIVISOR_M,
    DEFAULT_MAX_BRANCH_LEVELS,
    DEFAULT_NUMBER_DIVISOR_M,
    DEFAULT_PATH_HIGH_RANK_MAX,
    DEFAULT_TRANSVERSAL_ROAD_PCT,
)


def clip_graph_to_polygon(
    G: nx.MultiDiGraph, polygon_wgs84: Polygon | MultiPolygon
) -> nx.MultiDiGraph | None:
    """Keep only ``G``'s own nodes falling inside ``polygon_wgs84`` (e.g.
    a subregion of a town), plus whichever edges survive between them.

    ``polygon_wgs84`` is reprojected to ``G``'s own CRS first. The
    result is reduced to its own largest real component again
    (``osm.largest_real_component``) - clipping a town-wide graph down
    to one subregion can easily leave several disconnected fragments
    behind, and every graph this codebase hands to
    ``street_ids.label_streets`` must already be reduced that way, never
    left for a caller to remember.

    Args:
        G: A projected street network graph (e.g. a whole town's own,
            from ``osm.load_projected_graph``).
        polygon_wgs84: The subregion polygon to clip to, in EPSG:4326.

    Returns:
        The clipped, largest-real-component subgraph, or ``None`` if
        fewer than two nodes (or no edges at all) survive the clip - a
        subregion with no usable street network of its own.
    """
    polygon = gpd.GeoSeries([polygon_wgs84], crs=4326).to_crs(G.graph["crs"]).iloc[0]
    keep_nodes = [
        n for n, data in G.nodes(data=True) if polygon.contains(Point(data["x"], data["y"]))
    ]
    if len(keep_nodes) < 2:
        return None
    Gs = G.subgraph(keep_nodes).copy()
    if Gs.number_of_edges() == 0:
        return None
    return osm.largest_real_component(Gs)


def country_and_province_polygons(aoi: gpd.GeoDataFrame) -> dict:
    """Look up the country/province polygons containing an AOI, once.

    ``numbering.add_admin_columns`` fills a column from whatever polygon
    in the chain resolves to that level. An AOI/subregion polygon itself
    only ever resolves to the town/neighbourhood levels, so country/
    province need their own, coarser lookups
    (``utils.reverse_geocode_boundary`` at zoom 3/5, see
    ``report/georanks.md``'s polygon rank table). Every subregion of the
    same town/city shares the same country/province, so a caller only
    needs to call this once per town, not once per subregion.

    Args:
        aoi: The town/city's own AOI.

    Returns:
        A dict with ``"country"``/``"province"`` keys mapped to their
        polygons, omitting a key if that lookup failed.
    """
    centroid = aoi.union_all().centroid
    polygons = {}
    for label, zoom in (("country", 3), ("province", 5)):
        result = utils.reverse_geocode_boundary(centroid.y, centroid.x, zooms=(zoom,))
        if result is not None:
            polygons[label] = result[3]
    return polygons


def write_gpkg(gdf: gpd.GeoDataFrame, path: str) -> gpd.GeoDataFrame:
    """Stringify a GeoDataFrame's non-numeric columns and write it to a
    GeoPackage.

    ``.gpkg`` (unlike a plain in-memory GeoDataFrame) can't hold a
    column of mixed/nullable-object dtype - every caller writing
    ``label_streets``/``build_addressed_streets`` output to disk needs
    ``street_ids.stringify_nonnumeric_columns`` first, so this bundles
    that step with the actual write.

    Args:
        gdf: The GeoDataFrame to write.
        path: Destination ``.gpkg`` path.

    Returns:
        The stringified GeoDataFrame actually written (a caller that
        needs the on-disk representation, e.g. to build a dependent
        streets table afterward, doesn't have to re-read the file).
    """
    out = street_ids.stringify_nonnumeric_columns(gdf)
    out.to_file(path, driver="GPKG")
    return out


def _orient_along_numbering(main: gpd.GeoDataFrame, dissolved: gpd.GeoDataFrame):
    """Flip each street line so it starts where the street's numbering starts.

    The dissolved/merged line has an arbitrary direction, but side detection and the distance
    along the street used for house numbers assume the line runs in the numbering direction.
    The start is the node with the smallest ``street_pos_*_m`` (from
    ``assign_street_directionality``); streets without those columns, or whose line is not a
    single LineString, are left as they are.

    Args:
        main: Main-path edges with ``street_id``, geometry and ``street_pos_u_m``/``_v_m``.
        dissolved: One row per street with its dissolved line.

    Returns:
        Series of geometries, oriented where possible.
    """
    if "street_pos_u_m" not in main.columns:
        return dissolved["geometry"]
    ends: dict = {}
    for sid, geom, u, v, pu, pv in zip(main["street_id"], main.geometry, main.index.get_level_values("u"),
                                       main.index.get_level_values("v"), main["street_pos_u_m"], main["street_pos_v_m"]):
        if geom is None or pd.isna(pu) or pd.isna(pv) or geom.geom_type != "LineString":
            continue
        c = list(geom.coords)
        for pos, xy in ((pu, c[0]), (pv, c[-1])):
            cur = ends.get(sid)
            if cur is None or pos < cur[0]:
                ends[sid] = (pos, xy)
    out = []
    for sid, geom in zip(dissolved["street_id"], dissolved["geometry"]):
        e = ends.get(sid)
        if e is None or geom.geom_type != "LineString":
            out.append(geom)
            continue
        c = list(geom.coords)
        d0 = (c[0][0] - e[1][0]) ** 2 + (c[0][1] - e[1][1]) ** 2
        d1 = (c[-1][0] - e[1][0]) ** 2 + (c[-1][1] - e[1][1]) ** 2
        out.append(geom if d0 <= d1 else type(geom)(c[::-1]))
    return gpd.GeoSeries(out, index=dissolved.index)


def streets_gdf_from_edges(
    edges_gdf: gpd.GeoDataFrame, streets_df: pd.DataFrame
) -> gpd.GeoDataFrame:
    """Build a one-row-per-street GeoDataFrame from labeled, directed edges.

    Dissolves each street_id's own main-path edges (``is_main_direction
    == True``, see ``street_direction.assign_street_directionality``)
    into a single line geometry, and attaches ``streets_df``'s own
    columns (``street_name``, ``road_type``, ``length_m``, house-number/
    admin columns if present, ...). This is the shape both
    ``street_editor.build_street_editor_map`` and
    ``numbering.geocode_points`` expect: a streets table with real
    geometry to draw or project onto.

    Args:
        edges_gdf: Edge GeoDataFrame with ``street_id`` and
            ``is_main_direction`` columns.
        streets_df: One row per ``street_id``, from ``label_streets``/
            ``assign_street_directionality``/``numbering``.

    Returns:
        A GeoDataFrame with one row per ``street_id`` present among the
        main-path edges, ``streets_df``'s columns, and a dissolved line
        ``geometry``, in ``edges_gdf``'s own CRS.
    """
    main = edges_gdf[edges_gdf["is_main_direction"] & edges_gdf["street_id"].notna()]
    dissolved = main[["street_id", "geometry"]].dissolve(by="street_id").reset_index()
    # A street's own main-path edges normally form one simple line, but
    # dissolving them one edge at a time can leave a MultiLineString even
    # when the pieces do connect end to end (dissolve doesn't merge
    # touching segments into one LineString by itself). linemerge fixes
    # that whenever the pieces genuinely do connect; a real gap (e.g. a
    # subregion clip cutting a street in two) is left as a MultiLineString,
    # which every consumer here (street_editor, numbering.geocode_points)
    # still handles via its own coordinate/projection logic.
    dissolved["geometry"] = dissolved["geometry"].apply(
        lambda geom: linemerge(geom) if geom.geom_type == "MultiLineString" else geom
    )
    dissolved["geometry"] = _orient_along_numbering(main, dissolved)
    merged = dissolved.merge(streets_df, on="street_id", how="left")
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=edges_gdf.crs)


def build_addressed_streets(
    G: nx.MultiDiGraph,
    origin: tuple[float, float],
    numbering_mode: str,
    direction_deg: float | None = None,
    admin_polygons: dict[str, Polygon | MultiPolygon] | None = None,
    transversal_road_pct: float = DEFAULT_TRANSVERSAL_ROAD_PCT,
    number_divisor_m: float = DEFAULT_NUMBER_DIVISOR_M,
    label_streets_kwargs: dict | None = None,
    name_branches: bool = False,
    max_branch_levels: int = DEFAULT_MAX_BRANCH_LEVELS,
    branch_number_divisor_m: float = DEFAULT_BRANCH_NUMBER_DIVISOR_M,
    path_high_rank_max: int = DEFAULT_PATH_HIGH_RANK_MAX,
    suffix_split_names: bool = True,
    backbone_streets=None,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Run the full street-identity -> directionality -> numbering pipeline.

    Args:
        G: A projected street network graph (see ``street_ids.label_streets``).
        origin: ``(x, y)`` center point, in radial mode; the reference point in path mode.
            Ignored in grid mode.
        numbering_mode: ``"radial"``, ``"grid"`` or ``"path"`` (see
            ``street_direction.assign_street_directionality`` and ``path_numbering``). In path
            mode streets are cut so each begins at its end nearest to the reference (by road
            network distance); ``street_id_original`` keeps the id before the cut.
        direction_deg: Compass direction for grid mode. If ``None`` and
            ``numbering_mode == "grid"``, it's autodetected via
            ``street_direction.detect_main_direction``.
        admin_polygons: Passed straight through to
            ``numbering.add_admin_columns``, if given. If ``None``, the
            admin columns are skipped.
        transversal_road_pct: Passed through to
            ``street_direction.assign_street_directionality``.
        number_divisor_m: Passed through to
            ``numbering.assign_house_numbers``.
        label_streets_kwargs: Extra keyword arguments forwarded to
            ``street_ids.label_streets``.
        name_branches: If True, unnamed roads branching off a named road get a
            derived name (see ``branches.assign_branch_names``), in the extra
            columns ``branch_name``/``display_name``.
        max_branch_levels: Maximum number of branch prefixes in a derived name.
        branch_number_divisor_m: Metres along the parent per branch-number step.
        path_high_rank_max: Path mode: maximum street rank of the high-rank network.
        backbone_streets: Path mode: street ids (named or to-be-named) the routes to the reference are
            restricted to; every other street hangs off them (see ``path_numbering.node_distances``).
        suffix_split_names: Path mode: give the parts after the first of a cut, named street a letter
            suffix in parentheses (``Rue X (B)``); see ``path_numbering.split_streets``.

    Returns:
        A tuple ``(nodes_gdf, edges_gdf, streets_gdf)``. ``streets_gdf``
        is one row per street_id with a dissolved main-path geometry
        (see ``streets_gdf_from_edges``), including house-number and
        admin columns.
    """
    label_streets_kwargs = label_streets_kwargs or {}
    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(G, **label_streets_kwargs)

    if numbering_mode == "grid" and direction_deg is None:
        direction_deg = street_direction.detect_main_direction(edges_gdf)

    node_distances = None
    if numbering_mode == "path":
        node_distances, _reference = path_numbering.node_distances(
            nodes_gdf, edges_gdf, origin, high_rank_max=path_high_rank_max,
            backbone_streets=backbone_streets,
        )
        edges_gdf, streets_df, _mapping = path_numbering.split_streets(
            edges_gdf, streets_df, node_distances, suffix_split_names=suffix_split_names)

    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf,
        edges_gdf,
        streets_df,
        origin=origin,
        transversal_road_pct=transversal_road_pct,
        numbering_mode=numbering_mode,
        direction_deg=direction_deg if direction_deg is not None else 0.0,
        node_distances=node_distances,
        path_high_rank_max=path_high_rank_max,
    )

    edges_gdf, streets_df = numbering.assign_house_numbers(
        edges_gdf, streets_df, number_divisor_m=number_divisor_m
    )

    if name_branches:
        edges_gdf, streets_df = branches.assign_branch_names(
            nodes_gdf, edges_gdf, streets_df,
            number_divisor_m=branch_number_divisor_m, max_levels=max_branch_levels,
        )

    if admin_polygons:
        streets_df = numbering.add_admin_columns(streets_df, admin_polygons)

    streets_gdf = streets_gdf_from_edges(edges_gdf, streets_df)

    return nodes_gdf, edges_gdf, streets_gdf
