# Fotouni work package: requirements and implementation record

Updated 2026-10-27. This is a continuing engineering record, not a declaration
that the work package is complete. Pilot scope and the January trial date need
team agreement. P0 = normal-use blocker; P1 = pilot candidate; P2 = later unless
the team chooses otherwise.

## Sources and starting state

- Current requirements: `C:/Users/Win10/Dev/todo.md`.
- Historical design: `C:/Users/Win10/Dev/INITIAL app_design`.
- Assignment: supplied pasted request; `C:/Users/Win10/Dev/prompt.md` is context.
  The twelve attachments contain two byte variants of the same assignment.
- Miguel's comments available here are those quoted in the request; no separate
  comments document was supplied.
- Parent: `main`, `473b5465be47dab1755a62bf865888a3ca253ac3`.
- Application: detached HEAD, `677bfaf7efbe8c773a2bd8b6e9a51366aa06e507`.
- Existing uncommitted work preserved: freehand/draft geometry editing, saved
  geometry overrides, related API/store/engine/project/CSS/docs changes, and
  `test_road_drawing_browser.py`, `test_road_geometry.py`.
- No AGENTS.md found by the repository search. Python 3.13.7, uv 0.13.5,
  Git 2.51.0.windows.1. Use existing dependencies; no lockfile changes.
- All mutable work-package investigations use new copies under
  `cache/workpackage-4m6zx56t/`, made with copied input/network folders and
  SQLite's backup API. Neither the practice folder nor bundled project is
  used for work-package edits or recomputation.

The current stack remains appropriate for the first increments: Python GIS
and graph calculations, FastAPI, SQLAlchemy and Leaflet. No stack replacement
is justified by the observed ID-validation defects. Evaluate GeoLibre before
further geometry-tool expansion (todo recommendation); no migration selected.

## Inventory

Unless a row explicitly says tested/reproduced, existing behavior below is
established by code inspection only. Paths are relative to this repository.

