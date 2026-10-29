"""Browser test of the app: reference first, compute, name a street, undo / redo, save, checkpoints, warnings."""

import socket
import sqlite3
import threading
import time

import pytest
import uvicorn

import app_helpers as H
from osmaddresssystem.app.server import create_app

playwright_sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture()
def live_app(tmp_path):
    """Run the app on a synthetic project in a background server thread.

    Yields:
        ``(url, project_root)``.
    """
    root = H.make_project(tmp_path / "proj")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(create_app(root, auth_enabled=False, auto_compute=False), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}", root
    server.should_exit = True
    t.join(timeout=10)


def wait_for(page, expr, seconds=90):
    """Wait until a JavaScript expression is truthy.

    Args:
        page: Playwright page.
        expr: Expression evaluated in the page.
        seconds: Timeout.
    """
    end = time.time() + seconds
    while time.time() < end:
        if page.evaluate(expr):
            return
        page.wait_for_timeout(300)
    raise AssertionError(f"timeout waiting for {expr}")


def test_whole_workflow_in_a_browser(live_app):
    """Choose the reference by clicking, compute, name a street, undo/redo, save (ids stored), warnings, restore."""
    url, root = live_app
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover - environment without a browser
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept("tester"))
        page.goto(url)
        page.wait_for_selector("#refbox:not([hidden])")
        assert "reference" in page.inner_text("#refbox").lower()                      # the first thing the map asks for
        assert page.evaluate("document.querySelector('#tabs button[data-tab=ids]').disabled")

        lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
        x, y = page.evaluate("""(ll) => { const p = map.latLngToContainerPoint(ll); const r = document.getElementById('map').getBoundingClientRect();
                                         return [r.left + p.x, r.top + p.y]; }""", [lat, lon])
        page.mouse.click(x, y)                                                       # the node is only proposed: nothing is saved yet
        page.wait_for_selector("#ref-yes")
        assert page.evaluate("S.project.reference") is None
        page.click("#ref-yes")
        wait_for_js = "!!S.network"
        wait_for(page, wait_for_js)
        assert page.evaluate("S.network.stats.uncovered") == 0 and page.evaluate("S.project.reference !== null")
        assert not page.evaluate("document.querySelector('#tabs button[data-tab=ids]').disabled")
        assert int(page.inner_text("#warn-count")) >= 1                              # streets still need a name

        page.click("#tabs button[data-tab=names]")
        page.wait_for_selector("#panel tr.click")
        page.click("#panel tr.click >> nth=0")
        page.wait_for_selector(".sel-popup #name-input")                                 # the name is edited in a popup on the map
        assert page.locator("#panel #name-input").count() == 0
        page.fill("#name-input", "Rue Test")
        page.click("button[data-act=set-name]")
        page.wait_for_function("Object.keys(S.project.names).length === 1")
        page.click("#btn-undo")                                                      # edits are saved at once; undo is yours only
        page.wait_for_function("Object.keys(S.project.names).length === 0")
        page.click("#btn-redo")
        page.wait_for_function("Object.keys(S.project.names).length === 1")

        page.click("#btn-save")                                                      # computes first, then stores names and ids
        page.wait_for_function("S.project.status.last_label === 'tester'", timeout=90000)
        con = sqlite3.connect(root / "project.db")
        assert con.execute("SELECT name FROM street_names").fetchall() == [("Rue Test",)]
        assert con.execute("SELECT COUNT(*) FROM street_ids").fetchone()[0] > 5

        assert page.is_visible("#warnings-box") and page.inner_text("#warn-list").strip()      # the list is always on the side
        page.click(".warn-item >> nth=0")                                            # the problem streets are drawn red
        assert page.evaluate("G.hl.getLayers().length") >= 1

        page.click("#btn-checkpoints")
        page.wait_for_selector("#modal-content button[data-restore]")
        page.click("#modal-content button[data-restore] >> nth=-1")
        page.wait_for_function("Object.keys(S.project.names).length === 0")
        assert not errors, errors
    finally:
        browser.close()
        pw.stop()


