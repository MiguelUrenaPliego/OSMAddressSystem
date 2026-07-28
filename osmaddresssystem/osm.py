from __future__ import annotations

import os
import requests
import tempfile
import shapely
import geopandas as gpd
import networkx as nx
import pandas as pd
import osmnx as ox
from osm2geojson import json2geojson
import time

from . import utils
from .default_values import DEFAULT_PARKING_SERVICE_VALUES, DEFAULT_PRIVATE_ACCESS_VALUES


def _write_poly_file(aoi: gpd.GeoDataFrame | gpd.GeoSeries, poly_path: str) -> None:
    """Write AOI geometry to a .poly file in Osmosis format, correctly
    handling Polygons, MultiPolygons, and interior rings (holes).

    Args:
        aoi: The area of interest geometry.
        poly_path: Path of the .poly file to write.

    Raises:
        ValueError: If the unioned AOI geometry isn't a Polygon or
            MultiPolygon.
    """
    # Ensure a single, valid geometry in WGS84 projection
    geom = aoi.to_crs(4326).union_all()

    with open(poly_path, "w") as f:
        f.write("aoi\n")  # Polygon name

        def write_ring(coords, ring_id: int, is_hole: bool = False) -> None:
            """Helper to write a single ring to the file.

            Args:
                coords: Iterable of (x, y) coordinate tuples for the ring.
                ring_id: Sequential ring number within this .poly file.
                is_hole: Whether this ring is an interior (hole) ring.
            """
            prefix = "!" if is_hole else ""
            f.write(f"{prefix}{ring_id}\n")
            for x, y in coords:
                # Use sufficient precision for coordinates
                f.write(f"  {x:.7f} {y:.7f}\n")
            f.write("END\n")

        ring_counter = 1

        # Standardize geometry into a list of polygons
        polygons = []
        if geom.geom_type == "Polygon":
            polygons.append(geom)
        elif geom.geom_type == "MultiPolygon":
            polygons.extend(geom.geoms)
        else:
            raise ValueError(
                f"Unsupported geometry type for .poly file: {geom.geom_type}"
            )

        for poly in polygons:
            # Write the exterior ring
            write_ring(poly.exterior.coords, ring_counter)
            ring_counter += 1
            # Write all interior rings (holes)
            for interior in poly.interiors:
                write_ring(interior.coords, ring_counter, is_hole=True)
                ring_counter += 1

        f.write("END\n")  # Final END for the entire polygon definition


def download_geofabrik(
    aoi: gpd.GeoDataFrame | gpd.GeoSeries, output_folder: str | None = None
) -> str:
    """Finds the smallest Geofabrik region that contains the AOI and downloads the PBF file.

    Args:
        aoi: A geopandas GeoDataFrame, GeoSeries, or a single shapely geometry.
        output_folder: The folder where the downloaded PBF file will be saved.

    Returns:
        The full path to the downloaded .osm.pbf file.

    Raises:
        ValueError: If no Geofabrik region is found that fully contains
            the AOI.
    """
    # Ensure the input AOI is a single shapely geometry in the correct CRS (EPSG:4326)

    aoi = aoi.to_crs(4326)
    aoi_geom = aoi.union_all()
    if not aoi_geom.is_valid:
        print("Validity problem:", shapely.validation.explain_validity(aoi_geom))

    # Load Geofabrik regions metadata from the current index file
    url = "https://download.geofabrik.de/index-v1.json"
    print(f"Fetching Geofabrik index from {url}...")
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()

    candidate_regions = []

    # Iterate through the flat list of features in the GeoJSON
    for feature in data["features"]:
        properties = feature.get("properties", {})

        # Region is only a candidate if it has a PBF URL and a geometry
        if not properties.get("urls", {}).get("pbf") or not feature.get("geometry"):
            continue

        try:
            # Create a shapely geometry from the GeoJSON feature
            region_geom = shapely.geometry.shape(feature["geometry"])
        except Exception as e:
            print(
                f"Warning: Could not process geometry for {properties.get('name', 'N/A')}: {e}"
            )
            continue

        # Check if the region's polygon completely contains the AOI.
        # This is more accurate than comparing bounding boxes.
        if region_geom.contains(aoi_geom):
            area = region_geom.area
            candidate_regions.append((area, properties))

    if not candidate_regions:
        raise ValueError(
            "No Geofabrik region was found to contain your AOI. Please ensure the AOI is correct and within a single region."
        )

    # Sort candidates by area (smallest first) to find the best fit
    candidate_regions.sort(key=lambda x: x[0])
    best_region = candidate_regions[0][1]

    pbf_url = best_region.get("urls", {}).get("pbf")

    # Prepare output filename & path
    safe_name = utils.sanitize_filename(best_region.get("name", "region"))
    filename = f"{safe_name}.osm.pbf"

    if output_folder is not None:
        os.makedirs(output_folder, exist_ok=True)
        output_file = os.path.join(output_folder, filename)
    else:
        output_file = filename

    if os.path.exists(output_file):
        print(f"File '{output_file}' already exists. Skipping download.")
        return output_file

    print(f"Downloading '{best_region['name']}' from {pbf_url} ...")
    pbf_response = requests.get(pbf_url, stream=True)
    pbf_response.raise_for_status()

    # Download to a temp file first and rename atomically once complete, so
    # an interrupted download can never be mistaken for a valid cached file.
    tmp_file = output_file + ".part"
    try:
        with open(tmp_file, "wb") as f:
            for chunk in pbf_response.iter_content(chunk_size=8192):
                f.write(chunk)
        os.replace(tmp_file, output_file)
    finally:
        if os.path.exists(tmp_file):
            os.remove(tmp_file)

    print(f"Downloaded geofabrik to {output_file}")
    return output_file