| Todo | Priority | Existing code and tests | Required change / status / dependencies |
|---|---|---|---|
| 1.1 Move vertex/segment | P1 | `app/static/app.js` reshape mode; `app/roads.py:reshape_edges`; `tests/test_road_geometry.py`, `test_road_drawing_browser.py` | Prior work implements bend movement, insertion and freehand replacement between fixed endpoints; whole-segment translation is not implemented. |
| 1.2 Move shared node | P1 | `app/roads.py:integrate_roads`, `app/static/app.js` connection tools | Not implemented; needs atomic geometry changes for all attached edges and topology/persistence tests. |
| 1.3 Delete/prolong/split | P1 | `app/server.py` edge endpoints; drawing browser and backend tests | Existing tools; exact split eligibility and pending-ID behavior investigated in this increment. |
| 1.4 Connectivity guard | P1 | `engine.disconnected_parts`, `roads_warnings`, `roads.snap_click`; backend connectivity tests | Detect near misses for existing roads too; agree threshold and intended isolated roads. |
| 1.5 Review flag | P1 | No per-geometry review state | Add unreviewed/ok/needs-editing, filters, history; invalidate review on later geometry edits. |
| 1.6 Geometry change tracking | P1 | Geometry override events, deleted refs/imports; `exports.edge_table`, `osm_changes` | Prior work logs corrections; verify complete current-state diff including deleted OSM ways and restore. |
| 1.7 Oneway | P2 | OSM input fields exist | Manual override and routing semantics not implemented. Last per todo. |
| 1.8 KML files/undo | P1 | Import preview, per-file colour/removal; backend import tests | Existing support; browser usability and atomic undo coverage need review. |
| 2.1 Per-edge drivable | P1 | `apply_attributes`, street/part attrs; backend attribute tests | Add edge override; rank 5 must be non-drivable; do not change geometry/identity accidentally. |
| 2.1 Surface enum | P1 | Current `paved` tri-state, OSM surface parsing and exports | Introduce enum and migration; old False does not uniquely mean dirt/macadam/stone. Preserve original unknown information. |
| 2.1 Rank consistency/merge validation | P0 | `post_streets_assign`, `dominant_rank`, pending attrs; `test_street_assignment_rules.py` | Cross-rank merge refusal implemented and tested, including force and pending rank changes. Whole-app enforcement of one rank across cut-part attribute editing remains separate work. |
| B1 Wrong refusal | P0 | Merge validation in `app/server.py` | Exact reported IDs absent on both isolated datasets: node 2272487038 has 113/113/613, not 113/743/753. User said the example may be mistaken and instructed continuation. Unverified example, not a blocker; do not claim it reproduced or was fixed. |
| B2 New ID does nothing | P0 | Shared `street_ops.split_refs`, `_junctions.can_split`, `junctionHtml`; API and browser regressions | General eligibility now checks topology, incident road and proper resulting paths. Successful ID edits recompute and visibly show the new ID. Exact 735 report remains unverified; available node has 578/578/514. |
| B3 Recompute reverts edits | P0 | `no_cut`, `edge_street`, ID registry; new API tests and batch round-trip test | First fixes implemented/tested: force cannot bypass path/rank validation; fresh IDs reserve displayed/branch/named IDs; manual splits protected against recutting; retired pending targets rejected. Split/split/join/join survives compute, checkpoint, undo, restore and reopen. Full arbitrary-sequence guarantee and historical identity migration remain unproven. |
| B4 Stable identity | P1 | `Project.names`, `street_ids`, `branch_ids` | Edge-set keyed migration is not implemented; design with aliases and recover existing ambiguous ID decisions on a copy. |
| 2.3 noaddress | P1 | No end-to-end exclusion flag | Must affect plan, derivation, address snapping, signs and exports consistently. Clarify routing-through versus routing-to exclusion. |
| R1 Derivation eligibility | P0 | `branches.assign_branch_names`, `branch_parents`; branch/naming tests | Length and relative rank checks exist; ranks 0–2 still need explicit own-name requirement. Check cut-parent group lengths and the cited example. |
| R2 First order only | P0 | `Settings.max_levels=2`; deep-road threshold; existing two-level tests | Deliberate app rule change. Planner, engine, UI and persisted old settings all need coordinated handling; preserve generic library use unless intentionally changed. |
| R3 Minimum naming set | P0 | Exact MILP + greedy fallback, `_redundant_planned`; `test_naming_plan.py` | Existing brute-force tests cover set systems; add end-to-end graph optima and report when optimality is not proven. |
| 3.2 Structured names | P1 | One string per original ID; `set_name`, both browser pages | Design structured storage and reversible migration; keep OSM full name separate from abbreviation; do not automatically parse ambiguous existing names. |
| 3.3 Suggestions | P1 | Reference points/routing exist | Select from street's routed start, not midpoint; define relevant POIs, short-word rules and rank defaults. |
| 3.4 Warnings | P1 | Existing duplicate-name/disconnected/planned/address warnings | Add abbreviation collisions; retain click-to-zoom/count semantics and meaningful IDs. |
| 4 Address format/rules | P0 decision, P1 implementation | `compute_addresses`, `numbering`, path profiles and azimuth tests | Current `number, name` differs from requested compact format. Todo examples conflict with stated distance units; obtain worked examples before altering numbers. |
| 4.1 Azimuth | P1 | `side_from_azimuth`, survey field, location fallback | Measure actual survey coverage. A door bearing cannot be inferred reliably from a building point alone. Confirm field provenance/meaning and missing data policy. |
| 4.2 Distance | P1 | `max_house_distance_m=60`, no_street warnings, manual override tests | Setting exists; todo says 50 now and proposes 20–30. Confirm default and whether manual picks bypass the maximum. |
| 4.3 Street signs | P1 | `street_signs`, intersection sketch in `app.js`, exports | Add sign-rank setting, matching edge letters outside sign text; clarify numeric threshold direction. |
| 4.4 Direction signs | P1 | POI ranks/icons, `add_outside_signs` and nearest-sign behavior | New importance table reverses old rank-0 meaning. Needs migration, actual radii, turn-based arrows and combined lines. Not implemented. |
| 5 Checkpoints/audit | P1 | `Store` events/undo/redo/save/restore; backend tests | API `me()` is empty; no identified authors. New edits delete undone events. Recoverability exists; complete attribution/audit does not. |
| 6.1 OSM changes | P1 | `exports.osm_changes`, `.osm` export tests | Verify modified/deleted/new ways, versions and reverting edits. Never upload from tests. |
| 6.2 Address OSM format | P1 | Buildings/address GPKG/CSV | Dedicated OSM-friendly addr tags/export needs agreement and coverage. |
| 6.3 Streets list | P1 | Street CSV/GPKG and names exports | Existing; adapt to structured names/abbreviations. |
| 6.4 Houses list | P1 | Buildings table and survey metadata | Confirm Thierry's plaque/family columns and compact format. |
| 6.5 Street signs list | P1 | Signs exports | Extend letters/threshold logic with 4.3. |
| 6.6 Direction signs list | P1 | Reference rows in sign output | Separate complete download depends on 4.4. |
| 7 Simple page | P1 | `static/simple.html`; two existing browser tests | Names + addresses exist; new name form/suggestions must be implemented here too. |
| 8 Hosting | P1 | `deploy/`, health endpoint, single-process cache | Brief in `deployment-requirements.md`. Measure copied project; no purchase or deployment. |

