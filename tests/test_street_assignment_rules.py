"""API regressions for current todo B2/B3 and rank-preserving ID edits."""

from types import SimpleNamespace

import pandas as pd
import pytest

from test_app_backend import client, root


def snapshot(client, roads, streets):
    """A computed street partition: each listed road exists in both directions.

    Original IDs may have multiple displayed parts, as after numbering cuts.
    Tests deliberately include IDs that the old max(original)+1 allocator reused.
    """
    records = []
    for u, v, sid in roads:
        records += [{'u': u, 'v': v, 'key': 0, 'street_id': sid},
                    {'u': v, 'v': u, 'key': 0, 'street_id': sid}]
    client.app.state.ctx.network = SimpleNamespace(
        edges=pd.DataFrame(records).set_index(['u', 'v', 'key']),
        streets=pd.DataFrame([{'street_id': sid, 'street_id_original': original,
                               'street_rank': rank, 'street_name': ''} for sid, original, rank in streets]))


@pytest.mark.parametrize('force', [False, True])
def test_different_ranks_cannot_be_joined_even_with_force(client, force):
    snapshot(client, [(1, 2, 10), (2, 3, 20)], [(10, 10, 3), (20, 20, 4)])
    revision = client.app.state.ctx.store.revision()
    r = client.post('/api/streets/assign', json={'ref': '1-2-0', 'mode': 'merge', 'target': 20, 'force': force})
    assert r.status_code == 409
    assert 'rank' in r.json()['detail']
    assert client.app.state.ctx.store.revision() == revision


def test_force_does_not_join_a_branch_back_into_a_non_path(client):
    snapshot(client, [(1, 2, 10), (2, 3, 10), (2, 4, 21)], [(10, 10, 4), (21, 10, 4)])
    revision = client.app.state.ctx.store.revision()
    r = client.post('/api/streets/assign', json={'ref': '2-4-0', 'mode': 'merge', 'target': 10, 'force': True})
    assert r.status_code == 409
    assert 'node 2' in r.json()['detail']
    assert client.app.state.ctx.store.revision() == revision


def test_split_allocates_above_displayed_ids_not_only_original_ids(client):
    snapshot(client, [(1, 2, 10), (2, 3, 10), (5, 6, 21), (6, 7, 20)],
             [(10, 10, 4), (20, 20, 4), (21, 20, 4)])
    r = client.post('/api/streets/assign', json={'ref': '1-2-0', 'mode': 'split', 'node': 2})
    assert r.status_code == 200
    assert r.json()['id'] > 21
    assert r.json()['id'] in client.app.state.ctx.store.project().roads['no_cut']


def test_split_does_not_reuse_a_retired_branch_or_named_id(client):
    snapshot(client, [(1, 2, 10), (2, 3, 10)], [(10, 10, 4)])
    client.app.state.ctx.store.put_computed('branch_ids', {'old-branch': 80})
    client.app.state.ctx.store.set_name(100, 'Historical street')
    r = client.post('/api/streets/assign', json={'ref': '1-2-0', 'mode': 'split', 'node': 2})
    assert r.status_code == 200
    assert r.json()['id'] > 100


def test_loop_with_two_roads_at_node_has_no_meaningful_split(client):
    snapshot(client, [(1, 2, 10), (2, 3, 10), (1, 3, 10)], [(10, 10, 4)])
    revision = client.app.state.ctx.store.revision()
    r = client.post('/api/streets/assign', json={'ref': '1-2-0', 'mode': 'split', 'node': 2})
    assert r.status_code == 409
    assert 'whole street' in r.json()['detail']
    assert client.app.state.ctx.store.revision() == revision


def test_new_split_id_can_be_merged_before_recomputing(client):
    snapshot(client, [(1, 2, 10), (2, 3, 10), (3, 4, 20)], [(10, 10, 4), (20, 20, 4)])
    r = client.post('/api/streets/assign', json={'ref': '2-3-0', 'mode': 'split', 'node': 2})
    assert r.status_code == 200
    new_id = r.json()['id']
    r = client.post('/api/streets/assign', json={'ref': '3-4-0', 'mode': 'merge', 'target': new_id})
    assert r.status_code == 200
    assert client.app.state.ctx.store.project().roads['edge_street']['2-3-0'] == new_id
    assert client.app.state.ctx.store.project().roads['edge_street']['3-4-0'] == new_id


def test_split_requires_clicked_road_to_touch_the_selected_node(client):
    snapshot(client, [(1, 2, 10), (2, 3, 10), (3, 4, 10)], [(10, 10, 4)])
    r = client.post('/api/streets/assign', json={'ref': '3-4-0', 'mode': 'split', 'node': 2})
    assert r.status_code == 409


def test_pending_rank_changes_are_checked_before_merge(client):
    snapshot(client, [(1, 2, 10), (2, 3, 20)], [(10, 10, 4), (20, 20, 4)])
    client.app.state.ctx.store.set_street_attrs({'20': {'rank': 3}})
    r = client.post('/api/streets/assign', json={'ref': '1-2-0', 'mode': 'merge', 'target': 20})
    assert r.status_code == 409
    assert 'rank' in r.json()['detail']


def test_pending_merge_does_not_resurrect_a_retired_target(client):
    snapshot(client, [(1, 2, 10), (2, 3, 20)], [(10, 10, 4), (20, 20, 4)])
    assert client.post('/api/streets/assign', json={'ref': '1-2-0', 'mode': 'merge', 'target': 20}).status_code == 200
    revision = client.app.state.ctx.store.revision()
    r = client.post('/api/streets/assign', json={'ref': '2-3-0', 'mode': 'merge', 'target': 10})
    assert r.status_code == 404
    assert client.app.state.ctx.store.revision() == revision
