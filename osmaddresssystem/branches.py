"""Names for unnamed roads that branch off a named road.

Many real roads have no name of their own but leave a named, more important
road. Such a road is called by the *number of the branch point along the
parent* followed by the parent's name, using the same left/right parity rule
as house numbers: a road that leaves the named road ``xyz`` 40 m along on its
left side is ``3-xyz`` (``round(40 / 20) = 2``, nudged to the odd number for
the left side; the divisor is :data:`DEFAULT_BRANCH_NUMBER_DIVISOR_M`, 20 m,
against 2 m for houses). A road leaving ``3-xyz`` is ``5-3-xyz`` and so on, up
to ``max_levels`` prefixes (default 2).

Only streets that have no name are named, and only below a street that is
named - either by its own name or by an earlier branch level. The parent must
be at least as important as the child (``street_rank`` not larger).

Run it after the numbering (``street_direction.assign_street_directionality``)
since it uses each parent's cumulative distance along the street.
"""

from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
import pandas as pd

from .default_values import DEFAULT_BRANCH_NUMBER_DIVISOR_M, DEFAULT_MAX_BRANCH_LEVELS
from .numbering import _house_number_from_distance, add_letters

_PREFIX = "{}-{}"
PLACEHOLDER = "#{}"
"""Reference to a street that is to be named but has no name yet: ``#512``."""


def is_named(name) -> bool:
    """Tell whether a street name is a real name.

    Args:
        name: Street name cell (string, None or NaN).

    Returns:
        False for missing/blank names and for names that are only a number.
    """
    if name is None or (isinstance(name, float) and math.isnan(name)):
        return False
    s = str(name).strip()
    return bool(s) and not s.isdigit()


def branch_depth(name: str) -> int:
    """Number of branch prefixes at the start of a derived name.

    Args:
        name: Street name such as ``"4-3-xyz"`` or ``"4A-3-xyz"``.

    Returns:
        2 for ``"4-3-xyz"``, 1 for ``"3-xyz"``, 0 for ``"xyz"``.
    """
    depth, rest = 0, str(name)
    while True:
        head, sep, tail = rest.partition("-")
        if sep and head[:1].isdigit() and head.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ").isdigit():
            depth, rest = depth + 1, tail
        else:
            return depth


def _unit(dx: float, dy: float) -> tuple[float, float] | None:
    """Normalise a vector.

    Args:
        dx: x component.
        dy: y component.

    Returns:
        The unit vector, or None for a zero vector.
    """
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 0 else None


def _leaving_vector(line, from_start: bool, probe_m: float) -> tuple[float, float] | None:
    """Direction in which a line leaves one of its ends.

    Args:
        line: Line geometry (projected CRS).
        from_start: True for the start of the line, False for its end.
        probe_m: Distance along the line used to measure the direction.

    Returns:
        Unit vector pointing away from that end along the line.
    """
    d = min(probe_m, line.length)
    if from_start:
        a, b = line.interpolate(0), line.interpolate(d)
    else:
        a, b = line.interpolate(line.length), line.interpolate(line.length - d)
    return _unit(b.x - a.x, b.y - a.y)


def _tangent(line, at_start: bool) -> tuple[float, float] | None:
    """Direction of travel of a line at one of its ends.

    Args:
        line: Line geometry oriented in its numbering direction.
        at_start: True for the start, False for the end.

    Returns:
        Unit vector of travel at that end.
    """
    coords = list(line.coords)
    (ax, ay), (bx, by) = (coords[0], coords[1]) if at_start else (coords[-2], coords[-1])
    return _unit(bx - ax, by - ay)


_INDEX_CACHE: list = []
"""Last results of :func:`_index_streets` (the same network is indexed several times in one computation)."""


def _index_streets(nodes_gdf, edges: gpd.GeoDataFrame, probe_m: float) -> tuple[dict, dict, set]:
    """Cached :func:`_index_streets_uncached` (keyed on the identity of the edges and the nodes; the result is read-only).

    Args:
        nodes_gdf: Nodes with ``node_id`` clusters, or None.
        edges: Edges.
        probe_m: Probe distance.

    Returns:
        ``(pos, leaving, drivable_streets)``.
    """
    for n, e, pm, res in _INDEX_CACHE:
        if n is nodes_gdf and e is edges and pm == probe_m:
            return res
    res = _index_streets_uncached(nodes_gdf, edges, probe_m)
    _INDEX_CACHE.append((nodes_gdf, edges, probe_m, res))
    del _INDEX_CACHE[:-3]
    return res


