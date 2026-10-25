"""Manual street-id/street-name correction via an offline folium map.

Automated street identity (``street_ids.label_streets``) gets most
streets right, but real OSM data always has a few edge cases worth a
human's eye: a street split in two that should be one, a wrong name, an
unnamed street a local mapper can identify, an edge that should (or
shouldn't) belong to a street's own numbered main line. This module
renders one editable map, per-street focused:

- **Default view**: every street drawn thin and muted, with an optional
  toggleable label layer (street_id + name). Clicking a street highlights
  just that one (dims every other) and opens a popup to edit its
  ``street_id``/``street_name``/``street_rank``/drivable flag, with an
  "Edit geometry" button.
- **Geometry-edit mode** (one street at a time): move/delete nodes,
  prolong/create edges, and add/remove edges to/from this street's own
  membership (a street-id-less edge shows dotted black as an "Add edge"
  candidate; an edge already on another street refuses with a warning).
  Every action is undoable. "Finish" only exits if the street's current
  edge set is one simple, fully connected path (no branch, no
  disconnection) - otherwise it's blocked with an inline error. "New
  street" (top-level button) mints a fresh street_id and drops straight
  into this same mode with an empty edge set.

Every edit exports to CSV via the top-level "Download CSV" button: one
file for street-level edits (``street_edits.csv``), one for edge-level
edits (``edge_edits.csv``, including ``geom_edited``/``is_new`` flags and
edited geometry). "Upload CSV" re-imports a previously exported pair
back into the browser session. ``apply_street_edits`` reads both back,
re-applies them onto the labeled network as the new authoritative
``street_id``/``street_name`` MEMBERSHIP, then calls
``street_ids.recompute_main_paths`` to fully recompute main path order,
walking direction, synthetic connectors, and turn-splitting from that
membership (none of which a browser click can compute on its own),
before finally re-running numbering - so numbering always stays
consistent with whatever was last edited, and a hand-edited street
spanning a real intersection gets a correct, complete main path instead
of a naive street_id/name pass-through. ``apply_street_edits_gpkg``/
``load_edited_gpkg`` do the same but as a full GeoPackage snapshot
(geometry included), for resuming editing later.

There's no server involved. The map is a single static HTML file; edits
live in the browser until exported, matching the offline workflow
already documented in ``report/upload_street_names_OSM.md``.

Possible future improvement (not done): rebuild this editor on MapLibre GL JS and use GeoLibre tools for the drawing, snapping and splitting
functions; the community app (``osmaddresssystem/app``) could move to them too.
"""

from __future__ import annotations

import json

import folium
import geopandas as gpd
import pandas as pd
from branca.element import Element
from shapely.geometry import LineString, Point

from . import numbering, street_direction, street_ids
from .default_values import (
    DEFAULT_MAIN_PATH_ANGLE_THRESHOLD_DEG,
    DEFAULT_NUMBER_DIVISOR_M,
    DEFAULT_TRANSVERSAL_ROAD_PCT,
)

_GOOGLE_HYBRID_TILE_URL = "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}"
_GOOGLE_SATELLITE_TILE_URL = "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}"
_ESRI_SATELLITE_TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
)

# name -> (tile URL, attribution, display label for the basemap dropdown).
_BASEMAPS = {
    "hybrid": (_GOOGLE_HYBRID_TILE_URL, "Google Hybrid", "Google Hybrid"),
    "satellite": (_GOOGLE_SATELLITE_TILE_URL, "Google Satellite", "Google Satellite"),
    "esri": (_ESRI_SATELLITE_TILE_URL, "Esri", "Esri Satellite"),
}
_DEFAULT_BASEMAP = "hybrid"

# 20 mutually distinct, satellite-legible colors (light/saturated enough
# to read against a dark hybrid basemap). osmColorForStreetId (below)
# hashes a street_id into this list - adjacent entries here are chosen
# to look different from each other, so two street_ids that happen to
# land next to each other in the list still read as different colors.
_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#f032e6",
    "#42d4f4", "#bfef45", "#fabed4", "#469990", "#dcbeff",
    "#9a6324", "#800000", "#aaffc3", "#808000", "#ffd8b1",
    "#000075", "#a9a9a9", "#ffe119", "#911eb4", "#fffac8",
]


def _new_map(location: list[float], zoom_start: int) -> tuple[folium.Map, dict[str, folium.TileLayer]]:
    """A folium Map with three swappable satellite/hybrid base layers, all
    at 60% opacity, ``_DEFAULT_BASEMAP`` (Google Hybrid) initially active.

    No default OpenStreetMap tiles are added (``tiles=None``). All three
    tile layers are created and added to the map (each gets its own
    Leaflet var, needed for the basemap-select dropdown to swap between
    them by var name), but every non-default one is immediately removed
    from the live map by the DOMContentLoaded script in
    ``build_street_editor_map`` - only one basemap is ever visible at a
    time, swapped by ``osmSetBaseLayer`` rather than folium's own
    ``LayerControl`` radio buttons (a plain ``<select>`` dropdown per the
    caller's own request). All three stay off the layer control itself
    (``control=False``), which only ever lists overlays.

    Args:
        location: ``[lat, lon]`` map center.
        zoom_start: Initial zoom level.

    Returns:
        A ``(map, tile_layers)`` tuple - the new folium Map, and a dict
        of every basemap name (``_BASEMAPS`` keys) to its own
        ``TileLayer``, all already added to the map.
    """
    # max_zoom=24 lets a caller zoom in well past every basemap's own
    # native tile resolution (typically ~19-20) - Leaflet/folium then
    # upscales the last real tile instead of going blank, which is
    # exactly what's wanted for close-up node/geometry editing work.
    # max_native_zoom on each TileLayer is what actually triggers that
    # upscaling (without it, Leaflet would just request - and fail to
    # get - nonexistent z20+ tiles).
    fmap = folium.Map(location=location, zoom_start=zoom_start, tiles=None, max_zoom=24)
    tile_layers = {}
    for name, (url, attr, _label) in _BASEMAPS.items():
        tile_layers[name] = folium.TileLayer(
            tiles=url,
            attr=attr,
            name=attr,
            opacity=0.6,
            overlay=False,
            control=False,
            max_zoom=24,
            max_native_zoom=20,
        ).add_to(fmap)
    return fmap, tile_layers


def _line_coord_lists(geom) -> list[list[tuple[float, float]]]:
    """Coordinate lists for a LineString or MultiLineString geometry.

    A street's main-path geometry is normally a single LineString, but a
    subregion clip can leave a real gap in it (a genuine MultiLineString,
    not just an artifact of how it was built - see
    ``pipeline.streets_gdf_from_edges``). Drawing each of its parts as
    its own PolyLine still shows the whole street correctly.

    Args:
        geom: A LineString or MultiLineString.

    Returns:
        One coordinate list (as (lon, lat) tuples) per line part, or an
        empty list if ``geom`` is empty/degenerate (e.g. a single-point
        street with no real length to draw).
    """
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "MultiLineString":
        return [list(part.coords) for part in geom.geoms if len(part.coords) >= 2]
    if geom.geom_type == "LineString" and len(geom.coords) >= 2:
        return [list(geom.coords)]
    return []


def _popup_html(
    street_id,
    street_name: str | None,
    highway: str | None = None,
    street_rank: float | None = None,
    is_drivable: bool | None = None,
) -> str:
    """Build the editable popup HTML for one street's PolyLine.

    Args:
        street_id: The street's own street_id.
        street_name: The street's current name, or ``None``.
        highway: The street's own ``highway`` value, or ``None``.
        street_rank: The street's own numeric ``street_rank``, or
            ``None``.
        is_drivable: The street's own current drivable flag, or
            ``None`` if unknown.

    Returns:
        The popup's inner HTML.
    """
    safe_name = "" if street_name in (None, "") else str(street_name)

    has_highway = highway not in (None, "") and not pd.isna(highway)
    has_rank = street_rank is not None and not pd.isna(street_rank)
    if has_highway and has_rank:
        highway_rank_text = f"{highway} {street_rank:.0f}"
    elif has_highway:
        highway_rank_text = str(highway)
    elif has_rank:
        highway_rank_text = f"{street_rank:.0f}"
    else:
        highway_rank_text = "unknown"

    rank_value = "" if not has_rank else f"{street_rank:.0f}"
    drivable_checked = "checked" if (is_drivable is None or bool(is_drivable)) else ""

    return f"""
    <div>
        <b>street_id:</b> {street_id}<br>
        <b>highway:</b> {highway_rank_text}<br>
        Street name (must be unique - click a NODE on this street to
        reassign which street_id an individual edge belongs to):<br>
        <input type="text" value="{safe_name}"
               onchange="osmStreetEditorSetName('{street_id}', this)"><br>
        Street rank (lower = more major):<br>
        <input type="number" value="{rank_value}"
               onchange="osmStreetEditorSetRank('{street_id}', this)"><br>
        <label>
            <input type="checkbox" {drivable_checked}
                   onchange="osmStreetEditorSetDrivable('{street_id}', this)">
            Drivable
        </label><br>
        <button onclick="osmStreetEditorEnterGeometryMode('{street_id}')">
            Edit geometry
        </button>
    </div>
    """


def _color_for_street_id(street_id, palette: list[str]) -> str:
    """Python mirror of ``osmColorForStreetId`` in ``_edit_ui_js`` (same
    FNV-1a hash, same palette indexing) - used so a street's initial
    on-load color (set directly via folium's own ``color=``, before any
    JS restyle ever runs) already matches what the JS side would later
    compute for the same street_id, instead of both drifting apart.

    Args:
        street_id: The street's own street_id (any type; stringified).
        palette: The hex color list to index into.

    Returns:
        A hex color string from ``palette``.
    """
    s = str(street_id)
    h = 0x811C9DC5
    for ch in s:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return palette[h % len(palette)]


def _weight_for_rank(street_rank) -> int:
    """Python mirror of ``osmWeightForStreetId`` in ``_edit_ui_js`` - a
    lower (more major) ``street_rank`` draws thicker, same convention as
    a printed road map. Unknown/missing rank falls back to weight 3.

    Args:
        street_rank: The street's own numeric ``street_rank``, or
            ``None``/``NaN``.

    Returns:
        A Leaflet PolyLine ``weight`` (pixels).
    """
    if street_rank is None or pd.isna(street_rank):
        return 3
    return max(2, 6 - round(float(street_rank)))


def _coords_json_to_geometry(coords_json: str, target_crs, reverse: bool = False) -> LineString:
    """A ``LineString`` in ``target_crs`` from ``edge_edits.csv``'s own
    ``coords`` cell (a JSON array of ``[lat, lon]`` pairs, WGS84 - the
    browser's own coordinate order, see ``_edit_ui_js``).

    Args:
        coords_json: The cell's raw JSON text.
        target_crs: The CRS to project into (``edges_gdf.crs``).
        reverse: Reverse point order first - for applying the SAME edit
            to an edge's opposite-direction row (``(v, u, key)``), whose
            own geometry runs the other way.

    Returns:
        The parsed, reprojected ``LineString``.
    """
    points = json.loads(coords_json)
    if reverse:
        points = list(reversed(points))
    line_wgs84 = LineString([(lon, lat) for lat, lon in points])
    return gpd.GeoSeries([line_wgs84], crs=4326).to_crs(target_crs).iloc[0]


def _edge_key(u, v, key) -> str:
    """The string key an edge is addressed by in the browser-side JS
    state (``__edgesByKey``/``__edgeEdits``) and in ``edge_edits.csv``.

    Args:
        u: The edge's own ``u`` (first raw osmid endpoint).
        v: The edge's own ``v`` (second raw osmid endpoint).
        key: The edge's own ``key`` (parallel-edge disambiguator).

    Returns:
        A ``"u|v|key"`` string, safe to use as a JS object key and a
        CSV/round-trippable identifier.
    """
    return f"{u}|{v}|{key}"