def build_osmium_filter_args(tag_filters: dict[str, set[str] | None]) -> str:
    """Build an ``osmium tags-filter`` argument string from tag/value filters.

    Args:
        tag_filters: Mapping of OSM tag key to the set of acceptable
            values, or ``None``/empty set to match the key with any value.

    Returns:
        A space-separated string of ``osmium tags-filter`` ``w/key[=value]``
        arguments (way filters only).
    """
    parts = []
    for k, vs in tag_filters.items():
        if vs is None or len(vs) == 0:
            # Filter by key only (any value)
            parts.append(f"w/{k}")
        else:
            for v in vs:
                parts.append(f"w/{k}={v}")
    return " ".join(parts)


def osmium_network_filter(network_type: str) -> str:
    """Build the ``osmium tags-filter`` argument string for a network type.

    Args:
        network_type: One of ``"walk"``, ``"bike"``, ``"drive"``,
            ``"all"``, ``"walk+bike"``, or ``"walk+bike+primary"``.

    Returns:
        The ``osmium tags-filter`` argument string for that network type.

    Raises:
        ValueError: If ``network_type`` is not one of the recognized values.
    """
    walk_highways = {
        "footway",
        "pedestrian",
        "path",
        "living_street",
        "steps",
        "residential",
        "service",
        "unclassified",
        "track",
    }
    bike_highways = {
        "cycleway",
        "path",
        "residential",
        "living_street",
        "unclassified",
        "service",
        "track",
    }
    drive_highways = {
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
        "residential",
        "unclassified",
        "service",
        "living_street",
    }
    primary_highways = {
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
        "residential",
        "unclassified",
        "service",
        "living_street",
    }

    if network_type == "walk":
        # ways with walk highways + foot=yes ways
        tag_filters = {
            "highway": walk_highways,
            "foot": {"yes"},
        }

    elif network_type == "bike":
        # ways with bike highways + bicycle=yes ways
        tag_filters = {
            "highway": bike_highways,
            "bicycle": {"yes"},
        }

    elif network_type == "drive":
        # only drive highways
        tag_filters = {
            "highway": drive_highways,
        }

    elif network_type == "all":
        # all highways
        tag_filters = {
            "highway": None,
        }

    elif network_type == "walk+bike":
        # combine walk+bike highways, plus foot=yes or bicycle=yes, exclude drive highways
        combined = walk_highways.union(bike_highways)
        tag_filters = {
            "highway": combined,
            "foot": {"yes"},
            "bicycle": {"yes"},
        }
    elif network_type == "walk+bike+primary":
        # combine walk+bike highways, plus foot=yes or bicycle=yes, exclude drive highways
        combined = walk_highways.union(bike_highways)
        combined = combined.union(primary_highways)
        tag_filters = {
            "highway": combined,
            "foot": {"yes"},
            "bicycle": {"yes"},
        }

    else:
        raise ValueError(f"Unknown network_type: {network_type}")

    return build_osmium_filter_args(tag_filters)


