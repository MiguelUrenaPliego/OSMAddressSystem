"""Fotouni (Bandja) POI direction signs: schools, healthcare, chiefferies,
and the Fotouni town centre itself as a polygon POI.

Downloads the three POI types from OSM via Overpass, restricted to the
Bandja subregion's own street network extent (see ``bandja.py``, which
must have already been run), builds one JSON POI file next to the
subregion's other generated outputs, then runs the full
``street_signs``/``poi_signs`` pipeline against the already-generated
``nodes.gpkg``/``edges.gpkg``/``streets.gpkg``/``streets.csv``, and
re-renders that same subregion's own ``street_signs_map.html`` (written
by ``bandja.py``) WITH the POI signs added - the POI-signs map is part
of the street-signs map, not a separate file.

Run with the OSMAddressSystem project's own venv, after ``bandja.py``:
    .venv/bin/python experiments/Bandja/bandja_pois.py
"""

from __future__ import annotations

import json
import os

import geopandas as gpd
import pandas as pd

from osmaddresssystem import osm, poi_signs, street_signs

TOWN_DIR = os.path.dirname(os.path.abspath(__file__))
SUBREGION_DIR = os.path.join(TOWN_DIR, "bandja")
POIS_JSON_PATH = os.path.join(SUBREGION_DIR, "pois.json")
STREET_SIGNS_HTML_PATH = os.path.join(SUBREGION_DIR, "street_signs_map.html")

# Fotouni's own main-intersection center (same point used as the default
# view for Fotouni's address/survey maps - see data/scripts/build_survey_maps.py
# FOTOUNI_DEFAULT_CENTER).
FOTOUNI_CENTER_LAT, FOTOUNI_CENTER_LON = 5.321098, 10.227436
FOTOUNI_POLYGON_BUFFER_M = 500.0

SCHOOL_HEALTHCARE_PARAMS = dict(
    sign_type=3, max_distance=4000.0, min_distance=0.0,
    detail_distance=1500.0, detail_node_rank=5, node_rank=5,
)
CHIEFFERIE_PARAMS = dict(
    sign_type=2, max_distance=5000.0, min_distance=0.0,
    detail_distance=2000.0, detail_node_rank=5, node_rank=5,
)
FOTOUNI_POLYGON_PARAMS = dict(
    sign_type=1, max_distance=10000.0, min_distance=200.0,
    detail_distance=2000.0, detail_node_rank=3, node_rank=1,
)


def _poi_name(row, fallback: str) -> str:
    name = row.get("name")
    return name if isinstance(name, str) and name.strip() else fallback


def _points_from_osm(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Collapse way/relation polygons to their centroid, keep node points as-is.

    ``gdf`` comes back from ``osm.schools``/``osm.healthcare``/
    ``osm.chiefferies`` reprojected to ``bounds``'s own CRS (the
    projected UTM CRS used for routing, since that's what's passed as
    ``bounds`` here) - reprojected to EPSG:4326 first, since POI JSON
    geometries are lon/lat.
    """
    if gdf.empty:
        return gdf
    gdf = gdf.to_crs(4326)
    utm_crs = gdf.estimate_utm_crs()
    centroids = gdf.to_crs(utm_crs).geometry.centroid.to_crs(4326)
    is_point = gdf.geometry.type.isin(["Point", "MultiPoint"])
    geometry = gpd.GeoSeries(
        list(gdf.geometry.where(is_point, centroids)), crs=4326, index=gdf.index
    )
    return gpd.GeoDataFrame(gdf.drop(columns="geometry"), geometry=geometry)


def _pois_to_records(gdf: gpd.GeoDataFrame, icon: str, fallback_name: str, description: str, params: dict) -> list[dict]:
    records = []
    for _, row in _points_from_osm(gdf).iterrows():
        records.append(
            {
                "name": _poi_name(row, fallback_name),
                "description": description,
                "icon": icon,
                "geometry": {"type": "Point", "coordinates": [row.geometry.x, row.geometry.y]},
                **params,
            }
        )
    return records


def build_pois_json(bounds: gpd.GeoDataFrame) -> list[dict]:
    schools = osm.schools(bounds)
    healthcare = osm.healthcare(bounds)
    chiefferies = osm.chiefferies(bounds)
    print(f"Downloaded {len(schools)} school(s), {len(healthcare)} healthcare facility(ies), "
          f"{len(chiefferies)} chiefferie(s)")

    records = []
    records += _pois_to_records(schools, "\U0001F3EB", "École", "School", SCHOOL_HEALTHCARE_PARAMS)
    records += _pois_to_records(
        healthcare, "\U0001F3E5", "Centre de santé", "Healthcare centre", SCHOOL_HEALTHCARE_PARAMS
    )
    records += _pois_to_records(chiefferies, "\U0001F3F0", "Chefferie", "Chiefferie", CHIEFFERIE_PARAMS)

    fotouni_point = gpd.GeoSeries(
        gpd.points_from_xy([FOTOUNI_CENTER_LON], [FOTOUNI_CENTER_LAT]), crs=4326
    )
    fotouni_polygon = (
        fotouni_point.to_crs(fotouni_point.estimate_utm_crs())
        .buffer(FOTOUNI_POLYGON_BUFFER_M)
        .to_crs(4326)
        .iloc[0]
    )
    records.append(
        {
            "name": "Fotouni",
            "description": "Fotouni town centre",
            "icon": "\U0001F3D8️",
            "geometry": json.loads(gpd.GeoSeries([fotouni_polygon], crs=4326).to_json())["features"][0][
                "geometry"
            ],
            **FOTOUNI_POLYGON_PARAMS,
        }
    )
    return records


def main():
    nodes_gdf = gpd.read_file(os.path.join(SUBREGION_DIR, "nodes.gpkg")).set_index("osmid")
    edges_gdf = gpd.read_file(os.path.join(SUBREGION_DIR, "edges.gpkg")).set_index(["u", "v", "key"])
    streets_df = pd.read_csv(os.path.join(SUBREGION_DIR, "streets.csv"))
    streets_gdf = gpd.read_file(os.path.join(SUBREGION_DIR, "streets.gpkg"))

    bounds = gpd.GeoDataFrame(
        geometry=[nodes_gdf.union_all().convex_hull.buffer(1000)], crs=nodes_gdf.crs
    )

    records = build_pois_json(bounds)
    with open(POIS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"Wrote {POIS_JSON_PATH} ({len(records)} POIs)")

    pois = poi_signs.load_pois(POIS_JSON_PATH)
    pois = poi_signs.merge_nearby_pois(pois, buffer_m=100.0)
    print(f"Merged down to {len(pois)} POI(s) after collapsing same-type POIs within 100m")
    intersections_gdf = street_signs.compute_intersections(nodes_gdf, edges_gdf, streets_df)
    signs = poi_signs.compute_poi_signs(intersections_gdf, edges_gdf, nodes_gdf, streets_df, pois)
    print(f"Assigned POI signs to {len(signs)} (node, street) sign(s)")

    street_signs.build_street_signs_map(
        streets_gdf, intersections_gdf, STREET_SIGNS_HTML_PATH,
        poi_signs=signs, pois=pois, edges_gdf=edges_gdf,
    )
    print(f"Wrote {STREET_SIGNS_HTML_PATH}")


if __name__ == "__main__":
    main()
