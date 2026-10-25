"""Nuevos Ministerios, Madrid: download streets and assign street ids.

Downloads the street network around a point in/near Nuevos Ministerios,
Madrid, crops it to the finest OSM boundary containing that point (or a
buffer, see BUFFER_M below), and runs street_ids.label_streets on it (see
report/street_identity.md for what that does and why).

Outputs nodes.gpkg, edges.gpkg, and streets.csv next to this script.
"""

import os

import geopandas as gpd
import networkx as nx
from shapely.geometry import Point

from osmaddresssystem import osm, pipeline, street_direction, street_ids

# --- Hyperparameters --------------------------------------------------

# Center of the download, and origin for street directionality (lat, lon).
# Nuevos Ministerios, Madrid.
LOCATION = (40.4460, -3.6928)  # (lat, lon)

NETWORK_TYPE = "all"  # "drive", "walk", "bike", "all", ...
BUFFER_M = None  # None -> use the finest OSM boundary containing the point, else buffer (m)

# Strip every edge's real OSM `name` before running label_streets, so
# real street names never factor into street_id assignment here (no
# fuzzy name matching, no name-donation) - only geometry/rank/dual-
# carriageway/parallel-proximity matching. This is specific to this one
# example script, not a general library default: label_streets itself
# still uses real OSM names whenever they're present, and the
# tests/test_files/nuevos_ministerios/ fixture (which real-data name-
# matching tests depend on as ground truth) is generated separately,
# WITH real names, precisely so flipping this doesn't affect that test
# suite. See the note at the top of tests/test_street_ids_real_data.py.
DROP_OSM_NAMES = True

# Streets within this many degrees of each other (undirected orientation,
# folded into 0-90 deg) are considered the same street when crossing an
# intersection with other equal-rank streets.
ANGLE_THRESHOLD_DEG = 10

# Looser per-node_id direction-binning tolerance (deg) - see
# report/street_identity.md "No more than one direction per node_id".
NODE_DIRECTION_TOLERANCE_DEG = 30

# Parallel-grouping distance thresholds (meters) - see
# report/street_identity.md "Parallel grouping and true parallelism".
# Bumped up from the library defaults (15m/5m) for the same wide-avenue
# reason as NODE_CLUSTER_M below (Paseo de la Castellana's two
# carriageways run wider apart than a typical street).
PARALLEL_PASS_TOUCHING_DIST_M = 30
PARALLEL_PASS_NON_TOUCHING_DIST_M = 10

# Graph nodes closer than this (meters) are treated as one real
# intersection - enables dual-carriageway auto-merging and connector-
# edge bridging. See report/street_identity.md "Node clustering".
# Nuevos Ministerios has some wide, divided avenues (Paseo de la
# Castellana), so this is bumped up to 30m from the library default of
# 15m.
NODE_CLUSTER_M = 30.0

# Distance (meters) within which an edge's own two endpoints must both
# fall of another street's combined geometry to be merged into it, even
# when neither endpoint clusters with any single node of that street -
# see report/street_identity.md "Parallel-street proximity merge".
# Disabled (0) in the library by default; enabled here for the same
# wide-avenue reason as NODE_CLUSTER_M above.
PARALLEL_DIST_M = 30.0

# Minimum fuzzy name-similarity score (0-100) for two named edges to
# be treated as the same street - see osmaddresssystem/name_matching.py.
NAME_SIMILARITY_THRESHOLD = 82.0

# A street is "transversal" (numbered clockwise around the origin)
# rather than "radial" (numbered outward from it) when the distance
# difference between its two endpoints is less than this percentage of
# its own length - see report/street_identity.md "Street directionality".
TRANSVERSAL_ROAD_PCT = 10.0

