"""Physical junction edits on current cut fragments, independent of street IDs."""
import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from test_app_backend import client, root, set_reference, compute
from test_road_sections import cut_request
from osmaddresssystem.app.roads import edge_ref
from osmaddresssystem.app.server import create_app


def gap(client):
    body, idx, line = cut_request(client, .45, .55)
    assert client.post('/api/roads/cut', json=body).status_code == 200
    base, _ = client.app.state.ctx.get_effective()
    refs = [i for i in base.edges.index if i[0]<i[1] and min(i[:2])<0]
    source,target = refs
    node = next(n for n in source[:2] if n<0)
    return base,source,target,node


@pytest.mark.parametrize('interior',[False,True])
def test_connect_fragments_undo_reopen_and_cut_again(client,root,interior):
    set_reference(client)
    base, source, target, node = gap(client)
    target_node=next(n for n in target[:2] if n<0)
    pt=base.edges.loc[target].geometry.interpolate(.5,normalized=True) if interior else base.nodes.loc[target_node].geometry
    pt=gpd.GeoSeries([pt],crs=base.crs).to_crs(4326).iloc[0]
    body={'ref':edge_ref(*source),'source_node':node,'target_ref':edge_ref(*target),'lon':pt.x,'lat':pt.y,'revision':client.app.state.ctx.store.revision()}
    preview=client.post('/api/roads/join/preview',json=body)
    assert preview.status_code==200,preview.text
    assert not preview.json()['already_connected']
    assert client.app.state.ctx.store.revision()==body['revision']
    response=client.post('/api/roads/join',json=body)
    assert response.status_code==200,response.text
    joined,res=client.app.state.ctx.get_effective()
    assert res.report['stale_cuts']==[]
    assert not any(node in i[:2] for i in joined.edges.index)
    assert set(joined.edges.street_id)==set(base.edges.street_id)
    assert len(joined.edges)==len(base.edges)+(2 if interior else 0)
    assert client.post('/api/roads/join',json=body).status_code==409
    persisted=TestClient(create_app(root,auth_enabled=False,auto_compute=False))
    reopened,_=persisted.app.state.ctx.get_effective()
    assert reopened.edges.index.equals(joined.edges.index)
    client.post('/api/undo')
    undone,_=client.app.state.ctx.get_effective()
    assert undone.edges.index.equals(base.edges.index)
    client.post('/api/redo')
    # Delete an actual current joined section; replay must join before cutting it.
    ref=next(edge_ref(*i) for i in joined.edges.index if i[0]<i[1] and node not in i[:2] and min(i[:2])<0)
    cut,_,_=cut_request(client,.2,.8,ref)
    response=client.post('/api/roads/cut',json=cut)
    assert response.status_code==200,response.text
    actual,res=client.app.state.ctx.get_effective()
    assert res.report['stale_cuts']==[]
    compute(client)


def test_already_shared_junction_does_not_create_an_edit(client):
    base,_=client.app.state.ctx.get_effective()
    refs=[i for i in base.edges.index if i[0]<i[1]]
    source,target=next((a,b) for a in refs for b in refs if a!=b and set(a[:2])&set(b[:2]))
    node=next(iter(set(source[:2])&set(target[:2])))
    pt=gpd.GeoSeries([base.nodes.loc[node].geometry],crs=base.crs).to_crs(4326).iloc[0]
    revision=client.app.state.ctx.store.revision()
    body={'ref':edge_ref(*source),'source_node':node,'target_ref':edge_ref(*target),'lon':pt.x,'lat':pt.y,'revision':revision}
    response=client.post('/api/roads/join',json=body)
    assert response.status_code==200,response.text
    assert response.json()['already_connected'] and not response.json()['changed']
    assert client.app.state.ctx.store.revision()==revision


def test_whole_section_delete_and_current_geometry(client):
    base,source,target,node=gap(client)
    body,_,_=cut_request(client,.2,.8,edge_ref(*source))
    body['whole']=True
    result=client.post('/api/roads/cut',json=body)
    assert result.status_code==200,result.text
    after,_=client.app.state.ctx.get_effective()
    assert source not in after.edges.index and target in after.edges.index
    features=client.get('/api/roads/geometry').json()['edges']['features']
    assert edge_ref(*source) not in {f['properties']['ref'] for f in features}


def test_corrected_drawing_joins_yellow_road_and_overlay_moves(client):
    import app_helpers as H
    set_reference(client)
    # Kept free explicitly so visual proximity alone does not form a junction.
    coords=[H.lonlat(H.X0+40,H.Y0+30),H.lonlat(H.X0+60,H.Y0+20)]
    response=client.post('/api/roads/draw',json={'coords':coords,'start':{'type':'none'},'end':{'type':'none'}})
    assert response.status_code==200,response.text
    base,_=client.app.state.ctx.get_effective()
    source=next(i for i,r in base.edges.iterrows() if i[0]<i[1] and str(r.osmid).startswith('road'))
    target=min((i for i,r in base.edges.iterrows() if i[0]<i[1] and not str(r.osmid).startswith('road')),
               key=lambda i:base.edges.loc[i].geometry.distance(base.nodes.loc[source[1]].geometry))
    node=source[1]
    line=base.edges.loc[target].geometry
    point=line.interpolate(line.project(base.nodes.loc[node].geometry))
    pt=gpd.GeoSeries([point],crs=base.crs).to_crs(4326).iloc[0]
    body={'ref':edge_ref(*source),'source_node':node,'target_ref':edge_ref(*target),'lon':pt.x,'lat':pt.y,'revision':client.app.state.ctx.store.revision()}
    result=client.post('/api/roads/join',json=body)
    assert result.status_code==200,result.text
    actual,res=client.app.state.ctx.get_effective()
    assert res.report['stale_cuts']==[]
    assert any(r['state']=='connected' for r in res.report['nodes'])
    overlay=client.get('/api/roads').json()
    assert len(overlay['edges']['features'])==1
    assert node not in {f['properties']['node'] for f in overlay['nodes']['features']}
    compute(client)
