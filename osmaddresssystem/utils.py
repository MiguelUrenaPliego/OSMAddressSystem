from __future__ import annotations

import re
import time
import unicodedata
import os
from rapidfuzz import process, fuzz
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
import geopandas as gpd
import osmnx as ox
from shapely.geometry import Polygon, MultiPolygon, Point, shape
import requests

# Nominatim's usage policy caps clients at roughly one request per
# second: https://operations.osmfoundation.org/policies/nominatim/.
# Every Nominatim call in this module goes through _throttled_get, which
# enforces that minimum spacing and retries a 429 (rate limited) response
# with exponential backoff, rather than giving up immediately - a burst
# of calls (e.g. add_admin_columns reverse-geocoding several polygons in
# a row across many subregions) is expected, not exceptional.
_MIN_NOMINATIM_INTERVAL_S = 1.5
_NOMINATIM_MAX_RETRIES = 8
_NOMINATIM_BACKOFF_BASE_S = 3.0
_NOMINATIM_MAX_BACKOFF_S = 60.0
_last_nominatim_request_time = 0.0


def _throttled_get(url: str, params: dict, headers: dict, timeout: int) -> requests.Response:
    """A ``requests.get`` wrapper that rate-limits and retries for Nominatim.

    Waits at least ``_MIN_NOMINATIM_INTERVAL_S`` since the last call this
    process made to Nominatim, then retries a 429 response with
    exponential backoff, up to ``_NOMINATIM_MAX_RETRIES`` times, before
    giving up and returning whatever response it last got (the caller
    decides what to do with a non-200 response, same as before this
    wrapper existed).

    Args:
        url: The request URL.
        params: Query parameters.
        headers: HTTP headers.
        timeout: Per-request timeout, in seconds.

    Returns:
        The last HTTP response received (whether or not it succeeded).
    """
    global _last_nominatim_request_time

    response = None
    for attempt in range(_NOMINATIM_MAX_RETRIES + 1):
        elapsed = time.monotonic() - _last_nominatim_request_time
        if elapsed < _MIN_NOMINATIM_INTERVAL_S:
            time.sleep(_MIN_NOMINATIM_INTERVAL_S - elapsed)

        response = requests.get(url, params=params, headers=headers, timeout=timeout)
        _last_nominatim_request_time = time.monotonic()

        if response.status_code != 429:
            return response

        if attempt < _NOMINATIM_MAX_RETRIES:
            backoff = min(_NOMINATIM_BACKOFF_BASE_S * (2 ** attempt), _NOMINATIM_MAX_BACKOFF_S)
            print(f"Nominatim rate-limited (429), retrying in {backoff:.1f}s...")
            time.sleep(backoff)

    return response


def geocode(
    q: str, results: int = 1, buffer: float = 0, user_agent: str = "OSMAddressSystem"
) -> gpd.GeoDataFrame | None:
    """Geocode a free-text query via the Nominatim search API.

    Args:
        q: The search query (e.g. an address or place name).
        results: Maximum number of results to request.
        buffer: If > 0, buffer each result point by this many meters
            (in an estimated local UTM CRS) instead of returning bare
            points.
        user_agent: HTTP User-Agent header sent to Nominatim.

    Returns:
        A GeoDataFrame (EPSG:4326) with one row per result (``query``,
        ``display_name``, ``lat``, ``lon``, ``geometry``), or ``None`` if
        the request failed or returned no results.
    """
    try:
        r = _throttled_get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": results},
            headers={"User-Agent": user_agent},
            timeout=8,
        )
        r.raise_for_status()

        data = r.json()
        
        if not data:
            return None
        
        records = []
        
        for item in data:
            lat = float(item["lat"])
            lon = float(item["lon"])
            
            records.append({
                "query": q,
                "display_name": item["display_name"],
                "lat": lat,
                "lon": lon,
                "geometry": Point(lon, lat)
            })
        
        gdf = gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")
        if buffer > 0:
            crs = gdf.crs 
            gdf = gdf.to_crs(gdf.estimate_utm_crs())
            gdf.geometry = gdf.geometry.buffer(buffer)
            gdf = gdf.to_crs(crs)
            
        return gdf
    
    except Exception as e:
        print("Error:", e)
        return None

