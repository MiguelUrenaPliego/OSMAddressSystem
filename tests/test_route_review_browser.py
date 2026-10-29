"""Actual browser interactions for connected tracing, local cuts and work areas."""

from test_app_frontend import live_app, playwright_sync_api
import app_helpers as H
from osmaddresssystem.app.store import Store
import pytest


def test_connected_trace_uncertainty_areas_and_local_cut(live_app):
    url, root = live_app
    with playwright_sync_api.sync_playwright() as pw:
        browser = H.launch_browser(pw)
        page = browser.new_page(viewport={'width':1600, 'height':1000})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('dialog', lambda d: d.accept())
        lon, lat = H.lonlat(H.X0+120, H.Y0+120)
        assert page.request.post(url+'/api/reference', data={'lat':lat,'lon':lon}).ok
        page.goto(url, wait_until='domcontentloaded')
        page.locator('#ref-confirm').click()
        page.wait_for_function('S.network && !S.polling')
        page.locator('#tabs button[data-tab=roads]').click()
        page.locator('#workarea-box summary').click()
        page.locator('#areas-create').click()
        page.wait_for_selector('#area-select')
        area_id = page.locator('#area-select option').nth(1).get_attribute('value')
        page.locator('#area-select').select_option(area_id)
        page.locator('#area-status').select_option('in_progress')
        page.locator('#area-note').fill('Roads checked; trees obscure the end')
        page.locator('#area-save').click()
        page.wait_for_function('!S.project.stale')
        page.locator('#area-select').select_option('')

        page.locator('details[data-sec=draw] summary').click()
        page.locator('[data-act=draw-route]').click()
        page.locator('#draw-snap').uncheck()
        page.mouse.click(300,300)
        page.wait_for_function('S.draw.phase === "end"')
        page.mouse.click(700,400)
        page.wait_for_function('S.draw.phase === "trace"')
        assert page.evaluate('S.draw.pts.length') == 1  # planned end has no invented connection
        target = page.evaluate('JSON.parse(JSON.stringify(S.draw.target))')
        page.locator('#draw-tool').select_option('freehand')
        page.mouse.move(500,200); page.mouse.down(); page.mouse.move(550,220,steps=6); page.mouse.up()
        assert page.evaluate('S.draw.pts.length') == 1
        page.mouse.move(300,300); page.mouse.down()
        page.mouse.move(350,320,steps=8); page.mouse.move(400,350,steps=8); page.mouse.up()
        page.wait_for_function('S.draw.pts.length >= 3')
        count = page.evaluate('S.draw.pts.length')
        last = page.locator('.road-vertex').last.bounding_box()
        page.mouse.move(last['x']+6,last['y']+6); page.mouse.down()
        page.mouse.move(450,360,steps=8); page.mouse.move(500,350,steps=8); page.mouse.up()
        assert page.evaluate('S.draw.pts.length') > count
        page.locator('[data-act=draw-undo-stroke]').click()
        assert page.evaluate('S.draw.pts.length') == count
        last = page.locator('.road-vertex').last.bounding_box()
        page.mouse.move(last['x']+6,last['y']+6); page.mouse.down()
        page.mouse.move(450,360,steps=8); page.mouse.move(500,350,steps=8); page.mouse.up()
        page.locator('[data-act=draw-finish]').click()
        assert page.evaluate('!!S.draw')  # no automatic bridge to planned end
        page.locator('#draw-uncertain').check()
        with page.expect_response(lambda r:r.url.endswith('/api/roads/draw')) as saved:
            page.locator('[data-act=draw-finish]').click()
        assert saved.value.status == 200
        page.wait_for_function('!S.draw')
        store = Store(root)
        feature = store.project().roads['imports'][-1]['features'][0]
        assert feature['end_review'] == 'uncertain'
        assert feature['end'] == {'type':'none'}
        assert feature['coords'][-1] != [target[1],target[0]]
        assert store.project().work_areas['features'][0]['properties']['note'] == 'Roads checked; trees obscure the end'
        store.engine.dispose()
        page.locator('.uncertain-end').wait_for(state='visible')
        assert page.locator('.uncertain-end').count() == 1

        page.locator('details[data-sec=del] summary').click()
        page.locator('[data-act=cut-pick]').click()
        page.wait_for_function('!!S.reshapePick')
        clicks = page.evaluate('''() => {
            const f=S.reshapePick.edges.features.find(f=>f.geometry.coordinates.length===2 && !f.properties.ref.startsWith('-'));
            map.fitBounds(L.geoJSON(f).getBounds().pad(.5),{animate:false});
            const [a,b]=f.geometry.coordinates, r=map.getContainer().getBoundingClientRect();
            return [.5,.25,.75].map(t=>{const p=map.latLngToContainerPoint([a[1]+(b[1]-a[1])*t,a[0]+(b[0]-a[0])*t]);return {x:r.left+p.x,y:r.top+p.y};});
        }''')
        page.mouse.click(clicks[0]['x'],clicks[0]['y'])
        page.wait_for_function('!!S.cut')
        for p in clicks[1:]:
            page.mouse.click(p['x'],p['y'])
        page.wait_for_function('!!S.cut.preview')
        with page.expect_response(lambda r:r.url.endswith('/api/roads/cut')) as saved:
            page.locator('[data-act=cut-save]').click()
        assert saved.value.status == 200
        page.wait_for_function('!S.cut')
        store = Store(root)
        assert len(store.project().roads['cuts']) == 1
        assert store.history()[0]['action'] == 'road_cut'
        store.engine.dispose()
        page.locator('#btn-undo').click()
        page.wait_for_function('S.project.status.can_redo')
        store = Store(root)
        assert not store.project().roads['cuts']
        store.engine.dispose()
        assert not errors
        browser.close()


def test_connected_click_route_finishes_at_the_planned_end(live_app):
    url, root = live_app
    with playwright_sync_api.sync_playwright() as pw:
        browser = H.launch_browser(pw)
        page = browser.new_page(viewport={'width':1400,'height':900})
        lon, lat = H.lonlat(H.X0+120,H.Y0+120)
        page.request.post(url+'/api/reference',data={'lat':lat,'lon':lon})
        page.goto(url,wait_until='domcontentloaded')
        page.locator('#ref-confirm').click()
        page.wait_for_function('S.network && !S.polling')
        page.locator('#tabs button[data-tab=roads]').click()
        page.locator('details[data-sec=draw] summary').click()
        page.locator('[data-act=draw-route]').click()
        page.locator('#draw-snap').uncheck()
        page.mouse.click(300,300)
        page.wait_for_function('S.draw.phase === "end"')
        page.mouse.click(600,400)
        page.wait_for_function('S.draw.phase === "trace"')
        expected = page.evaluate('S.draw.target.map((v,i)=>S.draw.target[1-i])')
        assert page.evaluate('S.draw.pts.length') == 1
        page.mouse.click(450,350)
        page.mouse.click(600,400)
        page.wait_for_function('S.draw.reached')
        assert page.evaluate('S.draw.pts.length') == 3
        page.locator('[data-act=draw-finish]').click()
        page.wait_for_function('!S.draw')
        store = Store(root)
        feature = store.project().roads['imports'][-1]['features'][0]
        assert len(feature['coords']) == 3
        assert feature['coords'][-1] == pytest.approx(expected,abs=1e-7)
        assert not feature.get('end_review')
        store.engine.dispose()
        browser.close()
