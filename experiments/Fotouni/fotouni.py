"""Fotouni addresses: path numbering from Carrefour Brouwer, with branch-road names.

Starts from the street graph of the Bandja run (``Bandja/bandja``: the one the community's
street names were assigned on, so the street ids in the edit files still match), applies the
community street/edge edits of 2026-09-15, recomputes the numbering in **path mode** (shortest road-network
distance to **Carrefour Brouwer (hôtel)**, see ``osmaddresssystem/path_numbering.py``; streets are cut so that
each begins at its end nearest to the reference, ``street_id_original`` keeps the id before the cut) and names every unnamed road that branches off a named road
(``3-xyz`` ... ``4-3-xyz``; see ``osmaddresssystem/branches.py``).

Name rules: a local (community) name always overrides the OSM name, a blank local name never
removes one, and where the two edit files disagree the most recent file wins (``resolve_edits``).

Inputs (all in ``inputs/``): ``nodes.gpkg``, ``edges.gpkg``, ``streets.csv`` (Bandja run) and the
two edit files. Outputs in ``fotouni/``: ``nodes.gpkg``, ``edges.gpkg``, ``streets.gpkg``,
``streets.csv``, ``street_id_mapping.csv`` (original id -> new id of every street), ``branch_names.csv``,
``branch_names_map.html`` and ``geocode_map.html``.

Run with the project's venv::

    uv run experiments/Fotouni/fotouni.py
"""

from __future__ import annotations

import html
import os
import re

import folium
import geopandas as gpd
import pandas as pd
from pyproj import Transformer

from osmaddresssystem import geocode_map, pipeline, street_editor

HERE = os.path.dirname(os.path.abspath(__file__))
IN = os.path.join(HERE, "inputs")
OUT = os.path.join(HERE, "fotouni")

#: Numbering centre: Carrefour Brouwer (hôtel), Tchisso, Fotouni (WGS84 lat, lon).
ORIGIN_LATLON = (5.3201118978688635, 10.227526846825613)
NUMBERING_MODE = "path"
PATH_HIGH_RANK_MAX = 2
MAX_BRANCH_LEVELS = 2
BRANCH_NUMBER_DIVISOR_M = 20.0


def _file_order(path: str) -> tuple[str, int]:
    """Recency key of an edit file.

    The date in the file name decides; on a tie the street-level file counts as the more
    recent one (on the original disk ``street_edits`` was saved after ``edge_edits``).
    Modification times are not used because a git clone does not preserve them.

    Args:
        path: Edit file such as ``street_edits_20260915.csv``.

    Returns:
        ``(YYYYMMDD or "", 1 for a street-level file else 0)``; larger means more recent.
    """
    m = re.search(r"(\d{8})", os.path.basename(path))
    return (m.group(1) if m else "", int("street" in os.path.basename(path)))


def _given(name) -> bool:
    """Whether a local street name is given (not blank and not just a number).

    Args:
        name: Cell value.

    Returns:
        True for a real name.
    """
    return isinstance(name, str) and bool(name.strip()) and not name.strip().isdigit()


def resolve_edits(street_edits: str, edge_edits: str, streets_df: pd.DataFrame, edges: gpd.GeoDataFrame, out_dir: str) -> tuple[str, str]:
    """Write the edit files with their names resolved.

    Rules: a local name always overrides the OSM name; a blank local name never removes
    one (the street keeps its existing name); when the two files disagree on a street id
    the most recent file wins.

    Args:
        street_edits: Street-level edit csv.
        edge_edits: Edge-level edit csv.
        streets_df: Street table of the run the edits refer to (names before the edits).
        edges: Edge table of that run, indexed by ``(u, v, key)``, with ``street_name``.
        out_dir: Folder for the resolved csvs.

    Returns:
        Paths of the resolved ``(street_edits, edge_edits)`` csvs.
    """
    se = pd.read_csv(street_edits, dtype=str)
    ee = pd.read_csv(edge_edits, dtype=str)
    local: dict[str, str] = {}
    for path, df in sorted(((street_edits, se), (edge_edits, ee)), key=lambda x: _file_order(x[0])):
        for sid, name in zip(df["new_street_id"], df["street_name"]):
            if _given(name) and isinstance(sid, str):
                local[sid.strip()] = name.strip()
    existing = streets_df.assign(street_id=streets_df["street_id"].astype(str)).set_index("street_id")["street_name"]
    se["street_name"] = [local.get(str(n).strip(), existing.get(str(o).strip())) for o, n in zip(se["old_street_id"], se["new_street_id"])]
    cur = edges["street_name"]
    ee["street_name"] = [local.get(str(n).strip(), cur.get((int(u), int(v), int(k)))) for u, v, k, n in zip(ee["u"], ee["v"], ee["key"], ee["new_street_id"])]
    os.makedirs(out_dir, exist_ok=True)
    p1, p2 = os.path.join(out_dir, "street_edits_resolved.csv"), os.path.join(out_dir, "edge_edits_resolved.csv")
    se.to_csv(p1, index=False)
    ee.to_csv(p2, index=False)
    return p1, p2


