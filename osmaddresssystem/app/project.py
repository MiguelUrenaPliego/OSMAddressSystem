"""The editable state of a project and the layout of its folder.

A project is **one folder** with everything needed to work on an area::

    <project>/
      project.db              SQLite database: names, reference, history, checkpoints, computed ids (see ``store``)
      input/streets.osm       the OpenStreetMap streets (used by ``setup`` to build the network)
      input/area.gpkg         polygon(s) of the area to process (optional)
      input/points.gpkg       important points: squares, intersections, facilities ... (optional)
      input/buildings.gpkg    the houses to address: points or polygons (optional)
      input/street_edits.csv  community street edits (optional)
      input/edge_edits.csv    community edge edits (optional)
      network/nodes.gpkg      the labelled street network built from the input
      network/edges.gpkg
      network/streets.csv
      exports/                CSV / GeoPackage exports

Names are keyed by the **street id after the community edits**, the id before path numbering cuts a street into parts.
It does not change when the reference or the names change; ``street_id_original`` in the computed state is the same id.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Settings:
    """Parameters of the computation. Numbering is always path numbering.

    Attributes:
        max_levels: Derivation levels of sub-street names (2 = ``4-3-xyz``).
        house_step_m: Metres per house-number step.
        branch_step_m: Metres per sub-street-number step.
        max_house_distance_m: Houses farther than this from any street get no address.
        connect_backbone: Named streets must be connected to the reference through named streets.
        plan_method: ``"auto"``, ``"exact"`` or ``"greedy"`` (see ``naming_plan``).
        time_limit_s: Time limit of the exact planner.
        second_level_max_drive_m: Second-level names (``4-3-xyz``) only for non-drivable roads and drivable roads shorter than this.
    """

    max_levels: int = 2
    house_step_m: float = 2.0
    branch_step_m: float = 20.0
    max_house_distance_m: float = 60.0
    connect_backbone: bool = True
    plan_method: str = "auto"
    time_limit_s: float = 60.0
    second_level_max_drive_m: float = 1000.0


@dataclass
class Project:
    """The state the computation needs.

    Attributes:
        root: Project folder.
        name: Display name.
        reference: ``{"lat", "lon", "node"}`` of the reference node, or None until it is chosen.
        settings: Computation parameters.
        names: Street id (after community edits) -> name given by the community.
        access: Building id -> ``{"lat", "lon"}`` of the road point chosen by hand for the numbering of that house
            (used instead of the nearest road point; also for houses too far from any road).
        roads: Missing roads: ``{"imports": [{"id", "file", "features": [...]}], "connections": {node key: target},
            "removed": [edge refs deleted by the editors], "removed_features": [drawn lines deleted], "keep": [existing edges
            that must stay although a new road replaces them]}``; see ``roads.integrate_roads``.
    """

    root: Path
    name: str = "project"
    reference: dict | None = None
    settings: Settings = field(default_factory=Settings)
    names: dict[int, str] = field(default_factory=dict)
    access: dict[str, dict] = field(default_factory=dict)
    roads: dict = field(default_factory=lambda: {"imports": [], "connections": {}, "removed": [], "removed_features": [], "keep": [], "edge_street": {}, "no_cut": [], "attrs": {}, "point_rank": {}})
    work_areas: dict = field(default_factory=dict)

    @property
    def input_dir(self) -> Path:
        """Folder with the input files (OSM streets, area, points, buildings, edits)."""
        return self.root / "input"

    @property
    def network_dir(self) -> Path:
        """Folder with the labelled network built from the input."""
        return self.root / "network"

    @property
    def exports_dir(self) -> Path:
        """Folder for exports."""
        return self.root / "exports"

    def to_dict(self) -> dict:
        """The saveable state (no paths).

        Returns:
            A JSON-serialisable dict.
        """
        return {"name": self.name, "reference": self.reference, "settings": asdict(self.settings),
                "names": {str(k): v for k, v in sorted(self.names.items())},
                "access": {k: self.access[k] for k in sorted(self.access)}, "roads": self.roads, "work_areas": self.work_areas}

    @classmethod
    def from_dict(cls, root: Path, data: dict) -> "Project":
        """Build a project state from :meth:`to_dict` output.

        Args:
            root: Project folder.
            data: Saved state.

        Returns:
            The project.
        """
        fields = {k: v for k, v in (data.get("settings") or {}).items() if k in Settings.__dataclass_fields__}
        return cls(root=Path(root), name=str(data.get("name", "project")), reference=data.get("reference"),
                   settings=Settings(**fields), names={int(k): str(v) for k, v in (data.get("names") or {}).items()},
                   access={str(k): dict(v) for k, v in (data.get("access") or {}).items()},
                   work_areas=dict(data.get("work_areas") or {}),
                   roads={"imports": list((data.get("roads") or {}).get("imports", [])),
                          "connections": dict((data.get("roads") or {}).get("connections", {})),
                          "removed": list((data.get("roads") or {}).get("removed", [])),
                          "removed_features": list((data.get("roads") or {}).get("removed_features", [])),
                          "keep": list((data.get("roads") or {}).get("keep", [])),
                          "edge_street": dict((data.get("roads") or {}).get("edge_street", {})),
                          "no_cut": list((data.get("roads") or {}).get("no_cut", [])),
                          "attrs": dict((data.get("roads") or {}).get("attrs", {})),
                          "geometry": dict((data.get("roads") or {}).get("geometry", {})),
                          "cuts": list((data.get("roads") or {}).get("cuts", [])),
                          "joins": list((data.get("roads") or {}).get("joins", [])),
                          "point_rank": dict((data.get("roads") or {}).get("point_rank", {}))})

    def state_hash(self) -> str:
        """Short hash of everything the computation depends on (to tell whether a result is stale).

        Returns:
            A hex digest.
        """
        d = self.to_dict()
        d.pop("name", None)
        d.pop("access", None)                    # the road point of a house only matters for the addresses (see address_hash)
        d.pop("work_areas", None)                # progress labels do not change geography or numbering
        if d.get("roads"):
            d["roads"] = {k: v for k, v in d["roads"].items() if k != "point_rank"}      # the rank of a reference point only matters for the signs (see address_hash)
        if d.get("reference"):                   # who confirmed the point does not change the computation
            d["reference"] = {k: d["reference"].get(k) for k in ("lat", "lon", "node")}
        return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]

    def address_hash(self) -> str:
        """Hash of everything the house addresses depend on (the network state and the chosen road points).

        Returns:
            A hex digest.
        """
        return hashlib.sha1((self.state_hash() + json.dumps(self.access, sort_keys=True) + json.dumps(self.roads.get("point_rank", {}), sort_keys=True)).encode()).hexdigest()[:16]

    def roads_hash(self) -> str:
        """Hash of the drawn roads and their connection choices.

        Returns:
            A hex digest.
        """
        return hashlib.sha1(json.dumps({k: v for k, v in self.roads.items() if k != "point_rank"}, sort_keys=True).encode()).hexdigest()[:16]
