"""Authenticated routes, revocation, invitations and retained audit evidence."""
import time
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_app_backend import root
from osmaddresssystem.app.server import create_app
from osmaddresssystem.app.auth import users,sessions,invites,attempts
from osmaddresssystem.app.store import Store,events_t,audit_t

PASSWORD='Only for synthetic tests! 2026'


@pytest.fixture
def secured(root):
    app=create_app(root,auto_compute=False)
    app.state.accounts.bootstrap('admin','Pilot Admin',PASSWORD)
    return TestClient(app,base_url='https://testserver')


def login(client,username='admin'):
    r=client.post('/api/auth/login',json={'username':username,'password':PASSWORD})
    assert r.status_code==200,r.text
    return r.json()


def write(client,path,body=None):
    csrf=client.get('/api/auth/status').json()['csrf_token']
    return client.post('/api'+path,json=body or {},headers={'X-CSRF-Token':csrf})


def member(admin,role='mapper',username='mapper'):
    response=write(admin,'/auth/users',{'username':username,'display_name':'Member '+username,'role':role})
    assert response.status_code==200,response.text
    data=response.json()
    client=TestClient(admin.app,base_url='https://testserver')
    activated=client.post('/api/auth/activate',json={'token':data['activation_token'],'password':PASSWORD})
    assert activated.status_code==200,activated.text
    return client,data


def test_auth_is_default_closed_and_actor_cannot_be_spoofed(secured):
    assert secured.get('/api/project').status_code==401
    assert secured.post('/api/names',json={'street_id':1,'name':'Fake','user':'admin'}).status_code==401
    state=login(secured)
    result=write(secured,'/names',{'street_id':1,'name':'A','user':'Miguel'})
    assert result.status_code==200,result.text
    assert secured.get('/api/history').json()[0]['user']==state['user']['id']
    record=next(r for r in secured.get('/api/audit').json() if r['action']=='name')
    assert record['actor']==state['user']['id']
    assert record['details']['display_name']=='Pilot Admin'
    assert record['details']['source']=='session'
    assert record['after']=='A'


@pytest.mark.parametrize('role',['viewer','contributor','mapper'])
def test_api_role_boundaries_and_project_admin_only(secured,role):
    login(secured)
    client,_=member(secured,role,role)
    assert client.get('/api/project').status_code==200
    r=write(client,'/names',{'street_id':1,'name':'Member name'})
    assert r.status_code==(403 if role=='viewer' else 200),r.text
    for path,body in [('/settings',{'max_levels':1}),('/reference',{'lat':5,'lon':10}),('/restore',{'id':1}),('/auth/users',{'username':'x'})]:
        assert write(client,path,body).status_code==403,path
    assert client.get('/api/auth/users').status_code==403
    # Permission is checked before interpreting a forged geometry payload.
    response=write(client,'/roads/geometry',{})
    assert response.status_code==(400 if role=='mapper' else 403),response.text
    assert client.get('/api/audit').status_code==(403 if role=='viewer' else 200)


def test_csrf_cross_origin_and_cookie_scope(secured):
    response=secured.post('/api/auth/login',json={'username':'admin','password':PASSWORD})
    cookie=response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'secure' in cookie and 'samesite=strict' in cookie
    csrf=response.json()['csrf_token']
    assert secured.post('/api/names',json={'street_id':1,'name':'Bad'}).status_code==403
    assert secured.post('/api/names',json={'street_id':1,'name':'Bad'},headers={'X-CSRF-Token':csrf,'Origin':'https://evil.example'}).status_code==403
    assert secured.post('/api/auth/login',data={'username':'admin','password':PASSWORD}).status_code==415
    assert write(secured,'/names',{'street_id':1,'name':'Good'}).status_code==200


def test_viewer_reference_confirmation_is_read_only(secured):
    import app_helpers as H
    login(secured)
    lon,lat=H.lonlat(H.X0+120,H.Y0+120)
    assert write(secured,'/reference',{'lon':lon,'lat':lat}).status_code==200
    viewer,_=member(secured,'viewer','reader')
    before=viewer.get('/api/project').json()
    result=write(viewer,'/reference/confirm',{'client':'cannot_rewrite_reference'})
    assert result.status_code==200,result.text
    after=viewer.get('/api/project').json()
    assert before['reference']==after['reference']
    assert before['status']['revision']==after['status']['revision']


def test_invitation_single_use_and_no_stored_raw_credentials(secured):
    login(secured)
    member_client,data=member(secured)
    token=data['activation_token']
    assert member_client.post('/api/auth/activate',json={'token':token,'password':PASSWORD}).status_code==400
    store=secured.app.state.ctx.store
    with store.engine.connect() as c:
        hashes=[r.password_hash for r in c.execute(sa.select(users))]
        assert all(h and h.startswith('scrypt$') and PASSWORD not in h for h in hashes)
        assert not c.execute(sa.select(invites)).first()
        session_data=str(c.execute(sa.select(sessions)).all())
        assert token not in session_data
    assert token not in str(store.audit()) and PASSWORD not in str(store.audit())


