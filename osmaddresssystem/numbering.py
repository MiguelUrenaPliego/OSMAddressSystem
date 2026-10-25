"""House-number generation and reverse geocoding on top of labeled,
directed streets.

``street_ids.label_streets`` answers "which edges belong to which
street?". ``street_direction.assign_street_directionality`` answers
"which way do the numbers go, and how far along is a given point?".
This module turns that cumulative distance into an actual house number,
attaches administrative columns (country/province/city/neighbourhood)
to the streets table, and answers the reverse question: given a point,
what's its address?
"""

from __future__ import annotations

import math
from typing import Iterable

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, Polygon, MultiPolygon

from . import utils
from .default_values import DEFAULT_ADMIN_LEVELS, DEFAULT_NUMBER_DIVISOR_M

# One representative Nominatim reverse zoom per admin column, from
# utils._BOUNDARY_ZOOMS/_ZOOM_LEVEL_NAMES (see report/georanks.md).
# add_admin_columns picks exactly one zoom per polygon (see below), so
# this only needs one zoom per column rather than the full cascade
# reverse_geocode_boundary itself falls back through.
_ADMIN_COLUMN_TO_ZOOM = {
    "country": 3,
    "province": 5,
    "city": 10,
    "neighbourhood": 16,
}


def _house_number_from_distance(
    distance_m: float, number_divisor_m: float, side: str
) -> int:
    """Turn a cumulative distance and a side of the street into a house
    number: even on the right, odd on the left, per "Left/right
    numbering" in report/street_numbering.md.

    ``round(distance_m / number_divisor_m)`` (halves rounded up) picks a base number common
    to both sides, then that base is nudged to the nearest number of the
    required parity for ``side`` (never down past the minimum for that
    side - see below). This means each side's own numbers climb
    ``number_divisor_m`` apart along the street, exactly like a real
    address scheme's two interleaved (evens/odds) sequences.

    Args:
        distance_m: Cumulative distance along the street, in meters.
        number_divisor_m: Meters of cumulative length per house number
            step, per side.
        side: ``"left"`` (odd numbers) or ``"right"`` (even numbers).

    Returns:
        The house number: always even and ``>= 2`` for ``"right"``,
        always odd and ``>= 1`` for ``"left"``.

    Raises:
        ValueError: If ``side`` isn't ``"left"`` or ``"right"``.
    """
    if side not in ("left", "right"):
        raise ValueError(f"side must be 'left' or 'right', got {side!r}")

    # Half up (not Python's round-half-even) so this agrees exactly with the browser's Math.round
    # used by the geocode map's JavaScript.
    base = int(math.floor(distance_m / number_divisor_m + 0.5))
    wants_even = side == "right"
    if (base % 2 == 0) != wants_even:
        base += 1
    minimum = 2 if wants_even else 1
    return max(minimum, base)


def number_letter(index: int) -> str:
    """Letter suffix for the ``index``-th item (0-based) of a crowded number slot.

    Args:
        index: 0 for ``A``, 1 for ``B``, ... 25 for ``Z``, 26 for ``AA``.

    Returns:
        The letter(s).
    """
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def add_letters(numbers: list[int], slots: list, order: list) -> list[str]:
    """Number labels with a letter where several items share a number slot.

    Items with the same ``slots`` value (e.g. street, side and number) are
    ordered by ``order`` (position along the street, ties by input order) and
    labelled ``12A``, ``12B``, ...; an item alone in its slot keeps its plain
    number (``12``).

    Args:
        numbers: House or road number of each item.
        slots: Hashable slot key of each item; items with equal keys collide.
        order: Sort key of each item within its slot (e.g. distance along the street).

    Returns:
        One label per item, in input order.
    """
    groups: dict = {}
    for i, key in enumerate(slots):
        groups.setdefault(key, []).append(i)
    labels = [str(n) for n in numbers]
    for members in groups.values():
        if len(members) > 1:
            for rank, i in enumerate(sorted(members, key=lambda k: (order[k], k))):
                labels[i] = f"{numbers[i]}{number_letter(rank)}"
    return labels


