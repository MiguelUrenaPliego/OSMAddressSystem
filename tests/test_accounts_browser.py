"""Two real editor sessions: setup, invite, attribution, history and revocation."""
import socket
import threading
import time
import pytest
import uvicorn
from test_app_frontend import playwright_sync_api
import app_helpers as H
from osmaddresssystem.app.server import create_app

PASSWORD='Only for browser tests! 2026'


@pytest.fixture
def protected_app(tmp_path):
    root=H.make_project(tmp_path/'project')
    app=create_app(root,auto_compute=True)
    base=app.state.ctx.get_base()
    node=base.nodes.index[0];row=base.nodes.loc[node]
    lon,lat=H.lonlat(row.x,row.y)
    app.state.ctx.store.set_reference(lat,lon,int(node),default=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    for _ in range(100):
        if server.started:break
        time.sleep(.1)
    yield f'http://127.0.0.1:{port}',root,app
    server.should_exit=True;thread.join(timeout=15)


def test_invited_contributor_and_attributed_history_in_two_browsers(protected_app):
    url,root,app=protected_app
    with playwright_sync_api.sync_playwright() as pw:
        browser=H.launch_browser(pw)
        admin=browser.new_page(viewport={'width':1500,'height':1000})
        errors=[]
        admin.on('pageerror',lambda e:errors.append(str(e)))
        admin.goto(url,wait_until='domcontentloaded')
        admin.locator('#account-username').fill('admin')
        admin.locator('#account-name').fill('Pilot Admin')
        admin.locator('#account-password').fill(PASSWORD)
        admin.locator('#account-repeat').fill(PASSWORD)
        admin.locator('#account-submit').click()
        admin.wait_for_function('S.ready')
        admin.locator('#ref-confirm').click()
        admin.wait_for_function('S.network && !S.polling')
        admin.locator('#account-team').click()
        admin.locator('#invite-form [name=username]').fill('community')
        admin.locator('#invite-form [name=display_name]').fill('Community Editor')
        admin.locator('#invite-form [name=role]').select_option('contributor')
        admin.locator('#invite-form button').click()
        admin.locator('#invitation-link').wait_for()
        link=admin.locator('#invitation-link').input_value()
        admin.locator('#account-dialog-close').click()
        # Separate storage/cookies: this must not inherit the administrator session.
        context=browser.new_context(viewport={'width':1400,'height':950})
        member=context.new_page();member.on('pageerror',lambda e:errors.append(str(e)))
        member.goto(link,wait_until='domcontentloaded')
        member.locator('#account-password').fill(PASSWORD)
        member.locator('#account-repeat').fill(PASSWORD)
        member.locator('#account-submit').click()
        member.wait_for_function('S.ready')
        assert '#invite' not in member.url
        member.locator('#ref-confirm').click()
        member.wait_for_function('S.network && !S.polling')
        assert member.locator('#ref-move').is_disabled()
        member.locator('#tabs button[data-tab=roads]').click()
        member.locator('details[data-sec=draw] summary').click()
        assert member.locator('[data-act=draw-new]').is_disabled()
        assert member.locator('#btn-save').is_disabled()
        member.locator('#tabs button[data-tab=names]').click()
        member.wait_for_function('S.tab === "names"')
        member.evaluate('selectStreet(S.network.streets.features[0].properties.id)')
        member.wait_for_function('document.querySelectorAll("#name-input").length===1')
        member.locator('#name-input').fill('Community test street')
        member.locator('[data-act=set-name]').click()
        member.wait_for_function('Object.values(S.project.names).includes("Community test street")')
        actor=member.evaluate('AuthUI.state.user.id')
        record=next(r for r in app.state.ctx.store.audit() if r['action']=='name' and r['after']=='Community test street')
        assert record['actor']==actor and record['details']['display_name']=='Community Editor'
        admin.locator('#account-history').click()
        admin.locator('#audit-filter').fill('Community Editor')
        admin.wait_for_function('document.querySelector("#audit-list").textContent.includes("name")')
        assert 'Community Editor' in admin.locator('#audit-list').inner_text()
        admin.locator('#account-dialog-close').click()
        # Role updates must revoke active sessions, including API requests.
        admin.locator('#account-team').click()
        row=admin.locator('[data-person]').filter(has_text='Community Editor')
        row.locator('select').select_option('viewer')
        with admin.expect_response(lambda r:'/api/auth/users/' in r.url and r.request.method=='POST'):
            row.locator('[data-update]').click()
        result=member.evaluate('''async()=>{const r=await fetch('api/project');return r.status;}''')
        assert result==401
        member.evaluate('api("/project").catch(()=>{})')
        member.locator('#account-login').wait_for()
        assert member.locator('#account-error').inner_text()
        assert errors==[]
        context.close();browser.close()


def test_french_local_setup_and_mobile_layout(protected_app):
    url,root,app=protected_app
    with playwright_sync_api.sync_playwright() as pw:
        browser=H.launch_browser(pw)
        page=browser.new_page(viewport={'width':390,'height':844})
        page.goto(url+'/simple/',wait_until='domcontentloaded')
        page.locator('#account-language').select_option('fr')
        assert page.locator('#account-submit').inner_text()=='Créer le compte administrateur'
        assert page.locator('#account-gate').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert page.evaluate('document.querySelector("#account-gate").scrollWidth<=document.querySelector("#account-gate").clientWidth')
        card=page.locator('.account-card').bounding_box()
        assert card['x']>=0 and card['x']+card['width']<=390
        page.screenshot(path=str(root.parent/'accounts-setup-fr.png'))
        browser.close()
