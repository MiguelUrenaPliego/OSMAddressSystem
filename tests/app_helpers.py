"""Builds a small project folder (synthetic grid network, houses, points, area) for the app tests."""

import base64
import json
import os
import shutil
from pathlib import Path

import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import LineString, Point, box

from osmaddresssystem import pipeline, street_ids
from osmaddresssystem.app.store import Store
from osmaddresssystem.synthetic_grid import build_idealized_grid

CRS = "EPSG:32632"
X0, Y0 = 635000.0, 577000.0


def launch_browser(playwright):
    """Use an installed browser on Windows/Linux without downloading one."""
    executable = os.environ.get('ADDRESS_TEST_BROWSER') or shutil.which('chromium') or shutil.which('chromium-browser')
    if not executable:
        executable = next((str(p) for p in [
            Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),
            Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
        ] if p.exists()), None)
    if not executable and not Path(playwright.chromium.executable_path).exists():
        import pytest
        pytest.skip('No installed browser; set ADDRESS_TEST_BROWSER to reuse one without downloading')
    args = ['--no-sandbox'] if hasattr(os, 'geteuid') and os.geteuid() == 0 else []
    browser = playwright.chromium.launch(**({'executable_path': executable} if executable else {}), headless=True, args=args)
    def offline_tiles(context):
        # UI workflows are local integration tests, not tests of public tile
        # servers. Avoid downloads and navigation stalls on limited connections.
        context.route('https://mt1.google.com/**', lambda route: route.abort())
        context.route('https://tile.openstreetmap.org/**', lambda route: route.abort())
    browser.on('context', offline_tiles)
    return browser


def make_project(root: Path, rows: int = 4, cols: int = 5, spacing: float = 120.0) -> Path:
    """Create a project folder around a synthetic street grid.

    The grid has a named horizontal street (``Elm Street``) and a primary one; houses stand 12 m beside the streets and four
    houses stand far from any street.

    Args:
        root: Project folder to create.
        rows: Grid rows.
        cols: Grid columns.
        spacing: Metres between intersections.

    Returns:
        ``root``.
    """
    G = build_idealized_grid(rows=rows, cols=cols, spacing=spacing, named_row=1, named_row_name="Elm Street", primary_row=2, crs=CRS)
    # shift the grid to real coordinates so lat/lon conversions work
    import networkx as nx

    H = nx.MultiDiGraph(crs=CRS)
    mapping = {}
    for n, d in G.nodes(data=True):
        H.add_node(n, x=d["x"] + X0, y=d["y"] + Y0, **{k: v for k, v in d.items() if k not in ("x", "y")})
    for u, v, k, d in G.edges(keys=True, data=True):
        d = dict(d)
        if "geometry" in d:
            d["geometry"] = LineString([(x + X0, y + Y0) for x, y in d["geometry"].coords])
        H.add_edge(u, v, k, **d)
    nodes, edges, streets = street_ids.label_streets(H, node_cluster_m=0)
    (root / "network").mkdir(parents=True, exist_ok=True)
    (root / "input").mkdir(exist_ok=True)
    pipeline.write_gpkg(nodes.reset_index(), str(root / "network" / "nodes.gpkg"))
    pipeline.write_gpkg(edges.reset_index(), str(root / "network" / "edges.gpkg"))
    streets.to_csv(root / "network" / "streets.csv", index=False)

    pts, ids = [], []
    for i in range(cols - 1):                                 # houses beside the first row
        for j in range(8):
            pts.append(Point(X0 + 15 + i * spacing + j * 12, Y0 + 12))
            ids.append(f"H{len(ids)}")
    for k in range(3):                                          # three houses in one number slot: they get letters
        pts.append(Point(X0 + 70.37 + 0.1 * k, Y0 + 12))
        ids.append(f"C{k}")
    far = [Point(X0 + 150, Y0 - 300 - 20 * k) for k in range(4)]
    pts += far
    ids += [f"FAR{k}" for k in range(4)]
    gpd.GeoDataFrame({"building_id": ids}, geometry=pts, crs=CRS).to_crs(4326).to_file(root / "input" / "buildings.gpkg")
    gpd.GeoDataFrame({"name": ["Market"], "kind": ["square"]}, geometry=[Point(X0 + 100, Y0 + 100)], crs=CRS).to_crs(4326).to_file(root / "input" / "points.gpkg")
    gpd.GeoDataFrame({"kind": ["area"]}, geometry=[box(X0 - 50, Y0 - 400, X0 + cols * spacing, Y0 + rows * spacing)], crs=CRS).to_crs(4326).to_file(root / "input" / "area.gpkg")
    write_osm(root, nodes, edges)
    Store(root, "Test grid")
    return root


