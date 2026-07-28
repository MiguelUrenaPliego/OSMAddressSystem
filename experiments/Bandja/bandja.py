"""Bandja, Cameroon: full addressed-streets pipeline, per subregion.

Bandja has no single obvious center point at the whole-town level, but
each of its own subregions does (a village center, a market square). So
this script uses radial numbering, with the origin picked per subregion
as that subregion's own centroid, rather than one shared origin for the
whole town.

For each subregion of Bandja (see subregions.find_subregions): downloads
and crops the street network once for the whole town, clips it to the
subregion, runs the full street_ids -> street_direction -> numbering
pipeline (pipeline.build_addressed_streets), and writes
nodes.gpkg/edges.gpkg/streets.gpkg/streets.csv plus an interactive
street_editor.html AND an interactive geocode_map.html (click anywhere
to reverse-geocode that point - see geocode_map.build_interactive_geocode_map),
both inside that subregion's own folder. Also applies any edited
streets.csv found in a subregion's folder (a no-op the first time,
since apply_street_edits is idempotent when nothing changed).
"""

from __future__ import annotations

import os

import geopandas as gpd
import networkx as nx
import pandas as pd

from osmaddresssystem import (
    geocode_map,
    osm,
    pipeline,
    street_editor,
    subregions,
    utils,
)

# --- Hyperparameters --------------------------------------------------

LOCATION = "Bandja, Cameroon"
NETWORK_TYPE = "all"
BUFFER_M = None
NUMBERING_MODE = "radial"

TOWN_DIR = os.path.dirname(os.path.abspath(__file__))
EXPERIMENTS_DIR = os.path.dirname(TOWN_DIR)
EXAMPLES_STREETS_DIR = os.path.join(os.path.dirname(EXPERIMENTS_DIR), "examples", "streets")
TOWN_STREETS_PATH = os.path.join(TOWN_DIR, "streets.osm")


def process_subregion(
    subregion_name: str,
    subregion_geom,
    G_town: nx.MultiDiGraph,
    aoi,
    country_province_polygons: dict,
) -> None:
    """Generate (or refresh) the addressed streets for one subregion."""
    folder = os.path.join(TOWN_DIR, utils.sanitize_filename(subregion_name))
    os.makedirs(folder, exist_ok=True)

    nodes_path = os.path.join(folder, "nodes.gpkg")
    edges_path = os.path.join(folder, "edges.gpkg")
    streets_gpkg_path = os.path.join(folder, "streets.gpkg")
    streets_csv_path = os.path.join(folder, "streets.csv")
    edge_edits_csv_path = os.path.join(folder, "edge_edits.csv")
    editor_html_path = os.path.join(folder, "streets_editor.html")
    geocode_map_html_path = os.path.join(folder, "geocode_map.html")

    already_generated = all(
        os.path.exists(p) for p in (nodes_path, edges_path, streets_gpkg_path, streets_csv_path)
    )

    if not already_generated:
        Gs = pipeline.clip_graph_to_polygon(G_town, subregion_geom)
        if Gs is None:
            print(f"Skipping {subregion_name!r}: no usable street network inside it")
            return

        origin_point = (
            gpd.GeoSeries([subregion_geom], crs=4326)
            .to_crs(aoi.estimate_utm_crs())
            .iloc[0]
            .centroid
        )
        origin = (origin_point.x, origin_point.y)

        admin_polygons = {
            **country_province_polygons,
            "town": aoi.union_all(),
            "neighbourhood": subregion_geom,
        }
        nodes_gdf, edges_gdf, streets_gdf = pipeline.build_addressed_streets(
            Gs,
            origin=origin,
            numbering_mode=NUMBERING_MODE,
            admin_polygons=admin_polygons,
        )

        nodes_out = pipeline.write_gpkg(nodes_gdf.reset_index(), nodes_path)
        edges_out = pipeline.write_gpkg(edges_gdf.reset_index(), edges_path)
        pipeline.write_gpkg(streets_gdf, streets_gpkg_path)
        streets_gdf.drop(columns="geometry").to_csv(streets_csv_path, index=False)

        print(f"Wrote {nodes_path}, {edges_path}, {streets_gpkg_path}, {streets_csv_path}")

        street_editor.build_street_editor_map(streets_gdf, edges_gdf, editor_html_path)
        geocode_map.build_interactive_geocode_map(
            streets_gdf, geocode_map_html_path, numbering_mode=NUMBERING_MODE, origin=origin,
        )
    else:
        print(f"{subregion_name!r} already generated, applying any pending edits")
        nodes_gdf = gpd.read_file(nodes_path).set_index("osmid")
        edges_gdf = gpd.read_file(edges_path).set_index(["u", "v", "key"])
        streets_df = pd.read_csv(streets_csv_path)

        origin_point = (
            gpd.GeoSeries([subregion_geom], crs=4326)
            .to_crs(aoi.estimate_utm_crs())
            .iloc[0]
            .centroid
        )
        origin = (origin_point.x, origin_point.y)

        edge_edits_path = edge_edits_csv_path if os.path.exists(edge_edits_csv_path) else None
        new_nodes, new_edges, new_streets_df = street_editor.apply_street_edits(
            nodes_gdf, edges_gdf, streets_df, streets_csv_path,
            edge_edits_csv_path=edge_edits_path, origin=origin,
            numbering_mode=NUMBERING_MODE,
        )
        new_edges = pipeline.write_gpkg(new_edges, edges_path)
        streets_gdf = pipeline.streets_gdf_from_edges(new_edges, new_streets_df)
        pipeline.write_gpkg(streets_gdf, streets_gpkg_path)
        streets_gdf.drop(columns="geometry").to_csv(streets_csv_path, index=False)
        street_editor.build_street_editor_map(streets_gdf, new_edges, editor_html_path)
        geocode_map.build_interactive_geocode_map(
            streets_gdf, geocode_map_html_path, numbering_mode=NUMBERING_MODE, origin=origin,
        )


def main():
    town_streets_path, aoi, name = osm.download_town_streets(
        LOCATION,
        town_streets_path=TOWN_STREETS_PATH,
        country_streets_folder=EXAMPLES_STREETS_DIR,
        network_type=NETWORK_TYPE,
        buffer_m=BUFFER_M,
    )
    print(f"Resolved area: {name!r}")

    G_town = osm.load_projected_graph(town_streets_path, aoi)

    regions = subregions.find_subregions(aoi)
    print(f"Found {len(regions)} subregion(s) for {name!r}")

    country_province_polygons = pipeline.country_and_province_polygons(aoi)

    for _, row in regions.iterrows():
        process_subregion(row["name"], row.geometry, G_town, aoi, country_province_polygons)


if __name__ == "__main__":
    main()
