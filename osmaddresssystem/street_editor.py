"""Manual street-id/street-name correction via an offline folium map.

Automated street identity (``street_ids.label_streets``) gets most
streets right, but real OSM data always has a few edge cases worth a
human's eye: a street split in two that should be one, a wrong name, an
unnamed street a local mapper can identify, an edge that should (or
shouldn't) belong to a street's own numbered main line. This module
renders one editable map with two modes:

- **Edit Streets**: one dissolved line per street_id (the existing
  behavior). Click a street, edit its ``street_id``/``street_name`` in a
  popup.
- **Edit Edges**: every individual edge, not just the dissolved main
  path. Type a target ``street_id`` into a box; every edge currently on
  that street_id's own main path is drawn in its color, every other edge
  (any other street, or a non-main edge of the SAME street_id) is drawn
  as a black dotted line behind it. Clicking a black-dotted edge assigns
  it to the target street_id (inheriting its current name). Clicking a
  colored edge undoes that - either reverting it to whatever it was
  before this session's edits, or, if it was never touched this
  session, unassigning it entirely.

Both modes' edits export to CSV on the same "Export CSV" button: one
file for street-level edits (``street_edits.csv``, unchanged format),
one for edge-level edits (``edge_edits.csv``, new). ``apply_street_edits``
reads both back, re-applies them onto the labeled network as the new
authoritative ``street_id``/``street_name`` MEMBERSHIP, then calls
``street_ids.recompute_main_paths`` to fully recompute main path order,
walking direction, synthetic connectors, and turn-splitting from that
membership (none of which a browser click can compute on its own),
before finally re-running numbering - so numbering always stays
consistent with whatever was last edited, and a hand-edited street
spanning a real intersection gets a correct, complete main path instead
of a naive street_id/name pass-through.

There's no server involved. The map is a single static HTML file; edits
live in the browser until exported, matching the offline workflow
already documented in ``report/upload_street_names_OSM.md``.
"""

from __future__ import annotations

import json

import folium
import geopandas as gpd
import pandas as pd
from branca.element import Element

from . import numbering, street_direction, street_ids
from .default_values import (
    DEFAULT_MAIN_PATH_ANGLE_THRESHOLD_DEG,
    DEFAULT_NUMBER_DIVISOR_M,
    DEFAULT_TRANSVERSAL_ROAD_PCT,
)

_GOOGLE_HYBRID_TILE_URL = "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}"

_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080",
]


def _new_map(location: list[float], zoom_start: int) -> folium.Map:
    """A folium Map whose only base layer is Google Hybrid at 60% opacity.

    No default OpenStreetMap tiles are added (``tiles=None``), and no
    layer control is needed since there's exactly one layer and nothing
    to switch to.

    Args:
        location: ``[lat, lon]`` map center.
        zoom_start: Initial zoom level.

    Returns:
        A new folium Map with the Google Hybrid tile layer added.
    """
    fmap = folium.Map(location=location, zoom_start=zoom_start, tiles=None)
    folium.TileLayer(
        tiles=_GOOGLE_HYBRID_TILE_URL,
        attr="Google Hybrid",
        name="Google Hybrid",
        opacity=0.6,
        overlay=False,
        control=False,
    ).add_to(fmap)
    return fmap


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
) -> str:
    """Build the editable popup HTML for one street's PolyLine.

    Args:
        street_id: The street's own street_id.
        street_name: The street's current name, or ``None``.
        highway: The street's own ``highway`` value, or ``None``.
        street_rank: The street's own numeric ``street_rank``, or
            ``None``.

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

    return f"""
    <div>
        <b>street_id:</b> {street_id}<br>
        <b>highway rank:</b> {highway_rank_text}<br>
        New street_id (blank keeps it, "None" drops it):<br>
        <input type="text" value="{street_id}"
               onchange="osmStreetEditorSetId('{street_id}', this)"><br>
        Street name:<br>
        <input type="text" value="{safe_name}"
               onchange="osmStreetEditorSetName('{street_id}', this)">
    </div>
    """


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


def _edit_ui_js(edges_json: str, street_id_to_name_json: str, palette_json: str) -> str:
    """Build the whole editing UI: mode toggle, both layer groups' worth
    of embedded data, and both CSV exports on one button.

    Args:
        edges_json: JSON array of every edge's own data (``u``, ``v``,
            ``key``, ``street_id``, ``street_name``, ``is_main``,
            ``coords``), for the Edit Edges mode.
        street_id_to_name_json: JSON object mapping every known
            street_id (as a string) to its current ``street_name``, used
            to auto-fill the target street's name box.
        palette_json: JSON array of the same hex color palette used for
            the Edit Streets mode's own per-street_id coloring, so a
            street_id gets the same color in both modes.

    Returns:
        The full ``<script>``/control HTML to embed in the map.
    """
    return f"""
