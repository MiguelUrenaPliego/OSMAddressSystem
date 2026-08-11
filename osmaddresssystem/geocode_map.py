"""Click-to-geocode interactive folium map.

``numbering.geocode_points`` answers "what's the address of this point?"
in Python, against a projected ``streets_gdf``. This module renders the
same lookup as a single, static HTML page: every street is drawn on a
Google Hybrid basemap (colored/weighted by its own highway importance
tier, labeled with its own name/id), and clicking anywhere computes the
nearest street, projects the click onto it (marked with a red dot),
detects which side of the street the click falls on, derives that
side's own house number - even on the right, odd on the left, looking
in the direction of travel, see ``numbering``'s own "Left/right
numbering" - and shows the formatted address - entirely in the browser,
no server involved, matching the offline design of ``street_editor.py``.

The browser has no access to ``streets_gdf``'s own projected CRS or to
``shapely``, so the nearest-street/project-onto-line math is
reimplemented in plain JS, working in an approximate local metric frame
(an equirectangular projection centered on the map's own data, accurate
to well under a meter across one town/subregion's extent) rather than
lat/lon degrees directly. This is a deliberate approximation - close
enough that a number rounded to the nearest ``number_divisor_m`` almost
always matches the Python backend's own answer for the same point, but
not bit-exact, since it isn't running the same projected CRS the real
pipeline uses. Good enough for an interactive demo, not a substitute for
``numbering.geocode_points`` itself.
"""

from __future__ import annotations

import json
import math

import folium
import geopandas as gpd
import pandas as pd
from branca.element import Element
from shapely.geometry import Point

from .default_values import DEFAULT_HIGHWAY_RANKS, DEFAULT_NUMBER_DIVISOR_M, DEFAULT_RANK
from .street_editor import _line_coord_lists, _new_map

_EARTH_RADIUS_M = 6_371_000.0

# Color/weight per DEFAULT_HIGHWAY_RANKS rank (0 = motorway/trunk, most
# important, down to 5 = footway/other non-drivable furniture) - the
# same importance tiering street_ids.label_streets already uses to
# decide identity across an intersection, reused here purely for
# display so a motorway reads as a thick red line and a footway as a
# thin gray one, the same visual convention most street map renderers
# use.
_RANK_STYLE = {
    0: {"color": "#e31a1c", "weight": 6},  # motorway / trunk
    1: {"color": "#fd8d3c", "weight": 5},  # primary
    2: {"color": "#feb24c", "weight": 4},  # secondary
    3: {"color": "#fed976", "weight": 3},  # tertiary
    4: {"color": "#ffffff", "weight": 2},  # residential / unclassified / service
    5: {"color": "#bdbdbd", "weight": 1},  # footway / other non-drivable
}
_FALLBACK_RANK_STYLE = {"color": "#ffffff", "weight": 2}


def _style_for_highway(highway) -> dict:
    """The color/weight this street's own ``highway`` value should be
    drawn with, via ``DEFAULT_HIGHWAY_RANKS``' importance tier.

    Args:
        highway: The street's own ``highway`` tag value (a string, or a
            list/tuple/set if osmnx merged several tagged ways, or
            ``None``/``NaN`` if missing).

    Returns:
        A dict with ``color``/``weight`` keys, ready to splat into
        ``folium.PolyLine(...)``.
    """
    if isinstance(highway, (list, tuple, set)):
        highway = next(iter(highway), None)
    if highway is None or (isinstance(highway, float) and pd.isna(highway)):
        rank = DEFAULT_RANK
    else:
        rank = DEFAULT_HIGHWAY_RANKS.get(str(highway), DEFAULT_RANK)
    return _RANK_STYLE.get(rank, _FALLBACK_RANK_STYLE)