def test_edit_streets_tab_preview_accept_and_delete_in_a_browser(live_app, tmp_path):
    """Upload a roads file: new streets, thick connection nodes and the replaced road are drawn; accept; then delete an edge."""
    import json
    import urllib.request

    url, root = live_app

    def post(path, body):
        req = urllib.request.Request(url + "/api" + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req))

    edges = json.load(urllib.request.urlopen(url + "/api/edges"))["edges"]["features"]
    e0 = next(f for f in edges if len(f["geometry"]["coordinates"]) == 2)
    (lo1, la1), (lo2, la2) = e0["geometry"]["coordinates"]
    mid = [(lo1 + lo2) / 2, (la1 + la2) / 2 + 0.0001]                                 # a bump of ~11 m: about 9 % longer
    dup = [[lo1 + 0.00002, la1 + 0.00002], mid, [lo2 - 0.00002, la2 + 0.00002]]
    free = [dup[0], [lo1 - 0.003, la1 - 0.003]]                                       # a second street that hangs free
    ring = "".join(f"<Placemark><name>{n}</name><LineString><coordinates>{' '.join(f'{x},{y},0' for x, y in ln)}</coordinates></LineString></Placemark>"
                   for n, ln in (("Dup", dup), ("Libre", free)))
    kml = tmp_path / "new.kml"
    kml.write_text(f'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>{ring}</Document></kml>')
    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    post("/reference", {"lat": lat, "lon": lon})
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())
        page.goto(url)
        page.wait_for_selector("#ref-confirm")                                                          # every start asks for the reference point
        assert page.evaluate("document.body.classList.contains('locked')")
        page.click("#ref-confirm")
        page.wait_for_function("!document.body.classList.contains('locked')")
        page.wait_for_selector("#tabs button[data-tab=roads]:not([disabled])")
        page.click("#tabs button[data-tab=roads]")
        page.set_input_files("#roads-file", str(kml))
        wait_for(page, "S.stage && S.stage.pv")
        sm = page.evaluate("S.stage.pv.summary")
        assert sm["lines"] == 2 and sm["connected"] >= 1 and sm["replaced"] == 1 and sm["not_connected"] >= 1
        page.wait_for_selector("#panel button[data-act=stage-accept]")
        assert "Replaced roads" in page.inner_text("#panel") and "connection nodes" in page.inner_text("#panel")
        assert page.evaluate("G.roadNodes.getLayers().flatMap(g => g.getLayers()).length") >= 3 and page.evaluate("G.roadEdges.getLayers().flatMap(g => g.getLayers()).length") >= 3
        assert page.evaluate("Math.max(...G.roadNodes.getLayers().flatMap(g => g.getLayers()).map(l => l.options.radius))") >= 9      # thick connection nodes
        assert page.evaluate("S.project.roads.imports.length") == 0                                       # nothing saved yet
        page.click("#panel button[data-act=stage-connect-suggested]")                                    # edit the connections
        page.wait_for_function("S.stage.pv.summary.connected >= 2 || S.stage.pv.summary.not_connected >= 1")
        page.click("#panel button[data-act=stage-cancel]")                                               # cancel: nothing happens
        assert page.evaluate("S.stage") is None and page.evaluate("S.project.roads.imports.length") == 0
        page.set_input_files("#roads-file", str(kml))
        wait_for(page, "S.stage && S.stage.pv")
        page.click("#panel button[data-act=stage-accept]")                                               # asks for confirmation (accepted)
        page.wait_for_function("S.project.roads.imports.length === 1")
        page.wait_for_selector("#panel button[data-act=conn-back]")                                      # the connection view opens after accepting
        page.click("#panel button[data-act=conn-back]")
        page.click("details[data-sec=del] summary")
        page.click("#panel button[data-act=del-start]")
        page.wait_for_selector("#panel button[data-act=del-apply]")
        wait_for(page, "S.edgeData")
        page.evaluate("""() => { const f = S.edgeData.edges.features.find(f => f.properties.street_id != null && f.geometry.coordinates.length === 2); const c = f.geometry.coordinates[0];
            map.setView([c[1], c[0]], 18, { animate: false }); }""")
        page.wait_for_timeout(800)
        pt = page.evaluate("""() => { const f = S.edgeData.edges.features.find(f => f.properties.street_id != null && f.geometry.coordinates.length === 2); const c = f.geometry.coordinates;
            const a = c[0], b = c[c.length - 1]; const p = map.latLngToContainerPoint([(a[1] + b[1]) / 2, (a[0] + b[0]) / 2]);
            const r = document.getElementById('map').getBoundingClientRect(); return [r.left + p.x, r.top + p.y]; }""")
        page.mouse.click(*pt)
        page.wait_for_function("S.del.sel.size >= 1")
        page.click("#panel button[data-act=del-apply]")
        wait_for(page, "S.edgeData && S.edgeData.removed.features.length >= 1")
        assert not errors, errors
    finally:
        browser.close()
        pw.stop()