def _edit_ui_js(
    edges_json: str,
    street_id_to_name_json: str,
    palette_json: str,
    street_id_to_rank_json: str,
    default_basemap: str,
    simple: bool = False,
    street_id_to_drivable_json: str = "{}",
) -> str:
    """Build the whole editing UI: per-street highlight/popup, geometry-
    edit mode, and CSV download/upload, all embedded as one script.

    Args:
        edges_json: JSON array of every edge's own data (``u``, ``v``,
            ``key``, ``street_id``, ``street_name``, ``is_main``,
            ``coords``) - the source geometry-edit mode builds a
            street's own edge set (plus dangling "Add edge" candidates)
            from.
        street_id_to_name_json: JSON object mapping every known
            street_id (as a string) to its current ``street_name``.
        palette_json: JSON array of the hex color palette used for
            per-street_id coloring.
        street_id_to_rank_json: JSON object mapping every known
            street_id (as a string) to its current numeric
            ``street_rank`` (or ``null``), used to make a more major
            street's default/dimmed line thicker (see
            ``osmWeightForStreetId``).
        default_basemap: The ``_BASEMAPS`` key active on load (see
            ``_DEFAULT_BASEMAP``).
        simple: If true, the geometry-edit panel only offers Add edge/
            Remove edge (street_id MEMBERSHIP only) - Move/Delete node,
            Prolong edge, and Draw new edge (all of which change the
            underlying geometry) are omitted entirely.

    Returns:
        The full ``<script>``/control HTML to embed in the map.
    """
    basemap_options = "\n".join(
        f'<option value="{name}"{" selected" if name == default_basemap else ""}>{label}</option>'
        for name, (_url, _attr, label) in _BASEMAPS.items()
    )
    # simple=True drops every geometry-CHANGING tool (Move/Delete node,
    # Prolong edge, Draw new edge) - only Add edge/Remove edge stay,
    # since those are pure street_id membership, no new/moved geometry.
    geometry_tool_buttons = "" if simple else """
        <button id="osmGeomEditTool_move" onclick="osmGeomEditSetTool('move')"
                title="Drag a node to reposition it.">Move node</button>
        <button id="osmGeomEditTool_delete" onclick="osmGeomEditSetTool('delete')"
                title="Click a node with exactly 2 edges to merge them into one.">Delete node</button>
        <button id="osmGeomEditTool_prolong" onclick="osmGeomEditSetTool('prolong')"
                title="Click a loose end, then click a map point to extend that edge's own line.">Prolong edge</button>
        <button id="osmGeomEditTool_create" onclick="osmGeomEditSetTool('create')"
                title="Draw a brand-new edge: click an existing node of this street, then click where it should end (empty space or another node). Makes NEW geometry.">
            Draw new edge
        </button>
    """
    return f"""
<script>
window.__streetEdits = window.__streetEdits || {{}};
window.__labelInfoById = window.__labelInfoById || {{}};
window.__labelInfoByEdgeKey = window.__labelInfoByEdgeKey || {{}};
window.__edgeLineByKey = window.__edgeLineByKey || {{}};
window.__activeBaseLayer = window.__activeBaseLayer || "{default_basemap}";
window.__basemapOpacity = window.__basemapOpacity || 0.6;

function osmSetBaseLayer(name) {{
    var map = window["__OSM_MAP_VAR__"];
    var current = window.__baseLayers[window.__activeBaseLayer];
    var next = window.__baseLayers[name];
    if (!next) {{ return; }}
    if (current && map.hasLayer(current)) {{ map.removeLayer(current); }}
    if (!map.hasLayer(next)) {{ map.addLayer(next); }}
    next.setOpacity(window.__basemapOpacity);
    next.bringToBack();
    window.__activeBaseLayer = name;
}}

function osmSetBasemapOpacity(value) {{
    window.__basemapOpacity = parseFloat(value);
    var current = window.__baseLayers[window.__activeBaseLayer];
    if (current) {{ current.setOpacity(window.__basemapOpacity); }}
}}
window.__edgeEdits = window.__edgeEdits || {{}};
window.__edgesByKey = {{}};
window.__edgesData = {edges_json};
window.__streetIdToName = {street_id_to_name_json};
window.__streetIdToRank = {street_id_to_rank_json};
window.__streetIdToDrivable = {street_id_to_drivable_json};
window.__palette = {palette_json};

window.__edgesData.forEach(function(e) {{
    window.__edgesByKey[e.key] = e;
}});

function osmColorForStreetId(sid) {{
    if (sid === null || sid === undefined || sid === "") {{
        return "#000000";
    }}
    // FNV-1a: two street_ids differing by only 1 (e.g. "101" vs "102",
    // extremely common - ids are usually assigned in sequence) must
    // still land on visibly different palette entries. A simple rolling
    // hash (hash*31 + charCode) barely perturbs the result for a
    // 1-character difference in the LAST digit, so neighboring ids kept
    // landing on the same or adjacent palette color; FNV-1a's
    // multiplicative mixing after each byte scatters a 1-digit change
    // across the whole hash instead.
    var s = String(sid);
    var hash = 0x811c9dc5;
    for (var i = 0; i < s.length; i++) {{
        hash ^= s.charCodeAt(i);
        hash = Math.imul(hash, 0x01000193);
    }}
    var idx = (hash >>> 0) % window.__palette.length;
    return window.__palette[idx];
}}

function osmWeightForStreetId(sid) {{
    // A more major street (lower street_rank - see
    // default_values.DEFAULT_HIGHWAY_RANKS: 0 = motorway/trunk, ...,
    // 5 = footway) draws thicker by default, same convention as a
    // printed road map. Unknown/missing rank falls back to a mid weight.
    var rank = window.__streetIdToRank[String(sid)];
    if (rank === null || rank === undefined || isNaN(rank)) {{ return 3; }}
    return Math.max(2, 6 - Math.round(rank));
}}

// Two different street_ids may never share the same non-empty name
// (empty/unnamed is exempt - many streets can be unnamed at once).
function osmStreetEditorNameIsTaken(name, excludeStreetId) {{
    if (!name) {{ return false; }}
    var ids = Object.keys(window.__labelInfoById || {{}});
    return ids.some(function(sid) {{
        if (String(sid) === String(excludeStreetId)) {{ return false; }}
        var n = osmStreetEditorEffectiveName(sid);
        return n && String(n).toLowerCase() === String(name).toLowerCase();
    }});
}}

function osmStreetEditorSetName(streetId, input) {{
    var name = input.value.trim();
    if (osmStreetEditorNameIsTaken(name, streetId)) {{
        alert('Street name "' + name + '" is already used by another street - names must be unique (one name per street_id).');
        input.value = osmStreetEditorEffectiveName(streetId) || "";
        return;
    }}
    input.value = name;
    window.__streetEdits[streetId] = window.__streetEdits[streetId] || {{}};
    window.__streetEdits[streetId].new_street_id = streetId;
    window.__streetEdits[streetId].street_name = name;
    osmStreetEditorRefreshStreetVisuals(streetId);
}}

function osmStreetEditorSetRank(streetId, input) {{
    window.__streetEdits[streetId] = window.__streetEdits[streetId] || {{}};
    window.__streetEdits[streetId].new_street_id = window.__streetEdits[streetId].new_street_id
        !== undefined ? window.__streetEdits[streetId].new_street_id : streetId;
    window.__streetEdits[streetId].street_rank = input.value.trim();
    // Feeds osmWeightForStreetId directly - the next restyle (e.g.
    // closing this popup) already draws this street at its new weight,
    // with no separate "refresh" step needed for rank.
    window.__streetIdToRank[String(streetId)] = input.value.trim() === "" ? null : parseFloat(input.value);
}}

function osmStreetEditorSetDrivable(streetId, input) {{
    window.__streetEdits[streetId] = window.__streetEdits[streetId] || {{}};
    window.__streetEdits[streetId].new_street_id = window.__streetEdits[streetId].new_street_id
        !== undefined ? window.__streetEdits[streetId].new_street_id : streetId;
    window.__streetEdits[streetId].is_drivable = input.checked ? "True" : "False";
    // Direct write (matches osmStreetEditorSetRank) - so a later popup
    // re-open, keyed straight off window.__streetIdToDrivable, always
    // shows the current value with no separate "effective" lookup.
    window.__streetIdToDrivable[String(streetId)] = input.checked;
}}

// --- Reactive visuals: label text / unnamed-star / dropdown always
// reflect the CURRENT (possibly edited) name, not just what was true
// when the map was first generated. ------------------------------------

function osmHtmlEscape(s) {{
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}}

function osmStreetEditorEffectiveName(streetId) {{
    var edit = window.__streetEdits[String(streetId)];
    if (edit && edit.street_name !== undefined) {{ return edit.street_name; }}
    return window.__streetIdToName[String(streetId)];
}}

function osmStreetEditorLabelIcon(text) {{
    return L.divIcon({{
        html: '<div style="font-size:12px;font-weight:bold;color:#ffffff;'
            + 'text-shadow:-1px -1px 2px #000,1px -1px 2px #000,-1px 1px 2px #000,1px 1px 2px #000;'
            + 'white-space:nowrap;">' + osmHtmlEscape(text) + '</div>',
        className: "empty"
    }});
}}

function osmStreetEditorStarIcon(show) {{
    return L.divIcon({{
        html: show
            ? '<div style="font-size:34px;line-height:34px;color:#ff1744;text-shadow:0 0 3px #000;'
              + 'text-align:center;transform:translate(-50%,-50%);">★</div>'
            : "",
        iconAnchor: [0, 0],
        className: "empty"
    }});
}}

function osmStreetEditorRefreshStreetVisuals(streetId) {{
    var sid = String(streetId);
    var name = osmStreetEditorEffectiveName(sid);
    var hasName = name !== null && name !== undefined && name !== "";
    var labelText = hasName ? (sid + " - " + name) : sid;
    var infos = window.__labelInfoById[sid] || [];
    infos.forEach(function(info) {{
        info.label.setIcon(osmStreetEditorLabelIcon(labelText));
        info.star.setIcon(osmStreetEditorStarIcon(!hasName));
    }});
    osmStreetEditorRefreshUnnamedDropdown();
}}

// Dynamically-built street popup (mirrors _popup_html in Python, minus
// the read-only highway/rank text line) - rebuilt fresh every time it's
// opened from CURRENT state (window.__streetEdits/__streetIdToRank/
// __streetIdToDrivable), so it's never stale the way a popup baked once
// at map-generation time would be.
// Mirrors default_values.DEFAULT_HIGHWAY_RANKS' own tiers (0 = most
// major). Presentation only - the stored value is still the plain rank
// number (see osmStreetEditorSetRank), same as before.
window.__osmRankOptions = [
    {{value: 0, label: "0 - Motorway / Trunk"}},
    {{value: 1, label: "1 - Primary"}},
    {{value: 2, label: "2 - Secondary"}},
    {{value: 3, label: "3 - Tertiary"}},
    {{value: 4, label: "4 - Residential / Service"}},
    {{value: 5, label: "5 - Footway"}}
];

function osmStreetPopupHtml(streetId) {{
    var sid = String(streetId);
    var name = osmStreetEditorEffectiveName(sid) || "";
    var rank = window.__streetIdToRank[sid];
    var hasRank = !(rank === null || rank === undefined || isNaN(rank));
    var drivable = window.__streetIdToDrivable[sid];
    var checked = (drivable === null || drivable === undefined || drivable) ? "checked" : "";

    var rankOptionsHtml = '<option value=""' + (hasRank ? "" : " selected") + '>(not set)</option>';
    window.__osmRankOptions.forEach(function(opt) {{
        var selected = hasRank && Math.round(rank) === opt.value ? " selected" : "";
        rankOptionsHtml += '<option value="' + opt.value + '"' + selected + '>' + osmHtmlEscape(opt.label) + '</option>';
    }});

    return '<div>'
        + '<b>street_id:</b> ' + osmHtmlEscape(sid) + ' (click a NODE on this street to '
        + 'reassign which street_id an individual edge belongs to)<br>'
        + 'Street name (must be unique - one name per street_id):<br>'
        + '<input type="text" value="' + osmHtmlEscape(name) + '" '
        + 'onchange="osmStreetEditorSetName(\\'' + sid + '\\', this)"><br>'
        + 'Street rank (lower = more major):<br>'
        + '<select onchange="osmStreetEditorSetRank(\\'' + sid + '\\', this)">' + rankOptionsHtml + '</select><br>'
        + '<label><input type="checkbox" ' + checked + ' '
        + 'onchange="osmStreetEditorSetDrivable(\\'' + sid + '\\', this)"> Drivable</label><br>'
        + '<button onclick="osmStreetEditorEnterGeometryMode(\\'' + sid + '\\')">Edit geometry</button>'
        + '</div>';
}}

function osmStreetEditorOpenPopup(streetId, lat, lon) {{
    var map = window["__OSM_MAP_VAR__"];
    L.popup({{maxWidth: 300}})
        .setLatLng([lat, lon])
        .setContent(osmStreetPopupHtml(streetId))
        .openOn(map);
}}

// Real click entry point for a street's own line (now drawn per
// physical edge, not once per dissolved street - see
// build_street_editor_map) - resolves the CLICKED edge's own current
// effective street_id (folding in any __edgeEdits override, e.g. a
// node-popup reassignment from earlier this session) and either feeds
// it to an in-progress merge, or highlights + opens that street's own
// popup.
function osmStreetEditorEdgeClick(edgeKey, lat, lon) {{
    var edge = window.__edgesByKey[edgeKey];
    if (!edge) {{ return; }}
    var eff = osmGeomEditEffectiveEdge(edge);
    if (eff.id === null || eff.id === undefined || eff.id === "") {{ return; }}
    if (window.__mergeMode) {{
        osmStreetEditorMergeClick(eff.id);
        return;
    }}
    osmStreetEditorHighlight(eff.id);
    osmStreetEditorOpenPopup(eff.id, lat, lon);
}}

function osmStreetEditorRefreshUnnamedDropdown() {{
    var select = document.getElementById("osmUnnamedStreetsSelect");
    if (!select) {{ return; }}
    var ids = Object.keys(window.__labelInfoById || {{}});
    var unnamed = ids.filter(function(sid) {{
        var name = osmStreetEditorEffectiveName(sid);
        return !(name !== null && name !== undefined && name !== "");
    }}).sort(function(a, b) {{
        return String(a).localeCompare(String(b), undefined, {{numeric: true}});
    }});
    select.innerHTML = "";
    var placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.innerText = "Unnamed streets (" + unnamed.length + ")";
    select.appendChild(placeholder);
    unnamed.forEach(function(sid) {{
        var opt = document.createElement("option");
        opt.value = sid;
        opt.innerText = sid;
        select.appendChild(opt);
    }});
}}

function osmStreetEditorZoomToStreet(streetId) {{
    if (!streetId) {{ return; }}
    var map = window["__OSM_MAP_VAR__"];
    var layers = window.__streetLayersById[String(streetId)] || [];
    if (layers.length === 0) {{ return; }}
    var bounds = layers[0].getBounds();
    layers.forEach(function(l) {{ bounds.extend(l.getBounds()); }});
    map.fitBounds(bounds, {{maxZoom: 18}});
}}

function osmUnnamedStreetsSelectChange(select) {{
    osmStreetEditorZoomToStreet(select.value);
    select.value = "";
}}

// --- Node popup: click a black node dot to see every street meeting
// there as a labeled, colored spoke (a straight line at that edge's own
// direction away from the node) and reassign which street_id each one
// belongs to right at the intersection. ---------------------------------

function osmStreetEditorNextFreeStreetId() {{
    var maxId = 0;
    for (var sid in window.__streetIdToName) {{
        var n = parseInt(sid, 10);
        if (!isNaN(n) && n > maxId) {{ maxId = n; }}
    }}
    window.__edgesData.forEach(function(e) {{
        var n2 = parseInt(e.street_id, 10);
        if (!isNaN(n2) && n2 > maxId) {{ maxId = n2; }}
    }});
    for (var key in window.__edgeEdits) {{
        var n3 = parseInt(window.__edgeEdits[key].new_street_id, 10);
        if (!isNaN(n3) && n3 > maxId) {{ maxId = n3; }}
    }}
    return String(maxId + 1);
}}

// Flat-plane bearing (degrees, 0 = north/+lat, clockwise) from
// (lat1,lon1) to (lat2,lon2) - only ever used over one short edge
// segment right next to the node, so the small-scale approximation
// (plain lat/lon deltas, not a real geographic bearing) is visually
// exact for this diagram.
function osmBearingDeg(lat1, lon1, lat2, lon2) {{
    var deg = Math.atan2(lon2 - lon1, lat2 - lat1) * 180 / Math.PI;
    return (deg + 360) % 360;
}}

function osmNodeSpokes(lat, lon) {{
    var nodeKey = osmGeomEditNodeKey(lat, lon);
    var spokes = [];
    var seenPairs = {{}};
    window.__edgesData.forEach(function(edge) {{
        var parts = edge.key.split("|");
        var pairKey = [parts[0], parts[1]].sort().join("|") + "|" + parts[2];
        if (seenPairs[pairKey]) {{ return; }}
        var eff = osmGeomEditEffectiveEdge(edge);
        if (!eff.isMain || eff.id === null || eff.id === undefined || eff.id === "") {{ return; }}
        var coords = edge.coords;
        var first = coords[0], last = coords[coords.length - 1];
        var atStart = osmGeomEditNodeKey(first[0], first[1]) === nodeKey;
        var atEnd = osmGeomEditNodeKey(last[0], last[1]) === nodeKey;
        if (!atStart && !atEnd) {{ return; }}
        seenPairs[pairKey] = true;
        var dirPoint = atStart ? coords[1] : coords[coords.length - 2];
        spokes.push({{
            edgeKey: edge.key,
            id: eff.id,
            name: eff.name,
            bearing: osmBearingDeg(lat, lon, dirPoint[0], dirPoint[1])
        }});
    }});
    return spokes;
}}

function osmNodePopupHtml(spokes) {{
    var size = 200, cx = size / 2, cy = size / 2, r = 65;
    var svg = '<svg width="' + size + '" height="' + size + '" style="background:#222;border-radius:4px;">';
    spokes.forEach(function(s) {{
        var rad = s.bearing * Math.PI / 180;
        var x2 = cx + r * Math.sin(rad);
        var y2 = cy - r * Math.cos(rad);
        var color = osmColorForStreetId(s.id);
        svg += '<line x1="' + cx + '" y1="' + cy + '" x2="' + x2 + '" y2="' + y2
            + '" stroke="' + color + '" stroke-width="4"/>';
        var lx = cx + (r + 18) * Math.sin(rad);
        var ly = cy - (r + 18) * Math.cos(rad);
        svg += '<text x="' + lx + '" y="' + ly + '" fill="' + color + '" font-size="11"'
            + ' text-anchor="middle" dominant-baseline="middle">' + osmHtmlEscape(s.id) + '</text>';
    }});
    svg += '<circle cx="' + cx + '" cy="' + cy + '" r="4" fill="#fff"/></svg>';

    if (spokes.length === 0) {{
        return '<div>' + svg + '<div style="margin-top:6px;">No street passes through this node.</div></div>';
    }}

    var rows = spokes.map(function(s, i) {{
        return '<div style="margin-top:4px;">'
            + '<span style="color:' + osmColorForStreetId(s.id) + ';font-weight:bold;">'
            + osmHtmlEscape(s.id) + (s.name ? ' - ' + osmHtmlEscape(s.name) : '') + '</span>: '
            + '<select id="osmNodeSpokeSelect_' + i + '"></select></div>';
    }}).join("");

    var note = '<div style="margin-top:6px;font-size:11px;color:#666;max-width:280px;">'
        + 'Reassigning a spoke here is recorded immediately (Edit geometry and Download CSV both '
        + 'reflect it right away) - the base map\\'s own street LINE fully re-draws after '
        + 'Download CSV, applying the edits, and regenerating the map.</div>';

    return '<div>' + svg + rows + note + '</div>';
}}

function osmNodePopupWireSelects(spokes, lat, lon) {{
    var uniqueIds = [];
    var seen = {{}};
    spokes.forEach(function(s) {{
        var key = String(s.id);
        if (!seen[key]) {{ seen[key] = true; uniqueIds.push(s.id); }}
    }});

    spokes.forEach(function(s, i) {{
        var select = document.getElementById("osmNodeSpokeSelect_" + i);
        if (!select) {{ return; }}
        uniqueIds.forEach(function(id) {{
            var opt = document.createElement("option");
            opt.value = id;
            opt.innerText = id;
            if (String(id) === String(s.id)) {{ opt.selected = true; }}
            select.appendChild(opt);
        }});
        var newOpt = document.createElement("option");
        newOpt.value = "__new__";
        newOpt.innerText = "New street id";
        select.appendChild(newOpt);

        select.onchange = function() {{
            osmNodeSpokeReassign(s.edgeKey, s.id, select.value, lat, lon);
        }};
    }});
}}

function osmNodeSpokeReassign(edgeKey, oldId, targetValue, lat, lon) {{
    var edge = window.__edgesByKey[edgeKey];
    var targetId = targetValue;
    var targetName;
    if (targetValue === "__new__") {{
        targetId = osmStreetEditorNextFreeStreetId();
        targetName = prompt("Name for new street " + targetId + ":", "") || "";
        if (osmStreetEditorNameIsTaken(targetName, targetId)) {{
            alert('Street name "' + targetName + '" is already used by another street - names must be unique. Reassignment cancelled.');
            return;
        }}
        window.__streetIdToName[targetId] = targetName;
    }} else {{
        targetName = osmStreetEditorEffectiveName(targetId) || "";
    }}
    var edit = window.__edgeEdits[edgeKey];
    window.__edgeEdits[edgeKey] = {{
        original_street_id: edit ? edit.original_street_id : (edge ? edge.street_id : null),
        original_street_name: edit ? edit.original_street_name : (edge ? edge.street_name : null),
        new_street_id: targetId,
        street_name: targetName,
        geom_edited: edit ? (edit.geom_edited || false) : false,
        is_new: edit ? (edit.is_new || false) : false,
        coords: edit ? edit.coords : (edge ? edge.coords : undefined)
    }};

    // Move this ONE edge's own map line from its old street's layer
    // group to the new one and recolor it right now - this is exactly
    // what was missing before: the default map is built per physical
    // edge (see build_street_editor_map), so a single edge's line CAN
    // be individually reparented/recolored without needing a full
    // regenerate, unlike the old one-dissolved-line-per-street model.
    var lineLayer = window.__edgeLineByKey[edgeKey];
    if (lineLayer) {{
        var oldKey = String(oldId);
        if (window.__streetLayersById[oldKey]) {{
            window.__streetLayersById[oldKey] = window.__streetLayersById[oldKey].filter(function(l) {{
                return l !== lineLayer;
            }});
        }}
        window.__streetLayersById[targetId] = (window.__streetLayersById[targetId] || []).concat([lineLayer]);
        lineLayer.setStyle(osmStreetEditorDefaultStyle(targetId));
    }}

    // Same move for this edge's own label/star - otherwise it would
    // keep showing the OLD street's id/name forever, even though the
    // line itself just changed color.
    var labelInfo = window.__labelInfoByEdgeKey[edgeKey];
    if (labelInfo) {{
        var oldLabelKey = String(oldId);
        if (window.__labelInfoById[oldLabelKey]) {{
            window.__labelInfoById[oldLabelKey] = window.__labelInfoById[oldLabelKey].filter(function(info) {{
                return info !== labelInfo;
            }});
        }}
        window.__labelInfoById[String(targetId)] = (window.__labelInfoById[String(targetId)] || []).concat([labelInfo]);
    }}

    osmStreetEditorRefreshStreetVisuals(oldId);
    osmStreetEditorRefreshStreetVisuals(targetId);
    window["__OSM_MAP_VAR__"].closePopup();
    osmNodePopupOpen(lat, lon);
}}

function osmNodePopupOpen(lat, lon) {{
    var map = window["__OSM_MAP_VAR__"];
    var spokes = osmNodeSpokes(lat, lon);
    var popup = L.popup({{maxWidth: 320}})
        .setLatLng([lat, lon])
        .setContent(osmNodePopupHtml(spokes))
        .openOn(map);
    setTimeout(function() {{ osmNodePopupWireSelects(spokes, lat, lon); }}, 0);
}}

// --- Geometry-edit mode --------------------------------------------------
//
// A dedicated per-street mode: hides the default muted streets layer,
// draws just this street's own edges (colored) plus every street-id-less
// edge (dotted black - "Claim unassigned edge" candidates) as
// "Add edge" candidates, on fresh Leaflet layers it fully owns (not the
// static folium-baked ones), so edges/nodes can be freely
// added/removed/reshaped. window.__geomEdit holds all of this mode's own
// state; it's null whenever geometry-edit mode isn't active.

function osmStreetEditorNewStreet() {{
    var maxId = 0;
    for (var sid in window.__streetIdToName) {{
        var n = parseInt(sid, 10);
        if (!isNaN(n) && n > maxId) {{ maxId = n; }}
    }}
    window.__edgesData.forEach(function(e) {{
        var n2 = parseInt(e.street_id, 10);
        if (!isNaN(n2) && n2 > maxId) {{ maxId = n2; }}
    }});
    for (var key in window.__edgeEdits) {{
        var n3 = parseInt(window.__edgeEdits[key].new_street_id, 10);
        if (!isNaN(n3) && n3 > maxId) {{ maxId = n3; }}
    }}
    var newId = String(maxId + 1);
    var name = prompt("Name for new street " + newId + ":", "") || "";
    window.__streetEdits[newId] = {{new_street_id: newId, street_name: name}};
    window.__streetIdToName[newId] = name;
    osmStreetEditorEnterGeometryMode(newId);
}}

function osmGeomEditNodeKey(lat, lon) {{
    return lat.toFixed(7) + "," + lon.toFixed(7);
}}

// The REAL raw osmid at this coordinate, if any ORIGINAL edge
// (window.__edgesData - the data as generated, not this session's own
// new/split edges) has it as an endpoint - null if this point isn't a
// real pre-existing node at all (a brand-new point created this
// session, e.g. a "Draw new edge" click on empty space).
function osmRealNodeIdForCoord(nodeKey) {{
    for (var i = 0; i < window.__edgesData.length; i++) {{
        var e = window.__edgesData[i];
        var first = e.coords[0], last = e.coords[e.coords.length - 1];
        if (osmGeomEditNodeKey(first[0], first[1]) === nodeKey) {{ return e.key.split("|")[0]; }}
        if (osmGeomEditNodeKey(last[0], last[1]) === nodeKey) {{ return e.key.split("|")[1]; }}
    }}
    return null;
}}

// A brand-new point (no real osmid) gets a fresh, session-wide-unique
// NEGATIVE integer id here (never collides with a real OSM id, which
// is always positive) - reused for every edge that touches the SAME
// coordinate, so two new edges meeting at a point drawn this session
// still share one real node identity once exported/applied.
function osmNodeIdForCoord(lat, lon) {{
    var nodeKey = osmGeomEditNodeKey(lat, lon);
    var real = osmRealNodeIdForCoord(nodeKey);
    if (real !== null) {{ return real; }}
    window.__syntheticNodeIds = window.__syntheticNodeIds || {{}};
    if (window.__syntheticNodeIds[nodeKey] === undefined) {{
        window.__syntheticNodeIdCounter = (window.__syntheticNodeIdCounter || 0) - 1;
        window.__syntheticNodeIds[nodeKey] = window.__syntheticNodeIdCounter;
    }}
    return window.__syntheticNodeIds[nodeKey];
}}

// A real "u|v|key" style key (not just an opaque counter) for a
// brand-new or split-off edge, so it survives CSV export/import and
// Python-side application exactly like any original edge - u/v resolve
// to real osmids where the endpoint already existed, or a shared
// synthetic negative id for a genuinely new point.
function osmMakeNewEdgeKey(startCoord, endCoord) {{
    var state = window.__geomEdit;
    var u = osmNodeIdForCoord(startCoord[0], startCoord[1]);
    var v = osmNodeIdForCoord(endCoord[0], endCoord[1]);
    return u + "|" + v + "|" + (state.newKeyCounter--);
}}

// The TRUE current street_id/name/is_main for one edge, folding in this
// session's own __edgeEdits override if present (an edge added via
// "Add edge"/geometry-mode this session counts as a main-path member
// going forward; one removed - new_street_id null/blank - does not).
function osmGeomEditEffectiveEdge(edge) {{
    var edit = window.__edgeEdits[edge.key];
    if (!edit) {{
        return {{id: edge.street_id, name: edge.street_name, isMain: edge.is_main}};
    }}
    var unassigned = edit.new_street_id === null || edit.new_street_id === undefined || edit.new_street_id === "";
    return {{id: edit.new_street_id, name: edit.street_name, isMain: !unassigned}};
}}

function osmStreetEditorEnterGeometryMode(streetId) {{
    var map = window["__OSM_MAP_VAR__"];
    if (map.hasLayer(window["__OSM_STREETS_GROUP_VAR__"])) {{
        map.removeLayer(window["__OSM_STREETS_GROUP_VAR__"]);
    }}
    map.closePopup();
    // The top-level button bar (Download/Upload/Reset/New street/Merge)
    // sits top-right, the same corner the geometry panel's own tool
    // buttons can grow wide enough to reach at narrower widths - hide it
    // for the duration of this one street's edit instead of fighting
    // over the same corner. osmStreetEditorExitGeometryMode restores it.
    var topButtons = document.getElementById("osmStreetEditorTopButtons");
    if (topButtons) {{ topButtons.style.display = "none"; }}

    // Every physical edge is shown, not just this street's own: any
    // street's own main-path edges draw in ITS OWN color (context, and
    // a real target for the "already belongs to another street"
    // warning below); everything that is NOT on any main path - no
    // street_id at all, OR a non-main/spur edge that nominally carries
    // some OTHER street_id but was never part of that street's actual
    // numbered line - draws dotted black as an "Add edge" candidate.
    // Only THIS street's own main-path edges are real, topology-
    // checked MEMBERS (state.edges[key].street_id === streetId) - a
    // same-street_id spur that isn't on the main path must NOT count as
    // a member here, or it can introduce a phantom branch that was
    // never actually part of the street shown on the default map.
    var edges = {{}};
    var original = {{}};
    var seenPhysicalPairs = {{}};
    window.__edgesData.forEach(function(edge) {{
        // edges_gdf carries BOTH directed copies of every physical edge
        // ((u,v,key) and (v,u,key)) - only keep one row per physical
        // edge, but keyed on (u, v SORTED, key) so two genuinely
        // distinct parallel edges (same endpoints, different key -
        // different OSM way) are never collapsed into one.
        var parts = edge.key.split("|");
        var pairKey = [parts[0], parts[1]].sort().join("|") + "|" + parts[2];
        if (seenPhysicalPairs[pairKey]) {{ return; }}
        seenPhysicalPairs[pairKey] = true;

        var eff = osmGeomEditEffectiveEdge(edge);
        var hasMainStreet = eff.isMain && eff.id !== null && eff.id !== undefined && eff.id !== "";
        var belongsHere = hasMainStreet && String(eff.id) === String(streetId);

        edges[edge.key] = {{
            key: edge.key,
            // null (rendered dotted) for anything not on ANY street's
            // main path, even if it nominally carries a street_id.
            street_id: hasMainStreet ? eff.id : null,
            street_name: hasMainStreet ? eff.name : null,
            coords: edge.coords.map(function(c) {{ return [c[0], c[1]]; }}),
            is_new: false, geom_edited: false
        }};
        original[edge.key] = JSON.parse(JSON.stringify(edges[edge.key]));
    }});

    window.__geomEdit = {{
        streetId: streetId,
        edges: edges,
        original: original,
        nodeLayers: {{}},
        internalNodeLayers: {{}},
        edgeLayers: {{}},
        history: [],
        tool: null,
        pendingNodeKey: null,
        newEdgeDraft: null,
        newKeyCounter: -1
    }};

    document.getElementById("osmGeomEditPanel").style.display = "block";
    document.getElementById("osmGeomEditError").style.display = "none";
    osmGeomEditRedraw();
}}

// Edges of THIS street meeting at nodeKey - deliberately scoped to this
// street's own members only (not every rendered edge, which now also
// includes other streets' main paths and dotted candidates for
// context) - move/delete/prolong only ever touch this street's own
// shape, never another street's line that happens to cross the same
// point.
function osmGeomEditIncidentEdgeKeys(nodeKey) {{
    var state = window.__geomEdit;
    var result = [];
    for (var key in state.edges) {{
        var edge = state.edges[key];
        if (edge.street_id === null || String(edge.street_id) !== String(state.streetId)) {{ continue; }}
        var coords = edge.coords;
        var firstKey = osmGeomEditNodeKey(coords[0][0], coords[0][1]);
        var lastKey = osmGeomEditNodeKey(coords[coords.length - 1][0], coords[coords.length - 1][1]);
        if (firstKey === nodeKey || lastKey === nodeKey) {{
            result.push(key);
        }}
    }}
    return result;
}}

// Network (endpoint) nodes: white border, thick black fill - these are
// real intersections, possibly shared with other streets not loaded
// into this edit session, so moving one always confirms first (see
// osmGeomEditNetworkNodeMouseDown). Internal (edge-vertex) nodes: small
// solid black dots - purely this one edge's own shape, never shared
// with anything else, so moving one never needs to ask.
// pane: "markerPane" (z-index 600, above overlayPane's 400 where every
// edge/street PolyLine renders) - keeps node markers on top regardless
// of any line's own bringToFront() call (e.g. on highlight), same as
// the default view's own node dots (see build_street_editor_map).
window.__osmNetworkNodeStyle = {{radius: 6, color: "#ffffff", weight: 3, fillColor: "#000000", fillOpacity: 1, pane: "markerPane"}};
window.__osmInternalNodeStyle = {{radius: 4, color: "#000000", weight: 1, fillColor: "#000000", fillOpacity: 1, pane: "markerPane"}};

function osmGeomEditRedraw() {{
    var map = window["__OSM_MAP_VAR__"];
    var state = window.__geomEdit;
    for (var k in state.edgeLayers) {{ map.removeLayer(state.edgeLayers[k]); }}
    for (var n in state.nodeLayers) {{ map.removeLayer(state.nodeLayers[n]); }}
    for (var iv in state.internalNodeLayers || {{}}) {{ map.removeLayer(state.internalNodeLayers[iv]); }}
    state.edgeLayers = {{}};
    state.nodeLayers = {{}};
    state.internalNodeLayers = {{}};

    var nodeKeys = {{}};
    for (var key in state.edges) {{
        var edge = state.edges[key];
        var isThisStreet = edge.street_id !== null && String(edge.street_id) === String(state.streetId);
        var isOtherMainPath = edge.street_id !== null && !isThisStreet;
        var style;
        if (isThisStreet) {{
            // The street actually being edited: distinct, unmistakably
            // highlighted style.
            style = {{color: osmColorForStreetId(state.streetId), weight: 6, opacity: 1.0}};
        }} else if (isOtherMainPath) {{
            // Another street's own main path: its own normal color, for
            // context (and a real click target for the "already belongs
            // to another street" warning), but not as prominent as the
            // street being edited.
            style = {{color: osmColorForStreetId(edge.street_id), weight: osmWeightForStreetId(edge.street_id), opacity: 0.7}};
        }} else {{
            // Not on ANY street's main path (unassigned, or a non-main
            // spur) - an "Add edge" candidate.
            style = {{color: "#ffffff", weight: 3, dashArray: "4,6", opacity: 0.8}};
        }}
        var layer = L.polyline(edge.coords, style).addTo(map);
        layer.on("click", (function(k) {{ return function() {{ osmGeomEditEdgeClick(k); }}; }})(key));
        state.edgeLayers[key] = layer;

        if (isThisStreet) {{
            var first = edge.coords[0];
            var last = edge.coords[edge.coords.length - 1];
            nodeKeys[osmGeomEditNodeKey(first[0], first[1])] = first;
            nodeKeys[osmGeomEditNodeKey(last[0], last[1])] = last;

            for (var vi = 1; vi < edge.coords.length - 1; vi++) {{
                var vpos = edge.coords[vi];
                var vMarker = L.circleMarker(vpos, window.__osmInternalNodeStyle).addTo(map);
                (function(edgeKey, vertexIndex) {{
                    if (state.tool === "move") {{
                        vMarker.on("mousedown", function() {{
                            osmGeomEditStartInternalVertexDrag(edgeKey, vertexIndex);
                        }});
                    }}
                }})(key, vi);
                state.internalNodeLayers[key + "#" + vi] = vMarker;
            }}
        }}
    }}

    for (var nk in nodeKeys) {{
        var pos = nodeKeys[nk];
        var marker = L.circleMarker(pos, window.__osmNetworkNodeStyle).addTo(map);
        marker.on("click", (function(key) {{ return function() {{ osmGeomEditNodeClick(key, marker.getLatLng()); }}; }})(nk));
        if (state.tool === "move") {{
            marker.on("mousedown", (function(key) {{
                return function() {{ osmGeomEditNetworkNodeMouseDown(key); }};
            }})(nk));
        }}
        state.nodeLayers[nk] = marker;
    }}

    if (state.draftLayer) {{ map.removeLayer(state.draftLayer); state.draftLayer = null; }}
    if (state.newEdgeDraft && state.newEdgeDraft.coords.length > 0) {{
        // Live preview of the edge currently being drawn - not part of
        // state.edges yet (only exists once a node/snap point finishes
        // it), so it needs its own layer to be visible at all while
        // drafting.
        state.draftLayer = L.polyline(state.newEdgeDraft.coords, {{
            color: "#00e5ff", weight: 4, dashArray: "2,6", opacity: 0.9
        }}).addTo(map);
    }}
}}

function osmGeomEditSetTool(tool) {{
    window.__geomEdit.tool = (window.__geomEdit.tool === tool) ? null : tool;
    window.__geomEdit.pendingNodeKey = null;
    window.__geomEdit.newEdgeDraft = null;
    var buttons = document.querySelectorAll("#osmGeomEditTools button");
    buttons.forEach(function(b) {{ b.style.fontWeight = "normal"; }});
    var active = document.getElementById("osmGeomEditTool_" + window.__geomEdit.tool);
    if (active) {{ active.style.fontWeight = "bold"; }}
    osmGeomEditRedraw();
}}

function osmGeomEditPushHistory(entry) {{
    window.__geomEdit.history.push(entry);
}}

function osmGeomEditUndo() {{
    var entry = window.__geomEdit.history.pop();
    if (!entry) {{ return; }}
    var state = window.__geomEdit;
    if (entry.type === "move" || entry.type === "prolong" || entry.type === "membership") {{
        state.edges[entry.key] = JSON.parse(JSON.stringify(entry.before));
    }} else if (entry.type === "create") {{
        delete state.edges[entry.key];
    }} else if (entry.type === "delete_merge") {{
        delete state.edges[entry.newKey];
        entry.removedKeys.forEach(function(k, i) {{
            state.edges[k] = JSON.parse(JSON.stringify(entry.removedEdges[i]));
        }});
    }} else if (entry.type === "split") {{
        entry.newKeys.forEach(function(k) {{ delete state.edges[k]; }});
        state.edges[entry.removedKey] = JSON.parse(JSON.stringify(entry.removedEdge));
    }}
    osmGeomEditRedraw();
}}

function osmGeomEditReset() {{
    var state = window.__geomEdit;
    state.edges = JSON.parse(JSON.stringify(state.original));
    state.history = [];
    osmGeomEditRedraw();
}}

// --- Tools -----------------------------------------------------------

function osmGeomEditNodeClick(nodeKey, latlng) {{
    var state = window.__geomEdit;
    if (state.tool === "delete") {{
        var incident = osmGeomEditIncidentEdgeKeys(nodeKey);
        if (incident.length !== 2) {{
            osmGeomEditShowError("Can only delete a node with exactly 2 edges of this street meeting there (found " + incident.length + ").");
            return;
        }}
        var e1 = state.edges[incident[0]];
        var e2 = state.edges[incident[1]];
        var c1 = e1.coords.slice();
        var c2 = e2.coords.slice();
        // Orient both away from the shared node, then splice: c1 (reversed
        // so it ends at the shared node) + c2 (starting at the shared node).
        var c1FirstKey = osmGeomEditNodeKey(c1[0][0], c1[0][1]);
        if (c1FirstKey === nodeKey) {{ c1.reverse(); }}
        var c2FirstKey = osmGeomEditNodeKey(c2[0][0], c2[0][1]);
        if (c2FirstKey !== nodeKey) {{ c2.reverse(); }}
        var merged = c1.concat(c2.slice(1));
        var newKey = osmMakeNewEdgeKey(merged[0], merged[merged.length - 1]);
        var removedEdges = [JSON.parse(JSON.stringify(e1)), JSON.parse(JSON.stringify(e2))];
        state.edges[newKey] = {{
            key: newKey, street_id: state.streetId, street_name: e1.street_name,
            coords: merged, is_new: true, geom_edited: true
        }};
        delete state.edges[incident[0]];
        delete state.edges[incident[1]];
        osmGeomEditPushHistory({{type: "delete_merge", newKey: newKey, removedKeys: incident, removedEdges: removedEdges}});
        osmGeomEditRedraw();
        return;
    }}
    if (state.tool === "prolong") {{
        state.pendingNodeKey = nodeKey;
        return;
    }}
    if (state.tool === "create") {{
        osmGeomEditCreateEdgeAddPoint(latlng);
        return;
    }}
}}

function osmGeomEditMapClick(latlng) {{
    var state = window.__geomEdit;
    if (!state) {{ return; }}
    if (state.tool === "prolong" && state.pendingNodeKey) {{
        var edgeKeys = osmGeomEditIncidentEdgeKeys(state.pendingNodeKey);
        if (edgeKeys.length !== 1) {{
            osmGeomEditShowError("Pick an edge END (only 1 edge meeting there) to prolong.");
            state.pendingNodeKey = null;
            return;
        }}
        var edge = state.edges[edgeKeys[0]];
        var before = JSON.parse(JSON.stringify(edge));
        var firstKey = osmGeomEditNodeKey(edge.coords[0][0], edge.coords[0][1]);
        var newPoint = [latlng.lat, latlng.lng];
        if (firstKey === state.pendingNodeKey) {{
            edge.coords.unshift(newPoint);
        }} else {{
            edge.coords.push(newPoint);
        }}
        edge.geom_edited = true;
        osmGeomEditPushHistory({{type: "prolong", key: edgeKeys[0], before: before}});
        state.pendingNodeKey = null;
        osmGeomEditRedraw();
        return;
    }}
    if (state.tool === "create") {{
        osmGeomEditCreateEdgeAddPoint(latlng);
    }}
}}

// --- Draw new edge: click a start point, then click through as many
// intermediate points as you like (ANYWHERE on the map) - clicking
// lands ON an existing node (or within OSM_SNAP_TOLERANCE_M of one)
// finishes the current edge there; continuing to click after that
// starts a brand-new edge from that same point, so a whole chain of
// edges can be drawn without re-selecting a start each time. Every
// click snaps to a nearby node within tolerance, so intersections land
// exactly on real shared points instead of a few cm off. ---------------

var OSM_SNAP_TOLERANCE_M = 5;

function osmMetersBetween(lat1, lon1, lat2, lon2) {{
    var dLat = (lat2 - lat1) * 111320;
    var dLon = (lon2 - lon1) * 111320 * Math.cos(lat1 * Math.PI / 180);
    return Math.sqrt(dLat * dLat + dLon * dLon);
}}

// The nearest EXISTING node (any edge's own endpoint, this street's or
// any other's) within toleranceM, or null if nothing is close enough.
function osmSnapToNearbyNode(lat, lon, toleranceM) {{
    var best = null, bestDist = toleranceM;
    window.__edgesData.forEach(function(edge) {{
        [edge.coords[0], edge.coords[edge.coords.length - 1]].forEach(function(pt) {{
            var d = osmMetersBetween(lat, lon, pt[0], pt[1]);
            if (d < bestDist) {{ bestDist = d; best = pt; }}
        }});
    }});
    var state = window.__geomEdit;
    for (var key in state.edges) {{
        var coords = state.edges[key].coords;
        [coords[0], coords[coords.length - 1]].forEach(function(pt) {{
            var d = osmMetersBetween(lat, lon, pt[0], pt[1]);
            if (d < bestDist) {{ bestDist = d; best = pt; }}
        }});
    }}
    return best;
}}

function osmGeomEditCreateEdgeAddPoint(latlng) {{
    var state = window.__geomEdit;
    var raw = [latlng.lat, latlng.lng];
    var snapNode = osmSnapToNearbyNode(raw[0], raw[1], OSM_SNAP_TOLERANCE_M);
    var point = snapNode || raw;
    var landedOnNode = snapNode !== null;

    if (!state.newEdgeDraft) {{
        var hasMembers = Object.keys(state.edges).some(function(k) {{
            return String(state.edges[k].street_id) === String(state.streetId);
        }});
        if (hasMembers && !landedOnNode) {{
            osmGeomEditShowError("Start a new edge from an existing node of this street (or within " + OSM_SNAP_TOLERANCE_M + "m of one).");
            return;
        }}
        state.newEdgeDraft = {{coords: [point]}};
        osmGeomEditRedraw();
        return;
    }}

    state.newEdgeDraft.coords.push(point);
    if (!landedOnNode) {{
        // An intermediate vertex - keep drafting, don't finish yet.
        osmGeomEditRedraw();
        return;
    }}

    var draftCoords = state.newEdgeDraft.coords;
    var newKey = osmMakeNewEdgeKey(draftCoords[0], draftCoords[draftCoords.length - 1]);
    state.edges[newKey] = {{
        key: newKey, street_id: state.streetId, street_name: null,
        coords: draftCoords, is_new: true, geom_edited: true
    }};
    osmGeomEditPushHistory({{type: "create", key: newKey}});
    osmGeomEditSplitAtCrossings(newKey);
    // Chain: the next click keeps drawing, starting a fresh edge from
    // the point this one just finished on - not a dead end.
    state.newEdgeDraft = {{coords: [point]}};
    osmGeomEditRedraw();
}}

// Auto-split at a crossing: only against THIS STREET'S OWN other edges
// (never another street's - a crossing street's own edges live in a
// DIFFERENT edit session, and a split made here has no way to be
// committed back into that other street's own history/CSV row, so
// silently splitting it would corrupt data outside this session's own
// view). If the new edge crosses another street's line, that's still
// visible (drawn in its own color) so the caller can see the conflict
// and split it manually from THAT street's own "Edit geometry" session.
function osmGeomEditSplitAtCrossings(newKey) {{
    var state = window.__geomEdit;
    var guard = 0;
    while (guard++ < 50) {{
        var newCoords = state.edges[newKey].coords;
        var found = null;
        for (var i = 0; i < newCoords.length - 1 && !found; i++) {{
            found = osmGeomEditFindCrossing(newKey, newCoords[i], newCoords[i + 1]);
            if (found) {{ found.insertAt = i + 1; }}
        }}
        if (!found) {{ return; }}
        osmGeomEditSplitEdgeAt(found.key, found.point);
        newCoords.splice(found.insertAt, 0, found.point);
    }}
}}

function osmGeomEditFindCrossing(excludeKey, a, b) {{
    var state = window.__geomEdit;
    for (var key in state.edges) {{
        if (key === excludeKey) {{ continue; }}
        var edge = state.edges[key];
        if (edge.street_id === null || String(edge.street_id) !== String(state.streetId)) {{ continue; }}
        var coords = edge.coords;
        for (var i = 0; i < coords.length - 1; i++) {{
            var pt = osmSegIntersect(a, b, coords[i], coords[i + 1]);
            if (pt) {{ return {{key: key, point: pt}}; }}
        }}
    }}
    return null;
}}

// Standard 2D segment intersection (lat/lon treated as flat y/x -
// accurate enough at the scale one street's own edit session covers).
// Excludes near-endpoint "intersections" (t/u close to 0 or 1) - two
// edges simply sharing an endpoint is normal node-sharing, not a real
// mid-line crossing that needs a split.
function osmSegIntersect(p1, p2, p3, p4) {{
    var x1 = p1[1], y1 = p1[0], x2 = p2[1], y2 = p2[0];
    var x3 = p3[1], y3 = p3[0], x4 = p4[1], y4 = p4[0];
    var d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4);
    if (Math.abs(d) < 1e-14) {{ return null; }}
    var t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / d;
    var u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / d;
    var eps = 1e-6;
    if (t > eps && t < 1 - eps && u > eps && u < 1 - eps) {{
        return [y1 + t * (y2 - y1), x1 + t * (x2 - x1)];
    }}
    return null;
}}

function osmDistPointToSegment(p, a, b) {{
    var x = p[1], y = p[0], x1 = a[1], y1 = a[0], x2 = b[1], y2 = b[0];
    var dx = x2 - x1, dy = y2 - y1;
    var len2 = dx * dx + dy * dy;
    var t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((x - x1) * dx + (y - y1) * dy) / len2));
    var px = x1 + t * dx, py = y1 + t * dy;
    return Math.sqrt((x - px) * (x - px) + (y - py) * (y - py));
}}

function osmGeomEditSplitEdgeAt(key, point) {{
    var state = window.__geomEdit;
    var edge = state.edges[key];
    var coords = edge.coords;
    var bestIdx = 0, bestDist = Infinity;
    for (var i = 0; i < coords.length - 1; i++) {{
        var d = osmDistPointToSegment(point, coords[i], coords[i + 1]);
        if (d < bestDist) {{ bestDist = d; bestIdx = i; }}
    }}
    var partA = coords.slice(0, bestIdx + 1).concat([point]);
    var partB = [point].concat(coords.slice(bestIdx + 1));
    var keyA = osmMakeNewEdgeKey(partA[0], partA[partA.length - 1]);
    var keyB = osmMakeNewEdgeKey(partB[0], partB[partB.length - 1]);
    state.edges[keyA] = {{key: keyA, street_id: edge.street_id, street_name: edge.street_name, coords: partA, is_new: true, geom_edited: true}};
    state.edges[keyB] = {{key: keyB, street_id: edge.street_id, street_name: edge.street_name, coords: partB, is_new: true, geom_edited: true}};
    delete state.edges[key];
    osmGeomEditPushHistory({{type: "split", removedKey: key, removedEdge: JSON.parse(JSON.stringify(edge)), newKeys: [keyA, keyB]}});
}}

function osmGeomEditEdgeClick(edgeKey) {{
    var state = window.__geomEdit;
    var edge = state.edges[edgeKey];
    var isThisStreet = edge.street_id !== null && String(edge.street_id) === String(state.streetId);

    if (state.tool === "add_edge") {{
        if (isThisStreet) {{ return; }}
        var original = window.__edgesByKey[edgeKey];
        if (original) {{
            var edit = window.__edgeEdits[edgeKey];
            var currentOtherId = edit ? edit.new_street_id : original.street_id;
            if (currentOtherId !== null && currentOtherId !== undefined && currentOtherId !== ""
                && String(currentOtherId) !== String(state.streetId)) {{
                osmGeomEditShowError("This edge already belongs to street " + currentOtherId
                    + " - remove it from that street first.");
                return;
            }}
        }}
        var before = JSON.parse(JSON.stringify(edge));
        edge.street_id = state.streetId;
        osmGeomEditPushHistory({{type: "membership", key: edgeKey, before: before}});
        osmGeomEditRedraw();
        return;
    }}
    if (state.tool === "remove_edge") {{
        if (!isThisStreet) {{ return; }}
        var before2 = JSON.parse(JSON.stringify(edge));
        edge.street_id = null;
        edge.street_name = null;
        osmGeomEditPushHistory({{type: "membership", key: edgeKey, before: before2}});
        osmGeomEditRedraw();
    }}
}}

// Real mouse entry point for dragging a NETWORK node (a shared
// intersection - this street's own edges here get moved live, but any
// OTHER street's edge through the same physical point, not loaded into
// this edit session, is left pointing at the old location) - always
// confirms first. osmGeomEditStartDrag itself has no confirmation, so
// tests/other callers can still drive a move programmatically.
function osmGeomEditNetworkNodeMouseDown(nodeKey) {{
    if (!confirm(
        "This node may be shared with edges from OTHER streets that " +
        "aren't part of this edit session - moving it will NOT move " +
        "those other edges, which may leave them disconnected from " +
        "this street at this point. Continue?"
    )) {{
        return;
    }}
    osmGeomEditStartDrag(nodeKey);
}}

function osmGeomEditStartDrag(nodeKey) {{
    var map = window["__OSM_MAP_VAR__"];
    var state = window.__geomEdit;
    var incident = osmGeomEditIncidentEdgeKeys(nodeKey);
    var before = {{}};
    incident.forEach(function(k) {{ before[k] = JSON.parse(JSON.stringify(state.edges[k])); }});
    map.dragging.disable();

    function onMove(e) {{
        incident.forEach(function(k) {{
            var edge = state.edges[k];
            var first = edge.coords[0];
            var last = edge.coords[edge.coords.length - 1];
            if (osmGeomEditNodeKey(first[0], first[1]) === nodeKey) {{
                edge.coords[0] = [e.latlng.lat, e.latlng.lng];
            }}
            if (osmGeomEditNodeKey(last[0], last[1]) === nodeKey) {{
                edge.coords[edge.coords.length - 1] = [e.latlng.lat, e.latlng.lng];
            }}
            edge.geom_edited = true;
        }});
        osmGeomEditRedraw();
    }}
    function onUp() {{
        map.off("mousemove", onMove);
        map.off("mouseup", onUp);
        map.dragging.enable();
        incident.forEach(function(k) {{
            osmGeomEditPushHistory({{type: "move", key: k, before: before[k]}});
        }});
    }}
    map.on("mousemove", onMove);
    map.on("mouseup", onUp);
}}

// An internal (edge-vertex) node only ever belongs to ONE edge's own
// shape - never shared with anything else - so it can always be moved
// with no confirmation, unlike a network node.
function osmGeomEditStartInternalVertexDrag(edgeKey, vertexIndex) {{
    var map = window["__OSM_MAP_VAR__"];
    var state = window.__geomEdit;
    var edge = state.edges[edgeKey];
    var before = JSON.parse(JSON.stringify(edge));
    map.dragging.disable();

    function onMove(e) {{
        edge.coords[vertexIndex] = [e.latlng.lat, e.latlng.lng];
        edge.geom_edited = true;
        osmGeomEditRedraw();
    }}
    function onUp() {{
        map.off("mousemove", onMove);
        map.off("mouseup", onUp);
        map.dragging.enable();
        osmGeomEditPushHistory({{type: "move", key: edgeKey, before: before}});
    }}
    map.on("mousemove", onMove);
    map.on("mouseup", onUp);
}}

function osmGeomEditShowError(msg) {{
    var el = document.getElementById("osmGeomEditError");
    el.innerText = msg;
    el.style.display = "block";
}}

// --- Finish: validate this street's edge set is one simple, fully
// connected path (no branch, no cycle, no disconnected piece) before
// letting the caller leave geometry-edit mode - a browser click can
// silently create a branch (e.g. "Add edge" pulling in a fork), and
// there's no Python round-trip at this point to catch it. --------------

function osmGeomEditValidateTopology() {{
    var state = window.__geomEdit;
    var memberKeys = Object.keys(state.edges).filter(function(k) {{
        var e = state.edges[k];
        return e.street_id !== null && String(e.street_id) === String(state.streetId);
    }});
    if (memberKeys.length === 0) {{
        return {{ok: false, message: "This street has no edges yet."}};
    }}
    var degree = {{}};
    var adjacency = {{}};
    memberKeys.forEach(function(k) {{
        var coords = state.edges[k].coords;
        var a = osmGeomEditNodeKey(coords[0][0], coords[0][1]);
        var b = osmGeomEditNodeKey(coords[coords.length - 1][0], coords[coords.length - 1][1]);
        degree[a] = (degree[a] || 0) + 1;
        degree[b] = (degree[b] || 0) + 1;
        adjacency[a] = adjacency[a] || [];
        adjacency[b] = adjacency[b] || [];
        adjacency[a].push({{node: b, edge: k}});
        adjacency[b].push({{node: a, edge: k}});
    }});

    var branchNodes = Object.keys(degree).filter(function(n) {{ return degree[n] > 2; }});
    if (branchNodes.length > 0) {{
        return {{ok: false, message: "Street branches at " + branchNodes.length + " node(s) - a street must be a single line with no forks."}};
    }}

    var endNodes = Object.keys(degree).filter(function(n) {{ return degree[n] === 1; }});
    if (endNodes.length !== 2 && !(endNodes.length === 0 && memberKeys.length === 0)) {{
        if (endNodes.length === 0) {{
            return {{ok: false, message: "Street forms a closed loop - it must have two distinct ends."}};
        }}
        return {{ok: false, message: "Street has " + endNodes.length + " loose end(s) - it must have exactly two."}};
    }}

    var visitedEdges = {{}};
    var stack = [endNodes[0]];
    var visitedNodes = {{}};
    while (stack.length) {{
        var node = stack.pop();
        if (visitedNodes[node]) {{ continue; }}
        visitedNodes[node] = true;
        (adjacency[node] || []).forEach(function(nb) {{
            visitedEdges[nb.edge] = true;
            if (!visitedNodes[nb.node]) {{ stack.push(nb.node); }}
        }});
    }}
    if (Object.keys(visitedEdges).length !== memberKeys.length) {{
        return {{ok: false, message: "Street is disconnected - some edges can't be reached from the rest."}};
    }}
    return {{ok: true}};
}}

function osmGeomEditFinish() {{
    var state = window.__geomEdit;
    var result = osmGeomEditValidateTopology();
    if (!result.ok) {{
        osmGeomEditShowError(result.message);
        return;
    }}

    // Commit: fold every geometry-mode edge back into
    // __edgesData/__edgeEdits (the same state CSV export/other tooling
    // reads), keyed by this street only - edges belonging to OTHER
    // streets that were merely shown as "add edge" candidates are left
    // untouched.
    for (var key in state.edges) {{
        var edge = state.edges[key];
        var belongsHere = edge.street_id !== null && String(edge.street_id) === String(state.streetId);
        var wasHere = state.original[key]
            && state.original[key].street_id !== null
            && String(state.original[key].street_id) === String(state.streetId);
        if (!belongsHere && !wasHere) {{ continue; }}

        var original = window.__edgesByKey[key];
        window.__edgeEdits[key] = {{
            original_street_id: original ? original.street_id : null,
            original_street_name: original ? original.street_name : null,
            new_street_id: belongsHere ? state.streetId : null,
            street_name: belongsHere ? edge.street_name : null,
            geom_edited: edge.geom_edited || false,
            is_new: edge.is_new || false,
            coords: edge.coords
        }};
        if (!window.__edgesByKey[key]) {{
            window.__edgesByKey[key] = {{
                key: key, street_id: null, street_name: null, is_main: false, coords: edge.coords
            }};
            window.__edgesData.push(window.__edgesByKey[key]);
        }}
    }}

    osmStreetEditorExitGeometryMode();
}}

function osmStreetEditorExitGeometryMode() {{
    var map = window["__OSM_MAP_VAR__"];
    var state = window.__geomEdit;
    if (state) {{
        for (var k in state.edgeLayers) {{ map.removeLayer(state.edgeLayers[k]); }}
        for (var n in state.nodeLayers) {{ map.removeLayer(state.nodeLayers[n]); }}
        for (var iv in state.internalNodeLayers || {{}}) {{ map.removeLayer(state.internalNodeLayers[iv]); }}
        if (state.draftLayer) {{ map.removeLayer(state.draftLayer); }}
    }}
    window.__geomEdit = null;
    document.getElementById("osmGeomEditPanel").style.display = "none";
    var topButtons = document.getElementById("osmStreetEditorTopButtons");
    if (topButtons) {{ topButtons.style.display = "block"; }}
    if (!map.hasLayer(window["__OSM_STREETS_GROUP_VAR__"])) {{
        map.addLayer(window["__OSM_STREETS_GROUP_VAR__"]);
    }}
    osmStreetEditorUnhighlightAll();
}}

// --- Per-street highlight (default view is muted; clicking a street
// highlights just that one and dims everything else) -------------------

// Default view: every street its own osmColorForStreetId color (full
// opacity, moderate weight) so adjacent streets are distinguishable at
// a glance, not one flat muted color for everything. Dimming (while
// another street is selected) keeps each street's own color but fades
// its opacity/weight down, rather than flattening every street to the
// same grey - so the highlighted street still reads as "this one, in
// its own color, more prominent" rather than "the only colored one".
function osmStreetEditorDefaultStyle(sid) {{
    return {{color: osmColorForStreetId(sid), weight: osmWeightForStreetId(sid), opacity: 0.85}};
}}

function osmStreetEditorDimmedStyle(sid) {{
    return {{color: osmColorForStreetId(sid), weight: Math.max(2, osmWeightForStreetId(sid) - 1), opacity: 0.35}};
}}

// --- Merge streets: click the button, then click two streets on the
// map - the FIRST street's own id/name wins, the SECOND's edges are all
// folded into it. ---------------------------------------------------

function osmStreetEditorSetMergeMode(active) {{
    if (active && window.__geomEdit) {{
        alert("Finish or exit geometry editing first.");
        return;
    }}
    window.__mergeMode = active;
    window.__mergeFirst = null;
    var btn = document.getElementById("osmStreetEditorMergeButton");
    if (btn) {{
        btn.innerText = active ? "Cancel merge" : "Merge streets";
        btn.style.fontWeight = active ? "bold" : "normal";
    }}
}}

function osmStreetEditorToggleMergeMode() {{
    osmStreetEditorSetMergeMode(!window.__mergeMode);
}}

function osmStreetEditorMergeClick(streetId) {{
    var clicked = String(streetId);
    if (!window.__mergeFirst) {{
        window.__mergeFirst = clicked;
        alert("Merge target set to street " + clicked + ". Now click the second street - it will be merged INTO " + clicked + ".");
        return;
    }}
    var keepId = window.__mergeFirst;
    var mergeId = clicked;
    if (keepId === mergeId) {{
        alert("Click a DIFFERENT street to merge into " + keepId + ".");
        return;
    }}

    var keepName = osmStreetEditorEffectiveName(keepId) || "";

    window.__streetEdits[mergeId] = window.__streetEdits[mergeId] || {{}};
    window.__streetEdits[mergeId].new_street_id = keepId;
    window.__streetEdits[mergeId].street_name = keepName;

    // Also write per-edge overrides for every edge CURRENTLY on
    // mergeId's main path, so geometry-edit mode (which reads
    // __edgeEdits, not __streetEdits) already reflects the merge this
    // session, with no reload/re-export round trip needed.
    window.__edgesData.forEach(function(edge) {{
        var edit = window.__edgeEdits[edge.key];
        var effectiveId = edit ? edit.new_street_id : edge.street_id;
        if (effectiveId === null || effectiveId === undefined || String(effectiveId) !== mergeId) {{ return; }}
        window.__edgeEdits[edge.key] = {{
            original_street_id: edit ? edit.original_street_id : edge.street_id,
            original_street_name: edit ? edit.original_street_name : edge.street_name,
            new_street_id: keepId,
            street_name: keepName,
            geom_edited: edit ? (edit.geom_edited || false) : false,
            is_new: edit ? (edit.is_new || false) : false
        }};
    }});

    // Fold mergeId's own map layers into keepId's group and recolor
    // them immediately, so the merge is visible without a reload.
    var mergeLayers = window.__streetLayersById[mergeId] || [];
    mergeLayers.forEach(function(layer) {{
        layer.setStyle(osmStreetEditorDefaultStyle(keepId));
    }});
    window.__streetLayersById[keepId] = (window.__streetLayersById[keepId] || []).concat(mergeLayers);
    delete window.__streetLayersById[mergeId];

    // Same move for every one of mergeId's own labels - each one should
    // now read as keepId's own id/name, not still show mergeId's.
    var mergeLabels = window.__labelInfoById[mergeId] || [];
    window.__labelInfoById[keepId] = (window.__labelInfoById[keepId] || []).concat(mergeLabels);
    delete window.__labelInfoById[mergeId];

    osmStreetEditorRefreshStreetVisuals(keepId);

    alert("Merged street " + mergeId + " into " + keepId + " ('" + keepName + "'). Download CSV to save this.");
    osmStreetEditorSetMergeMode(false);
}}

function osmStreetEditorHighlight(streetId) {{
    if (window.__mergeMode) {{
        osmStreetEditorMergeClick(streetId);
        return;
    }}
    for (var sid in window.__streetLayersById) {{
        var layers = window.__streetLayersById[sid];
        var isSelected = String(sid) === String(streetId);
        layers.forEach(function(layer) {{
            if (isSelected) {{
                layer.setStyle({{color: osmColorForStreetId(sid), weight: osmWeightForStreetId(sid) + 3, opacity: 1.0}});
                layer.bringToFront();
            }} else {{
                layer.setStyle(osmStreetEditorDimmedStyle(sid));
            }}
        }});
    }}
}}

function osmStreetEditorUnhighlightAll() {{
    for (var sid in window.__streetLayersById) {{
        window.__streetLayersById[sid].forEach(function(layer) {{
            layer.setStyle(osmStreetEditorDefaultStyle(sid));
        }});
    }}
}}

function osmCsvEscape(cell) {{
    return '"' + String(cell === null || cell === undefined ? "" : cell).replace(/"/g, '""') + '"';
}}

function osmDownloadCsv(filename, rows) {{
    var csvContent = rows.map(function(r) {{
        return r.map(osmCsvEscape).join(",");
    }}).join("\\n");
    var blob = new Blob([csvContent], {{type: "text/csv"}});
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}}

function osmStreetEditorResetAllEdits() {{
    if (!confirm("Discard every edit made this session? This cannot be undone.")) {{
        return;
    }}
    window.__streetEdits = {{}};
    window.__edgeEdits = {{}};
    osmStreetEditorUnhighlightAll();
}}

function osmCsvParse(text) {{
    var lines = text.split(/\\r?\\n/).filter(function(l) {{ return l.length > 0; }});
    return lines.map(function(line) {{
        var cells = [];
        var cur = "";
        var inQuotes = false;
        for (var i = 0; i < line.length; i++) {{
            var c = line[i];
            if (inQuotes) {{
                if (c === '"' && line[i + 1] === '"') {{ cur += '"'; i++; }}
                else if (c === '"') {{ inQuotes = false; }}
                else {{ cur += c; }}
            }} else {{
                if (c === '"') {{ inQuotes = true; }}
                else if (c === ",") {{ cells.push(cur); cur = ""; }}
                else {{ cur += c; }}
            }}
        }}
        cells.push(cur);
        return cells;
    }});
}}

function osmStreetEditorImportCsvFiles(fileList) {{
    Array.prototype.forEach.call(fileList, function(file) {{
        var reader = new FileReader();
        reader.onload = function() {{
            var rows = osmCsvParse(reader.result);
            var header = rows[0] || [];
            if (header[0] === "old_street_id") {{
                for (var i = 1; i < rows.length; i++) {{
                    var r = rows[i];
                    window.__streetEdits[r[0]] = {{
                        new_street_id: r[1], street_name: r[2] || "",
                        street_rank: r[3] || "", is_drivable: r[4] || ""
                    }};
                }}
            }} else if (header[0] === "u") {{
                for (var j = 1; j < rows.length; j++) {{
                    var e = rows[j];
                    var key = e[0] + "|" + e[1] + "|" + e[2];
                    var edge = window.__edgesByKey[key];
                    var coords = e[7] ? JSON.parse(e[7]) : (edge ? edge.coords : undefined);
                    window.__edgeEdits[key] = {{
                        original_street_id: edge ? edge.street_id : null,
                        original_street_name: edge ? edge.street_name : null,
                        new_street_id: e[3] === "None" ? null : e[3],
                        street_name: e[4] || "",
                        geom_edited: e[5] === "True",
                        is_new: e[6] === "True",
                        coords: coords
                    }};
                    if (coords && !window.__edgesByKey[key]) {{
                        window.__edgesByKey[key] = {{
                            key: key, street_id: null, street_name: null, is_main: false, coords: coords
                        }};
                        window.__edgesData.push(window.__edgesByKey[key]);
                    }}
                }}
            }}
            osmStreetEditorUnhighlightAll();
        }};
        reader.readAsText(file);
    }});
}}

function osmStreetEditorExportCsv() {{
    var streetRows = [["old_street_id", "new_street_id", "street_name", "street_rank", "is_drivable"]];
    for (var streetId in window.__streetEdits) {{
        var edit = window.__streetEdits[streetId];
        streetRows.push([
            streetId, edit.new_street_id, edit.street_name || "",
            edit.street_rank || "", edit.is_drivable || ""
        ]);
    }}
    osmDownloadCsv("street_edits.csv", streetRows);

    // "coords" (JSON [[lat,lon],...]) is only populated for a
    // geometry-touched or brand-new edge - membership-only edits (a
    // plain "Add edge"/"Remove edge", no shape change) leave it blank,
    // meaning "keep the original geometry unchanged".
    var edgeRows = [["u", "v", "key", "new_street_id", "street_name", "geom_edited", "is_new", "coords"]];
    for (var edgeKey in window.__edgeEdits) {{
        var parts = edgeKey.split("|");
        var edgeEdit = window.__edgeEdits[edgeKey];
        var hasGeom = (edgeEdit.geom_edited || edgeEdit.is_new) && edgeEdit.coords;
        edgeRows.push([
            parts[0], parts[1], parts[2],
            edgeEdit.new_street_id === null ? "None" : edgeEdit.new_street_id,
            edgeEdit.street_name || "",
            edgeEdit.geom_edited ? "True" : "False",
            edgeEdit.is_new ? "True" : "False",
            hasGeom ? JSON.stringify(edgeEdit.coords) : ""
        ]);
    }}
    osmDownloadCsv("edge_edits.csv", edgeRows);
}}
</script>
<div id="osmStreetEditorTopButtons" style="position: fixed; top: 10px; right: 10px; z-index: 9999;">
    <button onclick="osmStreetEditorExportCsv()"
            style="padding: 8px 14px; font-size: 14px; cursor: pointer;">
        Download CSV
    </button>
    <button onclick="document.getElementById('osmStreetEditorUploadInput').click()"
            style="padding: 8px 14px; font-size: 14px; cursor: pointer;">
        Upload CSV
    </button>
    <input type="file" id="osmStreetEditorUploadInput" accept=".csv" multiple
           style="display: none;"
           onchange="osmStreetEditorImportCsvFiles(this.files)">
    <button onclick="osmStreetEditorResetAllEdits()"
            style="padding: 8px 14px; font-size: 14px; cursor: pointer;">
        Reset edits
    </button>
    <button onclick="osmStreetEditorNewStreet()"
            style="padding: 8px 14px; font-size: 14px; cursor: pointer;">
        New street
    </button>
    <button id="osmStreetEditorMergeButton" onclick="osmStreetEditorToggleMergeMode()"
            style="padding: 8px 14px; font-size: 14px; cursor: pointer;"
            title="Click, then click two streets on the map - the first one's id/name wins, the second's edges merge into it.">
        Merge streets
    </button>
</div>
<div id="osmStreetEditorViewControls" style="position: fixed; top: 80px; left: 10px;
     z-index: 9999; background: white; padding: 8px; border: 1px solid #333; font-size: 13px;">
    <div>
        Basemap:
        <select id="osmBaseLayerSelect" onchange="osmSetBaseLayer(this.value)">
            {basemap_options}
        </select>
    </div>
    <div style="margin-top: 4px;">
        Basemap opacity:
        <input type="range" min="0" max="1" step="0.05" value="0.6"
               oninput="osmSetBasemapOpacity(this.value)">
    </div>
    <div style="margin-top: 4px;">
        <select id="osmUnnamedStreetsSelect" onchange="osmUnnamedStreetsSelectChange(this)">
            <option value="">Unnamed streets (0)</option>
        </select>
    </div>
</div>
<div id="osmGeomEditPanel" style="display: none; position: fixed; top: 10px; left: 50%;
     transform: translateX(-50%); z-index: 9999; background: white; padding: 10px;
     border: 1px solid #333; text-align: center;">
    <div id="osmGeomEditTools">
        {geometry_tool_buttons}
        <button id="osmGeomEditTool_add_edge" onclick="osmGeomEditSetTool('add_edge')"
                title="Claim an already-existing edge that has no street yet (shown dotted black) for this street. Does NOT create geometry.">
            Claim unassigned edge
        </button>
        <button id="osmGeomEditTool_remove_edge" onclick="osmGeomEditSetTool('remove_edge')"
                title="Click one of this street's own edges to strip it back to unassigned (dotted black). The edge itself isn't deleted.">Remove edge</button>
    </div>
    <div style="margin-top: 6px;">
        <button onclick="osmGeomEditUndo()">Undo</button>
        <button onclick="osmGeomEditReset()">Reset street</button>
        <button onclick="osmGeomEditFinish()">Finish</button>
    </div>
    <div id="osmGeomEditError" style="display: none; margin-top: 6px; color: #b00020; max-width: 480px;"></div>
</div>
"""


