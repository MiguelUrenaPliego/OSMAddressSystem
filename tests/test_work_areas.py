"""Work tracking is persistent but does not change address calculations."""

from fastapi.testclient import TestClient
from test_app_backend import client, root, set_reference, compute
from osmaddresssystem.app.server import create_app


def test_grid_status_roundtrip_and_no_recomputation(client, root):
    set_reference(client)
    compute(client)
    store = client.app.state.ctx.store
    before = store.project()
    r = client.post('/api/work-areas/grid', json={'cell_m':250, 'revision':store.revision()})
    assert r.status_code == 200, r.text
    areas = r.json()['areas']
    assert len(areas['features']) > 1
    assert all(f['properties']['status'] == 'unreviewed' for f in areas['features'])
    assert store.project().state_hash() == before.state_hash()
    assert store.project().address_hash() == before.address_hash()
    assert not client.get('/api/project').json()['stale']
    body = {'id':areas['features'][0]['properties']['id'], 'status':'needs_review', 'note':'Path hidden by trees', 'revision':store.revision()}
    assert client.post('/api/work-areas/status', json=body).status_code == 200
    assert client.post('/api/work-areas/status', json=body).status_code == 409
    checkpoint = client.post('/api/save', json={'label':'area review'}).json()['checkpoint']
    client.post('/api/undo')
    assert store.project().work_areas['features'][0]['properties']['status'] == 'unreviewed'
    client.post('/api/restore', json={'id':checkpoint})
    reopened = TestClient(create_app(root, auth_enabled=False, auto_compute=False))
    saved = reopened.get('/api/project').json()['work_areas']['features'][0]['properties']
    assert saved['status'] == 'needs_review'
    assert saved['note'] == 'Path hidden by trees'
    assert reopened.post('/api/work-areas/grid', json={'cell_m':100,'revision':store.revision()}).status_code == 409


def test_grid_rejects_invalid_size_and_stale_create(client):
    store = client.app.state.ctx.store
    assert client.post('/api/work-areas/grid', json={'cell_m':1,'revision':store.revision()}).status_code == 400
    rev = store.revision()
    store.set_name(1,'Test')
    assert client.post('/api/work-areas/grid', json={'cell_m':250,'revision':rev}).status_code == 409
    assert not store.project().work_areas


def test_automatic_grid_handles_a_large_boundary_without_tiny_cells():
    import geopandas as gpd
    from shapely.geometry import box
    from osmaddresssystem.app.work_areas import make_grid
    boundary = gpd.GeoSeries([box(600000,570000,620000,590000)],crs=32632).to_crs(4326).iloc[0]
    grid = make_grid(boundary,32632,'auto')
    assert 1 < len(grid['features']) <= 144
    assert grid['cell_m'] > 250


def test_grid_counts_actual_work_areas_not_empty_space_between_them():
    import geopandas as gpd
    from shapely.geometry import box
    from shapely.ops import unary_union
    from osmaddresssystem.app.work_areas import make_grid
    boundary = gpd.GeoSeries([unary_union([box(600000,570000,600400,570400),box(620000,590000,620400,590400)])],crs=32632).to_crs(4326).iloc[0]
    grid = make_grid(boundary,32632,500)
    assert 1 < len(grid['features']) < 10