def test_disable_role_change_logout_and_password_change_revoke_sessions(secured):
    login(secured)
    mapper,data=member(secured)
    result=write(secured,'/auth/users/'+data['user_id'],{'role':'viewer','enabled':True})
    assert result.status_code==200
    assert mapper.get('/api/project').status_code==401
    login(mapper,'mapper')
    assert write(mapper,'/names',{'street_id':1,'name':'No'}).status_code==403
    old_cookie=dict(mapper.cookies)
    assert write(mapper,'/auth/logout').status_code==200
    assert mapper.get('/api/project').status_code==401
    stolen=TestClient(secured.app,base_url='https://testserver');stolen.cookies.update(old_cookie)
    assert stolen.get('/api/project').status_code==401
    login(mapper,'mapper')
    assert write(mapper,'/auth/password',{'current_password':PASSWORD,'password':'A new synthetic passphrase!'}).status_code==200
    assert mapper.get('/api/project').status_code==401
    assert mapper.post('/api/auth/login',json={'username':'mapper','password':PASSWORD}).status_code==401


def test_session_expiry_and_invite_expiry(secured):
    login(secured)
    mapper,data=member(secured)
    engine=secured.app.state.ctx.store.engine
    with engine.begin() as c:c.execute(sessions.update().where(sessions.c.user_id==data['user_id']).values(last_seen=time.time()-1900))
    assert mapper.get('/api/project').status_code==401
    new=write(secured,'/auth/users',{'username':'expired','display_name':'Expired','role':'viewer'}).json()
    with engine.begin() as c:c.execute(invites.update().values(expires=time.time()-1))
    assert mapper.post('/api/auth/activate',json={'token':new['activation_token'],'password':PASSWORD}).status_code==400


def test_login_throttle_and_generic_errors(secured):
    errors=[]
    for username in ['admin','missing','admin','missing','admin','missing','admin','missing']:
        response=secured.post('/api/auth/login',json={'username':username,'password':'Wrong test passphrase'})
        assert response.status_code==401
        errors.append(response.json())
    assert all(e==errors[0] for e in errors)
    assert secured.post('/api/auth/login',json={'username':'admin','password':PASSWORD}).status_code==429


def test_two_editors_undo_and_restore_have_conflict_guards(secured):
    login(secured)
    mapper,_=member(secured)
    assert write(secured,'/names',{'street_id':1,'name':'Admin change'}).status_code==200
    assert write(mapper,'/names',{'street_id':2,'name':'Mapper change'}).status_code==200
    assert write(secured,'/undo').status_code==409
    assert write(mapper,'/undo').status_code==200
    assert write(secured,'/undo').status_code==200
    checkpoint=write(secured,'/save',{'label':'Before restore'}).json()['checkpoint']
    rev=secured.get('/api/project').json()['status']['revision']
    assert write(secured,'/names',{'street_id':3,'name':'Later'}).status_code==200
    assert write(secured,'/restore',{'id':checkpoint,'revision':rev,'reason':'Review restoration'}).status_code==409
    rev=secured.get('/api/project').json()['status']['revision']
    result=write(secured,'/restore',{'id':checkpoint,'revision':rev,'reason':'Review restoration'})
    assert result.status_code==200,result.text
    assert any(r['kind']=='checkpoint_restore' and r['details']['reason']=='Review restoration' for r in secured.get('/api/audit').json())
    assert len(secured.get('/api/auth/users').json())==2  # restore never rolls back accounts


def test_audit_retains_discarded_redo_and_legacy_identity(root):
    store=Store(root)
    store.set_name(1,'Anonymous')
    store.undo()
    store.set_name(2,'Other')
    assert store.redo() is None
    assert len(store.history())==2
    audit=store.audit()
    assert any(r['after']=='Anonymous' and r['details']['display_name']=='Unknown (legacy)' for r in audit)
    assert any(r['kind']=='undo' for r in audit)
    assert any(r['kind']=='redo_branch_closed' for r in audit)
    again=Store(root)
    assert len(again.audit())==len(audit)
    with again.engine.connect() as c:assert c.execute(sa.select(sa.func.count()).select_from(events_t)).scalar_one()==2


def test_redo_refuses_when_another_editor_undid_its_prerequisite(secured):
    login(secured)
    mapper,_=member(secured)
    assert write(secured,'/names',{'street_id':1,'name':'First'}).status_code==200
    assert write(mapper,'/names',{'street_id':2,'name':'Second'}).status_code==200
    assert write(mapper,'/undo').status_code==200
    assert write(secured,'/undo').status_code==200
    assert write(mapper,'/redo').status_code==409


