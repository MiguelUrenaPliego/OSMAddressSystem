"""HTTP API and static front end of the community address app.

``create_app(project_folder)`` returns a FastAPI application. The project database is opened when the app starts
(nothing to load); when a reference node is already set the network is computed in the background so the map opens with
the current result. Computing never blocks the API: ``POST /api/compute`` starts a job and ``GET /api/job`` reports it.

Main endpoints (all JSON unless noted):

* ``GET /api/project`` - name, reference, settings, save status (pending edits, undo / redo), whether the computed
  result is stale, which input layers exist.
* ``GET /api/base/streets``, ``/api/layers/{area|points|buildings}`` - GeoJSON of the inputs.
* ``POST /api/reference`` ``{lat, lon}`` - snap to the nearest drivable node and set it as the reference.
* ``POST /api/names`` ``{street_id, name}``, ``POST /api/settings`` - edits .
* ``POST /api/undo`` / ``/api/redo`` / ``/api/discard``, ``POST /api/save`` ``{label, user}``,
  ``GET /api/checkpoints``, ``POST /api/restore`` ``{id}``, ``GET /api/history``.
* ``POST /api/compute``, ``GET /api/job``, ``GET /api/network`` - street ids, routes, names, plan.
* ``POST /api/addresses/compute``, ``GET /api/addresses`` - house numbers, addresses, street signs.
* ``GET /api/export/{addresses|streets|signs|names}.csv``.
"""

from __future__ import annotations

import base64
import csv
import io
import re
import json
import tempfile
import threading
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from pyproj import Transformer
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..branches import is_named
from . import engine, exports, roads as roads_mod
from .project import Project
from .store import Conflict, Store

def me() -> str:
    """The editor name recorded with an edit (the app has no login, so it is empty).

    Returns:
        The editor name.
    """
    return ""


STATIC = Path(__file__).resolve().parent / "static"


class Context:
    """Everything the running app keeps in memory.

    Attributes:
        store: The project database.
        base: Base data (loaded lazily).
        network: The latest :class:`engine.NetworkState` or None.
        network_json: Its GeoJSON form for the map.
        network_hash: ``Project.state_hash`` the network was computed for.
        addresses: The latest :class:`engine.AddressState` or None.
        addresses_hash: Hash the addresses were computed for.
        job: Status of the background computation.
    """

    def __init__(self, root: Path, url: str | None = None):
        """Open the project.

        Args:
            root: Project folder.
            url: Database URL (default: SQLite file in the folder or ``ADDRESS_DB_URL``).
        """
        self.store = Store(root, url=url)
        self.root = Path(root)
        self.base: engine.BaseData | None = None
        self.network: engine.NetworkState | None = None
        self.network_json: dict | None = None
        self.network_hash: str | None = None
        self.addresses: engine.AddressState | None = None
        self.addresses_hash: str | None = None
        self.effective: tuple | None = None
        self.effective_hash: str | None = None
        self.lock = threading.Lock()
        self.job = {"state": "idle", "kind": None, "step": "", "error": None}

    def get_base(self) -> engine.BaseData:
        """The base data, loaded on first use.

        Returns:
            The :class:`engine.BaseData`.
        """
        if self.base is None:
            self.base = engine.load_base(self.store.project())
        return self.base

    def get_effective(self, proj: Project | None = None) -> tuple:
        """The network with the project's drawn roads added, cached until the roads change.

        Args:
            proj: The current project state (read from the store when omitted).

        Returns:
            ``(base, roads_result)`` from :func:`engine.effective_base`.
        """
        proj = proj or self.store.project()
        h = proj.roads_hash()
        if self.effective is None or self.effective_hash != h:
            self.effective = engine.effective_base(self.get_base(), proj)
            self.effective_hash = h
        return self.effective

    def run_job(self, kind: str) -> bool:
        """Start a background computation unless one is running.

        Args:
            kind: ``"network"`` or ``"addresses"``.

        Returns:
            True when a job was started.
        """
        with self.lock:
            if self.job["state"] == "running":
                return False
            self.job = {"state": "running", "kind": kind, "step": "starting", "error": None}
        threading.Thread(target=self._work, args=(kind,), daemon=True).start()
        return True

    def _work(self, kind: str) -> None:
        """Thread body of a computation.

        Args:
            kind: ``"network"`` or ``"addresses"``.
        """
        try:
            proj = self.store.project()
            base, rres = self.get_effective(proj)
            report = rres.report if rres else None
            if kind == "network":
                state = engine.compute_network(base, proj, progress=lambda s: self.job.update(step=s), roads_report=report,
                                               hint_extra=getattr(self.network, "extra", None))
                data = engine.network_geojson(state, proj)
                self.network, self.network_json, self.network_hash = state, data, proj.state_hash()
                self.addresses = None
                self.store.put_computed("summary", {"stats": state.stats, "notes": state.notes, "hash": self.network_hash})
                self.store.put_computed("plan", data["plan"])
            else:
                if self.network is None or self.network_hash != proj.state_hash():
                    self.job.update(step="computing the network first")
                    state = engine.compute_network(base, proj, progress=lambda s: self.job.update(step=s), roads_report=report,
                                               hint_extra=getattr(self.network, "extra", None))
                    self.network, self.network_json, self.network_hash = state, engine.network_geojson(state, proj), proj.state_hash()
                self.job.update(step="addressing the houses")
                self.addresses = engine.compute_addresses(base, self.network, proj)
                self.addresses_hash = proj.address_hash()
            self.job = {"state": "done", "kind": kind, "step": "done", "error": None}
        except Exception as exc:  # reported to the client, never crashes the server
            self.job = {"state": "error", "kind": kind, "step": "failed", "error": f"{exc}", "trace": traceback.format_exc(limit=4)}


def _fc(gdf: gpd.GeoDataFrame, keep: list[str]) -> dict:
    """GeoJSON FeatureCollection of selected columns.

    Args:
        gdf: GeoDataFrame in WGS84.
        keep: Property columns to include.

    Returns:
        The FeatureCollection.
    """
    feats = []
    for _, r in gdf.iterrows():
        if r.geometry is None or r.geometry.is_empty:
            continue
        props = {}
        for c in keep:
            v = r.get(c)
            props[c] = None if v is None or (isinstance(v, float) and v != v) else (v.item() if hasattr(v, "item") else v)
        feats.append({"type": "Feature", "properties": props, "geometry": shapely.geometry.mapping(shapely.set_precision(r.geometry, 1e-6))})
    return {"type": "FeatureCollection", "features": feats}


