# osmaddresssystem.app

The community address app: a map in the browser (Leaflet) on top of a small Python server (FastAPI) that does all the computing. Numbering is always **path numbering**.

| File | What |
|---|---|
| `server.py` | HTTP API (see its docstring) and the static front end. |
| `engine.py` | The compute pipeline: plan names, route through named streets, cut and re-assign street ids, derive sub-street names, check coverage, house numbers, addresses, street signs, warnings. |
| `roads.py` | Edit streets (new roads, duplicates, deleted edges): read KML / GeoJSON / GeoPackage / shapefile lines, generate nodes, connect to the network (auto within 10 m, by hand otherwise), move the connected ends. |
| `project.py` | The project state (reference, settings, names, road points of houses, drawn roads) and the project folder layout. |
| `store.py` | The project database (SQLAlchemy: SQLite or PostgreSQL): edit log, undo / redo, conflicts, checkpoints, restore, `street_names` and `street_ids` tables. |
| `setup.py` | Build `network/` from `input/streets.osm` (+ `area.gpkg`) when it does not exist yet. |
| `__main__.py` | `python -m osmaddresssystem.app init | setup | serve`. |
| `static/` | `index.html`, `app.js`, `style.css` and a vendored Leaflet (the app works offline except for the base map tiles). |

Run: `uv run --extra app --extra planning python -m osmaddresssystem.app serve --project projects/fotouni` and open http://127.0.0.1:8000.
See `../../reports/app_design.md` for the design.

## Tracing missing roads

In **Edit streets**, expand **Draw or prolong a road**. Start a new road or
choose an existing dead end to prolong. **Click points** places individual
bends; **Freehand trace** records a line while the left mouse button is held.
Release to review the draft. Switch back to Click points to pan the map.
Drag the white handles to reshape the draft, click a small midpoint to insert
a bend, or right-click a handle to remove it. A prolongation's starting node
stays fixed. Remove last point, Cancel and Finish remain available.

**Snap to existing roads** controls connections to the existing network.
Turn it off to place the line precisely without automatic endpoint connections;
those endpoints can be connected later. Freehand strokes retain their interior
shape and snap only at their final ends. Finish saves the line, while street IDs
are applied by the normal recomputation workflow. Existing saved roads are not
changed by these draft tools. Satellite tracing needs review against field
knowledge, especially for obscured paths and house entrances.

## Correcting an existing road

In **Draw, prolong or reshape a road**, choose **Reshape an existing road**,
then click a cyan section. Its existing geometry becomes the draft, so only
the misplaced bends need adjustment. Orange dashes show the previous shape.
Drag the white points, insert bends with the small midpoint handles, or use
Freehand trace to replace the interior. The green start and red end stay fixed
to preserve junctions. Reset draft restores the original shape before saving.

Finish stores a geometry override as an undoable event, without deleting the
road, changing its graph identity, or rewriting the source network files.
Both directed edges and their lengths are updated. Recompute applies the new
lengths to numbering and addresses, which can therefore change. Concurrent
project edits require reloading the section before saving. Undo/redo and
checkpoints include these corrections.

This tool does not move junctions, create connections at crossings, or verify
that a visible strip is a public/passable road. Later network imports that
replace an edge or move its endpoints may invalidate its override; corrections
are only applied to matching references with matching endpoints. Compare
imagery alignment with nearby roads and survey information before systematic
correction. Review small areas rather than replacing the full road network.

## Connected tracing, partial deletion and work areas

**Trace route: start → end** first records a starting point and a planned end.
Choosing the end does not draw a line to it. Click intermediate bends or use
Freehand trace in short strokes; each stroke must begin within 12 screen pixels
of the previous endpoint and is anchored exactly to that endpoint. Gapped strokes
are rejected. Undo last stroke lets you retry a section. Finish requires reaching
the planned end, or explicitly checking **Stop here: path beyond this point is
unclear**. That flag saves the current endpoint without snapping it onto another
road, shows a question mark and feeds a review warning after computation. It is
a manual uncertainty record, not an image-recognition result.

