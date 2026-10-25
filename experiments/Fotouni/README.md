# experiments/Fotouni

Fotouni addresses in **path numbering** mode with branch-road names.

- `fotouni.py` - the run: applies the community street/edge edits of 2026-09-15 (names resolved: a local name always overrides the OSM name, a blank one never removes it, the newest file wins on conflicts), numbers every street by shortest road-network distance to Carrefour Brouwer (hôtel) - streets are cut so each begins at its end nearest to the reference - and names unnamed roads that branch off a named road (`3-xyz`, `4A-3-xyz`).
- `inputs/` - the Bandja run (`nodes.gpkg`, `edges.gpkg`, `streets.csv`; the graph the community names were assigned on, so street ids match) and the two edit files.
- `fotouni/` - outputs: `edges.gpkg`, `nodes.gpkg`, `streets.gpkg`, `streets.csv`, `street_id_mapping.csv` (original id -> new id of every street), `branch_names.csv`, `branch_names_map.html`, `geocode_map.html`, and `resolved_edits/` (the edit files after name resolution).

- `naming_plan.py` - plans the fewest streets that still need a hand-given name so that every street has a name or a derived name (two levels): writes `fotouni/naming_plan.csv` (priority order, with position), `naming_coverage.csv`, `naming_plan_area.csv` (targets limited to the Fotouni area, optional input) and `naming_plan_map.html`.

Run: `uv run experiments/Fotouni/fotouni.py`, then `uv run --extra planning experiments/Fotouni/naming_plan.py`.

- `building_attributes.py` - use (residential / office / commercial / industrial / public), residents, name, English service text and workers of every surveyed building from the EFYL questionnaire (`inputs/survey_export_20260827.csv`, anonymised): rule tables plus manual spelling corrections; writes `inputs/building_attributes.csv`, which `make_app_project.py` joins to the buildings. Rows to check by hand are in its `review` column.
- `make_app_project.py` - builds `projects/fotouni`; the Fotouni area of interest is derived from the surveyed buildings (`osmaddresssystem.app.area.aoi_from_points`: concave outline + 50 m).
