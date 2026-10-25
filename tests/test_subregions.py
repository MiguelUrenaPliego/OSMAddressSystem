"""Tests for subregions.find_subregions."""

import geopandas as gpd
import pytest
from shapely.geometry import box

from osmaddresssystem import osm, subregions


def _aoi(minx=0.0, miny=0.0, maxx=100.0, maxy=100.0):
    """Test helper: aoi."""
    return gpd.GeoDataFrame({"geometry": [box(minx, miny, maxx, maxy)]}, crs="EPSG:32631")


def _candidates(*boxes, names=None):
    """Test helper: candidates."""
    names = names or [f"sub{i}" for i in range(len(boxes))]
    return gpd.GeoDataFrame(
        {"name": names, "geometry": list(boxes)}, crs="EPSG:32631"
    )


def test_no_subregions_found_returns_aoi_itself(monkeypatch):
    """Check no subregions found returns aoi itself."""
    monkeypatch.setattr(osm, "overpass_api_query", lambda query, bounds, timeout=120: gpd.GeoDataFrame())
    aoi = _aoi()
    result = subregions.find_subregions(aoi)
    assert len(result) == 1
    assert result.geometry.iloc[0].equals(aoi.geometry.iloc[0])


def test_full_coverage_subregions_are_used(monkeypatch):
    """Check full coverage subregions are used."""
    # Two candidates splitting the AOI exactly in half - full coverage.
    candidates = _candidates(box(0, 0, 50, 100), box(50, 0, 100, 100))
    monkeypatch.setattr(osm, "overpass_api_query", lambda query, bounds, timeout=120: candidates)
    aoi = _aoi()
    result = subregions.find_subregions(aoi)
    assert len(result) == 2
    assert set(result["name"]) == {"sub0", "sub1"}


def test_partial_coverage_falls_back_to_aoi_whole(monkeypatch):
    """Check partial coverage falls back to aoi whole."""
    # Only a small sliver of the AOI is covered by the "found" district -
    # OSM's own subregion data for this place is incomplete, so splitting
    # on it would silently drop every street outside that sliver.
    candidates = _candidates(box(0, 0, 20, 20))
    monkeypatch.setattr(osm, "overpass_api_query", lambda query, bounds, timeout=120: candidates)
    aoi = _aoi()
    result = subregions.find_subregions(aoi)
    assert len(result) == 1
    assert result.geometry.iloc[0].equals(aoi.geometry.iloc[0])


def test_min_coverage_fraction_is_configurable(monkeypatch):
    """Check min coverage fraction is configurable."""
    # Same partial candidate as above (4% coverage), but with a
    # permissive threshold it should be accepted instead of falling back.
    candidates = _candidates(box(0, 0, 20, 20))
    monkeypatch.setattr(osm, "overpass_api_query", lambda query, bounds, timeout=120: candidates)
    aoi = _aoi()
    result = subregions.find_subregions(aoi, min_coverage_fraction=0.01)
    assert len(result) == 1
    assert result["name"].iloc[0] == "sub0"
