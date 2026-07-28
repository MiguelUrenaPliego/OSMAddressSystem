"""Shared plotting/graph-building helpers for the tutorial notebooks.

Kept out of the notebooks themselves so each notebook's code cells stay
short and the notebook's own content is the explanation, not this
bookkeeping. Nothing here is part of the public package API - see
osmaddresssystem/street_ids.py and osmaddresssystem/street_direction.py
for the actual implementation these notebooks illustrate.
"""

import math

import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd
from matplotlib.lines import Line2D
from shapely.geometry import LineString

from osmaddresssystem import street_direction, street_ids
from osmaddresssystem.synthetic_grid import build_idealized_grid, build_irregular_grid  # noqa: F401


def arm(angle_deg: float, length: float = 100.0) -> tuple[float, float]:
    """A point `length` away from the origin at compass bearing `angle_deg`."""
    rad = math.radians(angle_deg)
    return (length * math.sin(rad), length * math.cos(rad))


def make_graph(edges, crs: str = "EPSG:3857") -> nx.MultiDiGraph:
    """Build a small MultiDiGraph in the shape street_ids/street_direction expect.

    Each item in `edges` is either:
      - (u, v, (ux, uy), (vx, vy)) - defaults to an unnamed residential edge, or
      - (u, v, (ux, uy), (vx, vy), highway, name) - explicit highway/name.
    """
    G = nx.MultiDiGraph(crs=crs)
    for edge in edges:
        if len(edge) == 4:
            u, v, uxy, vxy = edge
            highway, name = "residential", None
        else:
            u, v, uxy, vxy, highway, name = edge
        G.add_node(u, x=uxy[0], y=uxy[1])
        G.add_node(v, x=vxy[0], y=vxy[1])
        fwd = LineString([uxy, vxy])
        bwd = LineString([vxy, uxy])
        G.add_edge(u, v, 0, highway=highway, name=name, geometry=fwd, length=fwd.length, osmid=1)
        G.add_edge(v, u, 0, highway=highway, name=name, geometry=bwd, length=bwd.length, osmid=1)
    return G


def new_graph(crs: str = "EPSG:3857") -> nx.MultiDiGraph:
    """An empty MultiDiGraph, for building up a graph one one-way edge at
    a time with add_oneway_edge (dual-carriageway/connector examples,
    where the two directions are separate ways, not a single reversible
    street the way make_graph's edges are)."""
    return nx.MultiDiGraph(crs=crs)


def add_oneway_edge(G, u, v, uxy, vxy, highway="residential", name=None, osmid=1):
    """Add a single directed edge only (no reciprocal edge back), as OSM
    represents a one-way way."""
    G.add_node(u, x=uxy[0], y=uxy[1])
    G.add_node(v, x=vxy[0], y=vxy[1])
    line = LineString([uxy, vxy])
    G.add_edge(u, v, 0, highway=highway, name=name, geometry=line, length=line.length, osmid=osmid)
    return G


def label(G, angle_threshold_deg: float = 20.0, **kwargs):
    """Run street_ids.label_streets. Returns (nodes_gdf, edges_gdf, streets_df)."""
    return street_ids.label_streets(G, angle_threshold_deg=angle_threshold_deg, **kwargs)


def label_and_direct_radial(G, origin, transversal_road_pct: float = 10.0):
    """label_streets + assign_street_directionality in radial mode.

    node_cluster_m=0 disables street_ids' node-clustering feature: these
    are small hand-built graphs illustrating directionality, not
    clustering, and some of their coordinates are deliberately within
    the default 15m cluster radius of each other (e.g. (0, 10) and
    (10, 0)).
    """
    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(G, node_cluster_m=0)
    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf,
        edges_gdf,
        streets_df,
        origin=origin,
        transversal_road_pct=transversal_road_pct,
        numbering_mode="radial",
    )
    return nodes_gdf, edges_gdf, streets_df


def label_and_direct_grid(G, direction_deg: float = 0.0):
    """label_streets + assign_street_directionality in grid mode.

    See label_and_direct_radial for why node_cluster_m=0.
    """
    nodes_gdf, edges_gdf, streets_df = street_ids.label_streets(G, node_cluster_m=0)
    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf,
        edges_gdf,
        streets_df,
        origin=(0, 0),  # ignored in grid mode
        numbering_mode="grid",
        direction_deg=direction_deg,
    )
    return nodes_gdf, edges_gdf, streets_df


