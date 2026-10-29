"""The project database: an edit log shared by a few editors, with per-editor undo, checkpoints and the computed street ids.

The same code runs on **SQLite** (a file in the project folder, the default: one person or a small team on one machine) and on
**PostgreSQL** (a server shared by 2-3 editors): set ``ADDRESS_DB_URL`` / ``--db`` to a SQLAlchemy URL such as
``postgresql+psycopg://user:password@host/dbname``. Only standard SQL is used and every change is one short transaction.

The model is an **event log**. Every edit (a name, the reference, settings, the road point of a house, an imported road, a connection)
is one row of ``events`` with the editor, the time and the old and new value, and is **saved at once** - everybody sees it, nothing is
lost on a crash. The current state is the fold of all events that are not undone, in id order, so the last edit of a street wins and the
whole history stays readable.

* **Undo / Redo** step back / forward through the edits of one author (all edits when no author is given); a new edit ends the redo chain.
* **Checkpoints** are named, saved states (the full state and the street ids and names computed for it). **Restore** adds a ``restore``
  event with the old state - itself undoable - so history is never rewritten.
* **Conflicts**: ``set_name(..., expected=...)`` refuses (``Conflict``) when somebody else changed the street since the editor loaded it.
* Plain tables for QGIS / DB Browser / scripts: ``street_names`` (current names, who and when) and ``street_ids`` (the computed ids).
* The API supplies verified account IDs. A separate append-only audit retains before/after data and Undo/Redo/checkpoint decisions.
* Undone replay events are retained; ``closed_redo`` ends a redo branch without deleting the evidence. ``meta`` holds the revision counter.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event

from .project import Project, Settings
from .identity import audit_identity

SCHEMA_VERSION = "3"
metadata = sa.MetaData()
meta_t = sa.Table("meta", metadata, sa.Column("key", sa.String(40), primary_key=True), sa.Column("value", sa.Text))
events_t = sa.Table("events", metadata, sa.Column("id", sa.Integer, primary_key=True, autoincrement=True), sa.Column("ts", sa.String(32), nullable=False),
                    sa.Column("user", sa.String(64), nullable=False, default=""), sa.Column("action", sa.String(24), nullable=False),
                    sa.Column("street", sa.Integer), sa.Column("old", sa.Text), sa.Column("new", sa.Text),
                    sa.Column("undone", sa.Boolean, nullable=False, default=False),
                    sa.Index("ix_events_user", "user", "undone"), sa.Index("ix_events_action_street", "action", "street"))
checkpoints_t = sa.Table("checkpoints", metadata, sa.Column("id", sa.Integer, primary_key=True, autoincrement=True), sa.Column("ts", sa.String(32), nullable=False),
                         sa.Column("label", sa.Text), sa.Column("user", sa.String(64)), sa.Column("revision", sa.Integer),
                         sa.Column("state", sa.Text, nullable=False), sa.Column("streets", sa.Text))
street_names_t = sa.Table("street_names", metadata, sa.Column("street_id", sa.Integer, primary_key=True), sa.Column("name", sa.Text, nullable=False),
                          sa.Column("updated", sa.String(32)), sa.Column("user", sa.String(64)))
street_ids_t = sa.Table("street_ids", metadata, sa.Column("street_id", sa.Integer, primary_key=True), sa.Column("original_id", sa.Integer, index=True),
                        sa.Column("part", sa.Integer), sa.Column("name", sa.Text), sa.Column("display_name", sa.Text), sa.Column("status", sa.String(16)),
                        sa.Column("source", sa.String(16)), sa.Column("level", sa.Integer), sa.Column("street_rank", sa.Integer),
                        sa.Column("length_m", sa.Float), sa.Column("parent_id", sa.Integer), sa.Column("root_id", sa.Integer),
                        sa.Column("d_start", sa.Float), sa.Column("d_end", sa.Float))
computed_t = sa.Table("computed", metadata, sa.Column("key", sa.String(40), primary_key=True), sa.Column("value", sa.Text))
audit_t = sa.Table('audit',metadata,sa.Column('id',sa.Integer,primary_key=True,autoincrement=True),
                   sa.Column('ts',sa.String(32),nullable=False),sa.Column('actor',sa.String(64),nullable=False),
                   sa.Column('kind',sa.String(32),nullable=False),sa.Column('event_id',sa.Integer),
                   sa.Column('action',sa.String(40),nullable=False),sa.Column('street',sa.Integer),
                   sa.Column('before',sa.Text),sa.Column('after',sa.Text),sa.Column('details',sa.Text,nullable=False))
closed_redo_t = sa.Table('closed_redo',metadata,sa.Column('event_id',sa.Integer,primary_key=True))


class Conflict(Exception):
    """Somebody else changed the thing being edited since the editor loaded it.

    Attributes:
        current: The value in the database now (a name, or None).
        by: Who made the latest change.
    """

    def __init__(self, current, by: str = ""):
        """Remember the current value.

        Args:
            current: The current value.
            by: The editor who set it.
        """
        super().__init__(f"changed by {by or 'someone else'}: now {current!r}")
        self.current, self.by = current, by


def _now() -> str:
    """Current UTC time as ISO text.

    Returns:
        ``YYYY-MM-DDTHH:MM:SS+00:00``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_url(root: Path) -> str:
    """The database URL of a project: ``ADDRESS_DB_URL`` or a SQLite file in the project folder.

    Args:
        root: Project folder.

    Returns:
        A SQLAlchemy URL.
    """
    return os.environ.get("ADDRESS_DB_URL") or f"sqlite:///{Path(root).resolve() / 'project.db'}"


