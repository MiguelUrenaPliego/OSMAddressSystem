# tests

pytest suite: `test_street_ids*.py` (street identity incl. real-data regressions), `test_numbering.py`, `test_street_direction.py`, `test_name_matching.py`, `test_subregions.py`, `test_geocode_map.py`, `test_street_editor.py`.

The community app also has backend, browser, naming/connectivity, path-numbering
and geometry regressions. Street-ID rules are in `test_street_assignment_rules.py`;
the visible split workflow is in `test_street_assignment_browser.py`. The backend
batch test covers split/join sequences through compute, checkpoint, undo, restore
and reopening.

On native Windows, app browser tests reuse installed Edge/Chrome/Chromium through
`app_helpers.launch_browser`. Set `ADDRESS_TEST_BROWSER` for another installed
executable. Workflow tests block public basemap tile requests to avoid downloads
and external network stalls; test imagery availability separately. Legacy
geocode/street-editor browser tests still use their own bundled-browser setup.
PostgreSQL tests skip when optional `pgserver` is absent.

Use a fresh scratch directory per run if the machine's shared pytest temporary
directory has access restrictions, for example:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_street_assignment_rules.py tests/test_street_assignment_browser.py -q --basetemp cache/id-check-UNIQUE
```

Do not reuse an existing project folder as pytest's basetemp: pytest clears it.
See `../docs/requirements.md` for actual results, outstanding requirements and
the current next task.