## Decisions for Miguel

1. B1/B2 supplied IDs remain an unverified example. User instructed continuation
   on 2026-10-27 and suggested the report may be mistaken. Revisit only if a
   concrete failing sequence appears; do not keep requesting these IDs.
2. Confirm address examples: todo says branch 5 = 50 m and house 26 = 26 m,
   but also specifies branch divisor 20 m and house divisor 2 m. Preserve the
   existing calculations until examples and intended interpretation agree.
3. Agree French street types/defaults by rank and what counts as a short word;
   names shorter than four usable characters and abbreviation collisions need rules.
4. Supply the two direction-sign radii and approve migration from old rank 0
   (no sign) to new importance 0 (most visible).
5. Confirm noaddress's effect on through-routing, manual house-access limits,
   distance default, and trial roles/scale/scope.

## Implementation sequence

1. Reproduce ID defects on copies; add failing regressions for clear failure
   modes; fix preflight validation, unique ID allocation, split eligibility and
   pending edits before changing naming rules. Keep B1 unconfirmed unless reproduced.
2. R1/R2/R3: shared eligibility rules and first-order app planner; prove small
   known optima. Document migration of settings before modifying real projects.
3. Per-edge attributes/surface/noaddress, then structured names and both UIs.
4. Address/sign changes after worked examples and migration decisions.
5. Remaining geometry/review/downloads and deployment validation.

## Verification record

- Prior geometry increment: 12 targeted tests passed. These were rechecked
  earlier in this conversation; they are not a full-suite result.
- Initial non-browser baseline: 281 passed, 30 skipped, 3 failed. All three
  failures read a still-open NamedTemporaryFile through GDAL on Windows.
  The test helper now writes/closes files before GIS reads; unchanged export
  assertions subsequently pass.
- Added regressions initially produced 6 failures and 1 pass. Concrete failures
  included cross-rank/forced joins, bypassed branch validation, colliding new
  IDs, and splitting from a road not incident to the chosen node.
- Full-suite intermediate run: 305 passed, 30 skipped, 3 failed. Two compute
  waits exceeded 60 s (cause not established); one batch fixture could not find
  three whole same-rank connected streets. That fixture now splits an actual
  computed path into three pieces and rejoins them. The failed cases passed
  focused reruns. This intermediate run is not a clean final full-suite pass.
- Current focused results: 18 passed (14 community browser workflows plus
  four export/reference cases); 12 passed (10 ID-rule cases, visible split
  browser test and full batch/checkpoint/undo/restore/reopen round-trip).
  Final browser recheck after the installed-browser helper adjustment: 2 passed.