def geofabrik_to_osm(
    output_file: str,
    input_file: str = "",
    aoi: gpd.GeoDataFrame | gpd.GeoSeries | None = None,
    osmium_filter_args: str = "",
    overwrite: bool = False,
) -> str:
    """Convert a (Geofabrik) PBF file to a filtered/clipped .osm file.

    Optionally applies an osmium tag filter, then either clips to an AOI
    polygon or just converts format, via the ``osmium`` CLI.

    Args:
        output_file: Path to write the resulting .osm file to.
        input_file: Path to an existing input PBF file. If missing/empty,
            it's downloaded via ``download_geofabrik(aoi, ...)``.
        aoi: Area of interest to clip the output to; if ``None``, no
            geometric clipping is performed.
        osmium_filter_args: ``osmium tags-filter`` argument string (see
            ``build_osmium_filter_args``); empty string skips tag filtering.
        overwrite: If False and ``output_file`` already exists, skip
            conversion and return the existing path.

    Returns:
        The path to the resulting .osm file (``output_file``).

    Raises:
        RuntimeError: If the ``osmium`` tag-filter, extract, or convert
            command exits non-zero.
    """
    if os.path.isfile(output_file) and (not overwrite):
        print(f"File '{output_file}' already exists. Skipping conversion.")
        return output_file

    # Your download logic (assumed to be working)
    if not input_file or not os.path.isfile(input_file):
        input_path = utils.get_folder(input_file)
        print(
            f"File {input_file} does not exist. Downloading best matching geofabrik file."
        )
        input_file = download_geofabrik(aoi, input_path)

    current_file = input_file
    tag_filter_temp_file_path = None

    try:
        # Step 1: Apply tag filter (if any) to an intermediate temporary file
        if osmium_filter_args:
            # Create a temp file, get its name, and close it immediately
            with tempfile.NamedTemporaryFile(suffix=".pbf", delete=False) as f:
                tag_filter_temp_file_path = f.name

            print(f"Applying tag filter: {osmium_filter_args}")
            cmd_filter = f"osmium tags-filter --overwrite {current_file} {osmium_filter_args} -o {tag_filter_temp_file_path}"
            if os.system(cmd_filter) != 0:
                raise RuntimeError("Tag filtering failed")
            current_file = tag_filter_temp_file_path  # The next step uses this new file

        # Step 2: Apply geometry filter or just convert
        if aoi is not None:
            # Create, write, and CLOSE the .poly file BEFORE using it
            poly_temp_file_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".poly", delete=False) as f:
                    poly_temp_file_path = f.name

                print("Creating .poly file for AOI clipping...")
                _write_poly_file(
                    aoi, poly_temp_file_path
                )  # Write to the closed file's path

                print("Extracting by geometry...")
                cmd_extract = f"osmium extract --overwrite --polygon {poly_temp_file_path} {current_file} -o {output_file} -f osm"
                if os.system(cmd_extract) != 0:
                    raise RuntimeError("Geometry extraction failed")
            finally:
                # Ensure the .poly file is always cleaned up
                if poly_temp_file_path and os.path.exists(poly_temp_file_path):
                    os.remove(poly_temp_file_path)
        else:
            # No AOI, just convert the format
            print("No AOI provided, converting to .osm format...")
            cmd_convert = f"osmium cat {current_file} -o {output_file} -f osm"
            if os.system(cmd_convert) != 0:
                raise RuntimeError("OSM conversion failed")

    finally:
        # Final cleanup: remove the intermediate tag-filtered file if it was created
        if tag_filter_temp_file_path and os.path.exists(tag_filter_temp_file_path):
            os.remove(tag_filter_temp_file_path)

    print("Finished. Final output:", output_file)
    return output_file


