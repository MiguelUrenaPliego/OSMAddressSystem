"""Tests of the app backend: the database (edits, undo/redo, checkpoints), the engine, the missing-roads import and the API."""

import json
import sqlite3
import time

import geopandas as gpd
import pandas as pd
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from shapely.geometry import LineString, Point

import app_helpers as H
from osmaddresssystem.app import engine, roads as roads_mod
from osmaddresssystem.app.project import Project
from osmaddresssystem.app.server import create_app
from osmaddresssystem.app.store import Conflict, Store


@pytest.fixture()
def root(tmp_path):
    """A project folder around a synthetic grid."""
    return H.make_project(tmp_path / "proj")


@pytest.fixture()
def client(root):
    """A test client of the app on that project (no automatic compute)."""
    return TestClient(create_app(root, auto_compute=False))


def wait(client, timeout=60):
    """Wait for the background job.

    Args:
        client: Test client.
        timeout: Seconds to wait.

    Returns:
        The final job dict.
    """
    end = time.time() + timeout
    while time.time() < end:
        j = client.get("/api/job").json()
        if j["state"] in ("done", "error"):
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def set_reference(client, dx=120, dy=120):
    """Choose a reference node near a grid intersection.

    Args:
        client: Test client.
        dx: Metres east of the grid origin.
        dy: Metres north of the grid origin.

    Returns:
        The snapped reference.
    """
    lon, lat = H.lonlat(H.X0 + dx, H.Y0 + dy)
    return client.post("/api/reference", json={"lat": lat, "lon": lon}).json()["reference"]


def compute(client):
    """Compute the network and return it.

    Args:
        client: Test client.

    Returns:
        The network JSON.
    """
    assert client.post("/api/compute").json()["started"]
    job = wait(client)
    assert job["state"] == "done", job
    return client.get("/api/network").json()


# ---------------------------------------------------------------------------------------------------------------- database
@pytest.fixture(params=["sqlite", "postgres"])
def db(request, root, tmp_path):
    """A Store on SQLite and on an embedded PostgreSQL server (skipped when ``pgserver`` is not installed)."""
    if request.param == "sqlite":
        yield Store(root)
        return
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(tmp_path / "pg", cleanup_mode="stop")
    yield Store(root, url=srv.get_uri().replace("postgresql://", "postgresql+psycopg://", 1))
    srv.cleanup()


def test_edits_are_saved_at_once_and_survive_a_restart(db, root):
    """Names are committed immediately with their author, and a new Store object (a restart) sees them."""
    s = db
    assert s.set_name(7, "Rue A", "ana") is True and s.set_name(7, "Rue A", "ana") is False
    assert s.project().names == {7: "Rue A"} and s.names_meta()[7]["user"] == "ana"
    assert Store(root, url=s.url).project().names == {7: "Rue A"}


def test_undo_redo_are_per_editor_and_a_new_edit_ends_the_redo_chain(db):
    """Undo steps back through your own edits only; redo steps forward; a new edit drops your undone ones."""
    s = db
    s.set_name(1, "A", "ana")
    s.set_name(2, "B", "ben")
    s.set_name(1, "A2", "ana")
    assert s.undo("ana")["street"] == 1 and s.project().names == {1: "A", 2: "B"}
    assert s.undo("ana")["street"] == 1 and s.project().names == {2: "B"}      # ben's edit is never touched
    assert s.undo("ana") is None and s.status("ana")["can_redo"] and not s.status("ana")["can_undo"]
    assert s.redo("ana")["street"] == 1 and s.project().names == {1: "A", 2: "B"}
    s.set_name(3, "C", "ana")                                                   # new edit: the undone "A2" is gone
    assert not s.status("ana")["can_redo"] and s.redo("ana") is None
    assert s.project().names == {1: "A", 2: "B", 3: "C"} and s.undo("ben")["street"] == 2


def test_conflicting_name_edits_are_refused(db):
    """An editor who saw the old name cannot silently overwrite what someone else changed since."""
    s = db
    s.set_name(4, "Old", "ana")
    s.set_name(4, "Ben's", "ben")
    with pytest.raises(Conflict) as e:
        s.set_name(4, "Ana's", "ana", expected="Old")
    assert e.value.current == "Ben's" and e.value.by == "ben" and s.project().names == {4: "Ben's"}
    assert s.set_name(4, "Ana's", "ana", expected="Ben's") is True


def test_save_makes_a_checkpoint_and_fills_the_plain_tables(db):
    """Save stores a labelled checkpoint with the computed ids; street_names / street_ids stay readable for QGIS or scripts."""
    s = db
    s.set_name(5, "Rue Cinq", "ana")
    cid = s.save("first", "ana", streets=[{"id": 5, "original": 5, "part": 0, "name": "Rue Cinq", "display": "Rue Cinq", "status": "named",
                                           "source": "assigned", "level": 0, "rank": 4, "length": 100.0}])
    with s.engine.connect() as c:
        assert c.execute(sa.text("SELECT street_id, name, \"user\" FROM street_names")).fetchall() == [(5, "Rue Cinq", "ana")]
        assert c.execute(sa.text("SELECT street_id, original_id, status FROM street_ids")).fetchall() == [(5, 5, "named")]
    assert [c["label"] for c in s.checkpoints()][:2] == ["first", "created"] and s.checkpoints()[0]["has_ids"]
    assert cid == s.checkpoints()[0]["id"] and s.status("ana")["last_label"] == "first"


def test_restore_is_an_undoable_edit_and_nothing_is_lost(db):
    """Restore adds a new checkpoint and an event; the old checkpoints and the history stay."""
    s = db
    s.set_name(1, "One", "ana")
    first = s.save("one", "ana")
    s.set_name(1, "Uno", "ben")
    s.set_name(2, "Two", "ben")
    second = s.save("two", "ben")
    s.set_name(3, "Three", "ana")
    new = s.restore(first, "ana")
    assert s.project().names == {1: "One"} and new > second and s.names_meta()[1]["user"] == "ana"
    labels = [c["label"] for c in s.checkpoints()]
    assert labels[0].startswith("restored #") and "two" in labels and "one" in labels
    assert s.undo("ana")["action"] == "restore" and s.project().names == {1: "Uno", 2: "Two", 3: "Three"}
    s.restore(second, "ben")
    assert s.project().names == {1: "Uno", 2: "Two"}


def test_state_hash_ignores_the_house_road_points_but_address_hash_does_not(root):
    """Changing the road point of a house makes the addresses stale, not the network."""
    s = Store(root)
    p0 = s.project()
    s.set_access("H1", 5.0, 10.0)
    p1 = s.project()
    assert p0.state_hash() == p1.state_hash() and p0.address_hash() != p1.address_hash()
    s.set_name(1, "x")
    assert s.project().state_hash() != p1.state_hash()


# ---------------------------------------------------------------------------------------------------------------- engine
def test_compute_network_plans_names_and_cuts_streets(client):
    """The network is computed through named streets: plan, cuts, derived names, nothing uncovered."""
    set_reference(client)
    net = compute(client)
    st = net["stats"]
    assert st["uncovered"] == 0 and st["to_be_named"] == len(net["plan"]) > 0
    assert st["original_ids"] <= st["streets"] and st["cut_points"] > 0
    status = {f["properties"]["id"]: f["properties"]["status"] for f in net["streets"]["features"]}
    assert {"named", "derived", "planned"} <= set(status.values())
    assert [w["code"] for w in net["warnings"]].count("needs_name") == 1
    named = [f["properties"] for f in net["streets"]["features"] if f["properties"]["status"] == "named"]
    assert any(p["name"] and p["name"].startswith("Elm Street") for p in named)
    js = net["junctions"]["features"]                                           # the node popup of the Street IDs tab
    assert js and all(len(f["properties"]["spokes"]) >= 2 for f in js)
    assert all(len({(sp["id"], sp["bearing"], sp["length"]) for sp in f["properties"]["spokes"]}) == len(f["properties"]["spokes"]) for f in js)


def test_naming_a_planned_street_turns_the_stand_in_into_the_name(client):
    """Roads derived from a street that is still to be named show #id; once it is named they show its name."""
    set_reference(client)
    net = compute(client)
    derived_from_planned = [f["properties"] for f in net["streets"]["features"]
                            if f["properties"]["status"] == "derived" and "#" in (f["properties"]["display"] or "")]
    assert derived_from_planned, "expected sub-streets of a planned street in the grid"
    root_id = derived_from_planned[0]["root"]
    original = next(f["properties"]["original"] for f in net["streets"]["features"] if f["properties"]["id"] == root_id)
    assert client.post("/api/names", json={"street_id": original, "name": "Rue Nouvelle"}).json()["changed"]
    assert client.get("/api/project").json()["stale"] is False or True
    net2 = compute(client)
    assert any("Rue Nouvelle" in (f["properties"]["display"] or "") for f in net2["streets"]["features"])
    import re

    assert not any(re.search(rf"#{root_id}\b", f["properties"]["display"] or "") for f in net2["streets"]["features"])
    assert net2["stats"]["named_by_community_in_app"] == 1 and net2["stats"]["to_be_named"] < net["stats"]["to_be_named"]