def _csv(rows: list[dict], columns: list[str]) -> Response:
    """CSV response.

    Args:
        rows: Row dicts.
        columns: Column order.

    Returns:
        A ``text/csv`` response.
    """
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8")


def apply_default_reference(ctx: "Context") -> None:
    """Set the project's default reference point (``input/default_reference.json``: ``{"name", "lat", "lon"}``) when no reference exists yet.

    The browser asks the editor to confirm or move it; areas without a default start with no reference.

    Args:
        ctx: The running app context.
    """
    path = ctx.root / "input" / "default_reference.json"
    if not path.exists() or ctx.store.project().reference:
        return
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        ref = engine.snap_reference(ctx.get_base(), float(d["lat"]), float(d["lon"]))
    except Exception:                                   # no network built yet or a bad file: the editor chooses
        return
    ctx.store.set_reference(ref["lat"], ref["lon"], ref["node"], "", by="default", name=d.get("name"), default=True)


def apply_default_roads(ctx: "Context") -> None:
    """Load ``input/missing_roads.kml`` (the new streets the project comes with) as the first file of the project, once.

    It appears in the list of added files (and in the colour-by-file view) like an uploaded file and can be removed; it is not loaded again
    after that.

    Args:
        ctx: The running app context.
    """
    path = ctx.root / "input" / "missing_roads.kml"
    if path.exists() and ctx.store.get_computed("default_roads") and not ctx.store.get_computed("default_roads_unnamed"):
        try:
            if not any(f.get("name") for f in roads_mod.features_from_lines(roads_mod.read_lines(path))):          # the file carries no names any more
                for imp in ctx.store.project().roads["imports"]:
                    if imp["file"] == path.name and any(f.get("name") for f in imp["features"]):
                        ctx.store.unname_roads(imp["id"], "")
        except Exception:
            pass
        ctx.store.put_computed("default_roads_unnamed", True)
        return
    if not path.exists() or ctx.store.get_computed("default_roads"):
        return
    try:
        feats = roads_mod.features_from_lines(roads_mod.read_lines(path))
    except Exception:                                    # not a readable file: the editors can still upload it by hand
        return
    ctx.store.import_roads(path.name, feats, "")
    ctx.store.put_computed("default_roads", True)


def apply_csv_names(ctx: "Context") -> None:
    """Move the street names of ``input/street_edits.csv`` into the project database, once.

    Names live in the database (one editable, deletable place) and win over the csv names. The csv files stay as they are: they still
    carry the id changes, and the edge file keeps naming the parts of a street that is cut, so the result does not change.

    Args:
        ctx: The running app context.
    """
    if ctx.store.get_computed("csv_names"):
        return
    names: dict[int, str] = {}
    files = [ctx.root / "input" / "street_edits.csv"]           # the edge-level names stay in their file: they name cut parts
    for path in files:
        if not path.exists():
            continue
        df = pd.read_csv(path, dtype=str).fillna("")
        if "street_name" not in df.columns or "new_street_id" not in df.columns:
            continue
        for sid, name in zip(df["new_street_id"], df["street_name"]):
            if engine._given(name) and sid.strip().isdigit():
                names[int(sid)] = name.strip()
    have = ctx.store.project().names
    for sid, name in sorted(names.items()):
        if sid not in have:
            ctx.store.set_name(sid, name, "csv import")
    ctx.store.put_computed("csv_names", True)