def _geocode_map_js(streets_json: str, number_divisor_m: float) -> str:
    """Build the click-to-geocode UI: embedded street data, the local
    equirectangular projection, nearest-street/project-onto-line math,
    and the click handler.

    Args:
        streets_json: JSON array of every street's own data (``street_id``,
            ``street_name``, admin columns, ``coords``).
        number_divisor_m: Meters of cumulative length per house number.

    Returns:
        The full ``<script>``/control HTML to embed in the map.
    """
    return f"""
<script>
window.__geocodeStreets = {streets_json};
window.__geocodeNumberDivisorM = {number_divisor_m};

// Local equirectangular projection, centered on the data's own mean
// lat/lon - accurate to well under a meter across one town/subregion's
// extent, good enough for "nearest street" and a house number rounded
// to the nearest number_divisor_m. See this module's own docstring for
// why this approximation is used instead of a real projected CRS.
window.__geocodeRefLat = 0;
(function() {{
    var sumLat = 0, n = 0;
    window.__geocodeStreets.forEach(function(s) {{
        s.coords.forEach(function(c) {{ sumLat += c[0]; n += 1; }});
    }});
    window.__geocodeRefLat = n > 0 ? sumLat / n : 0;
}})();

function osmGeocodeToLocalMeters(lat, lon) {{
    var latRad = lat * Math.PI / 180;
    var lonRad = lon * Math.PI / 180;
    var refLatRad = window.__geocodeRefLat * Math.PI / 180;
    return [
        {_EARTH_RADIUS_M} * Math.cos(refLatRad) * lonRad,
        {_EARTH_RADIUS_M} * latRad
    ];
}}

// Inverse of osmGeocodeToLocalMeters - turns the projected point (the
// nearest point ON the chosen street, in local meters) back into
// lat/lon, so it can be drawn on the map (the red projection dot).
function osmGeocodeFromLocalMeters(x, y) {{
    var refLatRad = window.__geocodeRefLat * Math.PI / 180;
    var latRad = y / {_EARTH_RADIUS_M};
    var lonRad = x / ({_EARTH_RADIUS_M} * Math.cos(refLatRad));
    return [latRad * 180 / Math.PI, lonRad * 180 / Math.PI];
}}

// Nearest point on segment (a, b) to point p, the distance along (a, b)
// to reach it, and which side of it p falls on - mirrors shapely's own
// LineString.project/nearest semantics for a single segment, in local
// meters. Side: positive cross product means p is to the LEFT of the
// segment's own direction (a -> b), negative means RIGHT - standard
// x=east/y=north convention (facing along dx/dy, north-ish is left).
// See numbering._side_of_line, which this mirrors exactly.
function osmGeocodeNearestOnSegment(px, py, ax, ay, bx, by) {{
    var dx = bx - ax, dy = by - ay;
    var lengthSq = dx * dx + dy * dy;
    var t = lengthSq === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / lengthSq;
    t = Math.max(0, Math.min(1, t));
    var nx = ax + t * dx, ny = ay + t * dy;
    var distToPoint = Math.sqrt((px - nx) * (px - nx) + (py - ny) * (py - ny));
    var distAlongSegment = t * Math.sqrt(lengthSq);
    var cross = dx * (py - ny) - dy * (px - nx);
    var side = cross > 0 ? "left" : "right";
    return {{
        distToPoint: distToPoint, distAlongSegment: distAlongSegment,
        segmentLength: Math.sqrt(lengthSq), nx: nx, ny: ny, side: side
    }};
}}

// Nearest distance from (px, py) to the whole polyline (localCoords, a
// list of [x, y] in local meters), the cumulative arc-length distance
// along the polyline to that nearest point, and which side p is on -
// shapely's own LineString.distance/.project plus
// numbering._side_of_line, reimplemented over one street's own
// (already-dissolved main-path) line. Also returns the nearest point's
// own local (x, y), for the red projection dot.
function osmGeocodeProjectOntoLine(px, py, localCoords) {{
    var best = null;
    var cumulative = 0;
    for (var i = 0; i < localCoords.length - 1; i++) {{
        var a = localCoords[i], b = localCoords[i + 1];
        var seg = osmGeocodeNearestOnSegment(px, py, a[0], a[1], b[0], b[1]);
        if (best === null || seg.distToPoint < best.distToPoint) {{
            best = {{
                distToPoint: seg.distToPoint, distanceAlong: cumulative + seg.distAlongSegment,
                nx: seg.nx, ny: seg.ny, side: seg.side
            }};
        }}
        cumulative += seg.segmentLength;
    }}
    return best;
}}

// Even on the right, odd on the left, looking in the direction of
// travel - mirrors numbering._house_number_from_distance exactly
// (right minimum 2, left minimum 1, never a naive 0).
function osmGeocodeHouseNumber(distanceAlong, side) {{
    var base = Math.round(distanceAlong / window.__geocodeNumberDivisorM);
    var wantsEven = side === "right";
    if ((base % 2 === 0) !== wantsEven) {{ base += 1; }}
    var minimum = wantsEven ? 2 : 1;
    return Math.max(minimum, base);
}}

function osmGeocodeAddress(bestStreet, bestProjection) {{
    var houseNumber = osmGeocodeHouseNumber(bestProjection.distanceAlong, bestProjection.side);
    var label = (bestStreet.street_name && bestStreet.street_name !== "")
        ? bestStreet.street_name
        : ("street " + bestStreet.street_id);

    var parts = [String(houseNumber), label];
    ["neighbourhood", "city", "province", "country"].forEach(function(col) {{
        var value = bestStreet[col];
        if (value !== null && value !== undefined && value !== "") {{
            parts.push(String(value));
        }}
    }});
    return parts.join(", ");
}}

function osmGeocodeNearest(lat, lon) {{
    var local = osmGeocodeToLocalMeters(lat, lon);
    var px = local[0], py = local[1];

    var bestStreet = null;
    var bestProjection = null;
    window.__geocodeStreets.forEach(function(street) {{
        var projection = osmGeocodeProjectOntoLine(px, py, street.localCoords);
        if (projection === null) {{ return; }}
        if (bestProjection === null || projection.distToPoint < bestProjection.distToPoint) {{
            bestStreet = street;
            bestProjection = projection;
        }}
    }});
    if (bestStreet === null) {{ return null; }}
    return {{street: bestStreet, projection: bestProjection}};
}}

function osmGeocodePoint(lat, lon) {{
    var nearest = osmGeocodeNearest(lat, lon);
    if (nearest === null) {{ return null; }}
    return osmGeocodeAddress(nearest.street, nearest.projection);
}}

function osmGeocodeOnMapClick(e) {{
    var map = window["__OSM_GEOCODE_MAP_VAR__"];
    var nearest = osmGeocodeNearest(e.latlng.lat, e.latlng.lng);
    var box = document.getElementById("osmGeocodeResultBox");

    if (window.__geocodeMarker) {{ map.removeLayer(window.__geocodeMarker); }}
    if (window.__geocodeProjectionMarker) {{ map.removeLayer(window.__geocodeProjectionMarker); }}

    if (nearest === null) {{
        box.innerText = "No street found nearby.";
        return;
    }}

    var address = osmGeocodeAddress(nearest.street, nearest.projection);
    box.innerText = address;

    window.__geocodeMarker = L.marker(e.latlng, {{title: address}}).addTo(map);
    window.__geocodeMarker.bindPopup(address).openPopup();

    // Red dot: the actual point ON the nearest street the click was
    // projected onto - distinct from the blue marker at the raw click
    // location, which usually sits a little off the street itself.
    var projectedLatLon = osmGeocodeFromLocalMeters(nearest.projection.nx, nearest.projection.ny);
    window.__geocodeProjectionMarker = L.circleMarker(projectedLatLon, {{
        radius: 6, color: "#ff0000", fillColor: "#ff0000", fillOpacity: 1, weight: 2
    }}).addTo(map);
}}

document.addEventListener("DOMContentLoaded", function() {{
    // Precompute each street's own local-meters coordinates once, up
    // front, rather than on every click.
    window.__geocodeStreets.forEach(function(street) {{
        street.localCoords = street.coords.map(function(c) {{
            return osmGeocodeToLocalMeters(c[0], c[1]);
        }});
    }});
    window["__OSM_GEOCODE_MAP_VAR__"].on("click", osmGeocodeOnMapClick);
}});
</script>
<div id="osmGeocodeControls" style="position: fixed; bottom: 20px; left: 20px; z-index: 9999;
     background: white; padding: 10px; border: 1px solid #333; max-width: 320px;">
    <b>Click the map to geocode a point</b><br>
    <div id="osmGeocodeResultBox" style="margin-top: 6px;">No point selected yet.</div>
</div>
"""


