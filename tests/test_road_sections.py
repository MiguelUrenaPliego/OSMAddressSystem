"""Partial deletions retain the outside geometry and survive recovery."""

import hashlib

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient

from test_app_backend import client, root, set_reference, compute
from osmaddresssystem.app.server import create_app
from osmaddresssystem.app.roads import edge_ref


def cut_request(client, start=.25, end=.75, ref=None):
    base, _ = client.app.state.ctx.get_effective()
    idx = next(i for i in base.edges.index if i[0] < i[1] and (ref is None or edge_ref(*i) == ref))
    line = base.edges.loc[idx].geometry
    points = gpd.GeoSeries([line.interpolate(line.length*f) for f in (start, end)], crs=base.crs).to_crs(4326)
    return {'ref': edge_ref(*idx), 'points': [[p.x, p.y] for p in points], 'revision': client.app.state.ctx.store.revision()}, idx, line


def test_partial_cut_preview_persistence_undo_restore_and_compute(client, root):
    set_reference(client)
    before, _ = client.app.state.ctx.get_effective()
    before_edges = before.edges.copy()
    source_hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'network').iterdir() if p.is_file()}
    body, idx, line = cut_request(client)
    preview = client.post('/api/roads/cut/preview', json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()['properties']['retained_parts'] == 2
    assert preview.json()['properties']['removed_m'] == pytest.approx(line.length/2, abs=.02)
    assert client.app.state.ctx.store.revision() == body['revision']
    response = client.post('/api/roads/cut', json=body)
    assert response.status_code == 200, response.text
    after, _ = client.app.state.ctx.get_effective()
    assert idx not in after.edges.index
    assert len(after.edges) == len(before_edges)+2  # two directions become four
    assert after.edges.length.sum() == pytest.approx(before_edges.length.sum()-line.length, abs=.02)
    assert after.nodes.index.min() < 0
    for (u, v, _), row in after.edges.iterrows():
        from shapely.geometry import Point
        assert Point(row.geometry.coords[0]).distance(after.nodes.loc[u].geometry) < .01
        assert Point(row.geometry.coords[-1]).distance(after.nodes.loc[v].geometry) < .01
    for p, old_hash in source_hashes.items():
        assert hashlib.sha256(p.read_bytes()).hexdigest() == old_hash
    assert client.app.state.ctx.store.history()[0]['action'] == 'road_cut'
    assert client.post('/api/roads/cut', json=body).status_code == 409
    compute(client)
    from shapely.ops import unary_union
    state = client.app.state.ctx.network
    original_osmid = str(before_edges.loc[idx].osmid)
    kept = state.edges[state.edges.osmid.astype(str) == original_osmid]
    assert line.interpolate(line.length/2).distance(unary_union(list(kept.geometry))) > 1
    checkpoint = client.post('/api/save', json={'label':'cut'}).json()['checkpoint']
    client.post('/api/undo')
    restored, _ = client.app.state.ctx.get_effective()
    assert restored.edges.geometry.equals(before_edges.geometry)
    client.post('/api/redo')
    client.post('/api/restore', json={'id': checkpoint})
    reopened = TestClient(create_app(root, auth_enabled=False, auto_compute=False))
    actual, _ = reopened.app.state.ctx.get_effective()
    assert actual.edges.index.equals(after.edges.index)
    assert actual.nodes.index.equals(after.nodes.index)
    compute(reopened)


def test_trim_end_keeps_the_rest_and_can_cut_a_retained_piece(client):
    body, _, line = cut_request(client, .8, 1)
    assert client.post('/api/roads/cut', json=body).status_code == 200
    base, _ = client.app.state.ctx.get_effective()
    fragment = next(i for i in base.edges.index if i[0] < i[1] and min(i[:2]) < 0)
    body, _, _ = cut_request(client, .2, .4, edge_ref(*fragment))
    assert client.post('/api/roads/cut', json=body).status_code == 200
    assert len(client.app.state.ctx.store.project().roads['cuts']) == 2
    base2, _ = client.app.state.ctx.get_effective()
    assert len(base2.edges) > len(base.edges)


def test_cut_validates_points_and_records_uncertain_route_ends(client):
    body, _, _ = cut_request(client)
    same = {**body, 'points': [body['points'][0], body['points'][0]]}
    assert client.post('/api/roads/cut', json=same).status_code == 400
    assert client.app.state.ctx.store.revision() == body['revision']
    # The uncertain endpoint must remain unconnected even with a snap target.
    a, b = body['points']
    r = client.post('/api/roads/draw', json={'coords': [a, b], 'end': {'type':'node','node':1}, 'end_review':'uncertain'})
    assert r.status_code == 200
    feature = client.app.state.ctx.store.project().roads['imports'][-1]['features'][0]
    assert feature['end_review'] == 'uncertain'
    assert feature['end'] == {'type':'none'}
    roads = client.get('/api/roads')
    assert roads.status_code == 200, roads.text
    assert len(roads.json()['report']['uncertain_ends']) == 1


def test_cutting_a_drawn_tail_does_not_keep_a_phantom_uncertainty_marker(client):
    from app_helpers import X0, Y0, lonlat
    a, b = lonlat(X0-400,Y0-400), lonlat(X0-300,Y0-400)
    assert client.post('/api/roads/draw', json={'coords':[a,b], 'start':{'type':'none'}, 'end_review':'uncertain'}).status_code == 200
    base, _ = client.app.state.ctx.get_effective()
    idx = next(i for i in base.edges.index if i[0]<i[1] and str(base.edges.loc[i].osmid).startswith('road'))
    body, _, _ = cut_request(client, .8, 1, edge_ref(*idx))
    assert client.post('/api/roads/cut', json=body).status_code == 200
    r = client.get('/api/roads')
    assert r.status_code == 200, r.text
    assert not r.json()['report']['uncertain_ends']


def test_old_csv_geometry_cannot_restore_the_deleted_portion(root):
    import json
    import pandas as pd
    from osmaddresssystem.app.engine import load_base
    from osmaddresssystem.app.project import Project
    from shapely.ops import unary_union
    base = load_base(Project(root))
    idx = next(i for i in base.edges.index if i[0]<i[1])
    line = base.edges.loc[idx].geometry
    coords = list(base.edges.to_crs(4326).loc[idx].geometry.coords)
    pd.DataFrame([{'u':idx[0],'v':idx[1],'key':idx[2], 'new_street_id':int(base.edges.loc[idx].street_id),
                  'street_name':'', 'geom_edited':'true','is_new':'false', 'coords':json.dumps([[lat,lon] for lon,lat in coords])}]).to_csv(root/'input/edge_edits.csv',index=False)
    c = TestClient(create_app(root,auth_enabled=False,auto_compute=False))
    set_reference(c)
    body, _, _ = cut_request(c,ref=edge_ref(*idx))
    assert c.post('/api/roads/cut',json=body).status_code == 200
    compute(c)
    state = c.app.state.ctx.network
    kept = state.edges[state.edges.osmid.astype(str) == str(base.edges.loc[idx].osmid)]
    assert line.interpolate(line.length/2).distance(unary_union(list(kept.geometry))) > 1


def test_reshape_a_cut_fragment_then_delete_it(client, root):
    body, _, _ = cut_request(client, .5, 1)
    assert client.post('/api/roads/cut',json=body).status_code == 200
    data = client.get('/api/roads/geometry').json()
    fragment = next(f for f in data['edges']['features'] if f['properties']['ref'].startswith('-'))
    coords = fragment['geometry']['coordinates']
    a,b = coords[0],coords[-1]
    shaped = [a,[(a[0]+b[0])/2+.00008,(a[1]+b[1])/2+.00006],b]
    assert client.post('/api/roads/geometry',json={'ref':fragment['properties']['ref'],'coords':shaped,'revision':data['revision']}).status_code == 200
    body, _, line = cut_request(client, 0, 1, fragment['properties']['ref'])
    before, _ = client.app.state.ctx.get_effective()
    assert client.post('/api/roads/cut',json=body).status_code == 200
    after, report = client.app.state.ctx.get_effective()
    assert not report.report['stale_cuts']
    assert len(after.edges) == len(before.edges)-2
    assert fragment['properties']['ref'] not in {edge_ref(*i) for i in after.edges.index}
    assert client.post('/api/undo').status_code == 200
    restored, _ = client.app.state.ctx.get_effective()
    assert len(restored.edges) == len(before.edges)
    client.post('/api/redo')
    reopened = TestClient(create_app(root,auth_enabled=False,auto_compute=False))
    final, _ = reopened.app.state.ctx.get_effective()
    assert final.edges.index.equals(after.edges.index)
