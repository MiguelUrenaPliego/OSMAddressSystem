from __future__ import annotations

import heapq
import math
import os
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
import shapely
from shapely.geometry.base import BaseGeometry
from shapely.ops import substring as _shapely_substring

from . import name_matching
from .default_values import (
    DEFAULT_ANGLE_THRESHOLD_DEG,
    DEFAULT_MAIN_PATH_ANGLE_THRESHOLD_DEG,
    DEFAULT_CENTER_TANGENT_FRACTIONS,
    DEFAULT_HIGHWAY_RANKS,
    DEFAULT_MIN_STREET_LENGTH_M,
    DEFAULT_NAME_SIMILARITY_THRESHOLD,
    DEFAULT_NODE_CLUSTER_M,
    DEFAULT_NODE_DIRECTION_TOLERANCE_DEG,
    DEFAULT_NON_DRIVABLE_COVERAGE_FRACTION,
    DEFAULT_NON_DRIVABLE_HIGHWAYS,
    DEFAULT_NUM_NODE_DIRECTIONS,
    DEFAULT_PARALLEL_ANGLE_THRESHOLD_DEG,
    DEFAULT_PARALLEL_DIST_M,
    DEFAULT_PARALLEL_PASS_NON_TOUCHING_DIST_M,
    DEFAULT_PARALLEL_PASS_TOUCHING_DIST_M,
    DEFAULT_PARKING_SERVICE_VALUES,
    DEFAULT_PRIVATE_ACCESS_VALUES,
    DEFAULT_RANK,
    DEFAULT_TANGENT_CONTINUATION_FRACTION,
    DEFAULT_TANGENT_CONTINUATION_WINDOW_M,
    OSM_FALSY_TAG_VALUES,
)

# Local aliases so every existing use of these names below (including
# the underscore-prefixed "private, module-internal" spelling some of
# these used before all defaults moved to default_values.py) keeps
# working unchanged - the canonical values themselves now live in
# default_values.py, this module just uses them under its own names.
_CENTER_TANGENT_FRACTIONS = DEFAULT_CENTER_TANGENT_FRACTIONS
_OSM_FALSY_TAG_VALUES = OSM_FALSY_TAG_VALUES

# Safety bound on the "reassign too-short streets <-> re-apply the
# connectivity invariant" fixed-point loop in label_streets - a real
# network settles in one or two iterations, but an explicit cap avoids
# any risk of looping forever on a pathological input.
_MIN_STREET_LENGTH_MAX_ITERATIONS = 10