def profile_position(profile, distance_along: float) -> float:
    """Numbering position of a point from its distance along the street line.

    In path mode the position along a street is not its length but the change of the shortest
    network distance to the reference, known at each node: ``profile`` lists
    ``[length along the street, position]`` per node and the position is interpolated linearly.

    Args:
        profile: ``[[along, position], ...]`` sorted by ``along`` (or a JSON string of it), or
            None/empty to use ``distance_along`` unchanged.
        distance_along: Distance along the street line from its start, in metres.

    Returns:
        The numbering position in metres.
    """
    import json

    if profile is None or (isinstance(profile, float) and pd.isna(profile)) or profile == "":
        return distance_along
    pts = json.loads(profile) if isinstance(profile, str) else profile
    if not pts:
        return distance_along
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if distance_along <= xs[0]:
        return ys[0]
    if distance_along >= xs[-1]:
        return ys[-1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= distance_along <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (distance_along - x0) / (x1 - x0)
    return distance_along


def _side_of_line(point: Point, line) -> str:
    """Which side of ``line`` (already oriented in its own numbering
    direction - see street_direction.assign_street_directionality)
    ``point`` falls on, looking in the direction of travel from the
    line's own start toward its end.

    Found at ``point``'s own nearest segment of ``line`` (the same
    segment ``line.project(point)`` would implicitly use), via the
    cross product of that segment's own direction vector and the vector
    from the segment to ``point``: positive means ``point`` is to the
    LEFT of the direction of travel, negative means RIGHT (standard
    right-handed x=east/y=north convention - facing east, north is to
    your left). A point exactly on the line (cross product 0) is
    arbitrarily called "right" - deterministic, though not meaningful,
    since a point truly on the street's own centerline isn't really on
    either side.

    Args:
        point: The point to classify.
        line: The street's own (main-path) line geometry, oriented start
            to end in its own numbering direction.

    Returns:
        ``"left"`` or ``"right"``.
    """
    coords = list(line.coords)
    best_dist = None
    best_side = "right"
    for (ax, ay), (bx, by) in zip(coords, coords[1:]):
        dx, dy = bx - ax, by - ay
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq == 0:
            continue
        t = ((point.x - ax) * dx + (point.y - ay) * dy) / seg_len_sq
        t = max(0.0, min(1.0, t))
        nx, ny = ax + t * dx, ay + t * dy
        dist = math.hypot(point.x - nx, point.y - ny)
        if best_dist is None or dist < best_dist:
            px, py = point.x - nx, point.y - ny
            cross = dx * py - dy * px
            best_side = "left" if cross > 0 else "right"
            best_dist = dist
    return best_side


def assign_house_numbers(
    edges_gdf: gpd.GeoDataFrame,
    streets_df: pd.DataFrame,
    number_divisor_m: float = DEFAULT_NUMBER_DIVISOR_M,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Turn cumulative street length into left/right house numbers.

    Must run after ``street_direction.assign_street_directionality``,
    which adds ``street_start_length_m``/``street_end_length_m`` to
    ``edges_gdf``. A real address scheme numbers the two sides of a
    street separately - even on the right, odd on the left, looking in
    the direction of travel (see "Left/right numbering" in
    report/street_numbering.md) - and an edge's own geometry doesn't by
    itself say which side any particular building sits on, so this adds
    BOTH sides' own numbers at both ends of every edge:
    ``house_number_start_left``/``_right`` and
    ``house_number_end_left``/``_right``. A caller with an actual point
    (not just an edge) should use ``geocode_points``/``geocode_point``
    instead, which detects the correct side directly (see
    ``_side_of_line``) rather than needing both.

    Args:
        edges_gdf: Edge GeoDataFrame with ``street_start_length_m``/
            ``street_end_length_m`` columns already set.
        streets_df: One row per ``street_id``, from ``label_streets``/
            ``assign_street_directionality``. Passed through unchanged;
            accepted for API symmetry with the rest of the pipeline.
        number_divisor_m: Meters of cumulative length per house number
            step, per side.

    Returns:
        A tuple ``(edges_gdf, streets_df)``: ``edges_gdf`` gains
        ``house_number_start_left``/``_right`` and
        ``house_number_end_left``/``_right`` (nullable integer columns,
        ``None`` wherever ``street_start_length_m``/
        ``street_end_length_m`` are ``None``). ``streets_df`` is
        returned unchanged, as a copy.
    """
    edges_gdf = edges_gdf.copy()

    def _to_number(length_m, side):
        """Convert a distance along the street to a house number.

        Args:
            length_m: Distance in metres from the street origin, or NaN.
            side: Side of the street (left/right).

        Returns:
            The house number, or None when the distance is missing.
        """
        if pd.isna(length_m):
            return None
        return _house_number_from_distance(length_m, number_divisor_m, side)

    for length_col, number_prefix in (
        ("street_start_length_m", "house_number_start"),
        ("street_end_length_m", "house_number_end"),
    ):
        for side in ("left", "right"):
            numbers = edges_gdf[length_col].map(lambda v, side=side: _to_number(v, side))
            # .map on a column with NaNs upcasts a plain None back to
            # float NaN; force the object dtype back so missing values
            # are real Python None, matching street_start_length_m/
            # street_end_length_m's own null convention exactly (see
            # assign_street_directionality).
            edges_gdf[f"{number_prefix}_{side}"] = numbers.astype(object).where(
                numbers.notna(), None
            )

    return edges_gdf, streets_df.copy()


def add_admin_columns(
    streets_df: pd.DataFrame,
    subregion_polygons: dict[str, Polygon | MultiPolygon],
    admin_levels: tuple[str, ...] = DEFAULT_ADMIN_LEVELS,
) -> pd.DataFrame:
    """Attach country/province/city/neighbourhood columns to a streets table.

    ``subregion_polygons`` is the chain of polygons a caller already has
    on hand from building the AOI/subregion (e.g. ``{"country": ...,
    "province": ..., "town": aoi_geom, "neighbourhood": subregion_geom}``,
    keys are only used for logging). The polygons are ranked by their own
    area, coarsest (largest) first, and paired up with ``admin_levels``
    in the same coarsest-first order: the largest polygon is assumed to
    be (at least as coarse as) ``admin_levels[0]``, the next largest
    ``admin_levels[1]``, and so on. A polygon's own centroid is then
    reverse-geocoded (``utils.reverse_geocode_boundary``) at exactly the
    zoom that corresponds to the level it was paired with (see
    ``_ADMIN_COLUMN_TO_ZOOM``), rather than the usual finest-to-coarsest
    cascade.

    Ranking by area first, rather than reverse-geocoding every polygon's
    centroid at every zoom and keeping whichever result happens to match
    a still-empty column, avoids a real failure mode: a country- or
    province-sized polygon's own centroid usually sits inside some
    unrelated small neighbourhood or village, since a large polygon's
    centroid is just an arbitrary point somewhere inside it. Searching
    that centroid with the usual finest-first cascade would happily
    return that unrelated small place and silently overwrite whichever
    column matches ITS level. Restricting each polygon to the one zoom
    that matches its own already-known rank avoids that entirely.

    If a level's own reverse-geocode attempt returns nothing (e.g. no
    boundary is mapped at exactly that zoom for that polygon), the
    column is left ``None`` rather than falling back to a different
    zoom, which could otherwise reintroduce the same mismatch this
    ranking is meant to avoid.

    Args:
        streets_df: The streets table to attach columns to.
        subregion_polygons: Mapping of an arbitrary label to a polygon
            somewhere in the AOI/subregion chain (country-level down to
            subregion geometry). Extra polygons beyond ``len(admin_levels)``
            are ignored (the smallest ones, i.e. the finest levels
            already covered by ``admin_levels[-1]``).
        admin_levels: Which admin columns to create, coarsest first.

    Returns:
        A copy of ``streets_df`` with ``admin_levels`` columns added,
        each a constant string (or ``None``) for every row, since a
        single streets table always belongs to one administrative chain.
    """
    streets_df = streets_df.copy()
    resolved: dict[str, str] = {}

    polygons = [p for p in subregion_polygons.values() if p is not None and not p.is_empty]
    polygons.sort(key=lambda p: p.area, reverse=True)

    for level, polygon in zip(admin_levels, polygons):
        zoom = _ADMIN_COLUMN_TO_ZOOM.get(level)
        if zoom is None:
            continue
        centroid = polygon.centroid
        result = utils.reverse_geocode_boundary(centroid.y, centroid.x, zooms=(zoom,))
        if result is None:
            continue
        _level, _zoom, name, _geom = result
        resolved[level] = name

    for column in admin_levels:
        streets_df[column] = resolved.get(column)

    return streets_df


def _project_point_onto_street(
    point: Point, street_geom, number_divisor_m: float
) -> tuple[float, str, int]:
    """Distance-along, side, and house number of ``point`` projected
    onto ``street_geom``.

    Always detects which side of the street ``point`` falls on (see
    ``_side_of_line``) and returns the number for THAT side - a
    real address's house number depends on which side of the street
    it's actually on, never both.

    Args:
        point: The point to project.
        street_geom: The street's own (main-path) line geometry.
        number_divisor_m: Meters of cumulative length per house number,
            per side.

    Returns:
        A tuple ``(distance_along_m, side, house_number)``, ``side``
        being ``"left"`` or ``"right"``.
    """
    distance_along = street_geom.project(point)
    side = _side_of_line(point, street_geom)
    house_number = _house_number_from_distance(distance_along, number_divisor_m, side)
    return distance_along, side, house_number


def geocode_point(
    streets_gdf: gpd.GeoDataFrame,
    point: tuple[float, float],
    number_divisor_m: float = DEFAULT_NUMBER_DIVISOR_M,
) -> str:
    """Reverse-geocode a single point to a formatted address string.

    Convenience wrapper around ``geocode_points`` for one point.

    Args:
        streets_gdf: The streets GeoDataFrame (main-path edges only,
            with admin columns from ``add_admin_columns``), in a
            projected CRS.
        point: An ``(x, y)`` point, in ``streets_gdf``'s own CRS.
        number_divisor_m: Meters of cumulative length per house number.

    Returns:
        The formatted address string. See ``geocode_points``.
    """
    return geocode_points(streets_gdf, [point], number_divisor_m=number_divisor_m)[0]


def geocode_points(
    streets_gdf: gpd.GeoDataFrame,
    points: Iterable[tuple[float, float]],
    number_divisor_m: float = DEFAULT_NUMBER_DIVISOR_M,
    letter_duplicates: bool = True,
) -> list[str]:
    """Reverse-geocode points to formatted address strings.

    For each point, finds the nearest street in ``streets_gdf`` (via its
    spatial index), projects the point onto that street's geometry to
    get a distance along it, detects which side of the street the point
    falls on (looking in the direction of travel - see
    ``_side_of_line``), and formats
    ``"{number}, {street label}, {neighbourhood}, {city}, {province},
    {country}"`` using that side's own house number - even on the
    right, odd on the left (see "Left/right numbering" in
    report/street_numbering.md). The street label is ``street_name`` if
    the street has one, otherwise ``"street {street_id}"``. Any admin
    column (``neighbourhood``/``city``/``province``/``country``) that is
    ``None`` or missing is skipped rather than printed as an empty or
    "None" segment.

    Args:
        streets_gdf: The streets GeoDataFrame (main-path edges only,
            with admin columns from ``add_admin_columns``), in a
            projected CRS, indexed however the caller likes.
        points: ``(x, y)`` points, in ``streets_gdf``'s own CRS.
        number_divisor_m: Meters of cumulative length per house number.
        letter_duplicates: If True (default), points of this call that fall into the same
            street, side and house number get a letter after the number (``12A``,
            ``12B``), ordered by distance along the street.

    Returns:
        One formatted address string per input point, in order.

    Raises:
        ValueError: If ``streets_gdf`` is empty.
    """
    if streets_gdf.empty:
        raise ValueError("streets_gdf is empty, nothing to geocode against")

    admin_columns = [
        c for c in ("neighbourhood", "city", "province", "country") if c in streets_gdf.columns
    ]

    sindex = streets_gdf.sindex
    rows = []

    for x, y in points:
        point = Point(x, y)
        nearest_pos = sindex.nearest(point, return_all=False)[1][0]
        street_row = streets_gdf.iloc[nearest_pos]

        distance_along, side, house_number = _project_point_onto_street(
            point, street_row.geometry, number_divisor_m
        )
        profile = street_row.get("path_profile")
        if profile is not None and not (isinstance(profile, float) and pd.isna(profile)) and profile != "":
            distance_along = profile_position(profile, distance_along)
            house_number = _house_number_from_distance(distance_along, number_divisor_m, side)

        street_name = street_row.get("street_name")
        if street_name is None or (isinstance(street_name, float) and pd.isna(street_name)) or street_name == "":
            label = f"street {street_row.get('street_id')}"
        else:
            label = street_name

        tail = [label]
        for column in admin_columns:
            value = street_row.get(column)
            if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
                continue
            tail.append(str(value))

        rows.append((house_number, (nearest_pos, side, house_number), distance_along, tail))

    numbers = [r[0] for r in rows]
    labels = (
        add_letters(numbers, [r[1] for r in rows], [r[2] for r in rows])
        if letter_duplicates
        else [str(n) for n in numbers]
    )
    return [", ".join([lab, *r[3]]) for lab, r in zip(labels, rows)]