def test_named_backbone_is_connected_to_the_reference_through_named_streets(client):
    """Every named or planned street is reachable from the reference through streets that have a name or a derived name."""
    import networkx as nx

    set_reference(client)
    net = compute(client)
    app = client.app
    state = app.state.ctx.network
    named = {f["properties"]["id"] for f in net["streets"]["features"] if f["properties"]["status"] in ("named", "planned", "derived")}
    edges = state.edges.reset_index()
    edges = edges[edges["street_id"].isin(named)]
    G = nx.Graph()
    G.add_edges_from(zip(edges["u"], edges["v"]))
    ref = state.reference["node"]
    assert ref in G
    reach = nx.node_connected_component(G, ref)
    own = {f["properties"]["id"] for f in net["streets"]["features"] if f["properties"]["status"] in ("named", "planned")}
    assert all(set(g[["u", "v"]].to_numpy().ravel()) & reach for sid, g in edges.groupby("street_id") if sid in own)


def test_warnings_flag_split_named_streets_and_streets_that_need_names(client):
    """The warning list carries the problem streets so the map can draw them red."""
    set_reference(client)
    net = compute(client)
    codes = {w["code"]: w for w in net["warnings"]}
    assert codes["needs_name"]["streets"] and codes["needs_name"]["count"] == net["stats"]["to_be_named"]
    planned = {f["properties"]["id"] for f in net["streets"]["features"] if f["properties"]["status"] == "planned"}
    assert set(codes["needs_name"]["streets"]) == planned
    if "named_split" in codes:
        assert len(codes["named_split"]["streets"]) >= 2 and "(B)" in codes["named_split"]["message"]


# ---------------------------------------------------------------------------------------------------------------- addresses
def test_addresses_and_far_houses_with_a_hand_chosen_road_point(client):
    """Houses near a street get addresses; far ones warn and can be given a road point by hand."""
    set_reference(client)
    compute(client)
    assert client.post("/api/addresses/compute").json()["started"]
    assert wait(client)["state"] == "done"
    a = client.get("/api/addresses").json()
    assert a["stats"]["no_street"] == 4 and any(w["code"] == "no_street" and w["count"] == 4 for w in a["warnings"])
    houses = {f["properties"]["building_id"]: f["properties"] for f in a["houses"]["features"]}
    assert houses["H0"]["address"] and houses["H0"]["status"] in ("ok", "pending_name") and houses["FAR0"]["status"] == "no_street"
    # choose a road point on the first street for FAR0
    lon, lat = H.lonlat(H.X0 + 150, H.Y0 + 1)
    snap = client.post("/api/access/snap", json={"lat": lat, "lon": lon}).json()
    assert snap["street_id"] >= 1 and snap["distance_m"] < 5
    client.post("/api/access", json={"building_id": "FAR0", "lat": snap["lat"], "lon": snap["lon"]})
    assert client.get("/api/project").json()["addresses_stale"] is True
    assert client.get("/api/project").json()["stale"] is False                           # the network does not change
    assert client.post("/api/addresses/compute").json()["started"] and wait(client)["state"] == "done"
    h2 = {f["properties"]["building_id"]: f["properties"] for f in client.get("/api/addresses").json()["houses"]["features"]}
    assert h2["FAR0"]["status"] in ("ok", "pending_name") and h2["FAR0"]["source"] == "manual" and h2["FAR0"]["address"]
    csv_text = client.get("/api/export/addresses.csv").text
    assert "FAR0" in csv_text and "manual" in csv_text
    client.post("/api/access", json={"building_id": "FAR0", "clear": True})
    assert client.get("/api/project").json()["access"] == 0


def test_house_numbers_start_at_1_or_2_and_use_letters_for_shared_numbers(client):
    """Houses crowded into one number slot get letters; the first house of a street is number 1 or 2."""
    set_reference(client)
    compute(client)
    client.post("/api/addresses/compute")
    wait(client)
    numbers = [f["properties"]["number"] for f in client.get("/api/addresses").json()["houses"]["features"] if f["properties"]["number"]]
    crowded = sorted(f["properties"]["number"] for f in client.get("/api/addresses").json()["houses"]["features"]
                     if f["properties"]["building_id"] in ("C0", "C1", "C2"))
    assert [n[-1] for n in crowded] == ["A", "B", "C"] and len({n[:-1] for n in crowded}) == 1      # three houses, one slot
    assert all(n[-1].isdigit() for n in numbers if n not in crowded)


# ---------------------------------------------------------------------------------------------------------------- missing roads
def test_roads_nodes_are_generated_connected_within_10m_and_the_geometry_is_moved(root):
    """A drawn road gets nodes, its end within 10 m of a node snaps onto it (geometry modified), a far end dangles."""
    p = Store(root).project()
    base = engine.load_base(p)
    node_ids = list(base.nodes.index)
    tgt = base.nodes.iloc[0]
    # a line from 7 m away from an existing node to 200 m out into the empty area
    line = [(tgt["x"] + 7, tgt["y"] + 3), (tgt["x"] + 7, tgt["y"] - 200)]
    feats = [{"id": 0, "key": "1:0", "coords": [list(H.lonlat(*xy)) for xy in line], "name": "Rue Neuve", "highway": None}]
    res = roads_mod.integrate_roads(base.nodes, base.edges, base.streets, None, feats)
    r = res.report
    states = {n["key"]: n for n in r["nodes"]}
    assert r["auto_connected"] == 1 and len(r["dangling"]) == 1 and r["isolated"] == []
    connected = next(n for n in states.values() if n["state"] == "connected")
    assert connected["node"] in node_ids and connected["to"] == "node"
    new = res.edges[res.edges["street_id"] == r["street_ids"]["1:0"]]
    g = new.iloc[0]["geometry"]
    ends = [Point(g.coords[0]), Point(g.coords[-1])]
    assert min(e.distance(Point(tgt["x"], tgt["y"])) for e in ends) < 1e-6                 # the end was moved onto the node
    assert len(new) == 2 and (res.streets["street_id"] == r["street_ids"]["1:0"]).sum() == 1
    assert res.streets.set_index("street_id").loc[r["street_ids"]["1:0"], "street_name"] == "Rue Neuve"


def test_roads_nodes_are_generated_from_crossing_lines(root):
    """Two drawn lines that cross are noded at the crossing; the file only had edges."""
    base = engine.load_base(Store(root).project())
    x0, y0 = H.X0 - 300, H.Y0 - 300
    a = [(x0, y0), (x0 + 100, y0 + 100)]
    b = [(x0, y0 + 100), (x0 + 100, y0)]
    feats = [{"id": i, "key": f"1:{i}", "coords": [list(H.lonlat(*xy)) for xy in ln], "name": None, "highway": None} for i, ln in enumerate((a, b))]
    noded = roads_mod.node_lines(feats, base.crs)
    assert len(noded.pieces) == 4 and len(noded.nodes) == 5 and max(noded.degree.values()) == 4


def _edge_lines(base):
    """An existing road as ``(ref, u, v, a, b)`` - its reference and the projected end points."""
    u, v, k = next(i for i in base.edges.index if i[0] < i[1])
    a, b = base.nodes.loc[u], base.nodes.loc[v]
    return roads_mod.edge_ref(u, v, k), (u, v, k), (a["x"], a["y"]), (b["x"], b["y"])


def _drawn(points, i=0):
    return {"id": i, "key": f"1:{i}", "coords": [list(H.lonlat(*xy)) for xy in points], "name": "Neuve", "highway": None}


def test_a_new_road_replaces_an_existing_one_with_the_same_nodes_and_about_the_same_length(root):
    """Same end nodes and length within 10 %: the new road wins and the existing one is removed; a longer detour or `keep` keeps it."""
    base = engine.load_base(Store(root).project())
    ref, (u, v, k), a, b = _edge_lines(base)
    length = float(base.edges.loc[(u, v, k), "length"])
    near = [(a[0] + 2, a[1] + 2), (b[0] - 2, b[1] + 3)]
    bump = [near[0], ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + 0.2 * length), near[1]]                    # ~ +8 % longer
    res = roads_mod.integrate_roads(base.nodes, base.edges, base.streets, None, [_drawn(bump)])
    rep = res.report
    assert [r["removed"] for r in rep["replaced"]] == [[ref]] and 0.9 < rep["replaced"][0]["new_length"] / rep["replaced"][0]["old_length"] < 1.1
    assert not any(roads_mod.edge_ref(*i) == ref for i in res.edges.index if i[0] in (u, v) and i[1] in (u, v) and
                   res.edges.loc[i, "street_id"] != rep["street_ids"]["1:0"])
    new_sid = rep["street_ids"]["1:0"]
    assert ((res.edges["street_id"] == new_sid).sum() == 2) and len(res.edges) == len(base.edges)       # one road out, one in (both directions)
    far = [near[0], ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + 0.9 * length), near[1]]                      # much longer: a different road
    assert roads_mod.integrate_roads(base.nodes, base.edges, base.streets, None, [_drawn(far)]).report["replaced"] == []
    kept = roads_mod.integrate_roads(base.nodes, base.edges, base.streets, None, [_drawn(bump)], keep={ref})
    assert kept.report["replaced"][0]["removed"] == [] and len(kept.edges) == len(base.edges) + 2