def get_city_geometry(city_name: str) -> gpd.GeoDataFrame:
    """Download city boundary geometry from OpenStreetMap.

    Args:
        city_name: Name of the city (e.g., "Berlin, Germany").

    Returns:
        GeoDataFrame containing the city boundary polygon in EPSG:4326.
    """
    # Query OSM for place boundary
    gdf = ox.geocode_to_gdf(city_name)

    # Ensure CRS is WGS84
    gdf = gdf.to_crs(epsg=4326)
    return gdf


# Nominatim reverse "zoom" levels, from finest (neighbourhood) to coarsest (country).
# See https://nominatim.org/release-docs/latest/api/Reverse/#result-limitation
_BOUNDARY_ZOOMS = (16, 14, 13, 12, 10, 8, 5, 3)
_ZOOM_LEVEL_NAMES = {
    16: "neighbourhood",
    14: "suburb",
    13: "village",
    12: "town/borough",
    10: "city",
    8: "county",
    5: "state",
    3: "country",
}


def reverse_geocode_boundary(
    lat: float,
    lon: float,
    zooms: tuple[int, ...] = _BOUNDARY_ZOOMS,
    user_agent: str = "OSMAddressSystem",
) -> tuple[str, int, str, Polygon | MultiPolygon] | None:
    """Find the finest-grained OSM administrative/place boundary containing a point.

    Tries Nominatim reverse geocoding from neighbourhood-level zoom down to
    country-level, returning the first boundary polygon found.

    Args:
        lat: Latitude of the point to reverse-geocode.
        lon: Longitude of the point to reverse-geocode.
        zooms: Nominatim reverse zoom levels to try, finest first.
        user_agent: HTTP User-Agent header sent to Nominatim.

    Returns:
        A tuple ``(level_name, zoom, place_name, geometry)``, or ``None``
        if no boundary was found. ``zoom`` is the Nominatim reverse zoom
        level the boundary was found at, out of ``max(zooms)`` (the
        finest level searched).
    """
    for zoom in zooms:
        try:
            r = _throttled_get(
                "https://nominatim.openstreetmap.org/reverse",
                params={
                    "lat": lat,
                    "lon": lon,
                    "format": "jsonv2",
                    "zoom": zoom,
                    "polygon_geojson": 1,
                    "addressdetails": 1,
                },
                headers={"User-Agent": user_agent},
                timeout=8,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"Reverse geocoding failed at zoom {zoom}: {e}")
            continue

        if not data or "geojson" not in data:
            continue
        if data.get("category") not in ("boundary", "place"):
            continue

        geom = shape(data["geojson"])
        if geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue

        name = data.get("name") or data.get("display_name", "").split(",")[0]
        level = data.get("addresstype") or _ZOOM_LEVEL_NAMES.get(zoom, str(zoom))
        return level, zoom, name, geom

    return None


def get_town_aoi(
    location: str | tuple[float, float] | Point,
    buffer_m: float | None = None,
    default_buffer_m: float = 5000,
) -> tuple[gpd.GeoDataFrame, str]:
    """Resolve a location (place name, (lat, lon), or Point) to an AOI and its name.

    The point is reverse-geocoded to the finest-grained OSM administrative or
    neighbourhood boundary containing it; that boundary becomes the AOI and
    its name is used as the town/area name. If no boundary is found, or
    ``buffer_m`` is given, falls back to a buffer (in meters) around the point.

    Args:
        location: A place name to geocode, a ``(lat, lon)`` tuple, or a
            shapely Point (assumed lon/lat, EPSG:4326).
        buffer_m: If given, always use a buffer of this many meters
            around the point instead of looking up an OSM boundary.
        default_buffer_m: Buffer (in meters) used as a fallback when no
            OSM boundary is found and ``buffer_m`` wasn't given.

    Returns:
        A tuple ``(aoi_gdf, name)``: the AOI GeoDataFrame (EPSG:4326) and
        the resolved town/area name.

    Raises:
        ValueError: If ``location`` is a string that can't be geocoded.
    """
    if isinstance(location, str):
        point_gdf = geocode(location, results=1)
        if point_gdf is None or point_gdf.empty:
            raise ValueError(f"Could not geocode '{location}'")
        point = point_gdf.geometry.iloc[0]
        fallback_name = location
    elif isinstance(location, Point):
        point = location
        fallback_name = f"{location.y:.5f}_{location.x:.5f}"
    else:
        lat, lon = location
        point = Point(lon, lat)
        fallback_name = f"{lat:.5f}_{lon:.5f}"

    result = reverse_geocode_boundary(point.y, point.x)

    if result is not None:
        level, zoom, name, geom = result
        max_zoom = max(_BOUNDARY_ZOOMS)
        print(
            f"Found OSM boundary '{name}' at level '{level}' ({zoom}/{max_zoom}) "
            f"for point ({point.y:.5f}, {point.x:.5f})"
        )
        if buffer_m is None:
            aoi = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
            return aoi, name
    else:
        name = fallback_name
        print(f"No OSM boundary found for point ({point.y:.5f}, {point.x:.5f}); using a buffer instead")

    buffer_m = buffer_m or default_buffer_m
    point_gdf = gpd.GeoDataFrame(geometry=[point], crs="EPSG:4326")
    utm_crs = point_gdf.estimate_utm_crs()
    center = point_gdf.to_crs(utm_crs).geometry.iloc[0]
    aoi = gpd.GeoDataFrame(geometry=[center.buffer(buffer_m)], crs=utm_crs)
    return aoi.to_crs(4326), name