def plot_streets(
    edges_gdf,
    nodes_gdf=None,
    ax=None,
    title=None,
    show_rank=False,
    show_main_direction=False,
    equal_aspect=True,
):
    """Plot label_streets output: one color per street_id, labeled by
    name/id. An edge with no street_id (an unresolved connector) is
    drawn as a thin gray dashed line, since it isn't part of any street.

    With show_main_direction=True, a raw edge flagged
    is_main_direction=False (the "minor" side of an auto-merged
    dual-carriageway pair - see "Node clustering" below) is drawn
    thinner and dashed instead of the usual solid line, so the two
    carriageways sharing one street_id are still visually distinguishable.

    equal_aspect=False stretches the plot to fill its figure instead of
    keeping x/y at the same scale - useful for a divided road's two
    carriageways, only meters apart end to end but hundreds of meters
    long, which would otherwise draw as a near-invisible sliver.
    """
    if ax is None:
        _, ax = plt.subplots(figsize=(5, 5))
    ids = sorted(sid for sid in edges_gdf["street_id"].unique() if pd.notna(sid))
    cmap = plt.get_cmap("tab10")
    colors = {sid: cmap(i % 10) for i, sid in enumerate(ids)}

    seen = set()
    for (u, v, key), row in edges_gdf.iterrows():
        pair = (min(u, v), max(u, v), key)
        if pair in seen:
            continue
        seen.add(pair)
        xs, ys = row.geometry.xy
        sid = row["street_id"]
        is_main = row.get("is_main_direction", True)
        if pd.isna(sid):
            ax.plot(xs, ys, color="0.6", linewidth=1.5, linestyle=":", solid_capstyle="round")
            text = "no street_id"
        else:
            c = colors[sid]
            style = "-" if is_main else (0, (3, 2))
            width = 3 if is_main else 1.8
            ax.plot(xs, ys, color=c, linewidth=width, linestyle=style, solid_capstyle="round")
            name = row["street_name"]
            text = name if isinstance(name, str) and name else f"id {sid}"
            if show_rank:
                text += f" (rank {row.get('highway', '')})"
            if show_main_direction and not is_main:
                text += " (minor)"
        mx, my = row.geometry.interpolate(0.5, normalized=True).coords[0]
        ax.annotate(
            text,
            (mx, my),
            fontsize=8,
            ha="center",
            bbox=dict(boxstyle="round", fc="white", ec="none", alpha=0.8),
        )

    if nodes_gdf is not None:
        ax.scatter(nodes_gdf.geometry.x, nodes_gdf.geometry.y, color="black", s=15, zorder=5)

    if equal_aspect:
        ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    if title:
        ax.set_title(title, fontsize=10)
    return ax


_RADIAL_COLOR = {"radial": "#1f77b4", "transversal": "#ff7f0e"}
_GRID_COLOR = {"primary": "#1f77b4", "cross": "#ff7f0e"}


def _plot_directed_streets(edges_gdf, nodes_gdf, streets_df, color_map, ax):
    """Plot each street colored by road_type, labeled with its
    street_start_length_m-street_end_length_m range.

    No arrowheads: assign_street_directionality no longer reports which
    way a directed edge's own u->v runs relative to the street's
    numbering start (the removed `direction` column), and
    street_start_length_m/street_end_length_m alone - a fixed,
    undirected-pair property, the same for both directed copies of a
    physical edge - aren't enough to know which literal endpoint of the
    line is the "near" one without redoing the street's own walk. Plain
    lines plus the length-range label are the honest way to show the
    same information without reconstructing that.
    """
    road_type = streets_df.set_index("street_id")["road_type"]
    seen = set()
    for (u, v, key), row in edges_gdf.iterrows():
        pair = (min(u, v), max(u, v), key)
        if pair in seen:
            continue
        seen.add(pair)
        color = color_map[road_type[row["street_id"]]]
        xs, ys = row.geometry.xy
        ax.plot(xs, ys, color=color, linewidth=2.5, zorder=1)

        mx, my = row.geometry.interpolate(0.5, normalized=True).coords[0]
        ax.annotate(
            f"{row['street_start_length_m']:.0f}-{row['street_end_length_m']:.0f}m",
            (mx, my),
            fontsize=6,
            ha="center",
            color="black",
            bbox=dict(boxstyle="round", fc="white", ec="none", alpha=0.75),
        )
    ax.scatter(nodes_gdf.geometry.x, nodes_gdf.geometry.y, color="black", s=12, zorder=3)