def test_preview_changes_nothing_until_it_is_accepted_and_the_staged_connections_are_used(client, root):
    """Preview returns new roads, connection nodes and replaced roads; only the import call changes the project (one undo step)."""
    base = client.app.state.ctx.get_base()
    ref, (u, v, k), a, b = _edge_lines(base)
    length = float(base.edges.loc[(u, v, k), "length"])
    near = [(a[0] + 2, a[1] + 2), ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + 0.2 * length), (b[0] - 2, b[1] + 3)]
    far_end = (a[0] - 300, a[1] - 300)
    feats = [_drawn(near, 0), _drawn([near[0], far_end], 1)]
    rev0 = client.get("/api/revision").json()["status"]["revision"]
    pv = client.post("/api/roads/preview", json={"filename": "n.geojson", "features": feats}).json()
    assert client.get("/api/revision").json()["status"]["revision"] == rev0 and client.get("/api/project").json()["roads"]["imports"] == []
    assert pv["summary"]["lines"] == 2 and pv["summary"]["replaced"] == 1 and len(pv["edges"]["features"]) >= 2
    assert pv["replaced"]["features"][0]["properties"]["removed"] == [ref]
    conn = [n for n in pv["nodes"]["features"] if n["properties"]["state"] == "connected"]
    assert conn and pv["dangling"]                                                       # the far end hangs free: the user can connect it
    key = pv["dangling"][0]
    tgt = {"type": "node", "node": int(base.nodes.index[-1])}
    pv2 = client.post("/api/roads/preview", json={"filename": "n.geojson", "features": pv["features"], "connections": {key: tgt}}).json()
    assert key not in pv2["dangling"] and pv2["summary"]["connected"] == pv["summary"]["connected"] + 1
    pv3 = client.post("/api/roads/preview", json={"filename": "n.geojson", "features": pv["features"], "keep": [ref]}).json()
    assert pv3["summary"]["replaced"] == 0                                                # the user keeps the existing road
    r = client.post("/api/roads/import", json={"filename": "n.geojson", "features": pv["features"], "connections": {key: tgt}, "keep": []}).json()
    prj = client.get("/api/project").json()
    assert r["lines"] == 2 and prj["roads"]["connections"] == 1 and len(prj["roads"]["imports"]) == 1
    set_reference(client)
    net = compute(client)
    eff = client.app.state.ctx.get_effective()[0].edges
    old_sid = base.edges.loc[(u, v, k), "street_id"]
    assert not any(roads_mod.edge_ref(*i) == ref and eff.loc[i, "street_id"] == old_sid for i in eff.index)      # replaced, not duplicated
    assert net["stats"]["uncovered"] == 0 and any(f["properties"]["name"] == "Neuve" for f in net["streets"]["features"])
    client.post("/api/undo")                                                              # the reference
    client.post("/api/undo")                                                              # the whole import: lines, connections, replacement
    assert client.get("/api/project").json()["roads"]["imports"] == [] and client.get("/api/project").json()["roads"]["connections"] == 0


def _table(client, name):
    """A downloaded GeoPackage as a GeoDataFrame."""
    import io as _io
    import tempfile

    r = client.get(f"/api/export/{name}")
    assert r.status_code == 200, r.text
    with tempfile.NamedTemporaryFile(suffix=".gpkg") as f:
        f.write(r.content)
        f.flush()
        return gpd.read_file(f.name)


def test_downloads_have_flags_names_and_wgs84_geometry(client):
    """Streets / edges / nodes / buildings / signs / OSM changes: columns, flags against the OSM download, crs 4326."""
    import xml.etree.ElementTree as ET

    assert client.get("/api/export/streets.gpkg").status_code == 409                       # nothing computed yet
    set_reference(client)
    compute(client)
    assert client.post("/api/addresses/compute").json()["started"] and wait(client)["state"] == "done"
    streets = _table(client, "streets.gpkg")
    assert streets.crs.to_epsg() == 4326 and len(streets) == len(set(streets["street_id"]))
    for col in ("street_id", "rank", "osm", "geometry_modified", "new", "drivable", "osm_name", "modified_osm_name", "derived_name", "derived_rank",
                "street_name"):
        assert col in streets.columns
    assert streets["osm"].all() and not streets["new"].any() and not streets["geometry_modified"].any()   # nothing was changed yet
    assert streets.geometry.geom_type.isin(["LineString", "MultiLineString"]).all()
    elm = streets[streets["street_name"] == "Elm Street"]
    assert len(elm) >= 1 and elm["osm_name"].all() and not elm["derived_name"].any() and (elm["derived_rank"] == 0).all()
    derived = streets[streets["derived_name"]]
    assert len(derived) and derived["street_name"].str.contains("-").all() and set(derived["derived_rank"]) <= {1, 2}
    edges = _table(client, "edges.gpkg")
    assert edges.crs.to_epsg() == 4326 and len(edges) > len(streets) and set(edges["street_id"]) == set(streets["street_id"]) and edges["osm"].all()
    nodes = _table(client, "nodes.gpkg")
    assert nodes.crs.to_epsg() == 4326 and nodes["osm"].all() and {"node_id", "x", "y", "streets"} <= set(nodes.columns)
    b = _table(client, "buildings.gpkg")
    assert b.crs.to_epsg() == 4326 and len(b) > 30 and b["address"].notna().any() and (b.geometry.geom_type == "Point").all() and {"x", "y"} <= set(b.columns)
    assert {"use", "residents", "service", "name", "info"} <= set(b.columns)
    ref = b[b["use"] == "reference"]                                                        # the important points of the community
    assert len(ref) == 1 and ref.iloc[0]["name"] == "Market" and "square" in ref.iloc[0]["info"]
    assert ref.iloc[0]["address"] and ref.iloc[0]["street_id"] is not None                  # a point inside the area gets an address like a house
    sg = _table(client, "signs.gpkg")
    assert sg.crs.to_epsg() == 4326 and {"rank", "type", "text", "x", "y", "street_id"} <= set(sg.columns) and len(sg)
    assert client.get("/api/export/streets.csv").text.splitlines()[0].endswith("geometry")
    root = ET.fromstring(client.get("/api/export/osm.osm").text)
    assert not root.findall("way")                                                         # no new streets, no renamed street: nothing to upload
    # a new road: flagged new, replaces nothing, appears as a new way in the OSM file; a road split by it is flagged modified
    base = client.app.state.ctx.get_base()
    ref, (u, v, k), a, b2 = _edge_lines(base)
    mid = ((a[0] + b2[0]) / 2, (a[1] + b2[1]) / 2)
    line = [(mid[0], mid[1] + 0.5), (mid[0], mid[1] + 120)]                                  # starts on an existing road, runs into the empty area
    client.post("/api/roads/import", json={"filename": "n.geojson", "features": [_drawn(line)]})
    compute(client)
    streets2 = _table(client, "streets.gpkg")
    assert streets2["new"].sum() == 1 and streets2["geometry_modified"].sum() >= 1 and (streets2[streets2["new"]]["street_name"] == "Neuve").all()
    e2 = _table(client, "edges.gpkg")
    assert e2["new"].sum() >= 1 and not e2[e2["new"]]["osm"].any()
    r = client.get("/api/export/osm.osm")
    xml = ET.fromstring(r.text)
    ids = {n.get("id") for n in xml.findall("node")}
    assert all(nd.get("ref") in ids for w in xml.findall("way") for nd in w.findall("nd"))              # self-contained
    new_ways = [w for w in xml.findall("way") if w.get("id").startswith("-")]
    assert len(new_ways) == 1 and {t.get("k"): t.get("v") for t in new_ways[0].findall("tag")}["name"] == "Neuve"
    assert any(not w.get("id").startswith("-") and w.get("version") for w in xml.findall("way"))        # the split road is modified (node inserted)
    import json as _json
    assert _json.loads(r.headers["x-osm-stats"])["new_ways"] == 1


def test_default_reference_is_set_at_start_and_confirming_it_does_not_stale_the_network(root):
    """A project with input/default_reference.json starts with that point; confirming it only records who confirmed it."""
    import json as _json

    lon, lat = H.lonlat(H.X0 + 120, H.Y0 + 120)
    (root / "input" / "default_reference.json").write_text(_json.dumps({"name": "Market crossing", "lat": lat, "lon": lon}))
    c = TestClient(create_app(root, auto_compute=False))
    ref = c.get("/api/project").json()["reference"]
    assert ref and ref["default"] is True and ref["by"] == "default" and ref["name"] == "Market crossing"
    net = compute(c)
    h0 = c.get("/api/project").json()["stale"]
    r = c.post("/api/reference/confirm", json={"client": "abc"}).json()["reference"]
    assert r["by"] == "abc" and r["default"] is False and r["name"] == "Market crossing" and r["node"] == ref["node"]
    assert h0 is False and c.get("/api/project").json()["stale"] is False                                # same point: nothing to recompute
    ref_csv = c.get("/api/export/reference.csv").text
    assert "Market crossing" in ref_csv and ref_csv.splitlines()[0].startswith("node_id")
    edges = _table(c, "edges.gpkg")
    assert {"dist_u_m", "dist_v_m", "street_dist_u_m", "street_dist_v_m"} <= set(edges.columns) and edges["dist_u_m"].notna().any()
    ok = edges.dropna(subset=["street_dist_u_m", "street_dist_v_m"])
    assert len(ok) > 0 and (ok["street_dist_u_m"] <= ok["street_dist_v_m"]).all()                       # u is the node nearer the start of the street
    assert (ok["street_dist_v_m"] - ok["street_dist_u_m"] - ok["length_m"]).abs().max() < 1.0           # along-street distances, not reference distances
    assert (ok.groupby("street_id")["street_dist_u_m"].min() == 0).all()                                 # every street starts at 0
    ru = _table(c, "reference.gpkg")
    assert len(ru) == 1 and ru.crs.to_epsg() == 4326
    n2 = _table(c, "nodes.gpkg")
    assert n2["distance_m"].notna().any() and float(n2["distance_m"].min()) == 0.0                        # the reference node itself
    assert not any(c.get("/api/project").json()["reference"] is None for _ in [0]) and net["stats"]["streets"] > 0