TOWN_DIR = os.path.dirname(os.path.abspath(__file__))
EXAMPLES_DIR = os.path.dirname(TOWN_DIR)
COUNTRY_STREETS_FOLDER = os.path.join(EXAMPLES_DIR, "streets")
TOWN_STREETS_PATH = os.path.join(TOWN_DIR, "streets.osm")
NODES_GPKG_PATH = os.path.join(TOWN_DIR, "nodes.gpkg")
EDGES_GPKG_PATH = os.path.join(TOWN_DIR, "edges.gpkg")
STREETS_CSV_PATH = os.path.join(TOWN_DIR, "streets.csv")


def main():
    """Run the Madrid Nuevos Ministerios example end to end."""
    # --- Download and crop the street network --------------------------
    # Resolves LOCATION to an AOI (the finest OSM administrative/place
    # boundary containing it, or a buffer), downloads the best-matching
    # Geofabrik region, and crops it to that AOI.
    town_streets_path, aoi, name = osm.download_town_streets(
        LOCATION,
        town_streets_path=TOWN_STREETS_PATH,
        country_streets_folder=COUNTRY_STREETS_FOLDER,
        network_type=NETWORK_TYPE,
        buffer_m=BUFFER_M,
    )
    print(f"Resolved area: {name!r}")

    # --- Load the graph and assign street ids ---------------------------
    # osm.load_projected_graph always reduces to the graph's own largest
    # REAL (non-private, non-parking) connected component - see its own
    # docstring, and report/street_identity.md "Node clustering" for why
    # label_streets needs one connected network to begin with. Cropping
    # to the AOI boundary can cut off small, genuinely disconnected
    # islands (e.g. a service road whose only real connection to the
    # rest of the network falls just outside the crop), which is exactly
    # what this step handles.
    G = osm.load_projected_graph(town_streets_path, aoi)

    if DROP_OSM_NAMES:
        nx.set_edge_attributes(G, {e: None for e in G.edges(keys=True)}, "name")

    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(
        G,
        angle_threshold_deg=ANGLE_THRESHOLD_DEG,
        node_cluster_m=NODE_CLUSTER_M,
        name_similarity_threshold=NAME_SIMILARITY_THRESHOLD,
        parallel_dist_m=PARALLEL_DIST_M,
        parallel_pass_touching_dist_m=PARALLEL_PASS_TOUCHING_DIST_M,
        parallel_pass_non_touching_dist_m=PARALLEL_PASS_NON_TOUCHING_DIST_M,
        node_direction_tolerance_deg=NODE_DIRECTION_TOLERANCE_DEG,
    )

    # --- Give each road a directionality --------------------------------
    # Classify every street as radial (numbered outward from LOCATION) or
    # transversal (numbered clockwise around it), and record each edge's
    # position along its street - see report/street_identity.md "Street
    # directionality" for the full rules. LOCATION is in lat/lon; it needs
    # to be in the same projected CRS as G (which label_streets also
    # required) before distances/angles from it mean anything.
    origin_point = (
        gpd.GeoSeries([Point(LOCATION[1], LOCATION[0])], crs=4326)
        .to_crs(aoi.estimate_utm_crs())
        .iloc[0]
    )
    origin = (origin_point.x, origin_point.y)

    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf,
        edges_gdf,
        streets_df,
        origin=origin,
        transversal_road_pct=TRANSVERSAL_ROAD_PCT,
    )

    # --- Save outputs -----------------------------------------------------
    pipeline.write_gpkg(nodes_gdf.reset_index(), NODES_GPKG_PATH)
    pipeline.write_gpkg(edges_gdf.reset_index(), EDGES_GPKG_PATH)
    # streets.csv itself only carries the columns an address scheme
    # actually needs; osmids stays on streets_df (see
    # report/street_identity.md "Outputs") but isn't written to the CSV.
    streets_csv_cols = ["street_id", "street_name", "street_rank", "highway", "road_type", "length_m"]
    streets_df[streets_csv_cols].to_csv(STREETS_CSV_PATH, index=False)
    print(f"Wrote {NODES_GPKG_PATH}, {EDGES_GPKG_PATH}, {STREETS_CSV_PATH}")


if __name__ == "__main__":
    main()
