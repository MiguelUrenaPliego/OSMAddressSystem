"""The AOI from surveyed buildings and the building use / residents / services classification."""

import importlib.util
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from osmaddresssystem.app.area import aoi_from_points

EXP = Path(__file__).resolve().parents[1] / "experiments" / "Fotouni"


def test_aoi_is_concave_contains_every_building_with_50_m_margin():
    """Two far-apart clusters (an L-shaped town): the outline is not the convex hull, holds every point and keeps 50 m around each."""
    pts = [Point(635000 + 20 * i, 577000 + 20 * j) for i in range(10) for j in range(10)] + [Point(635000 + 20 * i, 577000 + 1000 + 20 * j) for i in range(10) for j in range(60)] + [Point(635000 + 1000 + 20 * i, 577000 + 20 * j) for i in range(60) for j in range(10)]
    g = gpd.GeoSeries(pts, crs=32632).to_crs(4326)
    aoi = gpd.GeoSeries([aoi_from_points(g, 50, 0.05)], crs=4326).to_crs(32632).iloc[0]
    allp = gpd.GeoSeries(pts, crs=32632)
    assert all(aoi.contains(p.buffer(49)) for p in pts)
    assert aoi.area < 0.8 * allp.union_all().convex_hull.buffer(50).area                  # concave: the empty land between is left out


def _attrs():
    spec = importlib.util.spec_from_file_location("building_attributes", EXP / "building_attributes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_building_use_residents_and_services_are_classified_with_corrected_spelling():
    """Households give residents; shops, workshops, schools and heritage sites give a use, a clean name and English services."""
    m = _attrs()
    house = m.classify(pd.Series({"survey_type": "household", "survey_total_persons": 4}))
    assert house["use"] == "residential" and house["residents"] == 4 and house["service"] == ""
    shop = m.classify(pd.Series({"survey_type": "commerce", "commerce_name": "Boutque de jean", "commerce_products": "Alimentation générale, Boissons", "commerce_workers": 2}))
    assert shop["use"] == "commercial" and shop["name"] == "Boutique de Jean" and shop["service"] == "general food store; drinks" and shop["workers"] == 2
    weld = m.classify(pd.Series({"survey_type": "commerce", "commerce_name": "Fokou William", "commerce_products": "Atelier de soudure"}))
    assert weld["use"] == "industrial" and weld["service"] == "welding workshop"
    school = m.classify(pd.Series({"survey_type": "public", "public_name": "Ecole publque de bamendjo-Fotouni", "public_category": "school"}))
    assert school["use"] == "public" and school["name"].startswith("École publique") and school["service"] == "primary school"
    hotel = m.classify(pd.Series({"survey_type": "public", "public_name": "hotel zogang", "public_category": "other", "public_category_other": "Hotel"}))
    assert hotel["use"] == "commercial" and "hotel" in hotel["service"]
    sacred = m.classify(pd.Series({"survey_type": "heritage", "heritage_name": "Forêt sacré de demkwo", "heritage_category": "sacred_forest", "heritage_kind": "sacred"}))
    assert sacred["use"] == "public" and "sacred forest" in sacred["service"]
    assert m.classify(pd.Series({"survey_type": None}))["use"] == "unknown"
    big = m.classify(pd.Series({"survey_type": "household", "survey_total_persons": 30}))
    assert "check" in big["review"]