def test_reference_point_is_confirmed_on_every_start_and_locks_the_app(live_app):
    """A default reference is set at start; each page load locks the app until it is confirmed (or moved by a click on the map)."""
    import json

    url, root = live_app
    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)
        page.wait_for_selector("#refbox:not([hidden])")                                                  # no default, no reference: choose it
        assert page.evaluate("document.body.classList.contains('locked')")
        assert page.evaluate("getComputedStyle(document.querySelector('#side')).pointerEvents") == "none"
        x, y = page.evaluate("""(ll) => { const p = map.latLngToContainerPoint(ll); const r = document.getElementById('map').getBoundingClientRect(); return [r.left + p.x, r.top + p.y]; }""", [lat, lon])
        page.mouse.click(x, y)
        page.wait_for_selector("#ref-yes")                                                               # clicking proposes a node and asks first
        assert page.evaluate("document.body.classList.contains('locked')") and page.evaluate("S.project.reference") is None
        page.click("#ref-other")                                                                         # choose another: back to choosing
        page.mouse.click(x, y)
        page.click("#ref-yes")
        page.wait_for_function("!document.body.classList.contains('locked')")
        page.reload()                                                                                    # a new start of the app asks again, even for the same browser
        page.wait_for_selector("#ref-confirm")
        assert page.evaluate("document.body.classList.contains('locked')") and page.is_visible("#refbox")
        page.click("#ref-move")                                                                          # move it: the map is the only thing that works
        assert page.evaluate("document.body.classList.contains('locked')")
        page.mouse.click(x + 40, y + 40)
        page.wait_for_selector("#ref-yes")
        assert page.evaluate("document.body.classList.contains('locked')")
        page.click("#ref-yes")
        page.wait_for_function("!document.body.classList.contains('locked')")
        assert page.evaluate("S.project.reference.by") == page.evaluate("CLIENT")
    finally:
        browser.close()
        pw.stop()