def get_geographic_suggestions_from_string(
    query: str,
    user_agent: str = "UrbanAccessAnalyzer",
    max_results: int = 25
) -> dict[str, list[str]]:
    """Suggest country codes, subdivisions, and municipalities for a string.

    Uses OpenStreetMap's Nominatim service. This version collects all
    relevant fields without skipping any. Counties are always included in
    municipalities.

    Args:
        query: The free-text place query to geocode.
        user_agent: HTTP User-Agent identifying this client to Nominatim.
        max_results: Maximum number of candidate locations to request.

    Returns:
        A dict with keys ``country_codes``, ``subdivision_names``, and
        ``municipalities``, each a sorted list of strings collected across
        every returned candidate.
    """
    geolocator = Nominatim(user_agent=user_agent, timeout=10)

    suggested_country_codes = set()
    suggested_subdivision_names = set()
    suggested_municipalities = set()

    try:
        locations = geolocator.geocode(
            query,
            addressdetails=True,
            language='en',
            exactly_one=False,
            limit=max_results
        )
        if locations:
            for location in locations:
                address = location.raw.get('address', {})

                # Country code
                country_code = address.get('country_code')
                if country_code:
                    suggested_country_codes.add(country_code.upper())

                # Collect all possible subdivisions
                for key in ['state', 'province', 'region', 'county']:
                    value = address.get(key)
                    if value:
                        suggested_subdivision_names.add(value)

                # Collect all possible municipalities
                for key in ['city', 'town', 'village', 'county']:
                    value = address.get(key)
                    if value:
                        suggested_municipalities.add(value)

    except (GeocoderTimedOut, GeocoderServiceError) as e:
        print(f"Geocoding failed: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")

    return {
        'country_codes': sorted(suggested_country_codes),
        'subdivision_names': sorted(suggested_subdivision_names),
        'municipalities': sorted(suggested_municipalities),
    }

