"""Builds idealized synthetic street networks for illustrating and testing
street_ids.label_streets and street_direction.assign_street_directionality.

A rows x cols grid of intersections. All streets are unnamed residential
by default, except:
  - one full horizontal row is named (demonstrates: named streets are
    left as-is, no geometric reasoning needed).
  - one full horizontal row is a higher-rank "primary" road (demonstrates:
    a higher-rank street keeps a single identity straight through
    lower-rank crossings, and the lower-rank streets crossing it are
    unaffected).
  - every other horizontal row and every vertical column is unnamed
    residential (demonstrates: equal-rank, aligned-direction streets are
    merged into one street_id across every intersection they pass
    through, while perpendicular residential streets never merge with
    each other).
"""

from __future__ import annotations

import math

import networkx as nx
from shapely.geometry import LineString


def _node_id(r: int, c: int, cols: int) -> int:
    """Flatten a (row, col) grid coordinate into a single node id.

    Args:
        r: Row index.
        c: Column index.
        cols: Number of columns in the grid.

    Returns:
        The flattened node id.
    """
    return r * cols + c


def _add_two_way_edge(
    G: nx.MultiDiGraph,
    u: int,
    v: int,
    highway: str = "residential",
    name: str | None = None,
) -> None:
    """Add a pair of reciprocal directed edges (u->v and v->u) to ``G``.

    Args:
        G: The graph to add the edges to, mutated in place.
        u: The first endpoint node id.
        v: The second endpoint node id.
        highway: The OSM ``highway`` tag value to set on both edges.
        name: The OSM ``name`` tag value to set on both edges, or ``None``
            for an unnamed street.
    """
    ux, uy = G.nodes[u]["x"], G.nodes[u]["y"]
    vx, vy = G.nodes[v]["x"], G.nodes[v]["y"]
    fwd = LineString([(ux, uy), (vx, vy)])
    bwd = LineString([(vx, vy), (ux, uy)])
    osmid = abs(hash((u, v))) % 10_000_000
    G.add_edge(u, v, 0, highway=highway, name=name, geometry=fwd, length=fwd.length, osmid=osmid)
    G.add_edge(v, u, 0, highway=highway, name=name, geometry=bwd, length=bwd.length, osmid=osmid)


def build_idealized_grid(
    rows: int = 4,
    cols: int = 4,
    spacing: float = 120.0,
    named_row: int | None = 1,
    named_row_name: str = "Elm Street",
    primary_row: int | None = 2,
    crs: str = "EPSG:3857",
) -> nx.MultiDiGraph:
    """Build a rows x cols grid graph in a synthetic planar CRS.

    Coordinates are plain metric offsets, not a real projection of
    anywhere. This makes the grid a clean, reproducible illustration
    rather than real OSM data.

    Args:
        rows: Number of grid rows (intersections along a column).
        cols: Number of grid columns (intersections along a row).
        spacing: Distance in meters between adjacent intersections.
        named_row: Row index whose horizontal street is named
            ``named_row_name``, or ``None`` to leave every row unnamed.
        named_row_name: The name given to ``named_row``'s street.
        primary_row: Row index whose horizontal street is tagged
            ``highway="primary"`` instead of ``"residential"``, or
            ``None`` to leave every row residential.
        crs: The CRS assigned to the returned graph.

    Returns:
        The synthetic grid graph.
    """
    G = nx.MultiDiGraph(crs=crs)

    for r in range(rows):
        for c in range(cols):
            G.add_node(_node_id(r, c, cols), x=c * spacing, y=r * spacing)

    # Horizontal roads (one per row).
    for r in range(rows):
        highway = "primary" if r == primary_row else "residential"
        name = named_row_name if r == named_row else None
        for c in range(cols - 1):
            _add_two_way_edge(
                G, _node_id(r, c, cols), _node_id(r, c + 1, cols), highway=highway, name=name
            )

    # Vertical roads (one per column), always unnamed residential.
    for c in range(cols):
        for r in range(rows - 1):
            _add_two_way_edge(G, _node_id(r, c, cols), _node_id(r + 1, c, cols))

    return G