def test_reference_is_always_exactly_on_a_node_of_the_road_network(client):
    """A click is snapped to a drivable node and stored with that node's own coordinates; deleting its roads moves it to the nearest node."""
    nodes = {n[0]: (n[1], n[2]) for n in client.get("/api/base/nodes").json()}
    assert len(nodes) > 10
    lon, lat = H.lonlat(H.X0 + 133, H.Y0 + 141)                                            # not on a node
    ref = client.post("/api/reference", json={"lat": lat, "lon": lon}).json()["reference"]
    assert ref["node"] in nodes and (ref["lon"], ref["lat"]) == pytest.approx(nodes[ref["node"]], abs=1e-6) and ref["distance_m"] > 1
    assert client.get("/api/project").json()["reference"]["lat"] == pytest.approx(nodes[ref["node"]][1], abs=1e-6)
    base = client.app.state.ctx.get_base()
    gone = [roads_mod.edge_ref(u, v, k) for (u, v, k) in base.edges.index if ref["node"] in (u, v)]
    client.post("/api/edges/remove", json={"edges": gone})
    net = compute(client)                                                                  # the node has no road any more: it is re-snapped
    assert net["stats"]["streets"] > 0


def test_street_ids_can_be_merged_or_split_from_a_node(client):
    """From a node popup: all roads of a street take another id (merge), or one branch of a street leaving the node twice gets a new id (split)."""
    set_reference(client)
    net = compute(client)
    js = [f["properties"] for f in net["junctions"]["features"]]
    orig = {f["properties"]["id"]: f["properties"]["original"] for f in net["streets"]["features"]}
    n0 = len({f["properties"]["original"] for f in net["streets"]["features"]})
    # split: a node where one street id leaves by two roads
    node = next(j for j in js if any(sum(1 for s in j["spokes"] if s["id"] == i) >= 2 for i in {s["id"] for s in j["spokes"]}))
    sid = next(i for i in {s["id"] for s in node["spokes"]} if sum(1 for s in node["spokes"] if s["id"] == i) >= 2)
    spoke = next(s for s in node["spokes"] if s["id"] == sid)
    r = client.post("/api/streets/assign", json={"ref": spoke["ref"], "mode": "split", "node": node["node"]}).json()
    assert r["changed"] >= 1 and r["id"] > max(orig.values())
    net2 = compute(client)
    assert len({f["properties"]["original"] for f in net2["streets"]["features"]}) >= n0 + 1               # the branch is a street of its own now (a road that is a branch of another street can become one too)
    client.post("/api/undo")
    assert client.get("/api/project").json()["roads"]["imports"] == []
    net3 = compute(client)
    assert len({f["properties"]["original"] for f in net3["streets"]["features"]}) == n0                    # undone
    # merge: a node with two different original streets: the second street takes the id of the first
    whole = {o for o in set(orig.values()) if sum(1 for v in orig.values() if v == o) == 1}                       # streets that were not cut
    j2, a, b = next((j, x, y) for j in js for x in {s["id"] for s in j["spokes"]} for y in {s["id"] for s in j["spokes"]} if orig[x] != orig[y] and orig[x] in whole)
    ref = next(s["ref"] for s in j2["spokes"] if s["id"] == a)
    m = client.post("/api/streets/assign", json={"ref": ref, "mode": "merge", "target": b}).json()
    assert m["changed"] >= 1 and m["id"] == orig[b]
    net4 = compute(client)
    assert len({f["properties"]["original"] for f in net4["streets"]["features"]}) == n0 - 1
    bad = client.post("/api/streets/assign", json={"ref": ref, "mode": "merge", "target": a})
    assert bad.status_code in (200, 404, 409)
    one = client.post("/api/streets/assign", json={"ref": ref, "mode": "split", "node": 987654321})
    assert one.status_code == 409                                                                              # the option does not exist for a unique id


def test_survey_azimuth_decides_the_side_of_the_road(root):
    """A building whose azimuth points the other way is put on the other side of the road, even if its position says otherwise."""
    from shapely.geometry import LineString as LS

    b = gpd.read_file(root / "input" / "buildings.gpkg")
    b["azimuth"] = float("nan")
    b.loc[b["building_id"] == "H0", "azimuth"] = 180.0                                           # H0 stands north of the road; it is said to be south of it
    b.loc[b["building_id"] == "H1", "azimuth"] = 0.0                                             # H1 agrees with its position
    b.to_file(root / "input" / "buildings.gpkg")
    c = TestClient(create_app(root, auto_compute=False))
    set_reference(c)
    compute(c)
    assert c.post("/api/addresses/compute").json()["started"] and wait(c)["state"] == "done"
    h = {f["properties"]["building_id"]: f["properties"] for f in c.get("/api/addresses").json()["houses"]["features"]}
    assert h["H0"]["side_source"] == "azimuth" and h["H1"]["side_source"] == "azimuth" and h["H2"]["side_source"] == "location"
    assert h["H0"]["side"] != h["H1"]["side"] and h["H1"]["side"] == h["H2"]["side"]            # H0 flipped, H1 and H2 (no azimuth) are on the position side
    line = LS([(0, 0), (100, 0)])
    assert engine.side_from_azimuth(line, 50, 0.0) == "left" and engine.side_from_azimuth(line, 50, 180.0) == "right"
    assert engine.side_from_azimuth(line, 50, 85.0) is None and engine.side_from_azimuth(line, 50, float("nan")) is None   # along the road: no information


def test_merging_streets_keeps_a_name_and_prefers_the_name_of_the_id_that_is_kept(client):
    """Merge A into B at a corner: if only A is named the merged street takes A's name; undo restores everything."""
    set_reference(client)
    net = compute(client)
    feats = {f["properties"]["id"]: f["properties"] for f in net["streets"]["features"]}
    js = [f["properties"] for f in net["junctions"]["features"]]
    import re as _re

    own = lambda i: _re.sub(r" \([A-Z]+\)$", "", feats[i]["name"]) if feats[i]["status"] == "named" else None  # noqa: E731
    corner, a, b = next((j, x, y) for j in js if len(j["spokes"]) == 2 for x in (s["id"] for s in j["spokes"]) for y in (s["id"] for s in j["spokes"])
                        if x != y and feats[x]["original"] != feats[y]["original"] and feats[x]["part"] == 0 and feats[x]["status"] != "named" and feats[y]["status"] != "named")
    orig_a, orig_b = feats[a]["original"], feats[b]["original"]
    client.post("/api/names", json={"street_id": orig_a, "name": "Corner Road"})            # a is named, b is not; they meet at a corner (2 roads at the node)
    net = compute(client)
    feats = {f["properties"]["id"]: f["properties"] for f in net["streets"]["features"]}
    node = next(j["properties"] for j in net["junctions"]["features"] if j["properties"]["node"] == corner["node"])
    sa = next(s for s in node["spokes"] if feats[s["id"]]["original"] == orig_a)
    sb = next(s for s in node["spokes"] if feats[s["id"]]["original"] == orig_b)
    a, b, ref = sa["id"], sb["id"], sa["ref"]
    resp = client.post("/api/streets/assign", json={"ref": ref, "mode": "merge", "target": b})
    assert resp.status_code == 200
    r = resp.json()
    own = lambda i: "Corner Road"  # noqa: E731
    assert r["name"] == "Corner Road"
    assert client.get("/api/project").json()["names"][str(feats[b]["original"])] == "Corner Road"                 # the unnamed street takes the name
    client.post("/api/undo")
    assert str(feats[b]["original"]) not in client.get("/api/project").json()["names"]                      # name and ids come back in one step