<script>
window.__streetEdits = window.__streetEdits || {{}};
window.__edgeEdits = window.__edgeEdits || {{}};
window.__edgesByKey = {{}};
window.__edgesData = {edges_json};
window.__streetIdToName = {street_id_to_name_json};
window.__palette = {palette_json};
window.__edgeLayersByKey = {{}};

window.__edgesData.forEach(function(e) {{
    window.__edgesByKey[e.key] = e;
}});

function osmColorForStreetId(sid) {{
    if (sid === null || sid === undefined || sid === "") {{
        return "#000000";
    }}
    var s = String(sid);
    var hash = 0;
    for (var i = 0; i < s.length; i++) {{
        hash = (hash * 31 + s.charCodeAt(i)) % window.__palette.length;
    }}
    return window.__palette[(hash + window.__palette.length) % window.__palette.length];
}}

function osmStreetEditorSetName(streetId, input) {{
    window.__streetEdits[streetId] = window.__streetEdits[streetId] || {{}};
    window.__streetEdits[streetId].new_street_id = streetId;
    window.__streetEdits[streetId].street_name = input.value;
}}

function osmStreetEditorSetId(streetId, input) {{
    window.__streetEdits[streetId] = window.__streetEdits[streetId] || {{}};
    var raw = input.value.trim();
    window.__streetEdits[streetId].new_street_id = raw === "" ? "" : raw;
}}

window.__osmEdgeEditorTarget = null; // {{id, name}} once committed via Show/list-click/
                                      // Create-new/clicking a main edge; null shows every
                                      // street's own main path colored (the initial view).

function osmEdgeEditorEffective(edgeKey) {{
    var edge = window.__edgesByKey[edgeKey];
    var edit = window.__edgeEdits[edgeKey];
    if (!edit) {{
        return {{id: edge.street_id, name: edge.street_name, isMain: edge.is_main}};
    }}
    var unassigned = edit.new_street_id === null || edit.new_street_id === "";
    return {{id: edit.new_street_id, name: edit.street_name, isMain: !unassigned}};
}}

function osmEdgeEditorIsColored(edgeKey) {{
    var effective = osmEdgeEditorEffective(edgeKey);
    if (!effective.isMain || effective.id === null || effective.id === undefined || effective.id === "") {{
        return false;
    }}
    var target = window.__osmEdgeEditorTarget;
    if (!target) {{
        return true;
    }}
    return String(effective.id) === String(target.id);
}}

function osmEdgeEditorRestyle(edgeKey) {{
    var layer = window.__edgeLayersByKey[edgeKey];
    if (!layer) {{ return; }}
    if (osmEdgeEditorIsColored(edgeKey)) {{
        var effective = osmEdgeEditorEffective(edgeKey);
        layer.setStyle({{color: osmColorForStreetId(effective.id), weight: 7, dashArray: null, opacity: 1.0}});
        layer.bringToFront();
    }} else {{
        layer.setStyle({{color: "#000000", weight: 4, dashArray: "4,6", opacity: 0.6}});
        layer.bringToBack();
    }}
}}

function osmEdgeEditorRestyleAll() {{
    for (var edgeKey in window.__edgeLayersByKey) {{
        osmEdgeEditorRestyle(edgeKey);
    }}
}}