def create_app(root: Path, auto_compute: bool = True, db_url: str | None = None) -> FastAPI:
    """Build the application for a project folder.

    Args:
        root: Project folder (created by ``python -m osmaddresssystem.app init``).
        auto_compute: Compute the network in the background at start when a reference node is set.
        db_url: Database URL (SQLite file in the folder by default, or ``ADDRESS_DB_URL``).

    Returns:
        The FastAPI app.
    """
    ctx = Context(Path(root), db_url)

    apply_default_reference(ctx)
    apply_default_roads(ctx)
    apply_csv_names(ctx)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Compute the current network in the background so the map opens with the latest result."""
        if auto_compute and ctx.store.project().reference:
            ctx.run_job("network")
        yield

    app = FastAPI(title="Community address app", lifespan=lifespan)
    app.state.ctx = ctx

    MAX_BODY = 60 * 1024 * 1024

    @app.middleware("http")
    async def limit_uploads(request: Request, call_next):
        """Refuse requests bigger than 60 MB (a roads file is a few hundred KB)."""
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"detail": "file too large (60 MB at most)"}, status_code=413)
        return await call_next(request)

    @app.get("/api/health")
    def get_health() -> dict:
        """Liveness / readiness for the host: the database answers, the revision, whether a computation runs."""
        try:
            rev = ctx.store.revision()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
        return {"ok": True, "database": ctx.store.engine.dialect.name, "revision": rev, "job": ctx.job["state"], "computed": ctx.network_json is not None}

    @app.get("/api/revision")
    def get_revision() -> dict:
        """Cheap change counter polled by the browsers (status of this editor plus whether the computed network is current)."""
        st = ctx.store.status(me())
        return {"status": st, "user": me(), "fresh": ctx.network_json is not None and ctx.network_hash == ctx.store.project().state_hash(),
                "network_hash": ctx.network_hash, "job": ctx.job}

    def streets_rows() -> list[dict] | None:
        """Computed street rows for a checkpoint, when the result is fresh.

        Returns:
            The rows (``properties`` of the street features) or None.
        """
        if ctx.network_json is None or ctx.network_hash != ctx.store.project().state_hash():
            return None
        return [f["properties"] for f in ctx.network_json["streets"]["features"]]

    @app.get("/api/project")
    def get_project() -> dict:
        """Project state and save status."""
        p = ctx.store.project()
        h = p.state_hash()
        base = None
        try:
            base = ctx.get_base()
        except FileNotFoundError:
            pass
        return {"name": p.name, "reference": p.reference, "settings": p.settings.__dict__, "names": {str(k): v for k, v in p.names.items()},
                "status": ctx.store.status(me()), "user": me(), "names_meta": {str(k): {"user": v["user"], "ts": v["ts"]} for k, v in ctx.store.names_meta().items()},
                "network_hash": ctx.network_hash, "network_ready": ctx.network_json is not None,
                "stale": ctx.network_json is not None and ctx.network_hash != h,
                "addresses_ready": ctx.addresses is not None, "addresses_stale": ctx.addresses is not None and ctx.addresses_hash != p.address_hash(),
                "roads": {"imports": [{"id": i["id"], "file": i["file"], "lines": len(i["features"])} for i in p.roads["imports"]],
                          "connections": len(p.roads["connections"])},
                "access": len(p.access),
                "has": {"network": base is not None, "area": bool(base and base.area is not None), "points": bool(base and base.points is not None),
                        "buildings": bool(base and base.buildings is not None)}}

    @app.get("/api/base/nodes")
    def base_nodes() -> list:
        """Nodes of drivable streets as ``[id, lon, lat]`` - the places the reference point can be (shown while it is moved)."""
        base = ctx.get_base()
        drv = base.edges["is_drivable"].astype(str).str.lower().isin(["true", "1"]) if "is_drivable" in base.edges else None
        e = base.edges[drv] if drv is not None else base.edges
        ids = set(e.index.get_level_values("u")) | set(e.index.get_level_values("v"))
        n = base.nodes[base.nodes.index.isin(ids)]
        lon, lat = Transformer.from_crs(base.crs, 4326, always_xy=True).transform(n["x"].to_numpy(), n["y"].to_numpy())
        return [[int(i), round(float(a), 7), round(float(b), 7)] for i, a, b in zip(n.index, lon, lat)]

    @app.get("/api/base/streets")
    def base_streets() -> dict:
        """The street lines of the base network (to choose the reference on)."""
        base, _res = ctx.get_effective()    # the network with the deleted edges removed and the drawn roads added
        e = base.edges.reset_index()
        e = e[e["u"] < e["v"]]
        e = e.assign(file=[(re.match(r"road(\d+):", str(o)) or [None, None])[1] for o in e["osmid"]])            # the file a road comes from (None: the downloaded network)
        g = gpd.GeoDataFrame(e[["street_id", "street_name", "highway", "file", "geometry"]], crs=base.crs).to_crs(4326)
        aoi = engine.aoi_polygon(base)
        if aoi is not None:                                         # only the streets inside the area of interest
            g = g[g.intersects(aoi)].copy()                         # whole edges that touch the area, never cropped
        return _fc(g, ["street_id", "street_name", "highway", "file"])

    @app.get("/api/base/components")
    def base_components() -> dict:
        """The parts of the network that are cut off from the main part (inside the area): their edges and a summary."""
        base, _res = ctx.get_effective()
        parts = engine.disconnected_parts(base)
        rows = []
        for p in parts:
            sub = base.edges.loc[p["edges"]].reset_index()
            sub = sub.assign(part=p["id"])
            rows.append(sub[["part", "street_id", "geometry"]])
        feats = {"type": "FeatureCollection", "features": []}
        if rows:
            g = gpd.GeoDataFrame(pd.concat(rows), crs=base.crs).to_crs(4326)
            feats = _fc(g, ["part", "street_id"])
        return {"parts": [{"id": p["id"], "streets": p["street_ids"], "edges": len(p["edges"]), "length_m": round(p["length_m"])} for p in parts], "edges": feats}

    @app.post("/api/base/components/delete")
    def post_components_delete(body: dict = Body(...)) -> dict:
        """Delete a whole separate part of the network (one of the parts flagged as not connected; never the main network): one undo step."""
        base, _res = ctx.get_effective()
        parts = {p["id"]: p for p in engine.disconnected_parts(base)}
        part = parts.get(int(body["part"]))
        if part is None:
            raise HTTPException(404, "that part is not a separate part of the network (the main network cannot be deleted)")
        edges, feats = [], []
        for (u, v, k) in part["edges"]:
            osmid = str(base.edges.loc[(u, v, k), "osmid"])
            if osmid.startswith("road"):
                feats.append(osmid[4:])
            else:
                edges.append(roads_mod.edge_ref(u, v, k))
        ctx.store.remove_edges(sorted(set(edges)), sorted(set(feats)), me())
        return {"edges": len(edges), "features": len(set(feats)), "status": ctx.store.status(me())}

    @app.get("/api/base/outside")
    def base_outside() -> dict:
        """The larger road network: the roads that do not touch the area of interest (shown light grey; they carry the routes to the reference points outside)."""
        base, _res = ctx.get_effective()
        aoi = engine.aoi_polygon(base)
        e = base.edges.reset_index()
        e = e[(e["u"] < e["v"]) & (e["key"] < roads_mod.SYNTHETIC_KEY)]
        g = gpd.GeoDataFrame(e[["street_id", "highway", "geometry"]], crs=base.crs).to_crs(4326)
        if aoi is not None:
            g = g[~g.intersects(aoi)].copy()
        g["geometry"] = g.geometry.simplify(0.00001)
        return _fc(g, ["street_id", "highway"])

    @app.get("/api/layers/{name}")
    def layer(name: str) -> dict:
        """GeoJSON of an input layer: ``area``, ``points`` or ``buildings``."""
        base = ctx.get_base()
        gdf = {"area": base.area, "points": base.points, "buildings": base.buildings}.get(name)
        if gdf is None:
            raise HTTPException(404, f"no {name} layer in this project")
        if name == "area" and "kind" in gdf.columns and gdf["kind"].astype(str).str.contains("aoi", case=False).any():
            gdf = gdf[gdf["kind"].astype(str).str.contains("aoi", case=False)]                 # only the boundary around the buildings
        return _fc(gdf, [c for c in gdf.columns if c != "geometry"][:8])

    @app.post("/api/reference")
    def post_reference(body: dict = Body(...)) -> dict:
        """Choose the reference node by clicking near it (``client`` = the anonymous id of the browser)."""
        ref = engine.snap_reference(ctx.get_base(), float(body["lat"]), float(body["lon"]))
        ctx.store.set_reference(ref["lat"], ref["lon"], ref["node"], me(), by=str(body.get("client") or "anonymous"))
        return {"reference": ref, "status": ctx.store.status(me())}

    @app.post("/api/reference/confirm")
    def post_reference_confirm(body: dict = Body(default={})) -> dict:
        """Confirm the current reference point (the default one, or one set by another editor): it is saved again under this browser."""
        cur = ctx.store.project().reference
        if not cur:
            raise HTTPException(409, "no reference point yet")
        ctx.store.set_reference(cur["lat"], cur["lon"], cur["node"], me(), by=str(body.get("client") or "anonymous"), name=cur.get("name"))
        return {"reference": ctx.store.project().reference, "status": ctx.store.status(me())}

    @app.post("/api/names")
    def post_name(body: dict = Body(...)) -> dict:
        """Give or remove a street name ."""
        try:
            changed = ctx.store.set_name(int(body["street_id"]), body.get("name"), me(), body.get("expected", "__unset__"))
        except Conflict as e:
            raise HTTPException(409, {"message": str(e), "current": e.current, "by": e.by})
        return {"changed": changed, "status": ctx.store.status(me())}

    @app.post("/api/access/snap")
    def post_access_snap(body: dict = Body(...)) -> dict:
        """Snap a click to the nearest road point (used to choose the road point of a house); nothing is stored yet."""
        if ctx.network is None:
            raise HTTPException(409, "compute the network first")
        snapped = engine.snap_to_street(ctx.network, float(body["lat"]), float(body["lon"]))
        if snapped is None:
            raise HTTPException(404, "no street within 60 m of that point")
        return snapped

    @app.post("/api/access")
    def post_access(body: dict = Body(...)) -> dict:
        """Use a chosen road point for the numbering of a house, or go back to the automatic point (``clear: true``)."""
        if body.get("clear"):
            changed = ctx.store.clear_access(str(body["building_id"]), me())
        else:
            ctx.store.set_access(str(body["building_id"]), float(body["lat"]), float(body["lon"]), me())
            changed = True
        return {"changed": changed, "status": ctx.store.status(me())}

    @app.get("/api/roads")
    def get_roads() -> dict:
        """The drawn (missing) roads as they are connected now: lines, nodes with their state and suggestions."""
        p = ctx.store.project()
        base, res = ctx.get_effective(p)
        if res is None:
            return {"imports": [{"id": i["id"], "file": i["file"], "lines": len(i["features"])} for i in p.roads["imports"]], "edges": {"type": "FeatureCollection", "features": []}, "nodes": {"type": "FeatureCollection", "features": []},
                    "report": {}}
        feats = [{"type": "Feature", "properties": {"street_id": int(r["street_id"]), "feature": r["feature"], "name": r["name"],
                                                     "u_state": r["u_state"], "v_state": r["v_state"]},
                  "geometry": shapely.geometry.mapping(shapely.set_precision(r.geometry, 1e-6))} for _, r in res.new_edges.iterrows()]
        nodes = [{"type": "Feature", "properties": {k: v for k, v in n.items() if k not in ("lat", "lon")},
                  "geometry": {"type": "Point", "coordinates": [n["lon"], n["lat"]]}} for n in res.report["nodes"]]
        rep = {k: v for k, v in res.report.items() if k != "nodes"}
        return {"imports": [{"id": i["id"], "file": i["file"], "lines": len(i["features"])} for i in p.roads["imports"]],
                "edges": {"type": "FeatureCollection", "features": feats}, "nodes": {"type": "FeatureCollection", "features": nodes}, "report": rep}

    def read_features(body: dict) -> tuple[str, list[dict]]:
        """The drawn lines of a request: the uploaded file, or the lines the browser already holds.

        Args:
            body: ``{"filename", "content_b64"}`` or ``{"filename", "features"}``.

        Returns:
            ``(file name, features)``.
        """
        name = Path(str(body.get("filename") or "roads")).name
        if body.get("features"):
            return name, body["features"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_bytes(base64.b64decode(body["content_b64"]))
            try:
                gdf = roads_mod.read_lines(path if path.suffix.lower() != ".zip" else f"zip://{path}")
            except Exception as exc:
                raise HTTPException(400, f"cannot read the roads from {name}: {exc}")
        return name, roads_mod.features_from_lines(gdf)

    @app.post("/api/roads/preview")
    def post_roads_preview(body: dict = Body(...)) -> dict:
        """Show how an uploaded roads file would be merged, without changing the project.

        Body: ``{"filename", "content_b64"}`` (first call) or ``{"filename", "features"}``, plus the staged ``connections`` (node key -> target)
        and ``keep`` (existing road references that must stay). The new roads win over duplicates (same end nodes, length within 10 %).

        Returns:
            ``features`` (to send back), ``edges`` (new roads), ``nodes`` (their nodes with the state), ``replaced`` (existing roads the
            new roads replace) and a ``summary``.
        """
        name, feats = read_features(body)
        p = ctx.store.project()
        staged = {"id": "p", "file": name, "features": feats}
        p.roads = {**p.roads, "imports": [*p.roads["imports"], staged], "connections": {**p.roads["connections"], **(body.get("connections") or {})},
                   "keep": sorted(set(p.roads.get("keep", [])) | set(body.get("keep") or []))}
        _base, res = engine.effective_base(ctx.get_base(), p, write_cache=False)
        rep = res.report
        mine = lambda keys: any(k.startswith("p:") for k in keys)  # noqa: E731
        new_edges = [{"type": "Feature", "properties": {"feature": r["feature"].split(":", 1)[1], "name": r["name"], "street_id": int(r["street_id"]),
                                                          "u_state": r["u_state"], "v_state": r["v_state"]},
                      "geometry": shapely.geometry.mapping(shapely.set_precision(r.geometry, 1e-6))}
                     for _, r in res.new_edges.iterrows() if r["feature"].startswith("p:")]
        nodes = [{"type": "Feature", "properties": {k: v for k, v in n.items() if k not in ("lat", "lon")},
                  "geometry": {"type": "Point", "coordinates": [n["lon"], n["lat"]]}} for n in rep["nodes"] if mine(n["features"])]
        replaced = [{"type": "Feature", "properties": {k: v for k, v in r.items() if k != "geometry"}, "geometry": r["geometry"]}
                    for r in rep.get("replaced", []) if r["feature"].startswith("p:")]
        mine_dangling = {n["properties"]["key"] for n in nodes if n["properties"]["key"] in set(rep["dangling"])}
        summary = {"lines": len(feats), "connected": sum(1 for n in nodes if n["properties"]["state"] == "connected"),
                   "auto": sum(1 for n in nodes if n["properties"]["state"] == "connected" and not n["properties"].get("manual")),
                   "not_connected": len(mine_dangling), "replaced": sum(len(r["properties"]["removed"]) for r in replaced),
                   "isolated": sum(1 for f in rep["isolated"] if f.startswith("p:")), "split_edges": rep["split_edges"]}
        return {"filename": name, "features": feats, "edges": {"type": "FeatureCollection", "features": new_edges},
                "nodes": {"type": "FeatureCollection", "features": nodes}, "replaced": {"type": "FeatureCollection", "features": replaced},
                "dangling": sorted(mine_dangling), "summary": summary}

    @app.post("/api/roads/import")
    def post_roads_import(body: dict = Body(...)) -> dict:
        """Add the previewed roads to the project: ``{"filename", "features" | "content_b64", "connections", "keep"}``."""
        name, feats = read_features(body)
        iid = ctx.store.import_roads(name, feats, me(), body.get("connections") or {}, body.get("keep") or [])
        return {"id": iid, "lines": len(feats), "status": ctx.store.status(me())}

    @app.post("/api/roads/snap")
    def post_roads_snap(body: dict = Body(...)) -> dict:
        """Turn a click into a connection target (an existing node, else a point of an existing road) without storing anything.

        Body: ``{"lat", "lon"}`` and optionally ``max_m`` (largest distance to a road, default 60) and ``node_m`` (a node this close wins, default 10).
        """
        _b, res = ctx.get_effective()
        base = _b
        used = set(base.edges.index.get_level_values("u")) | set(base.edges.index.get_level_values("v"))      # nodes of deleted roads are not targets
        target = roads_mod.snap_click(base.nodes[base.nodes.index.isin(used)], base.edges, float(body["lat"]), float(body["lon"]), float(body.get("node_m", 10.0)), float(body.get("max_m", 60.0)))
        if target is None:
            raise HTTPException(404, "no road or node near that point")
        return {"target": target}

    @app.get("/api/base/ends")
    def base_ends() -> list:
        """Dead ends of the road network (nodes where only one road ends) as ``[node, lon, lat, street id]`` - the roads that can be prolonged."""
        base, _res = ctx.get_effective()
        e = base.edges.reset_index()
        e = e[(e["key"] < roads_mod.SYNTHETIC_KEY) & (e["u"] < e["v"])]
        deg: dict = {}
        street: dict = {}
        for u, v, sid in zip(e["u"], e["v"], e["street_id"]):
            for n in (u, v):
                deg[int(n)] = deg.get(int(n), 0) + 1
                street[int(n)] = None if pd.isna(sid) else int(sid)
        ends = [n for n, d in deg.items() if d == 1 and n in base.nodes.index]
        aoi = engine.aoi_polygon(base)
        pts = base.nodes.loc[ends].to_crs(4326) if ends else base.nodes.iloc[:0].to_crs(4326)
        out = []
        for n, g in zip(ends, pts.geometry):
            if aoi is None or aoi.buffer(0.001).contains(g):
                out.append([n, round(g.x, 7), round(g.y, 7), street[n]])
        return out

    @app.post("/api/roads/draw")
    def post_roads_draw(body: dict = Body(...)) -> dict:
        """Add roads drawn by hand (one undo step): ``{"pieces": [{"coords": [[lon, lat], ...], "start", "end"}], "name", "highway", "join"}``.

        ``start`` / ``end`` are the snap targets (node or road point) of a piece's ends, or null; ``join`` is the street id the road continues.
        """
        pieces = body.get("pieces") or [{"coords": body.get("coords"), "start": body.get("start"), "end": body.get("end")}]
        feats = []
        for i, pc in enumerate(pieces):
            coords = pc.get("coords") or []
            if len(coords) < 2:
                raise HTTPException(400, "a road needs at least two points")
            f = {"id": i, "coords": [[round(float(x), 7), round(float(y), 7)] for x, y in coords], "name": (body.get("name") or None),
                 "highway": body.get("highway") or None}
            for k, v in (("start", pc.get("start")), ("end", pc.get("end")), ("join", body.get("join")), ("rank", body.get("rank"))):
                if v is not None:
                    f[k] = v
            for k in ("drivable", "paved"):
                if k in body:
                    f[k] = None if body[k] is None else bool(body[k])           # null = unknown
            feats.append(f)
        iid = ctx.store.import_roads("drawn by hand", feats, me(), {}, [])
        return {"id": iid, "status": ctx.store.status(me())}

    @app.get("/api/edges")
    def get_edges() -> dict:
        """Every street edge that can be deleted (network edges and drawn lines) and the deleted ones, as GeoJSON for the delete tool."""
        p = ctx.store.project()
        base = ctx.get_base()
        und = base.edges[[u < v for u, v, _ in base.edges.index]]
        und = und[[k < roads_mod.SYNTHETIC_KEY for _, _, k in und.index]].to_crs(4326)
        aoi = engine.aoi_polygon(base)
        if aoi is not None:
            und = und[und.intersects(aoi)]
        und_all = base.edges[[u < v for u, v, _ in base.edges.index]]
        und_all = und_all[[k < roads_mod.SYNTHETIC_KEY for _, _, k in und_all.index]].to_crs(4326)
        outside = []
        if aoi is not None:                                                     # the larger network can be edited too (a road that goes out of the area)
            for (u, v, k), r in zip(und_all.index, und_all.itertuples()):
                if not r.geometry.intersects(aoi):
                    outside.append({"type": "Feature", "properties": {"ref": roads_mod.edge_ref(u, v, k), "street_id": None if r.street_id != r.street_id else int(r.street_id),
                                                                       "name": "", "highway": r.highway, "length": round(float(r.length), 1)},
                                    "geometry": shapely.geometry.mapping(shapely.set_precision(r.geometry, 1e-6))})
        gone = set(p.roads.get("removed", []))
        live, dead = [], []
        for (u, v, k), r in zip(und.index, und.itertuples()):
            ref = roads_mod.edge_ref(u, v, k)
            nm = r.street_name if isinstance(r.street_name, str) and r.street_name else (r.name if isinstance(r.name, str) else "")
            f = {"type": "Feature", "properties": {"ref": ref, "street_id": None if r.street_id != r.street_id else int(r.street_id), "name": nm,
                                                    "highway": r.highway, "length": round(float(r.length), 1)},
                 "geometry": shapely.geometry.mapping(shapely.set_precision(r.geometry, 1e-6))}
            (dead if ref in gone else live).append(f)
        removed_feats = set(p.roads.get("removed_features", []))
        drawn = []
        for imp in p.roads["imports"]:
            for f in imp["features"]:
                key = f"{imp['id']}:{f['id']}"
                drawn.append({"type": "Feature", "properties": {"ref": key, "file": imp["file"], "name": f.get("name"), "removed": key in removed_feats},
                              "geometry": {"type": "LineString", "coordinates": f["coords"]}})
        fc = lambda x: {"type": "FeatureCollection", "features": x}  # noqa: E731
        return {"edges": fc(live), "removed": fc(dead), "drawn": fc(drawn), "outside": fc(outside)}

    @app.post("/api/streets/assign")
    def post_streets_assign(body: dict = Body(...)) -> dict:
        """Change the street id of the roads at a node (from the node popup of the Street IDs tab).

        Body: ``{"ref": "u-v-key"}`` the road clicked and either ``{"mode": "merge", "target": <street id>}`` (all roads of its street take the
        id of the target street) or ``{"mode": "split", "node": <node id>}`` (the branch that leaves the node on that road, as far as the
        street goes, gets a new id; only when the street leaves the node by more than one road). Applied at the next Compute.
        """
        import networkx as nx

        if ctx.network is None:
            raise HTTPException(409, "compute the network first")
        st = ctx.network
        es = st.edges.reset_index()
        es = es[(es["key"] < roads_mod.SYNTHETIC_KEY) & es["street_id"].notna()]
        es = es.assign(ref=[roads_mod.edge_ref(u, v, k) for u, v, k in zip(es["u"], es["v"], es["key"])]).drop_duplicates("ref").set_index("ref")
        ref = str(body["ref"])
        if ref not in es.index:
            raise HTTPException(404, "that road is not part of the computed network")
        fid = int(es.loc[ref, "street_id"])
        orig = {int(a): int(b) for a, b in zip(st.streets["street_id"], st.streets["street_id_original"])}
        proj = ctx.store.project()
        if body.get("mode") == "merge":
            target = int(body["target"])
            if target not in orig:
                raise HTTPException(404, "no such street id")
            if orig[target] == orig[fid]:                          # two parts of one street that were cut at a minimum of the distance
                if not body.get("force"):
                    raise HTTPException(409, "these are parts of the same street, cut automatically at a minimum of the distance to the reference: join them with the red option")
                ctx.store.assign_edge_streets({}, me(), None, [orig[target]])      # never cut it again: the next computation joins the parts
                return {"changed": 0, "id": orig[target], "name": None, "status": ctx.store.status(me())}
            mine = es[es["street_id"].astype(int) == fid].index
            own = {int(r.street_id): re.sub(r" \([A-Z]+\)$", "", str(r.street_name).strip()) for r in st.streets.itertuples() if is_named(r.street_name)}
            name_from, name_to = own.get(fid), own.get(target)
            names = {}
            if name_from and not name_to:                          # only the street that is joined has a name: the merged street takes it
                names[orig[target]] = name_from                    # (when both have names, the street whose id is kept keeps its name)
            mine_attrs = proj.roads.get("attrs", {})
            rk = {int(a): int(b) for a, b in zip(st.streets["street_id"], st.streets["street_rank"])}
            for sid_, o_ in orig.items():                          # a rank the editors set counts, also when the network was not recomputed since
                if "rank" in mine_attrs.get(str(o_), {}):
                    rk[sid_] = int(mine_attrs[str(o_)]["rank"])
            rank_new = rk[target] if name_to else rk[fid] if name_from else rk[target]          # the rank of the named street, else of the id that is taken
            ctx.store.assign_edge_streets({r: orig[target] for r in mine}, me(), names, [orig[target]] if body.get("force") else None,
                                          {orig[target]: {"rank": rank_new}})
            return {"changed": len(mine), "id": orig[target], "name": names.get(orig[target]) or name_to, "status": ctx.store.status(me())}
        if body.get("mode") == "split":
            node = int(body["node"])
            mine = es[es["street_id"].astype(int) == fid]
            at_node = [r for r, row in mine.iterrows() if node in (int(row["u"]), int(row["v"]))]
            if len(at_node) < 2:
                raise HTTPException(409, "the street leaves this node by one road only: there is nothing to split")
            G = nx.Graph()
            for r, row in mine.iterrows():
                if int(row["u"]) != node and int(row["v"]) != node:
                    G.add_edge(int(row["u"]), int(row["v"]))
            row = es.loc[ref]
            far = int(row["v"]) if int(row["u"]) == node else int(row["u"])
            comp = nx.node_connected_component(G, far) if far in G else {far}
            take = [r for r, rw in mine.iterrows() if (int(rw["u"]) in comp or int(rw["v"]) in comp)]
            if len(take) == len(mine):
                raise HTTPException(409, "this branch is the whole street: there is nothing to split off")
            used = [int(x) for x in st.streets["street_id_original"]] + [int(v) for v in proj.roads.get("edge_street", {}).values()]
            new_id = max(used) + 1
            ctx.store.assign_edge_streets({r: new_id for r in take}, me())
            return {"changed": len(take), "id": new_id, "status": ctx.store.status(me())}
        raise HTTPException(400, "mode must be merge or split")

    def current_attrs() -> dict:
        """The rank / drivable / paved of every original street as computed now."""
        out: dict = {}
        for r in tables()["streets"].itertuples():
            out.setdefault(int(r.original_id), {"rank": int(r.rank), "drivable": None if pd.isna(r.drivable) else bool(r.drivable),
                                                "paved": None if pd.isna(r.paved) else bool(r.paved)})
        return out

    @app.post("/api/streets/attrs")
    def post_street_attrs(body: dict = Body(...)) -> dict:
        """Change the rank (0-5), the drivable flag or the paved flag of a street: ``{"original": id, "part"?, "rank"?, "drivable"?, "paved"?}`` (``part``: only that part of a cut street)."""
        a: dict = {}
        if body.get("rank") is not None:
            if int(body["rank"]) not in engine.RANK_HIGHWAY:
                raise HTTPException(400, "rank must be 0 to 5")
            a["rank"] = int(body["rank"])
        for k in ("drivable", "paved"):
            if k in body:
                a[k] = None if body[k] is None else bool(body[k])           # null = unknown
        key = str(int(body["original"])) + (f":{int(body['part'])}" if body.get("part") is not None else "")        # "114:2": only part 2 of street 114
        ctx.store.set_street_attrs({key: a}, me())
        return {"status": ctx.store.status(me())}

    @app.post("/api/streets/attrs/import")
    def post_street_attrs_import(body: dict = Body(...)) -> dict:
        """Apply a street data CSV (downloaded streets file, edited): columns ``original_id`` (or ``street_id``), ``rank``, ``drivable``, ``paved``.

        Only the values that differ from the current ones are stored (one undo step).
        """
        raw = base64.b64decode(body["content_b64"]).decode("utf-8-sig")
        df = pd.read_csv(io.StringIO(raw), dtype=str).fillna("")
        df.columns = [c.strip().lower() for c in df.columns]
        key = "original_id" if "original_id" in df.columns else "street_id" if "street_id" in df.columns else None
        if key is None:
            raise HTTPException(400, "the file needs an original_id (or street_id) column")
        t = tables()["streets"]
        to_orig = {int(a): int(b) for a, b in zip(t["street_id"], t["original_id"])}
        cur = current_attrs()
        truth = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
        changes: dict = {}
        for _, r in df.iterrows():
            try:
                oid = int(float(r[key]))
            except ValueError:
                continue
            oid = to_orig.get(oid, oid) if key == "street_id" else oid
            if oid not in cur:
                continue
            new: dict = {}
            if "rank" in df.columns and str(r["rank"]).strip() != "":
                v = int(float(r["rank"]))
                if v in engine.RANK_HIGHWAY and v != cur[oid]["rank"]:
                    new["rank"] = v
            for k in ("drivable", "paved"):
                if k not in df.columns:
                    continue
                txt = str(r[k]).strip().lower()
                val = truth.get(txt) if txt in truth else (None if txt in ("", "none", "unknown", "nan") else "bad")
                if val != "bad" and val != cur[oid][k]:
                    new[k] = val
            if new:
                changes[oid] = new
        ctx.store.set_street_attrs(changes, me())
        return {"changed": len(changes), "status": ctx.store.status(me())}

    @app.get("/api/points/ranks")
    def get_point_ranks() -> list:
        """The reference points inside the area with their rank for the street signs (1 landmark ... 3 local, 0 not on the signs)."""
        base = ctx.get_base()
        rows = engine.reference_point_rows(base, ctx.store.project().roads.get("point_rank"))
        return [{"id": r.building_id, "name": r.name, "info": r.info, "rank": int(r.sign_rank), "lat": r.geometry.y, "lon": r.geometry.x} for r in rows.itertuples()]

    @app.post("/api/points/rank")
    def post_point_rank(body: dict = Body(...)) -> dict:
        """Change the sign rank (0-3) of one reference point (``{"id", "rank"}``) or of several (``{"ids": [...], "rank"}``)."""
        rank = int(body["rank"])
        if rank not in (0, 1, 2, 3):
            raise HTTPException(400, "rank must be 0 to 3")
        ids = body.get("ids") or [body["id"]]
        ctx.store.set_point_ranks({str(i): rank for i in ids}, me())
        return {"status": ctx.store.status(me())}

    @app.post("/api/edges/remove")
    def post_edges_remove(body: dict = Body(...)) -> dict:
        """Delete street edges: ``{"edges": ["u-v-key", ...], "features": ["<import>:<id>", ...]}`` (one undo step)."""
        ctx.store.remove_edges([str(x) for x in body.get("edges", [])], [str(x) for x in body.get("features", [])], me())
        return {"status": ctx.store.status(me())}

    @app.post("/api/edges/restore")
    def post_edges_restore(body: dict = Body(...)) -> dict:
        """Bring deleted street edges back."""
        ctx.store.restore_edges([str(x) for x in body.get("edges", [])], [str(x) for x in body.get("features", [])], me())
        return {"status": ctx.store.status(me())}

    @app.post("/api/roads/remove")
    def post_roads_remove(body: dict = Body(...)) -> dict:
        """Remove an import of missing roads."""
        ctx.store.remove_roads(int(body["id"]), me())
        return {"status": ctx.store.status(me())}

    @app.post("/api/roads/connect")
    def post_roads_connect(body: dict = Body(...)) -> dict:
        """Connect a node of a drawn road: to the node or road point nearest to the click, to a given target, or not at all."""
        key = str(body["node"])
        if body.get("clear"):
            return {"changed": ctx.store.clear_road_connection(key, me()), "status": ctx.store.status(me())}
        if body.get("none"):
            target = {"type": "none"}
        elif "target" in body:
            target = body["target"]
        else:
            base = ctx.get_base()
            target = roads_mod.snap_click(base.nodes, base.edges, float(body["lat"]), float(body["lon"]))
            if target is None:
                raise HTTPException(404, "no road or node within 60 m of that point")
        ctx.store.connect_road_node(key, target, me())
        return {"target": target, "status": ctx.store.status(me())}

    @app.post("/api/roads/connect-suggested")
    def post_roads_connect_suggested(body: dict = Body(default={})) -> dict:
        """Accept the suggestion of every unconnected end that has one within ``max_m`` (one undo step)."""
        max_m = float(body.get("max_m", 25.0))
        _base, res = ctx.get_effective()
        picks = {n["key"]: n["suggestion"] for n in (res.report["nodes"] if res else [])
                 if n.get("suggestion") and n["key"] in set(res.report["dangling"]) and n["suggestion"]["distance_m"] <= max_m}
        ctx.store.connect_road_nodes(picks, me())
        return {"connected": len(picks), "status": ctx.store.status(me())}

    @app.post("/api/settings")
    def post_settings(body: dict = Body(...)) -> dict:
        """Change computation settings."""
        ctx.store.set_settings(body, me())
        return {"status": ctx.store.status(me())}

    @app.post("/api/undo")
    def post_undo() -> dict:
        """Undo the latest pending edit."""
        return {"undone": ctx.store.undo(me()), "status": ctx.store.status(me())}

    @app.post("/api/redo")
    def post_redo() -> dict:
        """Redo the earliest undone edit."""
        return {"redone": ctx.store.redo(me()), "status": ctx.store.status(me())}

    @app.post("/api/save")
    def post_save(body: dict = Body(default={})) -> dict:
        """Save: pending edits become a checkpoint and the street ids / names tables are updated."""
        cid = ctx.store.save(body.get("label", ""), me(), streets_rows())
        return {"checkpoint": cid, "status": ctx.store.status(me()), "ids_saved": streets_rows() is not None}

    @app.get("/api/checkpoints")
    def get_checkpoints() -> list:
        """All checkpoints, newest first."""
        return ctx.store.checkpoints()

    @app.post("/api/restore")
    def post_restore(body: dict = Body(...)) -> dict:
        """Go back to an older checkpoint (appends a new checkpoint with its content)."""
        try:
            cid = ctx.store.restore(int(body["id"]), me())
        except KeyError:
            raise HTTPException(404, "no such checkpoint")
        return {"checkpoint": cid, "status": ctx.store.status(me())}

    @app.get("/api/history")
    def get_history(limit: int = 200) -> list:
        """Recent edits."""
        return ctx.store.history(limit)

    @app.post("/api/compute")
    def post_compute() -> dict:
        """Start recomputing ids, routes and names."""
        if not ctx.store.project().reference:
            raise HTTPException(400, "choose the reference node first")
        return {"started": ctx.run_job("network"), "job": ctx.job}

    @app.get("/api/job")
    def get_job() -> dict:
        """Status of the background computation."""
        return ctx.job

    @app.get("/api/network")
    def get_network():
        """The computed network (street ids, names, status, plan)."""
        if ctx.network_json is None:
            return JSONResponse({"detail": "not computed yet"}, status_code=409)
        return ctx.network_json

    @app.post("/api/addresses/compute")
    def post_addresses() -> dict:
        """Start computing house numbers, addresses and street signs."""
        if not ctx.store.project().reference:
            raise HTTPException(400, "choose the reference node first")
        return {"started": ctx.run_job("addresses"), "job": ctx.job}

    @app.get("/api/addresses")
    def get_addresses():
        """House addresses and street signs."""
        if ctx.addresses is None:
            return JSONResponse({"detail": "not computed yet"}, status_code=409)
        a = ctx.addresses
        return {"houses": _fc(a.houses, ["building_id", "address", "number", "street_id", "street", "side", "side_source", "road_lat", "road_lon", "azimuth", "status", "distance_m", "address_id", "source", "use", "name", "residents", "sign_rank",
                                    "access_lat", "access_lon"]),
                "signs": {"type": "FeatureCollection", "features": [
                    {"type": "Feature", "properties": {"node_id": int(r.node_id), "labels": list(r.labels), "rank": r["rank"], "pois": list(r.pois), "spokes": list(r.spokes), "street_ids": str(r.street_ids or "")},
                     "geometry": shapely.geometry.mapping(r.geometry)} for _, r in a.signs.iterrows()]},
                "stats": a.stats, "warnings": a.warnings}

    @app.get("/api/export/edges.kml")
    def export_edges_kml() -> Response:
        """Every street edge (network edges and drawn lines, deleted ones left out) as KML, to edit in Google Earth and upload again.

        One placemark per edge, named after its street (empty when it has none). A line uploaded again with the same end nodes and about
        the same length replaces the edge it came from; a changed or new line is merged as a new street.
        """
        from xml.sax.saxutils import escape

        e = get_edges()
        marks = []
        for f in e["edges"]["features"]:
            q = f["properties"]
            desc = f"street {q['street_id']}, {q['highway']}, {q['length']} m, edge {q['ref']}" if q["street_id"] is not None else f"edge {q['ref']}"
            marks.append((q["name"], desc, f["geometry"]["coordinates"]))
        for f in e["drawn"]["features"]:
            q = f["properties"]
            if not q["removed"]:
                marks.append((q["name"] or "", f"drawn line {q['ref']} ({q['file']})", f["geometry"]["coordinates"]))
        body = "".join(f"<Placemark><name>{escape(n or '')}</name><description>{escape(d)}</description><LineString><tessellate>1</tessellate>"
                       f"<coordinates>{' '.join(f'{x},{y},0' for x, y in c)}</coordinates></LineString></Placemark>" for n, d, c in marks)
        xml = (f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>{escape(ctx.store.project().name)} streets</name>'
               f"{body}</Document></kml>")
        return Response(xml, media_type="application/vnd.google-earth.kml+xml",
                        headers={"Content-Disposition": f'attachment; filename="{ctx.store.project().name}_streets.kml"'})

    def tables() -> dict:
        """The export tables of the computed network (cached until the network is computed again)."""
        if ctx.network is None:
            raise HTTPException(409, "compute the network first (Street IDs tab)")
        key = (ctx.network_hash, ctx.store.project().names.__repr__())
        if getattr(ctx, "_tables_key", None) != key:
            proj = ctx.store.project()
            base = ctx.get_base()
            edges = exports.edge_table(ctx.network, base)
            eb, _r = ctx.get_effective()                                   # parts cut off from the main network are not exported
            off = {roads_mod.edge_ref(u, v, k) for p in engine.disconnected_parts(eb) for (u, v, k) in p["edges"]}
            if off:
                edges = edges[[roads_mod.edge_ref(u, v, k) not in off for u, v, k in zip(edges["u"], edges["v"], edges["key"])]].copy()
            streets = exports.street_table(ctx.network, proj, base, edges)
            if off:
                streets = streets[streets["street_id"].isin(set(edges["street_id"].dropna().astype(int)))].copy()
            ctx._tables = {"streets": streets, "edges": exports.with_street_columns(edges, streets), "nodes": exports.node_table(ctx.network, base, edges)}
            ctx._tables_key = key
        return ctx._tables

    def serve_table(gdf: gpd.GeoDataFrame, name: str, fmt: str) -> Response:
        """A table as CSV (geometry as WKT) or GeoPackage (EPSG:4326).

        Args:
            gdf: The table.
            name: File name without extension.
            fmt: ``csv`` or ``gpkg``.

        Returns:
            The download.
        """
        if fmt == "csv":
            return Response(exports.to_csv(gdf), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
        if fmt == "gpkg":
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"{name}.gpkg"
                gdf.to_file(path, layer=name, driver="GPKG")
                return Response(path.read_bytes(), media_type="application/geopackage+sqlite3",
                                headers={"Content-Disposition": f'attachment; filename="{name}.gpkg"'})
        raise HTTPException(404, f"unknown format {fmt}")

    @app.get("/api/export/{kind}.{fmt}")
    def export(kind: str, fmt: str):
        """Downloads: ``streets`` / ``edges`` / ``nodes`` / ``buildings`` / ``signs`` as ``csv`` or ``gpkg``, ``osm`` as an OSM change file, ``names`` as csv.

        Every geometry is in EPSG:4326; see ``exports`` for the columns and flags.
        """
        if kind == "names" and fmt == "csv":
            p = ctx.store.project()
            return _csv([{"street_id": k, "name": v} for k, v in sorted(p.names.items())], ["street_id", "name"])
        if kind == "reference":
            if ctx.network is None:
                raise HTTPException(409, "compute the network first (Street IDs tab)")
            return serve_table(exports.reference_table(ctx.network, ctx.store.project()), "reference", fmt)
        if kind in ("streets", "edges", "nodes"):
            return serve_table(tables()[kind], kind, fmt)
        if kind in ("buildings", "addresses", "signs"):
            if ctx.addresses is None:
                raise HTTPException(409, "compute the addresses first (Addresses tab)")
            if kind == "signs":
                return serve_table(exports.signs_table(ctx.addresses, ctx.network), "signs", fmt)
            return serve_table(exports.buildings_table(ctx.addresses, ctx.get_base().points), "buildings", fmt)
        if kind == "osm" and fmt == "osm":
            t = tables()
            path = ctx.store.project().input_dir / "streets.osm"
            if not path.exists():
                raise HTTPException(404, "the project has no input/streets.osm to compare with")
            xml, stats = exports.osm_changes(ctx.network, ctx.get_base(), ctx.store.project(), t["edges"], t["streets"], path)
            return Response(xml, media_type="application/xml", headers={"Content-Disposition": 'attachment; filename="osm_changes.osm"',
                                                                         "X-Osm-Stats": json.dumps(stats)})
        raise HTTPException(404, f"{kind}.{fmt}")

    @app.get("/")
    def index() -> FileResponse:
        """The single-page front end."""
        return FileResponse(STATIC / "index.html")

    @app.get("/simple")
    @app.get("/simple/")
    def simple() -> FileResponse:
        """The simplified page: street names and addresses only."""
        return FileResponse(STATIC / "simple.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