def test_initial_setup_loopback_only_and_cannot_replace_admin(root):
    app=create_app(root,auto_compute=False)
    remote=TestClient(app,base_url='https://example.org')
    assert remote.post('/api/auth/setup',json={'username':'admin','display_name':'Admin','password':PASSWORD}).status_code==403
    local=TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345))
    result=local.post('/api/auth/setup',json={'username':'admin','display_name':'Admin','password':PASSWORD})
    assert result.status_code==200,result.text
    assert local.get('/api/project').status_code==200
    assert local.post('/api/auth/setup',json={'username':'other','display_name':'Other','password':PASSWORD}).status_code==409
    assert write(local,'/auth/users/'+result.json()['user']['id'],{'enabled':False}).status_code==409


def test_accounts_sessions_and_audit_survive_restart(secured,root):
    original=login(secured)
    assert write(secured,'/names',{'street_id':1,'name':'Persistent'}).status_code==200
    audit=secured.get('/api/audit').json()
    reopened=TestClient(create_app(root,auto_compute=False),base_url='https://testserver')
    reopened.cookies.update(dict(secured.cookies))
    status=reopened.get('/api/auth/status').json()
    assert status['user']['id']==original['user']['id']
    assert reopened.get('/api/project').json()['names']['1']=='Persistent'
    assert len(reopened.get('/api/audit').json())==len(audit)


def test_cookie_scope_and_no_store_under_deployment_prefix(root):
    app=create_app(root,auto_compute=False)
    app.state.accounts.bootstrap('admin','Admin',PASSWORD)
    client=TestClient(app,base_url='https://testserver/pilot',root_path='/pilot')
    response=client.post('/api/auth/login',json={'username':'admin','password':PASSWORD})
    assert response.status_code==200,response.text
    assert 'Path=/pilot/' in response.headers['set-cookie']
    assert response.headers['cache-control']=='no-store'
    assert client.get('/api/project').status_code==200


def test_queued_write_loses_permissions_after_revocation(secured):
    import asyncio
    import httpx
    login(secured)
    mapper,data=member(secured)
    admin_csrf=secured.get('/api/auth/status').json()['csrf_token']
    mapper_csrf=mapper.get('/api/auth/status').json()['csrf_token']
    async def scenario():
        entered,released=asyncio.Event(),asyncio.Event()
        @secured.app.post('/api/roads/_test_pause')
        async def pause():
            entered.set();await released.wait();return {'ok':True}
        transport=httpx.ASGITransport(app=secured.app)
        async with httpx.AsyncClient(transport=transport,base_url='https://testserver',cookies=dict(secured.cookies),headers={'X-CSRF-Token':admin_csrf}) as admin:
            async with httpx.AsyncClient(transport=transport,base_url='https://testserver',cookies=dict(mapper.cookies),headers={'X-CSRF-Token':mapper_csrf}) as other:
                hold=asyncio.create_task(admin.post('/api/roads/_test_pause',json={}))
                await entered.wait()
                revoke=asyncio.create_task(admin.post('/api/auth/users/'+data['user_id'],json={'enabled':False}))
                await asyncio.sleep(.03)
                edit=asyncio.create_task(other.post('/api/names',json={'street_id':1,'name':'Must not be saved'}))
                await asyncio.sleep(.03)
                released.set()
                results=await asyncio.gather(hold,revoke,edit)
                assert [r.status_code for r in results]==[200,200,401]
    asyncio.run(scenario())
    assert 'Must not be saved' not in secured.app.state.ctx.store.project().names.values()


def test_admin_access_reset_rotates_invitation_and_preserves_actor_id(secured):
    login(secured)
    mapper,data=member(secured)
    response=write(secured,'/auth/users/'+data['user_id'],{'reset':True,'enabled':True})
    assert response.status_code==200,response.text
    token=response.json()['activation_token']
    assert mapper.get('/api/project').status_code==401
    assert mapper.post('/api/auth/login',json={'username':'mapper','password':PASSWORD}).status_code==401
    result=mapper.post('/api/auth/activate',json={'token':token,'password':'A different synthetic passphrase!'})
    assert result.status_code==200,result.text
    assert result.json()['user']['id']==data['user_id']
    assert token not in str(secured.app.state.ctx.store.audit())


def test_local_console_recovery_revokes_existing_sessions(secured):
    login(secured)
    account=secured.app.state.accounts
    account.reset_local('admin','Recovered Admin','Recovered synthetic passphrase!')
    assert secured.get('/api/project').status_code==401
    result=secured.post('/api/auth/login',json={'username':'admin','password':'Recovered synthetic passphrase!'})
    assert result.status_code==200,result.text
    assert result.json()['user']['display_name']=='Recovered Admin'
    assert any(r['action']=='local_account_recovery' for r in secured.app.state.ctx.store.audit())