def build_street_editor_map(
    streets_gdf: gpd.GeoDataFrame,
    edges_gdf: gpd.GeoDataFrame,
    out_html_path: str,
    simple: bool = False,
) -> folium.Map:
    """Render an interactive, editable, per-street-focused folium map.

    ``simple=True`` (see also ``build_street_editor_map_simple``) drops
    every GEOMETRY-changing tool from the "Edit geometry" panel (Move
    node, Delete node, Prolong edge, Draw new edge) - only Add edge/
    Remove edge stay (pure street_id MEMBERSHIP, no new/moved geometry),
    alongside the node-popup's own spoke reassignment and Merge streets,
    which are membership-only by nature and unaffected either way. Use
    this for a reviewer who should be able to fix street_id/name/rank
    mistakes but never touch the underlying road network shape.

    The basemap is Google's hybrid (satellite + labels) tile layer at
    60% opacity, and nothing else, no OpenStreetMap tiles.

    **Default view**: every street drawn thin and muted (grey), so
    nothing dominates the map. An optional "Street labels" overlay
    (toggle via the layer control, top right) shows each street's
    ``street_id - street_name`` at its midpoint. Clicking a street
    highlights just that one (its own palette color, full weight) and
    dims every other street, and opens a popup with editable
    ``street_id``/``street_name``/``street_rank``/drivable-checkbox
    fields, plus an "Edit geometry" button.

    **Geometry-edit mode** (entered via "Edit geometry" or the
    map-level "New street" button): a dedicated top-center panel with
    tools scoped to exactly one street at a time -

    - Move node / Delete node (merges its 2 edges) / Prolong edge
      (extend a loose end).
    - Draw new edge: click an existing node of this street, then click
      where the new edge should end - creates NEW geometry.
    - Claim unassigned edge: click a dotted-black edge with no
      street_id yet and add it to this street (no new geometry); an
      edge already on another street refuses with a warning instead of
      silently stealing it.
    - Remove edge: strip an edge back to unassigned.

    Every action is undoable (Undo), or the whole street can be
    reverted to how it looked entering this session (Reset street).
    "Finish" only exits geometry-edit mode if the street's current edge
    set is one simple, fully connected path (no branch, no
    disconnection) - otherwise it's blocked in place with an inline
    error naming the problem. This only ever controls street_id/name
    MEMBERSHIP and geometry - the actual main-path
    (``is_main_direction``) and edge orientation are still recomputed by
    ``street_ids``' main-path logic on the next full pipeline run, using
    that membership as its input (see ``apply_street_edits``).

    Map-level buttons (always visible, top right): "Download CSV"
    (``street_edits.csv`` + ``edge_edits.csv``, the latter including
    ``geom_edited``/``is_new`` flags and edited geometry), "Upload CSV"
    (re-imports a previously downloaded pair back into the session),
    "Reset edits" (confirm-gated, discards every edit made this
    session), and "New street" (autoassigns the next free street_id,
    prompts for a name, and drops straight into geometry-edit mode with
    an empty edge set).

    Args:
        streets_gdf: The streets GeoDataFrame (main-path edges only,
            e.g. from ``pipeline.streets_gdf_from_edges``), with
            ``street_id``/``street_name`` columns, for the default view.
        edges_gdf: Every edge (main and non-main), with ``street_id``/
            ``street_name``/``is_main_direction`` columns, for
            geometry-edit mode.
        out_html_path: Path to write the standalone HTML map to.

    Returns:
        The folium Map object (also already saved to ``out_html_path``).

    Raises:
        ValueError: If ``streets_gdf`` is empty.
    """
    if streets_gdf.empty:
        raise ValueError("streets_gdf is empty, nothing to render")

    streets_wgs84 = streets_gdf.to_crs(4326)
    bounds = streets_wgs84.total_bounds  # minx, miny, maxx, maxy
    center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]

    fmap, tile_layers = _new_map(center, zoom_start=15)

    # street_id -> street_rank, purely for _weight_for_rank while walking
    # edges below (edges_gdf itself carries no street_rank column - see
    # street_ids.py; streets_gdf is the one authoritative source).
    street_rank_by_id = {
        sid: rank for sid, rank in zip(streets_gdf["street_id"], streets_gdf.get("street_rank", []))
    }

    # The default view is now built PER PHYSICAL EDGE (like labels/nodes
    # below), not once per dissolved street_id - a single edge's own
    # street_id can change live (node-popup spoke reassignment, Merge
    # streets) and each edge needs its OWN line to recolor/reparent
    # in-place for that to actually show up on the map, which a single
    # dissolved-geometry PolyLine per street never could. The popup is
    # no longer statically baked at generation time either - clicking
    # any edge calls osmStreetEditorEdgeClick, which builds it fresh
    # from CURRENT state (osmStreetPopupHtml), so it's never stale.
    streets_group = folium.FeatureGroup(name="streets", show=True)
    street_layer_vars_by_id: dict[str, list[str]] = {}
    edge_line_var_by_key: dict[str, str] = {}

    # Every edge's own data (u/v/key, street_id/name, is_main, coords) is
    # needed as plain JS state for geometry-edit mode (its own dangling-
    # edge "Add edge" candidates, and the source it rebuilds a street's
    # edge set from) - but, unlike the old Edit Edges mode, none of this
    # is rendered as a standing map layer; geometry-edit mode draws its
    # own Leaflet layers fresh each time it's entered.
    #
    # Labels are placed HERE (per physical main-path EDGE), not once per
    # dissolved street - a long street only had one label at its own
    # midpoint before, which could be far off-screen when zoomed into
    # one end of it; one label per edge means a label is always nearby
    # wherever you're looking. edges_gdf carries BOTH directed copies of
    # every physical edge (see the identical dedup in
    # osmStreetEditorEnterGeometryMode's own JS), so a physical pair is
    # only labeled once.
    # show=True (unlike "Unnamed streets" below, a diagnostic overlay
    # that stays opt-in) - labels are part of the default view, and a
    # caller editing a street's name/rank in its popup needs to actually
    # SEE the label update live, not have to know to flip a layer-
    # control checkbox on first.
    labels_group = folium.FeatureGroup(name="Street labels", show=True)
    unnamed_group = folium.FeatureGroup(name="Unnamed streets", show=False)
    # Shown by default (unlike the other two overlays) - a black dot per
    # real intersection, clicked to open the node popup (angle diagram +
    # per-edge street_id reassignment, see osmNodePopupOpen in JS).
    nodes_group = folium.FeatureGroup(name="Nodes", show=True)
    seen_node_coords: set[tuple[float, float]] = set()
    node_marker_vars: list[str] = []
    seen_physical_pairs: set[tuple] = set()
    # (street_id_str, edge_key, label_var, star_var, lat, lon) for every
    # label/star pair placed - bound into window.__labelInfoById AND
    # window.__labelInfoByEdgeKey in the DOMContentLoaded script below,
    # so a later street_name edit (typed into a popup, or a merge) can
    # update that label's own text/star live, and a single-edge
    # reassignment (node-popup spoke select) can MOVE its one label
    # from its old street's registration to the new one.
    label_marker_info: list[tuple[str, str, str, str, float, float]] = []
    edges_wgs84 = edges_gdf.to_crs(4326) if not edges_gdf.empty else edges_gdf
    edges_data = []
    for idx, row in edges_wgs84.iterrows():
        u, v, key = idx
        edge_key = _edge_key(u, v, key)
        street_id = row.get("street_id")
        street_id = None if pd.isna(street_id) else street_id
        street_name = row.get("street_name")
        street_name = None if pd.isna(street_name) else street_name
        is_main = bool(row.get("is_main_direction", False))
        parts = _line_coord_lists(row.geometry)
        if not parts:
            continue
        coords = [(lat, lon) for lon, lat in parts[0]]
        edges_data.append(
            {
                "key": edge_key,
                "street_id": street_id,
                "street_name": street_name,
                "is_main": is_main,
                "coords": coords,
            }
        )

        pair = (min(u, v), max(u, v), key)
        if is_main and street_id is not None and pair not in seen_physical_pairs:
            seen_physical_pairs.add(pair)

            street_rank = street_rank_by_id.get(street_id)
            line = folium.PolyLine(
                coords,
                # Matches osmStreetEditorDefaultStyle in _edit_ui_js - a
                # distinct per-street_id color (see _color_for_street_id)
                # at a rank-scaled weight, restored by
                # osmStreetEditorUnhighlightAll() after any highlight.
                color=_color_for_street_id(street_id, _PALETTE),
                weight=_weight_for_rank(street_rank),
                opacity=0.85,
            )
            line.add_to(streets_group)
            street_layer_vars_by_id.setdefault(str(street_id), []).append(line.get_name())
            edge_line_var_by_key[edge_key] = line.get_name()

            has_name = street_name not in (None, "")
            label_text = f"{street_id} - {street_name}" if has_name else str(street_id)
            mid_lat, mid_lon = coords[len(coords) // 2]
            label_marker = folium.Marker(
                (mid_lat, mid_lon),
                icon=folium.DivIcon(
                    # No background box - a plain white label with a dark
                    # text-shadow stays legible over both the satellite
                    # basemap and a colored street line without a jarring
                    # opaque rectangle covering the map underneath it.
                    html=f'<div style="font-size:12px;font-weight:bold;color:#ffffff;'
                    f'text-shadow:-1px -1px 2px #000,1px -1px 2px #000,'
                    f'-1px 1px 2px #000,1px 1px 2px #000;'
                    f'white-space:nowrap;">{label_text}</div>'
                ),
            ).add_to(labels_group)
            # The star marker always exists (so a JS var is always
            # available to toggle later) - its icon is empty from the
            # start whenever the street already has a name.
            star_marker = folium.Marker(
                (mid_lat, mid_lon),
                icon=folium.DivIcon(
                    # A big red star sits BEHIND the label at the same
                    # point (negative zIndexOffset), flagging an unnamed
                    # street at a glance without replacing its id-only
                    # label text.
                    html=(
                        '<div style="font-size:34px;line-height:34px;color:#ff1744;'
                        'text-shadow:0 0 3px #000;text-align:center;'
                        'transform:translate(-50%,-50%);">★</div>'
                        if not has_name
                        else ""
                    ),
                    icon_anchor=(0, 0),
                ),
                z_index_offset=-1000,
            ).add_to(unnamed_group)
            label_marker_info.append(
                (str(street_id), edge_key, label_marker.get_name(), star_marker.get_name(), mid_lat, mid_lon)
            )

            for node_lat, node_lon in (coords[0], coords[-1]):
                node_coord = (round(node_lat, 7), round(node_lon, 7))
                if node_coord in seen_node_coords:
                    continue
                seen_node_coords.add(node_coord)
                node_marker = folium.CircleMarker(
                    node_coord, radius=4, color="#000000", weight=1,
                    fill_color="#000000", fill_opacity=1,
                )
                # folium.vector_layers.path_options only whitelists a
                # fixed set of Leaflet path options and silently drops
                # anything else - "pane" has to be injected directly
                # into the built options dict instead of passed as a
                # kwarg. markerPane (z-index 600) sits above overlayPane
                # (400, where street/edge PolyLines render), so nodes
                # stay on top even when a line calls bringToFront() (on
                # highlight) - that only reorders within its OWN pane.
                node_marker.options["pane"] = "markerPane"
                node_marker.add_to(nodes_group)
                node_marker_vars.append(
                    f'{node_marker.get_name()}.on("click", function() {{ '
                    f'osmNodePopupOpen({node_coord[0]}, {node_coord[1]}); }});'
                )
    streets_group.add_to(fmap)
    labels_group.add_to(fmap)
    unnamed_group.add_to(fmap)
    nodes_group.add_to(fmap)

    # Bound directly to fmap.get_root().html (not fmap itself) - a plain
    # Element added via .add_to(fmap) never makes it into the rendered
    # output (folium.Map's own render() doesn't walk arbitrary Element
    # children the way a FeatureGroup/PolyLine does), while the root
    # Figure's own .html section always does.
    #
    # BUT: the actual `var poly_line_xxx = L.polyline(...)` declarations
    # for every street live in the root Figure's own .script section,
    # which branca/folium renders AFTER .html's own content in the final
    # page - confirmed empirically (a naive `.html.add_child` here throws
    # "poly_line_xxx is not defined" in a real browser, silently
    # aborting this whole script block, which left __streetLayersById
    # permanently empty and every street un-clickable - caught via
    # Playwright, not by string-content assertions alone). Deferring
    # this whole block to DOMContentLoaded guarantees every inline
    # <script> earlier in the document (including .script's own
    # polyline declarations, wherever they end up) has already run.
    street_bindings_js = ""
    for street_id, layer_vars in street_layer_vars_by_id.items():
        var_list = "[" + ",".join(layer_vars) + "]"
        street_bindings_js += f'window.__streetLayersById["{street_id}"] = {var_list};'
    for edge_key, layer_var in edge_line_var_by_key.items():
        street_bindings_js += (
            f'window.__edgeLineByKey["{edge_key}"] = {layer_var};'
            f'{layer_var}.on("click", function(e) {{ osmStreetEditorEdgeClick("{edge_key}", e.latlng.lat, e.latlng.lng); }});'
        )
    label_bindings_js = "".join(
        'window.__labelInfoById["%s"] = window.__labelInfoById["%s"] || [];'
        'var __li_%s = {label: %s, star: %s, lat: %s, lon: %s};'
        'window.__labelInfoById["%s"].push(__li_%s);'
        'window.__labelInfoByEdgeKey["%s"] = __li_%s;'
        % (
            sid, sid,
            label_var, label_var, star_var, lat, lon,
            sid, label_var,
            edge_key, label_var,
        )
        for sid, edge_key, label_var, star_var, lat, lon in label_marker_info
    )
    base_layer_bindings_js = "window.__baseLayers = {" + ",".join(
        f'"{name}": {layer.get_name()}' for name, layer in tile_layers.items()
    ) + "};"
    base_layer_bindings_js += "".join(
        f"{layer.get_name()}.remove();"
        for name, layer in tile_layers.items()
        if name != _DEFAULT_BASEMAP
    )
    node_bindings_js = "".join(node_marker_vars)
    fmap.get_root().html.add_child(
        Element(
            "<script>document.addEventListener('DOMContentLoaded', function() {"
            "window.__streetLayersById = window.__streetLayersById || {};"
            "window.__labelInfoById = window.__labelInfoById || {};"
            f"{street_bindings_js}"
            f"{label_bindings_js}"
            f"{node_bindings_js}"
            f"{base_layer_bindings_js}"
            "osmStreetEditorRefreshUnnamedDropdown();"
            f"{fmap.get_name()}.on('click', function(e) {{ osmGeomEditMapClick(e.latlng); }});"
            # A street popup is opened dynamically (osmStreetEditorEdgeClick),
            # not bound to one layer via bindPopup - so 'popupclose' has to
            # be caught on the MAP itself (Leaflet's own generic popup
            # events), not per-layer. Harmless when nothing was
            # highlighted (a node popup closing, say) - it just reapplies
            # the already-current default style to everything.
            f"{fmap.get_name()}.on('popupclose', function() {{ osmStreetEditorUnhighlightAll(); }});"
            # Fold the basemap/opacity/unnamed-streets controls INTO the
            # native Leaflet layer control's own panel (bottom right),
            # right below its checkboxes, instead of a second floating
            # panel elsewhere on the map.
            "var viewControls = document.getElementById('osmStreetEditorViewControls');"
            "var layerControlList = document.querySelector('.leaflet-control-layers-list');"
            "if (viewControls && layerControlList) {"
            "  viewControls.style.position = 'static';"
            "  viewControls.style.border = 'none';"
            "  viewControls.style.padding = '6px 0 0 0';"
            "  viewControls.style.background = 'transparent';"
            "  var sep = document.createElement('div');"
            "  sep.className = 'leaflet-control-layers-separator';"
            "  layerControlList.appendChild(sep);"
            "  layerControlList.appendChild(viewControls);"
            "}"
            "});</script>"
        )
    )

    street_id_to_name = {
        str(sid): (None if pd.isna(name) else name)
        for sid, name in zip(streets_gdf["street_id"], streets_gdf.get("street_name", []))
    }
    street_id_to_rank = {
        str(sid): (None if pd.isna(rank) else float(rank))
        for sid, rank in zip(streets_gdf["street_id"], streets_gdf.get("street_rank", []))
    }
    street_id_to_drivable = {
        str(sid): (None if pd.isna(drivable) else bool(drivable))
        for sid, drivable in zip(streets_gdf["street_id"], streets_gdf.get("is_drivable", []))
    }

    ui_html = _edit_ui_js(
        json.dumps(edges_data),
        json.dumps(street_id_to_name),
        json.dumps(_PALETTE),
        json.dumps(street_id_to_rank),
        _DEFAULT_BASEMAP,
        simple,
        json.dumps(street_id_to_drivable),
    )
    ui_html = ui_html.replace("__OSM_STREETS_GROUP_VAR__", streets_group.get_name())
    ui_html = ui_html.replace("__OSM_MAP_VAR__", fmap.get_name())

    fmap.get_root().html.add_child(Element(ui_html))
    # bottomright, not the default topright - the button bar (Download/
    # Upload/Reset/New street) already occupies the top-right corner.
    folium.LayerControl(collapsed=False, position="bottomright").add_to(fmap)
    fmap.save(out_html_path)
    return fmap


def build_street_editor_map_simple(
    streets_gdf: gpd.GeoDataFrame, edges_gdf: gpd.GeoDataFrame, out_html_path: str
) -> folium.Map:
    """``build_street_editor_map(..., simple=True)`` - street_id/name/
    rank/drivable reassignment (street popup, node-popup spoke
    reassignment, Merge streets, and Add/Remove edge membership) only.
    No node/vertex move or delete, no Prolong edge, no Draw new edge -
    the underlying road network geometry can never change through this
    map, only which street_id/name each edge belongs to. Use this for a
    reviewer who should be able to fix identity mistakes but not reshape
    the network itself; use ``build_street_editor_map`` for full
    geometry editing.

    Args:
        streets_gdf: As ``build_street_editor_map``.
        edges_gdf: As ``build_street_editor_map``.
        out_html_path: Path to write the standalone HTML map to.

    Returns:
        The folium Map object (also already saved to ``out_html_path``).
    """
    return build_street_editor_map(streets_gdf, edges_gdf, out_html_path, simple=True)


def apply_street_edits(
    nodes_gdf: gpd.GeoDataFrame,
    edges_gdf: gpd.GeoDataFrame,
    streets_df: pd.DataFrame,
    edited_csv_path: str,
    edge_edits_csv_path: str | None = None,
    origin: tuple[float, float] = (0.0, 0.0),
    numbering_mode: str = "radial",
    direction_deg: float | None = None,
    transversal_road_pct: float = DEFAULT_TRANSVERSAL_ROAD_PCT,
    number_divisor_m: float = DEFAULT_NUMBER_DIVISOR_M,
    main_path_angle_threshold_deg: float = DEFAULT_MAIN_PATH_ANGLE_THRESHOLD_DEG,
    name_branches: bool = False,
    max_branch_levels: int = 2,
    branch_number_divisor_m: float = 20.0,
    path_high_rank_max: int = 2,
    suffix_split_names: bool = True,
    backbone_streets=None,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, pd.DataFrame]:
    """Re-apply manually edited street/edge csvs onto the labeled network.

    Reads ``edited_csv_path`` (columns ``old_street_id``, ``new_street_id``,
    ``street_name``, ``street_rank``, ``is_drivable``), remaps
    ``street_id``/``street_name`` across ``edges_gdf``/``streets_df`` for
    every edge sharing an ``old_street_id``, and applies any
    ``street_rank``/``is_drivable`` override as a final pass (surviving
    the recompute below, which would otherwise re-derive both from each
    edge's own ``highway`` value). Then, if ``edge_edits_csv_path`` is
    given (the browser's own per-street "Edit geometry" mode export),
    applies its per-edge overrides (columns ``u``, ``v``, ``key``,
    ``new_street_id``, ``street_name``, ``geom_edited``, ``is_new``,
    ``coords``) on top - these win over the street-level remap for the
    specific edges they name, since they're a more specific, more recent
    edit. Both directed rows of the same physical edge (``(u, v, key)``
    and its ``(v, u, key)`` counterpart, if present) are kept in sync.

    ``coords`` (a JSON ``[[lat, lon], ...]`` array, WGS84) is only
    populated when ``geom_edited`` or ``is_new`` is true. For an
    EXISTING edge with ``geom_edited=True``, it replaces that edge's own
    geometry (reprojected into ``edges_gdf``'s own CRS). For an edge
    with ``is_new=True`` that matches no existing ``(u, v, key)`` row at
    all (a brand-new edge drawn in geometry-edit mode), both directed
    rows are appended to ``edges_gdf``, and any endpoint ``u``/``v`` not
    already present in ``nodes_gdf`` (a point created by clicking empty
    space, with no real OSM node id) gets a fresh node row of its own
    there, so ``street_ids.recompute_main_paths`` below can resolve
    every edge's own endpoints to a real node cluster.

    Once that remapping is done, ``street_id``/``street_name`` MEMBERSHIP
    is treated as final and authoritative - but a browser click can only
    ever decide membership, never the actual main-path machinery (which
    edges form the numbered through-line, in what order, walked which
    way, with what synthetic connectors through a real intersection, or
    whether a hand-picked path now needs a turn-split). So
    ``street_ids.recompute_main_paths`` is called next, fully
    recomputing all of that from scratch for every ``street_id`` now
    present, and its own returned ``(edges_gdf, streets_df)`` become
    authoritative for everything downstream - this is what actually fixes
    a hand-edited street spanning an intersection ending up with wrong or
    missing ``is_main_direction``, wrong edge orientation, or no
    synthetic connector, which a naive street_id/name remap alone would
    silently produce. Only then does grid-mode direction autodetection
    (if needed) and ``street_direction.assign_street_directionality`` +
    ``numbering.assign_house_numbers`` run, so numbering always reflects
    both the latest edit AND a freshly recomputed main path. Calling this
    with csvs that have no real edits (e.g. the freshly generated
    ``streets.csv``, before anyone has touched it) is a no-op besides
    that recomputation, so it's safe to call unconditionally on every
    run.

    ``new_street_id`` may be blank/``None``/the literal string
    ``"None"``, meaning "unassign this street/edge entirely" - it loses
    its ``street_id``/``street_name`` and drops out of numbering, the
    same as an edge that never had a street_id.

    Args:
        nodes_gdf: Node GeoDataFrame from ``street_ids.label_streets``,
            with its own ``node_id`` column already resolved. Untouched
            by remapping (nodes don't carry a street_id), but read by
            ``street_ids.recompute_main_paths`` and returned for a
            consistent 3-tuple.
        edges_gdf: Edge GeoDataFrame from ``street_ids.label_streets``/
            ``street_direction.assign_street_directionality``.
        streets_df: One row per ``street_id``. Almost entirely
            superseded further down by ``street_ids.recompute_main_paths``'s
            own freshly rebuilt ``streets_df`` - only used here as the
            source for the street-level csv's own remap.
        edited_csv_path: Path to the edited street-level csv.
        edge_edits_csv_path: Path to the edited edge-level csv, or
            ``None`` to skip that step (e.g. no edges were ever edited).
        origin: Passed through to
            ``street_direction.assign_street_directionality``.
        numbering_mode: Passed through to
            ``street_direction.assign_street_directionality``.
        direction_deg: Passed through to
            ``street_direction.assign_street_directionality``. If
            ``None`` and ``numbering_mode == "grid"``, it's autodetected
            via ``street_direction.detect_main_direction`` on the
            already-recomputed ``edges_gdf`` (fresh, correct
            ``is_main_direction`` flags), not the pre-recompute one.
        transversal_road_pct: Passed through to
            ``street_direction.assign_street_directionality``.
        number_divisor_m: Passed through to
            ``numbering.assign_house_numbers``.
        main_path_angle_threshold_deg: Passed through to
            ``street_ids.recompute_main_paths`` - the turn-deviation (deg)
            beyond which a street_id's own recomputed main path is split
            into two. Default ``DEFAULT_MAIN_PATH_ANGLE_THRESHOLD_DEG``.
        name_branches: If True, name unnamed roads branching off a named road after
            the numbering is recomputed (see ``branches.assign_branch_names``).
        max_branch_levels: Maximum number of branch prefixes in a derived name.
        branch_number_divisor_m: Metres along the parent per branch-number step.
        path_high_rank_max: Path mode (``numbering_mode="path"``): maximum street rank of the high-rank
            network. In path mode the recomputed streets are cut so each begins at its end nearest to
            the reference; ``street_id_original`` keeps the id before the cut.
        backbone_streets: Path mode: street ids (named or to-be-named, as they are after the edits) the routes to
            the reference are restricted to; see ``path_numbering.node_distances``.
        suffix_split_names: Path mode: letter suffix in parentheses on the parts after the first of a cut,
            named street (``Rue X (B)``); see ``path_numbering.split_streets``.

    Returns:
        A tuple ``(nodes_gdf, edges_gdf, streets_df)`` with edits
        applied, main paths/orientation/connectors/turn-splitting fully
        recomputed, and numbering recomputed.
    """
    edits = pd.read_csv(edited_csv_path, dtype=str)

    edges_gdf = edges_gdf.copy()
    streets_df = streets_df.copy()

    # id_map: old street_id (as string, to match the csv) -> new
    # street_id, or None if that street is being unassigned entirely.
    id_map: dict[str, str | None] = {}
    name_map: dict[str, str | None] = {}

    def _coerce_id(raw: str):
        """A purely numeric new_street_id (the common case, keeping or
        swapping to another existing numeric street_id) is cast back to
        int, so it compares equal to the untouched numeric ids already
        in edges_gdf/streets_df. A non-numeric id (a brand new,
        user-chosen label) is kept as the literal string."""
        raw = raw.strip()
        return int(raw) if raw.lstrip("-").isdigit() else raw

    def _parse_new_id(raw) -> str | int | None:
        """Parse the ``new_street_id`` cell of an edits table.

        Args:
            raw: Cell value (may be blank, NaN or ``"None"``).

        Returns:
            The coerced street id, or None for blanks.
        """
        if pd.isna(raw) or str(raw).strip() in ("", "None"):
            return None
        return _coerce_id(str(raw))

    # rank_map/drivable_map: final (post-remap) street_id -> manually
    # overridden street_rank/is_drivable, applied AFTER
    # recompute_main_paths (which would otherwise re-derive both from
    # each edge's own highway value, silently discarding the override).
    rank_map: dict[str, float] = {}
    drivable_map: dict[str, bool] = {}

    for _, edit_row in edits.iterrows():
        old_id = str(edit_row["old_street_id"])
        new_id = _parse_new_id(edit_row.get("new_street_id"))
        id_map[old_id] = new_id
        street_name = edit_row.get("street_name")
        name_map[old_id] = None if pd.isna(street_name) or street_name == "" else street_name

        if new_id is not None:
            rank_raw = edit_row.get("street_rank")
            if not pd.isna(rank_raw) and str(rank_raw).strip() != "":
                rank_map[str(new_id)] = float(rank_raw)
            drivable_raw = edit_row.get("is_drivable")
            if not pd.isna(drivable_raw) and str(drivable_raw).strip() != "":
                drivable_map[str(new_id)] = str(drivable_raw).strip().lower() == "true"

    def _remap_id(street_id):
        """Return the new street id for an old one (unchanged if not edited).

        Args:
            street_id: Original street id.
        """
        key = str(street_id)
        if key not in id_map:
            return street_id
        return id_map[key]

    def _remap_name(street_id, current_name):
        """Return the edited street name for a street id (unchanged if not edited).

        Args:
            street_id: Street id.
            current_name: Name before the edit.
        """
        key = str(street_id)
        if key not in name_map:
            return current_name
        return name_map[key]

    original_ids = edges_gdf["street_id"]
    edges_gdf["street_name"] = [
        _remap_name(sid, name) for sid, name in zip(original_ids, edges_gdf["street_name"])
    ]
    edges_gdf["street_id"] = original_ids.map(_remap_id)

    streets_original_ids = streets_df["street_id"]
    streets_df["street_name"] = [
        _remap_name(sid, name)
        for sid, name in zip(streets_original_ids, streets_df["street_name"])
    ]
    streets_df["street_id"] = streets_original_ids.map(_remap_id)

    # Edge-level overrides (Edit Edges mode) apply on top of the
    # street-level remap above, since they're more specific - a caller
    # who both renamed a whole street_id AND hand-picked one extra edge
    # into a different street_id wants the hand-picked edge to win.
    # geom_edited_keys/new_keys: (u, v, key) pairs (both directed copies)
    # whose geometry was hand-edited / which are brand new this session -
    # tracked separately from the street_id/name remap above and
    # reapplied by index membership after every downstream recompute, so
    # they survive `street_ids.recompute_main_paths` rebuilding
    # `edges_gdf` from scratch rather than relying on it to pass unknown
    # columns through untouched.
    geom_edited_keys: set[tuple[str, str, str]] = set()
    new_keys: set[tuple[str, str, str]] = set()

    if edge_edits_csv_path is not None:
        target_crs = edges_gdf.crs
        edge_edits = pd.read_csv(edge_edits_csv_path, dtype=str)
        edges_reset = edges_gdf.reset_index()

        def _match_key(u, v, key):
            """Normalise an edge key ``(u, v, key)`` to strings for matching.

            Args:
                u: Start node.
                v: End node.
                key: Parallel-edge key.
            """
            return (str(u), str(v), str(key))

        edge_index_by_key = {
            _match_key(u, v, key): i
            for i, (u, v, key) in enumerate(
                zip(edges_reset["u"], edges_reset["v"], edges_reset["key"])
            )
        }

        # A target street_id typed into Edit Edges mode's box doesn't
        # have to already exist in streets_df (the whole point of the
        # "Street name" box is to let a caller name a brand-new
        # street_id). Any such new id gets a minimal streets_df row of
        # its own here (name only - rank/highway have no single source
        # to inherit from at edge granularity), so it survives the
        # `notna()` filter below and shows up in
        # `pipeline.streets_gdf_from_edges`'s output instead of being
        # silently dropped.
        known_ids = set(streets_df["street_id"].map(str))
        new_rows: dict[str, str | None] = {}

        # new_edge_rows: brand-new physical edges (drawn in geometry-edit
        # mode) that don't match ANY existing (u, v, key) - appended to
        # edges_reset once, after the loop. synthesized_node_ids tracks
        # which endpoint node ids this call has already added to
        # nodes_gdf, so two new edges sharing a freshly-drawn point
        # don't each try to add the same node row twice.
        new_edge_rows: list[dict] = []
        synthesized_node_ids: set = set()

        for _, edit_row in edge_edits.iterrows():
            new_sid = _parse_new_id(edit_row.get("new_street_id"))
            street_name = edit_row.get("street_name")
            street_name = None if pd.isna(street_name) or street_name == "" else street_name

            geom_edited = str(edit_row.get("geom_edited", "")).strip().lower() == "true"
            is_new = str(edit_row.get("is_new", "")).strip().lower() == "true"
            coords_cell = edit_row.get("coords")
            has_coords = isinstance(coords_cell, str) and coords_cell.strip() != ""

            matched_any = False
            for u_col, v_col in (("u", "v"), ("v", "u")):
                match = _match_key(edit_row[u_col], edit_row[v_col], edit_row["key"])
                if geom_edited:
                    geom_edited_keys.add(match)
                if is_new:
                    new_keys.add(match)
                pos = edge_index_by_key.get(match)
                if pos is None:
                    continue
                matched_any = True
                edges_reset.loc[pos, "street_id"] = new_sid
                edges_reset.loc[pos, "street_name"] = street_name
                if has_coords and geom_edited:
                    # u_col == "v" means this row is the (v, u, key)
                    # counterpart of the csv's own (u, v) direction - its
                    # own geometry runs the opposite way.
                    new_geom = _coords_json_to_geometry(coords_cell, target_crs, reverse=(u_col == "v"))
                    edges_reset.loc[pos, "geometry"] = new_geom
                    edges_reset.loc[pos, "length"] = new_geom.length

            if new_sid is not None and str(new_sid) not in known_ids:
                new_rows[str(new_sid)] = street_name

            if not matched_any and is_new and has_coords:
                # A brand-new physical edge (drawn this session) -
                # neither directed row exists anywhere yet. Append both
                # directions, and make sure both endpoint nodes are real
                # rows in nodes_gdf (a point created by clicking empty
                # space in "Draw new edge" has no raw osmid at all until
                # now - see osmNodeIdForCoord in _edit_ui_js).
                u_id = _coerce_id(str(edit_row["u"]))
                v_id = _coerce_id(str(edit_row["v"]))
                key_id = _coerce_id(str(edit_row["key"]))
                forward_geom = _coords_json_to_geometry(coords_cell, target_crs, reverse=False)
                backward_geom = _coords_json_to_geometry(coords_cell, target_crs, reverse=True)
                base_row = {
                    "street_id": new_sid, "street_name": street_name,
                    "highway": "residential", "is_drivable": True,
                    "is_main_direction": True, "osmid": key_id,
                }
                new_edge_rows.append(
                    {**base_row, "u": u_id, "v": v_id, "key": key_id,
                     "geometry": forward_geom, "length": forward_geom.length}
                )
                new_edge_rows.append(
                    {**base_row, "u": v_id, "v": u_id, "key": key_id,
                     "geometry": backward_geom, "length": backward_geom.length}
                )
                points = json.loads(coords_cell)
                for node_id, (lat, lon) in ((u_id, points[0]), (v_id, points[-1])):
                    if node_id in nodes_gdf.index or node_id in synthesized_node_ids:
                        continue
                    synthesized_node_ids.add(node_id)
                    point_geom = gpd.GeoSeries([Point(lon, lat)], crs=4326).to_crs(target_crs).iloc[0]
                    nodes_gdf.loc[node_id, "geometry"] = point_geom
                    nodes_gdf.loc[node_id, "node_id"] = node_id

        edges_gdf = edges_reset.set_index(["u", "v", "key"])

        if new_edge_rows:
            # A fresh row via .loc above can silently demote nodes_gdf's
            # "geometry" column out of active-geometry status - reassert
            # it before this function relies on nodes_gdf as a
            # GeoDataFrame again (recompute_main_paths reads
            # nodes_gdf.geometry directly).
            nodes_gdf = gpd.GeoDataFrame(nodes_gdf, geometry="geometry", crs=target_crs)
            new_edges_df = pd.DataFrame(new_edge_rows)
            edges_gdf = pd.concat(
                [edges_gdf.reset_index(), new_edges_df], ignore_index=True
            ).set_index(["u", "v", "key"])
            edges_gdf = gpd.GeoDataFrame(edges_gdf, geometry="geometry", crs=target_crs)

        if new_rows:
            extra = pd.DataFrame(
                {
                    "street_id": [_coerce_id(sid) for sid in new_rows],
                    "street_name": list(new_rows.values()),
                }
            )
            streets_df = pd.concat([streets_df, extra], ignore_index=True)

    streets_df = streets_df[streets_df["street_id"].notna()].reset_index(drop=True)

    # street_id/street_name MEMBERSHIP is now final (the user's own
    # explicit choice) - but path order, walking direction, synthetic
    # connectors, and turn-splitting are not something a browser click
    # can compute, so they're fully recomputed here, from scratch, for
    # every street_id now present. This function's own returned
    # streets_df becomes authoritative, replacing the caller-supplied one
    # (now stale) almost entirely.
    edges_gdf, streets_df = street_ids.recompute_main_paths(
        nodes_gdf, edges_gdf, main_path_angle_threshold_deg=main_path_angle_threshold_deg
    )

    if numbering_mode == "grid" and direction_deg is None:
        direction_deg = street_direction.detect_main_direction(
            edges_gdf[edges_gdf["street_id"].notna()]
        )

    node_distances = None
    if numbering_mode == "path":
        from . import path_numbering

        node_distances, _reference = path_numbering.node_distances(
            nodes_gdf, edges_gdf, origin, high_rank_max=path_high_rank_max,
            backbone_streets=backbone_streets,
        )
        edges_gdf, streets_df, _mapping = path_numbering.split_streets(
            edges_gdf, streets_df, node_distances, suffix_split_names=suffix_split_names)
    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf,
        edges_gdf,
        streets_df,
        origin=origin,
        transversal_road_pct=transversal_road_pct,
        numbering_mode=numbering_mode,
        direction_deg=direction_deg if direction_deg is not None else 0.0,
        node_distances=node_distances,
        path_high_rank_max=path_high_rank_max,
    )
    edges_gdf, streets_df = numbering.assign_house_numbers(
        edges_gdf, streets_df, number_divisor_m=number_divisor_m
    )

    # street_rank/is_drivable overrides applied LAST, after every
    # recompute above (which would otherwise silently re-derive both
    # from each edge's own highway value and discard a manual choice).
    if rank_map:
        street_id_strs = streets_df["street_id"].map(str)
        streets_df["street_rank"] = [
            rank_map.get(sid, rank) for sid, rank in zip(street_id_strs, streets_df["street_rank"])
        ]
        edge_id_strs = edges_gdf["street_id"].map(str)
        if "street_rank" in edges_gdf.columns:
            edges_gdf["street_rank"] = [
                rank_map.get(sid, rank) for sid, rank in zip(edge_id_strs, edges_gdf["street_rank"])
            ]
    if drivable_map:
        edge_id_strs = edges_gdf["street_id"].map(str)
        edges_gdf["is_drivable"] = [
            drivable_map.get(sid, drivable)
            for sid, drivable in zip(edge_id_strs, edges_gdf["is_drivable"])
        ]

    # geom_edited/is_new: tracked from edge_edits.csv (see above), and
    # reapplied here by (u, v, key) index membership since neither
    # column exists on the original edges_gdf/is understood by
    # `recompute_main_paths` - every edge not named in the csv defaults
    # to False.
    edges_index_strs = [
        (str(u), str(v), str(k)) for u, v, k in edges_gdf.index
    ]
    edges_gdf["geom_edited"] = [key in geom_edited_keys for key in edges_index_strs]
    edges_gdf["is_new"] = [key in new_keys for key in edges_index_strs]

    # Last, so it sees the final street names, ranks and drivability.
    if name_branches:
        from . import branches

        edges_gdf, streets_df = branches.assign_branch_names(
            nodes_gdf, edges_gdf, streets_df,
            number_divisor_m=branch_number_divisor_m, max_levels=max_branch_levels,
        )

    return nodes_gdf.copy(), edges_gdf, streets_df