def _project_to_wgs84(x: float, y: float, crs) -> tuple[float, float]:
    """A single ``(x, y)`` point, in ``crs``, as ``(lat, lon)``.

    Args:
        x: The point's own x coordinate, in ``crs``.
        y: The point's own y coordinate, in ``crs``.
        crs: The CRS ``(x, y)`` is in.

    Returns:
        A ``(lat, lon)`` tuple in EPSG:4326.
    """
    point = gpd.GeoSeries([Point(x, y)], crs=crs).to_crs(4326).iloc[0]
    return point.y, point.x


def _draw_origin_marker(fmap: folium.Map, origin: tuple[float, float], crs) -> None:
    """Draw a big red X at the radial numbering's own origin point.

    Args:
        fmap: The map to draw on.
        origin: The origin ``(x, y)``, in ``crs`` (the same projected
            CRS ``street_direction.assign_street_directionality`` used).
        crs: The CRS ``origin`` is in.
    """
    lat, lon = _project_to_wgs84(origin[0], origin[1], crs)
    folium.Marker(
        [lat, lon],
        icon=folium.DivIcon(
            html=(
                '<div style="font-size: 48px; font-weight: bold; color: #ff0000; '
                "line-height: 1; transform: translate(-50%, -50%); "
                'text-shadow: -2px -2px 0 #000, 2px -2px 0 #000, -2px 2px 0 #000, '
                '2px 2px 0 #000;">&times;</div>'
            )
        ),
    ).add_to(fmap)


