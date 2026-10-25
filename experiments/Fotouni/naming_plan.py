"""Which Fotouni streets must be named by hand so that every street has a name or a derived name?

Reads the outputs of ``fotouni.py`` (``fotouni/edges.gpkg``, ``nodes.gpkg``, ``streets.csv``) and runs
``osmaddresssystem.naming_plan.plan_minimal_naming`` with two derivation levels (``3-xyz``, ``4-3-xyz``). The plan is
then verified end to end: the chosen streets get a placeholder name and ``branches.assign_branch_names`` must name every
target. A second plan is made for the streets that touch the Fotouni area when
``data/fotouni/town_survey/streets_places/fotouni_area_candidates.gpkg`` is present (optional).

Outputs in ``fotouni/``: ``naming_plan.csv`` (the streets to name, in priority order, with position),
``naming_coverage.csv`` (every target: from which street it is named and at which level),
``naming_plan_area.csv`` (same, targets limited to the Fotouni area) and ``naming_plan_map.html``.

Run::

    uv run --extra planning experiments/Fotouni/naming_plan.py
"""

from __future__ import annotations

import html
import os

import folium
import geopandas as gpd
import pandas as pd

from osmaddresssystem import branches, naming_plan, pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fotouni")
AREA_GPKG = os.path.join(HERE, "..", "..", "..", "..", "data", "fotouni", "town_survey", "streets_places", "fotouni_area_candidates.gpkg")
MAX_LEVELS = 2


def load():
    """Load the Fotouni run.

    Returns:
        ``(nodes_gdf, edges_gdf, streets_df)`` as needed by the planner.
    """
    nodes = gpd.read_file(os.path.join(OUT, "nodes.gpkg")).set_index("osmid")
    edges = gpd.read_file(os.path.join(OUT, "edges.gpkg")).set_index(["u", "v", "key"])
    edges["street_id"] = edges["street_id"].astype(float)
    edges["is_main_direction"] = edges["is_main_direction"].astype(str) == "True"
    return nodes, edges, pd.read_csv(os.path.join(OUT, "streets.csv"))


def verify(plan, nodes, edges, streets) -> int:
    """Name the chosen streets and check that branch naming then names every target.

    Args:
        plan: The :class:`~osmaddresssystem.naming_plan.NamingPlan`.
        nodes: Nodes.
        edges: Edges.
        streets: Streets table.

    Returns:
        The number of targets that still have no name (0 when the plan is valid).
    """
    named = streets.copy()
    for sid in plan.chosen["street_id"]:
        named.loc[named["street_id"] == sid, "street_name"] = f"planned {sid}"
    _, out = branches.assign_branch_names(nodes, edges, named, max_levels=MAX_LEVELS)
    got = out.set_index("street_id")["display_name"]
    return sum(1 for t in plan.coverage["street_id"] if not (isinstance(got.get(t), str) and got[t]))


