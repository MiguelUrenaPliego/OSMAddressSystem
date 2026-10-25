"""Create the Fotouni project folder for the community address app.

Everything is taken from ``inputs/`` so the project can be rebuilt from the repository alone:

* ``input/streets.osm`` - the OpenStreetMap file the street network was generated from (the *old* file, so the street ids
  of the community edits still match),
* ``network/`` - the labelled network of that run (``nodes.gpkg``, ``edges.gpkg``, ``streets.csv``) with the street names of OSM,
* ``input/street_edits.csv`` / ``input/edge_edits.csv`` - the street names and edge edits given by the community (2026-09-15),
* ``input/area.gpkg`` - the Bandja commune boundary (the area to process) and the estimated Fotouni area,
* ``input/points.gpkg`` - the important points: squares, intersections and facilities of the community workbook, OSM places,
  schools, health centres and chiefferies from OSM,
* ``input/buildings.gpkg`` - the surveyed houses of the Fotouni town survey,
* ``input/missing_roads.kml`` - the roads the community drew in Google Earth (import them in the Missing roads tab).

Run (from the repository root)::

    uv run experiments/Fotouni/make_app_project.py [projects/fotouni]
    uv run --extra app --extra planning python -m osmaddresssystem.app serve --project projects/fotouni
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, shape

from osmaddresssystem.app.area import aoi_from_points
from osmaddresssystem.app.store import Store

HERE = Path(__file__).resolve().parent
IN = HERE / "inputs"


def main(out: Path) -> None:
    """Build the project folder.

    Args:
        out: Project folder to create (existing input/network files are overwritten, the database is kept).
    """
    (out / "input").mkdir(parents=True, exist_ok=True)
    (out / "network").mkdir(exist_ok=True)
    for f in ("nodes.gpkg", "edges.gpkg", "streets.csv"):
        shutil.copyfile(IN / f, out / "network" / f)
    shutil.copyfile(IN / "streets.osm", out / "input" / "streets.osm")
    shutil.copyfile(IN / "street_edits_20260915.csv", out / "input" / "street_edits.csv")
    shutil.copyfile(IN / "edge_edits_20260915.csv", out / "input" / "edge_edits.csv")
    shutil.copyfile(IN / "missing_roads.kml", out / "input" / "missing_roads.kml")

    b = pd.read_csv(IN / "survey_buildings.csv")
    attrs = pd.read_csv(IN / "building_attributes.csv").fillna({"name": "", "service": "", "review": ""})          # see building_attributes.py
    b = b.merge(attrs, on="building_id", how="left")
    bpts = gpd.GeoSeries([Point(x, y) for x, y in zip(b.lon, b.lat)], crs=4326)
    commune = gpd.read_file(IN / "bandja_boundary.geojson")
    fot = gpd.GeoDataFrame({"name": ["Fotouni (surveyed buildings + 50 m)"], "kind": ["fotouni_aoi"]}, geometry=[aoi_from_points(bpts, 50.0, 0.15)], crs=4326)
    area = pd.concat([commune[["name", "kind", "geometry"]], fot], ignore_index=True)
    gpd.GeoDataFrame(area[["name", "kind"]], geometry=area.geometry, crs=4326).to_file(out / "input" / "area.gpkg")

    rows = []
    for src in ("places_community.csv", "places_osm.csv"):
        rows += pd.read_csv(IN / src).to_dict("records")
    for p in json.load(open(IN / "pois_osm.json", encoding="utf-8")):
        pt = shape(p["geometry"]).representative_point()           # POIs may be points or polygons
        x, y = pt.x, pt.y
        rows.append({"name": p.get("name"), "kind": p.get("description") or "poi", "settlement": None, "source": "OSM", "lat": y, "lon": x})
    pts = pd.DataFrame(rows).dropna(subset=["lat", "lon"])
    gpd.GeoDataFrame(pts[["name", "kind", "settlement", "source"]], geometry=[Point(x, y) for x, y in zip(pts.lon, pts.lat)], crs=4326
                     ).to_file(out / "input" / "points.gpkg")
    ranks = pd.read_csv(IN / "point_sign_ranks.csv")                              # revised by hand, see point_ranks.py
    pg = gpd.read_file(out / "input" / "points.gpkg")
    pg["sign_rank"] = ranks["sign_rank"].reindex(range(len(pg))).fillna(3).astype(int).to_numpy()
    pg.to_file(out / "input" / "points.gpkg")

    cols = ["building_id", "address_id", "district", "survey_type", "use", "residents", "name", "service", "workers", "azimuth", "review"]
    gpd.GeoDataFrame(b[cols], geometry=[Point(x, y) for x, y in zip(b.lon, b.lat)], crs=4326).to_file(out / "input" / "buildings.gpkg")
    (out / "input" / "default_reference.json").write_text(json.dumps({"name": "Carrefour Brouwer (hôtel)", "lat": 5.3201511, "lon": 10.2276009}, ensure_ascii=False, indent=1), encoding="utf-8")   # the node 6957219888 of Carrefour Brouwer
    Store(out, "Fotouni")
    print(f"Fotouni project created in {out}: {len(pts)} important points, {len(b)} houses, area polygons {len(area)}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[1] / "projects" / "fotouni")