def test_node_popup_offers_id_changes_only_where_they_make_sense(live_app):
    """The popup of a node lists every road with a selector: other ids of the node, and a split only when the id leaves the node twice."""
    import json
    import urllib.request

    url, root = live_app
    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    req = urllib.request.Request(url + "/api/reference", json.dumps({"lat": lat, "lon": lon}).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(req)
    urllib.request.urlopen(urllib.request.Request(url + "/api/compute", b"{}", {"Content-Type": "application/json"}))
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)
        page.wait_for_selector("#ref-confirm")
        page.click("#ref-confirm")
        wait_for(page, "S.network && S.network.junctions")
        html = page.evaluate("""() => { const out = {}; for (const f of S.network.junctions.features) { const p = f.properties; const ids = new Set(p.spokes.map(s => s.id));
            const twice = [...ids].some(i => p.spokes.filter(s => s.id === i).length > 1); const h = junctionHtml(p);
            const hasSplit = h.includes('value="split"'); out[twice ? 'twice' : 'once'] = (out[twice ? 'twice' : 'once'] || 0) + 1; if (hasSplit !== twice) out.wrong = (out.wrong || 0) + 1; }
            return out; }""")
        assert html.get("twice") and html.get("once") and not html.get("wrong")                       # split only for ids that leave the node twice
        red = page.evaluate("""() => { const cut = splitForNumbering(); let bad = 0, shown = 0;
            for (const f of S.network.junctions.features) { const h = junctionHtml(f.properties); for (const m of h.matchAll(/<option value="merge:(\\d+)"([^>]*)>/g)) { shown++; if (cut.has(+m[1]) !== m[2].includes('#d50000')) bad++; } }
            return [shown, bad]; }""")
        assert red[0] > 0 and red[1] == 0                                                            # ids cut for the numbering are marked red in the selector
        worst = page.evaluate("""() => { ensureColors(); const parse = (c) => { const m = c.match(/hsl\\((\\d+),(\\d+)%,(\\d+)%\\)/); return { h: +m[1], s: +m[2], l: +m[3] }; }; let worst = 9, same = 0;
            for (const f of S.network.junctions.features) { const ids = [...new Set(f.properties.spokes.map(s => s.id))];
                for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) { const d = colorDistance(parse(hue(ids[i])), parse(hue(ids[j]))); if (d === 0) same++; worst = Math.min(worst, d); } }
            return [worst, same]; }""")
        assert worst[1] == 0 and worst[0] > 0.2                                                       # streets meeting at a node never share or nearly share a colour
    finally:
        browser.close()
        pw.stop()


def test_all_building_points_are_shown_on_the_addresses_tab_before_computing(live_app):
    """The Addresses tab draws every surveyed building straight away; computing then colours them by address status."""
    url, root = live_app
    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    import json
    import urllib.request

    urllib.request.urlopen(urllib.request.Request(url + "/api/reference", json.dumps({"lat": lat, "lon": lon}).encode(), {"Content-Type": "application/json"}))
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)
        page.wait_for_selector("#ref-confirm")
        page.click("#ref-confirm")
        page.click("#tabs button[data-tab=addresses]")
        wait_for(page, "S.rawBuildings && G.houses.getLayers().length > 0")
        total = page.evaluate("S.rawBuildings.features.length")
        assert total > 30 and page.evaluate("G.houses.getLayers().flatMap(g => g.getLayers()).filter(l => l.options.radius).length") == total       # the buildings (the reference points are emoji markers)
        assert "buildings are shown" in page.inner_text("#panel")
    finally:
        browser.close()
        pw.stop()


def test_everything_recomputes_by_itself_and_there_are_no_compute_buttons(live_app):
    """Street ids follow every edit (also of another editor); the addresses are computed when the Addresses tab opens."""
    import json
    import urllib.request

    url, root = live_app

    def post(path, body):
        return json.load(urllib.request.urlopen(urllib.request.Request(url + "/api" + path, json.dumps(body).encode(), {"Content-Type": "application/json"})))

    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    post("/reference", {"lat": lat, "lon": lon})
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)
        page.wait_for_selector("#ref-confirm")
        page.click("#ref-confirm")
        wait_for(page, "S.network && S.network.streets")                                    # computed without any button
        assert page.locator("[data-act=compute], [data-act=compute-addresses]").count() == 0
        for tab in ("names", "addresses", "ids"):
            page.click(f"#tabs button[data-tab={tab}]")
            assert page.locator("[data-act=compute], [data-act=compute-addresses]").count() == 0
        page.click("#tabs button[data-tab=addresses]")
        wait_for(page, "S.addresses && S.addresses.houses")                                 # computed when the tab was opened
        sid = page.evaluate("S.network.plan[0].street_id_original")
        post("/names", {"street_id": sid, "name": "Rue Auto"})                              # another editor names a street
        page.wait_for_selector("#recompute-now", state="visible", timeout=60000)                        # edits wait: the bar offers the recomputation
        page.click("#recompute-now")
        wait_for(page, "S.network.streets.features.some(f => f.properties.name === 'Rue Auto')", seconds=120)
        wait_for(page, "S.addresses && S.addresses.houses && !S.project.addresses_stale", seconds=120)       # the addresses follow on the open tab
    finally:
        browser.close()
        pw.stop()