def plot_radial_directionality(edges_gdf, nodes_gdf, streets_df, origin, ax=None, title=None):
    """Plot assign_street_directionality (radial mode) output: colored by
    road_type, labeled with each edge's cumulative-length range, and the
    origin marked with a star."""
    if ax is None:
        _, ax = plt.subplots(figsize=(5, 5))
    _plot_directed_streets(edges_gdf, nodes_gdf, streets_df, _RADIAL_COLOR, ax)
    ax.scatter([origin[0]], [origin[1]], color="red", marker="*", s=200, zorder=4)

    all_x = list(nodes_gdf.geometry.x) + [origin[0]]
    all_y = list(nodes_gdf.geometry.y) + [origin[1]]
    pad_x = max(1.0, (max(all_x) - min(all_x)) * 0.15)
    pad_y = max(1.0, (max(all_y) - min(all_y)) * 0.15)
    ax.set_xlim(min(all_x) - pad_x, max(all_x) + pad_x)
    ax.set_ylim(min(all_y) - pad_y, max(all_y) + pad_y)

    handles = [Line2D([0], [0], color=c, lw=2.5, label=t) for t, c in _RADIAL_COLOR.items()]
    handles.append(Line2D([0], [0], color="red", marker="*", lw=0, markersize=12, label="origin"))
    ax.legend(handles=handles, fontsize=7, loc="upper left", bbox_to_anchor=(1.02, 1.0))

    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    if title:
        ax.set_title(title, fontsize=10)
    return ax


def plot_grid_directionality(
    edges_gdf, nodes_gdf, streets_df, direction_deg=0.0, ax=None, title=None
):
    """Plot assign_street_directionality (grid mode) output: colored by
    road_type, labeled with each edge's cumulative-length range, and a
    small compass rose showing direction_deg (solid) and its
    perpendicular (dashed)."""
    if ax is None:
        _, ax = plt.subplots(figsize=(5, 5))
    _plot_directed_streets(edges_gdf, nodes_gdf, streets_df, _GRID_COLOR, ax)

    all_x = list(nodes_gdf.geometry.x)
    all_y = list(nodes_gdf.geometry.y)
    pad_x = max(1.0, (max(all_x) - min(all_x)) * 0.2)
    pad_y = max(1.0, (max(all_y) - min(all_y)) * 0.2)
    cx, cy = min(all_x) - pad_x * 0.4, min(all_y) - pad_y * 0.4
    arrow_len = max(pad_x, pad_y) * 0.8
    for angle, style in ((direction_deg, "-"), (direction_deg + 90, "--")):
        theta = math.radians(angle)
        dx, dy = math.sin(theta) * arrow_len, math.cos(theta) * arrow_len
        ax.annotate(
            "",
            xy=(cx + dx, cy + dy),
            xytext=(cx, cy),
            arrowprops=dict(arrowstyle="-|>", color="red", lw=1.5, linestyle=style),
            zorder=4,
        )

    ax.set_xlim(min(all_x) - pad_x, max(all_x) + pad_x)
    ax.set_ylim(min(all_y) - pad_y, max(all_y) + pad_y)

    handles = [Line2D([0], [0], color=c, lw=2.5, label=t) for t, c in _GRID_COLOR.items()]
    handles.append(Line2D([0], [0], color="red", lw=1.5, label=f"direction_deg={direction_deg:g}"))
    handles.append(Line2D([0], [0], color="red", lw=1.5, linestyle="--", label="perpendicular"))
    ax.legend(handles=handles, fontsize=7, loc="upper left", bbox_to_anchor=(1.02, 1.0))

    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    if title:
        ax.set_title(title, fontsize=10)
    return ax
