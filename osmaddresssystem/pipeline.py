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

from . import numbering, osm, street_direction, street_ids, utils
from .default_values import (
    DEFAULT_NUMBER_DIVISOR_M,
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
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Run the full street-identity -> directionality -> numbering pipeline.

    Args:
        G: A projected street network graph (see ``street_ids.label_streets``).
        origin: ``(x, y)`` center point, in radial mode. Ignored in grid
            mode.
        numbering_mode: ``"radial"`` or ``"grid"`` (see
            ``street_direction.assign_street_directionality``).
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

    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf,
        edges_gdf,
        streets_df,
        origin=origin,
        transversal_road_pct=transversal_road_pct,
        numbering_mode=numbering_mode,
        direction_deg=direction_deg if direction_deg is not None else 0.0,
    )

    edges_gdf, streets_df = numbering.assign_house_numbers(
        edges_gdf, streets_df, number_divisor_m=number_divisor_m
    )

    if admin_polygons:
        streets_df = numbering.add_admin_columns(streets_df, admin_polygons)

    streets_gdf = streets_gdf_from_edges(edges_gdf, streets_df)

    return nodes_gdf, edges_gdf, streets_gdf
