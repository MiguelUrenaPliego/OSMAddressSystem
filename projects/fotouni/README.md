# Fotouni project (community address app)

Everything for Fotouni (Bandja commune) in one folder:

| Path | Contents |
|---|---|
| `input/streets.osm` | the OpenStreetMap streets the network was generated from (the old file, so the street ids of the community edits match) |
| `input/street_edits.csv`, `input/edge_edits.csv` | street names / edge edits of the community (2026-09-15); names from OSM stay unless a community name is given |
| `input/area.gpkg` | the Bandja commune boundary and the Fotouni area of interest (derived from the surveyed buildings: concave outline + 50 m) |
| `input/points.gpkg` | 102 important points: squares, intersections, facilities of the community workbook, OSM places, schools, health centres, chiefferies |
| `input/buildings.gpkg` | the 2,127 surveyed buildings of the Fotouni town survey with `use`, `residents`, `name`, `service`, `workers` (from the questionnaire, spelling corrected by hand in `experiments/Fotouni/building_attributes.py`) |
| `input/missing_roads.kml` | the 24 roads drawn by the community in Google Earth: load it in the **Edit streets** tab (preview, then accept) |
| `input/default_reference.json` | the default reference point (Carrefour Brouwer, hôtel): every start asks to confirm or move it |
| `network/` | the labelled street network (OSM names included) |
| `project.db` | the project database: starts with no reference point and no names; fills as people work (edits are saved at once; Save checkpoint labels a state) |

Run (from `code/OSMAddressSystem`):

```bash
uv sync --extra app --extra planning
uv run python -m osmaddresssystem.app serve --project projects/fotouni      # http://127.0.0.1:8000
```

Rebuild the folder from the inputs of the repository: `uv run experiments/Fotouni/make_app_project.py`.
The first thing the map asks for is the reference point; Carrefour Brouwer (hôtel) is at 5.3201 N, 10.2275 E.
