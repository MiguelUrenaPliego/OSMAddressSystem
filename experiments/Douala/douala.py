"""Douala, Cameroon (Bonamoussadi district): addressed-streets pipeline,
per subregion.

Processing the whole "Communauté urbaine de Douala" (all six arrondissements)
takes far too long for an example run, so this script scopes itself to
just the district containing Bonamoussadi instead - resolved the normal
way (utils.get_town_aoi, the finest OSM boundary containing the point),
same as every other single-town example/experiment in this repo.

Douala's core is substantially grid-laid-out, so this script uses grid
numbering rather than radial: numbers climb along a fixed compass
direction and its perpendicular, not outward from any one center point.
The dominant direction is autodetected per subregion via
street_direction.detect_main_direction, rather than assumed to be true
north.

For each subregion of the district (see subregions.find_subregions):
downloads and crops the street network once for the whole district,
clips it to the subregion, runs the full street_ids -> street_direction
-> numbering pipeline (pipeline.build_addressed_streets), and writes
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
    street_direction,
    street_editor,
    street_signs,
    subregions,
    utils,
)

# --- Hyperparameters --------------------------------------------------

LOCATION = "Bonamoussadi, Douala, Cameroon"
NETWORK_TYPE = "all"
BUFFER_M = None
NUMBERING_MODE = "grid"

TOWN_DIR = os.path.dirname(os.path.abspath(__file__))
EXPERIMENTS_DIR = os.path.dirname(TOWN_DIR)
EXAMPLES_STREETS_DIR = os.path.join(os.path.dirname(EXPERIMENTS_DIR), "examples", "streets")
TOWN_STREETS_PATH = os.path.join(TOWN_DIR, "streets.osm")


def process_subregion(
    subregion_name: str,
    subregion_geom,
    G_town: nx.MultiDiGraph,
    aoi,
    origin,
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
    street_signs_html_path = os.path.join(folder, "street_signs_map.html")

    already_generated = all(
        os.path.exists(p) for p in (nodes_path, edges_path, streets_gpkg_path, streets_csv_path)
    )

    if not already_generated:
        Gs = pipeline.clip_graph_to_polygon(G_town, subregion_geom)
        if Gs is None:
            print(f"Skipping {subregion_name!r}: no usable street network inside it")
            return

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

        pipeline.write_gpkg(nodes_gdf.reset_index(), nodes_path)
        pipeline.write_gpkg(edges_gdf.reset_index(), edges_path)
        pipeline.write_gpkg(streets_gdf, streets_gpkg_path)
        streets_gdf.drop(columns="geometry").to_csv(streets_csv_path, index=False)

        print(f"Wrote {nodes_path}, {edges_path}, {streets_gpkg_path}, {streets_csv_path}")

        street_editor.build_street_editor_map(streets_gdf, edges_gdf, editor_html_path)
        # detect_main_direction is a pure function of the labeled edges'
        # own bearings, called on this same edges_gdf (via
        # build_addressed_streets) to pick NUMBERING_MODE="grid"'s own
        # direction_deg above - recomputing it here is deterministic and
        # gives the identical value already baked into the numbering,
        # since build_addressed_streets never returns it directly.
        direction_deg = street_direction.detect_main_direction(edges_gdf)
        geocode_map.build_interactive_geocode_map(
            streets_gdf, geocode_map_html_path,
            numbering_mode=NUMBERING_MODE, direction_deg=direction_deg,
        )
        intersections_gdf = street_signs.compute_intersections(nodes_gdf, edges_gdf, streets_gdf)
        street_signs.build_street_signs_map(
            streets_gdf, intersections_gdf, street_signs_html_path, edges_gdf=edges_gdf,
        )
    else:
        print(f"{subregion_name!r} already generated, applying any pending edits")
        nodes_gdf = gpd.read_file(nodes_path).set_index("osmid")
        edges_gdf = gpd.read_file(edges_path).set_index(["u", "v", "key"])
        streets_df = pd.read_csv(streets_csv_path)

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
        direction_deg = street_direction.detect_main_direction(new_edges)
        geocode_map.build_interactive_geocode_map(
            streets_gdf, geocode_map_html_path,
            numbering_mode=NUMBERING_MODE, direction_deg=direction_deg,
        )
        intersections_gdf = street_signs.compute_intersections(nodes_gdf, new_edges, streets_gdf)
        street_signs.build_street_signs_map(
            streets_gdf, intersections_gdf, street_signs_html_path, edges_gdf=new_edges,
        )


def main():
    # get_town_aoi (used internally by download_town_streets) resolves
    # to the FINEST OSM boundary containing the point - for a point in
    # Bonamoussadi that's its own district (e.g. "Douala 5ème" or
    # whichever arrondissement/quarter OSM has mapped there), not the
    # whole "Communauté urbaine de Douala". That's exactly the scope
    # this script wants: processing all of Douala's six arrondissements
    # takes far too long for an example run, so this stays scoped to
    # Bonamoussadi's own district, same as every other single-town
    # example/experiment in this repo.
    town_streets_path, aoi, name = osm.download_town_streets(
        LOCATION,
        town_streets_path=TOWN_STREETS_PATH,
        country_streets_folder=EXAMPLES_STREETS_DIR,
        network_type=NETWORK_TYPE,
        buffer_m=BUFFER_M,
    )
    print(f"Resolved area: {name!r}")

    G_town = osm.load_projected_graph(town_streets_path, aoi)

    origin_point = aoi.to_crs(aoi.estimate_utm_crs()).union_all().centroid
    origin = (origin_point.x, origin_point.y)

    regions = subregions.find_subregions(aoi)
    print(f"Found {len(regions)} subregion(s) for {name!r}")

    country_province_polygons = pipeline.country_and_province_polygons(aoi)

    for _, row in regions.iterrows():
        process_subregion(
            row["name"], row.geometry, G_town, aoi, origin, country_province_polygons
        )


if __name__ == "__main__":
    main()