def test_draw_a_road_and_prolong_its_dead_end_onto_another_road(live_app):
    """Draw a road from a node (snapped), then prolong its dead end onto a road: the new part joins the street and everything is connected."""
    import json
    import urllib.request

    url, root = live_app
    lon0, lat0 = H.lonlat(H.X0 + 120, H.Y0 + 120)
    urllib.request.urlopen(urllib.request.Request(url + "/api/reference", json.dumps({"lat": lat0, "lon": lon0}).encode(), {"Content-Type": "application/json"}))
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")

    def px(page, x, y):
        lon, lat = H.lonlat(x, y)
        return page.evaluate("""(ll) => { const p = map.latLngToContainerPoint(ll); const r = document.getElementById('map').getBoundingClientRect(); return [r.left + p.x, r.top + p.y]; }""", [lat, lon])

    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)
        page.wait_for_selector("#ref-confirm")
        page.click("#ref-confirm")
        wait_for(page, "S.network")
        page.click("#tabs button[data-tab=roads]")
        page.evaluate(f"map.setView([{lat0}, {lon0}], 18, {{ animate: false }}); 0")
        page.wait_for_timeout(500)
        page.click("details[data-sec=draw] summary")
        page.click("#panel button[data-act=draw-new]")
        assert page.evaluate("S.draw.mode") == "new"
        page.mouse.click(*px(page, H.X0 + 2, H.Y0 + 1))                      # first point: snapped onto the node
        wait_for(page, "S.draw.pts.length === 1 && S.draw.start && S.draw.start.type === 'node'")
        page.mouse.click(*px(page, H.X0 + 3, H.Y0 - 60))
        page.wait_for_function("S.draw.pts.length === 2")
        page.click("#panel button[data-act=draw-finish]")
        page.wait_for_function("S.project.roads.imports.length === 1")
        page.wait_for_selector("#recompute-now", state="visible", timeout=60000)                        # edits wait: the bar offers the recomputation
        page.click("#recompute-now")
        wait_for(page, "S.project.network_ready && !S.project.stale")
        page.click("details[data-sec=draw] summary") if not page.evaluate("document.querySelector('details[data-sec=draw]').open") else None
        page.click("#panel button[data-act=draw-prolong]")
        wait_for(page, "S.draw.ends && S.draw.ends.some(e => Math.abs(e[2] - " + str(H.lonlat(H.X0 + 3, H.Y0 - 60)[1]) + ") < 1e-4)")
        page.mouse.click(*px(page, H.X0 + 3, H.Y0 - 60))                      # the dead end (orange dot)
        wait_for(page, "S.draw.start && S.draw.join !== null && S.draw.pts.length === 1")
        page.mouse.click(*px(page, H.X0 + 60, H.Y0 + 0.5))                    # onto the road, 60 m from the node
        page.wait_for_function("S.draw.pts.length === 2")
        page.keyboard.press("Enter")                                          # finishes the road
        page.wait_for_function("S.project.roads.imports.length === 2")
        page.wait_for_selector("#recompute-now", state="visible", timeout=60000)                        # edits wait: the bar offers the recomputation
        page.click("#recompute-now")
        wait_for(page, "S.project.network_ready && !S.project.stale && S.network", seconds=120)
        assert not page.evaluate("S.network.warnings.some(w => w.code === 'roads_isolated' || w.code === 'roads_dangling')")
    finally:
        browser.close()
        pw.stop()


