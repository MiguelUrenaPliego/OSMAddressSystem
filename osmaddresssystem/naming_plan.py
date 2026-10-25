"""Plan the fewest streets that must be named so that every street has a name or a derived name.

Roads without a name of their own can be named after a named, at-least-as-important road they
branch off (``3-xyz``, ``4A-3-xyz``; see :mod:`branches`) up to ``max_levels`` times. Given the
network, :func:`plan_minimal_naming` answers: *which streets should the community name by hand so that
every street ends up with a name or a derived name within ``max_levels`` derivations - and how few
are enough?*

A street ``c`` that is named **covers** every street reachable from it by at most ``max_levels``
branch steps (``parent -> child`` where the child touches the parent's numbered main path, the parent
is at least as important and the child is drivable). Choosing the fewest streets whose covers contain all
target streets is a set-cover problem:

* ``method="exact"`` solves it as an integer programme (``scipy.optimize.milp``, install the
  ``planning`` extra) and returns a provably minimum set (ties broken in favour of more important roads),
* ``method="greedy"`` repeatedly names the street that covers the most still-uncovered streets (also gives a
  useful *priority order*; within ln(n) of the optimum),
* ``method="auto"`` (default) uses the exact solver when scipy is available and falls back to greedy.

Streets that already have a name are roots at no cost. The derivation rules are the ones of
:func:`branches.assign_branch_names`, whose parent relation is reused (:func:`branches.branch_parents`): the main
street must be at least as important **and longer** than the derived street, so naming the chosen streets really
makes :func:`branches.assign_branch_names` name every target.

**Connected backbone.** With ``reference_node`` the named streets (those that already have a name from OSM or the
community, plus the chosen ones) must also form one network that is connected to the reference point *through named
streets only*: at least one street at the reference is named and every named street touches the reference through a
chain of named streets. The streets needed only to make that connection are part of the plan
(``reason == "connection"``). Named streets that cannot reach the reference through drivable streets at all are
reported in ``unconnectable`` and left out of the requirement. The exact method solves coverage and connectivity
together (a single-commodity flow formulation); the greedy method first connects and then grows the backbone.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
import pandas as pd

from .branches import DEFAULT_SECOND_LEVEL_MAX_DRIVE_M, branch_parents, is_named, second_level_streets, street_adjacency
from .default_values import DEFAULT_MAX_BRANCH_LEVELS


@dataclass
class NamingPlan:
    """Result of :func:`plan_minimal_naming`.

    Attributes:
        chosen: The streets to name by hand, in priority order: ``order``, ``street_id``, ``street_rank``,
            ``highway``, ``length_m``, ``street_name`` (current), ``newly_covered`` (targets this street
            brings in, counted in order) and ``covers_total`` (targets within its own reach).
        coverage: One row per target street: ``street_id``, ``root`` (street it is named from, itself when
            named), ``depth`` (0 = has a name, 1 = ``3-xyz``, 2 = ``4-3-xyz`` ...), ``source`` (``existing``,
            ``planned`` or ``uncovered``).
        uncovered: Target streets that no candidate can cover.
        n_targets: Number of target streets.
        n_already_named: Targets that are covered by existing names (depth <= ``max_levels``).
        method: Solver used (``exact`` or ``greedy``).
        optimal: True when ``chosen`` is a proven minimum, False for greedy, None when unknown.
        max_levels: Derivation levels allowed.
        notes: Free-text remarks (e.g. a solver fall-back).
        backbone: Street ids that are named or will be named (existing names plus ``chosen``).
        unconnectable: Named streets that cannot reach the reference through drivable streets.
        root_streets: Streets that touch the reference node.
    """

    chosen: pd.DataFrame
    coverage: pd.DataFrame
    uncovered: list = field(default_factory=list)
    n_targets: int = 0
    n_already_named: int = 0
    method: str = "greedy"
    optimal: bool | None = None
    max_levels: int = DEFAULT_MAX_BRANCH_LEVELS
    notes: list = field(default_factory=list)
    backbone: set = field(default_factory=set)
    unconnectable: list = field(default_factory=list)
    root_streets: list = field(default_factory=list)


def _children(parents: dict[int, set[int]]) -> dict[int, set[int]]:
    """Invert the parent relation.

    Args:
        parents: Child -> set of possible parents.

    Returns:
        Parent -> set of children.
    """
    out: dict[int, set[int]] = {}
    for child, ps in parents.items():
        for p in ps:
            out.setdefault(p, set()).add(child)
    return out


def _reach(children: dict[int, set[int]], roots, max_levels: int, deep_ok: set | None = None) -> dict[int, tuple[int, int]]:
    """Streets reachable from some root within ``max_levels`` derivation steps.

    Args:
        children: Parent -> children relation.
        roots: Streets that have a name.
        max_levels: Maximum number of derivation steps.
        deep_ok: Streets allowed at depth 2 or more (None = all).

    Returns:
        Street -> ``(depth, root)`` with the smallest depth (ties: smallest root id).
    """
    best: dict[int, tuple[int, int]] = {}
    queue = deque()
    for r in sorted(roots):
        best[r] = (0, r)
        queue.append(r)
    while queue:
        s = queue.popleft()
        depth, root = best[s]
        if depth >= max_levels:
            continue
        for c in sorted(children.get(s, ())):
            if c not in best and (depth + 1 < 2 or deep_ok is None or c in deep_ok):
                best[c] = (depth + 1, root)
                queue.append(c)
    return best


def _solve_exact(candidates: list[int], cover: dict[int, set[int]], remaining: set[int], cost: dict[int, float],
                 time_limit_s: float):
    """Minimum-cost set cover with ``scipy.optimize.milp``.

    Args:
        candidates: Streets that may be named.
        cover: Candidate -> remaining targets it covers.
        remaining: Targets still to cover.
        cost: Candidate -> cost (about 1, small tie-break terms).
        time_limit_s: Solver time limit.

    Returns:
        ``(chosen candidates or None, proven_optimal)``; None when scipy or the solve fails.
    """
    try:
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import lil_matrix
    except ImportError:
        return None, False
    targets = sorted(remaining)
    t_index = {t: i for i, t in enumerate(targets)}
    A = lil_matrix((len(targets), len(candidates)))
    for j, c in enumerate(candidates):
        for t in cover[c]:
            A[t_index[t], j] = 1
    res = milp(c=np.array([cost[c] for c in candidates]), constraints=LinearConstraint(A.tocsr(), lb=1, ub=np.inf),
               integrality=np.ones(len(candidates)), bounds=Bounds(0, 1), options={"time_limit": time_limit_s})
    if res.x is None:
        return None, False
    return [c for c, x in zip(candidates, res.x) if x > 0.5], bool(res.status == 0)


def _solve_greedy(candidates: list[int], cover: dict[int, set[int]], remaining: set[int], tie: dict[int, tuple]):
    """Greedy set cover: repeatedly take the candidate covering the most uncovered targets.

    Args:
        candidates: Streets that may be named.
        cover: Candidate -> remaining targets it covers.
        remaining: Targets still to cover.
        tie: Candidate -> sort key preferring more important / longer streets on ties.

    Returns:
        The chosen candidates in the order picked.
    """
    left = set(remaining)
    chosen = []
    while left:
        best = max(candidates, key=lambda c: (len(cover[c] & left), tuple(-x for x in tie[c])))
        gain = cover[best] & left
        if not gain:
            break
        chosen.append(best)
        left -= gain
    return chosen


def _solve_exact_connected(cand, cover, remaining, cost, adjacent, named, roots, time_limit_s):
    """Minimum-cost naming with coverage **and** connectivity to the roots (mixed integer programme).

    Coverage: every remaining target is covered by a chosen candidate. Connectivity: every named street (existing or
    chosen) receives one unit of flow that travels only along touching named streets from a root street, which
    forces the named streets to form one network connected to the reference.

    Args:
        cand: Streets that may be chosen.
        cover: Candidate -> remaining targets it covers.
        remaining: Targets still to cover.
        cost: Candidate -> cost.
        adjacent: Street -> touching streets.
        named: Streets that already have a name (and are reachable from the roots).
        roots: Streets touching the reference node.
        time_limit_s: Solver time limit.

    Returns:
        ``(chosen list or None, proven_optimal)``.
    """
    try:
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import lil_matrix
    except ImportError:
        return None, False
    cand = list(cand)
    xi = {c: i for i, c in enumerate(cand)}
    verts = sorted(set(cand) | set(named))
    vset = set(verts)
    arcs = [(u, v) for u in verts for v in sorted(adjacent.get(u, ())) if v in vset]
    sarcs = [r for r in sorted(roots) if r in vset]
    nx_, na, ns = len(cand), len(arcs), len(sarcs)
    n = nx_ + na + ns
    big = float(len(verts))
    targets = sorted(remaining)
    rows = len(targets) + len(verts) + na + ns
    A = lil_matrix((rows, n))
    lb = np.zeros(rows)
    ub = np.full(rows, np.inf)
    r = 0
    for t in targets:                                            # coverage: sum x_c >= 1
        for c in cand:
            if t in cover[c]:
                A[r, xi[c]] = 1
        lb[r] = 1
        r += 1
    arc_in = {v: [] for v in verts}
    arc_out = {v: [] for v in verts}
    for k, (u, v) in enumerate(arcs):
        arc_out[u].append(nx_ + k)
        arc_in[v].append(nx_ + k)
    for k, rr in enumerate(sarcs):
        arc_in[rr].append(nx_ + na + k)
    for v in verts:                                              # inflow - outflow = y_v
        for j in arc_in[v]:
            A[r, j] = 1
        for j in arc_out[v]:
            A[r, j] = -1
        if v in xi:
            A[r, xi[v]] = -1
            lb[r], ub[r] = 0, 0
        else:
            lb[r], ub[r] = 1, 1
        r += 1
    for k, (u, v) in enumerate(arcs):                            # flow only out of a named street
        if u in xi:
            A[r, nx_ + k] = 1
            A[r, xi[u]] = -big
            lb[r], ub[r] = -np.inf, 0
        else:
            lb[r], ub[r] = -np.inf, big
            A[r, nx_ + k] = 1
        r += 1
    for k, rr in enumerate(sarcs):
        A[r, nx_ + na + k] = 1
        if rr in xi:
            A[r, xi[rr]] = -big
            lb[r], ub[r] = -np.inf, 0
        else:
            lb[r], ub[r] = -np.inf, big
        r += 1
    c_vec = np.concatenate([[cost[c] for c in cand], np.zeros(na + ns)])
    integrality = np.concatenate([np.ones(nx_), np.zeros(na + ns)])
    bounds = Bounds(np.zeros(n), np.concatenate([np.ones(nx_), np.full(na + ns, big)]))
    res = milp(c=c_vec, constraints=LinearConstraint(A.tocsr(), lb=lb, ub=ub), integrality=integrality,
               bounds=bounds, options={"time_limit": time_limit_s})
    if res.x is None:
        return None, False
    return [c for c in cand if res.x[xi[c]] > 0.5], bool(res.status == 0)


def _bfs_cost(adjacent: dict, backbone: set) -> tuple[dict, dict]:
    """Number of streets that would have to be named to reach each street from the backbone.

    Args:
        adjacent: Street -> touching streets.
        backbone: Streets that are named (cost 0).

    Returns:
        ``(dist, parent)``: ``dist[v]`` = unnamed streets on the cheapest chain from the backbone to ``v``
        (including ``v``); ``parent`` for rebuilding the chain.
    """
    dist = {b: 0 for b in backbone}
    parent: dict = {}
    queue = deque(sorted(backbone))
    while queue:
        u = queue.popleft()
        for v in sorted(adjacent.get(u, ())):
            if v not in dist:
                dist[v] = dist[u] + 1
                parent[v] = u
                queue.append(v)
    return dist, parent


def _solve_greedy_connected(cand, cover, remaining, tie, adjacent, named, roots):
    """Greedy naming that keeps the named streets connected to the roots.

    First the existing names (and a root street) are connected by repeatedly adding the cheapest chain; then the
    candidate with the best ``newly covered / streets to name to connect it`` is added with its chain.

    Args:
        cand: Streets that may be chosen.
        cover: Candidate -> remaining targets it covers.
        remaining: Targets still to cover.
        tie: Candidate -> sort key preferring more important / longer streets.
        adjacent: Street -> touching streets.
        named: Existing named streets reachable from the roots.
        roots: Streets touching the reference node.

    Returns:
        List of chosen streets.
    """
    chosen: list = []
    free = set(cand)
    connected = set(r for r in roots if r in named)
    if not connected and roots:
        pick = max((r for r in roots if r in free), key=lambda r: tie[r], default=None) if free else None
        if pick is not None:
            chosen.append(pick)
            free.discard(pick)
            connected = {pick}

    def add_chain(target):
        """Name the unnamed streets on the cheapest chain from the connected set to ``target``."""
        nonlocal connected
        dist, parent = _bfs_cost(adjacent, connected)
        chain, v = [], target
        while v not in connected:
            if v not in dist:
                return False
            chain.append(v)
            v = parent[v]
        for s in chain:
            if s in free:
                chosen.append(s)
                free.discard(s)
            connected.add(s)
        return True

    # connect every existing named street (nearest first)
    todo = sorted(named - connected)
    while todo:
        dist, _ = _bfs_cost(adjacent, connected)
        todo = sorted((s for s in todo if s in dist and s not in connected), key=lambda s: (dist[s], s))
        if not todo:
            break
        if not add_chain(todo[0]):
            todo = todo[1:]
        else:
            todo = [s for s in todo if s not in connected]
    left = set(remaining)
    for c in chosen:
        left -= cover.get(c, set())
    while left:
        dist, _ = _bfs_cost(adjacent, connected | set(chosen))
        best, best_score = None, 0.0
        for c in sorted(free):
            gain = len(cover[c] & left)
            if not gain or c not in dist:
                continue
            score = (gain / max(dist[c], 1), tuple(-x for x in tie[c]))
            if best is None or score > best_score:
                best, best_score = c, score
        if best is None:
            break
        add_chain(best)
        for c in chosen:
            left -= cover.get(c, set())
    return chosen


def plan_minimal_naming(
    nodes_gdf: gpd.GeoDataFrame | None,
    edges_gdf: gpd.GeoDataFrame,
    streets_df: pd.DataFrame,
    max_levels: int = DEFAULT_MAX_BRANCH_LEVELS,
    targets=None,
    candidates=None,
    only_drivable: bool = False,
    second_level_max_drive_m: float | None = DEFAULT_SECOND_LEVEL_MAX_DRIVE_M,
    method: str = "auto",
    time_limit_s: float = 60.0,
    reference_node=None,
    use_length_rule: bool = True,
) -> NamingPlan:
    """Find the fewest streets to name by hand so every target street has a name or a derived name.

    Args:
        nodes_gdf: Nodes with ``node_id`` clusters, or None.
        edges_gdf: Edges after ``street_direction.assign_street_directionality`` (numbered main paths).
        streets_df: One row per street with ``street_id``, ``street_name`` and ``street_rank`` (``highway`` and
            ``length_m`` are reported when present).
        max_levels: Derivation levels allowed (2 = ``4-3-xyz``).
        targets: Street ids that must end up named; default: every street that can be named (drivable
            when ``only_drivable``).
        candidates: Street ids that may be chosen for hand naming; default: every street that can be named.
        only_drivable: Only drivable streets are named or derived (default False: every road, footpaths included, needs a name).
        second_level_max_drive_m: Second-level names (``4-3-xyz``) only for non-drivable roads and drivable roads shorter than this (m).
        method: ``"auto"``, ``"exact"`` or ``"greedy"`` (see the module docstring).
        time_limit_s: Time limit of the exact solver.
        reference_node: Node id of the reference point. When given, the named streets (existing and chosen) must be
            connected to it through named streets only (see the module docstring).
        use_length_rule: A derived street must be shorter than its main street.

    Returns:
        The :class:`NamingPlan`.

    Raises:
        ValueError: If ``method`` is not one of the three options.
    """
    if method not in ("auto", "exact", "greedy"):
        raise ValueError(f"method must be 'auto', 'exact' or 'greedy', got {method!r}")
    parents = branch_parents(nodes_gdf, edges_gdf, streets_df, only_drivable=only_drivable, use_length_rule=use_length_rule)
    children = _children(parents)
    deep_ok = second_level_streets(nodes_gdf, edges_gdf, streets_df, second_level_max_drive_m)
    info = streets_df.set_index("street_id")
    namable = set(parents)
    named = {int(s) for s, n in info["street_name"].items() if is_named(n)}
    target_set = set(namable if targets is None else (int(t) for t in targets)) - named
    cand = sorted((namable if candidates is None else {int(c) for c in candidates} & namable) - named)
    notes: list = []

    # ---- connectivity to the reference ------------------------------------------------------------------
    adjacent, snodes = street_adjacency(nodes_gdf, edges_gdf, streets_df, only_drivable=only_drivable)
    roots: list = []
    unconnectable: list = []
    named_conn = set()
    connect = reference_node is not None
    if connect:
        cluster = nodes_gdf["node_id"].to_dict() if nodes_gdf is not None and "node_id" in nodes_gdf else {}
        ref = cluster.get(reference_node, reference_node)
        roots = sorted(s for s, ns in snodes.items() if ref in ns)
        if not roots:
            notes.append("no drivable street touches the reference node: connectivity was not required")
            connect = False
        else:
            seen, queue = set(roots), deque(roots)
            while queue:
                u = queue.popleft()
                for v in adjacent.get(u, ()):
                    if v not in seen:
                        seen.add(v)
                        queue.append(v)
            unconnectable = sorted(named - seen)
            named_conn = (named & seen)
            cand = [c for c in cand if c in seen]
            if unconnectable:
                notes.append(f"{len(unconnectable)} named streets cannot reach the reference through drivable streets")

    existing = _reach(children, named, max_levels, deep_ok)
    remaining = {t for t in target_set if t not in existing}
    already = len(target_set) - len(remaining)

    rank = info["street_rank"].to_dict()
    length = info["length_m"].to_dict() if "length_m" in info else {}
    cover = {c: {t for t, (d, _) in _reach(children, {c}, max_levels, deep_ok).items() if t in remaining} for c in cand}
    uncoverable = sorted(t for t in remaining if not any(t in cover[c] for c in cand))
    solvable = remaining - set(uncoverable)
    cand_useful = [c for c in cand if cover[c] & solvable]
    cost = {c: 1.0 + 1e-4 * float(rank.get(c, 4)) - 1e-9 * float(length.get(c, 0) or 0) for c in cand}
    tie = {c: (-float(rank.get(c, 4)), float(length.get(c, 0) or 0)) for c in cand}
    need_root = connect and not (named_conn & set(roots))

    used, optimal, picked = "greedy", False, None
    if connect and (solvable or need_root or len(named_conn) > 1) and method in ("auto", "exact"):
        picked, proven = _solve_exact_connected(cand, cover, solvable, cost, adjacent, named_conn, roots, time_limit_s)
        if picked is not None:
            used, optimal = "exact", proven
            if not proven:
                notes.append("exact solver stopped at its time limit: the set is feasible but not proven minimum")
        elif method == "exact":
            notes.append("scipy is not available or the solve failed; fell back to greedy")
    elif not connect and solvable and method in ("auto", "exact"):
        picked, proven = _solve_exact(cand_useful, cover, solvable, cost, time_limit_s)
        if picked is not None:
            used, optimal = "exact", proven
            if not proven:
                notes.append("exact solver stopped at its time limit: the set is feasible but not proven minimum")
        elif method == "exact":
            notes.append("scipy is not available or the solve failed; fell back to greedy (pip install scipy / extra 'planning')")
    if picked is None:
        if connect:
            picked = _solve_greedy_connected(cand, cover, solvable, tie, adjacent, named_conn, roots)
        else:
            picked = _solve_greedy(cand_useful, cover, solvable, tie) if solvable else []
        optimal = False if (solvable or connect) else True

    # order: streets closer to the reference through named streets first, then by what they cover
    backbone = named | set(picked)
    depth_from_root: dict = {}
    if connect:
        sub = {s: (adjacent.get(s, set()) & (named_conn | set(picked))) for s in (named_conn | set(picked))}
        queue = deque(r for r in roots if r in sub)
        depth_from_root = {r: 0 for r in queue}
        while queue:
            u = queue.popleft()
            for v in sub[u]:
                if v not in depth_from_root:
                    depth_from_root[v] = depth_from_root[u] + 1
                    queue.append(v)
    left = set(solvable)
    pool = sorted(picked, key=lambda c: (depth_from_root.get(c, 0), -len(cover.get(c, ())), c))
    order = []
    while pool:
        level = min(depth_from_root.get(c, 0) for c in pool)
        layer = [c for c in pool if depth_from_root.get(c, 0) == level]
        c = max(layer, key=lambda x: (len(cover.get(x, set()) & left), tie[x]))
        order.append((c, len(cover.get(c, set()) & left)))
        left -= cover.get(c, set())
        pool.remove(c)
    chosen = pd.DataFrame({
        "order": range(1, len(order) + 1), "street_id": [c for c, _ in order],
        "street_rank": [rank.get(c) for c, _ in order], "highway": [info["highway"].get(c) if "highway" in info else None for c, _ in order],
        "length_m": [length.get(c) for c, _ in order],
        "street_name": [info["street_name"].get(c) for c, _ in order],
        "newly_covered": [g for _, g in order], "covers_total": [len(cover.get(c, ())) for c, _ in order],
        "reason": ["coverage" if g > 0 else "connection" for _, g in order],
        "depth_from_reference": [depth_from_root.get(c) for c, _ in order],
    })

    final = _reach(children, backbone, max_levels, deep_ok)
    rows = []
    chosen_ids = {c for c, _ in order}
    for t in sorted(target_set | (named & set(streets_df["street_id"].astype(int)) if targets is None else set())):
        if t in final:
            depth, root = final[t]
            src = "planned" if root in chosen_ids and t not in existing else "existing"
            rows.append({"street_id": t, "root": root, "depth": depth, "source": src})
        else:
            rows.append({"street_id": t, "root": None, "depth": None, "source": "uncovered"})
    return NamingPlan(chosen, pd.DataFrame(rows), uncoverable, len(target_set), already, used, optimal, max_levels, notes,
                      backbone, unconnectable, roots)