def _draw_direction_arrows(
    fmap: folium.Map,
    anchor: tuple[float, float],
    direction_deg: float,
    bounds_proj,
    crs,
) -> None:
    """Draw two big red arrows from ``anchor``, along grid numbering's
    own primary direction and its perpendicular (the cross direction).

    Args:
        fmap: The map to draw on.
        anchor: Where the arrows start, ``(x, y)`` in ``crs`` - the
            origin if one is known, otherwise the streets' own bounding
            box center.
        direction_deg: The primary axis (deg, 0 = north, clockwise) -
            same convention as
            ``street_direction.assign_street_directionality``'s own
            ``direction_deg``. The cross axis is ``direction_deg + 90``.
        bounds_proj: ``streets_gdf.total_bounds`` in ``crs`` (used to
            size the arrows relative to the data's own extent, so
            they're always "big" regardless of the subregion's size).
        crs: The CRS ``anchor``/``bounds_proj`` are in.
    """
    minx, miny, maxx, maxy = bounds_proj
    diagonal = math.hypot(maxx - minx, maxy - miny)
    arrow_length = diagonal * 0.25

    anchor_lat, anchor_lon = _project_to_wgs84(anchor[0], anchor[1], crs)

    for angle_deg in (direction_deg % 360, (direction_deg + 90) % 360):
        angle_rad = math.radians(angle_deg)
        end_x = anchor[0] + arrow_length * math.sin(angle_rad)
        end_y = anchor[1] + arrow_length * math.cos(angle_rad)
        end_lat, end_lon = _project_to_wgs84(end_x, end_y, crs)

        folium.PolyLine(
            [[anchor_lat, anchor_lon], [end_lat, end_lon]],
            color="#ff0000", weight=8, opacity=0.9,
        ).add_to(fmap)

        # The arrowhead glyph is placed a little further out than the
        # line's own end point (rather than centered exactly on it), so
        # it doesn't land on top of a street-name label that happens to
        # sit at the line's tip - real street networks are dense enough
        # near the map center that this collision is otherwise common.
        label_x = anchor[0] + (arrow_length + diagonal * 0.02) * math.sin(angle_rad)
        label_y = anchor[1] + (arrow_length + diagonal * 0.02) * math.cos(angle_rad)
        label_lat, label_lon = _project_to_wgs84(label_x, label_y, crs)
        folium.Marker(
            [label_lat, label_lon],
            icon=folium.DivIcon(
                html=(
                    '<div style="font-size: 40px; font-weight: bold; color: #ff0000; '
                    f"line-height: 1; transform: translate(-50%, -50%) rotate({angle_deg}deg); "
                    'text-shadow: -2px -2px 0 #000, 2px -2px 0 #000, -2px 2px 0 #000, '
                    '2px 2px 0 #000;">&#8593;</div>'
                )
            ),
        ).add_to(fmap)