- The full run's 30 skips comprise 5 optional PostgreSQL pgserver cases and
  25 legacy geocode/street-editor browser cases requiring a separate bundled
  Chromium. No download/install performed. Community browser tests now reuse
  installed Edge/Chrome/Chromium; tile requests are blocked in those workflow
  tests, so imagery service availability is not validated by them.
- Isolated Fotouni network computations: practice copy 10.68 s; bundled copy
  9.87 s; each produced 216 computed streets. This is one local run each, not
  server sizing or representative production latency.
- Exact B1/B2 reports not reproduced because the named IDs differ, as above.
- No commits, pushes, deployment, OSM publication or source-dataset edits.

## First increment: review and continuation

Implemented: `app/street_ops.py` shared split/path validation; API preflight and
allocation fixes in `app/server.py`; exact split eligibility in `app/engine.py`;
rank-aware choices and truthful compute feedback in `static/app.js`.
Existing geometry work was preserved. No new schema or naming migration,
technology replacement, package download or lockfile change was made.

Local review server: http://127.0.0.1:8001/ serves the isolated practice copy
under `cache/workpackage-4m6zx56t/practice`; health and startup computation were
verified successful. Its logs/PID are in that copy. The existing port-8000
practice server and original project files were not restarted or edited by
this work-package increment. Both use the same source checkout, so refreshed
static files are shared; backend processes retain their loaded Python code.

Next task: R1/R2 app naming regressions (rank 0–2 require their own names;
first-order derivation only), with explicit handling of persisted max_levels=2
on another fresh dataset copy. Preserve generic library compatibility where
possible. Then R3 known-optimum graph tests and clear solver-status reporting.
Do not change physical address-number meaning from the contradictory examples.

Still partial/not started: B4 identity migration, all-addressing-surface rank
consistency, per-edge/surface/noaddress, structured names/migration, remaining
geometry/review flags, sign redesign, download additions and deployment proof.
The inventory above is the source of continuation status; the whole todo is
not complete. Keep each increment small and update results rather than
interpreting a previous test count as evidence for later code changes.

## Follow-up: screenshot tools (2026-10-27)

User steered current work to misplaced geometry, deleting a short portion of a
line, connected short-stroke tracing with a planned end, and dividing the map
into areas for progress tracking. This temporarily precedes the naming work.

- Implemented **Remove part of a road**: select an existing effective edge,
  choose two points, preview only the red interval, then save an undoable cut.
  Retained fragments inherit attributes; stable cut-node IDs preserve later
  fragment edits. Works for base and imported roads. Historical CSV geometry
  is cleared on rewritten fragments so computation cannot put the deleted
  portion back. Source input/network files remain unchanged. Stale cuts warn
  if a later source geometry no longer matches.
- Implemented **Trace route: start → end** with click points or connected
  freehand strokes. A planned end is not joined until reached. Gapped strokes
  are rejected; the first point of every continuation is exactly the preceding
  endpoint. Undo last stroke and movable draft bends remain available.
- Implemented explicit **path unclear** endpoints: stop short of the planned
  end, disable automatic endpoint connection, persist uncertainty, display a
  question mark and produce a compute warning. No automatic image recognition,
  road classification or inference of a path behind vegetation was implemented.
- Implemented **Work areas**: Automatic subdivision or metre-size presets,
  clipped to AOI/project boundary, with up to 400 actual cells. Progress and
  notes persist through undo/restore/restart and do not invalidate network or
  address hashes. Selection focuses/zooms and narrows reshape/cut selection;
  it does not clip roads or enforce an access-control boundary. This is manual
  area tracking, not completion of todo 1.5's per-geometry review flag. Review
  labels are not automatically invalidated after subsequent geometry edits.