class Store:
    """The database of one project (see the module docstring)."""

    def __init__(self, root: Path, name: str | None = None, url: str | None = None):
        """Open the project database, creating the tables on first use.

        Args:
            root: Project folder.
            name: Display name used when the database is created.
            url: SQLAlchemy URL (default: :func:`default_url`).
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.url = url or default_url(self.root)
        self.engine = sa.create_engine(self.url, pool_pre_ping=True)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi, _rec):
                """Write-ahead logging and a patient busy timeout so a few editors can write at once."""
                cur = dbapi.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
        insp = sa.inspect(self.engine)
        if insp.has_table("checkpoints") and "streets" not in {c["name"] for c in insp.get_columns("checkpoints")}:
            raise RuntimeError(f"{self.url} was made by an older version of the app (single-editor schema); create the project again with 'init'")
        metadata.create_all(self.engine)
        self._cache: tuple[int, dict] | None = None
        with self.engine.begin() as c:
            if c.execute(sa.select(meta_t.c.value).where(meta_t.c.key == "schema")).first() is None:
                c.execute(meta_t.insert(), [{"key": "schema", "value": SCHEMA_VERSION}, {"key": "revision", "value": "0"},
                                           {"key": "name", "value": name or self.root.name}])
                c.execute(checkpoints_t.insert().values(ts=_now(), label="created", user="", revision=0,
                                                         state=json.dumps(self._initial_state(c, name or self.root.name))))
            if c.execute(sa.select(meta_t.c.value).where(meta_t.c.key=='audit_migrated')).first() is None:
                for ev in c.execute(sa.select(events_t).order_by(events_t.c.id)).mappings():
                    self._audit(c,'legacy_edit',ev['action'],ev['user'],event_id=ev['id'],street=ev['street'],
                                before=json.loads(ev['old']) if ev['old'] else None,
                                after=json.loads(ev['new']) if ev['new'] else None,
                                details={'legacy':True,'undone_at_migration':bool(ev['undone']),'original_ts':ev['ts']})
                c.execute(meta_t.insert().values(key='audit_migrated',value='1'))
                c.execute(meta_t.update().where(meta_t.c.key=='schema').values(value=SCHEMA_VERSION))

    # ---- state ----------------------------------------------------------------------------------------------------------
    @staticmethod
    def _initial_state(_c, name: str) -> dict:
        """The state of an empty project.

        Args:
            _c: Open connection (unused, kept for symmetry).
            name: Display name.

        Returns:
            State dict as in ``Project.to_dict``.
        """
        return Project(root=Path("."), name=name).to_dict()

    @staticmethod
    def _apply(state: dict, ev) -> None:
        """Apply one event to a state dict (in place).

        Args:
            state: State as in ``Project.to_dict``.
            ev: Event row (mapping access).
        """
        new = json.loads(ev["new"]) if ev["new"] is not None else None
        act = ev["action"]
        roads = lambda: state.setdefault("roads", {"imports": [], "connections": {}, "removed": [], "removed_features": [], "keep": [], "edge_street": {}, "no_cut": [], "attrs": {}, "point_rank": {}})  # noqa: E731
        if act == "name":
            state["names"][str(ev["street"])] = new
        elif act == "unname":
            state["names"].pop(str(ev["street"]), None)
        elif act == "reference":
            state["reference"] = new
        elif act == "settings":
            state["settings"] = {**state["settings"], **(new or {})}
        elif act == "access":
            state.setdefault("access", {})[new["building"]] = {"lat": new["lat"], "lon": new["lon"]}
        elif act == "unaccess":
            state.setdefault("access", {}).pop(json.loads(ev["old"])["building"], None)
        elif act == "roads_import":
            new = dict(new)
            roads()["connections"].update(new.pop("connections", None) or {})
            roads()["keep"] = sorted(set(roads().get("keep", [])) | set(new.pop("keep", None) or []))
            roads()["imports"].append(new)
        elif act == "roads_unname":
            for imp in roads()["imports"]:
                if imp["id"] == new["id"]:
                    for f in imp["features"]:
                        f["name"] = None
        elif act == "point_rank":
            roads().setdefault("point_rank", {}).update({k: int(v) for k, v in new["ranks"].items()})
        elif act == "street_attrs":
            for sid_, a_ in new["attrs"].items():
                roads().setdefault("attrs", {}).setdefault(str(sid_), {}).update(a_)
        elif act == "road_geometry":
            roads().setdefault("geometry", {})[new["ref"]] = new["coords"]
        elif act == "road_cut":
            roads().setdefault("cuts", []).append(new)
        elif act == "road_join":
            roads().setdefault("joins", []).append(new)
        elif act == "work_areas":
            state["work_areas"] = new
        elif act == "edges_assign":
            roads().setdefault("edge_street", {}).update({k: int(v) for k, v in new["edges"].items()})
            roads()["no_cut"] = sorted(set(roads().get("no_cut", [])) | {int(x) for x in new.get("no_cut", [])})
            for sid_, a_ in (new.get("attrs") or {}).items():
                roads().setdefault("attrs", {}).setdefault(str(sid_), {}).update(a_)
            for sid, nm in (new.get("names") or {}).items():
                state["names"][str(sid)] = nm
        elif act in ("edges_remove", "edges_restore"):
            for field_, key in (("removed", "edges"), ("removed_features", "features")):
                cur = set(roads().get(field_, []))
                cur = cur | set(new.get(key, [])) if act == "edges_remove" else cur - set(new.get(key, []))
                roads()[field_] = sorted(cur)
        elif act == "roads_remove":
            roads()["imports"] = [i for i in roads()["imports"] if i["id"] != new["id"]]
        elif act == "road_connect":
            roads()["connections"][new["node"]] = new["target"]
        elif act == "road_connect_many":
            roads()["connections"].update(new["nodes"])
        elif act == "road_unconnect":
            roads()["connections"].pop(new["node"], None)
        elif act == "restore":
            keep = state.get("name")
            state.clear()
            state.update(copy.deepcopy(new))
            state["name"] = keep or state.get("name")

    def revision(self, c=None) -> int:
        """The revision counter, increased by every change (edit, undo, redo).

        Args:
            c: Open connection, or None to open one.

        Returns:
            The revision.
        """
        if c is None:
            with self.engine.connect() as c2:
                return self.revision(c2)
        return int(c.execute(sa.select(meta_t.c.value).where(meta_t.c.key == "revision")).scalar_one())

    def _bump(self, c) -> None:
        """Increase the revision in one atomic statement.

        Args:
            c: Open connection (inside a transaction).
        """
        c.execute(meta_t.update().where(meta_t.c.key == "revision").values(
            value=sa.cast(sa.cast(meta_t.c.value, sa.Integer) + 1, sa.String)))

    def _state(self, c) -> dict:
        """The current state: the fold of the events that are not undone (cached per revision).

        Args:
            c: Open connection.

        Returns:
            State dict (a copy).
        """
        rev = self.revision(c)
        if self._cache is None or self._cache[0] != rev:
            name = c.execute(sa.select(meta_t.c.value).where(meta_t.c.key == "name")).scalar_one()
            state = self._initial_state(c, name)
            for ev in c.execute(sa.select(events_t).where(events_t.c.undone.is_(False)).order_by(events_t.c.id)).mappings():
                self._apply(state, ev)
            self._cache = (rev, state)
        return copy.deepcopy(self._cache[1])

    def project(self) -> Project:
        """The current state shared by all editors.

        Returns:
            The :class:`Project` state.
        """
        with self.engine.connect() as c:
            return Project.from_dict(self.root, self._state(c))

    # ---- editing ----------------------------------------------------------------------------------------------------------
    def _add_event(self, c, action: str, street, old, new, user: str) -> int:
        """Append an event (ending the author's redo chain) and bump the revision.

        Args:
            c: Open connection (inside a transaction).
            action: Event type.
            street: Street id or None.
            old: Previous value (JSON-serialisable).
            new: New value (JSON-serialisable).
            user: Author.

        Returns:
            The event id.
        """
        retired=list(c.execute(sa.select(events_t.c.id).where(sa.and_(events_t.c.user==user,events_t.c.undone.is_(True),
                         ~events_t.c.id.in_(sa.select(closed_redo_t.c.event_id))))).scalars())
        if retired:
            self._audit(c,'redo_branch_closed','redo_branch_closed',user,details={'events':retired})
            c.execute(closed_redo_t.insert(),[{'event_id':eid} for eid in retired])
        res = c.execute(events_t.insert().values(ts=_now(), user=user, action=action, street=street, old=json.dumps(old), new=json.dumps(new),
                                                 undone=False))
        self._bump(c)
        self._audit(c,'edit',action,user,event_id=int(res.inserted_primary_key[0]),street=street, before=old,after=new)
        return int(res.inserted_primary_key[0])

    def _audit(self,c,kind,action,user='',event_id=None,street=None,before=None,after=None,details=None):
        """Append evidence independently of the mutable undo/replay projection."""
        c.execute(audit_t.insert().values(ts=_now(),actor=user,kind=kind,action=action,event_id=event_id,street=street,
                  before=json.dumps(before) if before is not None else None,after=json.dumps(after) if after is not None else None,
                  details=json.dumps({**audit_identity(user),'revision':self.revision(c),**(details or {})})))

    def record_audit(self,action,user='',kind='account',**details):
        with self.engine.begin() as c:
            self._audit(c,kind,action,user,details=details)

    def audit(self,limit=100,before_id=None):
        with self.engine.connect() as c:
            query=sa.select(audit_t).order_by(audit_t.c.id.desc()).limit(max(1,min(int(limit),200)))
            if before_id is not None: query=query.where(audit_t.c.id<int(before_id))
            rows=c.execute(query).mappings().all()
        return [{**dict(r),'before':json.loads(r['before']) if r['before'] else None,
                 'after':json.loads(r['after']) if r['after'] else None,'details':json.loads(r['details'])} for r in rows]

    def _after(self, c) -> None:
        """Keep the plain ``street_names`` table equal to the current state.

        Args:
            c: Open connection (inside a transaction).
        """
        state = self._state(c)
        meta = self._names_meta(c)
        c.execute(street_names_t.delete())
        rows = [{"street_id": int(k), "name": v, "updated": meta.get(int(k), {}).get("ts"), "user": meta.get(int(k), {}).get("user")}
                for k, v in sorted(state["names"].items(), key=lambda kv: int(kv[0]))]
        if rows:
            c.execute(street_names_t.insert(), rows)

    def _names_meta(self, c) -> dict[int, dict]:
        """Who set the current name of every street, and when.

        Args:
            c: Open connection.

        Returns:
            ``street id -> {"user", "ts", "name"}``.
        """
        out: dict[int, dict] = {}
        for ev in c.execute(sa.select(events_t).where(sa.and_(events_t.c.undone.is_(False), events_t.c.action.in_(["name", "unname", "restore"])))
                            .order_by(events_t.c.id)).mappings():
            if ev["action"] == "name":
                out[int(ev["street"])] = {"user": ev["user"], "ts": ev["ts"], "name": json.loads(ev["new"])}
            elif ev["action"] == "unname":
                out.pop(int(ev["street"]), None)
            else:
                out = {int(k): {"user": ev["user"], "ts": ev["ts"], "name": v} for k, v in json.loads(ev["new"]).get("names", {}).items()}
        return out

    def names_meta(self) -> dict[int, dict]:
        """Who set the current name of every street, and when.

        Returns:
            ``street id -> {"user", "ts", "name"}``.
        """
        with self.engine.connect() as c:
            return self._names_meta(c)

    def set_name(self, street_id: int, name: str | None, user: str = "", expected="__unset__") -> bool:
        """Give (or remove, with an empty name) the name of a street.

        Args:
            street_id: Street id after the community edits.
            name: The name; None or blank removes the community name.
            user: Author.
            expected: The name the editor saw when opening the street (``None`` = no name). When given and different from the
                current name, nothing is changed and :class:`Conflict` is raised.

        Returns:
            True when something changed.

        Raises:
            Conflict: If somebody else changed the street in the meantime.
        """
        sid = int(street_id)
        name = (name or "").strip()
        with self.engine.begin() as c:
            old = self._state(c)["names"].get(str(sid))
            if expected != "__unset__" and (expected or None) != (old or None):
                by = self._names_meta(c).get(sid, {}).get("user", "")
                raise Conflict(old, by)
            if name == (old or ""):
                return False
            self._add_event(c, "name" if name else "unname", sid, old, name or None, user)
            self._after(c)
        return True

    def set_reference(self, lat: float, lon: float, node: int, user: str = "", by: str = "", name: str | None = None, default: bool = False) -> None:
        """Choose (or confirm) the reference node.

        Args:
            lat: Latitude of the snapped node.
            lon: Longitude of the snapped node.
            node: Node id.
            user: Author.
            by: Anonymous id of the browser that set or confirmed it (``"default"`` for the project's default point).
            name: Name of the point (the default reference has one).
            default: True when it is the project's default reference, not yet confirmed by anybody.
        """
        with self.engine.begin() as c:
            ref = {"lat": float(lat), "lon": float(lon), "node": int(node), "by": by, "default": bool(default)}
            if name:
                ref["name"] = name
            self._add_event(c, "reference", None, self._state(c)["reference"], ref, user)

    def set_settings(self, changes: dict, user: str = "") -> None:
        """Change computation settings.

        Args:
            changes: Setting name -> value (unknown names are ignored).
            user: Author.
        """
        valid = {k: v for k, v in changes.items() if k in Settings.__dataclass_fields__}
        if valid:
            with self.engine.begin() as c:
                cur = self._state(c)["settings"]
                self._add_event(c, "settings", None, {k: cur.get(k) for k in valid}, valid, user)

    def set_access(self, building_id: str, lat: float, lon: float, user: str = "") -> None:
        """Choose, by hand, the road point used to number a house.

        Args:
            building_id: The house (its ``building_id``).
            lat: Latitude of the chosen point on a road.
            lon: Longitude of the chosen point on a road.
            user: Author.
        """
        with self.engine.begin() as c:
            old = self._state(c).get("access", {}).get(str(building_id))
            self._add_event(c, "access", None, old, {"building": str(building_id), "lat": float(lat), "lon": float(lon)}, user)

    def clear_access(self, building_id: str, user: str = "") -> bool:
        """Go back to the automatic (nearest road) point of a house.

        Args:
            building_id: The house.
            user: Author.

        Returns:
            True when a chosen point was removed.
        """
        with self.engine.begin() as c:
            old = self._state(c).get("access", {}).get(str(building_id))
            if old is None:
                return False
            self._add_event(c, "unaccess", None, {"building": str(building_id), **old}, None, user)
        return True

    def import_roads(self, file: str, features: list[dict], user: str = "", connections: dict | None = None, keep: list | None = None) -> int:
        """Add a file of missing roads (drawn lines) to the project, with the connection choices made while previewing it.

        Args:
            file: File name, for the record.
            features: Drawn lines from ``roads.features_from_lines``.
            user: Author.
            connections: Connection choices by node key (see ``roads.integrate_roads``).
            keep: Existing edges that must stay although a new road replaces them.

        Returns:
            The id of the import.
        """
        with self.engine.begin() as c:
            imports = self._state(c)["roads"]["imports"]
            nid = max([i["id"] for i in imports] + [0]) + 1
            self._add_event(c, "roads_import", None, None, {"id": nid, "file": file, "features": features, "connections": connections or {}, "keep": keep or []}, user)
        return nid

    def unname_roads(self, import_id: int, user: str = "") -> None:
        """Take the names off the roads of an import (the labels of a file are not street names).

        Args:
            import_id: Id of the import.
            user: Author.
        """
        with self.engine.begin() as c:
            self._add_event(c, "roads_unname", None, None, {"id": int(import_id)}, user)

    def remove_roads(self, import_id: int, user: str = "") -> None:
        """Remove an import of missing roads.

        Args:
            import_id: Id from :meth:`import_roads`.
            user: Author.
        """
        with self.engine.begin() as c:
            self._add_event(c, "roads_remove", None, {"id": int(import_id)}, {"id": int(import_id)}, user)

    def set_point_ranks(self, ranks: dict, user: str = "") -> None:
        """Change the rank of reference points for the street signs (one undo step).

        Args:
            ranks: ``{"POINT-n": 0-3}``.
            user: Author.
        """
        if ranks:
            with self.engine.begin() as c:
                self._add_event(c, "point_rank", None, None, {"ranks": {str(k): int(v) for k, v in ranks.items()}}, user)

    def set_street_attrs(self, attrs: dict, user: str = "") -> None:
        """Change the rank / drivable / paved flag of streets (one undo step).

        Args:
            attrs: ``{original street id: {"rank": 0-5, "drivable": bool, "paved": bool}}`` (only the keys that change).
            user: Author.
        """
        attrs = {str(k): {kk: vv for kk, vv in v.items() if kk in ("rank", "drivable", "paved") and (vv is not None or kk != "rank")} for k, v in attrs.items()}
        attrs = {k: v for k, v in attrs.items() if v}
        if attrs:
            with self.engine.begin() as c:
                self._add_event(c, "street_attrs", None, None, {"attrs": attrs}, user)

    def assign_edge_streets(self, mapping: dict, user: str = "", names: dict | None = None, no_cut: list | None = None, attrs: dict | None = None) -> None:
        """Give roads another street id (one undo step), optionally with the name the street takes.

        Args:
            mapping: Edge reference ``"u-v-key"`` -> street id.
            user: Author.
            names: Street id -> name to give at the same time (when two streets are merged and only the street that is joined has a name).
        """
        if mapping or no_cut or names or attrs:
            with self.engine.begin() as c:
                self._add_event(c, "edges_assign", None, None, {"edges": {str(k): int(v) for k, v in mapping.items()},
                                                                  "names": {str(k): v for k, v in (names or {}).items()}, "no_cut": [int(x) for x in (no_cut or [])], "attrs": {str(k): v for k, v in (attrs or {}).items()}}, user)
                self._after(c)

    def set_road_geometry(self, ref: str, coords: list, expected_revision: int, user: str = "") -> None:
        """Reshape one edge, retaining its identity; one undoable event."""
        with self.engine.begin() as c:
            if self.revision(c) != expected_revision:
                raise Conflict("project changed; reload the road before saving")
            old = self._state(c).get("roads", {}).get("geometry", {}).get(ref)
            self._add_event(c, "road_geometry", None, {"ref": ref, "coords": old}, {"ref": ref, "coords": coords}, user)

    def remove_edges(self, edges: list[str], features: list[str], user: str = "") -> None:
        """Delete street edges (base network edges by reference, drawn lines by key); undoable.

        Args:
            edges: Edge references ``"u-v-key"``.
            features: Drawn-line keys ``"<import>:<id>"``.
            user: Author.
        """
        if edges or features:
            with self.engine.begin() as c:
                self._add_event(c, "edges_remove", None, None, {"edges": list(edges), "features": list(features)}, user)

    def add_road_cut(self, cut: dict, expected_revision: int, user: str = "") -> None:
        self._road_operation('road_cut', cut, expected_revision, user)

    def add_road_join(self, join: dict, expected_revision: int, user: str = "") -> None:
        self._road_operation('road_join', join, expected_revision, user)

    def _road_operation(self, action: str, operation: dict, expected_revision: int, user: str) -> None:
        with self.engine.begin() as c:
            if self.revision(c) != expected_revision:
                raise Conflict('project changed; reload the road')
            rd = self._state(c).get('roads', {})
            orders = [op.get('order', i+1) for i, op in enumerate(rd.get('cuts', []))]
            orders += [op['order'] for op in rd.get('joins', [])]
            operation = {**operation, 'order': max(orders, default=0)+1}
            self._add_event(c, action, None, None, operation, user)

    def set_work_areas(self, areas: dict, expected_revision: int, user: str = "") -> None:
        with self.engine.begin() as c:
            if self.revision(c) != expected_revision:
                raise Conflict('project changed; reload the areas')
            old = self._state(c).get('work_areas', {})
            self._add_event(c, 'work_areas', None, old, areas, user)

    def restore_edges(self, edges: list[str], features: list[str], user: str = "") -> None:
        """Bring back deleted street edges.

        Args:
            edges: Edge references ``"u-v-key"``.
            features: Drawn-line keys.
            user: Author.
        """
        if edges or features:
            with self.engine.begin() as c:
                self._add_event(c, "edges_restore", None, None, {"edges": list(edges), "features": list(features)}, user)

    def connect_road_node(self, node_key: str, target: dict, user: str = "") -> None:
        """Choose how a node of a drawn road is connected.

        Args:
            node_key: Key of the generated node.
            target: ``{"type": "node", "node": id}``, ``{"type": "edge", "u", "v", "key", "lat", "lon"}`` or ``{"type": "none"}``.
            user: Author.
        """
        with self.engine.begin() as c:
            self._add_event(c, "road_connect", None, self._state(c)["roads"]["connections"].get(node_key), {"node": node_key, "target": target}, user)

    def connect_road_nodes(self, targets: dict, user: str = "") -> None:
        """Connect several nodes at once (one undo step).

        Args:
            targets: Node key -> connection target.
            user: Author.
        """
        if targets:
            with self.engine.begin() as c:
                self._add_event(c, "road_connect_many", None, None, {"nodes": targets}, user)

    def clear_road_connection(self, node_key: str, user: str = "") -> bool:
        """Go back to the automatic connection of a node.

        Args:
            node_key: Key of the generated node.
            user: Author.

        Returns:
            True when a choice was removed.
        """
        with self.engine.begin() as c:
            old = self._state(c)["roads"]["connections"].get(node_key)
            if old is None:
                return False
            self._add_event(c, "road_unconnect", None, {"node": node_key, "target": old}, {"node": node_key}, user)
        return True

    # ---- undo / redo (own edits only) -----------------------------------------------------------------------------------------------
    def undo(self, user: str = "", strict: bool = False) -> dict | None:
        """Undo the latest edit *of this editor*.

        Args:
            user: The editor.

        Returns:
            The undone event as a dict, or None when this editor has nothing to undo.
        """
        with self.engine.begin() as c:
            ev = c.execute(sa.select(events_t).where(sa.and_(events_t.c.user == user, events_t.c.undone.is_(False)))
                           .order_by(events_t.c.id.desc()).limit(1)).mappings().first()
            if ev is None:
                return None
            if strict and c.execute(sa.select(events_t.c.id).where(sa.and_(events_t.c.id>ev['id'],events_t.c.user!=user,events_t.c.undone.is_(False))).limit(1)).first():
                raise Conflict('another editor has newer changes; review history before undoing')
            c.execute(events_t.update().where(events_t.c.id == ev["id"]).values(undone=True))
            self._bump(c)
            self._audit(c,'undo',ev['action'],user,event_id=ev['id'],street=ev['street'],before={'undone':False},after={'undone':True})
            self._after(c)
            return dict(ev)

    def redo(self, user: str = "", strict: bool = False) -> dict | None:
        """Redo the earliest undone edit of this editor.

        Args:
            user: The editor.

        Returns:
            The redone event as a dict, or None when there is nothing to redo.
        """
        with self.engine.begin() as c:
            last = c.execute(sa.select(sa.func.coalesce(sa.func.max(events_t.c.id), 0)).where(
                sa.and_(events_t.c.user == user, events_t.c.undone.is_(False)))).scalar_one()
            ev = c.execute(sa.select(events_t).where(sa.and_(events_t.c.user == user, events_t.c.undone.is_(True), events_t.c.id > last,
                                   ~events_t.c.id.in_(sa.select(closed_redo_t.c.event_id))))
                           .order_by(events_t.c.id).limit(1)).mappings().first()
            if ev is None:
                return None
            if strict and c.execute(sa.select(events_t.c.id).where(sa.and_(events_t.c.id>ev['id'],events_t.c.user!=user,events_t.c.undone.is_(False))).limit(1)).first():
                raise Conflict('another editor has newer changes; review history before redoing')
            if strict:
                last_undo=c.execute(sa.select(sa.func.max(audit_t.c.id)).where(sa.and_(audit_t.c.event_id==ev['id'],audit_t.c.kind=='undo'))).scalar_one() or 0
                if c.execute(sa.select(audit_t.c.id).where(sa.and_(audit_t.c.id>last_undo,audit_t.c.actor!=user,
                             audit_t.c.kind.in_(['edit','undo','redo','checkpoint_restore']))).limit(1)).first():
                    raise Conflict('another editor changed the project since this undo; review before redoing')
            c.execute(events_t.update().where(events_t.c.id == ev["id"]).values(undone=False))
            self._bump(c)
            self._audit(c,'redo',ev['action'],user,event_id=ev['id'],street=ev['street'],before={'undone':True},after={'undone':False})
            self._after(c)
            return dict(ev)

    def status(self, user: str = "") -> dict:
        """Where the project stands.

        Args:
            user: The editor asking (for the undo / redo flags).

        Returns:
            ``revision``, ``checkpoints``, ``last_saved`` / ``last_label`` / ``last_saved_by`` (latest checkpoint), ``last_edit``
            (``{"user", "ts", "action"}`` of the latest edit that is not undone), ``can_undo``, ``can_redo``.
        """
        with self.engine.connect() as c:
            mine = events_t.c.user == user
            can_undo = c.execute(sa.select(sa.func.count()).where(sa.and_(mine, events_t.c.undone.is_(False)))).scalar_one() > 0
            last = c.execute(sa.select(sa.func.coalesce(sa.func.max(events_t.c.id), 0)).where(sa.and_(mine, events_t.c.undone.is_(False)))).scalar_one()
            can_redo = c.execute(sa.select(sa.func.count()).where(sa.and_(mine, events_t.c.undone.is_(True), events_t.c.id > last,
                                       ~events_t.c.id.in_(sa.select(closed_redo_t.c.event_id))))).scalar_one() > 0
            cp = c.execute(sa.select(checkpoints_t).order_by(checkpoints_t.c.id.desc()).limit(1)).mappings().first()
            n = c.execute(sa.select(sa.func.count()).select_from(checkpoints_t)).scalar_one()
            le = c.execute(sa.select(events_t.c.user, events_t.c.ts, events_t.c.action).where(events_t.c.undone.is_(False))
                           .order_by(events_t.c.id.desc()).limit(1)).mappings().first()
            return {"revision": self.revision(c), "checkpoints": int(n), "last_saved": cp["ts"], "last_label": cp["label"], "last_saved_by": cp["user"],
                    "last_edit": dict(le) if le else None, "can_undo": can_undo, "can_redo": can_redo}

    # ---- checkpoints -----------------------------------------------------------------------------------------------------------------
    def save(self, label: str = "", user: str = "", streets: list[dict] | None = None) -> int:
        """Make a checkpoint of the current state (and of the computed street ids when given) and update ``street_ids``.

        Args:
            label: Short description.
            user: Author.
            streets: Computed street rows (ids, names, status) to store with the checkpoint, or None.

        Returns:
            The checkpoint id.
        """
        with self.engine.begin() as c:
            state = self._state(c)
            res = c.execute(checkpoints_t.insert().values(ts=_now(), label=label or "saved", user=user, revision=self.revision(c), state=json.dumps(state),
                                                          streets=json.dumps(streets) if streets is not None else None))
            if streets is not None:
                self._write_ids(c, streets)
            self._audit(c,'checkpoint','checkpoint_saved',user,details={'checkpoint':int(res.inserted_primary_key[0]),'label':label or 'saved'})
            return int(res.inserted_primary_key[0])

    def _write_ids(self, c, streets: list[dict]) -> None:
        """Rewrite the plain ``street_ids`` table.

        Args:
            c: Open connection (inside a transaction).
            streets: Computed street rows.
        """
        c.execute(street_ids_t.delete())
        if streets:
            c.execute(street_ids_t.insert(), [{"street_id": s["id"], "original_id": s["original"], "part": s["part"], "name": s.get("name"),
                                              "display_name": s.get("display"), "status": s["status"], "source": s.get("source"), "level": s.get("level"),
                                              "street_rank": s.get("rank"), "length_m": s.get("length"), "parent_id": s.get("parent"),
                                              "root_id": s.get("root"), "d_start": s.get("d0"), "d_end": s.get("d1")} for s in streets])

    def checkpoints(self) -> list[dict]:
        """All checkpoints, newest first.

        Returns:
            ``{"id", "ts", "label", "user", "named_streets", "has_ids"}`` per checkpoint.
        """
        with self.engine.connect() as c:
            rows = c.execute(sa.select(checkpoints_t.c.id, checkpoints_t.c.ts, checkpoints_t.c.label, checkpoints_t.c.user, checkpoints_t.c.state,
                                       checkpoints_t.c.streets).order_by(checkpoints_t.c.id.desc())).mappings().all()
        return [{"id": r["id"], "ts": r["ts"], "label": r["label"], "user": r["user"], "named_streets": len(json.loads(r["state"]).get("names", {})),
                 "has_ids": r["streets"] is not None} for r in rows]

    def restore(self, checkpoint_id: int, user: str = "", expected_revision: int | None = None, reason: str = '') -> int:
        """Go back to an older checkpoint: add a ``restore`` event (undoable) and a checkpoint with its content.

        Args:
            checkpoint_id: The checkpoint to return to.
            user: Author.

        Returns:
            The id of the new checkpoint.

        Raises:
            KeyError: If the checkpoint does not exist.
        """
        with self.engine.begin() as c:
            if expected_revision is not None and self.revision(c)!=expected_revision:
                raise Conflict('project changed; review the current revision before restoring')
            row = c.execute(sa.select(checkpoints_t).where(checkpoints_t.c.id == int(checkpoint_id))).mappings().first()
            if row is None:
                raise KeyError(checkpoint_id)
            self._add_event(c, "restore", None, self._state(c), json.loads(row["state"]), user)
            self._audit(c,'checkpoint_restore','restore',user,details={'checkpoint':checkpoint_id,'reason':reason})
            self._after(c)
            streets = json.loads(row["streets"]) if row["streets"] else None
            res = c.execute(checkpoints_t.insert().values(ts=_now(), label=f"restored #{row['id']} ({row['label']})", user=user, revision=self.revision(c),
                                                          state=row["state"], streets=row["streets"]))
            if streets is not None:
                self._write_ids(c, streets)
            return int(res.inserted_primary_key[0])

    def history(self, limit: int = 200) -> list[dict]:
        """Recent edits, newest first.

        Args:
            limit: Maximum number of rows.

        Returns:
            ``{"id", "ts", "user", "action", "street", "old", "new", "undone"}`` per edit (``restore`` events carry no payload here).
        """
        with self.engine.connect() as c:
            rows = c.execute(sa.select(events_t).order_by(events_t.c.id.desc()).limit(int(limit))).mappings().all()
        return [{"id": r["id"], "ts": r["ts"], "user": r["user"], "action": r["action"], "street": r["street"],
                 "old": None if r["action"] == "restore" or not r["old"] else json.loads(r["old"]),
                 "new": None if r["action"] in ("restore", "roads_import") or not r["new"] else json.loads(r["new"]), "undone": bool(r["undone"])} for r in rows]

    # ---- computed results ------------------------------------------------------------------------------------------------------------------
    def put_computed(self, key: str, value) -> None:
        """Store a computed result (latest only).

        Args:
            key: Name (``summary``, ``plan`` ...).
            value: JSON-serialisable value.
        """
        with self.engine.begin() as c:
            c.execute(computed_t.delete().where(computed_t.c.key == key))
            c.execute(computed_t.insert().values(key=key, value=json.dumps(value)))

    def get_computed(self, key: str, default=None):
        """Read a stored computed result.

        Args:
            key: Name.
            default: Value when missing.

        Returns:
            The stored value.
        """
        with self.engine.connect() as c:
            v = c.execute(sa.select(computed_t.c.value).where(computed_t.c.key == key)).scalar()
        return json.loads(v) if v is not None else default

    def checkpoint_streets(self, checkpoint_id: int) -> list[dict] | None:
        """The street ids and names stored with a checkpoint.

        Args:
            checkpoint_id: The checkpoint.

        Returns:
            The computed street rows, or None.
        """
        with self.engine.connect() as c:
            v = c.execute(sa.select(checkpoints_t.c.streets).where(checkpoints_t.c.id == int(checkpoint_id))).scalar()
        return json.loads(v) if v else None

    def backup(self, target: Path) -> Path:
        """Copy the database to a file (SQLite) - for PostgreSQL use ``pg_dump``.

        Args:
            target: Destination file.

        Returns:
            The path written.

        Raises:
            RuntimeError: When the database is not SQLite.
        """
        if self.engine.dialect.name != "sqlite":
            raise RuntimeError("backup of a server database: use pg_dump")
        import sqlite3

        src = sqlite3.connect(self.engine.url.database)
        dst = sqlite3.connect(str(target))
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        return Path(target)
