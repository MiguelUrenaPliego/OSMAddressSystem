"""The area of interest derived from the surveyed buildings."""

from __future__ import annotations

import geopandas as gpd
import shapely
from shapely.geometry import MultiPoint
from shapely.ops import unary_union


def aoi_from_points(points: gpd.GeoSeries, buffer_m: float = 50.0, ratio: float = 0.15, crs=32632) -> shapely.Geometry:
    """A concave outline around surveyed points with a buffer, in WGS84.

    The outline is a concave hull (so empty land between far-apart settlements is left out, unlike a convex hull) that contains every
    point, buffered by ``buffer_m``; the union with the ``buffer_m`` circle of every point makes sure each building has at least
    that margin. Holes are filled.

    Args:
        points: Points in any CRS.
        buffer_m: Margin around the buildings in metres.
        ratio: Concave hull ratio (0 = most concave, 1 = convex hull).
        crs: Projected CRS used for the metres (UTM zone of the area).

    Returns:
        A (multi)polygon in EPSG:4326.
    """
    pts = gpd.GeoSeries(points, crs=points.crs).to_crs(crs)
    hull = shapely.concave_hull(MultiPoint(list(pts)), ratio=ratio, allow_holes=False)
    area = unary_union([hull.buffer(buffer_m), *[p.buffer(buffer_m) for p in pts]])
    if area.geom_type == "Polygon":
        area = shapely.Polygon(area.exterior)
    else:
        area = unary_union([shapely.Polygon(g.exterior) for g in area.geoms])
    return gpd.GeoSeries([area], crs=crs).to_crs(4326).iloc[0]