def download_town_streets(
    location: str | tuple[float, float],
    town_streets_path: str,
    country_streets_folder: str,
    network_type: str = "all",
    buffer_m: float | None = None,
    aoi_override: tuple[gpd.GeoDataFrame, str] | None = None,
) -> tuple[str, gpd.GeoDataFrame, str]:
    """Download and crop the street network around a location.

    ``location`` (a place name or a (lat, lon) point) is resolved to an AOI
    via ``utils.get_town_aoi``: the finest-grained OSM administrative or
    neighbourhood boundary containing the point, or a buffer around it.
    Downloads the best-matching Geofabrik region PBF into
    ``country_streets_folder`` and crops it to that AOI, saving the result
    to ``town_streets_path``.

    ``get_town_aoi`` always picks the FINEST boundary containing a point,
    which for a large multi-district city can be a single district rather
    than the whole city - e.g. reverse-geocoding a point in Douala can
    resolve to the "Douala II" commune alone, never the wider "Communauté
    urbaine de Douala" that actually contains every district. When the
    whole-city polygon is what's wanted instead (so its own districts can
    be found and processed via ``subregions.find_subregions``), resolve it
    separately (e.g. via ``utils.get_city_geometry``) and pass it as
    ``aoi_override`` to use it here directly, skipping ``get_town_aoi``
    entirely.

    Args:
        location: A place name to geocode, a ``(lat, lon)`` tuple, or a
            shapely Point. Ignored (but still required by the signature)
            when ``aoi_override`` is given.
        town_streets_path: Where to write the cropped ``.osm`` file.
        country_streets_folder: Folder for the cached Geofabrik ``.osm.pbf``.
        network_type: Passed to ``osmium_network_filter``.
        buffer_m: Passed to ``utils.get_town_aoi``. Ignored when
            ``aoi_override`` is given.
        aoi_override: An already-resolved ``(aoi, name)`` pair to use
            instead of resolving ``location`` via ``utils.get_town_aoi``.

    Returns:
        A tuple ``(town_streets_path, aoi, name)``.
    """
    if aoi_override is not None:
        aoi, name = aoi_override
    else:
        aoi, name = utils.get_town_aoi(location, buffer_m=buffer_m)

    os.makedirs(country_streets_folder, exist_ok=True)
    os.makedirs(utils.get_folder(town_streets_path), exist_ok=True)

    pbf_path = download_geofabrik(aoi, output_folder=country_streets_folder)
    network_filter = osmium_network_filter(network_type)

    geofabrik_to_osm(
        town_streets_path,
        input_file=pbf_path,
        aoi=aoi,
        osmium_filter_args=network_filter,
        overwrite=False,
    )
    return town_streets_path, aoi, name


def download_street_graph(
    bounds: gpd.GeoSeries | gpd.GeoDataFrame,
    network_type: str = "walk",
    custom_filter: str | None = None,
) -> nx.MultiDiGraph:
    """Download a street network graph for an area via osmnx and project it.

    Args:
        bounds: Area to download the network for.
        network_type: osmnx network type (e.g. ``"walk"``, ``"drive"``,
            ``"all"``); ignored if ``custom_filter`` is given.
        custom_filter: An osmnx/Overpass custom way filter string, or
            ``None`` to use ``network_type``.

    Returns:
        The downloaded street network, projected to ``bounds``'s CRS (or
        an estimated UTM CRS if ``bounds`` isn't already projected).
    """
    if bounds.crs.is_projected:
        crs = bounds.crs
    else:
        crs = bounds.estimate_utm_crs()

    G = ox.graph.graph_from_polygon(
        bounds.to_crs(4326).union_all(),
        network_type=network_type,
        simplify=True,
        retain_all=True,
        truncate_by_edge=True,
        custom_filter=custom_filter,
    )
    G = ox.projection.project_graph(G, to_crs=crs)

    return G


