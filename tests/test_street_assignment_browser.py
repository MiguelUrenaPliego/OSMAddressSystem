"""A manual split must become visible without a separate recompute click."""

from test_app_frontend import live_app, playwright_sync_api
import app_helpers as H


def test_split_recomputes_and_displays_the_new_id(live_app):
    url, root = live_app
    with playwright_sync_api.sync_playwright() as pw:
        browser = H.launch_browser(pw)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
        assert page.request.post(url + '/api/reference', data={'lat': lat, 'lon': lon}).ok
        page.goto(url, wait_until='domcontentloaded')
        page.locator('#ref-confirm').click()
        page.wait_for_function('S.network && S.network.junctions && !S.polling')
        page.evaluate('''() => {
            const f=S.network.junctions.features.find(f=>f.properties.spokes.some(s=>s.can_split));
            const ll=L.latLng(f.geometry.coordinates[1],f.geometry.coordinates[0]);
            map.setView(ll,17,{animate:false});
            L.popup({maxWidth:340}).setLatLng(ll).setContent(junctionHtml(f.properties)).openOn(map);
        }''')
        selector = page.locator('select.id-change').filter(has=page.locator('option[value=split]')).first
        with page.expect_response(lambda response: response.url.endswith('/api/streets/assign')) as response:
            selector.select_option('split')
        result = response.value.json()
        assert response.value.status == 200
        new_id = result['id']
        page.wait_for_function('id => !S.polling && S.network.streets.features.some(f=>f.properties.id===id)', arg=new_id)
        assert not page.locator('#stalebar').is_visible()
        # Persistence is separate from display and remains an undoable decision.
        from osmaddresssystem.app.store import Store
        store = Store(root)
        assert new_id in store.project().roads['no_cut']
        assert store.history()[0]['action'] == 'edges_assign'
        store.engine.dispose()
        assert not errors
        browser.close()