def test_the_map_view_is_not_moved_when_a_recomputation_ends(live_app):
    """Moving the map while the street ids are recomputed (or editing again) keeps the view where the user put it."""
    import json
    import urllib.request

    url, root = live_app
    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    urllib.request.urlopen(urllib.request.Request(url + "/api/reference", json.dumps({"lat": lat, "lon": lon}).encode(), {"Content-Type": "application/json"}))
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)
        page.wait_for_selector("#ref-confirm")
        page.click("#ref-confirm")
        wait_for(page, "S.network && !S.project.stale")
        page.evaluate("map.setView([5.3, 10.2], 12, { animate: false }); 0")                 # the user looks somewhere else
        page.evaluate("fetch('api/names', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({street_id: S.network.plan[0].street_id_original, name: 'Rue Vue'})}).then(() => 0)")
        page.wait_for_selector("#recompute-now", state="visible", timeout=60000)                        # edits wait: the bar offers the recomputation
        page.click("#recompute-now")
        wait_for(page, "S.network.streets.features.some(f => f.properties.name === 'Rue Vue')", seconds=120)
        wait_for(page, "!S.project.stale && !S.polling")
        c = page.evaluate("[map.getCenter().lat, map.getCenter().lng, map.getZoom()]")
        assert abs(c[0] - 5.3) < 1e-6 and abs(c[1] - 10.2) < 1e-6 and c[2] == 12
    finally:
        browser.close()
        pw.stop()


def test_background_map_opacity_slider_in_the_layer_control(live_app):
    """The layer control has an opacity slider for the background map."""
    url, root = live_app
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)
        page.hover(".leaflet-control-layers")                                                  # the control opens on hover
        page.wait_for_selector(".bg-opacity input", state="visible")
        page.evaluate("(() => { const i = document.querySelector('.bg-opacity input'); i.value = 40; i.dispatchEvent(new Event('input', { bubbles: true })); })()")
        assert page.evaluate("hybrid.options.opacity") == 0.4 and page.evaluate("osmTiles.options.opacity") == 0.4
        assert "40%" in page.inner_text(".bg-opacity")
    finally:
        browser.close()
        pw.stop()


def test_colour_by_rank_drivable_paved_and_attribute_editing_in_the_popup(live_app):
    """The colour dropdown has rank / drivable / paved; the street popup (ids and names tabs) edits rank, drivable and paved."""
    import json
    import urllib.request

    url, root = live_app
    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    urllib.request.urlopen(urllib.request.Request(url + "/api/reference", json.dumps({"lat": lat, "lon": lon}).encode(), {"Content-Type": "application/json"}))
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)
        page.wait_for_selector("#ref-confirm")
        page.click("#ref-confirm")
        wait_for(page, "S.network && !S.project.stale")
        assert page.locator("#color-mode option").count() == 5
        for mode in ("rank", "drivable", "paved"):
            page.select_option("#color-mode", mode)
            assert page.evaluate("S.idColor") == mode and page.locator("#panel .legend").count() == 1
        for tab in ("ids", "names"):
            page.click(f"#tabs button[data-tab={tab}]")
            wait_for(page, "S.network")
            page.evaluate("selectStreet(S.network.streets.features[0].properties.id, true)")
            page.wait_for_selector(".sel-popup .attr-edit")
            assert page.locator(".sel-popup select[data-attr=rank]").count() == 1 and page.locator(".sel-popup select[data-attr=paved]").count() == 1
        orig = page.evaluate("S.sel.id && streetById(S.sel.id).properties.original")
        assert page.evaluate("streetById(S.sel.id).properties.paved") is None
        page.locator(".sel-popup select[data-attr=paved]").select_option("true")
        wait_for(page, "streetById(S.sel.id).properties.paved === true", seconds=120)                 # the street (or its part) just edited
    finally:
        browser.close()
        pw.stop()


def test_reference_point_sign_ranks_are_editable_on_the_addresses_tab(live_app):
    """The Addresses tab lists the reference points with a rank for the signs; changing one is stored."""
    import json
    import urllib.request

    url, root = live_app
    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    urllib.request.urlopen(urllib.request.Request(url + "/api/reference", json.dumps({"lat": lat, "lon": lon}).encode(), {"Content-Type": "application/json"}))
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)
        page.wait_for_selector("#ref-confirm")
        page.click("#ref-confirm")
        wait_for(page, "S.network && !S.project.stale")
        page.click("#tabs button[data-tab=addresses]")
        wait_for(page, "S.addresses && S.pointRanks && S.pointRanks.length > 0")
        page.click("details.sec summary")
        page.select_option("select.pt-rank >> nth=0", "1")
        wait_for(page, "S.project.roads === undefined || true")
        wait_for(page, "S.pointRanks && S.pointRanks[0].rank === 1", seconds=60)
        assert json.load(urllib.request.urlopen(url + "/api/points/ranks"))[0]["rank"] == 1
    finally:
        browser.close()
        pw.stop()