def _index_streets_uncached(nodes_gdf, edges: gpd.GeoDataFrame, probe_m: float) -> tuple[dict, dict, set]:
    """Index what the branch rules need to know about every street.

    Args:
        nodes_gdf: Nodes with ``node_id`` clusters, or None.
        edges: Edges (see :func:`assign_branch_names`).
        probe_m: Distance along a road used to measure the direction in which it leaves a node.

    Returns:
        ``(pos, leaving, drivable_streets)``: for every street its numbered main-path nodes with
        ``(position along the street, tangent in the numbering direction)``; the nodes it touches with the
        direction and length of each leaving edge; and the set of streets with a drivable edge.
    """
    idx = edges.index.to_frame(index=False)
    cluster = nodes_gdf["node_id"].to_dict() if nodes_gdf is not None and "node_id" in nodes_gdf else {}
    cu = idx["u"].map(lambda n: cluster.get(n, n)).to_numpy()
    cv = idx["v"].map(lambda n: cluster.get(n, n)).to_numpy()
    sid = edges["street_id"].to_numpy()
    main = edges["is_main_direction"].fillna(False).astype(bool).to_numpy()
    geoms = edges.geometry.to_numpy()
    # Position of each edge's u and v node along the street numbering. Both directed copies of an
    # edge are flagged main and the u->v order says nothing about the numbering direction, so the
    # direction comes from these positions (written by assign_street_directionality). Without
    # them the edge is assumed to run u -> v with start/end lengths (the old behaviour).
    if "street_pos_u_m" in edges.columns:
        pos_u = edges["street_pos_u_m"].to_numpy(dtype=float)
        pos_v = edges["street_pos_v_m"].to_numpy(dtype=float)
    else:
        pos_u = edges["street_start_length_m"].to_numpy(dtype=float)
        pos_v = edges["street_end_length_m"].to_numpy(dtype=float)
    drivable = edges["is_drivable"].fillna(True).astype(bool).to_numpy() if "is_drivable" in edges else np.ones(len(edges), bool)

    pos: dict = {}      # street -> node -> (distance along street, tangent)
    leaving: dict = {}  # street -> node -> [(edge length, vector)]
    drivable_streets: set = set()
    for i in range(len(edges)):
        s = sid[i]
        if pd.isna(s):
            continue
        s = int(s)
        if drivable[i]:
            drivable_streets.add(s)
        g = geoms[i]
        if g is None or g.is_empty:
            continue
        if main[i] and not (math.isnan(pos_u[i]) or math.isnan(pos_v[i])):
            forward = pos_u[i] <= pos_v[i]
            go = g if forward else g.reverse()          # geometry in the numbering direction
            first, last = (cu[i], cv[i]) if forward else (cv[i], cu[i])
            lo, hi = (pos_u[i], pos_v[i]) if forward else (pos_v[i], pos_u[i])
            pos.setdefault(s, {}).setdefault(first, (lo, _tangent(go, True)))
            pos.setdefault(s, {}).setdefault(last, (hi, _tangent(go, False)))
        for node, start in ((cu[i], True), (cv[i], False)):
            vec = _leaving_vector(g, start, probe_m)
            if vec is not None:
                leaving.setdefault(s, {}).setdefault(node, []).append((g.length, vec))
    return pos, leaving, drivable_streets


def placeholder(street_id) -> str:
    """Stand-in for the name of a street that will be named later.

    Args:
        street_id: The street id.

    Returns:
        ``"#<id>"``; a derived name built on it (``3-#512``) becomes ``3-<name>`` once the street is named
        (:func:`apply_assigned_names`).
    """
    return PLACEHOLDER.format(int(street_id))