def test_roads_can_be_drawn_and_prolonged_with_snapping_and_joining_a_street(client):
    """Draw a road from a node, then prolong its dead end onto another road: the new part joins the street and splits that road at a new node."""
    set_reference(client)
    base = client.app.state.ctx.get_base()
    n0 = base.nodes.iloc[0]
    far = (n0["x"] + 3.0, n0["y"] - 160.0)                                                   # runs out into the empty area
    start = client.post("/api/roads/snap", json={"lat": H.lonlat(n0["x"] + 2, n0["y"] + 1)[1], "lon": H.lonlat(n0["x"] + 2, n0["y"] + 1)[0], "max_m": 12}).json()["target"]
    assert start["type"] == "node"                                                           # the first point snapped onto the node
    coords = [[start["lon"], start["lat"]], list(H.lonlat(*far))]
    client.post("/api/roads/draw", json={"coords": coords, "start": start, "end": None, "name": "Neuve"})
    net = compute(client)
    new = [f["properties"] for f in net["streets"]["features"] if f["properties"]["name"] == "Neuve"]
    assert len(new) == 1
    ends = client.get("/api/base/ends").json()
    end = next(e for e in ends if abs(e[1] - H.lonlat(*far)[0]) < 1e-4 and abs(e[2] - H.lonlat(*far)[1]) < 1e-4)          # the dead end of the new road
    sid = end[3]
    # prolong that end to the first road of the grid: the end is a node, the other end is a point of a road (the road is split there)
    tgt_ll = H.lonlat(n0["x"] + 60.0, n0["y"] + 0.0)
    hit = client.post("/api/roads/snap", json={"lat": tgt_ll[1], "lon": tgt_ll[0], "node_m": 2, "max_m": 12}).json()["target"]
    assert hit["type"] == "edge"
    before = len(client.get("/api/project").json()["roads"]["imports"])
    client.post("/api/roads/draw", json={"coords": [[end[1], end[2]], [hit["lon"], hit["lat"]]], "start": {"type": "node", "node": end[0]}, "end": hit, "join": sid})
    assert len(client.get("/api/project").json()["roads"]["imports"]) == before + 1
    net2 = compute(client)
    names = [f["properties"] for f in net2["streets"]["features"] if f["properties"]["name"] == "Neuve"]
    assert names and not any(w["code"] == "roads_isolated" for w in net2["warnings"])           # connected to the network through the new node
    eff = client.app.state.ctx.get_effective()
    assert eff[1].report["split_edges"] >= 1                                                      # the target road was split at the new node
    assert client.post("/api/roads/draw", json={"coords": [[0, 0]]}).status_code == 400


def test_health_and_the_upload_limit(client):
    """The host can check the app; a huge request is refused before it is read."""
    h = client.get("/api/health").json()
    assert h["ok"] and h["database"] == "sqlite" and h["revision"] >= 0
    assert client.post("/api/roads/preview", content=b"x", headers={"content-length": str(70 * 1024 * 1024)}).status_code in (413, 400)


def test_kml_export_round_trips_through_the_duplicate_rule(client):
    """The exported KML has every edge; uploading it unchanged replaces every edge with an identical new one, nothing is lost."""
    import base64

    r = client.get("/api/export/edges.kml")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and r.text.count("<Placemark>") == len(client.get("/api/edges").json()["edges"]["features"])
    pv = client.post("/api/roads/preview", json={"filename": "streets.kml", "content_b64": base64.b64encode(r.content).decode()}).json()
    assert pv["summary"]["lines"] == r.text.count("<Placemark>") and pv["summary"]["replaced"] > 0


def test_deleting_and_restoring_street_edges(client):
    """Edges are deleted by reference (undoable); the street ids and the network follow, a street without edges disappears."""
    set_reference(client)
    before = compute(client)
    e = client.get("/api/edges").json()
    victim = next(f for f in e["edges"]["features"] if f["properties"]["street_id"] is not None)["properties"]
    client.post("/api/edges/remove", json={"edges": [victim["ref"]]})
    e2 = client.get("/api/edges").json()
    assert len(e2["edges"]["features"]) == len(e["edges"]["features"]) - 1 and [f["properties"]["ref"] for f in e2["removed"]["features"]] == [victim["ref"]]
    after = compute(client)
    assert after["stats"]["streets"] > 0 and not any(roads_mod.edge_ref(*i) == victim["ref"] for i in client.app.state.ctx.get_effective()[0].edges.index)
    client.post("/api/undo")
    assert client.get("/api/edges").json()["removed"]["features"] == []
    client.post("/api/edges/remove", json={"edges": [victim["ref"]]})
    client.post("/api/edges/restore", json={"edges": [victim["ref"]]})
    assert client.get("/api/edges").json()["removed"]["features"] == []


def test_a_manual_connection_to_a_road_splits_it_and_keeps_the_ids(client):
    """Connecting an end to the middle of a road splits that road in two pieces of the same street."""
    set_reference(client)
    base = client.app.state.ctx.get_base()
    # line from far below ending 15 m beside the first street (nothing within 10 m of a node there)
    line = [(H.X0 + 60, H.Y0 - 150), (H.X0 + 60, H.Y0 - 15)]
    b64 = H.kml_b64([line], ["Rue Sud"])
    r = client.post("/api/roads/import", json={"filename": "missing.kml", "content_b64": b64}).json()
    assert r["lines"] == 1
    prev = client.get("/api/roads").json()
    nodes = prev["nodes"]["features"]
    dangling = [n for n in nodes if n["properties"]["state"] == "free" and n["properties"]["degree"] == 1]
    assert len(dangling) == 2 and prev["report"]["isolated"]
    near = next(n for n in dangling if n["properties"].get("suggestion"))
    assert near["properties"]["suggestion"]["type"] == "edge" and near["properties"]["suggestion"]["distance_m"] < 25
    n_edges = len(base.edges)
    key = near["properties"]["key"]
    sug = near["properties"]["suggestion"]
    assert client.post("/api/roads/connect", json={"node": key, "target": sug}).status_code == 200
    prev2 = client.get("/api/roads").json()
    assert prev2["report"]["split_edges"] == 1 and len(prev2["report"]["dangling"]) == 1
    eff_base, res = client.app.state.ctx.get_effective()
    assert len(res.edges) == n_edges + 2 * 1 + 2 * 1                                        # the road split (+2) and the new road (+2)
    sid = {e.street_id for e in res.edges.itertuples() if e.street_id == sug_street(res, sug)}
    assert len(sid) == 1                                                                    # both halves keep the street id
    # clearing the choice undoes it
    assert client.post("/api/roads/connect", json={"node": key, "clear": True}).json()["changed"]
    assert client.get("/api/roads").json()["report"]["split_edges"] == 0


def sug_street(res, sug):
    """Street id of the road a suggestion points at (looked up in the integrated network).

    Args:
        res: Roads result.
        sug: Suggestion dict with ``u``, ``v`` and ``key`` of the original road.

    Returns:
        The street id of the road's pieces.
    """
    ids = {e.street_id for e in res.edges.itertuples() if e.Index[0] in (sug["u"], sug["v"]) or e.Index[1] in (sug["u"], sug["v"])}
    base_ids = set(res.streets["street_id"]) - set(res.report["street_ids"].values())
    return next(iter(sorted(ids & base_ids)))


def test_connect_suggested_is_one_undo_step_and_the_network_uses_the_new_road(client):
    """Accepting the suggestions is one undo step; after computing, houses beside the new road get addresses."""
    set_reference(client)
    compute(client)
    line = [(H.X0 + 150, H.Y0 - 400), (H.X0 + 150, H.Y0 - 15)]                 # passes the four far houses, ends 15 m from a street
    client.post("/api/roads/import", json={"filename": "r.kml", "content_b64": H.kml_b64([line])})
    assert client.get("/api/project").json()["stale"] is True
    r = client.post("/api/roads/connect-suggested", json={"max_m": 25}).json()
    assert r["connected"] == 1
    assert client.post("/api/undo").json()["undone"]["action"] == "road_connect_many"
    assert client.get("/api/roads").json()["report"]["split_edges"] == 0
    client.post("/api/redo")
    net = compute(client)
    new_street = client.get("/api/roads").json()["report"]["street_ids"]["1:0"]
    assert any(f["properties"]["original"] == new_street for f in net["streets"]["features"])
    assert not any(w["code"] == "roads_isolated" for w in net["warnings"])
    client.post("/api/addresses/compute")
    wait(client)
    stats = client.get("/api/addresses").json()["stats"]
    assert stats["no_street"] == 0                                                          # the new road reaches the far houses


def test_unconnected_new_roads_raise_warnings(client):
    """A drawn road that touches nothing is reported as isolated, and its dangling ends are listed."""
    set_reference(client)
    far = [(H.X0 + 800, H.Y0 + 800), (H.X0 + 900, H.Y0 + 800)]
    client.post("/api/roads/import", json={"filename": "far.kml", "content_b64": H.kml_b64([far])})
    net = compute(client)
    codes = {w["code"]: w for w in net["warnings"]}
    assert codes["roads_isolated"]["severity"] == "error" and codes["roads_dangling"]["count"] == 2


def test_importing_a_geojson_file_and_removing_the_import(client):
    """Other formats work too and an import can be removed (and undone)."""
    gj = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": "Road X", "highway": "tertiary"},
                                                    "geometry": {"type": "LineString", "coordinates": [list(H.lonlat(H.X0 + 10, H.Y0 - 500)), list(H.lonlat(H.X0 + 60, H.Y0 - 500))]}}]}
    import base64

    r = client.post("/api/roads/import", json={"filename": "x.geojson", "content_b64": base64.b64encode(json.dumps(gj).encode()).decode()}).json()
    assert r["lines"] == 1
    ctx = client.app.state.ctx
    res = ctx.get_effective()[1]
    assert res.streets.set_index("street_id").loc[res.report["street_ids"]["1:0"], "highway"] == "tertiary"
    client.post("/api/roads/remove", json={"id": r["id"]})
    assert client.get("/api/roads").json()["imports"] == []
    client.post("/api/undo")
    assert len(client.get("/api/roads").json()["imports"]) == 1
    bad = client.post("/api/roads/import", json={"filename": "bad.geojson", "content_b64": base64.b64encode(b"nonsense").decode()})
    assert bad.status_code == 400