def largest_real_component(
    G: nx.MultiDiGraph,
    private_access_values: set | None = None,
    parking_service_values: set | None = None,
) -> nx.MultiDiGraph:
    """Keep only the largest connected component of a street graph's own
    REAL (non-private, non-parking-aisle) network.

    A raw downloaded graph can include disconnected fragments: a
    driveway behind a gate, a parking lot's own internal aisles, a
    genuinely separate scrap of road OSM has mapped but that isn't
    actually reachable from the rest of the network as far as
    ``label_streets``/``assign_street_directionality`` are concerned.
    Computing "largest component" over the whole graph as given would
    let a large private estate's own internal road network count as
    real, or split the real public network's own single component in
    two around a private inholding. So this is computed over a COPY
    with private/parking edges already removed (``access`` matching
    ``private_access_values``, or ``service`` matching
    ``parking_service_values``), then the corresponding node set is used
    to select the returned subgraph from the ORIGINAL graph - the
    private/parking edges themselves are kept in the result, they just
    never influence which component counts as "largest".

    Every place in this codebase that loads a street network graph
    should call this on it before running ``street_ids.label_streets``,
    so a caller never has to remember to do it themselves - see
    ``load_projected_graph`` and ``pipeline.clip_graph_to_polygon``,
    which already do.

    Args:
        G: The street network graph (osmnx-style ``MultiDiGraph``).
        private_access_values: OSM ``access`` tag values that mean an
            edge is private (default ``DEFAULT_PRIVATE_ACCESS_VALUES``).
        parking_service_values: OSM ``service`` tag values that mean an
            edge is a parking-lot internal way (default
            ``DEFAULT_PARKING_SERVICE_VALUES``).

    Returns:
        The subgraph of ``G`` (a copy) induced by the largest weakly
        connected component of its own real (non-private, non-parking)
        edges. If removing private/parking edges leaves no nodes at all
        (a degenerate, all-private graph), ``G`` itself is returned
        unchanged rather than an empty graph.
    """
    private_values = private_access_values or DEFAULT_PRIVATE_ACCESS_VALUES
    parking_values = parking_service_values or DEFAULT_PARKING_SERVICE_VALUES

    real_only = G.copy()
    private_or_parking = [
        (u, v, k)
        for u, v, k, data in G.edges(keys=True, data=True)
        if str(data.get("access", "")).strip().lower() in private_values
        or str(data.get("service", "")).strip().lower() in parking_values
    ]
    real_only.remove_edges_from(private_or_parking)
    if real_only.number_of_nodes() == 0:
        return G

    largest_nodes = max(nx.weakly_connected_components(real_only), key=len)
    return G.subgraph(largest_nodes).copy()


def load_projected_graph(
    town_streets_path: str,
    aoi: gpd.GeoDataFrame,
    private_access_values: set | None = None,
    parking_service_values: set | None = None,
) -> nx.MultiDiGraph:
    """Load a cropped ``.osm`` file (see ``download_town_streets``) into
    a projected, real-network-only graph, ready for
    ``street_ids.label_streets``.

    Combines the three steps every caller in this codebase needs after
    downloading a town's own streets: ``ox.graph_from_xml``, projecting
    to ``aoi``'s own estimated UTM CRS, and reducing to the largest real
    component (``largest_real_component`` - always applied, never
    optional, so a caller never has to remember it themselves).

    Args:
        town_streets_path: Path to the ``.osm`` file, as written by
            ``download_town_streets``.
        aoi: The town's own AOI (used only for its CRS estimate).
        private_access_values: Passed through to
            ``largest_real_component``.
        parking_service_values: Passed through to
            ``largest_real_component``.

    Returns:
        The projected, largest-real-component street graph.
    """
    G = ox.graph_from_xml(town_streets_path)
    G = ox.project_graph(G, to_crs=aoi.estimate_utm_crs())
    return largest_real_component(
        G,
        private_access_values=private_access_values,
        parking_service_values=parking_service_values,
    )