def street_lengths(edges: gpd.GeoDataFrame, streets: pd.DataFrame) -> tuple[dict, dict]:
    """Length of every street, and of the original street it was cut from.

    Args:
        edges: Edges indexed by ``(u, v, key)`` with ``street_id``, ``is_main_direction`` and geometry.
        streets: One row per street; ``length_m`` and ``street_id_original`` are used when present.

    Returns:
        ``(own, group)``: street id -> main-path length in metres, and street id -> total length of all the
        streets that share its ``street_id_original`` (equal to ``own`` when streets were never cut). A derived
        street has to be shorter than the *group* length of its parent, so cutting a parent never makes it
        "too short" for its children.
    """
    own: dict = {}
    if "length_m" in streets.columns and streets["length_m"].notna().any():
        own = {int(s): float(v) for s, v in zip(streets["street_id"], streets["length_m"]) if pd.notna(v)}
    else:
        idx = edges.index.to_frame(index=False)
        key = list(zip(np.minimum(idx["u"], idx["v"]), np.maximum(idx["u"], idx["v"]), idx["key"]))
        frame = pd.DataFrame({"s": edges["street_id"].to_numpy(), "k": key, "len": edges.geometry.length.to_numpy(),
                              "main": edges["is_main_direction"].fillna(False).astype(bool).to_numpy()})
        frame = frame[frame["main"] & frame["s"].notna()].drop_duplicates("k")
        own = {int(s): float(v) for s, v in frame.groupby("s")["len"].sum().items()}
    if "street_id_original" in streets.columns:
        orig = {int(s): int(o) for s, o in zip(streets["street_id"], streets["street_id_original"]) if pd.notna(o)}
        tot: dict = {}
        for s, o in orig.items():
            tot[o] = tot.get(o, 0.0) + own.get(s, 0.0)
        group = {s: tot[o] for s, o in orig.items()}
    else:
        group = dict(own)
    return own, group


DEFAULT_SECOND_LEVEL_MAX_DRIVE_M = 1000.0
"""Second-level names (``4-3-xyz``) are only for non-drivable roads and for drivable roads shorter than this (metres)."""


def second_level_streets(nodes_gdf: gpd.GeoDataFrame | None, edges_gdf: gpd.GeoDataFrame, streets_df: pd.DataFrame,
                         max_drive_len_m: float | None = DEFAULT_SECOND_LEVEL_MAX_DRIVE_M, probe_m: float = 10.0) -> set[int]:
    """Streets that may take a name of the second level or deeper (``4-3-xyz``).

    Only non-drivable roads (paths, footways) and drivable roads shorter than ``max_drive_len_m`` may; longer drivable roads need their
    own name or a first-level name (``3-xyz``).

    Args:
        nodes_gdf: Nodes with ``node_id`` clusters, or None.
        edges_gdf: Edges with ``street_id`` and ``is_drivable``.
        streets_df: One row per street.
        max_drive_len_m: Length limit for drivable roads; None = no limit.
        probe_m: Passed to the street index.

    Returns:
        The street ids.
    """
    _pos, leaving, drivable = _index_streets(nodes_gdf, edges_gdf, probe_m)
    own_len, _group = street_lengths(edges_gdf, streets_df)
    return {int(s) for s in streets_df["street_id"]
            if max_drive_len_m is None or int(s) not in drivable or own_len.get(int(s), 0.0) < max_drive_len_m}