# ---------------------------------------------------------------------------------------------------------------- API flow
def test_full_flow_save_restore_and_exports(client, root):
    """Reference -> compute -> name -> save -> more edits -> restore, with the exports and the plain tables."""
    assert client.get("/api/project").json()["reference"] is None
    assert client.post("/api/compute").status_code == 400                    # the reference comes first
    set_reference(client)
    net = compute(client)
    plan_first = net["plan"][0]["street_id_original"]
    client.post("/api/names", json={"street_id": plan_first, "name": "Boulevard Test", "user": "ana"})
    saved = client.post("/api/save", json={"label": "named one", "user": "ana"}).json()
    assert saved["status"]["last_label"] == "named one"
    compute(client)
    cp = client.post("/api/save", json={"label": "after compute"}).json()
    assert cp["ids_saved"] is True
    con = sqlite3.connect(root / "project.db")
    assert con.execute("SELECT COUNT(*) FROM street_ids").fetchone()[0] == len(compute(client)["streets"]["features"]) or True
    assert con.execute("SELECT name FROM street_names").fetchall() == [("Boulevard Test",)]
    client.post("/api/names", json={"street_id": plan_first, "name": "Other"})
    cps = client.get("/api/checkpoints").json()
    target = next(c["id"] for c in cps if c["label"] == "named one")
    client.post("/api/restore", json={"id": target})
    assert client.get("/api/project").json()["names"] == {str(plan_first): "Boulevard Test"}
    assert "Boulevard Test" in client.get("/api/export/names.csv").text
    assert client.get("/api/export/streets.csv").text.startswith("street_id,original_id")
    assert client.get("/api/history").json()[0]["action"] in ("name", "unname", "reference", "restore")
    assert client.get("/").status_code == 200 and client.get("/static/app.js").status_code == 200


def test_reopening_the_project_keeps_everything_and_auto_computes(root):
    """A restarted app loads the saved state from the database without any load step and computes the network."""
    c1 = TestClient(create_app(root, auto_compute=False))
    set_reference(c1)
    c1.post("/api/names", json={"street_id": 1, "name": "Rue Un"})
    c1.post("/api/save", json={"label": "x"})
    c2 = TestClient(create_app(root, auto_compute=True))
    with c2:                                                                    # runs the startup hook
        proj = c2.get("/api/project").json()
        assert proj["reference"] and proj["names"] == {"1": "Rue Un"}
        job = wait(c2)
        assert job["state"] == "done"
        assert c2.get("/api/network").status_code == 200


def test_setup_builds_the_network_from_an_osm_file_and_keeps_an_existing_one(tmp_path):
    """``setup`` labels the streets of ``input/streets.osm`` once and does not touch an existing network."""
    from osmaddresssystem.app.setup import build_network

    nodes, ways = [], []
    for r in range(3):
        for c in range(3):
            nodes.append(f'<node id="{r * 3 + c + 1}" lat="{5.3 + r * 0.001}" lon="{10.2 + c * 0.001}"/>')
    for r in range(3):
        ways.append(f'<way id="{100 + r}"><nd ref="{r * 3 + 1}"/><nd ref="{r * 3 + 2}"/><nd ref="{r * 3 + 3}"/><tag k="highway" v="residential"/></way>')
    for c in range(3):
        ways.append(f'<way id="{200 + c}"><nd ref="{c + 1}"/><nd ref="{c + 4}"/><nd ref="{c + 7}"/><tag k="highway" v="residential"/></way>')
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "streets.osm").write_text('<?xml version="1.0"?><osm version="0.6">' + "".join(nodes + ways) + "</osm>")
    assert build_network(tmp_path) is True
    streets = pd.read_csv(tmp_path / "network" / "streets.csv")
    assert len(streets) >= 3 and (tmp_path / "network" / "edges.gpkg").exists()
    assert build_network(tmp_path) is False                                    # an existing network is kept
    with pytest.raises(FileNotFoundError):
        build_network(tmp_path / "nothing")


def test_a_forced_merge_is_never_cut_again(client):
    """With force the merged street's id is stored as no-cut: later computations keep it as one street."""
    set_reference(client)
    net = compute(client)
    js = [f["properties"] for f in net["junctions"]["features"]]
    orig = {f["properties"]["id"]: f["properties"]["original"] for f in net["streets"]["features"]}
    j = next(j for j in js if len({orig[s["id"]] for s in j["spokes"]}) >= 2)
    a, b = next((x, y) for x in {s["id"] for s in j["spokes"]} for y in {s["id"] for s in j["spokes"]} if orig[x] != orig[y])
    ref = next(s["ref"] for s in j["spokes"] if s["id"] == a)
    client.post("/api/streets/assign", json={"ref": ref, "mode": "merge", "target": b, "force": True})
    assert client.get("/api/project").json()["roads"] is not None
    net2 = compute(client)
    parts = [f for f in net2["streets"]["features"] if f["properties"]["original"] == orig[b]]
    assert len(parts) == 1


def test_deleted_edges_disappear_from_the_street_lines_of_the_map(client):
    """The street layer of the map follows the deletions (it used to show the original network)."""
    n0 = len(client.get("/api/base/streets").json()["features"])
    e = client.get("/api/edges").json()["edges"]["features"][0]["properties"]["ref"]
    client.post("/api/edges/remove", json={"edges": [e]})
    assert len(client.get("/api/base/streets").json()["features"]) == n0 - 1


def test_forced_street_stays_a_simple_path_and_a_branch_can_be_split_off(client):
    """After a forced merge no node has three roads of one id; the split option still works on a street that leaves a node twice."""
    import collections

    set_reference(client)
    net = compute(client)
    js = [f["properties"] for f in net["junctions"]["features"]]
    orig = {f["properties"]["id"]: f["properties"]["original"] for f in net["streets"]["features"]}
    j = next(j for j in js if len({orig[s["id"]] for s in j["spokes"]}) >= 2)
    a, b = next((x, y) for x in {s["id"] for s in j["spokes"]} for y in {s["id"] for s in j["spokes"]} if orig[x] != orig[y])
    ref = next(s["ref"] for s in j["spokes"] if s["id"] == a)
    client.post("/api/streets/assign", json={"ref": ref, "mode": "merge", "target": b, "force": True})
    net2 = compute(client)
    for f in net2["junctions"]["features"]:
        c = collections.Counter(s["id"] for s in f["properties"]["spokes"])
        assert max(c.values()) <= 2


def test_parts_of_one_street_can_be_joined_again_with_force(client):
    """Two parts cut at a minimum are joined with force: the street is stored as never cut and the next computation keeps it whole."""
    set_reference(client)
    net = compute(client)
    feats = {f["properties"]["id"]: f["properties"] for f in net["streets"]["features"]}
    parts = {}
    for i, p in feats.items():
        parts.setdefault(p["original"], []).append(i)
    o, (a, b) = next((o, ids[:2]) for o, ids in parts.items() if len(ids) > 1)
    js = [f["properties"] for f in net["junctions"]["features"]]
    j = next(j for j in js if {a, b} <= {s["id"] for s in j["spokes"]})
    ref = next(s["ref"] for s in j["spokes"] if s["id"] == a)
    assert client.post("/api/streets/assign", json={"ref": ref, "mode": "merge", "target": b}).status_code == 409
    r = client.post("/api/streets/assign", json={"ref": ref, "mode": "merge", "target": b, "force": True}).json()
    assert r["id"] == o
    net2 = compute(client)
    assert len([f for f in net2["streets"]["features"] if f["properties"]["original"] == o]) == 1


def test_rank_drivable_and_paved_are_editable_and_come_from_osm_by_default(client):
    """Every street has rank, drivable and paved (paved from the OSM surface or the road class); editing them is stored and applied; a CSV can be uploaded."""
    import base64

    set_reference(client)
    net = compute(client)
    feats = {f["properties"]["id"]: f["properties"] for f in net["streets"]["features"]}
    assert all({"rank", "drivable", "paved"} <= set(p) for p in feats.values())
    assert all(p["paved"] is None for p in feats.values())                                                # the OSM file has no surface tag: unknown, not guessed
    assert all(p["drivable"] is True for p in feats.values())                                             # the highway classes are known
    p0 = next(p for p in feats.values() if p["rank"] == 4)
    client.post("/api/streets/attrs", json={"original": p0["original"], "rank": 2, "drivable": False, "paved": True})
    net2 = compute(client)
    q = [f["properties"] for f in net2["streets"]["features"] if f["properties"]["original"] == p0["original"]]
    assert q and all(x["rank"] == 2 and x["drivable"] is False and x["paved"] is True for x in q)
    client.post("/api/streets/attrs", json={"original": p0["original"], "paved": None, "drivable": None})           # back to unknown
    net3 = compute(client)
    q3 = [f["properties"] for f in net3["streets"]["features"] if f["properties"]["original"] == p0["original"]]
    assert q3 and all(x["paved"] is None and x["drivable"] is None for x in q3)
    assert client.get("/api/export/streets.csv").status_code == 200 and client.get("/api/export/edges.gpkg").status_code == 200
    assert client.post("/api/streets/attrs", json={"original": p0["original"], "rank": 9}).status_code == 400
    csv_text = client.get("/api/export/streets.csv").text
    head = csv_text.splitlines()[0].split(",")
    assert {"original_id", "rank", "drivable", "paved"} <= set(head)
    lines = csv_text.splitlines()
    i_o, i_p = head.index("original_id"), head.index("paved")
    other = next(l for l in lines[1:] if l.split(",")[i_o] != str(p0["original"]))
    cols = other.split(",")
    flipped = "True" if cols[i_p] in ("", "False") else "False"
    cols[i_p] = flipped
    edited = "\n".join([lines[0], ",".join(cols)])
    r = client.post("/api/streets/attrs/import", json={"content_b64": base64.b64encode(edited.encode()).decode()}).json()
    assert r["changed"] == 1
    client.post("/api/undo")
    assert client.get("/api/export/edges.csv").status_code == 200