def cut_and_start_points(edges_gdf: gpd.GeoDataFrame, nodes_gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Where path numbering cut the streets, and where every street id starts.

    Args:
        edges_gdf: Edges with ``street_id``, ``street_id_original``, ``street_pos_u_m``/``_v_m``.
        nodes_gdf: Nodes indexed by node id with a point ``geometry``.

    Returns:
        ``(cuts, starts)``: points (WGS84) at the nodes shared by two parts of one original street,
        and points at the start node (position 0) of every street id.
    """
    flat = edges_gdf.reset_index()
    flat = flat[flat["street_id"].notna()]
    ends = pd.concat([flat[["u", "street_id", "street_id_original"]].rename(columns={"u": "node"}),
                      flat[["v", "street_id", "street_id_original"]].rename(columns={"v": "node"})])
    parts = ends.groupby(["node", "street_id_original"])["street_id"].nunique()
    cut_nodes = parts[parts > 1].index.get_level_values("node").unique()
    starts = pd.concat([flat[flat["street_pos_u_m"] == 0][["u", "street_id"]].rename(columns={"u": "node"}),
                        flat[flat["street_pos_v_m"] == 0][["v", "street_id"]].rename(columns={"v": "node"})]).drop_duplicates()
    geo = nodes_gdf.geometry
    cuts = gpd.GeoDataFrame({"node": list(cut_nodes)}, geometry=[geo[n] for n in cut_nodes], crs=nodes_gdf.crs).to_crs(4326)
    st = gpd.GeoDataFrame(starts.reset_index(drop=True), geometry=[geo[n] for n in starts["node"]], crs=nodes_gdf.crs).to_crs(4326)
    return cuts, st


def build_map(streets_gdf: gpd.GeoDataFrame, edges_gdf: gpd.GeoDataFrame, origin_latlon, path: str,
              cuts: gpd.GeoDataFrame | None = None, starts: gpd.GeoDataFrame | None = None) -> None:
    """Write a map of the streets labelled with their display name.

    Args:
        streets_gdf: Streets with ``street_id``, ``display_name`` and ``branch_name``.
        edges_gdf: Edges (unused geometry source kept for symmetry with the other maps).
        origin_latlon: ``(lat, lon)`` of the numbering centre, drawn as a marker.
        path: Output HTML path.
        cuts: Cut points from :func:`cut_and_start_points` (red circles), optional.
        starts: Street start points (blue dots: house number 1/2), optional.
    """
    import colorsys

    s = streets_gdf.to_crs(4326)
    m = folium.Map(location=list(origin_latlon), zoom_start=15, max_zoom=21, tiles=None)
    folium.TileLayer("OpenStreetMap", max_zoom=21, max_native_zoom=19).add_to(m)
    folium.TileLayer("https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", attr="Google", name="Satellite", max_zoom=21).add_to(m)
    fg = folium.FeatureGroup(name="Streets (colour = street id)")
    lab = folium.FeatureGroup(name="Names (own name, or derived branch name in blue)")
    for r in s.itertuples():
        h = (int(r.street_id) * 0.61803398875) % 1.0
        col = "#%02x%02x%02x" % tuple(int(255 * c) for c in colorsys.hsv_to_rgb(h, 0.7, 0.8))
        own = isinstance(r.street_name, str) and r.street_name.strip() and not r.street_name.strip().isdigit()
        name = r.display_name if isinstance(r.display_name, str) else None
        tip = f"{name or '(unnamed)'} | street {int(r.street_id)}" + (f" | branch of street {int(r.branch_parent_id)}" if pd.notna(r.branch_parent_id) else "")
        folium.GeoJson(r.geometry.__geo_interface__, style_function=lambda _, c=col: {"color": c, "weight": 4}, tooltip=tip).add_to(fg)
        if name:
            p = r.geometry.interpolate(0.5, normalized=True)
            color = "#111" if own else "#0d47a1"
            folium.Marker([p.y, p.x], icon=folium.DivIcon(
                html=f'<div style="font:600 11px sans-serif;white-space:nowrap;transform:translate(-50%,-50%);color:{color};'
                     f'text-shadow:-1px -1px 0 #fff,1px -1px 0 #fff,-1px 1px 0 #fff,1px 1px 0 #fff">{html.escape(name)}</div>',
                icon_size=(0, 0))).add_to(lab)
    fg.add_to(m)
    lab.add_to(m)
    own_name = streets_gdf["street_name"].map(lambda n: isinstance(n, str) and bool(n.strip()) and not n.strip().isdigit())
    n_parts = streets_gdf.groupby("street_id_original")["street_id"].transform("size")
    split_named = s[(n_parts > 1).to_numpy() & own_name.to_numpy()]
    hl = folium.FeatureGroup(name="Named streets that were split (black, one tooltip per part)")
    for r in split_named.itertuples():
        folium.GeoJson(r.geometry.__geo_interface__, style_function=lambda _: {"color": "#000", "weight": 9, "opacity": 0.45},
                       tooltip=f"{r.street_name} | street {int(r.street_id)} = part {int(r.street_part) + 1} of original {int(r.street_id_original)}").add_to(hl)
    hl.add_to(m)
    if cuts is not None:
        fc = folium.FeatureGroup(name=f"Cut points ({len(cuts)}): a street starts here on both sides")
        for r in cuts.itertuples():
            folium.CircleMarker([r.geometry.y, r.geometry.x], radius=6, color="#d50000", weight=3, fill=False, tooltip="street cut").add_to(fc)
        fc.add_to(m)
    if starts is not None:
        fs = folium.FeatureGroup(name=f"Street starts ({len(starts)}): house 1 (left) / 2 (right)", show=False)
        for r in starts.itertuples():
            folium.CircleMarker([r.geometry.y, r.geometry.x], radius=3, color="#0d47a1", fill=True, fill_opacity=1,
                                tooltip=f"start of street {int(r.street_id)}").add_to(fs)
        fs.add_to(m)
    folium.Marker(list(origin_latlon), tooltip="Numbering centre: Carrefour Brouwer (hôtel)",
                  icon=folium.Icon(color="red", icon="star")).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(path)


def main() -> None:
    """Apply the community edits, renumber around Carrefour Brouwer and name the branch roads."""
    os.makedirs(OUT, exist_ok=True)
    nodes = gpd.read_file(os.path.join(IN, "nodes.gpkg")).set_index("osmid")
    edges = gpd.read_file(os.path.join(IN, "edges.gpkg")).set_index(["u", "v", "key"])
    streets_df = pd.read_csv(os.path.join(IN, "streets.csv"))
    x, y = Transformer.from_crs(4326, nodes.crs, always_xy=True).transform(ORIGIN_LATLON[1], ORIGIN_LATLON[0])

    street_edits, edge_edits = resolve_edits(
        os.path.join(IN, "street_edits_20260915.csv"), os.path.join(IN, "edge_edits_20260915.csv"),
        streets_df, edges, os.path.join(OUT, "resolved_edits"))
    nodes, edges, streets_df = street_editor.apply_street_edits(
        nodes, edges, streets_df, street_edits,
        edge_edits_csv_path=edge_edits,
        origin=(x, y), numbering_mode=NUMBERING_MODE, path_high_rank_max=PATH_HIGH_RANK_MAX,
        name_branches=True, max_branch_levels=MAX_BRANCH_LEVELS, branch_number_divisor_m=BRANCH_NUMBER_DIVISOR_M,
    )
    edges = pipeline.write_gpkg(edges, os.path.join(OUT, "edges.gpkg"))
    pipeline.write_gpkg(nodes.reset_index() if "osmid" not in nodes.columns else nodes, os.path.join(OUT, "nodes.gpkg"))
    streets_gdf = pipeline.streets_gdf_from_edges(edges, streets_df)
    pipeline.write_gpkg(streets_gdf, os.path.join(OUT, "streets.gpkg"))
    streets_gdf.drop(columns="geometry").to_csv(os.path.join(OUT, "streets.csv"), index=False)

    streets_gdf[["street_id", "street_id_original", "street_part", "street_name"]].sort_values(
        ["street_id_original", "street_part"]).to_csv(os.path.join(OUT, "street_id_mapping.csv"), index=False)
    geocode_map.build_interactive_geocode_map(
        streets_gdf, os.path.join(OUT, "geocode_map.html"), numbering_mode=NUMBERING_MODE, origin=(x, y))
    n_split = int((streets_gdf["street_part"] > 0).sum())
    print(f"path numbering: {n_split} new street ids from cutting streets "
          f"({streets_gdf['street_id_original'].nunique()} original ids -> {len(streets_gdf)} ids)")
    b = streets_gdf[streets_gdf["branch_name"].notna()]
    b[["street_id", "street_name", "branch_name", "branch_parent_id", "branch_number", "branch_label", "branch_side", "branch_level",
       "branch_distance_m", "street_rank", "highway", "length_m"]].to_csv(os.path.join(OUT, "branch_names.csv"), index=False)
    cuts, starts = cut_and_start_points(edges, nodes if "geometry" in nodes else nodes.reset_index())
    build_map(streets_gdf, edges, ORIGIN_LATLON, os.path.join(OUT, "branch_names_map.html"), cuts=cuts, starts=starts)
    own = streets_gdf["street_name"].map(lambda n: isinstance(n, str) and bool(n.strip()) and not n.strip().isdigit())
    print(f"{len(streets_gdf)} streets: {int(own.sum())} with their own name, {len(b)} named by branch "
          f"(level 1: {int((b.branch_level == 1).sum())}, level 2: {int((b.branch_level == 2).sum())})")


if __name__ == "__main__":
    main()
