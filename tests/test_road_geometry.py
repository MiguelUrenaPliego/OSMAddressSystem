"""Road shape corrections retain graph identities and remain undoable."""

import hashlib

import pytest

from test_app_backend import client, root, set_reference, compute
from osmaddresssystem.app.server import create_app
from fastapi.testclient import TestClient


def curve(coords):
    a, b = coords[0], coords[-1]
    return [a, [(a[0] + b[0]) / 2 + 0.00004, (a[1] + b[1]) / 2 + 0.00006], b]


def test_shape_preserves_ids_junctions_files_and_history(client, root):
    files = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / 'network').iterdir() if p.is_file()}
    set_reference(client)
    ctx = client.app.state.ctx
    original, _ = ctx.get_effective()
    old_edges = original.edges.copy()
    old_nodes = original.nodes.copy()
    data = client.get('/api/roads/geometry').json()
    edge = data['edges']['features'][0]
    ref = edge['properties']['ref']
    coords = curve(edge['geometry']['coordinates'])
    before_hash = ctx.store.project().state_hash()
    response = client.post('/api/roads/geometry', json={'ref': ref, 'coords': coords, 'revision': data['revision']})
    assert response.status_code == 200, response.text
    assert ctx.store.project().state_hash() != before_hash
    changed, _ = ctx.get_effective()
    assert changed.edges.index.equals(old_edges.index)
    assert changed.edges['street_id'].equals(old_edges['street_id'])
    assert changed.nodes.equals(old_nodes)
    edited = [idx for idx in changed.edges.index if not changed.edges.loc[idx].geometry.equals_exact(old_edges.loc[idx].geometry, 1e-6)]
    assert len(edited) == 2  # both directed representations of one edge
    for idx in edited:
        assert changed.edges.loc[idx, 'length'] == pytest.approx(changed.edges.loc[idx].geometry.length)
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in files.items())
    assert ctx.store.history()[0]['action'] == 'road_geometry'
    assert client.post('/api/undo').status_code == 200
    restored, _ = ctx.get_effective()
    assert restored.edges.geometry.equals(old_edges.geometry)
    assert client.post('/api/redo').status_code == 200
    reopened = TestClient(create_app(root, auth_enabled=False, auto_compute=False))
    assert ref in reopened.app.state.ctx.store.project().roads['geometry']
    assert compute(reopened)['streets']['features']


def test_shape_rejects_stale_edits_moving_junctions_and_invalid_coordinates(client):
    data = client.get('/api/roads/geometry').json()
    edge = data['edges']['features'][0]
    payload = {'ref': edge['properties']['ref'], 'coords': curve(edge['geometry']['coordinates']), 'revision': data['revision']}
    moving = {**payload, 'coords': [[p[0] + .001, p[1]] for p in payload['coords']]}
    assert client.post('/api/roads/geometry', json=moving).status_code == 400
    assert client.post('/api/roads/geometry', json={**payload, 'coords': [[0, 100], [0, 101]]}).status_code == 400
    assert client.post('/api/roads/geometry', json=payload).status_code == 200
    assert client.post('/api/roads/geometry', json=payload).status_code == 409


def test_shape_of_drawn_road_preserves_import_and_connections(client):
    from app_helpers import lonlat, X0, Y0
    a, b = lonlat(X0 - 500, Y0 - 500), lonlat(X0 - 400, Y0 - 500)
    assert client.post('/api/roads/draw', json={'coords': [a, b], 'start': {'type': 'none'}, 'end': {'type': 'none'}}).status_code == 200
    ctx = client.app.state.ctx
    before, _ = ctx.get_effective()
    road_edges = before.edges[before.edges['osmid'].astype(str).str.startswith('road')]
    from osmaddresssystem.app.roads import edge_ref
    idx = next(idx for idx in road_edges.index if idx[0] < idx[1])
    ref = edge_ref(*idx)
    # This isolated road is outside the AOI; saving by stable reference still works.
    coords = list(road_edges.to_crs(4326).loc[idx].geometry.coords)
    imports = ctx.store.project().roads['imports']
    response = client.post('/api/roads/geometry', json={'ref': ref, 'coords': curve(coords), 'revision': ctx.store.revision()})
    assert response.status_code == 200, response.text
    after, result = ctx.get_effective()
    assert ctx.store.project().roads['imports'] == imports
    assert before.edges.index.equals(after.edges.index)
    assert before.nodes.equals(after.nodes)
    assert before.edges['street_id'].equals(after.edges['street_id'])
    assert not before.edges.loc[idx].geometry.equals(after.edges.loc[idx].geometry)
    overlay = result.new_edges.to_crs(after.crs).iloc[0].geometry
    assert overlay.hausdorff_distance(after.edges.loc[idx].geometry) < 1e-5


def test_current_shape_wins_over_historical_csv_geometry(root):
    import json
    import pandas as pd
    import geopandas as gpd
    from shapely.geometry import LineString
    from osmaddresssystem.app.engine import load_base
    from osmaddresssystem.app.project import Project
    from osmaddresssystem.app.roads import edge_ref

    base = load_base(Project(root))
    idx = next(idx for idx in base.edges.index if idx[0] < idx[1])
    row = base.edges.loc[idx]
    coords = list(base.edges.to_crs(4326).loc[idx].geometry.coords)
    historical = curve(coords)
    pd.DataFrame([{'u': idx[0], 'v': idx[1], 'key': idx[2], 'new_street_id': int(row.street_id),
                   'street_name': '', 'geom_edited': True, 'is_new': False,
                   'coords': json.dumps([[lat, lon] for lon, lat in historical])}]).to_csv(root / 'input' / 'edge_edits.csv', index=False)
    app = TestClient(create_app(root, auth_enabled=False, auto_compute=False))
    set_reference(app)
    revised = curve(coords)
    revised[1][0] -= .00008
    assert app.post('/api/roads/geometry', json={'ref': edge_ref(*idx), 'revision': app.app.state.ctx.store.revision(), 'coords': revised}).status_code == 200
    compute(app)
    expected = gpd.GeoSeries([LineString(revised)], crs=4326).to_crs(base.crs).iloc[0]
    assert app.app.state.ctx.network.edges.loc[idx].geometry.hausdorff_distance(expected) < 1e-5
