# OSMAddressSystem

Python package (`osmaddresssystem`) that turns an OpenStreetMap street network into an address system: street identities (merging edges into named streets), street direction, house numbering (radial or grid), geocoding / reverse geocoding, routing, street and POI sign maps, and a street-editor map for manual corrections.

| Folder | Contents |
|---|---|
| `osmaddresssystem/` | The package. `street_ids.py` (street identity), `numbering.py`, `street_direction.py`, `name_matching.py`, `geocode_map.py`, `street_editor.py`, `street_signs.py`, `poi_signs.py`, `routing.py`, `subregions.py`, `branches.py` (names for unnamed roads that branch off a named road, e.g. `3-xyz`), `path_numbering.py` (numbering by shortest network distance to a reference point, with street cutting), `naming_plan.py` (fewest streets to name by hand so that every street has a name or a derived name), `pipeline.py` (one-call pipeline), `default_values.py` (all tunable defaults), `utils.py`, `osm.py`. |
| `tests/` | pytest suite (synthetic networks and real-data regression tests). |
| `examples/` | `tutorial/` notebooks, `fotouni/` and `nuevos_ministerios/` worked examples. |
| `experiments/` | Full runs for Bandja (Fotouni) and Douala, with generated interactive maps and `index.html` listing them. |
| `deploy/` | Hosting: Dockerfile, docker-compose (SQLite or PostgreSQL, Caddy HTTPS), systemd unit, backup script, see `deploy/README.md`. |
| `osmaddresssystem/app/` | The community address app (browser map + FastAPI backend + SQLite / PostgreSQL project database), see `reports/app_design.md`. |
| `projects/` | App projects, e.g. `projects/fotouni`. |
| `reports/` | Design notes (street identity, numbering, geocoding, styling, OSM upload of street names). |

Install and test: `uv sync && uv run pytest`.

## Possible future improvements

Not done yet, only ideas for later: the map of the community app (`osmaddresssystem/app/static`, Leaflet) and the offline street editor (`osmaddresssystem/street_editor.py`, folium / Leaflet) could be rebuilt on **[MapLibre GL JS](https://maplibre.org/)** (vector tiles, smooth rendering of many streets and buildings, rotation / pitch) and could use **GeoLibre** tools for the editing functions (drawing, snapping, splitting and merging of lines). The current Leaflet code keeps working as it is.