def get_geographic_suggestions_from_aoi(
    aoi: Polygon | MultiPolygon | gpd.GeoDataFrame | gpd.GeoSeries,
    num_points: int = 1,
    user_agent: str = "MobilityDatabaseClient"
) -> dict[str, list[str]]:
    """Reverse-geocode AOI geometry to suggest country, subdivision, and municipality.

    Args:
        aoi: The area of interest, as a shapely Polygon/MultiPolygon or a
            geopandas GeoDataFrame/GeoSeries.
        num_points: Number of points to reverse-geocode within ``aoi``.
            ``1`` uses the AOI's representative point; more than one
            samples uniformly at random within the AOI's bounding box.
        user_agent: HTTP User-Agent identifying this client to Nominatim.

    Returns:
        A dict with keys ``country_codes``, ``subdivision_names``, and
        ``municipalities``, each a sorted list of strings collected across
        every reverse-geocoded point.

    Raises:
        ValueError: If ``aoi`` is an empty GeoDataFrame/GeoSeries or its
            unioned geometry is empty.
        TypeError: If ``aoi`` is not one of the supported types.
    """
    import random

    if isinstance(aoi, (gpd.GeoDataFrame, gpd.GeoSeries)):
        if aoi.empty:
            raise ValueError("GeoDataFrame/GeoSeries is empty.")
        target_geometry = aoi.to_crs(4326).unary_union
    elif isinstance(aoi, (Polygon, MultiPolygon)):
        target_geometry = aoi
    else:
        raise TypeError("AOI must be Polygon, MultiPolygon, GeoDataFrame, or GeoSeries.")

    if target_geometry.is_empty:
        raise ValueError("AOI geometry is empty.")

    geolocator = Nominatim(user_agent=user_agent, timeout=10)
    suggested_country_codes = set()
    suggested_subdivision_names = set()
    suggested_municipalities = set()

    points_to_geocode: list[Point] = []
    min_lon, min_lat, max_lon, max_lat = target_geometry.bounds

    if num_points <= 0:
        num_points = 1
    if num_points == 1:
        points_to_geocode.append(target_geometry.representative_point())
    else:
        for _ in range(num_points):
            points_to_geocode.append(Point(random.uniform(min_lon, max_lon), random.uniform(min_lat, max_lat)))

    for i, point in enumerate(points_to_geocode):
        lat, lon = point.y, point.x
        print(f"Reverse geocoding point {i+1}/{len(points_to_geocode)}: ({lat}, {lon})")
        try:
            location = geolocator.reverse((lat, lon), language='en')
            if location and location.raw:
                address = location.raw.get('address', {})
                if cc := address.get('country_code'):
                    suggested_country_codes.add(cc.upper())
                if subdivision := address.get('state') or address.get('province') or address.get('region') or address.get('county'):
                    suggested_subdivision_names.add(subdivision)
                if municipality := address.get('city') or address.get('town') or address.get('village') or address.get('county'):
                    suggested_municipalities.add(municipality)
        except (GeocoderTimedOut, GeocoderServiceError) as e:
            print(f"Geocoding failed for point ({lat}, {lon}): {e}")
        except Exception as e:
            print(f"Unexpected error for point ({lat}, {lon}): {e}")

    return {
        'country_codes': sorted(list(suggested_country_codes)),
        'subdivision_names': sorted(list(suggested_subdivision_names)),
        'municipalities': sorted(list(suggested_municipalities))
    }


def get_folder(path: str) -> str | None:
    """
    Returns the directory for a given path.
    - If path is a file (has an extension), returns its parent folder.
    - If path is a folder, returns the normalized folder path.
    - If path is just a filename (e.g. "file.txt"), returns None.
    """
    path = os.path.normpath(path)
    path = os.path.abspath(path)

    # Check if it's a file (has extension)
    if os.path.splitext(path)[1]:
        folder = os.path.dirname(path)
        return folder if folder else None
    else:
        return path


def normalize_text(text: object) -> str:
    """Lowercase + strip accents from a string.

    Args:
        text: The value to normalize; coerced to ``str`` first.

    Returns:
        The lowercased, accent-stripped string.
    """
    text = str(text).lower().strip()
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def sanitize_filename(name: str) -> str:
    """Replaces spaces and invalid filename characters with an underscore.

    Args:
        name: The candidate filename/string to sanitize.

    Returns:
        A filesystem-safe version of ``name`` (normalized, then every
        character outside ``[a-zA-Z0-9_-]`` replaced with ``_``).
    """
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", normalize_text(name))


def gdf_fuzzy_match(
    gdf: gpd.GeoDataFrame, city_name: str, column: str = "NAMEUNIT"
) -> gpd.GeoDataFrame:
    """Find the row in ``gdf`` whose ``column`` best matches ``city_name``.

    Tries an exact match (after normalizing both sides) first, falling
    back to fuzzy token-sort-ratio matching via rapidfuzz.

    Args:
        gdf: The GeoDataFrame to search.
        city_name: The name to match against ``column``.
        column: Name of the column in ``gdf`` holding the names to match.

    Returns:
        A one-row GeoDataFrame slice of ``gdf`` for the best match.
    """
    # Normalize input city name
    norm_city = normalize_text(city_name)

    # Normalize column
    gdf["_match_norm"] = gdf[column].astype(str).apply(normalize_text)

    # Check for exact match first
    exact = gdf[gdf["_match_norm"] == norm_city]
    if not exact.empty:
        return exact.iloc[0:1]

    # Fuzzy match using token_sort_ratio
    choices = gdf["_match_norm"].tolist()
    best_match, score, index = process.extractOne(
        norm_city, choices, scorer=fuzz.token_sort_ratio
    )

    return gdf.iloc[index : index + 1].drop(columns=["_match_norm"])