def assign_branch_names(
    nodes_gdf: gpd.GeoDataFrame | None,
    edges_gdf: gpd.GeoDataFrame,
    streets_df: pd.DataFrame,
    number_divisor_m: float = DEFAULT_BRANCH_NUMBER_DIVISOR_M,
    max_levels: int = DEFAULT_MAX_BRANCH_LEVELS,
    only_drivable: bool = False,
    probe_m: float = 10.0,
    planned_streets=None,
    use_length_rule: bool = True,
    second_level_max_drive_m: float | None = DEFAULT_SECOND_LEVEL_MAX_DRIVE_M,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Name unnamed roads after the named road they branch from.

    A road can only branch from a street that is at least as important (``street_rank`` not larger) **and
    longer** than itself (``use_length_rule``): a derived street is always shorter than its main street.

    Args:
        nodes_gdf: Nodes (indexed by OSM id, with ``node_id`` clusters) used to treat
            merged intersection nodes as one; ``None`` to use ``u``/``v`` as they are.
        edges_gdf: Edges indexed by ``(u, v, key)`` with ``street_id``, ``is_main_direction``,
            ``street_start_length_m``/``street_end_length_m`` (after
            ``assign_street_directionality``), ``is_drivable`` and geometry in a projected CRS.
        streets_df: One row per street with ``street_id``, ``street_name`` and ``street_rank``.
        number_divisor_m: Metres along the parent per number step, per side.
        max_levels: Maximum number of branch prefixes in a name (default 2).
        only_drivable: Only name streets that have at least one drivable edge.
        probe_m: Distance along a road used to decide on which side of the parent it leaves.
        planned_streets: Streets that are *going to be* named but have no name yet. They act as main streets
            under the stand-in name ``#<id>`` (:func:`placeholder`), so the roads that branch from them are
            numbered now (``3-#512``) and get the real name when :func:`apply_assigned_names` is called.
        use_length_rule: Require the derived street to be shorter than its main street.
        second_level_max_drive_m: Names of the second level and deeper (``4-3-xyz``) are only given to non-drivable roads and to
            drivable roads shorter than this many metres (None = no limit).

    Returns:
        ``(edges_gdf, streets_df)`` copies. ``streets_df`` gains ``branch_name``,
        ``branch_parent_id``, ``branch_number``, ``branch_label`` (number plus letter when the slot is shared),
        ``branch_side``, ``branch_level``, ``branch_distance_m``, ``branch_prefix`` (``"4A-3-"``),
        ``branch_root_id`` (the main street the chain ends at), ``to_be_named`` (planned streets) and
        ``display_name`` (own name, else branch name, else the ``#id`` stand-in). ``edges_gdf``
        gains ``branch_name`` and ``display_name`` through ``street_id``.
    """
    edges = edges_gdf.copy()
    streets = streets_df.copy()
    pos, leaving, drivable_streets = _index_streets(nodes_gdf, edges, probe_m)

    rank = streets.set_index("street_id")["street_rank"].to_dict()
    own_name = streets.set_index("street_id")["street_name"].to_dict()
    length = {s: sum(l for v in leaving.get(s, {}).values() for l, _ in v) for s in rank}
    own_len, group_len = street_lengths(edges, streets)
    names = {int(s): str(n).strip() for s, n in own_name.items() if is_named(n)}
    planned = {int(s) for s in ([] if planned_streets is None else list(planned_streets)) if int(s) not in names}
    for s in planned:
        names[s] = placeholder(s)
    prefix_of = {s: "" for s in names}
    root_of = {s: s for s in names}
    rows: dict[int, dict] = {}
    deep_ok = second_level_streets(nodes_gdf, edges, streets, second_level_max_drive_m)

    for level in range(1, max_levels + 1):
        parents = {s for s, n in names.items() if branch_depth(n) == level - 1 and s in pos}
        new_rows = {}
        for s in streets["street_id"]:
            s = int(s)
            if s in names or s in new_rows or s not in leaving or (only_drivable and s not in drivable_streets):
                continue
            if level >= 2 and s not in deep_ok:                  # a long drivable road cannot take a second-level name
                continue
            best = None
            for p in parents:
                if p == s or rank.get(p, 99) > rank.get(s, 99):
                    continue
                if use_length_rule and not group_len.get(p, 0.0) > own_len.get(s, 0.0):
                    continue
                shared = set(leaving[s]) & set(pos[p])
                if not shared:
                    continue
                key = (rank.get(p, 99), -length.get(p, 0), p)
                if best is None or key < best[0]:
                    best = (key, p, shared)
            if best is None:
                continue
            _, p, shared = best
            own_pos = pos.get(s, {})
            node = min(shared, key=lambda n: (own_pos.get(n, (math.inf,))[0], pos[p][n][0], str(n)))
            dist, tan = pos[p][node]
            vec = max(leaving[s][node], key=lambda t: t[0])[1]
            side = "left" if tan is not None and tan[0] * vec[1] - tan[1] * vec[0] > 0 else "right"
            number = _house_number_from_distance(dist, number_divisor_m, side)
            new_rows[s] = {"branch_parent_id": p, "branch_number": number, "branch_side": side,
                           "branch_level": level, "branch_distance_m": round(float(dist), 1)}
        # Several roads in one number slot (same parent, side and number) get letters: 3A-xyz, 3B-xyz.
        order = sorted(new_rows)
        labels = add_letters(
            [new_rows[s]["branch_number"] for s in order],
            [(new_rows[s]["branch_parent_id"], new_rows[s]["branch_side"], new_rows[s]["branch_number"]) for s in order],
            [(new_rows[s]["branch_distance_m"], s) for s in order],
        )
        for s, label in zip(order, labels):
            p = new_rows[s]["branch_parent_id"]
            root = root_of[p]
            prefix = f"{label}-{prefix_of[p]}"
            new_rows[s].update({"branch_label": label, "branch_prefix": prefix, "branch_root_id": root,
                                "branch_name": prefix + names[root]})
        for s, r in new_rows.items():
            names[s] = r["branch_name"]
            prefix_of[s] = r["branch_prefix"]
            root_of[s] = r["branch_root_id"]
        rows.update(new_rows)

    for col in ("branch_name", "branch_parent_id", "branch_number", "branch_label", "branch_side", "branch_level",
                "branch_distance_m", "branch_prefix", "branch_root_id"):
        streets[col] = streets["street_id"].map(lambda s, col=col: rows.get(int(s), {}).get(col))
    streets["to_be_named"] = streets["street_id"].map(lambda s: int(s) in planned)
    streets["display_name"] = [
        n if is_named(n) else (b if isinstance(b, str) else (placeholder(s) if int(s) in planned else None))
        for s, n, b in zip(streets["street_id"], streets["street_name"], streets["branch_name"])
    ]
    for col in ("branch_name", "display_name", "to_be_named"):
        edges[col] = edges["street_id"].map(streets.set_index("street_id")[col].to_dict())
    return edges, streets


def _part_name(name: str, part) -> str:
    """Name of one part of a cut street.

    Args:
        name: Name given to the street.
        part: ``street_part`` (0 = the part that kept the id).

    Returns:
        ``name`` for part 0, else ``name (B)``, ``name (C)`` ...
    """
    from .numbering import number_letter

    return name if not part or int(part) == 0 else f"{name} ({number_letter(int(part))})"


def apply_assigned_names(edges_gdf: gpd.GeoDataFrame, streets_df: pd.DataFrame, assigned: dict) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Give the planned streets their real names and convert the roads derived from them.

    Roads that branch from a street that had no name yet carry the stand-in ``#<id>`` (``3-#512``). After the
    street is named this puts the name in: ``3-Rue X``. A street that was cut into parts gets the name on
    every part, with ``(B)``, ``(C)`` ... on the later parts, and the roads derived from a part use that part's
    name.

    Args:
        edges_gdf: Edges from :func:`assign_branch_names`.
        streets_df: Streets from :func:`assign_branch_names` (with ``branch_prefix`` / ``branch_root_id``).
        assigned: Street id -> name. Keys are matched against ``street_id_original`` when that column exists
            (naming the original street names all its parts), otherwise against ``street_id``.

    Returns:
        ``(edges_gdf, streets_df)`` copies with ``street_name``, ``to_be_named``, ``branch_name`` and
        ``display_name`` updated.
    """
    streets = streets_df.copy()
    edges = edges_gdf.copy()
    key = "street_id_original" if "street_id_original" in streets.columns else "street_id"
    part = streets["street_part"] if "street_part" in streets.columns else pd.Series(0, index=streets.index)
    for i, (k, p) in enumerate(zip(streets[key], part)):
        if int(k) in assigned and isinstance(assigned[int(k)], str) and assigned[int(k)].strip():
            streets.iloc[i, streets.columns.get_loc("street_name")] = _part_name(assigned[int(k)].strip(), p)
            streets.iloc[i, streets.columns.get_loc("to_be_named")] = False
    own = streets.set_index("street_id")["street_name"].to_dict()

    def _root_text(root) -> str:
        """Name of the main street a chain ends at, or its ``#id`` stand-in while it has none."""
        n = own.get(int(root))
        return n if is_named(n) else placeholder(root)

    streets["branch_name"] = [
        (pre + _root_text(r)) if isinstance(pre, str) and pd.notna(r) else None
        for pre, r in zip(streets["branch_prefix"], streets["branch_root_id"])
    ]
    streets["display_name"] = [
        n if is_named(n) else (b if isinstance(b, str) else (placeholder(s) if bool(tb) else None))
        for s, n, b, tb in zip(streets["street_id"], streets["street_name"], streets["branch_name"], streets["to_be_named"])
    ]
    for col in ("street_name", "branch_name", "display_name", "to_be_named"):
        edges[col] = edges["street_id"].map(streets.set_index("street_id")[col].to_dict())
    return edges, streets


def branch_parents(
    nodes_gdf: gpd.GeoDataFrame | None,
    edges_gdf: gpd.GeoDataFrame,
    streets_df: pd.DataFrame,
    only_drivable: bool = False,
    probe_m: float = 10.0,
    use_length_rule: bool = True,
) -> dict[int, set[int]]:
    """Which streets each street could take its derived name from.

    A street may branch from another street that it touches at a node of that street's numbered main
    path, that is at least as important (``street_rank`` not larger) and, with ``use_length_rule``, longer than
    itself - exactly the rule :func:`assign_branch_names` applies. Whether the parent is *named* is not considered.

    Args:
        nodes_gdf: Nodes with ``node_id`` clusters, or None.
        edges_gdf: Edges after ``assign_street_directionality``.
        streets_df: One row per street with ``street_id`` and ``street_rank``.
        only_drivable: Only streets with a drivable edge can be derived (and named).
        probe_m: Passed to the street index.
        use_length_rule: Require the derived street to be shorter than its main street.

    Returns:
        Mapping child street id -> set of possible parent street ids (empty set when it has none).
        Streets that cannot be named at all (not numbered/drivable) are absent.
    """
    pos, leaving, drivable = _index_streets(nodes_gdf, edges_gdf, probe_m)
    rank = streets_df.set_index("street_id")["street_rank"].to_dict()
    own_len, group_len = street_lengths(edges_gdf, streets_df)
    out: dict[int, set[int]] = {}
    at_node: dict = {}                                        # node -> streets whose numbered main path passes it
    for p, nodes in pos.items():
        for n in nodes:
            at_node.setdefault(n, set()).add(p)
    for s in (int(x) for x in streets_df["street_id"]):
        if s not in leaving or (only_drivable and s not in drivable):
            continue
        touching = set()
        rs = rank.get(s, 99)
        ls = own_len.get(s, 0.0)
        cands = set()
        for n in leaving[s]:
            cands |= at_node.get(n, set())
        for p in cands:
            if p == s or rank.get(p, 99) > rs:
                continue
            if use_length_rule and not group_len.get(p, 0.0) > ls:
                continue
            touching.add(p)
        out[s] = touching
    return out


def street_adjacency(
    nodes_gdf: gpd.GeoDataFrame | None,
    edges_gdf: gpd.GeoDataFrame,
    streets_df: pd.DataFrame,
    only_drivable: bool = False,
) -> tuple[dict[int, set[int]], dict[int, set]]:
    """Which streets touch each other (share a node), and which nodes every street has.

    Args:
        nodes_gdf: Nodes with ``node_id`` clusters, or None.
        edges_gdf: Edges with ``street_id``.
        streets_df: One row per street.
        only_drivable: Only streets with a drivable edge are considered.

    Returns:
        ``(adjacent, nodes)``: street id -> ids of the streets it touches, and street id -> its set of nodes
        (cluster ids when ``nodes_gdf`` has ``node_id``).
    """
    _pos, leaving, drivable = _index_streets(nodes_gdf, edges_gdf, 10.0)
    ids = [int(s) for s in streets_df["street_id"] if int(s) in leaving and (not only_drivable or int(s) in drivable)]
    nodes = {s: set(leaving[s]) for s in ids}
    by_node: dict = {}
    for s, ns in nodes.items():
        for n in ns:
            by_node.setdefault(n, set()).add(s)
    adjacent = {s: set() for s in ids}
    for members in by_node.values():
        for a in members:
            adjacent[a] |= members - {a}
    return adjacent, nodes
