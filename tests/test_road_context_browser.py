"""Right-click tools select effective sections and persist physical joins."""
from test_app_frontend import live_app, playwright_sync_api
import app_helpers as H
from osmaddresssystem.app.store import Store


def test_context_delete_and_join(live_app):
    url, root=live_app
    with playwright_sync_api.sync_playwright() as pw:
        browser=H.launch_browser(pw)
        page=browser.new_page(viewport={'width':1600,'height':1000})
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('dialog',lambda d:d.accept())
        lon,lat=H.lonlat(H.X0+120,H.Y0+120)
        page.request.post(url+'/api/reference',data={'lon':lon,'lat':lat})
        page.goto(url,wait_until='domcontentloaded')
        page.wait_for_function('S.ready')
        page.locator('#ref-confirm').click()
        page.locator('#tabs button[data-tab=roads]').click()
        page.wait_for_function('S.tab === "roads" && !S.locked')
        point=page.evaluate('''async()=>{
          const data=await api('/roads/geometry');
          window.testRoad=data.edges.features.find(f=>f.geometry.coordinates.length===2);
          map.fitBounds(L.geoJSON(testRoad).getBounds().pad(.6),{animate:false});
          const c=testRoad.geometry.coordinates, a=c[0],b=c[c.length-1];
          const p=map.latLngToContainerPoint([(a[1]+b[1])/2,(a[0]+b[0])/2]),r=map.getContainer().getBoundingClientRect();
          return {x:r.left+p.x,y:r.top+p.y};
        }''')
        page.mouse.click(point['x'],point['y'],button='right')
        page.locator('[data-road-action=whole]').click()
        page.wait_for_function('!!S.cut?.preview')
        ref=page.evaluate('S.cut.feature.properties.ref')
        with page.expect_response(lambda r:r.url.endswith('/api/roads/cut')) as response:
            page.locator('[data-act=cut-save]').click()
        assert response.value.ok
        page.wait_for_function('!S.cut')
        assert ref not in {f['properties']['ref'] for f in page.request.get(url+'/api/roads/geometry').json()['edges']['features']}
        page.locator('#btn-undo').click()
        page.wait_for_function('S.project.status.can_redo')
        # Make a small gap, then reconnect its current endpoints with actual clicks.
        page.evaluate('''async()=>{
          const c=testRoad.geometry.coordinates,a=c[0],b=c[c.length-1];
          const points=[.45,.55].map(t=>[a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])]);
          await api('/roads/cut',{ref:testRoad.properties.ref,points,revision:S.rev});
          await refreshProject(); await loadRoads(); drawTab();
        }''')
        point=page.evaluate('''async()=>{
          const data=await api('/roads/geometry'); window.fragments=data.edges.features.filter(f=>Math.min(f.properties.u,f.properties.v)<0);
          const c=fragments[0].geometry.coordinates,a=c[0],b=c[c.length-1],p=map.latLngToContainerPoint([(a[1]+b[1])/2,(a[0]+b[0])/2]),r=map.getContainer().getBoundingClientRect();
          return {x:r.left+p.x,y:r.top+p.y};
        }''')
        page.mouse.click(point['x'],point['y'],button='right')
        page.locator('[data-road-action=join]').click()
        page.wait_for_function('!!S.join')
        free=page.evaluate('S.join.feature.properties.degree_u===1 ? "A" : "B"')
        page.locator('.leaflet-marker-icon strong',has_text=free).click()
        page.wait_for_function('!!S.join.source')
        point=page.evaluate('''()=>{
          const f=fragments.find(f=>f.properties.ref!==S.join.feature.properties.ref),p=f.properties,c=f.geometry.coordinates;
          const v=p.u<0?c[0]:c[c.length-1], q=map.latLngToContainerPoint([v[1],v[0]]),r=map.getContainer().getBoundingClientRect();
          return {x:r.left+q.x,y:r.top+q.y};
        }''')
        page.mouse.click(point['x'],point['y'])
        page.get_by_role('button',name='Connect here',exact=True).click()
        page.wait_for_function('!S.join')
        store=Store(root)
        assert len(store.project().roads['joins'])==1
        store.engine.dispose()
        assert errors==[]
        browser.close()