function osmEdgeEditorToggle(edgeKey) {{
    var target = window.__osmEdgeEditorTarget;
    var edge = window.__edgesByKey[edgeKey];
    var edit = window.__edgeEdits[edgeKey];

    if (!target) {{
        // No target selected yet: clicking one of the currently-colored
        // (main-path) edges picks its own street as the target, rather
        // than toggling anything - there's nothing to add/remove
        // relative to "no target".
        if (osmEdgeEditorIsColored(edgeKey)) {{
            var effective = osmEdgeEditorEffective(edgeKey);
            osmEdgeEditorCommitTarget(effective.id, effective.name);
        }}
        return;
    }}

    if (osmEdgeEditorIsColored(edgeKey)) {{
        if (edit) {{
            delete window.__edgeEdits[edgeKey];
        }} else {{
            window.__edgeEdits[edgeKey] = {{
                original_street_id: edge.street_id,
                original_street_name: edge.street_name,
                new_street_id: null,
                street_name: null
            }};
        }}
    }} else {{
        var originalId = edit ? edit.original_street_id : edge.street_id;
        var originalName = edit ? edit.original_street_name : edge.street_name;
        window.__edgeEdits[edgeKey] = {{
            original_street_id: originalId,
            original_street_name: originalName,
            new_street_id: target.id,
            street_name: target.name
        }};
    }}
    osmEdgeEditorRestyle(edgeKey);
}}

// --- Target selection: searchable id/name boxes, never freeform -------
//
// Typing in either box only filters a dropdown of EXISTING street_ids/
// names (substring match) - it never commits a target by itself. A
// target is only ever committed by clicking a dropdown row, clicking a
// currently-colored edge with no target selected, or "Create new
// street" (which mints a brand-new id no typing could ever produce).

function osmEdgeEditorKnownStreets() {{
    var seen = {{}};
    var list = [];
    for (var id in window.__streetIdToName) {{
        if (seen[id]) {{ continue; }}
        seen[id] = true;
        list.push({{id: id, name: window.__streetIdToName[id] || ""}});
    }}
    return list;
}}

function osmEdgeEditorRenderDropdown(boxKind) {{
    var isId = boxKind === "id";
    var box = document.getElementById(isId ? "osmEdgeEditorStreetIdBox" : "osmEdgeEditorStreetNameBox");
    var dropdown = document.getElementById(isId ? "osmEdgeEditorIdDropdown" : "osmEdgeEditorNameDropdown");
    var query = box.value.trim().toLowerCase();
    dropdown.innerHTML = "";
    // An empty query (just focused/clicked, nothing typed yet) shows
    // every known street, not none - the whole point of opening the
    // dropdown on focus is to let a caller browse the full list before
    // narrowing it down by typing.
    // No cap on the number of matches shown - the dropdown itself
    // scrolls (max-height/overflow-y on its own container), and a
    // caller browsing the full list on an empty query needs to actually
    // see every street, not just the first 20.
    var matches = osmEdgeEditorKnownStreets().filter(function(it) {{
        if (query === "") {{ return true; }}
        var field = (isId ? it.id : it.name).toLowerCase();
        return field.indexOf(query) !== -1;
    }});
    if (matches.length === 0) {{ dropdown.style.display = "none"; return; }}
    matches.forEach(function(it) {{
        var row = document.createElement("div");
        row.innerText = it.id + (it.name ? " - " + it.name : "");
        row.style.cursor = "pointer";
        row.style.padding = "2px 4px";
        // onmousedown (not onclick) fires before the box's own onblur,
        // so the selection still registers even though clicking the
        // dropdown moves focus away from the input.
        row.onmousedown = function() {{
            osmEdgeEditorCommitTarget(it.id, it.name);
        }};
        dropdown.appendChild(row);
    }});
    dropdown.style.display = "block";
}}

function osmEdgeEditorHideDropdown(boxKind) {{
    var dropdown = document.getElementById(boxKind === "id" ? "osmEdgeEditorIdDropdown" : "osmEdgeEditorNameDropdown");
    setTimeout(function() {{ dropdown.style.display = "none"; }}, 150);
}}

function osmEdgeEditorCommitTarget(id, name) {{
    window.__osmEdgeEditorTarget = {{id: String(id), name: name || ""}};
    document.getElementById("osmEdgeEditorStreetIdBox").value = id;
    document.getElementById("osmEdgeEditorStreetNameBox").value = name || "";
    document.getElementById("osmEdgeEditorIdDropdown").style.display = "none";
    document.getElementById("osmEdgeEditorNameDropdown").style.display = "none";
    osmEdgeEditorRestyleAll();
}}