**Remove part of a road** selects one existing section, then two positions on it.
The red preview is the only part deleted. Outside fragments retain their source
street attributes and get stable new cut endpoints; source input/network files
are not changed. Gaps may disconnect a street and change computed parts/numbers.
The cut is one event with Undo/Redo/checkpoint recovery. Endpoint trimming and
later cuts on retained fragments are supported. A changed/missing source target
raises a stale-cut warning rather than silently applying the cut to another road.
Inspect this warning before using computed output.

**Work areas** creates a metre-based grid clipped to the project's area of
interest (or its supplied boundary if no AOI is tagged). Choose Automatic or a
metre-size preset, up to 400 areas. Selecting a cell zooms/highlights it and narrows the reshape/cut
picker by intersecting bounds, while keeping crossing roads whole. Set Not
reviewed, In progress, Reviewed or Needs review and add a note. The grid and
progress are saved as recoverable events and included in checkpoints. These
administrative labels do not change network/address hashes or trigger a
computation. They are manual work tracking, not per-geometry approval, and do
not automatically validate or invalidate later road edits. A grid cannot be
overwritten through Create; Undo/restore retains earlier review history.

New projects and existing databases load optional `cuts`/`work_areas` state
without a destructive migration. Keep a database backup for rollback: older
application versions do not understand these event types. Do not run an older
server against a project after using the new tools.

## Street-ID edits

Street-ID edits now validate the complete joined path before saving: differing
ranks, branches, loops and disconnected results are refused, including forced
joins. Pending rank and ID changes are included in the check. New manual IDs
avoid existing part/branch/name IDs, and manually split streets are protected
from automatic recutting. The split option uses the same topology check as the
API. A successful ID edit starts recomputation so the map shows its actual
result; other edits retain the existing deferred recomputation workflow.

## Possible future improvements

Not done yet, only ideas for later: the map of the community app (`osmaddresssystem/app/static`, Leaflet) and the offline street editor (`osmaddresssystem/street_editor.py`, folium / Leaflet) could be rebuilt on **[MapLibre GL JS](https://maplibre.org/)** (vector tiles, smooth rendering of many streets and buildings, rotation / pitch) and could use **GeoLibre** tools for the editing functions (drawing, snapping, splitting and merging of lines). The current Leaflet code keeps working as it is.


Right-click a road in the full map to select its current section. Where lines
are close or overlap, use the selector and cyan highlight. Delete a portion uses
two points on that section; Delete this whole section previews its complete
removal before confirmation. Undo restores it.

To reconnect a corrected road: right-click it, choose Connect an endpoint to a
road, select its free A/B endpoint, click the target road at the intended
junction, then confirm Connect here. This creates a shared graph node, splitting
the target if necessary; visual contact alone is insufficient. Street ID grouping
is separate. Already-shared nodes need no connection edit. Existing junctions
cannot be moved by this tool; for gaps over 60 m trace a connector instead.


Choose English or Francais in the top bar of either page. The preference is
saved only in that browser. It changes the interface, not street names or map
data, and does not discard an unfinished road or a typed name. Core controls
and common warnings are translated; unmapped technical diagnostics may remain
in English. To add a language, extend the dictionary in `static/i18n.js` and
keep user data outside translated literal strings. The editor uses tagged HTML
literal parts so data interpolated into a label is never translated by accident.


Accounts are now required by the serve command. On the local review server,
create the first administrator in the startup screen; on a hosted server use
`python -m osmaddresssystem.app account --project PATH --username admin --name NAME`
from its console. Passphrases are entered privately, not as command arguments.
Administrators invite teammates through Team; History shows retained authors,
operations and before/after values. Contributor, mapper, viewer and administrator
permissions are checked by the API. See `docs/accounts-and-history.md` for
setup, recovery, role scope, backup/migration and hosting requirements.