def overpass_api_query(
    query: str, bounds: gpd.GeoDataFrame | gpd.GeoSeries, timeout: int = 120
) -> gpd.GeoDataFrame:
    """Run an Overpass QL query, trying several public mirror servers.

    ``{{bbox}}`` in ``query`` is substituted with ``bounds``'s bounding
    box (as "south,west,north,east"), and the query's declared output
    format is forced to JSON regardless of what ``query`` requests.

    Args:
        query: Overpass QL query text, with an optional ``{{bbox}}``
            placeholder.
        bounds: Area used both to fill in ``{{bbox}}`` and to filter/
            reproject the returned features.
        timeout: Per-request HTTP timeout, in seconds.

    Returns:
        A GeoDataFrame of the query results, reprojected to ``bounds``'s
        CRS and filtered to features intersecting ``bounds``, with OSM
        tags expanded into columns.

    Raises:
        RuntimeError: If every Overpass mirror server fails.
    """

    bbox = bounds.to_crs(4326).total_bounds
    bbox_str = f"{bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]}"

    query = query.replace("{{bbox}}", bbox_str)
    query = query.replace("[out:xml]", "[out:json]")

    overpass_urls = [
        "https://overpass-api.de/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]

    response_json = None

    for i, url in enumerate(overpass_urls, start=1):
        try:
            response = requests.get(
                url,
                params={"data": query},
                timeout=timeout,
                headers={"User-Agent": "Python Overpass Client"},
            )

            if response.status_code != 200:
                print(f"Warning: server {i}/{len(overpass_urls)} failed ({url}) HTTP {response.status_code}")
                if i < len(overpass_urls):
                    print(f"Retrying with next Overpass server...")
                continue

            try:
                response_json = response.json()
            except Exception as e:
                print(f"Warning: server {i}/{len(overpass_urls)} failed JSON decode ({e})")
                if i < len(overpass_urls):
                    print("Retrying with next Overpass server...")
                continue

            if "elements" not in response_json:
                print(f"Warning: server {i}/{len(overpass_urls)} missing 'elements'")
                if i < len(overpass_urls):
                    print("Retrying with next Overpass server...")
                continue

            break  # success (even if empty)

        except Exception as e:
            print(f"Warning: server {i}/{len(overpass_urls)} request error ({e})")
            if i < len(overpass_urls):
                print("Retrying with next Overpass server...")
            time.sleep(1)
            continue

    if response_json is None:
        raise RuntimeError("All Overpass servers failed.")

    geojson_response = json2geojson(response_json)

    gdf = gpd.GeoDataFrame.from_features(
        geojson_response,
        crs="EPSG:4326",
    ).reset_index(drop=True)

    # Empty result (no retry)
    if len(gdf) == 0:
        print("Warning: No OSM features found for this query.")
        return gdf.to_crs(bounds.crs)

    # Expand tags safely
    if "tags" in gdf.columns:
        tags = gdf["tags"].apply(pd.Series)

        if "type" in tags.columns:
            tags = tags.rename(columns={"type": "geometry_type"})

        gdf = pd.concat(
            [gdf.drop(columns=["tags"]), tags],
            axis=1,
        )

    gdf = gdf.loc[:, ~gdf.columns.duplicated()]
    gdf = gdf.to_crs(bounds.crs)
    gdf = gdf[gdf.geometry.intersects(bounds.union_all())]

    return gdf

def green_areas(
    bounds: gpd.GeoDataFrame | gpd.GeoSeries,
    intersected_geom: gpd.GeoDataFrame | gpd.GeoSeries | None = None,
    min_area: float = 200,
    min_width: float = 10,
    buffer: float = 5,
) -> gpd.GeoDataFrame:
    """Fetch green-space polygons (gardens, parks, forests, grass) from OSM.

    Applies a shrink/grow/shrink buffer sequence to simplify the geometry
    and drop features narrower than ``min_width``, then optionally
    restricts results to those intersecting ``intersected_geom``.

    Args:
        bounds: Area to query and to reproject results back to.
        intersected_geom: If given, only keep parts intersecting this
            geometry.
        min_area: Minimum polygon area (in the estimated UTM CRS's
            units, normally m²) to keep before the buffer sequence.
        min_width: Minimum feature width (in meters) to survive the
            shrink/grow/shrink buffer sequence.
        buffer: Amount (in meters) the grow/shrink steps add back after
            the initial shrink.

    Returns:
        A GeoDataFrame of green-area polygons, reprojected to
        ``bounds``'s CRS.
    """
    query = """
        [out:json][timeout:25];
        (
        node[leisure = "garden"]({{bbox}});
        node[leisure = "park"]({{bbox}});
        node[landuse = "greenfield"]({{bbox}});
        node[landuse = "grass"]({{bbox}});
        node[landuse = "forest"]({{bbox}});
        way[leisure = "garden"]({{bbox}});
        way[leisure = "park"]({{bbox}});
        way[landuse = "greenfield"]({{bbox}});
        way[landuse = "grass"]({{bbox}});
        way[landuse = "forest"]({{bbox}});
        relation[leisure = "garden"]({{bbox}});
        relation[leisure = "park"]({{bbox}});
        relation[landuse = "greenfield"]({{bbox}});
        relation[landuse = "grass"]({{bbox}});
        relation[landuse = "forest"]({{bbox}});
        );
        out body;
        >;
        out skel qt;
    """
    green_areas_gdf = overpass_api_query(query,bounds)  # query,bounds)
    crs = green_areas_gdf.estimate_utm_crs()
    green_areas_gdf = green_areas_gdf.to_crs(crs)
    green_areas_gdf = green_areas_gdf[green_areas_gdf.geometry.area > min_area]
    green_areas_gdf = green_areas_gdf.geometry.union_all()
    green_areas_gdf = shapely.buffer(green_areas_gdf,-min_width,quad_segs=2)
    green_areas_gdf = shapely.buffer(green_areas_gdf,buffer+min_width,quad_segs=2)
    green_areas_gdf = shapely.buffer(green_areas_gdf,-buffer,quad_segs=2)
    green_areas_gdf = gpd.GeoDataFrame({},geometry=shapely.get_parts(green_areas_gdf),crs=crs)

    if intersected_geom is not None:
        intersected_geom_union = (
            intersected_geom
            .to_crs(green_areas_gdf.crs)
            .union_all()
        )
        geoms = list(green_areas_gdf.geometry)
        shapely.prepare(geoms)
        green_areas_gdf = green_areas_gdf[shapely.intersects(geoms, intersected_geom_union)]

    return green_areas_gdf.to_crs(bounds.crs)


def bus_stops(bounds: gpd.GeoDataFrame | gpd.GeoSeries) -> gpd.GeoDataFrame:
    """Fetch bus stop points from OSM via Overpass.

    Args:
        bounds: Area to query and to reproject results back to.

    Returns:
        A GeoDataFrame of matching features, reprojected to ``bounds``'s CRS.
    """
    query = """
        [out:json][timeout:25];
        (
        node["highway"="bus_stop"]({{bbox}});
        );
        out body;
        >;
        out skel qt;
    """
    stops = overpass_api_query(query, bounds)
    return stops.to_crs(bounds.crs)


def schools(bounds: gpd.GeoDataFrame | gpd.GeoSeries) -> gpd.GeoDataFrame:
    """Fetch school points/ways/relations from OSM via Overpass.

    Args:
        bounds: Area to query and to reproject results back to.

    Returns:
        A GeoDataFrame of matching features, reprojected to ``bounds``'s CRS.
    """
    query = """
        [out:xml] [timeout:25];
        (
            node["amenity"="school"]({{bbox}});
            way["amenity"="school"]({{bbox}});
            relation["amenity"="school"]({{bbox}});
        );
        (._;>;);
        out body;
    """
    pois = overpass_api_query(query, bounds)
    return pois.to_crs(bounds.crs)


def healthcare(bounds: gpd.GeoDataFrame | gpd.GeoSeries) -> gpd.GeoDataFrame:
    """Fetch healthcare facility points/ways/relations (hospitals, clinics, doctors, pharmacies via the generic healthcare tag) from OSM via Overpass.

    Args:
        bounds: Area to query and to reproject results back to.

    Returns:
        A GeoDataFrame of matching features, reprojected to ``bounds``'s CRS.
    """
    query = """
        [out:xml] [timeout:25];
        (
            node["amenity"~"hospital|clinic|doctors|healthcare"]({{bbox}});
            way["amenity"~"hospital|clinic|doctors|healthcare"]({{bbox}});
            relation["amenity"~"hospital|clinic|doctors|healthcare"]({{bbox}});
            
            node["healthcare"]({{bbox}});
            way["healthcare"]({{bbox}});
            relation["healthcare"]({{bbox}});
        );
        (._;>;);
        out body;
    """
    pois = overpass_api_query(query, bounds)
    return pois.to_crs(bounds.crs)


def groceries(bounds: gpd.GeoDataFrame | gpd.GeoSeries) -> gpd.GeoDataFrame:
    """Fetch grocery/supermarket/marketplace points/ways/relations from OSM via Overpass.

    Args:
        bounds: Area to query and to reproject results back to.

    Returns:
        A GeoDataFrame of matching features, reprojected to ``bounds``'s CRS.
    """
    query = """[out:xml][timeout:25];
    (
        node["shop"~"supermarket|grocery|convenience"]({{bbox}});
        way["shop"~"supermarket|grocery|convenience"]({{bbox}});
        relation["shop"~"supermarket|grocery|convenience"]({{bbox}});
        
        node["amenity"="marketplace"]({{bbox}});
        way["amenity"="marketplace"]({{bbox}});
        relation["amenity"="marketplace"]({{bbox}});
    );
    (._;>;);
    out body;
    """ 
    pois = overpass_api_query(query, bounds)
    return pois.to_crs(bounds.crs)


def shops(bounds: gpd.GeoDataFrame | gpd.GeoSeries) -> gpd.GeoDataFrame:
    """Fetch generic shop points/ways/relations from OSM via Overpass.

    Args:
        bounds: Area to query and to reproject results back to.

    Returns:
        A GeoDataFrame of matching features, reprojected to ``bounds``'s CRS.
    """
    query = """[out:xml][timeout:25];
        (
            node["shop"]({{bbox}});
            way["shop"]({{bbox}});
            relation["shop"]({{bbox}});
        );
        (._;>;);
        out body;
    """
    pois = overpass_api_query(query, bounds)
    return pois.to_crs(bounds.crs)


def restaurants(bounds: gpd.GeoDataFrame | gpd.GeoSeries) -> gpd.GeoDataFrame:
    """Fetch restaurant/bar/pub/cafe/fast-food points/ways/relations from OSM via Overpass.

    Args:
        bounds: Area to query and to reproject results back to.

    Returns:
        A GeoDataFrame of matching features, reprojected to ``bounds``'s CRS.
    """
    query = """[out:xml][timeout:25];
        (
            node["amenity"~"restaurant|bar|pub|cafe|fast_food"]({{bbox}});
            way["amenity"~"restaurant|bar|pub|cafe|fast_food"]({{bbox}});
            relation["amenity"~"restaurant|bar|pub|cafe|fast_food"]({{bbox}});
        );
        (._;>;);
        out body;
    """
    pois = overpass_api_query(query, bounds)
    return pois.to_crs(bounds.crs)


def libraries(bounds: gpd.GeoDataFrame | gpd.GeoSeries) -> gpd.GeoDataFrame:
    """Fetch library points/ways/relations from OSM via Overpass.

    Args:
        bounds: Area to query and to reproject results back to.

    Returns:
        A GeoDataFrame of matching features, reprojected to ``bounds``'s CRS.
    """
    query = """[out:xml][timeout:25];
        (
            node["amenity"="library"]({{bbox}});
            way["amenity"="library"]({{bbox}});
            relation["amenity"="library"]({{bbox}});
        );
        (._;>;);
        out body;
    """
    pois = overpass_api_query(query, bounds)
    return pois.to_crs(bounds.crs)


def pharmacies(bounds: gpd.GeoDataFrame | gpd.GeoSeries) -> gpd.GeoDataFrame:
    """Fetch pharmacy points/ways/relations from OSM via Overpass.

    Args:
        bounds: Area to query and to reproject results back to.

    Returns:
        A GeoDataFrame of matching features, reprojected to ``bounds``'s CRS.
    """
    query = """[out:xml][timeout:25];
        (
            node["amenity"="pharmacy"]({{bbox}});
            way["amenity"="pharmacy"]({{bbox}});
            relation["amenity"="pharmacy"]({{bbox}});
        );
        (._;>;);
        out body;
    """
    pois = overpass_api_query(query, bounds)
    return pois.to_crs(bounds.crs)


def gyms(bounds: gpd.GeoDataFrame | gpd.GeoSeries) -> gpd.GeoDataFrame:
    """Fetch gym points/ways/relations from OSM via Overpass.

    Args:
        bounds: Area to query and to reproject results back to.

    Returns:
        A GeoDataFrame of matching features, reprojected to ``bounds``'s CRS.
    """
    query = """
        [out:xml][timeout:25];
        (
            node["amenity"="gym"]({{bbox}});
            way["amenity"="gym"]({{bbox}});
            relation["amenity"="gym"]({{bbox}});
        );
        (._;>;);
        out body;
    """
    pois = overpass_api_query(query, bounds)
    return pois.to_crs(bounds.crs)


def cinemas(bounds: gpd.GeoDataFrame | gpd.GeoSeries) -> gpd.GeoDataFrame:
    """Fetch cinema points/ways/relations from OSM via Overpass.

    Args:
        bounds: Area to query and to reproject results back to.

    Returns:
        A GeoDataFrame of matching features, reprojected to ``bounds``'s CRS.
    """
    query = """
        [out:xml][timeout:25];
        (
            node["amenity"="cinema"]({{bbox}});
            way["amenity"="cinema"]({{bbox}});
            relation["amenity"="cinema"]({{bbox}});
        );
        (._;>;);
        out body;
    """
    pois = overpass_api_query(query, bounds)
    return pois.to_crs(bounds.crs)