def test_a_click_on_an_empty_place_clears_the_selection_and_the_special_styles(live_app):
    """The azimuth arrow / road point of a building, the highlight and the popup go away when the map is clicked where nothing is."""
    import json
    import urllib.request

    url, root = live_app
    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    urllib.request.urlopen(urllib.request.Request(url + "/api/reference", json.dumps({"lat": lat, "lon": lon}).encode(), {"Content-Type": "application/json"}))
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)
        page.wait_for_selector("#ref-confirm")
        page.click("#ref-confirm")
        wait_for(page, "S.network && !S.project.stale")
        page.click("#tabs button[data-tab=addresses]")
        wait_for(page, "S.addresses && S.addresses.houses")
        page.evaluate("(() => { const f = S.addresses.houses.features.find(f => f.properties.road_lat); S.sel = { type: 'house', props: f.properties, ll: L.latLng(f.geometry.coordinates[1], f.geometry.coordinates[0]) }; drawAccess(f.properties); refreshSel(); })()")
        page.wait_for_selector(".sel-popup")
        assert page.evaluate("G.access.getLayers().length") > 0
        page.evaluate("map.setView([5.0, 9.0], 12, { animate: false }); 0")                       # somewhere with nothing on it
        page.mouse.click(300, 400)
        wait_for(page, "!S.sel && G.access.getLayers().length === 0 && !document.querySelector('.sel-popup')")
    finally:
        browser.close()
        pw.stop()


def test_simple_page_names_a_street_and_shows_addresses_in_the_same_database(live_app):
    """/simple/ lists the streets that need a name, saves a name into the same project and shows the addresses."""
    import json
    import urllib.request

    url, root = live_app
    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    urllib.request.urlopen(urllib.request.Request(url + "/api/reference", data=json.dumps({"lat": lat, "lon": lon}).encode(), headers={"Content-Type": "application/json"}))
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url + "/simple/")
        wait_for(page, "document.querySelector('#side .big') !== null", 120)
        need = page.evaluate("S.network.streets.features.map(f => f.properties).filter(NEEDS).map(p => p.original)")
        assert need, "the synthetic project has streets without a name"
        page.evaluate("(id) => openStreet(id, null)", need[0])
        page.fill("#nm", "Simple Road")
        page.press("#nm", "Enter")
        wait_for(page, "S.network.streets.features.some(f => f.properties.name === 'Simple Road')", 120)
        names = json.loads(urllib.request.urlopen(url + "/api/project").read())["names"]
        assert "Simple Road" in names.values()                                        # saved in the project database, the full app sees it
        page.click(".tab[data-tab=addresses]")
        wait_for(page, "S.addresses && document.querySelector('#side .big') && document.querySelector('#side #q')", 180)
        assert not errors, errors
    finally:
        browser.close()
        pw.stop()


def test_simple_page_redirects_to_the_full_app_when_the_network_has_connection_problems(live_app):
    """A road that reaches nothing is a serious problem: /simple/ sends the visitor to the full app."""
    import json
    import urllib.request

    url, root = live_app
    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    post = lambda path, body: urllib.request.urlopen(urllib.request.Request(url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}))
    post("/api/reference", {"lat": lat, "lon": lon})
    a, b = H.lonlat(H.X0 + 3000, H.Y0 + 3000), H.lonlat(H.X0 + 3100, H.Y0 + 3000)
    post("/api/roads/draw", {"pieces": [{"coords": [list(a), list(b)], "start": None, "end": None}], "name": "Island"})
    try:
        pw = playwright_sync_api.sync_playwright().start()
        browser = H.launch_browser(pw)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no Chromium available: {exc}")
    try:
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        page.goto(url + "/simple/")
        wait_for(page, "!location.pathname.includes('simple')", 120)
        assert page.url.rstrip("/") == url
    finally:
        browser.close()
        pw.stop()
