# osmaddresssystem (package)

See the module docstrings. Pipeline order: `osm` (download) -> `street_ids` (street identity) -> `street_direction` (modes radial, grid, path; `path_numbering` for the latter) -> `numbering` -> `branches` (optional; `naming_plan` plans which streets to name by hand) -> `geocode_map` / `street_signs` / `poi_signs` / `routing`; `street_editor` for manual fixes; `pipeline.run` chains the core steps; `default_values` holds every default threshold.
