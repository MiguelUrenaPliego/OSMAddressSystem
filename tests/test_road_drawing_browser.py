"""Exercise tracing with actual mouse events and persist the resulting curve."""

from pathlib import Path
import shutil

import pytest

import app_helpers as H
from test_app_frontend import live_app, playwright_sync_api


def test_freehand_reshape_cancel_and_save(live_app):
    url, root = live_app
    executable = next((str(p) for p in [
        Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),
        Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
    ] if p.exists()), None) or shutil.which('chromium')
    if not executable:
        pytest.skip('No installed browser')
    with playwright_sync_api.sync_playwright() as pw:
        browser = H.launch_browser(pw)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
        assert page.request.post(url + '/api/reference', data={"lat": lat, "lon": lon}).ok
        page.goto(url, wait_until='domcontentloaded')
        page.locator('#ref-confirm').click()
        page.locator('#tabs button[data-tab=roads]').click()
        page.locator('details[data-sec=draw] summary').click()
        page.locator('[data-act=draw-new]').click()
        page.locator('#draw-snap').uncheck()
        page.locator('#draw-tool').select_option('freehand')
        assert page.evaluate('!map.dragging.enabled()')
        page.mouse.move(300, 250)
        page.mouse.down()
        page.mouse.move(350, 280, steps=10)
        page.mouse.move(400, 350, steps=10)
        page.mouse.move(500, 300, steps=10)
        page.mouse.up()
        page.wait_for_function('S.draw.pts.length >= 3')
        before = page.evaluate('JSON.parse(JSON.stringify(S.draw.pts))')
        page.locator('#draw-tool').select_option('points')
        assert page.evaluate('map.dragging.enabled()')
        point = page.locator('.road-vertex').nth(1)
        box = point.bounding_box()
        page.mouse.move(box['x'] + 6, box['y'] + 6)
        page.mouse.down()
        page.mouse.move(box['x'] + 26, box['y'] + 36, steps=8)
        page.mouse.up()
        page.wait_for_function('S.draw.snaps.every(t => t === null)')
        after = page.evaluate('JSON.parse(JSON.stringify(S.draw.pts))')
        assert before != after
        midpoint = page.evaluate('''() => {
            const a = S.draw.pts[0], b = S.draw.pts[1];
            const p = map.latLngToContainerPoint([(a[0]+b[0])/2, (a[1]+b[1])/2]);
            const r = map.getContainer().getBoundingClientRect();
            return {x: r.left+p.x, y: r.top+p.y};
        }''')
        count = page.locator('.road-vertex').count()
        page.mouse.click(midpoint['x'], midpoint['y'])
        assert page.locator('.road-vertex').count() == count + 1
        count = page.locator('.road-vertex').count()
        page.locator('.road-vertex').nth(1).click(button='right')
        assert page.locator('.road-vertex').count() == count - 1
        revision = page.request.get(url + '/api/revision').json()['status']['revision']
        page.locator('[data-act=draw-cancel]').click()
        assert page.request.get(url + '/api/revision').json()['status']['revision'] == revision

        page.locator('[data-act=draw-new]').click()
        page.locator('#draw-snap').uncheck()
        page.locator('#draw-tool').select_option('freehand')
        page.mouse.move(300, 250)
        page.mouse.down()
        page.mouse.move(400, 350, steps=10)
        page.mouse.move(500, 300, steps=10)
        page.mouse.up()
        page.wait_for_function('S.draw.pts.length >= 3')
        curve = page.evaluate('S.draw.pts.map(p => [p[1], p[0]])')
        page.locator('[data-act=draw-finish]').click()
        page.wait_for_function('!S.draw')
        # Read through the application's store to verify persisted coordinates,
        # not just the immediate optimistic drawing on the map.
        from osmaddresssystem.app.store import Store
        store = Store(root)
        feature = store.project().roads['imports'][-1]['features'][0]
        assert len(feature['coords']) == len(curve)
        for actual, expected in zip(feature['coords'], curve):
            assert actual == pytest.approx(expected, abs=1e-7)
        assert feature['start'] == {'type': 'none'}
        assert feature['end'] == {'type': 'none'}
        assert page.evaluate('map.dragging.enabled()')
        # Integration also respects explicit unsnapped endpoints, rather than
        # silently moving them through its normal automatic connection rule.
        from osmaddresssystem.app.engine import effective_base, load_base
        project = store.project()
        _, integrated = effective_base(load_base(project), project, write_cache=False)
        assert integrated is not None
        ends = [n for n in integrated.report['nodes'] if n['degree'] == 1]
        assert len(ends) == 2
        assert all(n['state'] == 'free' for n in ends)
        assert integrated.report['auto_connected'] == 0
        store.engine.dispose()

        # A prolongation has a fixed first node whose snap target contains only
        # a node ID. Saving must retain its actual coordinates.
        page.locator('[data-act=draw-prolong]').click()
        page.wait_for_function('S.draw && S.draw.ends && S.draw.ends.length')
        start = page.evaluate('''() => {
            const [node, lon, lat, sid] = S.draw.ends[0];
            map.setView([lat, lon], 18);
            return {node, lat, lon};
        }''')
        page.evaluate('''() => {
            const [node, lon, lat] = S.draw.ends[0];
            const marker = G.roadNodes.getLayers().find(m => m.getLatLng().lat === lat && m.getLatLng().lng === lon);
            marker.fire('click', {originalEvent: {stopPropagation() {}}});
        }''')
        assert not page.locator('.road-vertex').first.evaluate('(e) => e.classList.contains("leaflet-marker-draggable")')
        page.mouse.click(600, 400)
        page.wait_for_function('S.draw.pts.length === 2 && S.draw.pending.size === 0')
        page.locator('[data-act=draw-finish]').click()
        page.wait_for_function('!S.draw')
        store = Store(root)
        feature = store.project().roads['imports'][-1]['features'][0]
        assert feature['coords'][0] == pytest.approx([start['lon'], start['lat']], abs=1e-7)
        assert feature['start']['node'] == start['node']
        store.engine.dispose()

        page.locator('[data-act=reshape-pick]').click()
        page.wait_for_function('S.reshapePick && S.reshapePick.edges.features.length')
        click = page.evaluate('''() => {
            const f = S.reshapePick.edges.features[0];
            const c = f.geometry.coordinates;
            map.fitBounds(L.geoJSON(f).getBounds().pad(0.5), {animate:false});
            const a=c[0], b=c[c.length-1];
            const p=map.latLngToContainerPoint([(a[1]+b[1])/2, (a[0]+b[0])/2]);
            const r=map.getContainer().getBoundingClientRect();
            return {x:r.left+p.x,y:r.top+p.y};
        }''')
        page.mouse.click(click['x'], click['y'])
        page.wait_for_function('S.draw && S.draw.mode === "reshape"')
        original = page.evaluate('JSON.parse(JSON.stringify(S.draw.original))')
        ref = page.evaluate('S.draw.ref')
        assert page.locator('.road-vertex').count() == 2
        page.mouse.click(click['x'], click['y'])  # insert a midpoint
        page.wait_for_function('S.draw.pts.length === 3')
        box = page.locator('.road-vertex').nth(1).bounding_box()
        page.mouse.move(box['x']+6, box['y']+6)
        page.mouse.down()
        page.mouse.move(box['x']+26, box['y']+36, steps=8)
        page.mouse.up()
        page.locator('[data-act=draw-finish]').click()
        page.wait_for_function('!S.draw')
        store = Store(root)
        saved = store.project().roads['geometry'][ref]
        assert len(saved) == 3
        assert saved[0] == pytest.approx(list(reversed(original[0])), abs=1e-8)
        assert saved[-1] == pytest.approx(list(reversed(original[-1])), abs=1e-8)
        assert store.history()[0]['action'] == 'road_geometry'
        store.engine.dispose()
        page.locator('[data-act=reshape-pick]').click()
        page.wait_for_function('S.reshapePick && S.reshapePick.edges.features.length')
        ends = page.evaluate('''ref => {
            const layer=G.roadEdges.getLayers()[0].getLayers().find(l=>l.feature.properties.ref===ref);
            layer.fire('click',{originalEvent:{stopPropagation(){}}});
            const r=map.getContainer().getBoundingClientRect();
            return [S.draw.pts[0], S.draw.pts[S.draw.pts.length-1]].map(ll=>{
                const p=map.latLngToContainerPoint(ll); return {x:r.left+p.x,y:r.top+p.y};
            });
        }''', ref)
        page.locator('#draw-tool').select_option('freehand')
        # Trace backwards to exercise orientation toward the retained endpoints.
        a, b = ends
        page.mouse.move(b['x']-10, b['y']-5)
        page.mouse.down()
        page.mouse.move((a['x']+b['x'])/2+15, (a['y']+b['y'])/2+20, steps=10)
        page.mouse.move(a['x']+10, a['y']+5, steps=10)
        page.mouse.up()
        page.locator('[data-act=draw-finish]').click()
        page.wait_for_function('!S.draw')
        store = Store(root)
        traced = store.project().roads['geometry'][ref]
        assert len(traced) >= 3
        assert traced[0] == pytest.approx(saved[0], abs=1e-8)
        assert traced[-1] == pytest.approx(saved[-1], abs=1e-8)
        store.engine.dispose()
        assert not errors
        browser.close()