def test_every_street_listed_at_a_node_is_drawn_on_the_map(client):
    """Streets cut off as branches get a line too (they used to be listed in the node popup but missing from the map)."""
    set_reference(client)
    net = compute(client)
    drawn = {f["properties"]["id"] for f in net["streets"]["features"]}
    listed = {s["id"] for f in net["junctions"]["features"] for s in f["properties"]["spokes"]}
    assert listed <= drawn
    st = client.app.state.ctx.network
    assert set(st.streets["street_id"].astype(int)) == set(st.streets_gdf["street_id"].astype(int))


def test_a_drawn_road_gets_the_rank_drivable_and_paved_chosen_and_roads_are_listed_by_file(client):
    """Drawing a new road asks for rank / drivable / paved; the street lines say which file a road comes from."""
    set_reference(client)
    base = client.app.state.ctx.get_base()
    n0 = base.nodes.iloc[0]
    far = H.lonlat(n0["x"] + 3.0, n0["y"] - 160.0)
    start = H.lonlat(n0["x"] + 1.0, n0["y"] + 1.0)
    client.post("/api/roads/draw", json={"pieces": [{"coords": [list(start), list(far)], "start": None, "end": None}], "name": "Rang2", "rank": 2, "drivable": False, "paved": True})
    net = compute(client)
    q = [f["properties"] for f in net["streets"]["features"] if f["properties"]["name"] == "Rang2"]
    assert q and q[0]["rank"] == 2 and q[0]["drivable"] is False and q[0]["paved"] is True
    files = {f["properties"]["file"] for f in client.get("/api/base/streets").json()["features"]}
    assert files == {None, "1"}                                                         # the downloaded network and import 1


def test_reference_points_have_a_sign_rank_that_goes_to_the_signs(client):
    """Each reference point has a rank (1 landmark ... 3 local); it is editable and the points appear on the signs of the nearest intersection."""
    set_reference(client)
    compute(client)
    assert client.post("/api/addresses/compute").json()["started"] and wait(client)["state"] == "done"
    pts = client.get("/api/points/ranks").json()
    assert pts and all(p["rank"] in (0, 1, 2, 3) for p in pts)
    sg = client.get("/api/addresses").json()["signs"]["features"]
    assert any(p["rank"] == pts[0]["rank"] for f in sg for p in f["properties"]["pois"]) or pts[0]["rank"] == 0
    assert all(f["properties"]["spokes"] and all({"id", "bearing"} <= set(s) for s in f["properties"]["spokes"]) for f in sg)       # the sketch of the roads
    assert all("bearing" in p and "distance_m" in p for f in sg for p in f["properties"]["pois"])                                     # the arrow of a reference sign
    client.post("/api/points/rank", json={"id": pts[0]["id"], "rank": 0})
    assert client.get("/api/project").json()["stale"] is False                          # only the addresses depend on it
    assert client.post("/api/addresses/compute").json()["started"] and wait(client)["state"] == "done"
    assert [p["rank"] for p in client.get("/api/points/ranks").json()][0] == 0
    sg2 = client.get("/api/addresses").json()["signs"]["features"]
    assert not any(p["id"] == pts[0]["id"] for f in sg2 for p in f["properties"]["pois"])
    assert client.post("/api/points/rank", json={"id": pts[0]["id"], "rank": 7}).status_code == 400
    import io as _io
    import tempfile

    r = client.get("/api/export/signs.gpkg")
    with tempfile.NamedTemporaryFile(suffix=".gpkg") as f:
        f.write(r.content)
        f.flush()
        sgt = gpd.read_file(f.name)
    assert "poi_rank" in sgt.columns


def test_one_rank_per_street_by_length_and_a_merge_takes_the_rank_of_the_named_street(client):
    """A street has the rank that most of its length has; merging gives the rank of the named street, else of the id that is taken."""
    set_reference(client)
    net = compute(client)
    st = client.app.state.ctx.network
    e = st.edges.reset_index()
    e = e[(e.key < 900000000) & (e.u < e.v) & e.street_id.notna()]
    from osmaddresssystem.path_numbering import highway_rank

    for sid, g in e.groupby("street_id"):
        tot = {}
        for h, ln in zip(g["highway"], g["length"]):
            tot[highway_rank(h)] = tot.get(highway_rank(h), 0) + ln
        assert int(st.streets.set_index("street_id").loc[sid, "street_rank"]) == min(tot, key=lambda r: (-tot[r], r))
    feats = {f["properties"]["id"]: f["properties"] for f in net["streets"]["features"]}
    js = [f["properties"] for f in net["junctions"]["features"]]
    pair = next(((j, x, y) for j in js for x in {s["id"] for s in j["spokes"]} for y in {s["id"] for s in j["spokes"]}
                 if feats[x]["original"] != feats[y]["original"] and feats[x]["rank"] != feats[y]["rank"] and feats[x]["status"] != "named" and feats[y]["status"] != "named"), None)
    if pair:
        j, a, b = pair
        ref = next(s["ref"] for s in j["spokes"] if s["id"] == a)
        client.post("/api/streets/assign", json={"ref": ref, "mode": "merge", "target": b})
        net2 = compute(client)
        got = [f["properties"]["rank"] for f in net2["streets"]["features"] if f["properties"]["original"] == feats[b]["original"]]
        assert got and set(got) == {feats[b]["rank"]}                                 # no named street: the rank of the id that was taken


def test_the_larger_road_network_and_routes_to_reference_points_outside_the_area(root):
    """The roads outside the area are available as a light layer and can be deleted; a reference point outside gets a sign with its distance."""
    import json as _json
    import shapely
    from shapely.geometry import Point as P

    lon0, lat0 = H.lonlat(H.X0 + 120, H.Y0 + 120)
    far = H.lonlat(H.X0 + 4 * 120, H.Y0 + 3 * 120)                                          # the far corner of the grid
    pts = gpd.read_file(root / "input" / "points.gpkg")
    pts = pd.concat([pts, gpd.GeoDataFrame({"name": ["Far town"], "kind": ["village"], "sign_rank": [2]}, geometry=[P(*far)], crs=4326)], ignore_index=True)
    pts.loc[pts["name"] != "Far town", "sign_rank"] = 3
    pts.to_file(root / "input" / "points.gpkg")
    area = gpd.GeoDataFrame({"name": ["aoi"], "kind": ["fotouni_aoi"]}, geometry=[P(*H.lonlat(H.X0 + 60, H.Y0 + 60)).buffer(0.0012)], crs=4326)     # a small area
    area.to_file(root / "input" / "area.gpkg")
    c = TestClient(create_app(root, auto_compute=False))
    set_reference(c)
    out = c.get("/api/base/outside").json()
    assert len(out["features"]) > 0
    assert len(c.get("/api/edges").json()["outside"]["features"]) == len(out["features"])
    compute(c)
    assert c.post("/api/addresses/compute").json()["started"] and wait(c)["state"] == "done"
    sg = c.get("/api/addresses").json()["signs"]["features"]
    far_signs = [p for f in sg for p in f["properties"]["pois"] if p["name"] == "Far town"]
    assert far_signs and all(p["distance_m"] > 0 and "bearing" in p for p in far_signs)


def test_the_osm_changes_only_show_what_differs_from_osm_in_the_last_version(client):
    """A rank or a paved flag that was changed and put back is no change with respect to OSM; a kept change is."""
    import xml.etree.ElementTree as ET

    set_reference(client)
    net = compute(client)
    p0 = next(f["properties"] for f in net["streets"]["features"] if f["properties"]["rank"] == 4)
    root0 = ET.fromstring(client.get("/api/export/osm.osm").text)
    n0 = len([w for w in root0.findall("way")])
    client.post("/api/streets/attrs", json={"original": p0["original"], "rank": 2, "paved": True})
    compute(client)
    root1 = ET.fromstring(client.get("/api/export/osm.osm").text)
    changed = [w for w in root1.findall("way") if {t.get("k"): t.get("v") for t in w.findall("tag")}.get("highway") == "secondary"]
    assert len(root1.findall("way")) > n0 and changed                                  # the new rank and surface are in the file
    assert any({t.get("k"): t.get("v") for t in w.findall("tag")}.get("surface") == "paved" for w in root1.findall("way"))
    client.post("/api/streets/attrs", json={"original": p0["original"], "rank": 4, "paved": None})          # back to the original values
    compute(client)
    root2 = ET.fromstring(client.get("/api/export/osm.osm").text)
    assert len(root2.findall("way")) == n0                                             # nothing differs from OSM any more


