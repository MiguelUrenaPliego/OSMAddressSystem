"""Small, persistent review areas; they organise work without clipping roads."""

import math

import geopandas as gpd
import shapely
from shapely.geometry import box

STATUSES = {'unreviewed', 'in_progress', 'reviewed', 'needs_review'}


def make_grid(aoi_wgs84, crs, cell_m: float | str) -> dict:
    aoi = gpd.GeoSeries([aoi_wgs84], crs=4326).to_crs(crs).iloc[0]
    x0, y0, x1, y1 = aoi.bounds
    if cell_m == 'auto':
        cell_m = max(50, math.ceil(max(x1-x0, y1-y0)/12/50)*50)
    elif not math.isfinite(cell_m) or not 50 <= cell_m <= 2000:
        raise ValueError('choose Auto or an area size from 50 to 2000 metres')
    cols, rows = math.ceil((x1-x0)/cell_m), math.ceil((y1-y0)/cell_m)
    if rows * cols > 10000:
        raise ValueError('more than 400 areas; choose Auto or a larger area size')
    cells = []
    for row in range(rows):
        for col in range(cols):
            shape = box(x0+col*cell_m, y1-(row+1)*cell_m, x0+(col+1)*cell_m, y1-row*cell_m).intersection(aoi)
            if shape.is_empty or shape.area < 1:
                continue
            geom = gpd.GeoSeries([shape], crs=crs).to_crs(4326).iloc[0]
            cells.append({'type': 'Feature', 'properties': {'id': f'R{row+1:02}C{col+1:02}', 'status': 'unreviewed', 'note': ''},
                          'geometry': shapely.geometry.mapping(geom)})
            if len(cells) > 400:
                raise ValueError('more than 400 areas; choose Auto or a larger area size')
    return {'cell_m': cell_m, 'type': 'FeatureCollection', 'features': cells}