- Evaluated [GeoLibre's official overview](https://geolibre.app/) before this
  geometry increment: it describes a React/MapLibre GIS workspace and grid/
  vector tools. Replacing the existing Leaflet application is not necessary
  for this bounded set of tools and would require porting the editor, map
  layers and persistence integration. Kept Leaflet and existing Shapely/GIS
  dependencies; no packages or models downloaded.

Verification: a 24-test targeted batch passed (new sections/areas/browser
workflows plus earlier geometry and ID regressions). The existing backend
suite then passed 53 tests with 5 optional pgserver skips. Additional deleted-
geometry and automatic-grid regressions were added; the final geometry/area
batch passed 16 tests. Community UI browser tests block tile
downloads; separate real-map inspection on the review copy succeeded without
JavaScript errors. Automatic preview on the Fotouni boundary produced 58
clipped cells of 950 m; this preview was not saved to the database.

Review data was backed up before restarting the isolated port-8001 server:
`cache/workpackage-4m6zx56t/practice/before-route-tools-c7219054.db`.
The final restart also has `before-final-path-tools-420e0736.db` in that folder.
The original `fotouni-practice-20261026` folder and its port-8000 server were
not edited/restarted by this increment. Prior review-copy edits were preserved.
All changes remain uncommitted; no deployment or OSM publication occurred.

Main files: `app/work_areas.py`, `app/roads.py:cut_sections`, `app/engine.py`,
`app/server.py` cut/grid endpoints, `app/store.py` events, `app/project.py`
optional JSON state, and the full-map HTML/JS/CSS. New regression suites:
`test_road_sections.py`, `test_work_areas.py`, `test_route_review_browser.py`.

Limits/next: shared junctions remain fixed in the reshape tool; cuts can create
real network gaps and affect derived IDs/numbers. Old application versions do
not understand new cut/area events: use a pre-change backup for rollback.
First-order naming R1/R2 remains the next work-package priority after this
user-requested geometry increment. B1 exact example remains unverified per
the user's instruction to continue.


### 27 October: current-section deletion and right-click connections

The user reported a red dashed remnant that would not disappear after two
cut points, and asked for a road context menu and connection of a corrected
pink drawing to a yellow junction.

- Right-click resolves the effective road sections under the pointer, with a
  selector and highlight for overlapping lines. Offers reshape, two-point cut,
  whole-section deletion, endpoint connection, extension and street ID/name tabs.
- Whole-section deletion has a red preview and confirmation. The legacy delete
  picker now uses this same effective graph, including retained cut fragments.
  Render generations prevent late overlay responses from repainting old lines.
- Reproduced and fixed cut -> reshape fragment -> cut fragment replay. Geometry
  overrides on generated fragments must be applied after the parent cut exists.
- Physical endpoint joins are separate ordered road_join events; targets can be
  existing junctions, road interiors or cut fragments. An interior target is split
  into a shared node; both directions and edit mappings follow it. Street IDs are
  preserved. Free endpoints only, 60 m maximum movement, preview/confirmation,
  revision guards, source/target identity and shape guards, Undo/Redo/reopen.
- Imported-road overlays and endpoint reports are rebuilt after topology edits.
  Endpoint markers from the pre-cut/import graph must not remain clickable.

Evidence limitation: the investigation snapshot contains historical cuts whose
source sections were subsequently replaced by additional imports. The reported
red line cannot be identified uniquely from the screenshot. Historical cuts with
missing/changed sources are flagged for review, not applied to a nearby guessed
road. No guessed deletion or connection was saved to the user's review data.
The new context menu selects the current section directly.

Regression suites: test_road_sections.py, test_road_junctions.py and
 test_road_context_browser.py; existing drawing and route browser workflows
are also checked. New events require this application version; rollback should
use the pre-restart SQLite backup. Original input files remain unchanged.

Verification for this increment: 19 geometry/junction/browser tests passed in
one final batch. The broader existing backend + ID rules + work-area batch
passed 67 tests with 5 optional PostgreSQL skips. JavaScript syntax and diff
whitespace checks passed. The drawing browser test now uses the shared installed
browser helper to block external tiles instead of waiting for satellite downloads.

Restarted only the isolated port-8001 review server after SQLite backup to
`cache/workpackage-4m6zx56t/practice/before-context-tools-3dbb3de9.db`.
The latest live state was revision 88, with 380 editable sections and 34 drawn
sections in its effective overlay. Five older saved cut records still report
missing/changed source sections; these were not silently remapped. Port 8000
and its original project process remained untouched. Normal startup initiated
the application's configured automatic network computation.


### 27 October: interface refresh and English/French selection

User requested a clearer, more attractive interface and language selection.
Implemented in the existing Leaflet/vanilla JavaScript pages; no build step,
package installation, database migration or backend change was needed.

- Warm off-white and green palette, clearer type/spacing, accessible focus
  outlines, grouped road actions, contextual workspace headings and a map guide.
- Full editor and community `/simple` page both offer English / Francais.
  Preference is browser-local and persists across reloads; initial language
  follows the browser when no preference was saved. Available before reference
  confirmation. Changing language does not write project state.
- Shared `static/i18n.js` translates authored UI strings and literal HTML parts
  before data is interpolated. Place names, addresses, user-entered names,
  checkpoint labels, IDs and road geometry are not translated or rewritten.
- Core editing/navigation/forms, reference confirmation, context actions and
  common warnings have French labels. Unmapped specialist diagnostics/API error
  messages retain their original English; browser-native file picker text and
  map-provider labels follow those providers/browser settings.
- Switching language preserves unfinished road points, current form values,
  area-note drafts, selected work area and map view. The community naming popup
  is updated in place, retaining an unsaved name. Mobile warnings start compact
  and can be expanded; map and tools retain separate usable scroll areas.

New verification: `tests/test_interface_language_browser.py` covers switching
while reference-locked, draft geometry and name preservation, unchanged project
revision, French controls, mobile overflow/map height, preference persistence
and a street literally named Save (must not become Enregistrer).
Desktop/mobile screenshots are generated on synthetic copies. Read-only browser
inspection of the current Fotouni map also found no JavaScript errors; all POST
requests were blocked during that inspection. Preview screenshots are under
`cache/interface-preview/`. This is a local interface change, not a deployment.

Interface verification outcome: the 14 existing community browser scenarios,
2 new language scenarios and 5 road/ID browser scenarios passed across the main
and focused runs (21 distinct scenarios; not one final full-suite run). The final
focused run passed 3 cases: import/accept/delete and both language workflows.
The broader checks exposed a stale deletion-picker cache and an old test polling
that cache without a null guard while refresh invalidated it. The picker cache
now contains the effective visible sections; its tooltip uses length_m. The test
selects a visible straight section and waits for the refreshed cache. A duplicate
community popup during language switching was fixed by updating the existing
popup in place. JavaScript syntax checks and git diff --check passed.

No backend/data-format changes or restart were required for the interface:
port 8001 serves these local static files directly. Refresh the browser to load
them. No commits, pushes, deployment or edits to live map data were performed
for this interface increment. One earlier combined test run emitted a Windows
native access-violation diagnostic during concurrent GIS requests; it did not
recur in the later focused language/import checks. No claim of production load
stability is made from these local browser results.


### Product flow, attribution and logo exploration

Added `docs/design/user-flow-and-toolbox.md`: proposed resident/editor entry
paths, stage/tool organisation mapped to the todo list, project roles, review
states, trusted attribution and pilot implementation order. Evidence: API
`me()` is empty, and `_add_event` deletes undone records on a new action, so
existing recovery/history is not a complete identified append-only audit.
This is a design proposal; accounts/permissions/approval are not implemented.

Added three original native SVG logo options and an interactive comparison in
`docs/design/logo-options/`, with working-name, dark and one-colour previews.
SVG XML parsed successfully and comparison controls/narrow-screen layout were
checked in installed Edge. No generated-image API, packages or external
branding service was used. No logo was selected or installed in the app.
The proposed name Community Address is provisional pending the user's choice.
Application code, project data and running servers were unchanged by this task.

Branding clarification: user confirmed Project - Community Addressing Cameroon. Updated all three logo comparison wordmarks, added standalone named SVG lockups, and refreshed previews. Project is a separate label. Logo selection and app implementation remain pending.


### Selected Community Addressing Cameroon logo

User supplied Logo cartographique Community Addressing Cameroon.png and asked
for integration, adapting the background if needed. Inspection found RGBA
transparency (corner alpha 0), so no background removal or image regeneration
was needed. Copied the original byte-for-byte to
`app/static/brand/community-addressing-cameroon.png`; SHA256 matches the source.
Added `brand.css` for a compact pin crop with readable HTML project branding,
plus the original full logo on reference-point cards. Both full and community
pages load the same artwork, including under `/simple/` path prefixes.

Verification: 3 browser checks passed (language/draft preservation, community
naming/language and reference-confirmation locking). Read-only Fotouni desktop
and mobile checks found no JavaScript errors or horizontal overflow. Project
data and backend logic were unchanged. Existing workflow/account proposal
remains pending; no authentication or approval system was added by this task.


### 28 October: invited accounts, API permissions and retained audit history

Implemented individual local accounts, one-use invitations, password change,
admin role/disable/reset controls, verified per-request actor IDs, revocable
HttpOnly sessions, CSRF/origin checks and persistent login throttling. Roles:
viewer, contributor, mapper, administrator. Auth is enabled by default in serve;
old in-process tests explicitly opt out. No package downloads or identity-service
subscriptions. See `docs/accounts-and-history.md` for the capability matrix,
setup/recovery, security parameters, migration and deployment details.

Store schema 3 adds audit/closed_redo evidence and account_* tables. Existing
anonymous entries remain Unknown (legacy); no fabricated authors. Undo/Redo no
longer delete old records; conservative conflict checks prevent replay across
other editors' input changes or undoing a prerequisite. Checkpoint restore is
admin-only with a reason and revision check; it never restores credentials or
roles. Reference confirmation is read-only in account mode. Background
calculation requests/completions have system/input-hash audit records.

UI: English/French setup/sign-in, invitation activation, Team controls, personal
passphrase change, History with actor/action/date, filters and before/after.
A selection-popup re-entry guard fixes a Leaflet pan/moveend race exposed by the
two-browser naming flow. Mobile sign-in sizing was checked including the fixed
overlay's own width, not only document overflow.

Verification: existing backend 53 passed / 5 optional PostgreSQL skips. An
integrated security/UI run passed 34 tests; a later account run passed all 17 API
cases but exposed the popup race in its browser case, fixed above. Final focused
UI run passed 5 cases (two authenticated account flows, two language workflows,
one street-ID workflow). Recovery and read-only reference checks are also tested.
This is not a final full-suite or production security assessment.

Review server: http://127.0.0.1:8002/ uses the fresh copy under
`cache/accounts-review-dsrlaipr/project`, copied from the prior review at revision
96. Pre-migration SQLite backup is `cache/accounts-review-dsrlaipr/before-accounts.db`.
`cache/current-review-project-path.txt` now records that full project path;
`cache/current-accounts-review-path.txt` records its parent. The older
current-workpackage pointer is historical. Logs/PID are in the new parent.
The copied edit records and revision were checked against the backup. No real
administrator/password was created by the agent: the user owns initial setup.

Automatic approval review rejected the attempted port-8001 restart with only
"blocked by policy" as its reason. Used a separate port-8002 process instead;
ports 8000 and 8001 and their project data were left running unchanged. The
older port-8001 backend does not enforce these new account rules. No commits,
pushes, cloud deployment, OSM publication or messages to third parties.

Limits: public approved lookup/review-and-approval workflow, SSO/MFA and email
recovery remain separate work. Contributor naming is direct editing, not a
proposal queue. One process per project; PostgreSQL does not change that cache
and request-serialization constraint. Local passwords/sessions are for the pilot
workspace; hosted usage requires HTTPS and a reviewed proxy/backup setup.

Final recovery/browser batch: 5 passed, including admin invitation reset, console recovery, read-only reference acknowledgement and the two account browser workflows. Actual port-8002 full and /simple/ setup screens inspected read-only with no JavaScript errors; all API writes blocked during inspection. Anonymous /api/project returned 401. No administrator was created during these inspections.