def write_osm(root: Path, nodes, edges) -> None:
    """Write ``input/streets.osm`` for the synthetic grid: one way per OSM id, chained through its edges (as an OSM download would be).

    Args:
        root: Project folder.
        nodes: Labelled nodes (projected, indexed by node id).
        edges: Labelled edges (indexed by ``(u, v, key)``).
    """
    import re

    from osmaddresssystem.app.exports import _paths

    lines = ["<?xml version='1.0' encoding='UTF-8'?>", "<osm version='0.6'>"]
    tr = Transformer.from_crs(CRS, 4326, always_xy=True)
    for n, r in nodes.iterrows():
        lo, la = tr.transform(r["x"], r["y"])
        lines.append(f"  <node id='{int(n)}' version='1' lat='{la:.7f}' lon='{lo:.7f}'/>")
    by_way: dict = {}
    seen = set()
    for (u, v, k), r in edges.iterrows():
        if (v, u, k) in seen:
            continue
        seen.add((u, v, k))
        by_way.setdefault(re.findall(r"\d+", str(r["osmid"]))[0], []).append((int(u), int(v), list(r.geometry.coords)))
    for wid, items in by_way.items():
        for i, path in enumerate(_paths(items)):
            refs = [path[0][0]] + [b for _a, b, _c in path]
            nm = edges[edges["osmid"].astype(str).str.contains(wid)]["name"].iloc[0]
            hw = edges[edges["osmid"].astype(str).str.contains(wid)]["highway"].iloc[0]
            tags = f"<tag k='highway' v='{hw}'/>" + (f"<tag k='name' v='{nm}'/>" if isinstance(nm, str) and nm else "")
            lines.append(f"  <way id='{wid if i == 0 else int(wid) * 1000 + i}' version='3'>" + "".join(f"<nd ref='{r}'/>" for r in refs) + tags + "</way>")
    lines.append("</osm>")
    (root / "input" / "streets.osm").write_text("\n".join(lines))


def lonlat(x: float, y: float) -> tuple[float, float]:
    """Convert projected test coordinates to lon, lat.

    Args:
        x: Easting.
        y: Northing.

    Returns:
        ``(lon, lat)``.
    """
    return Transformer.from_crs(CRS, 4326, always_xy=True).transform(x, y)


def kml_b64(lines: list[list[tuple[float, float]]], names: list[str] | None = None) -> str:
    """A KML file with the given lines (projected coordinates), base64 encoded as the app receives it.

    Args:
        lines: Lines as lists of projected ``(x, y)``.
        names: Optional placemark names.

    Returns:
        Base64 text of the KML.
    """
    body = ""
    for i, ln in enumerate(lines):
        coords = " ".join(f"{lonlat(x, y)[0]},{lonlat(x, y)[1]},0" for x, y in ln)
        nm = f"<name>{names[i]}</name>" if names else ""
        body += f"<Placemark>{nm}<LineString><coordinates>{coords}</coordinates></LineString></Placemark>"
    xml = f'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>{body}</Document></kml>'
    return base64.b64encode(xml.encode()).decode()