def stringify_nonnumeric_columns(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Cast every non-numeric, non-geometry column to string.

    GeoPackage (via Fiona/pyogrio) can't serialize columns with mixed or
    list-valued types, which OSM attributes like ``highway``/``name``
    commonly are. Apply this right before writing a labeled edges/nodes
    GeoDataFrame to a .gpkg file.
    """
    gdf = gdf.copy()
    for col in gdf.columns:
        if col != "geometry" and not pd.api.types.is_numeric_dtype(gdf[col]):
            gdf[col] = gdf[col].fillna("").astype(str)
    return gdf


def _highway_rank(highway: str | list | tuple | set | float | None, ranks: dict) -> int:
    """Resolve a raw OSM ``highway`` cell to its rank number.

    Args:
        highway: The raw ``highway`` cell value: a plain string, a
            list/tuple/set (when osmnx merges parallel/duplicate ways),
            or NaN/None if missing.
        ranks: Highway-type-to-rank mapping (see ``DEFAULT_HIGHWAY_RANKS``).

    Returns:
        The lowest (highest-priority) rank number among the cell's
        highway type(s), or ``DEFAULT_RANK`` if unrecognized/missing.
    """
    if isinstance(highway, (list, tuple, set)):
        return min((ranks.get(h, DEFAULT_RANK) for h in highway), default=DEFAULT_RANK)
    if pd.isna(highway):
        return DEFAULT_RANK
    return ranks.get(highway, DEFAULT_RANK)


def _highway_value_and_rank(
    highway: str | list | tuple | set | float | None, ranks: dict
) -> tuple[str | None, int]:
    """Resolve a raw OSM ``highway`` cell to (value, rank).

    OSM's ``highway`` tag can be a list when osmnx merges parallel or
    duplicate ways - in that case the highest-rank (lowest rank number)
    entry is taken as the representative value, matching how
    ``_highway_rank`` already picks the rank for such cells.

    Args:
        highway: The raw ``highway`` cell value: a plain string, a
            list/tuple/set, or NaN/None if missing.
        ranks: Highway-type-to-rank mapping (see ``DEFAULT_HIGHWAY_RANKS``).

    Returns:
        A tuple ``(value, rank)``: the representative highway string (or
        ``None`` if missing/empty) and its rank number.
    """
    if isinstance(highway, (list, tuple, set)):
        best_value, best_rank = None, None
        for h in highway:
            r = ranks.get(h, DEFAULT_RANK)
            if best_rank is None or r < best_rank:
                best_value, best_rank = h, r
        return best_value, (DEFAULT_RANK if best_rank is None else best_rank)
    if pd.isna(highway):
        return None, DEFAULT_RANK
    return highway, ranks.get(highway, DEFAULT_RANK)


def _clean_name(name: str | list | tuple | set | float | None) -> str | None:
    """Resolve a raw OSM ``name`` cell to a clean, non-empty string or ``None``.

    Args:
        name: The raw ``name`` cell value: a plain string, a list/tuple/
            set (first truthy element is used), or NaN/None if missing.

    Returns:
        The stripped name string, or ``None`` if missing/empty.
    """
    if isinstance(name, (list, tuple, set)):
        name = next((n for n in name if n), None)
    if name is None or pd.isna(name):
        return None
    name = str(name).strip()
    return name or None


def _vectorized_highway_rank_and_value(
    highway: pd.Series, ranks: dict
) -> tuple[pd.Series, pd.Series]:
    """Vectorized ``_highway_value_and_rank`` over a whole column.

    Returns (rank, value): ``value`` is the representative highway string
    for each edge - itself for a plain string, or (since OSM's
    ``highway`` tag can be a list, e.g. when osmnx merges parallel or
    duplicate ways) the highest-rank entry of the list.

    OSM's ``highway`` is almost always a plain string, so the fast path
    is a single ``Series.map`` (a dict lookup per element, no per-row
    Python function call). List-valued cells can't go through ``.map``
    (dict lookup requires hashable keys), so they're detected and
    resolved separately, only for that (normally tiny) subset.
    """
    is_list = highway.map(lambda h: isinstance(h, (list, tuple, set)))
    rank = highway.where(~is_list).map(ranks)
    value = highway.where(~is_list)
    if is_list.any():
        resolved = highway.loc[is_list].map(lambda hs: _highway_value_and_rank(hs, ranks))
        value.loc[is_list] = resolved.map(lambda vr: vr[0])
        rank.loc[is_list] = resolved.map(lambda vr: vr[1])
    rank = rank.fillna(DEFAULT_RANK).astype(int)
    value = value.astype(object).where(value.notna(), None)
    return rank, value


def _vectorized_clean_name(name: pd.Series) -> pd.Series:
    """Vectorized ``_clean_name`` over a whole column, returning an
    object-dtype Series that holds real Python ``None`` (not float
    ``NaN``) for missing/empty names.

    List-valued cells (rare) are resolved to their first truthy element
    with a small per-row pass; everything else runs through pandas'
    vectorized string ops (`.str.strip`).
    """
    is_list = name.map(lambda x: isinstance(x, (list, tuple, set)))
    scalarized = name
    if is_list.any():
        scalarized = name.copy()
        scalarized.loc[is_list] = name.loc[is_list].map(
            lambda ns: next((n for n in ns if n), None)
        )

    stripped = scalarized.astype("string").str.strip()
    stripped = stripped.replace("", pd.NA)
    return stripped.astype(object).where(stripped.notna(), None)



def _osm_tag_is_truthy(value: str | list | tuple | set | float | None) -> bool:
    """Whether a single OSM tag cell (possibly list-valued, from osmnx
    merging parallel/duplicate ways) is "set" in OSM's yes/no-ish tag
    convention - see _OSM_FALSY_TAG_VALUES.

    Args:
        value: The raw tag cell value.

    Returns:
        True if the value counts as "set" under OSM's yes/no-ish
        convention.
    """
    if isinstance(value, (list, tuple, set)):
        return any(_osm_tag_is_truthy(v) for v in value)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return str(value).strip().lower() not in _OSM_FALSY_TAG_VALUES


def _vectorized_structure_type(tunnel: pd.Series, bridge: pd.Series) -> pd.Series:
    """Resolve OSM's separate ``tunnel``/``bridge`` tag columns to one
    ``structure_type`` column: ``"tunnel"``, ``"bridge"``, or ``None``
    for a plain at-grade edge. An edge tagged as both (invalid in
    practice, but OSM data isn't always clean) is reported as
    ``"tunnel"`` - arbitrary, but deterministic and documented, same as
    how a list-valued ``highway`` tag is resolved to its single
    highest-rank member elsewhere in this module.

    Args:
        tunnel: The raw ``tunnel`` tag column.
        bridge: The raw ``bridge`` tag column.

    Returns:
        An object-dtype Series of ``"tunnel"``, ``"bridge"``, or ``None``
        per row.
    """
    is_tunnel = tunnel.map(_osm_tag_is_truthy)
    is_bridge = bridge.map(_osm_tag_is_truthy)
    return pd.Series(
        np.select([is_tunnel, is_bridge], ["tunnel", "bridge"], default=None),
        index=tunnel.index,
        dtype=object,
    )


def _access_is_private(
    value: str | list | tuple | set | float | None, private_access_values: set
) -> bool:
    """Whether a single OSM ``access`` tag cell (possibly list-valued,
    from osmnx merging parallel/duplicate ways) marks the edge as
    private - see DEFAULT_PRIVATE_ACCESS_VALUES.

    Args:
        value: The raw ``access`` tag cell value.
        private_access_values: Lowercased ``access`` values that count
            as private.

    Returns:
        True if the cell's value (or any element, if list-valued) is a
        private access value.
    """
    if isinstance(value, (list, tuple, set)):
        return any(_access_is_private(v, private_access_values) for v in value)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return str(value).strip().lower() in private_access_values


def _vectorized_is_private(access: pd.Series, private_access_values: set) -> pd.Series:
    """Vectorized ``_access_is_private`` over a whole ``access`` column."""
    return access.map(lambda v: _access_is_private(v, private_access_values))


def _service_is_parking(
    value: str | list | tuple | set | float | None, parking_service_values: set
) -> bool:
    """Whether a single OSM ``service`` tag cell (possibly list-valued,
    from osmnx merging parallel/duplicate ways) marks the edge as a
    parking-lot internal way - see DEFAULT_PARKING_SERVICE_VALUES.
    Mirrors ``_access_is_private`` exactly, keyed off ``service`` instead
    of ``access``.

    Args:
        value: The raw ``service`` tag cell value.
        parking_service_values: Lowercased ``service`` values that count
            as "inside a parking lot".

    Returns:
        True if the cell's value (or any element, if list-valued) is a
        parking-service value.
    """
    if isinstance(value, (list, tuple, set)):
        return any(_service_is_parking(v, parking_service_values) for v in value)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    return str(value).strip().lower() in parking_service_values


def _vectorized_is_parking(service: pd.Series, parking_service_values: set) -> pd.Series:
    """Vectorized ``_service_is_parking`` over a whole ``service`` column."""
    return service.map(lambda v: _service_is_parking(v, parking_service_values))


def _endpoint_angle_deg(geom: BaseGeometry, at_start: bool) -> float:
    """Tangent direction (deg) of the line at one of its endpoints, as an
    undirected orientation folded into [0, 180). The tangent is measured
    from the endpoint to the immediately adjacent vertex - i.e. exactly
    at the edge end, not inset some fraction of the way along the edge.

    Args:
        geom: The edge's line geometry.
        at_start: True to measure the tangent at the geometry's first
            coordinate, False for its last.

    Returns:
        The undirected tangent angle in degrees, folded into [0, 180).
    """
    p0 = geom.coords[0] if at_start else geom.coords[-1]
    p1 = geom.coords[1] if at_start else geom.coords[-2]
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    # Rounded to avoid the interpolation above nudging an otherwise-exact
    # angle (e.g. two segments meant to be exactly 20 deg apart) across a
    # comparison threshold by a few ULPs.
    return round(math.degrees(math.atan2(dy, dx)) % 180, 6)


def _vectorized_endpoint_angles(
    geometries: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized ``_endpoint_angle_deg`` for both endpoints of every
    geometry in one shot, using shapely's array (ufunc) API instead of
    a Python loop + per-geometry ``.coords`` access.

    Also returns the same tangents' *raw*, directed bearing (mod 360,
    not folded to [0, 180)) - the "away from this endpoint, into the
    edge" direction as an ordinary compass bearing. Two edges meeting at
    a node continue each other in a genuine straight line only if their
    raw away-from-node bearings are roughly *opposite* (~180 deg apart);
    if they're instead roughly the *same* (~0 deg apart), the two edges
    head off in the same general direction from the node - e.g. two
    branches of a Y opening on one side, not a through-line - even
    though both cases fold to the same undirected value under mod 180
    and so look identical to every other angle comparison in this
    module (rule 3, the fork tie-break in ``_enforce_max_two_per_node``,
    etc.). See ``_enforce_max_two_per_node``'s ``raw_angle_at_node``
    parameter for where this distinction is used.

    Args:
        geometries: Array of line geometries, one per undirected edge.

    Returns:
        A 4-tuple ``(angle_at_start, angle_at_end, raw_bearing_at_start,
        raw_bearing_at_end)``, one value per input geometry: the first
        two folded into [0, 180) same as before, the last two the same
        tangents' raw compass bearing in [0, 360).
    """
    p0_start = shapely.get_coordinates(shapely.get_point(geometries, 0))
    p1_start = shapely.get_coordinates(shapely.get_point(geometries, 1))
    p0_end = shapely.get_coordinates(shapely.get_point(geometries, -1))
    p1_end = shapely.get_coordinates(shapely.get_point(geometries, -2))

    # Rounded for the same reason as _endpoint_angle_deg: interpolation
    # can nudge an otherwise-exact angle across a comparison threshold by
    # a few ULPs.
    raw_start = np.round(
        np.degrees(np.arctan2(p1_start[:, 1] - p0_start[:, 1], p1_start[:, 0] - p0_start[:, 0]))
        % 360,
        6,
    )
    raw_end = np.round(
        np.degrees(np.arctan2(p1_end[:, 1] - p0_end[:, 1], p1_end[:, 0] - p0_end[:, 0])) % 360,
        6,
    )
    start_angle = raw_start % 180
    end_angle = raw_end % 180
    return start_angle, end_angle, raw_start, raw_end


def _angle_diff(a: float, b: float) -> float:
    """Difference between two undirected (mod-180) orientations, folded
    into [0, 90].

    Args:
        a: First orientation, in degrees.
        b: Second orientation, in degrees.

    Returns:
        The folded circular distance, in [0, 90].
    """
    diff = abs(a - b) % 180
    return min(diff, 180 - diff)


def _direction_bin_offset(angles_deg: list[float], num_bins: int) -> float:
    """The rotation offset for splitting a node's own incident tangent
    angles into ``num_bins`` equal-width bins over [0, 180) WITHOUT
    cutting a real cluster of nearby directions in half - inspired by
    the reference gap-finding technique in
    report/reference_angle_algorithm.txt's own ``max_separation_angle``.

    Since the fixed grid's own boundaries repeat every ``bin_width``
    (``180 / num_bins``), whether any two angles end up split across a
    boundary depends only on their own values MODULO ``bin_width``, not
    their raw separation in the full [0, 180) range - two angles 3 deg
    apart can still land in different bins if a boundary happens to fall
    between them, while two angles 70 deg apart can share a bin if
    neither's own remainder crosses one. So every real angle is first
    projected to ``angle % bin_width``, the single widest gap between
    those PROJECTED values is found (exactly ``max_separation_angle``'s
    own technique, just over a period of ``bin_width`` instead of 180),
    and the grid is rotated to place a boundary at that gap's own
    midpoint - the placement most likely to avoid splitting any real
    cluster, given a single global rotation has to serve every node's
    own present directions at once.

    Args:
        angles_deg: Every distinct tangent angle (already folded to
            [0, 180)) present at this node.
        num_bins: Number of equal-width bins the grid will be split
            into - see ``num_node_directions``/``_direction_bin_index``.

    Returns:
        The offset (degrees, in [0, 180)) to rotate the fixed bin grid
        by - see ``_direction_bin_index``.
    """
    if num_bins <= 0:
        return 0.0
    bin_width = 180.0 / num_bins
    reduced = sorted(set(a % bin_width for a in angles_deg))
    if len(reduced) <= 1:
        return (reduced[0] + bin_width / 2) % bin_width if reduced else 0.0
    n = len(reduced)

    def min_boundary_distance(offset: float) -> float:
        return min(
            min((p - offset) % bin_width, bin_width - (p - offset) % bin_width)
            for p in reduced
        )

    # Every gap's own midpoint is a placement candidate. Gap WIDTH alone
    # isn't a reliable score on its own: two points exactly bin_width/2
    # apart produce two equally-wide gaps (the one directly between them
    # and the one going the other way around), only ONE of which
    # actually avoids splitting them - so candidates are ranked by how
    # many DISTINCT bins they'd actually produce across every real angle
    # first (fewer is strictly better - the real objective), and only
    # tie-broken by ``min_boundary_distance`` (the more centered,
    # generically robust placement) when two candidates tie on that.
    best_offset = 0.0
    best_score = (len(angles_deg) + 1, -1.0)
    for i in range(n):
        a = reduced[i]
        b = reduced[(i + 1) % n]
        gap = (b - a) % bin_width
        if gap == 0:
            gap = bin_width
        candidate = (a + gap / 2) % bin_width
        n_bins_used = len({_direction_bin_index(x, candidate, num_bins) for x in angles_deg})
        score = (n_bins_used, -min_boundary_distance(candidate))
        if score < best_score:
            best_score = score
            best_offset = candidate
    return best_offset


def _direction_bin_index(angle_deg: float, offset: float, num_bins: int) -> int:
    """Which of ``num_bins`` equal-width direction bins over [0, 180)
    ``angle_deg`` falls into, after rotating the fixed grid by
    ``offset`` (see ``_direction_bin_offset``).

    Args:
        angle_deg: A folded ([0, 180)) tangent angle.
        offset: The grid's own rotation, from ``_direction_bin_offset``.
        num_bins: Number of equal-width bins to split [0, 180) into -
            see ``num_node_directions``.

    Returns:
        The bin index, in ``[0, num_bins)``.
    """
    shifted = (angle_deg - offset) % 180
    idx = int(shifted * num_bins / 180)
    return min(idx, num_bins - 1)


def _point_to_line_distance(
    point: tuple[float, float], line_p0: tuple[float, float], line_p1: tuple[float, float]
) -> float:
    """Perpendicular distance from ``point`` to the INFINITE line through
    ``line_p0``/``line_p1``. Used by the branch-drift check: a branch's
    own tracked reference line is anchored by real points it has already
    passed through, so there's no risk of matching something merely
    collinear far away in space the way an infinite line would for two
    edges with no other relationship at all (contrast
    ``_edges_are_truly_parallel``'s deliberately bounded-geometry check).

    Args:
        point: The point to measure from, as an (x, y) tuple.
        line_p0, line_p1: Two points defining the line.

    Returns:
        The perpendicular distance, or the plain point-to-point distance
        to ``line_p0`` if the two line points coincide (degenerate).
    """
    x0, y0 = point
    x1, y1 = line_p0
    x2, y2 = line_p1
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        return math.hypot(x0 - x1, y0 - y1)
    return abs((x0 - x1) * dy - (y0 - y1) * dx) / length


def _capped_far_point(
    geom: BaseGeometry, at_start: bool, max_dist: float
) -> tuple[float, float]:
    """A point along ``geom``, measured from its ``at_start`` end, capped
    to at most ``max_dist`` away - for the branch-drift point-to-line
    check's own "far" point (see its callers).

    That check is meant to catch two edges that meet at a shared point,
    head the same way, and then IMMEDIATELY curve apart - a local
    fork - not to demand that either edge run straight over its ENTIRE
    length. Using the edge's own raw opposite endpoint as "the far point"
    conflates the two: a real, gently curving road (e.g. a rural road
    tracking terrain) can easily drift more than ``max_dist`` from its
    own near-node tangent line over a long span even though it never
    forks - failing this check for every long edge regardless of how well
    its immediate tangent matches at the node, a real bug found via
    real-data testing (two collinear, unnamed, same-rank edges meeting
    cleanly at one node, differing by ~1 deg in tangent, refused merging
    because one of them ran ~500m and gently curved along the way).
    Capping both sides' own "far" point to the same local ``max_dist``
    keeps the check about immediate local straightness; genuine multi-hop
    drift across a whole branch is still caught afterward, over the final
    settled composition, by the branching invariant
    (``_split_branching_street_ids``), exactly as intended.

    Args:
        geom: The edge's line geometry.
        at_start: True if the near (shared) endpoint is ``geom``'s first
            coordinate, False if it's the last.
        max_dist: The cap, in the same units as the geometry's own
            coordinates (meters for a projected CRS).

    Returns:
        The capped point, as an (x, y) tuple.
    """
    length = geom.length
    if length <= 0:
        return geom.coords[0]
    dist = min(length, max_dist)
    pos = dist if at_start else length - dist
    return geom.interpolate(pos, normalized=False).coords[0]


def _tangent_continuation_fraction(
    short_geom: BaseGeometry,
    short_at_start: bool,
    long_geom: BaseGeometry,
    long_at_start: bool,
    corridor_width: float,
    window_m: float,
) -> float:
    """What fraction of ``long_geom``'s own length NEAR THE SHARED NODE
    (only the first ``window_m`` of it, not its full length) stays close
    to the straight (two-sided, INFINITE-in-effect) line through the
    shared node along ``short_geom``'s own immediate tangent there - see
    DEFAULT_TANGENT_CONTINUATION_FRACTION.

    Distinguishes a genuine gentle curve (most of the longer edge's own
    length, near the node, stays within ``corridor_width`` of that
    straight tangent line, even though the two edges' own WHOLE-EDGE
    bearings can end up quite different) from a real fork (the edge
    heads somewhere else for good almost immediately). The shorter
    edge's own tangent is used as the reference direction since it's the
    more locally reliable one - a short edge has had less length over
    which to drift from its own initial direction.

    Two things this deliberately does NOT do, both found to matter on
    real data (streets several hundred meters long, gently curving the
    whole way): the reference line is TWO-sided (extends past the node
    in both directions, not just outward along short_geom's own tangent)
    since a genuine "straight through" continuation runs the OPPOSITE
    compass direction from short_geom's own tangent, not the same one;
    and long_geom's own coverage is measured only within ``window_m`` of
    the node, not over its full length - demanding a long street stay
    within a fixed corridor for its ENTIRE length fails it for perfectly
    ordinary curvature that only matters over enough distance, exactly
    the false-negative this whole mechanism exists to avoid.

    Args:
        short_geom: The shorter of the two edges being compared - its
            own tangent (at the shared node) sets the reference line.
        short_at_start: True if the shared node is ``short_geom``'s
            first coordinate, False if it's the last.
        long_geom: The longer edge, whose own near-node coverage is
            measured.
        long_at_start: True if the shared node is ``long_geom``'s first
            coordinate, False if it's the last.
        corridor_width: How far from the reference line still counts as
            "close" (same units as the geometries' own coordinates -
            meters for a projected CRS).
        window_m: How far along ``long_geom``, from the shared node, to
            measure coverage over - the rest of a long edge (past this
            distance) is ignored entirely.

    Returns:
        The covered fraction, in [0, 1] (0 if ``long_geom`` has no
        length, or if the shared endpoint's own adjacent segment is
        degenerate).
    """
    if long_geom.length <= 0:
        return 0.0
    coords = list(short_geom.coords)
    p0 = coords[0] if short_at_start else coords[-1]
    p1 = coords[1] if short_at_start else coords[-2]
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    seg_len = math.hypot(dx, dy)
    if seg_len == 0:
        return 0.0
    ux, uy = dx / seg_len, dy / seg_len
    # Extended past window_m in BOTH directions from the node - a real
    # "straight through" continuation runs the opposite compass
    # direction from short_geom's own tangent, so a one-sided ray
    # outward along that tangent would miss it entirely.
    extend = window_m + short_geom.length + 1.0
    p_neg = (p0[0] - ux * extend, p0[1] - uy * extend)
    p_pos = (p0[0] + ux * extend, p0[1] + uy * extend)
    corridor = shapely.LineString([p_neg, p_pos]).buffer(corridor_width, cap_style="flat")

    long_length = long_geom.length
    window = min(long_length, window_m)
    near_geom = (
        _shapely_substring(long_geom, 0, window)
        if long_at_start
        else _shapely_substring(long_geom, long_length - window, long_length)
    )
    if near_geom.length <= 0:
        return 0.0
    covered = near_geom.intersection(corridor)
    return covered.length / near_geom.length


def _farthest_point_pair(
    points: list[tuple[float, float]],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """The single farthest-apart pair among ``points`` (at most a
    handful, so a plain O(n^2) scan is fine) - used to extend a branch's
    own tracked reference line as it grows to cover its own full,
    now-larger span. ``points`` must have at least 2 entries.
    """
    best_pair = (points[0], points[1])
    best_dist = -1.0
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            dist = math.hypot(points[i][0] - points[j][0], points[i][1] - points[j][1])
            if dist > best_dist:
                best_dist = dist
                best_pair = (points[i], points[j])
    return best_pair


def _cluster_nodes(nodes_gdf: gpd.GeoDataFrame, node_cluster_m: float) -> dict[int, int]:
    """Group graph nodes within ``node_cluster_m`` of each other into a
    shared cluster id (the id of whichever node became that cluster's
    "leader" - see below).

    Nodes closer than ``node_cluster_m`` are treated as "the same
    intersection" for the purposes of dual-carriageway detection and
    connector-edge resolution (see ``label_streets``), since a real
    intersection is often mapped as several nodes a few meters apart
    (e.g. where a divided road's two carriageways separate).

    Uses leader (star) clustering, not a transitive (single-linkage)
    closure: nodes are visited in id order, and the first not-yet-
    clustered node in that order becomes a cluster leader, absorbing
    every other not-yet-clustered node within ``node_cluster_m`` *of
    that leader*. A single-linkage closure would instead chain clusters
    through intermediate nodes indefinitely: e.g. A 8m from B, B 12m
    from C, but A 21m from C, would still put all three in one cluster
    (each direct pair is within a typical 15m radius, but the cluster
    as a whole spans 21m - well past what "the same intersection"
    should mean, and enough to wrongly trigger dual-carriageway
    auto-merging between unrelated edges touching A and C). Leader
    clustering bounds every cluster's diameter to at most
    ``2 * node_cluster_m`` instead, since every member is within
    ``node_cluster_m`` of the same leader.

    Args:
        nodes_gdf: Graph nodes with point geometries, indexed by node id.
        node_cluster_m: Distance (in ``nodes_gdf``'s CRS units) within
            which two nodes are treated as the same intersection. A
            value <= 0 disables clustering (every node is its own
            singleton cluster).

    Returns:
        A dict mapping every node id to its cluster leader's node id.
    """
    node_ids = nodes_gdf.index.to_numpy()
    if node_cluster_m <= 0 or len(node_ids) == 0:
        return {n: n for n in node_ids}

    points = nodes_gdf.geometry.to_numpy()
    tree = shapely.STRtree(points)
    left, right = tree.query(points, predicate="dwithin", distance=node_cluster_m)

    neighbors = defaultdict(list)
    for i, j in zip(left, right):
        if i != j:
            neighbors[i].append(j)

    cluster_of: dict = {}
    for i in np.argsort(node_ids):
        node = node_ids[i]
        if node in cluster_of:
            continue
        cluster_of[node] = node
        for j in neighbors[i]:
            neighbor = node_ids[j]
            if neighbor not in cluster_of:
                cluster_of[neighbor] = node

    return cluster_of


def _overall_bearing_deg(geom: BaseGeometry) -> float:
    """Undirected (mod-180) bearing of the straight line between the
    points _CENTER_TANGENT_FRACTIONS of the way along an edge geometry -
    its direction through its own center, ignoring endpoint noise, as
    opposed to ``_endpoint_angle_deg``'s tangent at one specific end.

    Args:
        geom: The edge's line geometry.

    Returns:
        The undirected bearing in degrees, folded into [0, 180).
    """
    frac_a, frac_b = _CENTER_TANGENT_FRACTIONS
    p0 = geom.interpolate(frac_a, normalized=True).coords[0]
    p1 = geom.interpolate(frac_b, normalized=True).coords[0]
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    return round(math.degrees(math.atan2(dy, dx)) % 180, 6)


def _center_segment(geom: BaseGeometry) -> BaseGeometry:
    """The short segment between the points ``_CENTER_TANGENT_FRACTIONS``
    of the way along an edge geometry - the same "central part" ``_overall_
    bearing_deg`` measures direction through. Used to measure distance
    between two edges' own central parts rather than their whole
    geometries (which can be skewed by a curve or a noisy digitized
    endpoint far from where the two edges actually run alongside each
    other).

    Args:
        geom: The edge's line geometry.

    Returns:
        A 2-point ``LineString`` spanning the edge's own central part.
    """
    frac_a, frac_b = _CENTER_TANGENT_FRACTIONS
    p0 = geom.interpolate(frac_a, normalized=True)
    p1 = geom.interpolate(frac_b, normalized=True)
    return shapely.LineString([p0, p1])


def _connected_components(idxs: list[int], adjacency: dict[int, set[int]], eids: np.ndarray) -> list[list[int]]:
    """Plain connected-components split of ``idxs`` over ``adjacency``,
    deterministic regardless of iteration order: each component is
    discovered starting from its own remaining member with the smallest
    edge id.

    Args:
        idxs: The (array-position) indices to partition.
        adjacency: Index -> set of adjacent indices, restricted to
            (a superset of) ``idxs``.
        eids: Undirected edge ids, aligned with the arrays ``idxs``'
            members index into - used only for the deterministic start
            order.

    Returns:
        A list of components, each a list of indices, in no particular
        order (the caller sorts if order matters).
    """
    remaining = set(idxs)
    components: list[list[int]] = []
    while remaining:
        start = min(remaining, key=lambda i: eids[i])
        comp: list[int] = []
        stack = [start]
        seen = {start}
        while stack:
            k = stack.pop()
            comp.append(k)
            for j in adjacency[k]:
                if j in remaining and j not in seen:
                    seen.add(j)
                    stack.append(j)
        remaining -= seen
        components.append(comp)
    return components


def _split_disconnected_components(
    eids: np.ndarray,
    cluster_u_arr: np.ndarray,
    cluster_v_arr: np.ndarray,
    geometries: np.ndarray,
    street_id_arr: np.ndarray,
    parallel_dist_m: float,
    next_id: int,
) -> tuple[np.ndarray, int]:
    """Final, unconditional invariant-enforcing pass: every resulting
    ``street_id``'s own edges must form exactly one connected component -
    where "connected" means either touching (sharing a clustered node) OR
    at least one genuinely parallel edge pair (``_edges_are_truly_parallel``)
    between two touching-only clusters within ``parallel_dist_m`` - plain
    proximity alone is never enough, since two edges can pass close to
    each other while running in completely different directions (e.g. an
    unrelated footway and a tunnel stairway) without being the same
    street. Run at the very end of ``label_streets``, after every other
    merge/split/donation step has finished, as a defensive safety net
    independent of any individual step's own connectivity behavior.

    This is needed because a purely touching-based connectivity check
    would wrongly split apart merges this module makes ON PURPOSE by
    proximity rather than a shared node - most notably a divided avenue's
    two physically separate carriageways (``_merge_parallel_edges``,
    ``_second_pass_reassign`` step (b)), and a non-drivable edge donated
    a nearby drivable street's id (``_donate_non_drivable_furniture``),
    both of which are still legitimately "the same street" for addressing
    purposes despite never sharing a node. Only a `street_id` whose
    clusters are farther apart than `parallel_dist_m` even from their own
    single nearest other cluster - e.g. two unrelated too-short streets
    each independently redistributed into the same distant target via
    `_redistribute_short_streets`'s nearest fallback, with nothing at all
    linking them to each other - gets split.

    Two-phase per `street_id`, to keep the common case (already fully
    touching-connected) cheap: first, clusters are found using ONLY
    clustered-node touching (see "Node clustering" - ``_cluster_nodes``).
    Only if that leaves more than one cluster does distance get checked
    at all (skipped entirely for every `street_id` that's already one
    connected cluster by touching alone): for every pair of
    touching-only clusters, the minimum distance between their own
    combined geometries is computed, and any pair at or under
    `parallel_dist_m` apart is merged together (transitively - a chain of
    three clusters each within `parallel_dist_m` of the next all end up
    merged, even if the two end clusters are themselves farther apart
    than that). Whatever cluster grouping remains after that is final.

    For each `street_id` whose own edges still span more than one
    cluster after both phases, every cluster keeps its own edges under a
    distinct `street_id`: the cluster containing the smallest edge id
    keeps the group's original `street_id`; every other cluster gets a
    fresh id from `next_id` upward, in order of each cluster's own
    smallest edge id - deterministic, never dependent on iteration
    order. Splitting a `street_id` this way necessarily changes which
    edges belong to which id, so the caller must recompute
    `is_main_direction`/main-path length (`_compute_main_direction`)
    fresh afterward, exactly as it already does after every other
    id-changing step in this module.

    Args:
        eids: Undirected edge ids.
        cluster_u_arr: First endpoint's own CLUSTERED node id per edge
            (see ``_cluster_nodes``), aligned with ``eids``.
        cluster_v_arr: Second endpoint's own clustered node id per edge,
            aligned with ``eids`` - see ``cluster_u_arr`` above.
        geometries: Line geometry per edge, aligned with ``eids``.
        street_id_arr: Current street_id per edge (``-1`` for none),
            aligned with ``eids``.
        parallel_dist_m: Maximum distance (meters) between two
            touching-only clusters of the same `street_id` for them to
            still count as one connected group. A value <= 0 disables
            the distance phase entirely (only literal touching counts).
        next_id: The next unused street_id to hand out.

    Returns:
        A tuple ``(street_id_arr, next_id)``: the updated array (a copy)
        and the next unused street_id after handing out any needed here.
    """
    out = street_id_arr.copy()

    by_sid: dict[int, list[int]] = defaultdict(list)
    for i in range(len(eids)):
        if out[i] != -1:
            by_sid[int(out[i])].append(i)

    has_dist_phase = bool(parallel_dist_m and parallel_dist_m > 0)

    for sid, idxs in by_sid.items():
        if len(idxs) <= 1:
            continue

        node_to_idxs: dict = defaultdict(list)
        for i in idxs:
            node_to_idxs[cluster_u_arr[i]].append(i)
            node_to_idxs[cluster_v_arr[i]].append(i)
        touch_adjacency: dict[int, set] = defaultdict(set)
        for i in idxs:
            for node in (cluster_u_arr[i], cluster_v_arr[i]):
                for j in node_to_idxs[node]:
                    if j != i:
                        touch_adjacency[i].add(j)

        components = _connected_components(idxs, touch_adjacency, eids)
        if len(components) > 1 and has_dist_phase:
            # Merge any pair of touching-only clusters that have at
            # least one genuinely parallel edge pair between them (see
            # _edges_are_truly_parallel) within parallel_dist_m - mere
            # proximity is NOT enough (two edges that happen to pass
            # close to each other while running in different directions,
            # e.g. a footway and an unrelated tunnel stairway, must not
            # be considered "the same street" just because they're near
            # each other).
            comp_parent = list(range(len(components)))

            def cfind(x: int, comp_parent=comp_parent) -> int:
                while comp_parent[x] != x:
                    comp_parent[x] = comp_parent[comp_parent[x]]
                    x = comp_parent[x]
                return x

            for a in range(len(components)):
                for b in range(a + 1, len(components)):
                    ca, cb = cfind(a), cfind(b)
                    if ca == cb:
                        continue
                    if any(
                        _edges_are_truly_parallel(geometries[i], geometries[j], parallel_dist_m)
                        for i in components[a]
                        for j in components[b]
                    ):
                        comp_parent[cb] = ca

            merged: dict[int, list[int]] = defaultdict(list)
            for c, comp in enumerate(components):
                merged[cfind(c)].extend(comp)
            components = list(merged.values())

        if len(components) <= 1:
            continue

        components.sort(key=lambda comp: min(eids[k] for k in comp))
        for i in components[0]:
            out[i] = sid
        for comp in components[1:]:
            for i in comp:
                out[i] = next_id
            next_id += 1

    return out, next_id


def _split_branching_street_ids(
    eids: np.ndarray,
    u_arr: np.ndarray,
    v_arr: np.ndarray,
    cluster_u_arr: np.ndarray,
    cluster_v_arr: np.ndarray,
    geometries: np.ndarray,
    bearing_arr: np.ndarray,
    start_angle: np.ndarray,
    end_angle: np.ndarray,
    street_id_arr: np.ndarray,
    parallel_angle_threshold_deg: float,
    parallel_dist_m: float,
    touching_dist_m: float,
    num_node_directions: int,
    pinned_eids: frozenset,
    next_id: int,
) -> tuple[np.ndarray, int]:
    """Re-enforce "no more than one direction per node_id" (see that
    section in report/street_identity.md) as a final, unconditional
    invariant over every street_id's FINAL composition, exactly the way
    phase 2 enforces it during the original per-node_id assignment.

    This is needed because several later steps - coverage-based
    donation, the general cross-street_id 90% pass, and length-based
    reassignment - move edges (or whole street_ids) by proximity/
    coverage alone, with no check for what direction the moved edge
    points at any node_id it shares with its NEW street_id's other
    edges. A real bug found via real-data testing: edges reassigned this
    way created genuine branches - two or more incompatible directions
    meeting at one node_id under the same street_id - well past what any
    single step's own local check could catch, since the check that
    matters is about the FINAL, fully-settled composition, not any one
    step's own immediate edit.

    For each street_id, its own member edges are re-grouped from
    scratch via a union-find using the SAME two signals the original
    assignment does: two members are unioned if they share a node_id
    and their own tangents there fall in the same FIXED direction bin
    (mirroring phase 2's own ``num_node_directions`` grid, gated by a
    point-to-line branch-drift check - see that phase's own docstring),
    OR they're genuinely parallel (``_edges_are_truly_parallel``) within
    ``parallel_dist_m`` even without touching (mirroring phase 1, so a
    legitimate non-touching dual-carriageway-style pair already merged
    earlier isn't wrongly split apart here). Whatever doesn't end up in
    the largest resulting group is peeled off into a fresh street_id of
    its own.

    Args:
        eids: Undirected edge ids.
        u_arr, v_arr: Raw node id per endpoint.
        cluster_u_arr, cluster_v_arr: Clustered node_id per endpoint.
        geometries: Line geometry per edge.
        bearing_arr: Each edge's own center bearing.
        start_angle, end_angle: Folded tangent angle at the u/v endpoint.
        street_id_arr: Current street_id per edge (``-1`` for none).
        parallel_angle_threshold_deg: Coarse bearing pre-filter for the
            non-touching genuine-parallelism check, applied before the
            more expensive ``_edges_are_truly_parallel`` - without it, a
            short perpendicular stub touching one point of a much longer
            edge can pass that check on bounded distance alone despite
            running a completely different direction.
        parallel_dist_m: Distance (meters) for the non-touching genuine-
            parallelism check - see ``_edges_are_truly_parallel``. A
            value <= 0 disables that branch (touching-only re-grouping).
        touching_dist_m: Distance (meters) for the touching, same-bin
            branch-drift check - see phase 2's own docstring in
            ``_assign_external_street_ids_for_pass``.
        num_node_directions: How many equal-width bins to split each
            node_id's own [0, 180) direction range into - see phase 2's
            own docstring.
        pinned_eids: Edge ids moved by a single-edge/single-street_id
            merge mechanism (coverage-based donation, the general
            cross-street_id 90% pass, or length-based reassignment - the
            same set tracked as ``never_main_eids``). These edges keep
            their own already-assigned street_id unconditionally and are
            EXCLUDED from the union-find regrouping below entirely - a
            coverage-matched edge is "the same street" for identity
            purposes on its own account, but must never become a BRIDGE
            that lets some OTHER, unrelated edge merge into that
            street_id just because it happens to sit at the same node -
            these mechanisms' own effect must stay confined to the one
            edge (or one street_id) they were actually applied to.
        next_id: The next unused street_id to hand out.

    Returns:
        A tuple ``(street_id_arr, next_id)``: the updated array (a copy)
        and the next unused street_id after handing out any needed here.
    """
    out = street_id_arr.copy()

    by_sid: dict[int, list[int]] = defaultdict(list)
    for i in range(len(eids)):
        if out[i] != -1:
            by_sid[int(out[i])].append(i)

    has_parallel_phase = bool(parallel_dist_m and parallel_dist_m > 0)

    for sid, all_members in by_sid.items():
        # Pinned edges (see ``pinned_eids`` above) always keep ``sid``
        # unconditionally and never participate in the regrouping below,
        # so they can never bridge some other, unrelated edge into this
        # street_id just by sitting at the same node.
        members = [i for i in all_members if eids[i] not in pinned_eids]
        for i in all_members:
            if eids[i] in pinned_eids:
                out[i] = sid
        if len(members) <= 1:
            continue

        parent = {i: i for i in members}

        def find(x: int, parent=parent) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int, parent=parent) -> None:
            ra, rb = find(a, parent), find(b, parent)
            if ra != rb:
                if rb < ra:
                    ra, rb = rb, ra
                parent[rb] = ra

        if has_parallel_phase and len(members) >= 2:
            # Fixed "branch anchor" bearing for this street_id's own
            # non-touching parallel-chain regrouping - mirrors
            # ``_assign_external_street_ids_for_pass``'s own
            # ``parallel_anchor_bearing``/``parallel_union``: a chain of
            # purely-pairwise-verified parallel links (A parallel to B,
            # B parallel to C) can still drift the branch as a whole away
            # from where it started, with nothing checking a new link
            # against the branch's own beginning - a real bug found via
            # real-data testing. Node-touching re-grouping below stays
            # purely pairwise (a long, gently curving street must still
            # be allowed to accumulate any total curve, one small,
            # locally-verified turn at a time).
            parallel_anchor_bearing = {i: float(bearing_arr[i]) for i in members}

            def parallel_union(a: int, b: int, parent=parent, anchor=parallel_anchor_bearing) -> None:
                ra, rb = find(a, parent), find(b, parent)
                if ra == rb:
                    return
                if _angle_diff(anchor[ra], anchor[rb]) > parallel_angle_threshold_deg:
                    return
                union(a, b, parent)

            member_arr = np.array(members)
            segs = geometries[member_arr]
            tree = shapely.STRtree(segs)
            left, right = tree.query(segs, predicate="dwithin", distance=parallel_dist_m)
            for a, b in zip(left, right):
                if a >= b:
                    continue
                i, j = int(member_arr[a]), int(member_arr[b])
                # Same two-part check as phase 1: a cheap bearing
                # pre-filter alongside the bounded-geometry check - on
                # its own, _edges_are_truly_parallel can pass for a
                # short PERPENDICULAR stub touching one point of a much
                # longer line (its near endpoint is at distance 0, its
                # far endpoint is within threshold as long as the
                # stub's own length is), which is exactly a branch, not
                # a genuine parallel pair - a real bug found via
                # real-data testing.
                if _angle_diff(bearing_arr[i], bearing_arr[j]) > parallel_angle_threshold_deg:
                    continue
                if _edges_are_truly_parallel(geometries[i], geometries[j], parallel_dist_m):
                    parallel_union(i, j)

        # Fixed N-direction bins, same technique as phase 2's own - see
        # ``_direction_bin_offset``/``_direction_bin_index``.
        node_members: dict = defaultdict(list)
        for i in members:
            node_members[cluster_u_arr[i]].append((i, float(start_angle[i]), True))
            node_members[cluster_v_arr[i]].append((i, float(end_angle[i]), False))
        for node_mem in node_members.values():
            if len(node_mem) <= 1:
                continue
            node_mem = sorted(node_mem)
            offset = _direction_bin_offset([angle for _i, angle, _at_start in node_mem], num_node_directions)
            bins: dict[int, list] = defaultdict(list)
            for i, angle, at_start in node_mem:
                bin_idx = _direction_bin_index(angle, offset, num_node_directions)
                bins[bin_idx].append((i, at_start))
            for bin_members in bins.values():
                for k in range(1, len(bin_members)):
                    i0, at_start0 = bin_members[0]
                    ik, at_startk = bin_members[k]
                    if find(i0, parent) == find(ik, parent):
                        continue
                    near0 = geometries[i0].coords[0] if at_start0 else geometries[i0].coords[-1]
                    neark = geometries[ik].coords[0] if at_startk else geometries[ik].coords[-1]
                    # Asymmetric by length - see the identical reasoning
                    # in _assign_external_street_ids_for_pass's own phase
                    # 2: checking a long edge's far point (possibly far
                    # away) against a short edge's line is ill-posed. Both
                    # "far" points are also each capped to at most
                    # touching_dist_m from the shared node (see
                    # ``_capped_far_point``) rather than using either
                    # edge's own raw opposite endpoint - see that phase's
                    # own docstring for why an uncapped far point spuriously
                    # rejects a real, gently curving street. A value <= 0
                    # disables the check entirely (matches every other
                    # 0-disables-it convention in this module) rather than
                    # rejecting every pair outright.
                    if touching_dist_m and touching_dist_m > 0:
                        far0 = _capped_far_point(geometries[i0], at_start0, touching_dist_m)
                        fark = _capped_far_point(geometries[ik], at_startk, touching_dist_m)
                        if geometries[i0].length <= geometries[ik].length:
                            if _point_to_line_distance(far0, neark, fark) > touching_dist_m:
                                continue
                        else:
                            if _point_to_line_distance(fark, near0, far0) > touching_dist_m:
                                continue
                    union(i0, ik)

        groups: dict[int, list[int]] = defaultdict(list)
        for i in members:
            groups[find(i)].append(i)
        if len(groups) <= 1:
            continue

        ordered_groups = sorted(groups.values(), key=lambda g: min(eids[k] for k in g))
        for i in ordered_groups[0]:
            out[i] = sid
        for group in ordered_groups[1:]:
            for i in group:
                out[i] = next_id
            next_id += 1

    return out, next_id


def _longest_path_in_component(
    comp_edges: list[tuple[int, int, int, float]],
) -> tuple[float, set[int]]:
    """This component's "main path": the longest *shortest* path between
    any two of its own nodes (the graph's weighted diameter, in the
    standard graph-theory sense - shortest-path distance between node
    pairs, not "longest simple path" - the two coincide on a tree/simple
    path, which is what this component normally is given the
    no-more-than-two-per-node invariant, but only the shortest-path
    definition stays unambiguous on the rare small cycle, e.g. a
    roundabout-derived street_id, where "longest simple path" is not
    even well-defined). Found via the standard two-pass Dijkstra/BFS
    "double sweep" technique: from an arbitrary start node, find the
    node A farthest from it by shortest-path distance; from A, find the
    node B farthest from *it*; the A->B shortest path is the diameter
    path. See "Main path detection" in report/street_identity.md
    for why this is the right practical choice.

    Args:
        comp_edges: This component's own edges, each as
            ``(eid, u, v, length)``.

    Returns:
        A tuple ``(total_length, path_eids)``.
    """
    if len(comp_edges) == 1:
        eid, _u, _v, length = comp_edges[0]
        return length, {eid}

    G = nx.MultiGraph()
    for eid, u, v, length in comp_edges:
        G.add_edge(u, v, key=eid, length=length)

    if G.number_of_nodes() == 1:
        # Every edge here is a self-loop on the same single node (no
        # real "path" to walk) - deterministically keep the single
        # longest edge, ties broken by smaller eid.
        eid, _u, _v, length = min(comp_edges, key=lambda e: (-e[3], e[0]))
        return length, {eid}

    def farthest(from_node):
        dist = nx.single_source_dijkstra_path_length(G, from_node, weight="length")
        # Deterministic tie-break: on equal max distance, the smaller
        # node id wins - a pure function of the nodes' own ids. Sorted
        # (not max-with-negation) so this works for any orderable node
        # id type (int or str), not just numeric ones.
        best_node, best_dist = min(
            dist.items(), key=lambda kv: (-kv[1], kv[0])
        )
        return best_node, best_dist

    any_node = next(iter(G.nodes))
    a, _ = farthest(any_node)
    b, total = farthest(a)

    if a == b:
        # Degenerate (a single node farthest from itself, e.g. every
        # edge a self-loop plus isolated node) - handled above already,
        # kept here only as a defensive fallback.
        eid, _u, _v, length = min(comp_edges, key=lambda e: (-e[3], e[0]))
        return length, {eid}

    path_nodes = nx.shortest_path(G, a, b, weight="length")
    path_eids = set()
    total_len = 0.0
    for n1, n2 in zip(path_nodes, path_nodes[1:]):
        edge_data = G.get_edge_data(n1, n2)
        # Parallel edges between the same node pair: pick the one whose
        # length matches the shortest-path weight nx used, ties broken
        # by the smaller eid - deterministic, a pure function of the
        # edges' own ids/lengths.
        best_eid, best_len = min(
            ((key, data["length"]) for key, data in edge_data.items()),
            key=lambda kv: (kv[1], kv[0]),
        )
        path_eids.add(best_eid)
        total_len += best_len

    return total_len, path_eids


def _best_diameter_path(
    edges: list[tuple[int, int, int, float]],
) -> tuple[float, set[int]]:
    """The single longest-shortest-path (weighted diameter) across every
    connected component formed by ``edges`` alone, keeping only the one
    overall-longest component's path. Factored out of
    ``_compute_main_direction`` so it can be run twice per street_id -
    once over all of that street_id's own edges, once over only its
    ``is_drivable`` ones - see that function's docstring.

    Args:
        edges: Candidate edges, each as ``(eid, u, v, length)``. Must be
            non-empty.

    Returns:
        A tuple ``(total_length, path_eids)`` for the single longest
        component-diameter path found.
    """
    node_parent: dict[int, int] = {}

    def nfind(x: int, node_parent=node_parent) -> int:
        node_parent.setdefault(x, x)
        while node_parent[x] != x:
            node_parent[x] = node_parent[node_parent[x]]
            x = node_parent[x]
        return x

    for _eid, u, v, _length in edges:
        ru, rv = nfind(u), nfind(v)
        if ru != rv:
            node_parent[rv] = ru

    comps: dict[int, list[tuple[int, int, int, float]]] = defaultdict(list)
    for e in edges:
        comps[nfind(e[1])].append(e)

    best_total = None
    best_min_eid = None
    best_path_eids: set = set()
    for comp_edges in comps.values():
        total, path_eids = _longest_path_in_component(comp_edges)
        # Deterministic cross-component tie-break: greater total length
        # wins; a tie is broken by the smaller of the component's own
        # edge ids (a pure function of the edges' own ids, never of
        # dict/processing order).
        min_eid = min(path_eids)
        if (
            best_total is None
            or total > best_total
            or (total == best_total and min_eid < best_min_eid)
        ):
            best_total = total
            best_min_eid = min_eid
            best_path_eids = path_eids

    return best_total, best_path_eids


# A street_id's drivable-only main path is preferred over its all-edges
# one whenever it isn't more than this fraction shorter - see
# _compute_main_direction.
_MAIN_DIRECTION_DRIVABLE_TOLERANCE = 0.05


def _compute_main_direction(
    eids: np.ndarray,
    u_arr: np.ndarray,
    v_arr: np.ndarray,
    length_arr: np.ndarray,
    street_id_arr: np.ndarray,
    is_drivable_arr: np.ndarray,
    excluded_from_main: frozenset[int] = frozenset(),
) -> dict[int, bool]:
    """Rule 3: compute exactly one "main path" per final street_id,
    strictly after all street_id assignment (main pass, Rule 1's
    invariant pass, Rule 2's second pass) has finished. This REPLACES
    the old, narrower is_main_direction computation that used to happen
    inline during dual-carriageway detection.

    For each street_id, build the undirected multigraph of just that
    street_id's own member edges (nodes = endpoints that have at least
    one edge of this street_id, edges = the street's own edges, weighted
    by real length). The "main path" is this graph's weighted diameter:
    the longest *shortest* path between any two of its own nodes (not a
    "longest simple path" enumeration - see ``_longest_path_in_component``
    for why that distinction matters on a cycle). Handle a street_id
    whose own edges span more than one connected component (can happen
    after stripping/pruning) by computing the diameter path in *each*
    component and keeping only the single overall longest one across
    all of them.

    **Drivable edges are preferred for the main path.** A street_id can
    now contain a mix of drivable and non-drivable edges (a footway
    donated a drivable street's id - see "Donating a drivable street's
    id to nearby non-drivable furniture"). Numbering a street along a
    sidewalk instead of the road itself would be wrong even though both
    share the same identity, so the diameter is computed twice: once
    over the street_id's edges as a whole (``all_total``/``all_path``),
    and once over only its ``is_drivable`` edges
    (``drivable_total``/``drivable_path``, skipped if the street_id has
    no drivable edges at all - e.g. a genuinely pedestrian-only street).
    The drivable-only path is used whenever it isn't more than
    ``_MAIN_DIRECTION_DRIVABLE_TOLERANCE`` (5%) shorter than the
    all-edges one (``drivable_total >= all_total * (1 -
    _MAIN_DIRECTION_DRIVABLE_TOLERANCE)``) - close enough that the
    non-drivable detour wasn't adding meaningfully more length, so
    there's no reason to prefer it. If the drivable-only diameter is
    more than 5% shorter, the non-drivable edges must be bridging a real
    gap in the drivable network for this street_id (e.g. the drivable
    side is only mapped in disconnected fragments), and the all-edges
    path is kept instead rather than losing that much of the street's
    real length.

    The graph built here is keyed by each edge's own CLUSTERED node_id
    (see "Node clustering"), not its raw node id - several raw OSM nodes
    a few meters apart can be the same real intersection, and treating
    them as separate graph vertices could let the diameter search walk
    "through" the same real intersection twice without ever detecting it
    as a repeat, or miss a genuine shortest path that a raw-node graph
    would show as longer than it really is. Keying by node_id also
    means the resulting main path can never repeat the same node_id (a
    property of any real shortest path with non-negative weights) except
    at an INTERNAL (loop/connector) edge's own two endpoints, which
    share one node_id by definition (see "Node clustering") - that's
    expected, not a repeat in the path-walking sense, since such an edge
    can never actually appear on the diameter path between two distinct
    farthest node_ids in the first place (a self-loop never shortens a
    path between two different nodes). Because of that, every internal
    edge with a resolved street_id is instead forced onto the main path
    directly, bypassing the diameter search entirely - see the "internal"
    handling below.

    Args:
        eids: Undirected edge ids.
        u_arr: First endpoint's own CLUSTERED node_id per edge, aligned
            with ``eids``.
        v_arr: Second endpoint's own clustered node_id per edge, aligned
            with ``eids``.
        length_arr: Edge length per edge, aligned with ``eids``.
        street_id_arr: Final street_id per edge (``-1`` for none),
            aligned with ``eids``.
        is_drivable_arr: Boolean drivable flag per edge, aligned with
            ``eids``.
        excluded_from_main: Edge ids that must never be flagged
            ``is_main_direction=True``, no matter what the diameter
            computation would otherwise pick - specifically, every edge
            "Redistributing too-short streets" has moved into a different
            street_id. Such an edge was absorbed purely for identity, not
            because it's really part of the target's own through-line, so
            it must never become (part of) that street's numbered main
            path just because it happens to extend the diameter -
            excluded from the diameter search entirely (both the
            all-edges and drivable-only candidate sets below), not merely
            deprioritized. A street_id whose every single edge is
            excluded (only possible in a degenerate case) falls back to
            considering all of its edges anyway, since some path must be
            chosen.

    Returns:
        A tuple ``(is_main, main_length)``: ``is_main`` maps every edge id
        that has a street_id to whether it lies on that street_id's single
        main (longest) path; ``main_length`` maps every street_id to that
        chosen main path's own total (real) length - the same value used
        by "Redistributing too-short streets" in report/street_identity.md
        to decide whether a street_id's main path is too short.
    """
    by_sid: dict[int, list[tuple[int, int, int, float, bool]]] = defaultdict(list)
    for eid, u, v, length, sid, is_drivable in zip(
        eids, u_arr, v_arr, length_arr, street_id_arr, is_drivable_arr
    ):
        if sid == -1:
            continue
        by_sid[int(sid)].append((eid, u, v, float(length), bool(is_drivable)))

    is_main: dict[int, bool] = {}
    main_length: dict[int, float] = {}
    for sid, edges_with_drivable in by_sid.items():
        for eid, _u, _v, _length, _is_drivable in edges_with_drivable:
            is_main[eid] = False

        # INTERNAL edges (u == v at the clustered-node level - see this
        # function's own docstring on "Node clustering") are a self-loop
        # in the diameter graph below, and a self-loop can never lie on a
        # shortest path between two DIFFERENT nodes. They no longer
        # participate in main-path computation AT ALL - not added to the
        # diameter graph, never forced main, never contributing length
        # here. A REAL internal edge (one that existed in the original
        # graph) is handled entirely by ``_assign_internal_connector_edges``
        # for street_id purposes and is simply never main. The physical
        # distance a real internal edge used to contribute to this
        # street's own main-path length is now instead carried by a NEW
        # synthetic straight connector edge, built by the caller
        # (``label_streets``) after this function returns, once the
        # external-only main path here is final - see "Synthetic main-
        # path connectors at a real intersection" in
        # report/street_identity.md.
        internal_length_total = 0.0

        non_internal = [e for e in edges_with_drivable if e[1] != e[2]]

        # Never consider a redistributed-in edge for the main path - see
        # "excluded_from_main" above. Falls back to the full (non-
        # internal) set only if excluding them would leave nothing to
        # compute a path from at all (every one of this street_id's own
        # non-internal edges was redistributed in - degenerate, but a
        # path still has to come from somewhere).
        eligible_with_drivable = [
            e for e in non_internal if e[0] not in excluded_from_main
        ] or non_internal

        if not eligible_with_drivable:
            # Every one of this street_id's edges was internal (a
            # degenerate street_id made up entirely of connector edges at
            # a single node_id) - nothing left to run a diameter search
            # over at all.
            main_length[sid] = internal_length_total
            continue

        if len(eligible_with_drivable) == 1:
            eid, _u, _v, length, _is_drivable = eligible_with_drivable[0]
            is_main[eid] = True
            main_length[sid] = length + internal_length_total
            continue

        edges = [(eid, u, v, length) for eid, u, v, length, _is_drivable in eligible_with_drivable]
        all_total, all_path = _best_diameter_path(edges)

        drivable_edges = [
            (eid, u, v, length)
            for eid, u, v, length, is_drivable in eligible_with_drivable
            if is_drivable
        ]
        chosen_path, chosen_total = all_path, all_total
        if drivable_edges:
            drivable_total, drivable_path = _best_diameter_path(drivable_edges)
            if drivable_total >= all_total * (1 - _MAIN_DIRECTION_DRIVABLE_TOLERANCE):
                chosen_path, chosen_total = drivable_path, drivable_total

        for eid in chosen_path:
            is_main[eid] = True
        main_length[sid] = chosen_total + internal_length_total

    return is_main, main_length


def _raw_turn_deviation_deg(a_deg: float, b_deg: float) -> float:
    """Deviation from continuing straight ahead between two DIRECTED
    (mod-360, not folded) travel bearings, folded into [0, 180].

    0 means ``b_deg`` continues exactly the same heading as ``a_deg`` (a
    straight line); 180 means it fully reverses it (a U-turn). Used to
    detect a sharp turn where a street_id's own main path meets itself at
    a shared node - see "Split a street_id at a sharp turn on its own
    main path" in report/street_identity.md.

    Args:
        a_deg: Arrival heading (deg, 0-360) into the shared node.
        b_deg: Departure heading (deg, 0-360) out of the shared node.

    Returns:
        The folded deviation, in [0, 180].
    """
    diff = abs(a_deg - b_deg) % 360
    return min(diff, 360 - diff)


def _ordered_main_path(
    main_positions: list[int],
    cluster_u_arr: np.ndarray,
    cluster_v_arr: np.ndarray,
) -> tuple[list[int], list[int]] | None:
    """Reconstruct a street_id's own main-path EXTERNAL edges (cluster_u
    != cluster_v) in walking order, as an alternating node/edge sequence,
    for the turn-splitting pass (``_split_main_path_at_turns``).

    Given the invariants this module already enforces on a street_id's
    own main path (no branching, connected), the main path's external
    edges form a single simple path (or, rarely, a closed loop) over
    node_ids. Reconstructed by walking from a degree-1 node (a genuine
    path end); if none exists (a closed loop, e.g. a roundabout-shaped
    main path), the walk instead starts at one arbitrary edge's own "u"
    endpoint - "the" interior node set for a closed loop isn't
    well-defined, so this is a deliberate, documented choice rather than
    an attempt to define one.

    Args:
        main_positions: Positions (indices into the caller's own arrays)
            of this street_id's own main-path edges, EXTERNAL ones only
            (internal/self-loop edges are handled separately by the
            caller).
        cluster_u_arr, cluster_v_arr: Each edge's own clustered node_id
            per endpoint, aligned the same way as ``main_positions``.

    Returns:
        A tuple ``(node_seq, edge_seq)`` - ``node_seq`` has one more
        entry than ``edge_seq``, with ``edge_seq[k]`` connecting
        ``node_seq[k]`` to ``node_seq[k + 1]`` - or ``None`` if
        ``main_positions`` is empty.
    """
    if not main_positions:
        return None
    if len(main_positions) == 1:
        i = main_positions[0]
        return [cluster_u_arr[i], cluster_v_arr[i]], [i]

    node_to_positions: dict[int, list[int]] = defaultdict(list)
    for i in main_positions:
        node_to_positions[cluster_u_arr[i]].append(i)
        node_to_positions[cluster_v_arr[i]].append(i)

    start_node = None
    for node, positions in node_to_positions.items():
        if len(positions) == 1:
            start_node = node
            break
    if start_node is None:
        # Closed loop - start at an arbitrary edge's own "u" endpoint.
        start_node = cluster_u_arr[main_positions[0]]

    visited_edges: set[int] = set()
    node_seq = [start_node]
    edge_seq: list[int] = []
    current = start_node
    while len(visited_edges) < len(main_positions):
        candidates = [i for i in node_to_positions[current] if i not in visited_edges]
        if not candidates:
            break
        i = candidates[0]
        visited_edges.add(i)
        other = cluster_v_arr[i] if cluster_u_arr[i] == current else cluster_u_arr[i]
        edge_seq.append(i)
        node_seq.append(other)
        current = other

    return node_seq, edge_seq


def _compute_nearest_main_eid(
    eids: np.ndarray,
    street_id_arr: np.ndarray,
    is_main_arr: np.ndarray,
    geometries: np.ndarray,
) -> dict:
    """For every non-main-path edge that has a street_id, its own
    street_id's own nearest main-path edge id (spatially, by centroid
    distance) - the ``nearest_main_eid`` column ``label_streets`` adds to
    its own returned ``edges_gdf``.

    Computed once, BEFORE turn-splitting (``_split_main_path_at_turns``)
    can reassign some main-path edges to a new street_id - splitting then
    cascades to every non-main edge whose own ``nearest_main_eid`` moved
    with it, so a parallel duplicate/spur/unresolved connector follows
    the specific piece of its street it actually sits next to, not
    arbitrarily whichever piece happens to keep the original street_id.

    Args:
        eids: Undirected edge ids.
        street_id_arr: Street_id per edge (``-1`` for none), aligned with
            ``eids``.
        is_main_arr: Whether each edge is on its own street_id's main
            path, aligned with ``eids``.
        geometries: Each edge's own geometry, aligned with ``eids``.

    Returns:
        A dict mapping every non-main edge id (that has a street_id) to
        its own street_id's nearest main-path edge id. A street_id with
        no main-path edges at all (degenerate) contributes nothing for
        its own members.
    """
    main_by_sid: dict[int, list[tuple[int, object]]] = defaultdict(list)
    for eid, sid, is_main, geom in zip(eids, street_id_arr, is_main_arr, geometries):
        if sid == -1 or not is_main:
            continue
        main_by_sid[int(sid)].append((eid, geom.centroid))

    nearest_main_eid: dict = {}
    for eid, sid, is_main, geom in zip(eids, street_id_arr, is_main_arr, geometries):
        if sid == -1 or is_main:
            continue
        candidates = main_by_sid.get(int(sid))
        if not candidates:
            continue
        own_centroid = geom.centroid
        best_eid, _best_dist = min(candidates, key=lambda c: (own_centroid.distance(c[1]), c[0]))
        nearest_main_eid[eid] = best_eid

    return nearest_main_eid


# Convention for allocating a unique synthetic-connector edge id/index
# tuple: this module already allocates fresh STREET ids from a running
# `next_id` counter (see e.g. `_split_main_path_at_turns`), but there is
# no equivalent running counter for EDGE ids, since `eids`/`edges_gdf`
# are keyed by the osmnx-style `(u, v, key)` tuple, not a flat integer -
# a synthetic connector's own `u`/`v` are real raw osmids (the specific
# two nodes it bridges), so only `key` needs to be invented. Offset far
# above any real osmnx `key` (which starts at 0 and is normally a single
# digit - one per parallel/duplicate way between the same two raw nodes)
# and incremented once per synthetic connector actually created, so
# every synthetic connector's own `(u, v, key)` tuple is guaranteed
# unique and trivially distinguishable from a real OSM edge by its `key`
# alone.
_SYNTHETIC_CONNECTOR_KEY_OFFSET = 900_000_000


def _reorient_main_path_edges(
    eids: np.ndarray,
    cluster_u_arr: np.ndarray,
    cluster_v_arr: np.ndarray,
    u_arr: np.ndarray,
    v_arr: np.ndarray,
    geometries: np.ndarray,
    street_id_arr: np.ndarray,
    is_main_arr: np.ndarray,
) -> np.ndarray:
    """Physically reorient every EXTERNAL main-path edge whose own stored
    direction runs backwards relative to its street_id's own main-path
    walking order - see "Physical edge reorientation" in
    report/street_identity.md.

    For each street_id, reconstructs the ordered walk over its own
    external main-path edges (``_ordered_main_path``, the same
    reconstruction ``_split_main_path_at_turns`` uses). Whenever walking
    from ``node_seq[k]`` to ``node_seq[k + 1]`` needs an edge whose own
    ``cluster_u_arr`` is actually ``node_seq[k + 1]`` (i.e. it's stored
    "the other way" relative to the walk), that edge's own ``u_arr``/
    ``v_arr`` (raw osmid), ``cluster_u_arr``/``cluster_v_arr``, and
    ``geometries`` (coordinate order) are swapped together, in place, so
    every array used downstream (synthetic-connector construction,
    turn-splitting, and the final persisted edges_gdf columns) sees this
    edge running the same direction the main path actually walks it.

    Args:
        eids: Undirected edge ids.
        cluster_u_arr, cluster_v_arr: Each edge's own clustered node_id
            per endpoint - mutated in place for reoriented edges.
        u_arr, v_arr: Each edge's own raw osmid per endpoint - mutated in
            place for reoriented edges.
        geometries: Each edge's own line geometry - mutated in place
            (coordinate order reversed) for reoriented edges.
        street_id_arr: Street_id per edge (``-1`` for none).
        is_main_arr: Whether each edge is on its own street_id's main
            path.

    Returns:
        A boolean array, aligned with ``eids``, ``True`` for every edge
        that was flipped.
    """
    reoriented = np.zeros(len(eids), dtype=bool)

    by_sid: dict[int, list[int]] = defaultdict(list)
    for i, (sid, is_main) in enumerate(zip(street_id_arr, is_main_arr)):
        if sid == -1 or not is_main:
            continue
        if cluster_u_arr[i] == cluster_v_arr[i]:
            continue  # a real internal edge is never main - defensive only
        by_sid[int(sid)].append(i)

    for _sid, main_positions in by_sid.items():
        if len(main_positions) < 2:
            continue
        reconstructed = _ordered_main_path(main_positions, cluster_u_arr, cluster_v_arr)
        if reconstructed is None:
            continue
        node_seq, edge_seq = reconstructed
        if len(edge_seq) != len(main_positions):
            continue
        for k, i in enumerate(edge_seq):
            if cluster_u_arr[i] == node_seq[k]:
                continue  # already forward
            # Stored backwards relative to the walk - flip everything
            # about this edge together.
            u_arr[i], v_arr[i] = v_arr[i], u_arr[i]
            cluster_u_arr[i], cluster_v_arr[i] = cluster_v_arr[i], cluster_u_arr[i]
            geometries[i] = shapely.LineString(list(geometries[i].coords)[::-1])
            reoriented[i] = True

    return reoriented


def _synthesize_main_path_connectors(
    eids: np.ndarray,
    cluster_u_arr: np.ndarray,
    cluster_v_arr: np.ndarray,
    u_arr: np.ndarray,
    v_arr: np.ndarray,
    is_main_arr: np.ndarray,
    street_id_arr: np.ndarray,
    node_geometry,
) -> list[dict]:
    """Build the NEW synthetic straight-line internal connector edges a
    street_id's own (external-only) main path needs at every real
    intersection (node_id) it passes through with more than one distinct
    raw osmid - see "Synthetic main-path connectors at a real
    intersection" in report/street_identity.md.

    For each street_id with at least two main-path (external) edges,
    reconstructs the ordered walk (``_ordered_main_path``) and, at every
    INTERIOR node_id of that walk, looks at the specific raw osmid the
    incoming edge actually touches there and the specific raw osmid the
    outgoing edge actually touches there (after ``_reorient_main_path_edges``
    has already made every edge's own ``u_arr``/``v_arr`` agree with the
    walk direction, this is simply the incoming edge's own ``v_arr`` and
    the outgoing edge's own ``u_arr``, but both endpoints are still
    checked defensively against ``cluster_u_arr``/``cluster_v_arr``
    rather than assumed). If the two osmids are the same raw node, the
    path already connects directly with no gap and no connector is
    created. Otherwise a brand-new straight ``LineString`` edge between
    those two specific osmids is synthesized.

    Args:
        eids: Undirected edge ids.
        cluster_u_arr, cluster_v_arr: Each edge's own clustered node_id
            per endpoint.
        u_arr, v_arr: Each edge's own raw osmid per endpoint.
        is_main_arr: Whether each edge is on its own street_id's main
            path.
        street_id_arr: Street_id per edge (``-1`` for none).
        node_geometry: Raw osmid -> its own point geometry (a
            ``GeoSeries``/mapping supporting ``[]``), for building each
            connector's own straight line.

    Returns:
        A list of dicts, one per synthesized connector, each with keys
        ``u``, ``v`` (raw osmids), ``street_id``, ``geometry``, ``length``.
    """
    by_sid: dict[int, list[int]] = defaultdict(list)
    for i, (sid, is_main) in enumerate(zip(street_id_arr, is_main_arr)):
        if sid == -1 or not is_main:
            continue
        if cluster_u_arr[i] == cluster_v_arr[i]:
            continue
        by_sid[int(sid)].append(i)

    connectors: list[dict] = []
    for sid, external in by_sid.items():
        if len(external) < 2:
            continue
        reconstructed = _ordered_main_path(external, cluster_u_arr, cluster_v_arr)
        if reconstructed is None:
            continue
        node_seq, edge_seq = reconstructed
        if len(edge_seq) != len(external):
            continue

        for k in range(len(edge_seq) - 1):
            prev_i, next_i = edge_seq[k], edge_seq[k + 1]
            node = node_seq[k + 1]
            in_osmid = v_arr[prev_i] if cluster_v_arr[prev_i] == node else u_arr[prev_i]
            out_osmid = u_arr[next_i] if cluster_u_arr[next_i] == node else v_arr[next_i]
            if in_osmid == out_osmid:
                continue
            p0 = node_geometry[in_osmid]
            p1 = node_geometry[out_osmid]
            geom = shapely.LineString([p0.coords[0], p1.coords[0]])
            connectors.append(
                {
                    "u": in_osmid,
                    "v": out_osmid,
                    "street_id": sid,
                    "geometry": geom,
                    "length": p0.distance(p1),
                }
            )
    return connectors


def _validate_main_path_invariant(
    eids: np.ndarray,
    u_arr: np.ndarray,
    v_arr: np.ndarray,
    cluster_u_arr: np.ndarray,
    cluster_v_arr: np.ndarray,
    street_id_arr: np.ndarray,
    is_main_arr: np.ndarray,
) -> None:
    """Hard internal-error check: every final street_id's own main path
    (external main edges plus their own synthesized connectors, post
    turn-split) must form exactly one connected, simple path - no osmid
    ever touched twice, no edge id repeated, and no node_id repeated
    except at a synthetic connector's own two endpoints (which by
    definition share one node_id but two different osmids). See "Full
    main-path connectivity validation" in report/street_identity.md.

    This should ALWAYS hold given a correct implementation of
    ``_reorient_main_path_edges``/``_synthesize_main_path_connectors``/
    ``_split_main_path_at_turns`` - a violation here means one of those
    has a real bug, so it's raised as a hard ``ValueError`` rather than
    silently producing invalid output.

    Args:
        eids: Undirected edge ids (including synthesized connectors).
        u_arr, v_arr: Each edge's own raw osmid per endpoint.
        cluster_u_arr, cluster_v_arr: Each edge's own clustered node_id
            per endpoint.
        street_id_arr: Final street_id per edge (``-1`` for none).
        is_main_arr: Whether each edge is on its own street_id's main
            path.

    Raises:
        ValueError: if any final street_id's own main-path edges don't
            form exactly one simple connected path.
    """
    by_sid: dict[int, list[int]] = defaultdict(list)
    for i, (sid, is_main) in enumerate(zip(street_id_arr, is_main_arr)):
        if sid != -1 and is_main:
            by_sid[int(sid)].append(i)

    for sid, members in by_sid.items():
        if len(members) <= 1:
            continue
        osmid_count: dict = defaultdict(int)
        for i in members:
            osmid_count[u_arr[i]] += 1
            osmid_count[v_arr[i]] += 1
        repeated = [o for o, c in osmid_count.items() if c > 2]
        if repeated:
            raise ValueError(
                f"Main-path invariant violated for street_id {sid}: osmid(s) "
                f"{repeated} touched more than twice by its own main-path edges "
                f"(eids {[eids[i] for i in members]}) - every osmid may only be "
                "touched once by the path, or twice only if it's a synthetic "
                "connector's own shared-node_id endpoint pair."
            )
        # Every osmid touched exactly twice must be touched by exactly two
        # DIFFERENT edges that also share one clustered node_id (the
        # synthetic-connector case) - an osmid touched twice by the SAME
        # edge (a degenerate zero-length loop) is also a bug.
        node_pair_adjacency: dict = defaultdict(set)
        for i in members:
            if u_arr[i] == v_arr[i]:
                raise ValueError(
                    f"Main-path invariant violated for street_id {sid}: edge "
                    f"{eids[i]} has identical osmid endpoints ({u_arr[i]})."
                )
            node_pair_adjacency[cluster_u_arr[i]].add(i)
            node_pair_adjacency[cluster_v_arr[i]].add(i)

        # Connectivity + simple-path shape: build the node_id-level
        # adjacency and confirm exactly one component with at most two
        # degree-1 node_ids (a proper path) or a closed loop (every
        # node_id degree 2) - mirrors what _ordered_main_path already
        # assumes is true; here it's actually verified.
        degree = {node: len(edges) for node, edges in node_pair_adjacency.items()}
        visited: set = set()
        stack = [next(iter(node_pair_adjacency))]
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            for i in node_pair_adjacency[n]:
                for other in (cluster_u_arr[i], cluster_v_arr[i]):
                    if other not in visited:
                        stack.append(other)
        if visited != set(node_pair_adjacency.keys()):
            raise ValueError(
                f"Main-path invariant violated for street_id {sid}: its own "
                "main-path edges do not form a single connected path."
            )


def _split_main_path_at_turns(
    eids: np.ndarray,
    cluster_u_arr: np.ndarray,
    cluster_v_arr: np.ndarray,
    raw_start_bearing: np.ndarray,
    raw_end_bearing: np.ndarray,
    street_id_arr: np.ndarray,
    is_main_arr: np.ndarray,
    nearest_main_eid: dict,
    main_path_angle_threshold_deg: float,
    next_id: int,
) -> tuple[np.ndarray, dict[int, int], int]:
    """Split a street_id into two (or more) distinct street_ids wherever
    its own main path makes a sharp turn - a turn exceeding
    ``main_path_angle_threshold_deg`` between two consecutive main-path edges means
    the "same street_id" is really two different physical through-lines
    for numbering purposes (e.g. a road that keeps its OSM name/rank
    across a near-right-angle corner). See "Split a street_id at a sharp
    turn on its own main path" in report/street_identity.md.

    Only the street_id's own EXTERNAL main-path edges are walked in path
    order (``_ordered_main_path``); an INTERNAL (self-loop) main-path
    edge (see "Node clustering") has no turn of its own to measure and is
    kept on whichever side of a split its own shared node_id ends up on
    (the side of the main-path edge immediately before it in walking
    order - an arbitrary but deterministic tie-break, since a connector
    sitting exactly at the split point has no more claim to one side
    than the other).

    Every non-main edge of the ORIGINAL street_id whose own
    ``nearest_main_eid`` (see ``_compute_nearest_main_eid``) landed on a
    main-path edge that moved to a new street_id here is moved along with
    it - the "furniture" (parallel duplicates, spurs, unresolved
    connectors) belonging with whichever piece of the split street it
    actually sits next to.

    Args:
        eids: Undirected edge ids.
        cluster_u_arr, cluster_v_arr: Each edge's own clustered node_id
            per endpoint, aligned with ``eids``.
        raw_start_bearing, raw_end_bearing: Each edge's own directed
            (mod 360, not folded) tangent bearing at its start/end point
            - "away from that endpoint, into the edge" (see
            ``_vectorized_endpoint_angles``), aligned with ``eids``.
        street_id_arr: Street_id per edge (``-1`` for none), aligned with
            ``eids``.
        is_main_arr: Whether each edge is on its own street_id's main
            path, aligned with ``eids``.
        nearest_main_eid: Every non-main edge's own nearest main-path
            edge id - see ``_compute_nearest_main_eid``.
        main_path_angle_threshold_deg: Turn deviation (deg, directed - see
            ``_raw_turn_deviation_deg``) beyond which a street_id is
            split at that node.
        next_id: The next unused street_id to hand out.

    Returns:
        A tuple ``(street_id_arr, split_from, next_id)``: the updated
        street_id array (a copy), a dict mapping every newly allocated
        street_id to the original street_id it was split off from (so
        ``streets_df`` can reuse that original's name/rank/highway), and
        the updated ``next_id``.
    """
    eid_to_pos = {eid: i for i, eid in enumerate(eids)}
    out = street_id_arr.copy()
    split_from: dict[int, int] = {}

    by_sid: dict[int, list[int]] = defaultdict(list)
    non_main_by_sid: dict[int, list[int]] = defaultdict(list)
    for i, (sid, is_main) in enumerate(zip(street_id_arr, is_main_arr)):
        if sid == -1:
            continue
        if is_main:
            by_sid[int(sid)].append(i)
        else:
            non_main_by_sid[int(sid)].append(i)

    for sid, main_positions in by_sid.items():
        # Every main-path position here is either a real external edge or
        # a synthetic straight connector (see
        # ``_synthesize_main_path_connectors``) - both are walkable path
        # members with their own real bearing, so both participate in the
        # ordered walk and in turn-angle measurement uniformly. A REAL
        # internal (self-loop) edge is never main anymore (see
        # ``_compute_main_direction``), so it never reaches here at all.
        external = list(main_positions)
        if len(external) < 2:
            continue

        reconstructed = _ordered_main_path(external, cluster_u_arr, cluster_v_arr)
        if reconstructed is None:
            continue
        node_seq, edge_seq = reconstructed
        if len(edge_seq) != len(external):
            # A branch or disconnection slipped through (shouldn't
            # happen given this module's own invariants) - skip
            # splitting this street_id rather than risk mis-walking it.
            continue

        split_indices: list[int] = []
        for k in range(len(edge_seq) - 1):
            prev_i, next_i = edge_seq[k], edge_seq[k + 1]
            node = node_seq[k + 1]
            arrival = (
                raw_end_bearing[prev_i]
                if cluster_v_arr[prev_i] == node
                else raw_start_bearing[prev_i]
            )
            arrival = (float(arrival) + 180.0) % 360.0
            departure = float(
                raw_start_bearing[next_i]
                if cluster_u_arr[next_i] == node
                else raw_end_bearing[next_i]
            )
            if _raw_turn_deviation_deg(arrival, departure) > main_path_angle_threshold_deg:
                split_indices.append(k + 1)

        if not split_indices:
            continue

        # Chunks of edge_seq's own positions (0-based, in walking
        # order), cut at each split point found above.
        chunks: list[list[int]] = []
        prev_cut = 0
        for cut in split_indices:
            chunks.append(edge_seq[prev_cut:cut])
            prev_cut = cut
        chunks.append(edge_seq[prev_cut:])

        # First chunk keeps the original street_id; every later chunk
        # gets a freshly allocated one.
        chunk_sid = [sid] + list(range(next_id, next_id + len(chunks) - 1))
        next_id += len(chunks) - 1

        pos_to_chunk: dict[int, int] = {}
        for chunk_idx, chunk in enumerate(chunks):
            for i in chunk:
                pos_to_chunk[i] = chunk_idx
                out[i] = chunk_sid[chunk_idx]
            if chunk_idx > 0:
                split_from[chunk_sid[chunk_idx]] = sid

        # Cascade: every non-main edge of this ORIGINAL street_id whose
        # own nearest main-path edge moved chunk follows it.
        for pos in non_main_by_sid.get(sid, ()):
            eid = eids[pos]
            ref_eid = nearest_main_eid.get(eid)
            if ref_eid is None:
                continue
            ref_pos = eid_to_pos.get(ref_eid)
            if ref_pos is None or ref_pos not in pos_to_chunk:
                continue
            out[pos] = chunk_sid[pos_to_chunk[ref_pos]]

    return out, split_from, next_id


def _edges_are_truly_parallel(geom_a: BaseGeometry, geom_b: BaseGeometry, threshold: float) -> bool:
    """Whether two edges run genuinely alongside each other, not merely
    sharing a similar direction/offset at one point that then drifts
    apart - either into a real branch (a fork sharing a node, then
    fanning out) or, just as importantly, into two unrelated edges that
    happen to sit on nearly the same line but run nowhere near each
    other in space (e.g. two residential streets on the same bearing on
    opposite sides of a city): true only if BOTH of the SHORTER edge's
    own endpoints are within ``threshold`` of the LONGER edge's own
    BOUNDED geometry (nearest point on its actual line, never an
    infinite extension of it).

    This is deliberately asymmetric by length, not "both edges' own
    endpoints close to the other's geometry": a short edge running
    alongside only part of a much longer one (e.g. a short tertiary
    stub next to one segment of a long primary avenue) is still a
    genuine parallel pair, even though the LONGER edge's own far
    endpoints are naturally nowhere near the shorter edge's own bounded
    geometry - only the shorter edge needs to fit entirely within the
    longer one's own real extent. Checking against the longer edge's
    BOUNDED geometry (not its infinite line) is what catches genuine
    drift: if the shorter edge's own far endpoint has wandered outside
    where the longer edge actually runs - whether because the two edges
    fan apart from a shared point, or because they were never near each
    other in the first place and only look aligned when the longer
    edge's own line is extended forever - the bounded nearest-point
    distance grows quickly past ``threshold``, exactly the way a plain
    Euclidean distance to the longer edge's own endpoint would. See
    "Parallel edges count as one direction" in report/street_identity.md.
    """
    if geom_a.length <= geom_b.length:
        short, long_ = geom_a, geom_b
    else:
        short, long_ = geom_b, geom_a
    s0 = shapely.Point(short.coords[0])
    s1 = shapely.Point(short.coords[-1])
    return s0.distance(long_) <= threshold and s1.distance(long_) <= threshold


def _assign_external_street_ids_for_pass(
    idx: np.ndarray,
    u_arr: np.ndarray,
    v_arr: np.ndarray,
    cluster_u_arr: np.ndarray,
    cluster_v_arr: np.ndarray,
    rank_arr: np.ndarray,
    name_arr: np.ndarray,
    bearing_arr: np.ndarray,
    geometries: np.ndarray,
    center_segments: np.ndarray,
    start_angle: np.ndarray,
    end_angle: np.ndarray,
    touching_dist_m: float,
    non_touching_dist_m: float,
    parallel_angle_threshold_deg: float,
    direction_tolerance_deg: float,
    name_similarity_threshold: float,
    num_node_directions: int,
    tangent_continuation_fraction: float = DEFAULT_TANGENT_CONTINUATION_FRACTION,
    tangent_continuation_window_m: float = DEFAULT_TANGENT_CONTINUATION_WINDOW_M,
) -> dict[int, int]:
    """Assign a street_id to every EXTERNAL edge (``cluster_u_arr[i] !=
    cluster_v_arr[i]``) in one pass (drivable or non-drivable), via one
    shared union-find over two combined signals:

    1. **Parallel grouping**: two edges are unioned when they're
       genuinely parallel over their whole length - see
       ``_edges_are_truly_parallel`` - within ``touching_dist_m`` if
       they share a raw node, the tighter ``non_touching_dist_m``
       otherwise. Catches the same street running alongside itself with
       no shared node at all (e.g. a divided road's two carriageways)
       or fragmenting at a busy intersection.
    2. **Per-node_id direction assignment**: at every node_id, this
       pass's own external edges touching it are grouped into direction
       "bins" (``direction_tolerance_deg``, tangent measured 10% into
       the edge from that node - the same convention used everywhere
       else in this module for "this edge's own direction at this
       node"). Only the TOP tier - edges whose own rank equals the best
       (lowest-numbered) rank present at this node_id - are unioned
       together within a bin; every edge with a strictly worse rank is
       left untouched by this node entirely (rule 2: a lower-rank
       street is cut by a higher-rank crossing, never merging with the
       dominant tier or even its own equal-rank continuation there - it
       ends its identity at this node_id and starts fresh past it).
       Every dominant-rank member of a bin is unioned together
       regardless of how many raw edges that is - a genuine parallel-
       duplicate pair sharing this node_id (e.g. a dual carriageway)
       counts as ONE direction, not two, since they already share both
       this literal node and this node_id's own single rank-appropriate
       direction bin. See "Parallel edges count as one direction" in
       report/street_identity.md.

    **No drift**: every group has a fixed "anchor" bearing - the center
    bearing of whichever edge became that group's root - set once and
    never updated as more edges join. Every subsequent union (from
    either signal above) is checked against the TWO groups' own anchors,
    not the two specific edges being compared - so a chain of locally-
    similar-direction edges can never silently accumulate into a group
    that, end to end, runs a completely different direction than where
    it started. This is what keeps the direction-based grouping from
    ever producing an incoherent, wildly-branching street_id no matter
    how many edges or nodes it passes through.

    **Name-gated throughout**: every union above is also rejected if the
    two groups being joined each already have at least one real OSM name
    and those names don't fuzzy-match (``name_matching.names_match``) -
    two differently-named streets are never merged into one street_id,
    regardless of how parallel or well-aligned they are.

    Args:
        idx: Row indices (into the full per-undirected-edge arrays) for
            this pass's own edges - already restricted to drivable-only
            or non-drivable-only, private/parking excluded.
        u_arr, v_arr: Each edge's own RAW node id per endpoint, aligned
            with the full arrays (indexed via ``idx``).
        cluster_u_arr, cluster_v_arr: Each edge's own node_id (clustered
            node - see "Node clustering") per endpoint.
        rank_arr: Highway rank per edge.
        name_arr: Clean name (or ``None``) per edge.
        bearing_arr: Each edge's own center bearing (``_overall_bearing_deg``).
        geometries: Each edge's own line geometry.
        center_segments: Each edge's own central segment (``_center_segment``).
        start_angle, end_angle: Each edge's own folded tangent angle at
            its ``u``/``v`` endpoint (10% in from that end).
        touching_dist_m, non_touching_dist_m: Parallel-grouping distance
            thresholds - see above. Either <= 0 disables that branch.
        parallel_angle_threshold_deg: Coarse pre-filter bearing tolerance,
            applied before the more expensive ``_edges_are_truly_parallel``
            check in phase 1.
        direction_tolerance_deg: Used as the union's own bearing-check
            tolerance (see ``union``) and as the branch-drift point-to-
            line distance's own gate value passed to ``touching_dist_m``
            below - the bin WIDTH itself comes from
            ``num_node_directions``, not this value.
        name_similarity_threshold: Minimum ``name_matching.name_similarity``
            score (0-100) for two names to be considered the same street.
        num_node_directions: How many equal-width bins to split each
            node_id's own [0, 180) direction range into (default 6, i.e.
            30 deg each - 12 bins once the opposite/continuation
            direction is counted too) - see phase 2 below.

    Returns:
        ``{i: group_root}`` for every external edge index ``i`` in
        ``idx`` - ``group_root`` is one of those same indices (the
        union-find's own representative), not yet renumbered into a
        final street_id.
    """
    external_idx = sorted(i for i in idx if cluster_u_arr[i] != cluster_v_arr[i])
    if not external_idx:
        return {}

    parent = {i: i for i in external_idx}
    names: dict = {i: ({name_arr[i]} if name_arr[i] is not None else set()) for i in external_idx}
    # Fixed "branch anchor" bearing - the center bearing of whichever
    # edge became a PARALLEL-chain group's own root, set once and never
    # updated. Consulted ONLY by phase 1's own non-touching/touching
    # parallel-grouping merges below (via ``parallel_union``), never by
    # phase 2's per-node_id direction assignment (which stays purely
    # pairwise - see ``union`` - so a long, gently curving real street
    # can still accumulate however much total curve it needs to, one
    # small, always-locally-verified turn at a time). Parallel grouping
    # is a fundamentally different kind of connection: it claims two
    # edges that DON'T share a node are the same physical street, so a
    # chain of purely-pairwise-verified parallel links could otherwise
    # silently drift a "branch" away from its own start with nothing
    # ever checking it against where that branch began - a real bug
    # found via real-data testing (two edges reported parallel to each
    # other individually, but the chain as a whole visibly drifting).
    parallel_anchor_bearing: dict = {i: float(bearing_arr[i]) for i in external_idx}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int, tol: float, angle_a: float | None = None, angle_b: float | None = None) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        # Pairwise: checked against the bearing of THIS specific pair -
        # the two edges actually being merged right now, not either
        # group's own original/first member. A long, gently curving
        # street is legitimately one street_id even if its overall
        # bearing changes a lot end to end, as long as every real
        # physical step along it stays within tolerance; what must never
        # happen is a single pair whose own two bearings are already too
        # far apart getting merged just because each of them separately
        # made it into a big group some other way.
        #
        # angle_a/angle_b let a caller substitute a different angle for
        # this specific check than bearing_arr's own whole-edge value -
        # used ONLY by phase 2 below, and only once its own
        # _tangent_continuation_fraction check has separately confirmed
        # the longer edge genuinely keeps hugging the shorter edge's own
        # local tangent for most of its length (see that check's own
        # docstring for why whole-edge bearing alone isn't enough to
        # decide a real curve from a real fork). Every other caller
        # (phase 1's parallel grouping, and phase 2 whenever that check
        # doesn't pass) leaves these at their default and gets the
        # original whole-edge-bearing behavior, unchanged.
        bearing_a = bearing_arr[a] if angle_a is None else angle_a
        bearing_b = bearing_arr[b] if angle_b is None else angle_b
        if _angle_diff(bearing_a, bearing_b) > tol:
            return False
        na, nb = names[ra], names[rb]
        if na and nb and not any(
            name_matching.names_match(x, y, name_similarity_threshold) for x in na for y in nb
        ):
            return False
        if rb < ra:
            ra, rb = rb, ra
        parent[rb] = ra
        names[ra] = na | nb
        return True

    def parallel_union(a: int, b: int, tol: float) -> bool:
        """Same as ``union``, but ALSO checked against each side's own
        fixed branch anchor - see ``parallel_anchor_bearing`` above."""
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        if _angle_diff(parallel_anchor_bearing[ra], parallel_anchor_bearing[rb]) > tol:
            return False  # would drift this parallel chain away from its own start - refuse
        return union(a, b, tol)

    # --- Phase 1: parallel grouping ---
    max_dist = max(touching_dist_m or 0.0, non_touching_dist_m or 0.0)
    if max_dist > 0 and len(external_idx) >= 2:
        idx_arr = np.array(external_idx)
        segs = center_segments[idx_arr]
        tree = shapely.STRtree(segs)
        left, right = tree.query(segs, predicate="dwithin", distance=max_dist)
        for a, b in zip(left, right):
            if a >= b:
                continue
            i, j = int(idx_arr[a]), int(idx_arr[b])
            touching = u_arr[i] in (u_arr[j], v_arr[j]) or v_arr[i] in (u_arr[j], v_arr[j])
            threshold = touching_dist_m if touching else non_touching_dist_m
            if not threshold or threshold <= 0:
                continue
            # Cheap pre-filter (center bearing) before the more expensive
            # whole-length check below.
            if _angle_diff(bearing_arr[i], bearing_arr[j]) > parallel_angle_threshold_deg:
                continue
            if not _edges_are_truly_parallel(geometries[i], geometries[j], threshold):
                continue
            parallel_union(i, j, parallel_angle_threshold_deg)

    # --- Phase 2: per-node_id direction assignment (fixed N-direction bins) ---
    # ``at_start`` records which of this edge's own two endpoints is the
    # one AT this node_id - needed below to find the edge's own FAR
    # (other) endpoint, for the branch-drift check.
    #
    # Directions at each node_id are split into ``num_node_directions``
    # FIXED-width bins over [0, 180) (default 6, i.e. 30 deg each - 12
    # bins once the opposite/continuation direction is counted too),
    # rather than the previous leader-based dynamic binning - mirrors
    # report/reference_angle_algorithm.txt's own technique: the fixed
    # grid's own rotation is chosen so its boundaries fall in the middle
    # of the widest real gap between directions actually present at this
    # node (``_direction_bin_offset``), so a real cluster of nearby
    # directions is never split by an arbitrarily-placed boundary.
    node_members: dict = defaultdict(list)
    for i in external_idx:
        node_members[cluster_u_arr[i]].append((i, float(start_angle[i]), int(rank_arr[i]), True))
        node_members[cluster_v_arr[i]].append((i, float(end_angle[i]), int(rank_arr[i]), False))

    for members in node_members.values():
        if len(members) <= 1:
            continue
        best_rank = min(r for _i, _a, r, _at_start in members)
        members = sorted(members)  # deterministic order, by edge index first
        # Each edge's own LOCAL tangent angle at THIS node (start_angle/
        # end_angle, whichever endpoint touches this node_id) - used
        # below only as the override union() falls back to once
        # _tangent_continuation_fraction has confirmed a genuine curve,
        # not a fork (see union()'s own angle_a/angle_b docstring).
        angle_by_i = {i: angle for i, angle, _r, _at_start in members}
        offset = _direction_bin_offset(
            [angle for _i, angle, _r, _at_start in members], num_node_directions
        )
        bins: dict[int, list] = defaultdict(list)
        for i, angle, rank, at_start in members:
            bin_idx = _direction_bin_index(angle, offset, num_node_directions)
            bins[bin_idx].append((i, rank, at_start))
        for bin_members in bins.values():
            dominant = [(i, at_start) for i, r, at_start in bin_members if r == best_rank]
            # Name-match overrides rank (the single most important rule -
            # see report/street_identity.md "Name match overrides rank"):
            # any bin member whose own name fuzzy-matches EITHER a
            # dominant-rank member's name OR another non-dominant
            # member's name in this SAME bin (same node_id, same
            # direction) is treated as dominant too, even though its own
            # rank is strictly worse than the best rank present here. A
            # shared/matching name through a connected crossing is a
            # stronger signal of true street identity than rank - two
            # named streets of matching name must keep their own shared
            # identity through this crossing even when the crossing's own
            # single dominant-rank member happens to be unnamed (e.g. an
            # unnamed motorway), not only when the dominant-rank member
            # itself carries that name.
            dominant_idx = {i for i, _at_start in dominant}
            named_non_dominant = [
                (i, at_start) for i, r, at_start in bin_members
                if r != best_rank and name_arr[i] is not None
            ]
            all_named = [(i, at_start) for i, at_start in dominant if name_arr[i] is not None] + named_non_dominant
            name_group_parent = {i: i for i, _at_start in all_named}

            def _name_find(x: int, name_group_parent=name_group_parent) -> int:
                while name_group_parent[x] != x:
                    name_group_parent[x] = name_group_parent[name_group_parent[x]]
                    x = name_group_parent[x]
                return x

            for a in range(len(all_named)):
                for b in range(a + 1, len(all_named)):
                    ia, _ = all_named[a]
                    ib, _ = all_named[b]
                    if name_matching.names_match(name_arr[ia], name_arr[ib], name_similarity_threshold):
                        ra, rb = _name_find(ia), _name_find(ib)
                        if ra != rb:
                            name_group_parent[rb] = ra

            cluster_size: dict[int, int] = defaultdict(int)
            for i, _at_start in all_named:
                cluster_size[_name_find(i)] += 1
            for i, at_start in named_non_dominant:
                if cluster_size[_name_find(i)] >= 2:
                    dominant.append((i, at_start))
            # Every dominant-rank member of this bin shares this node_id's
            # single allowed direction here - including a genuine
            # parallel-duplicate pair (e.g. a dual carriageway), which
            # counts as ONE direction, not two, no matter how many raw
            # edges share this node_id.
            #
            # **Branch-drift check**: sharing this node AND falling in
            # the same direction bin isn't quite enough on its own - two
            # edges can meet at a shared point heading the same way and
            # then immediately curve apart, never actually running
            # alongside each other past that one point. Each pair is
            # checked with a point-to-line test: the FAR (non-shared)
            # endpoint of each edge must fall within ``touching_dist_m``
            # of the OTHER edge's own line (not a persisted, growing
            # group reference - a straight reference line accumulated
            # over many hops fails exactly the cases that must keep
            # merging, like a street gently curving all the way around a
            # closed loop, since the loop's own two "farthest" points end
            # up on a chord nowhere near the curve itself). Multi-hop
            # drift across a whole BRANCH - several individually-fine
            # local steps that still add up to something incoherent - is
            # instead caught afterward, over the FINAL settled
            # composition, by the branching invariant
            # (``_split_branching_street_ids``), which re-validates every
            # street_id's own final shape rather than trying to predict
            # it merge by merge.
            for k in range(1, len(dominant)):
                i0, at_start0 = dominant[0]
                ik, at_startk = dominant[k]
                r0, rk = find(i0), find(ik)
                if r0 == rk:
                    continue
                near0 = geometries[i0].coords[0] if at_start0 else geometries[i0].coords[-1]
                neark = geometries[ik].coords[0] if at_startk else geometries[ik].coords[-1]
                # Asymmetric by length, same principle as
                # _edges_are_truly_parallel: only the SHORTER edge's own
                # far point is checked, against the LONGER edge's line -
                # checking the reverse (a long edge's far point, possibly
                # hundreds of meters away, against a short edge's line)
                # is ill-posed regardless of method, since even a tiny
                # bend in the short edge gets amplified into a large
                # perpendicular distance once extrapolated that far - a
                # real bug found via real-data testing (two collinear
                # named edges of very different lengths failing to merge
                # this way).
                # Both "far" points are also each capped to at most
                # touching_dist_m from the shared node (see
                # ``_capped_far_point``) rather than using either edge's
                # own raw opposite endpoint - this check is only meant to
                # catch an IMMEDIATE fork right at the node, and using the
                # true far endpoint of a long-ish edge means even a
                # perfectly clean, gently curving real street (like a
                # rural road tracking terrain) drifts more than
                # touching_dist_m from its own near-node tangent line
                # over its full length, failing this check regardless of
                # how well its immediate tangent matches at the node - a
                # real bug found via real-data testing (two collinear,
                # unnamed, same-rank edges meeting cleanly at one node,
                # tangents ~1 deg apart, refused merging solely because
                # one of them ran ~500m and gently curved along the way).
                # A value <= 0 disables the check entirely (matches every
                # other 0-disables-it convention in this module) rather
                # than rejecting every pair outright.
                if touching_dist_m and touching_dist_m > 0:
                    far0 = _capped_far_point(geometries[i0], at_start0, touching_dist_m)
                    fark = _capped_far_point(geometries[ik], at_startk, touching_dist_m)
                    if geometries[i0].length <= geometries[ik].length:
                        if _point_to_line_distance(far0, neark, fark) > touching_dist_m:
                            continue
                    else:
                        if _point_to_line_distance(fark, near0, far0) > touching_dist_m:
                            continue
                # Local tangents already agree (same direction bin) and
                # the branch-drift check above already passed - normally
                # enough to merge. But if the two edges' own WHOLE-EDGE
                # bearings disagree by more than direction_tolerance_deg,
                # union() would otherwise still reject them; only rescue
                # that case when the longer edge genuinely keeps hugging
                # the shorter one's local tangent for MOST of its own
                # length (a real curve), not just right at the node
                # before forking off somewhere else for good - see
                # _tangent_continuation_fraction and
                # DEFAULT_TANGENT_CONTINUATION_FRACTION's own docstrings.
                if _angle_diff(bearing_arr[i0], bearing_arr[ik]) > direction_tolerance_deg:
                    if geometries[i0].length <= geometries[ik].length:
                        short_geom, short_at_start = geometries[i0], at_start0
                        long_geom, long_at_start = geometries[ik], at_startk
                    else:
                        short_geom, short_at_start = geometries[ik], at_startk
                        long_geom, long_at_start = geometries[i0], at_start0
                    corridor_width = touching_dist_m or 0.0
                    if corridor_width <= 0 or _tangent_continuation_fraction(
                        short_geom, short_at_start, long_geom, long_at_start,
                        corridor_width, tangent_continuation_window_m,
                    ) < tangent_continuation_fraction:
                        continue
                    union(i0, ik, direction_tolerance_deg, angle_by_i[i0], angle_by_i[ik])
                else:
                    union(i0, ik, direction_tolerance_deg)

    return {i: find(i) for i in external_idx}


def _best_coverage_match(
    geom: BaseGeometry,
    candidate_members: dict[int, list[int]],
    geometries: np.ndarray,
    threshold: float,
    coverage_fraction: float,
) -> int | None:
    """The "90% rule": whether at least ``coverage_fraction`` (0-1) of
    ``geom``'s own length falls within ``threshold`` of the COMBINED
    candidate network (every candidate street_id's own edges, all
    together), and if so, which single candidate street_id ``geom`` runs
    alongside for the largest share of its own length - not simply
    whichever candidate has the most total length of its own nearby (a
    real bug found via real-data testing: a long, mostly-irrelevant
    street that only brushes past a small corner of ``geom`` could
    otherwise outvote a much shorter street ``geom`` actually runs
    alongside for most of its length).

    Args:
        geom: The geometry being matched - a single edge, or a whole
            street_id's own combined geometry.
        candidate_members: Candidate street_id -> its own list of row
            indices, aligned with ``geometries``. Must not include
            ``geom``'s own street_id.
        geometries: Line geometry per edge.
        threshold: Maximum distance (meters) for "near".
        coverage_fraction: Minimum fraction (0-1) of ``geom``'s own
            length that must fall within ``threshold`` of the combined
            candidate network for a match to be returned at all.

    Returns:
        The best-matching candidate street_id, or ``None`` if the
        overall coverage threshold isn't met (or there are no
        candidates).
    """
    if geom.length <= 0 or not candidate_members or not threshold or threshold <= 0:
        return None
    candidate_buffers = _build_candidate_buffers(candidate_members, geometries, threshold)
    combined_buffer = shapely.union_all(list(candidate_buffers.values()))
    return _match_against_buffers(geom, combined_buffer, candidate_buffers, coverage_fraction)


def _build_candidate_buffers(
    candidate_members: dict[int, list[int]], geometries: np.ndarray, threshold: float
) -> dict[int, BaseGeometry]:
    """Precompute each candidate street_id's own buffered (by
    ``threshold``) combined geometry once - factored out of
    ``_best_coverage_match`` so a caller matching MANY geometries against
    the SAME candidate set (e.g. every edge of one street_id, one at a
    time) can build these once and reuse them, instead of redundantly
    re-unioning and re-buffering the same candidate geometries on every
    single call - a real performance bug found via real-data testing
    (this step alone made a whole real-world run take several minutes).
    """
    return {
        sid: shapely.union_all([geometries[j] for j in members]).buffer(threshold)
        for sid, members in candidate_members.items()
    }


def _match_against_buffers(
    geom: BaseGeometry,
    combined_buffer: BaseGeometry,
    candidate_buffers: dict[int, BaseGeometry],
    coverage_fraction: float,
) -> int | None:
    """The core of the 90% rule (see ``_best_coverage_match``), given
    already-built candidate buffers (``_build_candidate_buffers``)."""
    if geom.length <= 0:
        return None
    covered_length = geom.intersection(combined_buffer).length
    if covered_length / geom.length < coverage_fraction:
        return None

    best_sid = None
    best_covered = -1.0
    for sid, buf in candidate_buffers.items():
        covered = geom.intersection(buf).length
        key = (covered, -sid)
        if best_sid is None or key > (best_covered, -best_sid):
            best_covered, best_sid = covered, sid
    return best_sid


def _coverage_donate_non_drivable_edges(
    non_drivable_idx: np.ndarray,
    drivable_idx: np.ndarray,
    geometries: np.ndarray,
    bearing_arr: np.ndarray,
    name_arr: np.ndarray,
    street_id_arr: np.ndarray,
    threshold: float,
    coverage_fraction: float,
    parallel_angle_threshold_deg: float,
    name_similarity_threshold: float,
) -> set[int]:
    """Reassign a non-drivable edge the DRIVABLE street_id it most
    belongs to, when almost its entire own length already runs near the
    drivable network - a sidewalk/footway mapped alongside a road for
    (nearly) its whole length is that road's own street for addressing
    purposes, independent of whether it's genuinely "parallel" in the
    stricter ``_edges_are_truly_parallel`` sense the rest of this module
    otherwise requires (e.g. a footway that weaves slightly, or briefly
    steps away from the road it's still obviously attached to).

    Run AFTER the non-drivable pass's own
    ``_assign_external_street_ids_for_pass``/
    ``_assign_internal_connector_edges`` have already given every
    non-drivable edge its own (non-drivable) street_id - this step then
    CHANGES that street_id for any edge the 90% rule
    (``_best_coverage_match``) matches to a drivable one. Mutates
    ``street_id_arr`` in place.

    **Bearing-gated, per edge.** Coverage/proximity alone is NOT enough:
    a non-drivable edge is only even considered "near" a given drivable
    street_id if at least one of that street_id's own edges is BOTH
    within ``threshold`` AND within ``parallel_angle_threshold_deg`` of
    the non-drivable edge's own bearing. Without this, a whole tangle of
    unrelated footways/cycleways in, say, a plaza or park (every
    direction, not just the road's own) sitting within ``threshold`` of
    one edge of a big road could all get swept into that road's own
    street_id regardless of their own direction - a real bug found via
    real-data testing: widening this step's own distance threshold (to
    fix a different bug, a wide-road sidewalk offset beyond the tight
    bound) caused a 227-edge incoherent blob mixing a secondary road
    with an entire nearby path mesh running every which way, exactly the
    class of bug "no more than one direction per node_id" and the
    anchor/pairwise bearing checks elsewhere in this module exist to
    prevent.

    Args:
        non_drivable_idx: Row indices of the non-drivable pass's own
            eligible edges, already carrying a resolved (non-drivable)
            street_id.
        drivable_idx: Row indices of the drivable pass's own eligible
            edges, already carrying a resolved ``street_id``.
        geometries: Line geometry per edge.
        bearing_arr: Each edge's own center bearing
            (``_overall_bearing_deg``).
        street_id_arr: Current street_id per edge - read for drivable
            edges, read AND overwritten for qualifying non-drivable ones.
        threshold: Maximum distance (meters) for "near" - see
            ``_best_coverage_match``.
        coverage_fraction: Minimum fraction (0-1) of a non-drivable
            edge's own length that must fall within ``threshold`` of the
            drivable network for it to be donated a street_id this way.
        parallel_angle_threshold_deg: Maximum folded bearing difference
            (deg) between a non-drivable edge and a candidate drivable
            edge for that drivable edge's own street_id to even be
            considered a candidate at all.
        name_similarity_threshold: Minimum ``name_matching.name_similarity``
            score (0-100) for two names to be considered the same street.

    Returns:
        The set of non-drivable row indices that were reassigned - these
        can never be their new street_id's own main path (see
        "Main path" in report/street_identity.md).
    """
    reassigned: set[int] = set()
    if len(drivable_idx) == 0 or len(non_drivable_idx) == 0 or not threshold or threshold <= 0:
        return reassigned

    drivable_by_sid: dict[int, list[int]] = defaultdict(list)
    for j in drivable_idx:
        sid = int(street_id_arr[j])
        if sid != -1:
            drivable_by_sid[sid].append(int(j))
    if not drivable_by_sid:
        return reassigned

    # A named non-drivable edge (e.g. a footway with its own real OSM
    # name) is never donated to a drivable street_id whose own known
    # name(s) don't fuzzy-match it - proximity/coverage/bearing alone
    # otherwise silently erases a genuinely different, explicitly named
    # street's identity by folding it into a nearby road's street_id, a
    # real gap found via real-data testing (invariant: two edges with two
    # different non-empty names must never share a street_id).
    drivable_sid_names: dict[int, set] = defaultdict(set)
    for sid, members in drivable_by_sid.items():
        for j in members:
            if name_arr[j] is not None:
                drivable_sid_names[sid].add(name_arr[j])

    def name_compatible(own_name, sid: int) -> bool:
        if own_name is None:
            return True
        known = drivable_sid_names.get(sid)
        if not known:
            return True
        return any(name_matching.names_match(own_name, other, name_similarity_threshold) for other in known)

    # Each drivable street_id's own buffer is built ONCE here and reused
    # for every non-drivable edge below, instead of being rebuilt fresh
    # for every single one - a real performance bug found via real-data
    # testing (this alone made a whole real-world run take over a
    # minute: tens of thousands of redundant shapely buffer calls for a
    # street network with only a few hundred drivable street_ids).
    sid_buffers = _build_candidate_buffers(drivable_by_sid, geometries, threshold)

    drivable_geoms = geometries[drivable_idx]
    tree = shapely.STRtree(drivable_geoms)

    for i in non_drivable_idx:
        i = int(i)
        geom = geometries[i]
        if geom.length <= 0:
            continue
        own_name = name_arr[i]
        candidate_local = tree.query(geom, predicate="dwithin", distance=threshold)
        eligible_sids: set[int] = set()
        for local_j in candidate_local:
            j = int(drivable_idx[local_j])
            if _angle_diff(bearing_arr[i], bearing_arr[j]) > parallel_angle_threshold_deg:
                continue
            sid = int(street_id_arr[j])
            if sid != -1 and name_compatible(own_name, sid):
                eligible_sids.add(sid)
        if not eligible_sids:
            continue
        eligible_buffers = {sid: sid_buffers[sid] for sid in eligible_sids}
        combined_buffer = shapely.union_all(list(eligible_buffers.values()))
        best_sid = _match_against_buffers(geom, combined_buffer, eligible_buffers, coverage_fraction)
        if best_sid is not None:
            street_id_arr[i] = best_sid
            reassigned.add(i)
    return reassigned


def _coverage_reassign_overlapping_street_ids(
    valid_idx: np.ndarray,
    geometries: np.ndarray,
    bearing_arr: np.ndarray,
    name_arr: np.ndarray,
    rank_arr: np.ndarray,
    is_drivable_arr: np.ndarray,
    street_id_arr: np.ndarray,
    threshold: float,
    coverage_fraction: float,
    name_similarity_threshold: float,
    parallel_angle_threshold_deg: float,
) -> set[int]:
    """The 90% rule, generalized to every resolved street_id against
    every OTHER one (drivable or non-drivable, either direction) - run
    once, after the two-pass assignment (and non-drivable-to-drivable
    coverage donation above) have both finished, before the connectivity
    invariant.

    A candidate other street_id is only even considered for a given
    street_id if at least one of the CANDIDATE's own edges is BOTH
    within ``threshold`` of, AND within ``parallel_angle_threshold_deg``
    bearing of, at least one of THIS street_id's own edges. Without this
    bearing gate, plain proximity/coverage alone could sweep an entire
    tangle of edges running every which way (a plaza's path mesh, a
    park) into one street_id just for being spatially close to it - a
    real bug found via real-data testing (this bearing gate is the
    generalized version of the identical fix applied to
    ``_coverage_donate_non_drivable_edges``).

    **Rank-gated when both sides have a drivable member.** If BOTH
    ``sid`` and a candidate ``other_sid`` have at least one drivable
    edge, their own DOMINANT rank (the best/lowest rank number among
    their own drivable edges) must match, mirroring rule 2 (only the
    dominant tier at an intersection may merge across it) - without
    this, coverage/bearing proximity alone could bridge two genuinely
    different real streets of different ranks (e.g. a primary avenue and
    a residential side street both radiating from the same complex
    junction, each locally bearing-compatible with some part of the
    other) into one `street_id`, which rule 2 would never allow through
    the ordinary per-node_id assignment. A real bug found via real-data
    testing: a real avenue's own `street_id` swallowed a whole tangle of
    primary/residential/tertiary/busway edges from a nearby complex
    junction this way - 8.4km of combined edge length packed into a
    1.4km-wide area, clearly not one coherent street. Non-drivable
    street_ids (no drivable member at all on one or both sides) aren't
    rank-gated - furniture like footways/cycleways has no rank tier to
    compare.

    For each street_id whose own COMBINED geometry has at least
    ``coverage_fraction`` of its length within ``threshold`` of every
    OTHER bearing- and name-compatible street_id's own combined geometry
    (``_best_coverage_match`` on the whole street_id at once - two
    differently-named streets are never merged this way, exactly like
    every other merge mechanism in this module): EACH of that
    street_id's own edges is individually reassigned to whichever
    specific other street_id it best matches (per-edge
    ``_best_coverage_match``, not necessarily the same target the whole
    street_id's own check picked) - two edges of the same short,
    largely-redundant street_id can genuinely run alongside two
    different neighbors.

    Args:
        valid_idx: Row indices with a resolved street_id (``!= -1``) to
            consider - both drivable and non-drivable together.
        geometries: Line geometry per edge.
        bearing_arr: Each edge's own center bearing.
        name_arr: Clean name (or ``None``) per edge.
        rank_arr: Highway rank per edge.
        is_drivable_arr: Boolean drivable flag per edge.
        street_id_arr: Current street_id per edge - read and (for
            qualifying edges) overwritten in place.
        threshold: Maximum distance (meters) for "near".
        coverage_fraction: Minimum fraction (0-1) - see
            ``_best_coverage_match``.
        name_similarity_threshold: Minimum ``name_matching.name_similarity``
            score (0-100) for two names to be considered the same street.
        parallel_angle_threshold_deg: Maximum folded bearing difference
            (deg) between two edges for their own street_ids to even be
            considered candidates of each other at all.

    Returns:
        The set of row indices that were reassigned - these can never be
        their new street_id's own main path (see "Main path" in
        report/street_identity.md).
    """
    reassigned: set[int] = set()
    if not threshold or threshold <= 0:
        return reassigned

    by_sid: dict[int, list[int]] = defaultdict(list)
    for i in valid_idx:
        sid = int(street_id_arr[i])
        if sid != -1:
            by_sid[sid].append(int(i))
    if len(by_sid) < 2:
        return reassigned

    sid_geom = {sid: shapely.union_all([geometries[i] for i in members]) for sid, members in by_sid.items()}
    sid_names: dict[int, set] = defaultdict(set)
    for sid, members in by_sid.items():
        for i in members:
            if name_arr[i] is not None:
                sid_names[sid].add(name_arr[i])

    def name_compatible(sid_a: int, sid_b: int) -> bool:
        na, nb = sid_names[sid_a], sid_names[sid_b]
        if not na or not nb:
            return True
        return any(name_matching.names_match(x, y, name_similarity_threshold) for x in na for y in nb)

    # Each sid's own dominant (best/lowest-numbered) rank among its own
    # DRIVABLE edges, or None if it has no drivable edge at all.
    sid_dominant_rank: dict[int, int | None] = {}
    for sid, members in by_sid.items():
        drivable_ranks = [int(rank_arr[i]) for i in members if is_drivable_arr[i]]
        sid_dominant_rank[sid] = min(drivable_ranks) if drivable_ranks else None

    def rank_compatible(sid_a: int, sid_b: int) -> bool:
        ra, rb = sid_dominant_rank[sid_a], sid_dominant_rank[sid_b]
        if ra is None or rb is None:
            return True
        return ra == rb

    # An STRtree spatial pre-filter, so each street_id's own edges only
    # ever query for candidate edges that could plausibly be within
    # `threshold` - unioning/buffering literally every OTHER edge in the
    # whole graph for every single street_id (the naive implementation)
    # is prohibitively slow on a real network with many street_ids.
    all_idx = np.array(sorted(i for members in by_sid.values() for i in members))
    tree = shapely.STRtree(geometries[all_idx])
    sid_buffers_cache = _build_candidate_buffers(by_sid, geometries, threshold)

    for sid, members in list(by_sid.items()):
        # Bearing-gated per MEMBER edge, not per whole street_id's own
        # combined geometry - a proximity query against the whole
        # (possibly long, curving) geometry has no way to know which
        # part of it a given nearby edge is actually near, so it can't
        # be bearing-filtered at that level.
        eligible_sids: set[int] = set()
        for i in members:
            geom = geometries[i]
            if geom.length <= 0:
                continue
            candidate_local = tree.query(geom, predicate="dwithin", distance=threshold)
            for local_j in candidate_local:
                j = int(all_idx[local_j])
                other_sid = int(street_id_arr[j])
                if other_sid == sid:
                    continue
                if _angle_diff(bearing_arr[i], bearing_arr[j]) > parallel_angle_threshold_deg:
                    continue
                if not rank_compatible(sid, other_sid):
                    continue
                if not name_compatible(sid, other_sid):
                    continue
                eligible_sids.add(other_sid)
        if not eligible_sids:
            continue
        candidate_buffers = {s: sid_buffers_cache[s] for s in eligible_sids}
        combined_buffer = shapely.union_all(list(candidate_buffers.values()))
        whole_match = _match_against_buffers(
            sid_geom[sid], combined_buffer, candidate_buffers, coverage_fraction
        )
        if whole_match is None:
            continue
        for i in members:
            best_sid = _match_against_buffers(
                geometries[i], combined_buffer, candidate_buffers, coverage_fraction
            )
            if best_sid is not None:
                street_id_arr[i] = best_sid
                reassigned.add(i)
    return reassigned


def _assign_internal_connector_edges(
    idx: np.ndarray,
    u_arr: np.ndarray,
    v_arr: np.ndarray,
    cluster_u_arr: np.ndarray,
    cluster_v_arr: np.ndarray,
    geometries: np.ndarray,
    start_angle: np.ndarray,
    end_angle: np.ndarray,
    name_arr: np.ndarray,
    street_id_arr: np.ndarray,
    angle_threshold_deg: float,
    name_similarity_threshold: float,
) -> None:
    """Assign a street_id to every INTERNAL (loop/connector) edge in
    ``idx`` - one whose two endpoints cluster to the SAME node_id -
    strictly by direction, never by rank or touching-candidate ranking:
    every other edge in ``idx`` (the external ones) must already carry
    its own resolved ``street_id`` in ``street_id_arr`` before this
    runs. Mutates ``street_id_arr`` in place for the internal edges'
    own positions.

    1. Prefer an external edge sharing this connector's own raw node
       osmid at either endpoint, whose own direction is within
       ``angle_threshold_deg`` of this connector's own center bearing -
       the strongest signal this connector is really that specific
       street continuing through the node_id.
    2. Otherwise, the candidate street_id with the single best (smallest)
       direction difference to this connector's own center bearing,
       among ALL external edges at this node_id, ties broken by the
       smaller street_id.

    Both steps skip any candidate whose own known name is incompatible
    with this connector's own real name (if it has one) - a named
    connector (e.g. a short residential link that happens to be the
    dominant-rank street's own worse-rank neighbor at a busy interchange)
    must never be silently absorbed into a differently-named street just
    because it points the same way.

    A connector with no eligible external incidence at its own node_id
    at all is left with no street_id (``-1``), same as before.
    """
    external_positions = [
        i for i in idx if cluster_u_arr[i] != cluster_v_arr[i] and street_id_arr[i] != -1
    ]
    internal_idx = [i for i in idx if cluster_u_arr[i] == cluster_v_arr[i]]
    if not internal_idx:
        return

    cluster_incidence: dict = defaultdict(list)
    sid_names: dict = defaultdict(set)
    for i in external_positions:
        sid = int(street_id_arr[i])
        cluster_incidence[cluster_u_arr[i]].append((u_arr[i], float(start_angle[i]), sid))
        cluster_incidence[cluster_v_arr[i]].append((v_arr[i], float(end_angle[i]), sid))
        if name_arr[i] is not None:
            sid_names[sid].add(name_arr[i])

    def eligible(own_name, sid: int) -> bool:
        if own_name is None:
            return True
        known = sid_names.get(sid)
        if not known:
            return True
        return any(name_matching.names_match(own_name, other, name_similarity_threshold) for other in known)

    for i in internal_idx:
        cu = cluster_u_arr[i]
        candidates = cluster_incidence.get(cu, ())
        if not candidates:
            continue
        own_bearing = _overall_bearing_deg(geometries[i])
        own_endpoints = (u_arr[i], v_arr[i])
        own_name = name_arr[i]

        best_same_osmid = None
        for raw_node, ext_angle, sid in candidates:
            if raw_node not in own_endpoints or not eligible(own_name, sid):
                continue
            diff = _angle_diff(own_bearing, ext_angle)
            if diff > angle_threshold_deg:
                continue
            key = (diff, sid)
            if best_same_osmid is None or key < best_same_osmid:
                best_same_osmid = key
        if best_same_osmid is not None:
            street_id_arr[i] = best_same_osmid[1]
            if own_name is not None:
                sid_names[best_same_osmid[1]].add(own_name)
            continue

        best_key = None
        best_sid = None
        for raw_node, ext_angle, sid in candidates:
            if not eligible(own_name, sid):
                continue
            diff = _angle_diff(own_bearing, ext_angle)
            key = (diff, sid)
            if best_key is None or key < best_key:
                best_key, best_sid = key, sid
        if best_sid is not None:
            street_id_arr[i] = best_sid
            if own_name is not None:
                sid_names[best_sid].add(own_name)


def _reassign_short_streets(
    eids: np.ndarray,
    u_arr: np.ndarray,
    v_arr: np.ndarray,
    cluster_u_arr: np.ndarray,
    cluster_v_arr: np.ndarray,
    geometries: np.ndarray,
    name_arr: np.ndarray,
    start_angle: np.ndarray,
    end_angle: np.ndarray,
    is_main_by_eid: dict,
    main_length_by_sid: dict,
    street_id_arr: np.ndarray,
    min_street_length_m: float,
    direction_tolerance_deg: float,
    name_similarity_threshold: float,
    coverage_threshold_m: float,
    coverage_fraction: float,
) -> tuple[np.ndarray, set[tuple]]:
    """Reassign every edge of a "too-short" street_id - one whose own
    main path (``main_length_by_sid`` - see "Main path detection")
    is shorter than ``min_street_length_m`` - as a whole, to another,
    valid (long enough) street_id.

    Each too-short street_id's own single target is chosen in this
    order:

    1. **The 90% rule** (``_best_coverage_match``, on the too-short
       street_id's own WHOLE combined geometry against every valid
       street_id's own combined geometry): if at least
       ``coverage_fraction`` of the too-short street_id's own length
       falls within ``coverage_threshold_m`` of the valid network as a
       whole, its target is whichever single valid street_id it runs
       alongside for the largest share of its own length.
    2. **Otherwise, direction match at the main path's own endpoints.**
       The too-short street_id's own main path has two endpoint (raw)
       nodes - the degree-1 nodes within its own main-path edges. At
       each endpoint's own node_id, every OTHER valid street_id's own
       edge there is compared by direction (the same tangent-at-node
       convention used throughout this module) to this street's own
       tangent at that same point. The single best (smallest angle
       difference), name-eligible candidate across both endpoints wins.

    Eligibility (both steps): a named too-short street can only
    reassign to a candidate whose own known names are compatible with it
    (or has no real name of its own yet); an unnamed too-short street is
    eligible for any candidate.

    Runs deterministically: too-short street_ids are processed in
    ascending order, and every ranking breaks a literal tie by the
    smaller candidate street_id.

    Returns:
        A tuple ``(street_id_arr, reassigned_eids)``: a copy of
        ``street_id_arr`` with every too-short street_id's edges
        reassigned to its own best target, where one was found, and the
        set of edge ids (``eids``, not row indices) that were moved -
        these can never be their new street_id's own main path (see
        "Main path" in report/street_identity.md).
    """
    reassigned_eids: set = set()
    if not min_street_length_m or min_street_length_m <= 0:
        return street_id_arr, reassigned_eids
    out = street_id_arr.copy()

    valid_idx = np.flatnonzero(out != -1)
    if valid_idx.size == 0:
        return out, reassigned_eids

    by_sid: dict[int, list[int]] = defaultdict(list)
    for i in valid_idx:
        by_sid[int(out[i])].append(int(i))

    short_sids = sorted(
        sid for sid in by_sid if main_length_by_sid.get(sid, 0.0) < min_street_length_m
    )
    if not short_sids:
        return out, reassigned_eids
    valid_sids = sorted(
        sid for sid in by_sid if main_length_by_sid.get(sid, 0.0) >= min_street_length_m
    )
    if not valid_sids:
        return out, reassigned_eids

    valid_sids_set = set(valid_sids)
    sid_names: dict[int, set] = defaultdict(set)
    for sid in valid_sids:
        for i in by_sid[sid]:
            if name_arr[i] is not None:
                sid_names[sid].add(name_arr[i])

    def eligible(own_name, cand_sid: int) -> bool:
        if own_name is not None and sid_names[cand_sid]:
            return any(
                name_matching.names_match(own_name, other, name_similarity_threshold)
                for other in sid_names[cand_sid]
            )
        return True

    # node_id -> [(candidate street_id, its own tangent angle there), ...]
    # for every valid street_id's own edges.
    node_valid_incidence: dict = defaultdict(list)
    for sid in valid_sids:
        for i in by_sid[sid]:
            node_valid_incidence[cluster_u_arr[i]].append((sid, float(start_angle[i])))
            node_valid_incidence[cluster_v_arr[i]].append((sid, float(end_angle[i])))

    # Each valid street_id's own buffer is built ONCE here and reused
    # for every short street_id's own coverage check below, instead of
    # being rebuilt fresh for every one - a real performance bug found
    # via real-data testing (this alone made a whole real-world run take
    # well over a minute).
    valid_sid_buffers = (
        _build_candidate_buffers({sid: by_sid[sid] for sid in valid_sids}, geometries, coverage_threshold_m)
        if coverage_threshold_m and coverage_threshold_m > 0
        else {}
    )

    for sid in short_sids:
        members = by_sid[sid]
        own_name = next((name_arr[i] for i in members if name_arr[i] is not None), None)

        eligible_valid_members = {
            cand_sid: js for cand_sid, js in by_sid.items()
            if cand_sid in valid_sids_set and eligible(own_name, cand_sid)
        }
        own_geom = shapely.union_all([geometries[i] for i in members])

        # Directly touching a valid street_id (sharing a literal raw
        # node) bypasses the 90% rule, but NOT direction compatibility
        # at that shared node - merging into a candidate that shares a
        # node but points a genuinely different way there would create
        # a branch (two incompatible directions at one node_id under the
        # same street_id), which "No more than one direction per
        # node_id" never allows, and which the branching invariant
        # (_split_branching_street_ids) would just undo again right
        # after - a real bug found via real-data testing: an earlier
        # version treated "touches it directly" as sufficient on its own
        # regardless of direction, which oscillated forever against the
        # branching invariant (this reassignment recreating the branch
        # every iteration, that invariant splitting it apart every
        # iteration) instead of ever reaching a fixed point. A touching
        # candidate is only eligible if at least one of THIS street_id's
        # own tangents at the shared node is within
        # ``direction_tolerance_deg`` of the candidate's own tangent
        # there. Best (largest real overlap) among multiple eligible
        # touching candidates wins.
        own_node_tangents: dict = defaultdict(list)
        for i in members:
            own_node_tangents[u_arr[i]].append(float(start_angle[i]))
            own_node_tangents[v_arr[i]].append(float(end_angle[i]))

        def touches_with_compatible_direction(cand_js, own_node_tangents=own_node_tangents):
            for j in cand_js:
                for raw_node, cand_angle in (
                    (u_arr[j], float(start_angle[j])), (v_arr[j], float(end_angle[j]))
                ):
                    for own_angle in own_node_tangents.get(raw_node, ()):
                        if _angle_diff(own_angle, cand_angle) <= direction_tolerance_deg:
                            return True
            return False

        touching_members = {
            cand_sid: js for cand_sid, js in eligible_valid_members.items()
            if touches_with_compatible_direction(js)
        }
        if touching_members:
            if coverage_threshold_m and coverage_threshold_m > 0:
                touching_buffers = {s: valid_sid_buffers[s] for s in touching_members}
                combined_buffer = shapely.union_all(list(touching_buffers.values()))
                best_sid = _match_against_buffers(own_geom, combined_buffer, touching_buffers, 0.0)
            else:
                best_sid = min(touching_members)
        elif coverage_threshold_m and coverage_threshold_m > 0:
            eligible_buffers = {s: valid_sid_buffers[s] for s in eligible_valid_members}
            combined_buffer = shapely.union_all(list(eligible_buffers.values()))
            best_sid = _match_against_buffers(own_geom, combined_buffer, eligible_buffers, coverage_fraction)
        else:
            best_sid = None

        if best_sid is None:
            main_members = [i for i in members if is_main_by_eid.get(eids[i], False)] or members

            degree: dict[int, int] = defaultdict(int)
            for i in main_members:
                degree[u_arr[i]] += 1
                degree[v_arr[i]] += 1
            end_raw_nodes = {n for n, d in degree.items() if d == 1} or set(degree.keys())

            best_key = None
            for i in main_members:
                for raw_node, at_start in ((u_arr[i], True), (v_arr[i], False)):
                    if raw_node not in end_raw_nodes:
                        continue
                    own_angle = start_angle[i] if at_start else end_angle[i]
                    node_id = cluster_u_arr[i] if at_start else cluster_v_arr[i]
                    for cand_sid, cand_angle in node_valid_incidence.get(node_id, ()):
                        if not eligible(own_name, cand_sid):
                            continue
                        diff = _angle_diff(own_angle, cand_angle)
                        if diff > direction_tolerance_deg:
                            continue
                        key = (diff, cand_sid)
                        if best_key is None or key < best_key:
                            best_key, best_sid = key, cand_sid

        if best_sid is None:
            continue
        for i in members:
            out[i] = best_sid
            reassigned_eids.add(eids[i])
        if own_name is not None:
            sid_names[best_sid] = sid_names[best_sid] | {own_name}

    return out, reassigned_eids


def _force_reassign_remaining_short_streets(
    eids: np.ndarray,
    geometries: np.ndarray,
    name_arr: np.ndarray,
    main_length_by_sid: dict,
    street_id_arr: np.ndarray,
    min_street_length_m: float,
    name_similarity_threshold: float,
) -> tuple[np.ndarray, set]:
    """Absolute, unconditional guarantee: NO `street_id` may end with a
    main path shorter than ``min_street_length_m``. Run once, as the
    very last `street_id`-assignment step, after ``_reassign_short_streets``,
    the branching invariant, and the connectivity invariant have all
    already run to their own fixed point - for whatever too-short
    `street_id`s are STILL left (no direction match, no 90% coverage
    match, nothing eligible under any of those steps' own narrower
    criteria), this force-merges each one's WHOLE set of edges into
    whichever valid `street_id` is plain geometrically nearest, no
    distance cap, no direction requirement, no coverage-fraction
    requirement - only name-compatibility is still preferred (and even
    that is dropped, as a last resort, if literally no name-compatible
    valid candidate exists anywhere).

    Minimum street length is treated as the single most important
    invariant this module enforces, overriding "no more than one
    direction per node_id" and the connectivity invariant when they
    would otherwise conflict for a genuinely isolated short fragment
    with no compatible neighbor - deliberately NOT followed by another
    invariant-enforcement pass, so this step's own merges are final and
    cannot be undone.

    Args:
        eids: Undirected edge ids.
        geometries: Line geometry per edge.
        name_arr: Clean name (or ``None``) per edge.
        main_length_by_sid: Each `street_id`'s own main path length, from
            ``_compute_main_direction``.
        street_id_arr: Current street_id per edge (``-1`` for none).
        min_street_length_m: Minimum main path length (meters). A value
            <= 0 disables this step entirely.
        name_similarity_threshold: Minimum ``name_matching.name_similarity``
            score (0-100) for two names to be considered the same street.

    Returns:
        A tuple ``(street_id_arr, reassigned_eids)``: a copy of
        ``street_id_arr`` with every still-too-short `street_id`'s edges
        force-merged into its nearest valid target, and the set of edge
        ids moved this way - these can never be their new `street_id`'s
        own main path (see "Main path" in report/street_identity.md).
    """
    reassigned_eids: set = set()
    if not min_street_length_m or min_street_length_m <= 0:
        return street_id_arr, reassigned_eids
    out = street_id_arr.copy()

    valid_idx = np.flatnonzero(out != -1)
    if valid_idx.size == 0:
        return out, reassigned_eids

    by_sid: dict[int, list[int]] = defaultdict(list)
    for i in valid_idx:
        by_sid[int(out[i])].append(int(i))

    short_sids = sorted(
        sid for sid in by_sid if main_length_by_sid.get(sid, 0.0) < min_street_length_m
    )
    if not short_sids:
        return out, reassigned_eids
    valid_sids = sorted(
        sid for sid in by_sid if main_length_by_sid.get(sid, 0.0) >= min_street_length_m
    )
    if not valid_sids:
        # No valid street_id anywhere in the whole graph to merge into -
        # nothing this step can do.
        return out, reassigned_eids

    sid_geom = {sid: shapely.union_all([geometries[i] for i in by_sid[sid]]) for sid in valid_sids}
    sid_names: dict[int, set] = defaultdict(set)
    for sid in valid_sids:
        for i in by_sid[sid]:
            if name_arr[i] is not None:
                sid_names[sid].add(name_arr[i])

    def name_compatible(own_name, cand_sid: int) -> bool:
        if own_name is not None and sid_names[cand_sid]:
            return any(
                name_matching.names_match(own_name, other, name_similarity_threshold)
                for other in sid_names[cand_sid]
            )
        return True

    for sid in short_sids:
        members = by_sid[sid]
        own_name = next((name_arr[i] for i in members if name_arr[i] is not None), None)
        own_geom = shapely.union_all([geometries[i] for i in members])

        best_sid, best_dist = None, None
        for cand_sid in valid_sids:
            if not name_compatible(own_name, cand_sid):
                continue
            dist = own_geom.distance(sid_geom[cand_sid])
            if best_dist is None or dist < best_dist:
                best_dist, best_sid = dist, cand_sid

        if best_sid is None:
            # No name-compatible valid candidate anywhere - minimum
            # street length still wins: ignore name-compatibility too,
            # rather than leave this street_id too short.
            for cand_sid in valid_sids:
                dist = own_geom.distance(sid_geom[cand_sid])
                if best_dist is None or dist < best_dist:
                    best_dist, best_sid = dist, cand_sid

        for i in members:
            out[i] = best_sid
            reassigned_eids.add(eids[i])
        if own_name is not None:
            sid_names[best_sid] = sid_names[best_sid] | {own_name}

    return out, reassigned_eids


def recompute_main_paths(
    nodes_gdf: gpd.GeoDataFrame,
    edges_gdf: gpd.GeoDataFrame,
    highway_ranks: dict | None = None,
    main_path_angle_threshold_deg: float = DEFAULT_MAIN_PATH_ANGLE_THRESHOLD_DEG,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Recompute main path/orientation/connectors/turn-splitting for a
    hand-edited ``street_id`` assignment - the exact same tail logic
    ``label_streets`` itself runs as steps 4-8 of its own docstring
    (main path detection, physical reorientation, synthetic connectors,
    turn-splitting, and the final connectivity validation), extracted
    into its own standalone, reusable function so it can be re-run
    without a fresh graph.

    This is for the manual street editor (``street_editor.apply_street_edits``):
    a person hand-reassigns which edges belong to which ``street_id`` in
    the browser, and that MEMBERSHIP is treated as final/authoritative -
    this function never second-guesses it. But path order, walking
    direction, synthetic connectors through a real multi-osmid
    intersection, and turn-splitting are not something a browser click
    can compute, so all of that is fully recomputed here, from scratch,
    for every ``street_id`` present in ``edges_gdf``, exactly the way
    ``label_streets`` would if it were building that same membership up
    fresh - see report/street_identity.md ("Recomputing main paths after
    manual edits") for the full write-up.

    Any existing synthetic connector row (``key >= _SYNTHETIC_CONNECTOR_KEY_OFFSET``)
    is dropped first - it belonged to whatever the main path used to be
    before this recompute, and since ``street_id`` membership may have
    changed, it's stale by construction; a fresh one gets synthesized
    below for whatever the NEW main path actually needs.

    Node clustering (``node_id``) is NOT recomputed - it's read straight
    off ``nodes_gdf["node_id"]`` (already stable from whichever
    ``label_streets`` run originally produced it), so this recompute
    stays cheap and keeps respecting the same real-intersection grouping
    the original run established.

    Args:
        nodes_gdf: Node GeoDataFrame from ``street_ids.label_streets``
            (or a prior ``recompute_main_paths`` call), with its own
            ``node_id`` column already resolved. Untouched by this
            function.
        edges_gdf: Edge GeoDataFrame with final, already-authoritative
            ``street_id``/``street_name`` columns (e.g. just remapped by
            ``street_editor.apply_street_edits``), plus ``highway``,
            ``geometry``, ``length``, ``is_drivable``.
        highway_ranks: Highway-type-to-rank mapping (default
            ``DEFAULT_HIGHWAY_RANKS``) - only used to pick each final
            ``street_id``'s own representative ``street_rank``/``highway``
            in ``streets_df``, exactly like ``label_streets`` does.
        main_path_angle_threshold_deg: Turn deviation (deg, directed)
            between two consecutive main-path edges beyond which a
            ``street_id`` is split into two - see ``label_streets``' own
            docstring for the full explanation. Default
            ``DEFAULT_MAIN_PATH_ANGLE_THRESHOLD_DEG``.

    Returns:
        A tuple ``(edges_gdf, streets_df)``:

        - edges_gdf: ``street_id`` membership exactly as given, but with
          ``is_main_direction``/``nearest_main_eid`` freshly recomputed,
          every main-path edge stored "backwards" relative to the new
          walk physically reoriented (``u``/``v``/geometry flipped),
          every stale synthetic connector dropped and replaced with a
          fresh one, and a turn-split ``street_id`` reassigned onto its
          own newly allocated id.
        - streets_df: one row per FINAL ``street_id`` (after
          turn-splitting), columns ``street_id``, ``street_name``,
          ``street_rank``, ``highway``, ``osmids``.

    Raises:
        ValueError: if the recomputed main path for some ``street_id``
            doesn't form one connected, simple path
            (``_validate_main_path_invariant``) - an internal bug in
            this function itself, not a caller error.
    """
    ranks = highway_ranks or DEFAULT_HIGHWAY_RANKS

    edges_gdf = edges_gdf[
        edges_gdf.index.get_level_values("key") < _SYNTHETIC_CONNECTOR_KEY_OFFSET
    ].copy()

    edges_reset = edges_gdf.reset_index()
    u_arr = edges_reset["u"].to_numpy()
    v_arr = edges_reset["v"].to_numpy()
    edges_reset["_pair"] = list(
        zip(np.minimum(u_arr, v_arr), np.maximum(u_arr, v_arr), edges_reset["key"])
    )
    undirected = edges_reset.drop_duplicates("_pair").set_index("_pair")

    node_to_cluster = nodes_gdf["node_id"].to_dict()

    start_angle, end_angle, raw_start_bearing, raw_end_bearing = _vectorized_endpoint_angles(
        undirected.geometry.to_numpy()
    )
    rank_series, highway_series = _vectorized_highway_rank_and_value(undirected["highway"], ranks)
    rank_arr = rank_series.to_numpy()
    highway_arr = highway_series.to_numpy()
    length_arr = undirected["length"].to_numpy()
    is_drivable_arr = undirected["is_drivable"].to_numpy()
    u_und = undirected["u"].to_numpy().copy()
    v_und = undirected["v"].to_numpy().copy()
    eids = undirected.index.to_numpy()
    all_geometries = undirected.geometry.to_numpy().copy()

    cluster_u = np.array([node_to_cluster[u] for u in u_und])
    cluster_v = np.array([node_to_cluster[v] for v in v_und])

    street_id_col = undirected["street_id"]
    raw_street_ids = [None if pd.isna(sid) else sid for sid in street_id_col]
    street_name_arr = undirected["street_name"].to_numpy()

    # Every helper function reused below (_compute_main_direction,
    # _reorient_main_path_edges, _synthesize_main_path_connectors,
    # _compute_nearest_main_eid, _split_main_path_at_turns) casts each
    # street_id to a plain int internally - fine for a fresh
    # ``label_streets`` run (ids are always its own dense 1..N range),
    # but a hand-assigned street_id here can be an arbitrary, non-numeric
    # string label (the editor's "Create new street" button, or a
    # literal string the CSV format still allows - see
    # ``street_editor.apply_street_edits``' own ``_coerce_id``). So every
    # DISTINCT current street_id (numeric or not) is mapped to its own
    # int surrogate here - the numeric ones keep their own value as their
    # surrogate (so a purely-numeric edit round-trips as a no-op), each
    # non-numeric one gets a freshly allocated surrogate above the
    # highest numeric id in use - and every helper below runs entirely in
    # surrogate-id space. ``next_id`` (where turn-splitting starts
    # allocating brand-new ids) continues on from there, and every
    # surrogate is translated back to its own original street_id value
    # (or, for a genuinely NEW id turn-splitting allocated, kept as the
    # plain int it already is) when edges_gdf/streets_df are built at the
    # very end.
    numeric_ids = [int(sid) for sid in raw_street_ids if isinstance(sid, (int, np.integer))]
    base_next_id = (max(numeric_ids) + 1) if numeric_ids else 10_000_000

    distinct_non_numeric = sorted(
        {sid for sid in raw_street_ids if sid is not None and not isinstance(sid, (int, np.integer))},
        key=str,
    )
    sid_to_surrogate: dict = {sid: sid for sid in numeric_ids}
    surrogate_to_sid: dict = {sid: sid for sid in numeric_ids}
    next_id = base_next_id
    for sid in distinct_non_numeric:
        sid_to_surrogate[sid] = next_id
        surrogate_to_sid[next_id] = sid
        next_id += 1

    def _surrogate_of(sid):
        if isinstance(sid, (int, np.integer)):
            return int(sid)
        return sid_to_surrogate[sid]

    edge_street_id = np.array(
        [-1 if sid is None else _surrogate_of(sid) for sid in raw_street_ids], dtype=np.int64
    )

    # Main-path computation over EXTERNAL edges only - every currently-
    # assigned edge is eligible, since there's no redistribution history
    # to exclude anything with here.
    main_direction_map, _main_length_by_sid = _compute_main_direction(
        eids, cluster_u, cluster_v, length_arr, edge_street_id, is_drivable_arr, frozenset()
    )
    is_main_final = np.array([main_direction_map.get(eid, True) for eid in eids], dtype=bool)

    reoriented_mask = _reorient_main_path_edges(
        eids, cluster_u, cluster_v, u_und, v_und, all_geometries, edge_street_id, is_main_final,
    )
    if reoriented_mask.any():
        r_idx = np.flatnonzero(reoriented_mask)
        r_start, r_end, r_raw_start, r_raw_end = _vectorized_endpoint_angles(all_geometries[r_idx])
        start_angle[r_idx] = r_start
        end_angle[r_idx] = r_end
        raw_start_bearing[r_idx] = r_raw_start
        raw_end_bearing[r_idx] = r_raw_end

    node_geometry = nodes_gdf.geometry
    connectors = _synthesize_main_path_connectors(
        eids, cluster_u, cluster_v, u_und, v_und, is_main_final, edge_street_id, node_geometry,
    )

    n_orig = len(eids)
    synthetic_keys = [
        _SYNTHETIC_CONNECTOR_KEY_OFFSET + k for k in range(len(connectors))
    ]
    synthetic_eids = np.empty(len(connectors), dtype=object)
    for _k, (c, key) in enumerate(zip(connectors, synthetic_keys)):
        synthetic_eids[_k] = (min(c["u"], c["v"]), max(c["u"], c["v"]), key)
    if len(connectors):
        syn_u = np.array([c["u"] for c in connectors])
        syn_v = np.array([c["v"] for c in connectors])
        syn_geoms = np.array([c["geometry"] for c in connectors], dtype=object)
        syn_lengths = np.array([c["length"] for c in connectors])
        syn_sid = np.array([c["street_id"] for c in connectors], dtype=np.int64)
        syn_cluster = np.array([node_to_cluster[c["u"]] for c in connectors])
        syn_start, syn_end, syn_raw_start, syn_raw_end = _vectorized_endpoint_angles(syn_geoms)

        eids = np.concatenate([eids, synthetic_eids])
        cluster_u = np.concatenate([cluster_u, syn_cluster])
        cluster_v = np.concatenate([cluster_v, syn_cluster])
        u_und = np.concatenate([u_und, syn_u])
        v_und = np.concatenate([v_und, syn_v])
        all_geometries = np.concatenate([all_geometries, syn_geoms])
        length_arr = np.concatenate([length_arr, syn_lengths])
        is_drivable_arr = np.concatenate([is_drivable_arr, np.ones(len(connectors), dtype=bool)])
        start_angle = np.concatenate([start_angle, syn_start])
        end_angle = np.concatenate([end_angle, syn_end])
        raw_start_bearing = np.concatenate([raw_start_bearing, syn_raw_start])
        raw_end_bearing = np.concatenate([raw_end_bearing, syn_raw_end])
        edge_street_id = np.concatenate([edge_street_id, syn_sid])
        is_main_final = np.concatenate([is_main_final, np.ones(len(connectors), dtype=bool)])

    nearest_main_eid_map = _compute_nearest_main_eid(
        eids, edge_street_id, is_main_final, all_geometries
    )

    edge_street_id, split_from, next_id = _split_main_path_at_turns(
        eids, cluster_u, cluster_v, raw_start_bearing, raw_end_bearing,
        edge_street_id, is_main_final, nearest_main_eid_map, main_path_angle_threshold_deg,
        next_id,
    )

    _validate_main_path_invariant(eids, u_und, v_und, cluster_u, cluster_v, edge_street_id, is_main_final)

    # Persist physical reorientation over the original (non-synthetic)
    # rows, exactly like label_streets does.
    edges_u = edges_gdf.index.get_level_values("u").to_numpy()
    edges_v = edges_gdf.index.get_level_values("v").to_numpy()
    edges_key = edges_gdf.index.get_level_values("key").to_numpy()
    eid_to_pos = {eid: i for i, eid in enumerate(eids)}
    pos = [
        eid_to_pos[(min(u, v), max(u, v), k)] for u, v, k in zip(edges_u, edges_v, edges_key)
    ]

    if reoriented_mask.any():
        eid_to_reoriented = {tuple(eids[:n_orig][i]): True for i in np.flatnonzero(reoriented_mask)}
        new_edges_u = edges_u.copy()
        new_edges_v = edges_v.copy()
        new_geoms_for_reorient = edges_gdf.geometry.to_numpy().copy()
        for k, i in enumerate(pos):
            key = (int(min(edges_u[k], edges_v[k])), int(max(edges_u[k], edges_v[k])), int(edges_key[k]))
            if eid_to_reoriented.get(key):
                new_edges_u[k], new_edges_v[k] = edges_v[k], edges_u[k]
                new_geoms_for_reorient[k] = shapely.LineString(list(new_geoms_for_reorient[k].coords)[::-1])
        edges_gdf = edges_gdf.reset_index()
        edges_gdf["u"] = new_edges_u
        edges_gdf["v"] = new_edges_v
        edges_gdf["geometry"] = gpd.GeoSeries(new_geoms_for_reorient, crs=edges_gdf.crs)
        edges_gdf = edges_gdf.set_index(["u", "v", "key"])
        edges_u, edges_v, edges_key = new_edges_u, new_edges_v, edges_key

    # Kept in SURROGATE-id space for now (only ``-1`` -> ``None``) - every
    # street_id-keyed lookup below (final_attrs, street_to_osmids, the
    # synthetic-connector name backfill) is built in that same surrogate
    # space, so it's all translated back to each original street_id
    # value in one final pass at the very end.
    def _sid_to_col(sid):
        return None if sid == -1 else sid

    edges_gdf["street_id"] = pd.Series(
        [_sid_to_col(edge_street_id[i]) for i in pos], index=edges_gdf.index, dtype=object
    )
    edges_gdf["is_main_direction"] = pd.Series(
        [bool(is_main_final[i]) for i in pos], index=edges_gdf.index, dtype=bool
    )
    edges_gdf["nearest_main_eid"] = pd.Series(
        [nearest_main_eid_map.get(eids[i]) for i in pos], index=edges_gdf.index, dtype=object
    )

    # Every edge already sharing a street_id already carries the SAME
    # street_name (set uniformly by apply_street_edits' own remap before
    # this function ever runs) - street_name itself doesn't need any
    # "representative edge" logic the way street_rank/highway still do.
    edges_gdf["street_name"] = pd.Series(
        [street_name_arr[i] if i < n_orig else None for i in pos], index=edges_gdf.index, dtype=object
    )

    if connectors:
        syn_rows = {
            "u": [int(u_und[i]) for i in range(n_orig, len(eids))],
            "v": [int(v_und[i]) for i in range(n_orig, len(eids))],
            "key": synthetic_keys,
            "geometry": [all_geometries[i] for i in range(n_orig, len(eids))],
            "length": [float(length_arr[i]) for i in range(n_orig, len(eids))],
            "street_id": pd.Series(
                [_sid_to_col(edge_street_id[i]) for i in range(n_orig, len(eids))], dtype=object
            ),
            "street_name": [None] * len(connectors),
            "is_main_direction": [True] * len(connectors),
            "is_drivable": [True] * len(connectors),
            "structure_type": [None] * len(connectors),
            "nearest_main_eid": [None] * len(connectors),
        }
        syn_gdf = gpd.GeoDataFrame(syn_rows, geometry="geometry", crs=edges_gdf.crs)
        syn_gdf = syn_gdf.set_index(["u", "v", "key"])
        for col in edges_gdf.columns:
            if col not in syn_gdf.columns:
                syn_gdf[col] = None
        syn_gdf = syn_gdf[edges_gdf.columns]
        edges_gdf = pd.concat([edges_gdf, syn_gdf])

    has_osmid_col = "osmid" in undirected.columns
    street_to_osmids = defaultdict(set)
    if has_osmid_col:
        for sid, osmid in zip(edge_street_id[:n_orig], undirected["osmid"].to_numpy()):
            if sid == -1:
                continue
            if isinstance(osmid, (list, tuple, set)):
                street_to_osmids[sid].update(osmid)
            else:
                street_to_osmids[sid].add(osmid)

    # Representative attrs per (pre-split) street_id: lowest rank number
    # wins, ties broken by smallest eid - same convention label_streets
    # uses, just computed directly over the current (already-final)
    # edge_street_id instead of a fresh rank_arr-driven pass.
    rep_key: dict = {}
    for i in range(n_orig):
        sid = edge_street_id[i]
        if sid == -1:
            continue
        key = (rank_arr[i], eids[i])
        if sid not in rep_key or key < rep_key[sid][0]:
            rep_key[sid] = (key, i)

    final_attrs: dict = {}
    for sid, (_key, i) in rep_key.items():
        name = street_name_arr[i] if pd.notna(street_name_arr[i]) else None
        final_attrs[sid] = (name, rank_arr[i], highway_arr[i])
    for split_sid, source_sid in split_from.items():
        final_attrs[split_sid] = final_attrs[source_sid]

    # A street_id that had no representative of its own at all (e.g. it
    # only ever had non-main/furniture edges pointing to it, degenerate)
    # still needs a name - fall back to the first non-null street_name
    # among its own members.
    for sid in set(int(s) for s in edge_street_id if s != -1):
        if sid in final_attrs:
            continue
        name = next(
            (
                street_name_arr[i] if i < n_orig else None
                for i in range(len(eids))
                if edge_street_id[i] == sid and i < n_orig and pd.notna(street_name_arr[i])
            ),
            None,
        )
        final_attrs[sid] = (name, None, None)

    if connectors:
        syn_street_id_col = edges_gdf["street_id"].to_numpy()
        syn_name_col = edges_gdf["street_name"].to_numpy(dtype=object).copy()
        syn_mask = edges_gdf["street_name"].isna().to_numpy() & pd.notna(syn_street_id_col)
        for k in np.flatnonzero(syn_mask):
            sid = syn_street_id_col[k]
            if sid in final_attrs:
                syn_name_col[k] = final_attrs[sid][0]
        edges_gdf["street_name"] = syn_name_col

    final_ids = sorted(final_attrs.keys(), key=str)
    streets_df = pd.DataFrame(
        {
            "street_id": [surrogate_to_sid.get(sid, sid) for sid in final_ids],
            "street_name": pd.Series(
                [final_attrs[sid][0] for sid in final_ids], dtype=object
            ),
            "street_rank": [final_attrs[sid][1] for sid in final_ids],
            "highway": pd.Series(
                [final_attrs[sid][2] for sid in final_ids], dtype=object
            ),
            "osmids": [
                ";".join(str(o) for o in sorted(street_to_osmids[sid], key=str))
                if has_osmid_col
                else ""
                for sid in final_ids
            ],
        }
    )

    # Translate every surrogate street_id in edges_gdf back to its own
    # original street_id value (a hand-assigned non-numeric label keeps
    # its own string; a genuinely NEW id turn-splitting allocated here
    # has no original to translate back to, so it's kept as-is).
    edges_gdf["street_id"] = edges_gdf["street_id"].map(
        lambda sid: sid if pd.isna(sid) else surrogate_to_sid.get(sid, sid)
    )

    return edges_gdf, streets_df


def label_streets(
    G: nx.MultiDiGraph,
    angle_threshold_deg: float = DEFAULT_ANGLE_THRESHOLD_DEG,
    highway_ranks: dict | None = None,
    node_cluster_m: float = DEFAULT_NODE_CLUSTER_M,
    name_similarity_threshold: float = DEFAULT_NAME_SIMILARITY_THRESHOLD,
    parallel_dist_m: float = DEFAULT_PARALLEL_DIST_M,
    parallel_angle_threshold_deg: float = DEFAULT_PARALLEL_ANGLE_THRESHOLD_DEG,
    non_drivable_highways: set | None = None,
    private_access_values: set | None = None,
    parking_service_values: set | None = None,
    min_street_length_m: float = DEFAULT_MIN_STREET_LENGTH_M,
    parallel_pass_touching_dist_m: float = DEFAULT_PARALLEL_PASS_TOUCHING_DIST_M,
    parallel_pass_non_touching_dist_m: float = DEFAULT_PARALLEL_PASS_NON_TOUCHING_DIST_M,
    node_direction_tolerance_deg: float = DEFAULT_NODE_DIRECTION_TOLERANCE_DEG,
    non_drivable_coverage_fraction: float = DEFAULT_NON_DRIVABLE_COVERAGE_FRACTION,
    num_node_directions: int = DEFAULT_NUM_NODE_DIRECTIONS,
    main_path_angle_threshold_deg: float = DEFAULT_MAIN_PATH_ANGLE_THRESHOLD_DEG,
    tangent_continuation_fraction: float = DEFAULT_TANGENT_CONTINUATION_FRACTION,
    tangent_continuation_window_m: float = DEFAULT_TANGENT_CONTINUATION_WINDOW_M,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, pd.DataFrame]:
    """Assign consistent street identities to a street network graph's edges.

    **Algorithm, in order** (see report/street_identity.md for the full
    write-up):

    1. **Node clustering** (``_cluster_nodes``): nodes within
       ``node_cluster_m`` of each other are treated as one real
       intersection, its own node_id - a single real intersection is
       often mapped as several nodes a few meters apart.
    2. **Two passes, drivable edges then non-drivable** (private/
       parking edges excluded from both, never get a street_id): the
       drivable pass resolves its own street_ids first, entirely on its
       own; the non-drivable pass then resolves its own edges the same
       way among themselves, EXCEPT that a resulting non-drivable group
       genuinely parallel to an already-resolved drivable street_id
       (same ``_edges_are_truly_parallel`` test as within-pass parallel
       grouping, name-gated) is donated that drivable street_id directly
       instead of getting a fresh one of its own - a sidewalk/footway
       running alongside a road is still "the same street" for
       addressing purposes. Every other non-drivable group gets a fresh
       id, numbered after every drivable one. Within each pass:
       a. **Parallel grouping**: two edges are joined into the same
          street_id when their own central segments are close (see
          ``parallel_pass_touching_dist_m``/``_non_touching_dist_m``)
          and run the same direction, gated by name-compatibility.
       b. **Per-node_id direction assignment for EXTERNAL edges** (an
          edge whose two endpoints cluster to two DIFFERENT node_ids):
          at every node_id, its own external edges are grouped into
          direction bins (``node_direction_tolerance_deg``); edges in
          the same bin are joined into one
          street_id, UNLESS an edge's own highway rank is worse than
          the best rank present at that node_id, in which case it's cut
          there instead (rule 2: a lower-rank street never continues
          through a higher-rank crossing). Every join here is also
          checked against each group's own fixed "anchor" direction
          (not just its immediate neighbor), so a chain of locally-
          similar-direction edges can never drift into an incoherent
          street spanning wildly different overall directions.
       c. **Direction-based assignment for INTERNAL edges** (both
          endpoints cluster to the SAME node_id - a connector inside one
          intersection): assigned to whichever external edge at that
          node_id shares its own raw node osmid with a similar
          direction, or failing that, whichever external edge there has
          the closest overall direction match.
    3. **Connectivity invariant**: every street_id's own edges must form
       one connected component - touching, or within
       ``max(parallel_pass_touching_dist_m, parallel_pass_non_touching_dist_m)``
       of each other - or it's split apart (see ``_split_disconnected_components``).
    4. **Main path detection** (``_compute_main_direction``) and
       **length-based reassignment**: a street_id whose own main path is
       shorter than ``min_street_length_m`` is reassigned as a whole to
       whichever OTHER, valid street_id has a similarly-directed edge at
       its own main path's start/end node_id, or, failing that, is
       plain geometrically nearest (bounded by ``parallel_dist_m``).
       Runs to a small, bounded fixed point together with the
       connectivity invariant, since either step can create a fresh
       fragment the other must get a chance to react to. This main path
       is computed over EXTERNAL edges only (``cluster_u != cluster_v``)
       - an INTERNAL (loop/connector) edge never participates here at
       all anymore (SPEC CHANGE from an older design that force-included
       it as a self-loop - see report/street_identity.md "Synthetic
       main-path connectors at a real intersection").
    5. **Physical reorientation** (``_reorient_main_path_edges``): every
       external main-path edge stored "backwards" relative to its own
       street_id's main-path walking order has its own raw osmid
       endpoints, clustered node_id, and geometry (coordinate order) all
       flipped together, in place, so its own persisted direction always
       agrees with the walk. See "Physical edge reorientation" in
       report/street_identity.md for how this interacts with osmnx's
       own directed-edge-pair convention.
    6. **Synthetic main-path connectors**
       (``_synthesize_main_path_connectors``): at every interior node_id
       a street_id's own (now-reoriented) external main path passes
       through, where the incoming and outgoing main-path edges touch two
       DIFFERENT raw osmids of that node_id, a brand-new straight
       ``LineString`` edge is synthesized bridging those two specific
       osmids, with a freshly allocated, globally unique ``(u, v, key)``
       index (see "New synthetic edge ids" in report/street_identity.md).
       This edge is what actually carries the main path's own physical
       distance through the intersection now - a REAL (pre-existing)
       internal edge at that same intersection, if there is one, is left
       completely untouched: its own street_id is still resolved
       normally (``_assign_internal_connector_edges``), but it is never
       main and its own OSM geometry is never replaced.
    7. **Turn-splitting** (``_split_main_path_at_turns``): once each
       street_id's own main path (external mains PLUS their own synthetic
       connectors from step 6, in true walking order) is final, it's
       walked in path order and split into two street_ids wherever two
       consecutive path members turn sharper than
       ``main_path_angle_threshold_deg`` - the same OSM identity/rank/
       name can still be two different physical through-lines for
       numbering purposes (e.g. a road that keeps its name across a
       near-right-angle corner). Since a synthetic connector is just
       another walkable path member with its own real (straight-line)
       bearing, the turn is measured independently at both of its own
       junctions (external-into-connector, connector-into-external) with
       no special-casing needed. Each edge's own direction-of-travel is
       worked out from whichever of its two endpoints actually matches
       the shared node in the main path's own walking order, not from its
       raw stored ``u``/``v`` (see ``_ordered_main_path``). Every
       non-main edge ("furniture" - a parallel duplicate, a spur, an
       unresolved connector) tracks its own street_id's nearest main-path
       edge (``nearest_main_eid``, computed once before splitting) and is
       reassigned along with it if that specific main-path edge moves to
       a new street_id here. A split street_id reuses its source's own
       name/rank/highway in ``streets_df`` - it's still physically the
       same road, only cut in two for numbering.
    8. **Full main-path connectivity validation**
       (``_validate_main_path_invariant``): a hard, unconditional
       internal-error check that every final street_id's own main-path
       edges (external mains plus their own synthetic connectors) form
       exactly one connected, simple path - no edge id repeated, no osmid
       ever touched more than twice (and only twice at a synthetic
       connector's own two endpoints), one connected node_id component.
       This should always hold given a correct implementation of steps
       4-7 above; a violation raises ``ValueError`` rather than silently
       returning invalid output.

    Args:
        G: The input street network graph (osmnx-style MultiDiGraph, in
            a projected CRS).
        angle_threshold_deg: Maximum folded angle difference (deg,
            undirected) between two edges' tangents at a shared node_id
            for them to be treated as "the same direction" there - used
            by both the per-node_id direction assignment and the
            internal-connector direction match.
        highway_ranks: Highway-type-to-rank mapping (default
            ``DEFAULT_HIGHWAY_RANKS``); lower rank number = higher
            priority for keeping identity across an intersection.
        node_cluster_m: Distance (meters, in the graph's own CRS units)
            within which two graph nodes are treated as the same real
            intersection - see "Node clustering" above.
        name_similarity_threshold: Minimum ``name_matching.name_similarity``
            score (0-100) for two named edges/street_ids to be
            considered the same street.
        parallel_dist_m: Maximum distance (meters) for the length-based
            reassignment's own 90%-rule step (step 4 above,
            ``_reassign_short_streets``) - the "near" threshold in
            "is at least ``non_drivable_coverage_fraction`` of this
            too-short street_id's own length within this distance of a
            valid street_id". A value <= 0 disables that step entirely
            (falls straight through to the direction-match-at-endpoint
            step instead).
        parallel_angle_threshold_deg: Maximum folded bearing difference
            (deg) between two edges' own center bearings for the
            parallel-grouping step (2a above) to consider them
            "parallel".
        non_drivable_highways: Highway types no car can drive on
            (default ``DEFAULT_NON_DRIVABLE_HIGHWAYS``). Used only to
            decide Pass 1 (drivable) vs Pass 2 (non-drivable) membership.
        private_access_values: OSM ``access`` tag values (default
            ``DEFAULT_PRIVATE_ACCESS_VALUES``) that mean an edge is a
            private access road/driveway - never gets a street_id, and
            excluded from every step above (so it can never bridge two
            real streets together just by touching both).
        parking_service_values: OSM ``service`` tag values (default
            ``DEFAULT_PARKING_SERVICE_VALUES``) that mean an edge is a
            parking-lot internal way - handled exactly like
            ``private_access_values`` above.
        min_street_length_m: Minimum length (meters) for a street_id's
            own main path to be considered valid - see step 4 above and
            ``_reassign_short_streets``. Default 50m.
        parallel_pass_touching_dist_m: Maximum central distance (meters
            - see ``_center_segment``) between two edges that already
            TOUCH (share a raw node) for the parallel-grouping step (2a
            above) to join them. Default 15m.
        parallel_pass_non_touching_dist_m: Same as
            ``parallel_pass_touching_dist_m``, but for two edges that do
            NOT touch at all - deliberately tighter. Default 5m.
        node_direction_tolerance_deg: Maximum folded angle difference
            (deg) between two external edges' own tangents AT A SHARED
            NODE_ID for them to be grouped into the same direction there
            (step 2b above), and for the length-based reassignment's own
            endpoint direction match (step 4 above). Deliberately looser
            than ``angle_threshold_deg`` - node geometry digitization
            noise is largest right at the node itself. Default 30 deg.
        non_drivable_coverage_fraction: Minimum fraction (0-1) of a
            non-drivable edge's own length that must fall within
            ``max(parallel_pass_touching_dist_m,
            parallel_pass_non_touching_dist_m)`` of the drivable network
            for it to be donated the nearby drivable street_id with the
            largest total nearby length, before the non-drivable pass
            runs its own assignment - see "Donating a drivable street's
            id to nearby non-drivable furniture". Default 0.9 (90%).
        num_node_directions: How many equal-width bins to split each
            node_id's own [0, 180) direction range into (default 6, i.e.
            30 deg each - 12 bins once the opposite/continuation
            direction is counted too) - see "No more than one direction
            per node_id".
        main_path_angle_threshold_deg: Turn deviation (deg, directed - 0
            means the main path keeps going perfectly straight, 180 means
            it fully reverses, see ``_raw_turn_deviation_deg``) between
            two consecutive main-path edges at a shared node_id beyond
            which a street_id is split into two - step 5 above. Kept
            separate from ``angle_threshold_deg``: that threshold decides
            whether two edges share an OSM identity across an
            intersection, a stricter question than whether an
            already-identified main path should still be numbered as one
            continuous line through a real corner. Default 90 deg only
            splits at a turn sharper than a right angle.
        tangent_continuation_fraction: Minimum fraction (0-1) of the
            LONGER edge's own length, within ``tangent_continuation_window_m``
            of the shared node, that must stay within
            ``parallel_pass_touching_dist_m`` of the SHORTER edge's own
            local tangent line (extended BOTH ways through the node) for
            step 2b's per-node_id direction assignment to still merge two
            edges whose whole-edge bearings disagree by more than
            ``node_direction_tolerance_deg`` - see
            ``_tangent_continuation_fraction``. Distinguishes a genuine
            gentle curve (most of the edge, near the node, stays close to
            the straight tangent) from a real fork (only a short stretch
            near the node does) even though both can look identical by
            local tangent alone.
        tangent_continuation_window_m: How far along the longer edge,
            from the shared node, ``tangent_continuation_fraction`` is
            measured over - the rest of a long edge is ignored entirely,
            so a real, long, gently curving street isn't penalized for
            drifting past a fixed corridor width only once accumulated
            over hundreds of meters.

    Returns:
        A tuple ``(nodes_gdf, edges_gdf, streets_df)``:

        - edges_gdf: original edges plus ``street_id`` (nullable int),
          ``street_name``, ``is_main_direction``, ``is_drivable`` (see
          ``non_drivable_highways`` above), ``structure_type``
          (``"tunnel"``, ``"bridge"``, or ``None``, resolved from OSM's
          separate ``tunnel``/``bridge`` tags), and ``nearest_main_eid``
          (nullable int - every non-main edge's own street_id's nearest
          main-path edge id, ``None`` for a main-path edge or one with no
          street_id - see step 7 above). Also includes every brand-new
          SYNTHETIC connector edge created in step 6 above, as additional
          rows keyed by a freshly allocated ``(u, v, key)`` tuple (real
          osmids, synthetic ``key`` - see "New synthetic edge ids" in
          report/street_identity.md); each has a straight-line
          ``geometry``/``length``, ``is_main_direction=True``,
          ``is_drivable=True``, ``structure_type=None``, and
          ``nearest_main_eid=None``. A REAL internal (loop/connector)
          edge's own ``geometry``/``length`` are left completely
          untouched (SPEC CHANGE - an older design replaced them with a
          straight line; that no longer happens, since a synthetic
          connector now carries that physical distance instead - see
          step 6 above). A main-path edge stored "backwards" relative to
          its own street_id's walking order has its own ``u``/``v``
          (index) and ``geometry`` (coordinate order) physically flipped
          - see step 5 above.
        - nodes_gdf: original nodes, plus ``node_id`` (see "Node
          clustering" above).
        - streets_df: one row per street_id with street_name and street_rank
          (a street_id split off by step 7 above reuses its source's own
          street_name/street_rank/highway).
    """
    ranks = highway_ranks or DEFAULT_HIGHWAY_RANKS
    non_drivable = non_drivable_highways or DEFAULT_NON_DRIVABLE_HIGHWAYS
    private_values = private_access_values or DEFAULT_PRIVATE_ACCESS_VALUES
    parking_values = parking_service_values or DEFAULT_PARKING_SERVICE_VALUES
    nodes_gdf, edges_gdf = ox.graph_to_gdfs(G)

    edges_gdf = edges_gdf.copy()
    name_col = (
        edges_gdf["name"]
        if "name" in edges_gdf.columns
        else pd.Series([None] * len(edges_gdf), index=edges_gdf.index, dtype=object)
    )
    edges_gdf["_clean_name"] = _vectorized_clean_name(name_col)
    edges_gdf["_rank"], edges_gdf["_highway"] = _vectorized_highway_rank_and_value(
        edges_gdf["highway"], ranks
    )
    # A missing/unrecognized highway value is treated permissively (drivable),
    # since assuming a real road is a footpath would be the worse mistake.
    edges_gdf["is_drivable"] = ~edges_gdf["_highway"].isin(non_drivable)

    tunnel_col = (
        edges_gdf["tunnel"]
        if "tunnel" in edges_gdf.columns
        else pd.Series([None] * len(edges_gdf), index=edges_gdf.index, dtype=object)
    )
    bridge_col = (
        edges_gdf["bridge"]
        if "bridge" in edges_gdf.columns
        else pd.Series([None] * len(edges_gdf), index=edges_gdf.index, dtype=object)
    )
    edges_gdf["structure_type"] = _vectorized_structure_type(tunnel_col, bridge_col)

    access_col = (
        edges_gdf["access"]
        if "access" in edges_gdf.columns
        else pd.Series([None] * len(edges_gdf), index=edges_gdf.index, dtype=object)
    )
    edges_gdf["_is_private"] = _vectorized_is_private(access_col, private_values)

    service_col = (
        edges_gdf["service"]
        if "service" in edges_gdf.columns
        else pd.Series([None] * len(edges_gdf), index=edges_gdf.index, dtype=object)
    )
    edges_gdf["_is_parking"] = _vectorized_is_parking(service_col, parking_values)

    node_to_cluster = _cluster_nodes(nodes_gdf, node_cluster_m)

    # Collapse to undirected topology: two-way streets appear as reciprocal
    # directed edges in the MultiDiGraph, but represent one physical street.
    edges_reset = edges_gdf.reset_index()
    u_arr = edges_reset["u"].to_numpy()
    v_arr = edges_reset["v"].to_numpy()
    edges_reset["_pair"] = list(
        zip(np.minimum(u_arr, v_arr), np.maximum(u_arr, v_arr), edges_reset["key"])
    )
    undirected = edges_reset.drop_duplicates("_pair").set_index("_pair")

    # Vectorized per-edge endpoint angles, computed once for every
    # undirected edge instead of re-walking each geometry's coordinate
    # sequence inside the node-building loop below.
    start_angle, end_angle, raw_start_bearing, raw_end_bearing = _vectorized_endpoint_angles(
        undirected.geometry.to_numpy()
    )
    rank_arr = undirected["_rank"].to_numpy()
    highway_arr = undirected["_highway"].to_numpy()
    name_arr = undirected["_clean_name"].to_numpy()
    length_arr = undirected["length"].to_numpy()
    is_drivable_arr = undirected["is_drivable"].to_numpy()
    is_private_arr = undirected["_is_private"].to_numpy()
    is_parking_arr = undirected["_is_parking"].to_numpy()
    # A private access road and a parking-lot internal way are excluded
    # from street_id assignment identically (see "Private access roads
    # never get a street_id" / the parking-aisle mirror of it in
    # report/street_identity.md) - combined into one exclusion mask so
    # every downstream use only needs to check one thing.
    is_excluded_arr = is_private_arr | is_parking_arr
    # Copies (not views) - _reorient_main_path_edges mutates these arrays
    # in place for main-path edges walked "backwards" relative to their
    # own stored direction (see "Physical edge reorientation" in
    # report/street_identity.md), which a read-only numpy view of the
    # underlying pandas column would reject.
    u_und = undirected["u"].to_numpy().copy()
    v_und = undirected["v"].to_numpy().copy()
    eids = undirected.index.to_numpy()
    all_geometries = undirected.geometry.to_numpy().copy()
    bearing_arr = np.array([_overall_bearing_deg(g) for g in all_geometries])
    center_segments = np.array([_center_segment(g) for g in all_geometries], dtype=object)

    cluster_u = np.array([node_to_cluster[u] for u in u_und])
    cluster_v = np.array([node_to_cluster[v] for v in v_und])

    # Two-pass assignment: Pass 1 resolves street_ids over the drivable
    # subnetwork alone, as if non-drivable edges didn't exist; Pass 2
    # resolves the non-drivable subnetwork the same way, separately. This
    # keeps a drivable road's rank-tier dominance from cutting a
    # genuinely distinct non-drivable path into fragments right where it
    # crosses that road (rule 2 is about real streets crossing each
    # other, not an unrelated footway crossing a road it has nothing to
    # do with). Private/parking edges (is_excluded_arr) participate in
    # neither pass.
    eligible_idx = np.flatnonzero(~is_excluded_arr)
    drivable_idx = eligible_idx[is_drivable_arr[eligible_idx]]
    non_drivable_idx = eligible_idx[~is_drivable_arr[eligible_idx]]

    street_id_arr = np.full(len(eids), -1, dtype=np.int64)
    next_id = 1
    # Every edge ever moved OFF the street_id its own direction/rank-
    # based assignment first gave it - by coverage-based donation, the
    # general cross-street_id 90% rule, or length-based reassignment
    # below - can never become (part of) its NEW street_id's own main
    # path: it was folded in purely for identity, not because it's
    # really part of that street's own through-line. See "Main path" in
    # report/street_identity.md.
    never_main_eids: set = set()

    # Must cover every OTHER step's own proximity threshold that can
    # legitimately produce a non-touching merge, including
    # parallel_dist_m (the length-based reassignment's own 90%-rule
    # threshold, step 6) - otherwise a merge one of those steps just
    # made could be immediately undone here, and since the connectivity/
    # branching invariants and length-based reassignment run together in
    # a fixed-point loop, that undoing would just get redone and undone
    # every iteration forever instead of ever reaching a fixed point (a
    # real bug found via real-data testing).
    connectivity_tolerance_m = max(
        parallel_pass_touching_dist_m or 0.0,
        parallel_pass_non_touching_dist_m or 0.0,
        parallel_dist_m or 0.0,
    )

    for pass_idx, is_drivable_pass in ((drivable_idx, True), (non_drivable_idx, False)):
        if len(pass_idx) == 0:
            continue

        roots = _assign_external_street_ids_for_pass(
            pass_idx, u_und, v_und, cluster_u, cluster_v, rank_arr, name_arr,
            bearing_arr, all_geometries, center_segments, start_angle, end_angle,
            parallel_pass_touching_dist_m, parallel_pass_non_touching_dist_m,
            parallel_angle_threshold_deg, node_direction_tolerance_deg, name_similarity_threshold,
            num_node_directions, tangent_continuation_fraction, tangent_continuation_window_m,
        )
        groups: dict[int, list[int]] = defaultdict(list)
        for i, root in roots.items():
            groups[root].append(i)

        for members in groups.values():
            sid = next_id
            next_id += 1
            for i in members:
                street_id_arr[i] = sid

        _assign_internal_connector_edges(
            pass_idx, u_und, v_und, cluster_u, cluster_v, all_geometries,
            start_angle, end_angle, name_arr, street_id_arr, angle_threshold_deg,
            name_similarity_threshold,
        )

    # Coverage-based donation (the 90% rule) runs AFTER the non-drivable
    # pass has fully resolved its own street_ids, changing some of them:
    # a non-drivable edge whose own length runs almost entirely near the
    # drivable network is reassigned that network's own best-matching
    # nearby street_id - see "Donating a drivable street's id to nearby
    # non-drivable furniture". Uses `parallel_pass_touching_dist_m` (the
    # LOOSER bound), not the tighter non-touching one: a sidewalk running
    # alongside a wide, multi-lane road is legitimately offset more than
    # the tight "two separate streets happen to be near each other" bound
    # - the same reason `parallel_pass_touching_dist_m` exists at all
    # (dual-carriageway-style offsets). A real bug found via real-data
    # testing: a 297m footway running the length of a 5-lane primary road
    # was only donated 23% coverage at the tight 10m bound (real offset
    # ~12-14m) but 100% at the looser touching bound.
    never_main_eids.update(
        eids[i]
        for i in _coverage_donate_non_drivable_edges(
            non_drivable_idx, drivable_idx, all_geometries, bearing_arr, name_arr, street_id_arr,
            parallel_pass_touching_dist_m, non_drivable_coverage_fraction,
            parallel_angle_threshold_deg, name_similarity_threshold,
        )
    )

    # The 90% rule, generalized to every resolved street_id (drivable or
    # non-drivable) against every other one: a street_id almost entirely
    # near another one has each of its own edges individually reassigned
    # to whichever specific neighbor it best matches. Two DIFFERENT,
    # not necessarily touching, street_ids - held to the tighter
    # "non-touching" distance bound, consistent with every other
    # non-touching proximity check in this module (phase 1's parallel
    # grouping, _edges_are_truly_parallel's own threshold choice, ...).
    coverage_threshold_m = parallel_pass_non_touching_dist_m
    never_main_eids.update(
        eids[i]
        for i in _coverage_reassign_overlapping_street_ids(
            np.flatnonzero(street_id_arr != -1), all_geometries, bearing_arr, name_arr,
            rank_arr, is_drivable_arr, street_id_arr,
            coverage_threshold_m, non_drivable_coverage_fraction, name_similarity_threshold,
            parallel_angle_threshold_deg,
        )
    )

    # No coverage-based step above ever checks what direction a moved
    # edge points at any node_id it now shares with its new street_id's
    # other edges - re-enforce "no more than one direction per node_id"
    # (rule 2's per-node_id invariant) over the FINAL composition,
    # splitting apart any street_id that ended up branching. Must run
    # before the connectivity invariant below: splitting off a branch
    # can itself leave what's left disconnected.
    street_id_arr, next_id = _split_branching_street_ids(
        eids, u_und, v_und, cluster_u, cluster_v, all_geometries, bearing_arr, start_angle, end_angle,
        street_id_arr, parallel_angle_threshold_deg, connectivity_tolerance_m,
        parallel_pass_touching_dist_m, num_node_directions, frozenset(never_main_eids), next_id,
    )

    # The connectivity invariant's own distance tolerance must cover the
    # parallel-grouping step's own thresholds too - otherwise a
    # legitimate parallel-grouping join could be immediately undone by
    # this check not recognizing it as a real connection.
    street_id_arr, next_id = _split_disconnected_components(
        eids, cluster_u, cluster_v, all_geometries, street_id_arr, connectivity_tolerance_m, next_id,
    )

    # Redistributing too-short streets, the branching invariant, and the
    # connectivity invariant run together as a small, bounded,
    # fixed-point loop: each step can strand a fragment, or create a new
    # branch or disconnection, that another step must get a chance to
    # react to - see "Redistributing too-short streets" in
    # report/street_identity.md.
    for _ in range(_MIN_STREET_LENGTH_MAX_ITERATIONS):
        before_iteration = street_id_arr.copy()

        is_main_by_eid, main_length_by_sid = _compute_main_direction(
            eids, cluster_u, cluster_v, length_arr, street_id_arr, is_drivable_arr,
            frozenset(never_main_eids),
        )
        street_id_arr, reassigned_eids = _reassign_short_streets(
            eids, u_und, v_und, cluster_u, cluster_v, all_geometries, name_arr,
            start_angle, end_angle, is_main_by_eid, main_length_by_sid,
            street_id_arr, min_street_length_m, node_direction_tolerance_deg,
            name_similarity_threshold, parallel_dist_m, non_drivable_coverage_fraction,
        )
        never_main_eids.update(reassigned_eids)

        street_id_arr, next_id = _split_branching_street_ids(
            eids, u_und, v_und, cluster_u, cluster_v, all_geometries, bearing_arr, start_angle, end_angle,
            street_id_arr, parallel_angle_threshold_deg, connectivity_tolerance_m,
            parallel_pass_touching_dist_m, num_node_directions, frozenset(never_main_eids), next_id,
        )

        street_id_arr, next_id = _split_disconnected_components(
            eids, cluster_u, cluster_v, all_geometries, street_id_arr, connectivity_tolerance_m, next_id,
        )

        if np.array_equal(street_id_arr, before_iteration):
            break

    # Minimum street length is the single most important invariant this
    # module enforces - it must hold unconditionally, even for a
    # genuinely isolated short fragment the loop above found no
    # direction- or coverage-compatible candidate for. Force-merge
    # whatever's still too short into its plain nearest valid street_id,
    # no distance cap, no direction/coverage requirement - deliberately
    # NOT followed by another branching/connectivity invariant pass, so
    # this final guarantee can never be undone.
    _, final_main_length_by_sid = _compute_main_direction(
        eids, cluster_u, cluster_v, length_arr, street_id_arr, is_drivable_arr,
        frozenset(never_main_eids),
    )
    street_id_arr, forced_eids = _force_reassign_remaining_short_streets(
        eids, all_geometries, name_arr, final_main_length_by_sid, street_id_arr,
        min_street_length_m, name_similarity_threshold,
    )
    never_main_eids.update(forced_eids)

    has_id = street_id_arr != -1

    # Representative attributes per final street_id: the member edge with
    # the highest priority (lowest street_rank, ties broken by the
    # smallest edge id) supplies the street's name/highway/rank, so a
    # street formed from a mix of edges (e.g. a dual-carriageway auto
    # merge) reports the most authoritative one rather than just
    # whichever edge happened to be encountered first.
    rep_key = {}
    for i, sid in enumerate(street_id_arr):
        if not has_id[i]:
            continue
        key = (rank_arr[i], eids[i])
        if sid not in rep_key or key < rep_key[sid][0]:
            rep_key[sid] = (key, i)

    # Per-edge street_name: every edge sharing a street_id gets that
    # street_id's own single representative name (same value
    # streets_df reports), never its own raw OSM name - an edge with no
    # OSM name of its own is named this way too (e.g. an unnamed
    # connector that got a name donated from a nearby named street via
    # the parallel-grouping step; see "Naming an unnamed road from a
    # nearby named one"). Two edges can share a street_id despite
    # carrying different literal OSM name strings (fuzzy name matching -
    # e.g. "Nationale 5" and "Douala-Bafoussam (N5)" - or rank-crossing
    # name-match-overrides-rank) - keeping each edge's own literal string
    # in that case would put two different ``street_name`` values under
    # one ``street_id``, breaking the invariant that every edge sharing a
    # street_id carries that street's own single name (a real gap found
    # via real-data testing).
    edge_street_id = street_id_arr
    edge_street_name = [
        name_arr[rep_key[sid][1]] if sid in rep_key else None
        for sid in edge_street_id
    ]

    eid_to_pos = {eid: i for i, eid in enumerate(eids)}
    edges_u = edges_gdf.index.get_level_values("u").to_numpy()
    edges_v = edges_gdf.index.get_level_values("v").to_numpy()
    edges_key = edges_gdf.index.get_level_values("key").to_numpy()
    pos = [
        eid_to_pos[(min(u, v), max(u, v), k)] for u, v, k in zip(edges_u, edges_v, edges_key)
    ]

    # Renumber street_id in street_rank order (highest priority/lowest rank
    # number first), so both streets.csv and the edges themselves list/sort
    # by rank. Ties broken by the original (first-seen) id, for a
    # deterministic, reproducible ordering. Edges without a street_id
    # (unresolved connectors) stay without one.
    real_ids = sorted(rep_key.keys(), key=lambda sid: (rank_arr[rep_key[sid][1]], sid))
    old_to_new = {old_id: new_id for new_id, old_id in enumerate(real_ids, start=1)}
    edge_street_id = np.array(
        [old_to_new[sid] if sid in old_to_new else -1 for sid in edge_street_id]
    )

    # Rule 3: exactly one main path per final street_id, computed strictly
    # after every other street_id assignment step above has finished.
    # excluded_from_main carries forward every edge ever moved off its
    # own original street_id above (coverage-based donation, the
    # general cross-street_id 90% rule, or length-based reassignment),
    # so none of them can be chosen as (part of) their new street_id's
    # own main path here either. See _compute_main_direction.
    main_direction_map, _final_main_length_by_sid = _compute_main_direction(
        eids, cluster_u, cluster_v, length_arr, edge_street_id, is_drivable_arr,
        frozenset(never_main_eids),
    )
    is_main_final = np.array([main_direction_map.get(eid, True) for eid in eids], dtype=bool)

    # Physical reorientation: for every street_id's own ordered external
    # main path (see "Physical edge reorientation" in
    # report/street_identity.md), make sure each main-path edge's own
    # ARRAY-level u/v (raw osmid) and clustered node_id agree with the
    # walk direction - if an edge is stored "backwards" relative to the
    # walk (its own cluster_v is the node the walk arrives FROM), its u/v,
    # clustered node_id, and geometry (coordinate order) are all swapped
    # together here, in place, over the arrays used for everything below
    # (synthetic-connector construction, turn-splitting, and the final
    # edges_gdf columns). `reoriented_mask` records which undirected
    # edges were flipped, so the persisted edges_gdf rows for those
    # physical edges can be kept consistent with it further down.
    reoriented_mask = _reorient_main_path_edges(
        eids, cluster_u, cluster_v, u_und, v_und, all_geometries, edge_street_id, is_main_final,
    )
    # Bearings/tangent angles depend on geometry's own coordinate order -
    # recomputed only for the (normally few) reoriented edges rather than
    # over the whole array.
    if reoriented_mask.any():
        r_idx = np.flatnonzero(reoriented_mask)
        r_start, r_end, r_raw_start, r_raw_end = _vectorized_endpoint_angles(all_geometries[r_idx])
        start_angle[r_idx] = r_start
        end_angle[r_idx] = r_end
        raw_start_bearing[r_idx] = r_raw_start
        raw_end_bearing[r_idx] = r_raw_end

    # Synthesize the NEW straight-line internal connectors every
    # street_id's own main path needs at a real intersection with more
    # than one distinct raw osmid - see "Synthetic main-path connectors
    # at a real intersection" in report/street_identity.md. Built from
    # the external-only main path just computed above, BEFORE
    # turn-splitting, so the connectors themselves participate in the
    # ordered walk/turn measurement below exactly like a real external
    # edge.
    node_geometry = nodes_gdf.geometry
    connectors = _synthesize_main_path_connectors(
        eids, cluster_u, cluster_v, u_und, v_und, is_main_final, edge_street_id, node_geometry,
    )

    n_orig = len(eids)
    synthetic_keys = [
        _SYNTHETIC_CONNECTOR_KEY_OFFSET + k for k in range(len(connectors))
    ]
    synthetic_eids = np.empty(len(connectors), dtype=object)
    for _k, (c, key) in enumerate(zip(connectors, synthetic_keys)):
        synthetic_eids[_k] = (min(c["u"], c["v"]), max(c["u"], c["v"]), key)
    if len(connectors):
        syn_u = np.array([c["u"] for c in connectors])
        syn_v = np.array([c["v"] for c in connectors])
        syn_geoms = np.array([c["geometry"] for c in connectors], dtype=object)
        syn_lengths = np.array([c["length"] for c in connectors])
        syn_sid = np.array([c["street_id"] for c in connectors], dtype=np.int64)
        syn_cluster = np.array([node_to_cluster[c["u"]] for c in connectors])
        syn_start, syn_end, syn_raw_start, syn_raw_end = _vectorized_endpoint_angles(syn_geoms)
        syn_bearing = np.array([_overall_bearing_deg(g) for g in syn_geoms])

        eids = np.concatenate([eids, synthetic_eids])
        cluster_u = np.concatenate([cluster_u, syn_cluster])
        cluster_v = np.concatenate([cluster_v, syn_cluster])
        u_und = np.concatenate([u_und, syn_u])
        v_und = np.concatenate([v_und, syn_v])
        all_geometries = np.concatenate([all_geometries, syn_geoms])
        length_arr = np.concatenate([length_arr, syn_lengths])
        is_drivable_arr = np.concatenate([is_drivable_arr, np.ones(len(connectors), dtype=bool)])
        bearing_arr = np.concatenate([bearing_arr, syn_bearing])
        start_angle = np.concatenate([start_angle, syn_start])
        end_angle = np.concatenate([end_angle, syn_end])
        raw_start_bearing = np.concatenate([raw_start_bearing, syn_raw_start])
        raw_end_bearing = np.concatenate([raw_end_bearing, syn_raw_end])
        edge_street_id = np.concatenate([edge_street_id, syn_sid])
        is_main_final = np.concatenate([is_main_final, np.ones(len(connectors), dtype=bool)])

    # Rule 3 continued - "furniture" tracking, computed BEFORE
    # turn-splitting so the cascade below has something to key off (see
    # _compute_nearest_main_eid and "Split a street_id at a sharp turn on
    # its own main path" in report/street_identity.md).
    nearest_main_eid_map = _compute_nearest_main_eid(
        eids, edge_street_id, is_main_final, all_geometries
    )

    # Split a street_id wherever its own main path turns sharper than
    # main_path_angle_threshold_deg - the same "same OSM identity,
    # different physical through-line" situation the rest of this module
    # already treats as two streets, just discovered only now that the
    # main path itself is finalized. Every non-main edge that sits
    # nearest a main-path edge which moves to a new piece here follows
    # it, via nearest_main_eid_map above. Deliberately its own threshold,
    # not angle_threshold_deg - see label_streets' own docstring. Turn
    # angle is measured at BOTH junctions around a synthetic connector
    # (external-into-synthetic, then synthetic-into-external) for free,
    # since a synthetic connector is just another walkable edge in the
    # sequence with its own real bearing - see "Turn-splitting around a
    # synthetic connector" in report/street_identity.md.
    edge_street_id, split_from, next_id = _split_main_path_at_turns(
        eids, cluster_u, cluster_v, raw_start_bearing, raw_end_bearing,
        edge_street_id, is_main_final, nearest_main_eid_map, main_path_angle_threshold_deg,
        len(real_ids) + 1,
    )

    _validate_main_path_invariant(eids, u_und, v_und, cluster_u, cluster_v, edge_street_id, is_main_final)

    edges_gdf = edges_gdf.drop(
        columns=["_clean_name", "_rank", "_highway", "_is_private", "_is_parking"]
    )

    # Physically persist the reorientation computed above: every directed
    # edges_gdf row (osmnx gives a two-way street two rows, u->v and
    # v->u - see "Physical edge reorientation" in
    # report/street_identity.md) belonging to a reoriented undirected
    # position gets its own u/v swapped and its own geometry's coordinate
    # order reversed, so its own stored direction always agrees with the
    # actual main-path walking direction. For a two-way edge this keeps
    # both directed rows mutually consistent (each individually flipped);
    # for a one-way edge (only one directed row exists in the source
    # graph) this bakes in a direction the main path actually needs to
    # walk it in, which OSM's own `oneway` tag never separately recorded
    # here - the module carries no other driving-direction semantics, so
    # this is judged an acceptable, clearly-documented trade-off.
    if reoriented_mask.any():
        eid_to_reoriented = {tuple(eids[:n_orig][i]): True for i in np.flatnonzero(reoriented_mask)}
        new_edges_u = edges_u.copy()
        new_edges_v = edges_v.copy()
        new_geoms_for_reorient = edges_gdf.geometry.to_numpy().copy()
        for k, i in enumerate(pos):
            key = (int(min(edges_u[k], edges_v[k])), int(max(edges_u[k], edges_v[k])), int(edges_key[k]))
            if eid_to_reoriented.get(key):
                new_edges_u[k], new_edges_v[k] = edges_v[k], edges_u[k]
                new_geoms_for_reorient[k] = shapely.LineString(list(new_geoms_for_reorient[k].coords)[::-1])
        edges_gdf = edges_gdf.reset_index()
        edges_gdf["u"] = new_edges_u
        edges_gdf["v"] = new_edges_v
        edges_gdf["geometry"] = gpd.GeoSeries(new_geoms_for_reorient, crs=edges_gdf.crs)
        edges_gdf = edges_gdf.set_index(["u", "v", "key"])
        edges_u, edges_v, edges_key = new_edges_u, new_edges_v, edges_key

    edges_gdf["street_id"] = pd.array(
        [int(edge_street_id[i]) if edge_street_id[i] != -1 else None for i in pos], dtype="Int64"
    )
    edges_gdf["street_name"] = pd.Series(
        [edge_street_name[i] if i < n_orig else None for i in pos], index=edges_gdf.index, dtype=object
    )
    edges_gdf["is_main_direction"] = pd.Series(
        [bool(is_main_final[i]) for i in pos], index=edges_gdf.index, dtype=bool
    )
    # nearest_main_eid's own value is this module's internal undirected
    # edge id - the (u, v, key) triple identifying an edge irrespective of
    # OSM direction (see ``eids`` above) - not a plain integer, so it's
    # kept as a plain object column rather than a nullable Int64 one.
    edges_gdf["nearest_main_eid"] = pd.Series(
        [nearest_main_eid_map.get(eids[i]) for i in pos], index=edges_gdf.index, dtype=object
    )

    # Append the synthesized connectors themselves as brand-new edges_gdf
    # rows - each one's own street_name/street_rank/highway come from
    # whichever final street_id it ended up under (post turn-split),
    # exactly like any other member of that street. street_name itself is
    # backfilled below, once ``final_attrs`` (which already knows how to
    # resolve a split-off street_id's own name back to its source) exists.
    if connectors:
        syn_rows = {
            "u": [int(u_und[i]) for i in range(n_orig, len(eids))],
            "v": [int(v_und[i]) for i in range(n_orig, len(eids))],
            "key": synthetic_keys,
            "geometry": [all_geometries[i] for i in range(n_orig, len(eids))],
            "length": [float(length_arr[i]) for i in range(n_orig, len(eids))],
            "street_id": pd.array(
                [int(edge_street_id[i]) if edge_street_id[i] != -1 else None for i in range(n_orig, len(eids))],
                dtype="Int64",
            ),
            "street_name": [None] * len(connectors),
            "is_main_direction": [True] * len(connectors),
            "is_drivable": [True] * len(connectors),
            "structure_type": [None] * len(connectors),
            "nearest_main_eid": [None] * len(connectors),
        }
        syn_gdf = gpd.GeoDataFrame(syn_rows, geometry="geometry", crs=edges_gdf.crs)
        syn_gdf = syn_gdf.set_index(["u", "v", "key"])
        for col in edges_gdf.columns:
            if col not in syn_gdf.columns:
                syn_gdf[col] = None
        syn_gdf = syn_gdf[edges_gdf.columns]
        edges_gdf = pd.concat([edges_gdf, syn_gdf])

    # Every OSM way (osmid) that ended up under each street_id - the map
    # you need to go tag those ways with a real name later (see
    # report/upload_street_names_OSM.md). osmid itself can be a list when
    # osmnx has merged parallel/duplicate ways, so it's flattened here.
    # Edges without a street_id (unresolved connectors) don't contribute.
    # Uses the post-split street_id so a newly split-off piece's own
    # osmids are attributed to it, not to the original it split from.
    street_to_osmids = defaultdict(set)
    for sid, osmid in zip(edge_street_id, undirected["osmid"].to_numpy()):
        if sid == -1:
            continue
        if isinstance(osmid, (list, tuple, set)):
            street_to_osmids[sid].update(osmid)
        else:
            street_to_osmids[sid].add(osmid)

    # A split-off street_id has no representative edge of its own picked
    # by rank (rep_key, above, ran before splitting existed) - it reuses
    # its source street_id's own name/rank/highway instead, since it's
    # still physically the same road, just cut in two for numbering.
    final_attrs: dict[int, tuple] = {}
    for old_id, new_id in old_to_new.items():
        i = rep_key[old_id][1]
        final_attrs[new_id] = (edge_street_name[i], rank_arr[i], highway_arr[i])
    for split_sid, source_sid in split_from.items():
        final_attrs[split_sid] = final_attrs[source_sid]

    if connectors:
        syn_street_id_col = edges_gdf["street_id"].to_numpy()
        syn_name_col = edges_gdf["street_name"].to_numpy(dtype=object).copy()
        syn_mask = edges_gdf["street_name"].isna().to_numpy() & pd.notna(syn_street_id_col)
        for k in np.flatnonzero(syn_mask):
            sid = int(syn_street_id_col[k])
            if sid in final_attrs:
                syn_name_col[k] = final_attrs[sid][0]
        edges_gdf["street_name"] = syn_name_col

    final_ids = sorted(final_attrs.keys())
    streets_df = pd.DataFrame(
        {
            "street_id": final_ids,
            "street_name": pd.Series(
                [final_attrs[sid][0] for sid in final_ids], dtype=object
            ),
            "street_rank": [final_attrs[sid][1] for sid in final_ids],
            "highway": pd.Series(
                [final_attrs[sid][2] for sid in final_ids], dtype=object
            ),
            "osmids": [
                ";".join(str(o) for o in sorted(street_to_osmids[new_sid]))
                for new_sid in final_ids
            ],
        }
    )

    # node_id: each node's own CLUSTERED id (see "Node clustering" -
    # _cluster_nodes) - the cluster leader's own raw osmid, distinct from
    # this row's own index (its own raw osmid). Every node in the same
    # real-world intersection shares one node_id even if OSM mapped it as
    # several distinct nodes a few meters apart, which is what lets a
    # downstream consumer group edges/nodes by real intersection the same
    # way this module's own node-touching decisions already do.
    nodes_gdf = nodes_gdf.copy()
    nodes_gdf["node_id"] = [node_to_cluster[n] for n in nodes_gdf.index]

    return nodes_gdf, edges_gdf, streets_df