function osmEdgeEditorReset() {{
    // Resets the CURRENTLY SELECTED target street back to whatever it
    // looked like before this session's own edits - not just undoing
    // one click, the whole street. Any edge whose edit either ADDED it
    // to this target (new_street_id === target.id) or REMOVED it from
    // this target (original_street_id === target.id, however its own
    // new_street_id currently reads) reverts entirely, deleting that
    // edit so the edge falls back to its own original street_id/name.
    // For a brand-new street_id minted via "Create new street", no edge
    // ever had it as an original_street_id, so this simply clears every
    // edge added to it this session - back to zero members, its own
    // real "original" state.
    var target = window.__osmEdgeEditorTarget;
    if (!target) {{ return; }}
    for (var edgeKey in window.__edgeEdits) {{
        var edit = window.__edgeEdits[edgeKey];
        if (String(edit.new_street_id) === String(target.id)
            || String(edit.original_street_id) === String(target.id)) {{
            delete window.__edgeEdits[edgeKey];
            osmEdgeEditorRestyle(edgeKey);
        }}
    }}
}}

function osmEdgeEditorCreateNew() {{
    var maxId = 0;
    osmEdgeEditorKnownStreets().forEach(function(it) {{
        var n = parseInt(it.id, 10);
        if (!isNaN(n) && n > maxId) {{ maxId = n; }}
    }});
    for (var edgeKey in window.__edgeEdits) {{
        var n2 = parseInt(window.__edgeEdits[edgeKey].new_street_id, 10);
        if (!isNaN(n2) && n2 > maxId) {{ maxId = n2; }}
    }}
    var newId = String(maxId + 1);
    window.__osmEdgeEditorTarget = {{id: newId, name: ""}};
    document.getElementById("osmEdgeEditorStreetIdBox").value = newId;
    document.getElementById("osmEdgeEditorStreetNameBox").value = "";
    osmEdgeEditorRestyleAll();
}}

function osmEditorSetMode(mode) {{
    var streetsGroup = window["__OSM_STREETS_GROUP_VAR__"];
    var edgesGroup = window["__OSM_EDGES_GROUP_VAR__"];
    var map = window["__OSM_MAP_VAR__"];
    var edgeControls = document.getElementById("osmEdgeEditorControls");
    if (mode === "edges") {{
        if (map.hasLayer(streetsGroup)) {{ map.removeLayer(streetsGroup); }}
        if (!map.hasLayer(edgesGroup)) {{ map.addLayer(edgesGroup); }}
        edgeControls.style.display = "block";
        osmEdgeEditorRestyleAll();
    }} else {{
        if (map.hasLayer(edgesGroup)) {{ map.removeLayer(edgesGroup); }}
        if (!map.hasLayer(streetsGroup)) {{ map.addLayer(streetsGroup); }}
        edgeControls.style.display = "none";
    }}
    document.getElementById("osmEditorModeButton").innerText =
        mode === "edges" ? "Switch to Edit Streets" : "Switch to Edit Edges";
    window.__osmEditorMode = mode;
}}

