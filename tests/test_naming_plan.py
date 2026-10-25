"""Tests for the minimum-naming planner."""

import itertools

import numpy as np
import pandas as pd
import pytest

from osmaddresssystem import branches, naming_plan
from osmaddresssystem.naming_plan import _reach, _solve_exact, _solve_greedy

from test_branch_names import _network


def _plan(max_levels=2, **kw):
    """Plan on the synthetic branch network.

    Args:
        max_levels: Derivation levels.
        **kw: Extra planner arguments.

    Returns:
        ``(plan, edges, streets)``.
    """
    e, s = _network()
    return naming_plan.plan_minimal_naming(None, e, s, max_levels=max_levels, **kw), e, s


def test_reach_follows_branches_up_to_the_allowed_levels():
    """Depth-limited breadth-first reach from named roots."""
    children = {1: {2, 3}, 2: {4}, 4: {5}}
    assert _reach(children, {1}, 2) == {1: (0, 1), 2: (1, 1), 3: (1, 1), 4: (2, 1)}
    assert set(_reach(children, {1}, 3)) == {1, 2, 3, 4, 5}
    assert set(_reach(children, {1}, 0)) == {1}


def test_plan_on_the_synthetic_network_names_the_streets_that_cannot_be_derived():
    """Street 7 is more important than any named road and street 5 is three levels deep: both need help."""
    plan, e, s = _plan()
    assert plan.method in ("exact", "greedy") and plan.optimal is True
    ids = set(plan.chosen["street_id"])
    assert 7 in ids                                              # no parent can be at least as important
    assert len(ids) == 2 and ids & {2, 4, 5}                    # one more name brings street 5 within two levels
    assert plan.n_targets == 6 and plan.n_already_named == 4    # 2, 3, 4 derive from xyz and so does street 8: footpaths need names too
    assert plan.uncovered == []
    assert (plan.coverage["depth"] <= 2).all()


def test_more_levels_need_fewer_names():
    """The number of hand-named streets never grows with the allowed derivation depth."""
    n = {lv: len(_plan(max_levels=lv)[0].chosen) for lv in (0, 1, 2, 3)}
    # no derivation: all five targets need a name; one level: 4 (also covers 5) and 7; two levels: 7 and one of
    # 2/4/5; three levels: only 7 (street 5 is then a third-level road)
    assert n == {0: 6, 1: 2, 2: 2, 3: 1}


def test_naming_the_chosen_streets_makes_branch_naming_name_every_target():
    """End to end: with the chosen names added, assign_branch_names gives every target a name."""
    for method in ("greedy", "exact"):
        plan, e, s = _plan(method=method)
        named = s.copy()
        for sid in plan.chosen["street_id"]:
            named.loc[named["street_id"] == sid, "street_name"] = f"planned{sid}"
        _, out = branches.assign_branch_names(None, e, named, max_levels=2)
        targets = set(plan.coverage["street_id"])
        got = out.set_index("street_id")["display_name"]
        assert all(isinstance(got[t], str) and got[t] for t in targets), (method, got.to_dict())
        depth = dict(zip(plan.coverage["street_id"], plan.coverage["depth"]))
        lvl = out.set_index("street_id")["branch_level"]
        assert all(pd.isna(lvl[t]) or lvl[t] <= 2 for t in targets) and max(depth.values()) <= 2


def test_restricted_candidates_can_leave_targets_uncovered():
    """If only street 2 may be named, street 7 can never be covered and is reported."""
    plan, _, _ = _plan(candidates=[2])
    assert 7 in plan.uncovered and set(plan.chosen["street_id"]) <= {2}


def test_exact_matches_brute_force_and_greedy_is_feasible_on_random_set_systems():
    """On random instances the exact solver finds the true minimum and greedy always covers everything."""
    rng = np.random.default_rng(3)
    for trial in range(40):
        n_t, n_c = int(rng.integers(4, 12)), int(rng.integers(3, 10))
        targets = set(range(n_t))
        cover = {100 + j: {int(t) for t in targets if rng.random() < 0.35} for j in range(n_c)}
        cover[100] |= {int(t) for t in targets if t not in set().union(*cover.values())}   # make it feasible
        cands = sorted(cover)
        cost = {c: 1.0 for c in cands}
        exact, proven = _solve_exact(cands, cover, targets, cost, 20)
        best = min(k for k in range(1, len(cands) + 1)
                   if any(set().union(*(cover[c] for c in combo)) >= targets for combo in itertools.combinations(cands, k)))
        assert proven and len(exact) == best, (trial, len(exact), best)
        greedy = _solve_greedy(cands, cover, targets, {c: (0, 0) for c in cands})
        assert set().union(*(cover[c] for c in greedy)) >= targets and len(greedy) >= best


def test_bad_method_is_rejected():
    """Only auto, exact and greedy are accepted."""
    e, s = _network()
    with pytest.raises(ValueError):
        naming_plan.plan_minimal_naming(None, e, s, method="magic")


def test_second_level_names_only_for_footpaths_and_short_drive_roads():
    """With a tiny limit the drivable street 4 (40 m, second level) can no longer be derived, so the plan has to name more streets."""
    base, _e, _s = _plan()
    strict, _e2, _s2 = _plan(second_level_max_drive_m=1.0)
    assert set(strict.chosen["street_id"]) != set(base.chosen["street_id"]) and 4 in set(strict.chosen["street_id"])   # street 4 needs its own name