def apply_street_edits_gpkg(
    nodes_gdf: gpd.GeoDataFrame,
    edges_gdf: gpd.GeoDataFrame,
    streets_df: pd.DataFrame,
    out_gpkg_path: str,
    edited_csv_path: str,
    edge_edits_csv_path: str | None = None,
    **kwargs,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, pd.DataFrame]:
    """``apply_street_edits`` plus a GeoPackage snapshot of the result.

    A thin wrapper: calls ``apply_street_edits`` with every argument
    passed straight through, then writes the resulting ``nodes_gdf``/
    ``edges_gdf`` to ``out_gpkg_path`` as two layers (``nodes``,
    ``edges``) of one GeoPackage - a full, resumable snapshot including
    geometry, unlike the CSV edit logs alone (which never carry new/
    moved vertex coordinates compactly). Pair with ``load_edited_gpkg``
    to resume editing from exactly this state later.

    Args:
        nodes_gdf: As ``apply_street_edits``.
        edges_gdf: As ``apply_street_edits``.
        streets_df: As ``apply_street_edits``.
        out_gpkg_path: Path to write the GeoPackage to.
        edited_csv_path: As ``apply_street_edits``.
        edge_edits_csv_path: As ``apply_street_edits``.
        **kwargs: Forwarded to ``apply_street_edits`` (``origin``,
            ``numbering_mode``, ``direction_deg``,
            ``transversal_road_pct``, ``number_divisor_m``,
            ``main_path_angle_threshold_deg``).

    Returns:
        The same ``(nodes_gdf, edges_gdf, streets_df)`` tuple
        ``apply_street_edits`` returns.
    """
    nodes_gdf, edges_gdf, streets_df = apply_street_edits(
        nodes_gdf, edges_gdf, streets_df,
        edited_csv_path=edited_csv_path, edge_edits_csv_path=edge_edits_csv_path, **kwargs,
    )
    edges_out = edges_gdf.reset_index()
    for col in ("u", "v", "key"):
        edges_out[col] = edges_out[col].astype(str)
    edges_out.to_file(out_gpkg_path, layer="edges", driver="GPKG")
    nodes_gdf.to_file(out_gpkg_path, layer="nodes", driver="GPKG", mode="a")
    return nodes_gdf, edges_gdf, streets_df


def load_edited_gpkg(gpkg_path: str) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Read a ``apply_street_edits_gpkg`` snapshot back in.

    Args:
        gpkg_path: Path to a GeoPackage previously written by
            ``apply_street_edits_gpkg``.

    Returns:
        A ``(nodes_gdf, edges_gdf)`` tuple, ``edges_gdf`` reindexed back
        onto ``(u, v, key)`` (numeric where possible, matching how
        ``street_ids``/``street_editor`` address edges elsewhere).
    """
    nodes_gdf = gpd.read_file(gpkg_path, layer="nodes")
    edges_gdf = gpd.read_file(gpkg_path, layer="edges")

    def _restore(raw):
        """Restore a street id read from a text field to int when possible.

        Args:
            raw: Stored value.

        Returns:
            ``int(raw)`` if it parses, otherwise ``raw`` unchanged.
        """
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw

    edges_gdf["u"] = edges_gdf["u"].map(_restore)
    edges_gdf["v"] = edges_gdf["v"].map(_restore)
    edges_gdf["key"] = edges_gdf["key"].map(_restore)
    edges_gdf = edges_gdf.set_index(["u", "v", "key"])
    return nodes_gdf, edges_gdf
