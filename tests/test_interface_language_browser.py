"""Language selection preserves editing state and leaves project data alone."""
from test_app_frontend import live_app, playwright_sync_api
import app_helpers as H


def test_language_switch_preserves_drawing_and_reference(live_app):
    url,root=live_app
    with playwright_sync_api.sync_playwright() as pw:
        browser=H.launch_browser(pw)
        page=browser.new_page(viewport={'width':1500,'height':1000})
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.add_init_script("localStorage.setItem('address-language','en')")
        lon,lat=H.lonlat(H.X0+120,H.Y0+120)
        page.request.post(url+'/api/reference',data={'lon':lon,'lat':lat})
        page.goto(url,wait_until='domcontentloaded')
        page.wait_for_function('S.ready')
        page.wait_for_function('document.querySelector(".brand-symbol img").naturalWidth === 1278')
        assert page.locator('.app-name').inner_text()=='Community Addressing Cameroon'
        assert page.locator('#refbox .reference-logo').get_attribute('src').endswith('community-addressing-cameroon.png')
        revision=page.request.get(url+'/api/project').json()['status']['revision']
        # Language remains available while reference confirmation locks editing.
        page.locator('#language-select').select_option('fr')
        assert page.locator('html').get_attribute('lang')=='fr'
        assert page.locator('#ref-confirm').inner_text()=='Oui, confirmer'
        assert page.request.get(url+'/api/project').json()['status']['revision']==revision
        page.locator('#ref-confirm').click()
        page.wait_for_function('S.network && !S.polling')
        page.locator('#tabs button[data-tab=roads]').click()
        page.locator('details[data-sec=draw] summary').click()
        page.locator('[data-act=draw-new]').click()
        page.locator('#draw-snap').uncheck()
        page.mouse.click(300,300);page.mouse.click(450,340)
        page.wait_for_function('S.draw.pts.length===2')
        page.locator('#draw-name').fill('Save')
        before=page.evaluate('JSON.stringify(S.draw.pts)')
        assert page.locator('[data-act=draw-finish]').inner_text()=='Terminer'
        revision=page.request.get(url+'/api/project').json()['status']['revision']
        page.locator('#language-select').select_option('en')
        assert page.locator('[data-act=draw-finish]').inner_text()=='Finish'
        assert page.locator('#draw-name').input_value()=='Save'
        assert page.evaluate('JSON.stringify(S.draw.pts)')==before
        assert page.request.get(url+'/api/project').json()['status']['revision']==revision
        page.locator('[data-act=draw-cancel]').click()
        page.locator('#language-select').select_option('fr')
        assert page.locator('[data-sec=draw] summary').inner_text()=='Dessiner, prolonger ou ajuster'
        point=page.evaluate('''async()=>{
          const f=(await api('/roads/geometry')).edges.features.find(f=>f.geometry.coordinates.length===2);
          map.fitBounds(L.geoJSON(f).getBounds().pad(.8),{animate:false});
          const [a,b]=f.geometry.coordinates,p=map.latLngToContainerPoint([(a[1]+b[1])/2,(a[0]+b[0])/2]);
          const r=map.getContainer().getBoundingClientRect();return {x:r.left+p.x,y:r.top+p.y};
        }''')
        page.mouse.click(point['x'],point['y'],button='right')
        assert page.locator('[data-road-action=whole]').inner_text()=='Supprimer ce tronçon entier'
        page.evaluate('map.closePopup()')
        page.screenshot(path=str(root.parent/'interface-fr.png'))
        # No horizontal overflow; the map stays usable on a narrow display.
        page.set_viewport_size({'width':390,'height':844})
        page.wait_for_timeout(150)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert page.locator('#map').bounding_box()['height']>=200
        page.screenshot(path=str(root.parent/'interface-mobile-fr.png'))
        assert errors==[]
        browser.close()


def test_community_page_language_and_names_are_not_translated(live_app):
    url,root=live_app
    with playwright_sync_api.sync_playwright() as pw:
        browser=H.launch_browser(pw)
        page=browser.new_page(viewport={'width':1300,'height':850})
        lon,lat=H.lonlat(H.X0+120,H.Y0+120)
        page.request.post(url+'/api/reference',data={'lon':lon,'lat':lat})
        page.goto(url,wait_until='domcontentloaded')
        page.wait_for_function('S.ready')
        page.locator('#ref-confirm').click()
        page.wait_for_function('S.network && !S.polling')
        original=page.evaluate('S.network.streets.features[0].properties.original')
        page.request.post(url+'/api/names',data={'street_id':original,'name':'Save','user':''})
        page.goto(url+'/simple',wait_until='domcontentloaded')
        page.wait_for_function('typeof S !== "undefined" && S.network && !S.busy')
        page.wait_for_function('document.querySelector(".brand-symbol img").naturalWidth === 1278')
        page.locator('#language-select').select_option('fr')
        assert page.locator('h1').inner_text()=='Noms des rues — Fotouni'
        assert page.locator('#side h2').inner_text()=='Rues à nommer'
        page.evaluate('(id)=>openStreet(id,null)',original)
        assert page.locator('#nm').input_value()=='Save'
        page.locator('#nm').fill('Nom en cours')
        page.locator('#language-select').select_option('en')
        assert page.locator('#nm').input_value()=='Nom en cours'
        page.locator('#language-select').select_option('fr')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('typeof I18n !== "undefined"')
        assert page.locator('html').get_attribute('lang')=='fr'
        assert page.locator('#language-select').input_value()=='fr'
        assert page.request.get(url+'/api/project').json()['names'][str(original)]=='Save'
        browser.close()