def main() -> None:
    """Plan, verify and write the outputs."""
    nodes, edges, streets = load()
    plan = naming_plan.plan_minimal_naming(nodes, edges, streets, max_levels=MAX_LEVELS, method="auto")
    missing = verify(plan, nodes, edges, streets)
    named_now = streets["street_name"].map(branches.is_named)
    print(f"{plan.n_targets} streets can be named and have no own name; {plan.n_already_named} are already within "
          f"{MAX_LEVELS} levels of an existing name; {len(plan.chosen)} more must be named by hand "
          f"({plan.method}, {'proven minimum' if plan.optimal else 'not proven minimum'}); "
          f"after naming them {missing} targets are still unnamed (must be 0); uncovered: {plan.uncovered}")

    streets_gdf = pipeline.streets_gdf_from_edges(edges, streets)
    geo = streets_gdf.set_index("street_id").geometry.to_crs(4326)
    cen = {int(k): g.interpolate(0.5, normalized=True) for k, g in geo.items()}
    chosen = plan.chosen.merge(streets[["street_id", "street_id_original", "street_part"]], on="street_id", how="left")
    chosen["lat"] = chosen["street_id"].map(lambda s: round(cen[int(s)].y, 6) if int(s) in cen else None)
    chosen["lon"] = chosen["street_id"].map(lambda s: round(cen[int(s)].x, 6) if int(s) in cen else None)
    chosen.to_csv(os.path.join(OUT, "naming_plan.csv"), index=False)
    plan.coverage.to_csv(os.path.join(OUT, "naming_coverage.csv"), index=False)

    if os.path.exists(AREA_GPKG):
        area = gpd.read_file(AREA_GPKG, layer="area_candidates")
        hull = area[area.kind == "buildings_of_fotouni_settlements"].to_crs(streets_gdf.crs).geometry.iloc[0]
        inside = set(streets_gdf[streets_gdf.geometry.intersects(hull)]["street_id"].astype(int))
        pa = naming_plan.plan_minimal_naming(nodes, edges, streets, max_levels=MAX_LEVELS, targets=inside)
        pa.chosen.assign(lat=pa.chosen["street_id"].map(lambda s: round(cen[int(s)].y, 6) if int(s) in cen else None),
                         lon=pa.chosen["street_id"].map(lambda s: round(cen[int(s)].x, 6) if int(s) in cen else None)
                         ).to_csv(os.path.join(OUT, "naming_plan_area.csv"), index=False)
        print(f"Fotouni area: {pa.n_targets} streets, {pa.n_already_named} already reachable, {len(pa.chosen)} more to name "
              f"({pa.method}, {'proven minimum' if pa.optimal else 'not proven minimum'})")

    colors = {0: "#2e7d32", 1: "#f9a825", 2: "#ef6c00"}
    depth = dict(zip(plan.coverage["street_id"], plan.coverage["depth"]))
    order = dict(zip(plan.chosen["street_id"], plan.chosen["order"]))
    m = folium.Map(location=[5.32, 10.2275], zoom_start=14, max_zoom=21, tiles=None)
    folium.TileLayer("OpenStreetMap", max_zoom=21, max_native_zoom=19).add_to(m)
    folium.TileLayer("https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", attr="Google", name="Satellite", max_zoom=21).add_to(m)
    fg = folium.FeatureGroup(name="Streets: green = has a name, yellow = 1 level, orange = 2 levels, grey = not a target")
    fp = folium.FeatureGroup(name=f"Streets to name by hand ({len(order)}), numbered by priority")
    for sid, g in geo.items():
        d = depth.get(sid)
        name = streets.set_index("street_id")["display_name"].get(sid) if "display_name" in streets else None
        is_own = bool(named_now[streets["street_id"] == sid].any())
        col = "#2e7d32" if is_own else colors.get(d, "#9e9e9e")
        folium.GeoJson(g.__geo_interface__, style_function=lambda _, c=col: {"color": c, "weight": 4},
                       tooltip=f"street {int(sid)} | {'own name' if is_own else ('level ' + str(int(d)) if d == d and d is not None else 'not a target')}").add_to(fg)
        if sid in order:
            folium.GeoJson(g.__geo_interface__, style_function=lambda _: {"color": "#d50000", "weight": 8, "opacity": 0.8},
                           tooltip=f"NAME THIS STREET (priority {order[sid]}), street id {int(sid)}").add_to(fp)
            p = cen[int(sid)]
            folium.Marker([p.y, p.x], icon=folium.DivIcon(
                html=f'<div style="background:#d50000;color:#fff;font:700 11px sans-serif;padding:1px 5px;border-radius:9px;'
                     f'transform:translate(-50%,-50%)">{order[sid]}</div>', icon_size=(0, 0))).add_to(fp)
    fg.add_to(m)
    fp.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(os.path.join(OUT, "naming_plan_map.html"))


if __name__ == "__main__":
    main()
