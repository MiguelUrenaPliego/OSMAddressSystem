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

## Possible future improvements

Not done yet, only ideas for later: the map of the community app (`osmaddresssystem/app/static`, Leaflet) and the offline street editor (`osmaddresssystem/street_editor.py`, folium / Leaflet) could be rebuilt on **[MapLibre GL JS](https://maplibre.org/)** (vector tiles, smooth rendering of many streets and buildings, rotation / pitch) and could use **GeoLibre** tools for the editing functions (drawing, snapping, splitting and merging of lines). The current Leaflet code keeps working as it is.
