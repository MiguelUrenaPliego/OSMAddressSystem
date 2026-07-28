"""Split a town's AOI into its smallest available administrative subregions.

Some towns have no finer-grained boundary than the town itself. Others
are made up of several named neighbourhoods, suburbs, or villages. This
module finds whichever is true for a given AOI, so a caller can run the
rest of the pipeline once per subregion without special-casing the
"just one region" case.
"""

from __future__ import annotations

import geopandas as gpd

from . import osm
from .default_values import (
    DEFAULT_SUBREGION_CONTAINMENT_FRACTION,
    DEFAULT_SUBREGION_COVERAGE_FRACTION,
    DEFAULT_SUBREGION_PLACE_VALUES,
)


def find_subregions(
    aoi: gpd.GeoDataFrame,
    place_values: tuple[str, ...] = DEFAULT_SUBREGION_PLACE_VALUES,
    min_coverage_fraction: float = DEFAULT_SUBREGION_COVERAGE_FRACTION,
) -> gpd.GeoDataFrame:
    """Find the subregions making up an AOI, one level finer than it.

    Queries Overpass for administrative boundaries and named places
    (``place`` in ``place_values``) whose own geometry is genuinely
    contained in the AOI (most of the candidate's own area falls inside
    it, see ``DEFAULT_SUBREGION_CONTAINMENT_FRACTION``), not merely
    touching or overlapping it. This is one level finer than however the
    AOI itself was resolved
    (see ``utils.reverse_geocode_boundary``'s zoom table in
    ``report/georanks.md``), since a town-level AOI's own subregions are
    exactly its neighbourhoods/suburbs/villages.

    OSM's own subregion boundaries are frequently incomplete: a large
    city might have only some of its real districts mapped as polygons,
    with the rest left ungeocoded. Splitting on a partial set like that
    would silently drop every street in the unmapped districts, so the
    found candidates' own union must cover at least ``min_coverage_fraction``
    of the AOI's own area before they're used at all. Below that
    threshold, this function falls back to the AOI as a whole, exactly
    like the "no subregions found" case, since a coarser but COMPLETE
    result is always preferable to a finer but PARTIAL one.

    Args:
        aoi: The town's own AOI, as resolved by ``utils.get_town_aoi``.
        place_values: OSM ``place`` tag values that count as a candidate
            subregion.
        min_coverage_fraction: Minimum fraction (0-1) of the AOI's own
            area that the found subregions must jointly cover for them
            to be used, rather than falling back to the AOI as a whole.

    Returns:
        A GeoDataFrame with one row per subregion (``name``, ``geometry``),
        in ``aoi``'s own CRS. If Overpass finds no subregions, or the ones
        it finds don't jointly cover at least ``min_coverage_fraction`` of
        the AOI, returns a single-row GeoDataFrame equal to ``aoi`` itself,
        so a caller can treat "one subregion" and "several subregions" the
        same way, and is never silently handed incomplete coverage.
    """
    place_filter = "|".join(place_values)
    query = f"""
        [out:json][timeout:60];
        (
            node["place"~"^({place_filter})$"]({{{{bbox}}}});
            way["place"~"^({place_filter})$"]({{{{bbox}}}});
            relation["place"~"^({place_filter})$"]({{{{bbox}}}});
            way["boundary"="administrative"]({{{{bbox}}}});
            relation["boundary"="administrative"]({{{{bbox}}}});
        );
        out body;
        >;
        out skel qt;
    """
    raw = osm.overpass_api_query(query, aoi)

    if raw.empty:
        return aoi.copy()

    # Area/containment only mean what you'd expect in a projected CRS,
    # not lat/lon degrees (the same reason label_streets/assign_street_
    # directionality require a projected CRS for distances and angles).
    utm_crs = aoi.estimate_utm_crs()
    aoi_geom = aoi.to_crs(utm_crs).union_all()
    raw_projected = raw.to_crs(utm_crs)

    # Keep only polygonal features (a bare node tagged place=village, with
    # no polygon of its own, can't define a subregion boundary) that are
    # genuinely CONTAINED in the AOI, not merely intersecting it.
    #
    # A plain `intersects` check lets two very different, wrong kinds of
    # feature through: a COARSER boundary the AOI itself sits inside (a
    # department or region containing the whole town, which trivially
    # "intersects" it) and a neighbouring, same-level administrative unit
    # that only shares a border edge with the AOI without being part of
    # it at all (touching counts as intersecting in shapely, even though
    # the two share no interior area). Both are wrong: the first is
    # coarser than the AOI, not finer, and the second is a sibling, not a
    # subdivision.
    #
    # Requiring most of the CANDIDATE's own area to fall inside the AOI
    # (not the other way around) rules out both: a coarser boundary's own
    # area is mostly outside the AOI (the AOI is a small piece of it), and
    # a merely-touching sibling's own area barely overlaps the AOI at all
    # (only a shared, zero-area border line does).
    polygonal = raw_projected[raw_projected.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    polygonal = polygonal[polygonal.geometry.area > 0]
    contained_fraction = (
        polygonal.geometry.intersection(aoi_geom).area / polygonal.geometry.area
    )
    inside = polygonal[contained_fraction >= DEFAULT_SUBREGION_CONTAINMENT_FRACTION]

    if "name" not in inside.columns:
        inside = inside.copy()
        inside["name"] = None
    inside = inside[inside["name"].notna()]
    inside = inside.drop_duplicates(subset="name")

    if inside.empty:
        return aoi.copy()

    # Reject a partial split: if the candidates found don't jointly
    # cover most of the AOI, OSM's own subregion data for this place is
    # incomplete, and falling back to the AOI whole avoids silently
    # losing every street in whichever district wasn't mapped.
    covered_fraction = inside.geometry.union_all().intersection(aoi_geom).area / aoi_geom.area
    if covered_fraction < min_coverage_fraction:
        return aoi.copy()

    result = inside[["name", "geometry"]].reset_index(drop=True)
    return result.to_crs(aoi.crs)