def build_irregular_grid(
    rows: int = 4,
    cols: int = 5,
    spacing: float = 120.0,
    named_row: int | None = 0,
    named_row_name: str = "Elm Street",
    primary_row: int | None = 1,
    wiggle_deg: float = 10.0,
    kink_row: int | None = 3,
    kink_col: int = 2,
    kink_deg: float = 35.0,
    crs: str = "EPSG:3857",
) -> nx.MultiDiGraph:
    """Build a grid that is not perfectly square.

    Every row zigzags a few degrees off true east-west, since real
    streets are rarely dead straight. One row also has a single sharp
    turn.

    Each row's nodes are placed by walking forward one `spacing` step
    at a time, alternating the heading by +/- `wiggle_deg` around due
    east every segment. The sideways drift from a +wiggle step is
    cancelled by the next -wiggle step, so the row stays roughly
    horizontal without needing to fix its endpoint. At
    `kink_row`/`kink_col`, one segment instead turns a full `kink_deg`
    off due east.

    With the default 10 deg wiggle, under the default 20 deg merge
    threshold, each row should still collapse to a single street_id
    despite never running perfectly straight. At `kink_col`, `kink_row`
    turns by `kink_deg` (35 deg, over the threshold) and keeps that new
    heading afterward, still wiggling +/- `wiggle_deg` around it. This
    is a permanent bend, not a one-segment zigzag. So it should split
    into two streets at that node and stay two streets, not three.
    Vertical roads simply connect each row's nodes column by column, so
    they end up with a bit of a zigzag themselves rather than a
    perfectly straight north-south line.

    Args:
        rows: Number of grid rows (intersections along a column).
        cols: Number of grid columns (intersections along a row).
        spacing: Distance in meters walked per heading step along a row.
        named_row: Row index whose horizontal street is named
            ``named_row_name``, or ``None`` to leave every row unnamed.
        named_row_name: The name given to ``named_row``'s street.
        primary_row: Row index whose horizontal street is tagged
            ``highway="primary"`` instead of ``"residential"``, or
            ``None`` to leave every row residential.
        wiggle_deg: Degrees each row's heading alternates +/- around due
            east, every segment.
        kink_row: Row index that gets one permanent heading change of
            ``kink_deg`` at ``kink_col``, or ``None`` for no kink.
        kink_col: Column index at which ``kink_row``'s heading changes.
        kink_deg: Degrees added to the heading at the kink, kept for
            every subsequent segment of that row.
        crs: The CRS assigned to the returned graph.

    Returns:
        The synthetic irregular grid graph.
    """
    G = nx.MultiDiGraph(crs=crs)

    for r in range(rows):
        x, y = 0.0, r * spacing
        base_heading_deg = 0.0
        G.add_node(_node_id(r, 0, cols), x=x, y=y)
        for c in range(1, cols):
            if r == kink_row and c == kink_col:
                base_heading_deg += kink_deg
                heading_deg = base_heading_deg
            else:
                heading_deg = base_heading_deg + (wiggle_deg if c % 2 == 0 else -wiggle_deg)
            rad = math.radians(heading_deg)
            x += spacing * math.cos(rad)
            y += spacing * math.sin(rad)
            G.add_node(_node_id(r, c, cols), x=x, y=y)

    # Horizontal roads (one per row).
    for r in range(rows):
        highway = "primary" if r == primary_row else "residential"
        name = named_row_name if r == named_row else None
        for c in range(cols - 1):
            _add_two_way_edge(
                G, _node_id(r, c, cols), _node_id(r, c + 1, cols), highway=highway, name=name
            )

    # Vertical roads (one per column), always unnamed residential.
    for c in range(cols):
        for r in range(rows - 1):
            _add_two_way_edge(G, _node_id(r, c, cols), _node_id(r + 1, c, cols))

    return G