function osmEditorToggleMode() {{
    osmEditorSetMode(window.__osmEditorMode === "edges" ? "streets" : "edges");
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

function osmStreetEditorExportCsv() {{
    var streetRows = [["old_street_id", "new_street_id", "street_name"]];
    for (var streetId in window.__streetEdits) {{
        var edit = window.__streetEdits[streetId];
        streetRows.push([streetId, edit.new_street_id, edit.street_name || ""]);
    }}
    osmDownloadCsv("street_edits.csv", streetRows);

    var edgeRows = [["u", "v", "key", "new_street_id", "street_name"]];
    for (var edgeKey in window.__edgeEdits) {{
        var parts = edgeKey.split("|");
        var edgeEdit = window.__edgeEdits[edgeKey];
        edgeRows.push([
            parts[0], parts[1], parts[2],
            edgeEdit.new_street_id === null ? "None" : edgeEdit.new_street_id,
            edgeEdit.street_name || ""
        ]);
    }}
    osmDownloadCsv("edge_edits.csv", edgeRows);
}}
</script>
<div style="position: fixed; top: 10px; right: 10px; z-index: 9999;">
    <button id="osmEditorModeButton" onclick="osmEditorToggleMode()"
            style="padding: 8px 14px; font-size: 14px; cursor: pointer;">
        Switch to Edit Edges
    </button>
    <button onclick="osmStreetEditorExportCsv()"
            style="padding: 8px 14px; font-size: 14px; cursor: pointer;">
        Export CSV
    </button>
</div>
<div id="osmEdgeEditorControls" style="display: none; position: fixed; top: 60px; right: 10px;
     z-index: 9999; background: white; padding: 10px; border: 1px solid #333; width: 260px;">
    <div style="position: relative;">
        Street ID:<br>
        <input type="text" id="osmEdgeEditorStreetIdBox" autocomplete="off"
               style="width: 100%; box-sizing: border-box;"
               oninput="osmEdgeEditorRenderDropdown('id')"
               onfocus="osmEdgeEditorRenderDropdown('id')"
               onblur="osmEdgeEditorHideDropdown('id')">
        <div id="osmEdgeEditorIdDropdown" style="display: none; position: absolute; top: 100%; left: 0;
             right: 0; background: white; border: 1px solid #999; max-height: 150px; overflow-y: auto;
             z-index: 10000;"></div>
    </div>
    <div style="position: relative; margin-top: 8px;">
        Street name:<br>
        <input type="text" id="osmEdgeEditorStreetNameBox" autocomplete="off"
               style="width: 100%; box-sizing: border-box;"
               oninput="osmEdgeEditorRenderDropdown('name')"
               onfocus="osmEdgeEditorRenderDropdown('name')"
               onblur="osmEdgeEditorHideDropdown('name')">
        <div id="osmEdgeEditorNameDropdown" style="display: none; position: absolute; top: 100%; left: 0;
             right: 0; background: white; border: 1px solid #999; max-height: 150px; overflow-y: auto;
             z-index: 10000;"></div>
    </div>
    <div style="margin-top: 8px;">
        <button onclick="osmEdgeEditorReset()">Reset</button>
        <button onclick="osmEdgeEditorCreateNew()">Create new street</button>
    </div>
</div>
"""


def build_street_editor_map(
    streets_gdf: gpd.GeoDataFrame, edges_gdf: gpd.GeoDataFrame, out_html_path: str
) -> folium.Map:
    """Render an interactive, editable folium map with two modes.

    The basemap is Google's hybrid (satellite + labels) tile layer at
    60% opacity, and nothing else, no OpenStreetMap tiles, no layer
    picker.

    **Edit Streets** (the default view): one ``folium.PolyLine`` per
    street_id, colored to distinguish adjacent streets. Clicking a
    street opens a popup with editable ``street_id``/``street_name``
    text inputs, plus a read-only ``highway``/``street_rank`` line (e.g.
    ``"primary 1"``).

    **Edit Edges**: every individual edge in ``edges_gdf``, not just the
    dissolved main path. Initially (no target street selected yet),
    every street's own main-path edges are drawn colored, exactly like
    Edit Streets mode but at edge granularity; everything else (every
    non-main edge) is a black dotted line behind them.

    A "Street ID" and "Street name" box (each a searchable dropdown -
    focusing one with nothing typed lists every EXISTING street_id/name,
    typing narrows it by substring match, nothing is ever created just
    by typing) pick a target street. Selecting a row in either dropdown,
    or clicking one of the currently-colored edges while no target is
    selected, commits that street as the target - filtering the view
    down to ONLY that street's own edges in color, everything else
    (including every other street's main path) black dotted. "Create new
    street" mints a fresh, never-before-used street_id (the id box gets
    it, the name box is left empty but editable) and commits it as the
    target with nothing colored yet, since it has no members. "Reset"
    reverts every edit made THIS session to whichever street is
    currently the target - every edge added to it, and every edge
    removed from it, both go back to their own original state - not
    just undoing the last click, the whole street at once. For a
    freshly created street_id, its own "original" state is simply no
    members at all.

    Once a target is selected: clicking a black-dotted edge assigns it
    to the target, inheriting the target's own name. Clicking a colored
    edge undoes that: if it was added THIS session, it reverts to
    whatever it was before (its original street_id/name, from
    ``edges_gdf`` as passed in); if it was never touched this session
    (an original main-path member), it's unassigned entirely
    (``street_id``/``street_name`` become ``None``). This only ever
    controls street_id/name MEMBERSHIP - the actual main-path
    (``is_main_direction``) and edge orientation are still recomputed by
    ``street_ids``' main-path logic on the next full pipeline run, using
    that membership as its input (see ``apply_street_edits``).

    A single "Export CSV" button (present in both modes) downloads BOTH
    files at once: ``street_edits.csv`` (street-level edits, unchanged
    format) and ``edge_edits.csv`` (``u, v, key, new_street_id,
    street_name`` - one row per edge actually touched this session).
    Both are ``apply_street_edits``' own expected input.

    Args:
        streets_gdf: The streets GeoDataFrame (main-path edges only,
            e.g. from ``pipeline.streets_gdf_from_edges``), with
            ``street_id``/``street_name`` columns, for Edit Streets mode.
        edges_gdf: Every edge (main and non-main), with ``street_id``/
            ``street_name``/``is_main_direction`` columns, for Edit
            Edges mode.
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

    fmap = _new_map(center, zoom_start=15)

    streets_group = folium.FeatureGroup(name="streets", show=True)
    for i, row in streets_wgs84.iterrows():
        street_id = row["street_id"]
        street_name = row.get("street_name")
        highway = row.get("highway")
        street_rank = row.get("street_rank")
        color = _PALETTE[i % len(_PALETTE)]
        for part_coords in _line_coord_lists(row.geometry):
            coords = [(lat, lon) for lon, lat in part_coords]
            line = folium.PolyLine(
                coords,
                color=color,
                weight=4,
                popup=folium.Popup(
                    _popup_html(street_id, street_name, highway, street_rank),
                    max_width=300,
                ),
            )
            line.add_to(streets_group)
    streets_group.add_to(fmap)

    edges_group = folium.FeatureGroup(name="edges", show=False)
    edges_wgs84 = edges_gdf.to_crs(4326) if not edges_gdf.empty else edges_gdf
    edges_data = []
    edge_layer_by_key = {}
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
        line = folium.PolyLine(coords, color="#000000", weight=4, dash_array="4,6", opacity=0.6)
        line.add_to(edges_group)
        edge_layer_by_key[edge_key] = line.get_name()
    edges_group.add_to(fmap)

    # Bound directly to fmap.get_root().html (not fmap itself) - a plain
    # Element added via .add_to(fmap) never makes it into the rendered
    # output (folium.Map's own render() doesn't walk arbitrary Element
    # children the way a FeatureGroup/PolyLine does), while the root
    # Figure's own .html section always does.
    #
    # BUT: the actual `var poly_line_xxx = L.polyline(...)` declarations
    # for every edge live in the root Figure's own .script section,
    # which branca/folium renders AFTER .html's own content in the final
    # page - confirmed empirically (a naive `.html.add_child` here throws
    # "poly_line_xxx is not defined" in a real browser, silently
    # aborting this whole script block, which left __edgeLayersByKey
    # permanently empty and every edge un-clickable - caught via
    # Playwright, not by string-content assertions alone). Deferring
    # this whole block to DOMContentLoaded guarantees every inline
    # <script> earlier in the document (including .script's own
    # polyline declarations, wherever they end up) has already run.
    edge_bindings_js = "".join(
        f'window.__edgeLayersByKey["{edge_key}"] = {layer_var};'
        f'{layer_var}.on("click", function() {{ osmEdgeEditorToggle("{edge_key}"); }});'
        for edge_key, layer_var in edge_layer_by_key.items()
    )
    fmap.get_root().html.add_child(
        Element(
            "<script>document.addEventListener('DOMContentLoaded', function() {"
            "window.__edgeLayersByKey = window.__edgeLayersByKey || {};"
            f"{edge_bindings_js}"
            # Some browsers restore a form field's own last-typed value
            # across a reload/reopen of the same file (independent of
            # autocomplete="off", which only suppresses the dropdown
            # suggestion list, not this restoration) - force both boxes
            # back to empty on every fresh load, so a stale name never
            # shows up before the user has picked anything.
            "var idBox = document.getElementById('osmEdgeEditorStreetIdBox');"
            "var nameBox = document.getElementById('osmEdgeEditorStreetNameBox');"
            "if (idBox) { idBox.value = ''; }"
            "if (nameBox) { nameBox.value = ''; }"
            "});</script>"
        )
    )

    street_id_to_name = {
        str(sid): (None if pd.isna(name) else name)
        for sid, name in zip(streets_gdf["street_id"], streets_gdf.get("street_name", []))
    }

    ui_html = _edit_ui_js(
        json.dumps(edges_data), json.dumps(street_id_to_name), json.dumps(_PALETTE)
    )
    ui_html = ui_html.replace("__OSM_STREETS_GROUP_VAR__", streets_group.get_name())
    ui_html = ui_html.replace("__OSM_EDGES_GROUP_VAR__", edges_group.get_name())
    ui_html = ui_html.replace("__OSM_MAP_VAR__", fmap.get_name())

    fmap.get_root().html.add_child(Element(ui_html))
    fmap.get_root().script.add_child(
        Element(f'window.__osmEditorMode = "streets";')
    )
    fmap.save(out_html_path)
    return fmap


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
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, pd.DataFrame]:
    """Re-apply manually edited street/edge csvs onto the labeled network.

    Reads ``edited_csv_path`` (columns ``old_street_id``, ``new_street_id``,
    ``street_name``, as exported by ``build_street_editor_map``'s Edit
    Streets mode), remaps ``street_id``/``street_name`` across
    ``edges_gdf``/``streets_df`` for every edge sharing an ``old_street_id``.
    Then, if ``edge_edits_csv_path`` is given (Edit Edges mode's own
    export), applies its per-edge overrides (columns ``u``, ``v``,
    ``key``, ``new_street_id``, ``street_name``) on top - these win over
    the street-level remap for the specific edges they name, since
    they're a more specific, more recent edit. Both directed rows of the
    same physical edge (``(u, v, key)`` and its ``(v, u, key)``
    counterpart, if present) are kept in sync.

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
        if pd.isna(raw) or str(raw).strip() in ("", "None"):
            return None
        return _coerce_id(str(raw))

    for _, edit_row in edits.iterrows():
        old_id = str(edit_row["old_street_id"])
        id_map[old_id] = _parse_new_id(edit_row.get("new_street_id"))
        street_name = edit_row.get("street_name")
        name_map[old_id] = None if pd.isna(street_name) or street_name == "" else street_name

    def _remap_id(street_id):
        key = str(street_id)
        if key not in id_map:
            return street_id
        return id_map[key]

    def _remap_name(street_id, current_name):
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
    if edge_edits_csv_path is not None:
        edge_edits = pd.read_csv(edge_edits_csv_path, dtype=str)
        edges_reset = edges_gdf.reset_index()

        def _match_key(u, v, key):
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

        for _, edit_row in edge_edits.iterrows():
            new_sid = _parse_new_id(edit_row.get("new_street_id"))
            street_name = edit_row.get("street_name")
            street_name = None if pd.isna(street_name) or street_name == "" else street_name

            for u_col, v_col in (("u", "v"), ("v", "u")):
                match = _match_key(edit_row[u_col], edit_row[v_col], edit_row["key"])
                pos = edge_index_by_key.get(match)
                if pos is None:
                    continue
                edges_reset.loc[pos, "street_id"] = new_sid
                edges_reset.loc[pos, "street_name"] = street_name

            if new_sid is not None and str(new_sid) not in known_ids:
                new_rows[str(new_sid)] = street_name

        edges_gdf = edges_reset.set_index(["u", "v", "key"])

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

    edges_gdf, streets_df = street_direction.assign_street_directionality(
        nodes_gdf,
        edges_gdf,
        streets_df,
        origin=origin,
        transversal_road_pct=transversal_road_pct,
        numbering_mode=numbering_mode,
        direction_deg=direction_deg if direction_deg is not None else 0.0,
    )
    edges_gdf, streets_df = numbering.assign_house_numbers(
        edges_gdf, streets_df, number_divisor_m=number_divisor_m
    )

    return nodes_gdf.copy(), edges_gdf, streets_df