def build_interactive_geocode_map(
    streets_gdf: gpd.GeoDataFrame,
    out_html_path: str,
    number_divisor_m: float = DEFAULT_NUMBER_DIVISOR_M,
    numbering_mode: str | None = None,
    origin: tuple[float, float] | None = None,
    direction_deg: float | None = None,
    default_center: tuple[float, float] | None = None,
    default_zoom: int | None = None,
) -> folium.Map:
    """Render a click-to-geocode folium map for one subregion.

    Every street in ``streets_gdf`` (one row per street_id, dissolved
    main-path geometry, e.g. from ``pipeline.streets_gdf_from_edges``) is
    drawn on a Google Hybrid basemap (see ``street_editor._new_map``),
    colored and weighted by its own ``highway`` value's importance tier
    (``DEFAULT_HIGHWAY_RANKS`` - a motorway draws as a thick red line, a
    footway as a thin gray one, the same convention most street map
    renderers use), with its own ``street_name`` (or ``"street
    {street_id}"`` if unnamed) labeled at its own midpoint.

    Clicking anywhere on the map runs the same lookup
    ``numbering.geocode_points`` does server-side - nearest street,
    projected distance along it, a house number, and the formatted
    address - entirely client-side in JS (see this module's own
    docstring for the local-projection approximation this relies on).
    A blue marker drops at the raw clicked point; a red dot marks the
    actual point ON the nearest street the click was projected onto
    (usually a little off from the raw click, unless the click landed
    exactly on the line). The resulting address shows in a fixed text
    box and a popup on the blue marker. Clicking again moves both
    markers and updates the box; there is no "confirm" step, every
    click is live.

    If ``numbering_mode="radial"`` and ``origin`` is given, a big red X
    marks the origin every radial street was numbered outward from
    (``street_direction.assign_street_directionality``'s own ``origin``).
    If ``numbering_mode="grid"`` and ``direction_deg`` is given, two big
    red arrows show the primary axis numbers climb along and its
    perpendicular (the cross axis), from ``origin`` if given, otherwise
    from the streets' own bounding box center - the same ``direction_deg``
    convention as ``assign_street_directionality``/``detect_main_direction``
    (0 = north, clockwise). Neither is drawn if the matching argument(s)
    aren't given, or if ``numbering_mode`` doesn't match.

    Args:
        streets_gdf: The streets GeoDataFrame, in a projected CRS, with
            ``street_id``/``street_name`` and (optionally)
            ``neighbourhood``/``city``/``province``/``country`` admin
            columns from ``numbering.add_admin_columns``.
        out_html_path: Path to write the standalone HTML map to.
        number_divisor_m: Meters of cumulative length per house number -
            must match whatever ``numbering.assign_house_numbers`` used
            when generating ``streets_gdf``, or the shown house numbers
            won't agree with the ones already baked into the gpkg/csv.
        numbering_mode: ``"radial"`` or ``"grid"`` - which of ``origin``/
            ``direction_deg`` below to actually draw. ``None`` (the
            default) draws neither, e.g. for a caller that doesn't have
            this information on hand.
        origin: The origin ``(x, y)``, in ``streets_gdf``'s own CRS, used
            for the red X in ``"radial"`` mode, or as the arrows'
            starting point in ``"grid"`` mode (optional there).
        direction_deg: The primary axis (deg, 0 = north, clockwise) for
            the two red arrows in ``"grid"`` mode.
        default_center: If given, overrides the computed streets-bounds
            center as the map's initial view.
        default_zoom: If given, overrides the default ``zoom_start`` (15)
            the map opens at.

    Returns:
        The folium Map object (also already saved to ``out_html_path``).

    Raises:
        ValueError: If ``streets_gdf`` is empty.
    """
    if streets_gdf.empty:
        raise ValueError("streets_gdf is empty, nothing to geocode against")

    streets_wgs84 = streets_gdf.to_crs(4326)
    bounds = streets_wgs84.total_bounds  # minx, miny, maxx, maxy
    bounds_proj = streets_gdf.total_bounds
    center = list(default_center) if default_center is not None else [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]

    fmap = _new_map(center, zoom_start=default_zoom if default_zoom is not None else 15)

    if numbering_mode == "radial" and origin is not None:
        _draw_origin_marker(fmap, origin, streets_gdf.crs)
    elif numbering_mode == "grid" and direction_deg is not None:
        anchor = origin if origin is not None else (
            (bounds_proj[0] + bounds_proj[2]) / 2, (bounds_proj[1] + bounds_proj[3]) / 2
        )
        _draw_direction_arrows(fmap, anchor, direction_deg, bounds_proj, streets_gdf.crs)

    admin_columns = [
        c for c in ("neighbourhood", "city", "province", "country") if c in streets_gdf.columns
    ]

    streets_data = []
    for _, row in streets_wgs84.iterrows():
        parts = _line_coord_lists(row.geometry)
        if not parts:
            continue
        # A street can be a MultiLineString (a real gap from a subregion
        # clip, see pipeline.streets_gdf_from_edges) - each part is
        # walked/projected onto independently in JS via a separate
        # "street" entry sharing the same street_id/name/admin columns,
        # exactly like nearest-street selection would naturally prefer
        # whichever part is actually closest to a click.
        street_name = row.get("street_name")
        street_name = None if pd.isna(street_name) else street_name
        street_id = row.get("street_id")
        entry_base = {"street_id": street_id, "street_name": street_name}
        for column in admin_columns:
            value = row.get(column)
            entry_base[column] = None if pd.isna(value) else value
        style = _style_for_highway(row.get("highway"))
        label = street_name if street_name else f"street {street_id}"
        for part_coords in parts:
            coords = [(lat, lon) for lon, lat in part_coords]
            entry = dict(entry_base)
            entry["coords"] = coords
            streets_data.append(entry)
            folium.PolyLine(
                coords, color=style["color"], weight=style["weight"], opacity=0.9
            ).add_to(fmap)

            mid_lat, mid_lon = coords[len(coords) // 2]
            folium.Marker(
                [mid_lat, mid_lon],
                icon=folium.DivIcon(
                    html=(
                        '<div style="font-size: 11px; font-weight: bold; color: white; '
                        'text-shadow: -1px -1px 0 #000, 1px -1px 0 #000, -1px 1px 0 #000, '
                        '1px 1px 0 #000; white-space: nowrap; transform: translate(-50%, -50%);">'
                        f"{label}</div>"
                    )
                ),
            ).add_to(fmap)

    ui_html = _geocode_map_js(json.dumps(streets_data), number_divisor_m)
    ui_html = ui_html.replace("__OSM_GEOCODE_MAP_VAR__", fmap.get_name())

    fmap.get_root().html.add_child(Element(ui_html))
    fmap.save(out_html_path)
    return fmap
