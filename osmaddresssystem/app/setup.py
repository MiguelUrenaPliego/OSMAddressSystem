"""Build the labelled street network of a project from its input folder.

``input/streets.osm`` (the OpenStreetMap streets) is loaded, clipped to the polygon(s) of ``input/area.gpkg`` when there is
one, and labelled with ``street_ids.label_streets`` (street identities, main paths); the result is written to
``network/nodes.gpkg``, ``network/edges.gpkg`` and ``network/streets.csv``. When the network files already exist
(for example a network the community edits were made on) nothing is rebuilt unless ``force`` is given: the edit files
refer to the street ids of that network.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd

from .. import osm, pipeline, street_ids


def build_network(root: Path, force: bool = False) -> bool:
    """Create ``network/`` from ``input/streets.osm`` (and ``input/area.gpkg``).

    Args:
        root: Project folder.
        force: Rebuild even if the network files exist.

    Returns:
        True when the network was built, False when it already existed.

    Raises:
        FileNotFoundError: If ``input/streets.osm`` is missing.
    """
    root = Path(root)
    net = root / "network"
    if not force and all((net / f).exists() for f in ("nodes.gpkg", "edges.gpkg", "streets.csv")):
        return False
    osm_path = root / "input" / "streets.osm"
    if not osm_path.exists():
        raise FileNotFoundError(f"{osm_path} is missing")
    area_path = root / "input" / "area.gpkg"
    area = gpd.read_file(area_path).to_crs(4326) if area_path.exists() else None
    import osmnx as ox

    G = ox.graph_from_xml(str(osm_path))
    crs_source = area if area is not None else gpd.GeoDataFrame(
        geometry=[gpd.GeoSeries(gpd.points_from_xy([d["x"] for _, d in G.nodes(data=True)], [d["y"] for _, d in G.nodes(data=True)]),
                                crs=4326).union_all().convex_hull], crs=4326)
    G = ox.project_graph(G, to_crs=crs_source.estimate_utm_crs())
    G = osm.largest_real_component(G)
    if area is not None:
        G = pipeline.clip_graph_to_polygon(G, area.union_all())
        if G is None:
            raise ValueError("no streets inside the area polygon")
    nodes, edges, streets = street_ids.label_streets(G)
    net.mkdir(parents=True, exist_ok=True)
    pipeline.write_gpkg(nodes.reset_index(), str(net / "nodes.gpkg"))
    pipeline.write_gpkg(edges.reset_index(), str(net / "edges.gpkg"))
    streets.to_csv(net / "streets.csv", index=False)
    return True