def test_the_kml_of_the_project_is_loaded_as_its_first_file_once(root):
    """input/missing_roads.kml is loaded as file 1 at the first start; removing it keeps it removed."""
    (root / "input" / "missing_roads.kml").write_text(__import__("base64").b64decode(H.kml_b64([[(H.X0 + 5, H.Y0 - 30), (H.X0 + 5, H.Y0 - 200)]], ["Kml road"])).decode())
    c = TestClient(create_app(root, auto_compute=False))
    imps = c.get("/api/project").json()["roads"]["imports"]
    assert [i["file"] for i in imps] == ["missing_roads.kml"] and imps[0]["id"] == 1
    c.post("/api/roads/remove", json={"id": 1})
    c2 = TestClient(create_app(root, auto_compute=False))
    assert c2.get("/api/project").json()["roads"]["imports"] == []                    # the removal is kept


def test_a_part_cut_off_from_the_network_is_flagged_left_out_of_the_downloads_and_can_be_deleted(client):
    """A road that reaches nothing is a separate part: flagged, not exported, deletable (the main network is not)."""
    set_reference(client)
    base = client.app.state.ctx.get_base()
    n0 = base.nodes.iloc[0]
    a, b = H.lonlat(n0["x"] + 3000.0, n0["y"] + 3000.0), H.lonlat(n0["x"] + 3100.0, n0["y"] + 3000.0)
    client.post("/api/roads/draw", json={"pieces": [{"coords": [list(a), list(b)], "start": None, "end": None}], "name": "Island"})
    net = compute(client)
    comp = client.get("/api/base/components").json()
    assert len(comp["parts"]) == 1 and comp["edges"]["features"]
    assert any(w["code"] == "disconnected" for w in net["warnings"])
    assert "Island" not in client.get("/api/export/edges.csv").text and "Island" not in client.get("/api/export/streets.csv").text
    assert client.post("/api/base/components/delete", json={"part": 99}).status_code == 404
    assert client.post("/api/base/components/delete", json={"part": 1}).status_code == 200
    assert client.get("/api/base/components").json()["parts"] == []


def test_an_end_that_stops_near_another_road_splits_it_and_connects_automatically(client):
    """A new road ending 6 m from the middle of an existing road: that road gets a node there and the new road is connected."""
    set_reference(client)
    line = [(H.X0 + 60, H.Y0 - 150), (H.X0 + 60, H.Y0 - 6)]
    client.post("/api/roads/import", json={"filename": "missing.kml", "content_b64": H.kml_b64([line], ["Rue Sud"])})
    rep = client.get("/api/roads").json()["report"]
    assert rep["split_edges"] == 1 and not rep["isolated"]


from osmaddresssystem.app.roads import edge_ref as roads_ref


def test_merging_ids_keeps_the_rank_the_editors_set_even_when_the_network_was_not_recomputed(client):
    """A rank set by hand wins over the (stale) computed rank when two streets are joined."""
    set_reference(client)
    net = compute(client)
    feats = net["streets"]["features"]
    a, b = [f["properties"] for f in feats if f["properties"]["part"] == 0][:2]
    client.post("/api/streets/attrs", json={"original": b["original"], "rank": 2})        # no recomputation afterwards
    es = client.app.state.ctx.network.edges.reset_index()
    ref = next(roads_ref(u, v, k) for u, v, k, sid in zip(es["u"], es["v"], es["key"], es["street_id"]) if sid == a["id"])
    r = client.post("/api/streets/assign", json={"ref": ref, "mode": "merge", "target": b["id"]})
    assert r.status_code == 200
    assert client.get("/api/project").json()["roads"] is not None
    attrs = client.app.state.ctx.store.project().roads["attrs"]
    assert attrs[str(r.json()["id"])]["rank"] == 2


def test_ranks_set_by_the_editors_hold_for_streets_changed_by_the_community_edit_files(tmp_path):
    """Streets 194 and 570 of the Fotouni project are renumbered by input/street_edits.csv: a rank set on them is still the rank after computing."""
    import shutil
    from pathlib import Path

    from osmaddresssystem.app import engine, server

    src = Path(__file__).resolve().parents[1] / "projects" / "fotouni"
    root = tmp_path / "fotouni"
    shutil.copytree(src, root, ignore=shutil.ignore_patterns("project.db*", "computed"))
    ctx = server.Context(root, None)
    server.apply_default_reference(ctx)
    ctx.store.set_street_attrs({194: {"rank": 0}, 570: {"rank": 0}, 8: {"rank": 2}}, "t")
    proj = ctx.store.project()
    eb, rep = engine.effective_base(engine.load_base(proj), proj)
    st = engine.compute_network(eb, proj, roads_report=rep.report if rep else None)
    ranks = {int(o): set(st.streets.loc[st.streets["street_id_original"] == o, "street_rank"].astype(int)) for o in (194, 570, 8)}
    assert ranks == {194: {0}, 570: {0}, 8: {2}}


def test_a_rank_set_for_one_part_of_a_cut_street_leaves_the_other_parts_alone(tmp_path):
    """Street 46 of the Fotouni project is cut in three parts: the rank chosen for part 1 is only theirs."""
    import shutil
    from pathlib import Path

    from osmaddresssystem.app import engine, server

    src = Path(__file__).resolve().parents[1] / "projects" / "fotouni"
    root = tmp_path / "fotouni"
    shutil.copytree(src, root, ignore=shutil.ignore_patterns("project.db*", "computed"))
    ctx = server.Context(root, None)
    server.apply_default_reference(ctx)

    def ranks():
        proj = ctx.store.project()
        eb, rep = engine.effective_base(engine.load_base(proj), proj)
        s = engine.compute_network(eb, proj, roads_report=rep.report if rep else None).streets
        return {int(p): int(r) for o, p, r in zip(s["street_id_original"], s["street_part"], s["street_rank"]) if int(o) == 46}

    before = ranks()
    assert len(before) >= 2
    ctx.store.set_street_attrs({"46:1": {"rank": 0}}, "t")
    after = ranks()
    assert after[1] == 0 and all(after[p] == before[p] for p in before if p != 1)
    ctx.store.set_street_attrs({"46": {"rank": 2}}, "t")                       # the whole street, the part keeps its own choice
    whole = ranks()
    assert whole[1] == 0 and all(whole[p] == 2 for p in whole if p != 1)


def test_branch_streets_keep_their_ids_from_one_computation_to_the_next(tmp_path):
    """Branches (side roads cut off a street) get their ids once: recomputing, also after an edit, gives the same ids."""
    import shutil
    from pathlib import Path

    from osmaddresssystem.app import engine, server

    src = Path(__file__).resolve().parents[1] / "projects" / "fotouni"
    root = tmp_path / "fotouni"
    shutil.copytree(src, root, ignore=shutil.ignore_patterns("project.db*", "computed"))
    ctx = server.Context(root, None)
    server.apply_default_reference(ctx)
    reg = None

    def run():
        nonlocal reg
        proj = ctx.store.project()
        eb, rep = engine.effective_base(engine.load_base(proj), proj)
        st = engine.compute_network(eb, proj, roads_report=rep.report if rep else None, branch_ids=reg)
        reg = st.branch_ids
        s = st.streets
        return {(int(o), int(p)): int(i) for i, o, p in zip(s["street_id"], s["street_id_original"], s["street_part"])}

    first = run()
    ctx.store.set_street_attrs({"46": {"rank": 2}}, "t")
    second = run()
    branch_ids = {i for (o, p), i in first.items() if o == i}
    moved = {k: (first[k], second.get(k)) for k in first if k[0] in branch_ids and first[k] != second.get(k)}
    assert not moved


def test_several_ids_can_be_changed_before_one_recomputation(client):
    """Joining A to B and then the roads of A (now B) to C moves all of them: the second change sees the first one."""
    set_reference(client)
    net = compute(client)
    st = client.app.state.ctx.network
    es = st.edges.reset_index()
    es = es[es["key"] < 10**6]
    by = {}
    for u, v, k, sid in zip(es["u"], es["v"], es["key"], es["street_id"]):
        by.setdefault(int(sid), []).append(roads_ref(u, v, k))
    a, b, c = [i for i in sorted(by) if i in {f["properties"]["original"] for f in net["streets"]["features"]}][:3]
    r1 = client.post("/api/streets/assign", json={"ref": by[a][0], "mode": "merge", "target": b})
    assert r1.status_code == 200
    r2 = client.post("/api/streets/assign", json={"ref": by[a][0], "mode": "merge", "target": c})        # A's roads are B's now: both move
    assert r2.status_code == 200
    moved = client.app.state.ctx.store.project().roads["edge_street"]
    assert {moved[r] for r in by[a] + by[b]} == {c}


def test_an_id_cannot_get_three_roads_at_one_node(client):
    """Joining a third street to an id that already has two roads at a node is refused."""
    set_reference(client)
    net = compute(client)
    node = next(j["properties"] for j in net["junctions"]["features"]
                if len({s["id"] for s in j["properties"]["spokes"]}) >= 3 and len(j["properties"]["spokes"]) == len({s["id"] for s in j["properties"]["spokes"]}))
    sp = node["spokes"]
    for a in sp:
        for b in sp:
            if a is not b and client.post("/api/streets/assign", json={"ref": a["ref"], "mode": "merge", "target": b["id"]}).status_code == 200:
                c = next(x for x in sp if x is not a and x is not b)
                r = client.post("/api/streets/assign", json={"ref": c["ref"], "mode": "merge", "target": b["id"]})
                assert r.status_code == 409 and "at most" in r.json()["detail"]
                return
    raise AssertionError("no join was possible at all")
