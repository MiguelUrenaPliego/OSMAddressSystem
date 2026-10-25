"""Tests for naming unnamed roads after the named road they branch from."""

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from osmaddresssystem import branches

# Node coordinates of the synthetic network (metres, projected CRS).
NODES = {
    "A": (0, 0), "B": (40, 0), "C": (100, 0), "D": (200, 0),
    "E": (40, 100), "G": (40, 60), "F": (100, -100), "H": (0, 60), "I": (0, 80),
    "J": (-60, 0), "K": (200, 50), "L": (200, -50),
}


def _network():
    """Build a small addressed network.

    The named main road ``xyz`` (street 1, east, 0-200 m) has a road leaving north at 40 m
    (street 2), one leaving south at 100 m (street 3), a road leaving 2's 60 m point west
    (street 4) and a road leaving 4 north (street 5). Every derived street is shorter than its parent. Street 6 has a name of its own,
    street 7 is more important than ``xyz`` and street 8 is a footpath.

    Returns:
        ``(edges_gdf, streets_df)`` as produced after ``assign_street_directionality``.
    """
    spec = [  # (street, u, v, start, end)
        (1, "A", "B", 0, 40), (1, "B", "C", 40, 100), (1, "C", "D", 100, 200),
        (2, "B", "G", 0, 60), (2, "G", "E", 60, 100),
        (3, "C", "F", 0, 100),
        (4, "G", "H", 0, 40),
        (5, "H", "I", 0, 20),
        (6, "A", "J", 0, 60),
        (7, "D", "K", 0, 50),
        (8, "D", "L", 0, 50),
    ]
    rows = []
    for sid, u, v, a, b in spec:
        rows.append({"u": u, "v": v, "key": 0, "street_id": sid, "is_main_direction": True,
                     "street_start_length_m": a, "street_end_length_m": b, "is_drivable": sid != 8,
                     "street_pos_u_m": a, "street_pos_v_m": b,
                     "geometry": LineString([NODES[u], NODES[v]])})
    edges = gpd.GeoDataFrame(rows, crs=32632).set_index(["u", "v", "key"])
    streets = pd.DataFrame({
        "street_id": range(1, 9),
        "street_name": ["xyz", None, "", None, None, "abc", None, None],
        "street_rank": [1, 4, 4, 4, 4, 4, 0, 4],
    })
    return edges, streets


def _names(max_levels=2, only_drivable=False):
    """Run the branch naming on the synthetic network.

    Args:
        max_levels: Maximum number of branch prefixes.
        only_drivable: Skip footpaths.

    Returns:
        Mapping street id -> derived name (None when unnamed).
    """
    e, s = _network()
    _, out = branches.assign_branch_names(None, e, s, max_levels=max_levels, only_drivable=only_drivable)
    return {k: (v if isinstance(v, str) else None) for k, v in out.set_index("street_id")["branch_name"].items()}


def test_left_branch_at_40m_is_3_xyz():
    """A road leaving on the left at 40 m is called 3-xyz."""
    assert _names()[2] == "3-xyz"


def test_right_branch_at_100m_is_even():
    """A road leaving on the right at 100 m gets the even number 6."""
    assert _names()[3] == "6-xyz"


def test_second_level_branch_prefixes_again():
    """A road leaving 3-xyz is named with a second prefix (3-3-xyz)."""
    assert _names()[4] == "3-3-xyz"


def test_max_levels_limits_the_prefixes():
    """The third level stays unnamed by default and is named with max_levels=3."""
    assert _names()[5] is None
    assert _names(max_levels=3)[5] == "2-3-3-xyz"


def test_named_roads_and_more_important_roads_are_not_renamed():
    """A street with its own name, and a street more important than the parent, keep no branch name."""
    n = _names()
    assert n[6] is None and n[7] is None and n[1] is None


def test_footpaths_are_named_too_unless_only_drivable_is_requested():
    """Every road needs a name: footpaths are derived by default and skipped only when asked."""
    assert _names()[8] == "10-xyz"
    assert _names(only_drivable=True)[8] is None


def test_second_level_names_need_a_footpath_or_a_short_drive_road():
    """Street 4 is a drivable road of 40 m: it takes 3-3-xyz, but not when the limit for drive roads is 1 m."""
    e, s = _network()
    names = lambda lim: branches.assign_branch_names(None, e, s, second_level_max_drive_m=lim)[1].set_index("street_id")["branch_name"]  # noqa: E731
    assert names(500.0)[4] == "3-3-xyz" and not isinstance(names(1.0)[4], str)
    assert branches.second_level_streets(None, e, s, 1.0) == {8}                 # only the footpath is always allowed


def test_nothing_is_named_below_an_unnamed_road():
    """Without any named parent no branch name is produced."""
    e, s = _network()
    s["street_name"] = None
    _, out = branches.assign_branch_names(None, e, s)
    assert out["branch_name"].isna().all()


def test_display_name_prefers_own_name():
    """display_name is the own name when present, else the branch name."""
    e, s = _network()
    _, out = branches.assign_branch_names(None, e, s)
    d = out.set_index("street_id")["display_name"]
    assert d[1] == "xyz" and d[6] == "abc" and d[2] == "3-xyz"


def test_crowded_slot_gets_letters():
    """Two roads leaving the same side of the parent in one 20 m slot become 3A-xyz and 3B-xyz."""
    e, s = _network()
    extra = e.loc[[("B", "G", 0)]].copy()
    extra.index = pd.MultiIndex.from_tuples([("C0", "M", 0)], names=["u", "v", "key"])
    # A second road leaving the parent at 44 m on the left (same slot as 40 m).
    extra["street_id"] = 9
    extra["geometry"] = [LineString([(44, 0), (44, 80)])]
    extra["street_start_length_m"], extra["street_end_length_m"] = 0, 80
    extra["street_pos_u_m"], extra["street_pos_v_m"] = 0, 80
    e2 = pd.concat([e, extra])
    e2 = gpd.GeoDataFrame(e2, geometry="geometry", crs=32632)
    s2 = pd.concat([s, pd.DataFrame({"street_id": [9], "street_name": [None], "street_rank": [4]})], ignore_index=True)
    # The new road touches the parent at node "C0": add that node to the parent as an extra edge split.
    parent = e2.loc[[("B", "C", 0)]].copy()
    e2 = e2.drop(index=[("B", "C", 0)])
    a = parent.copy(); a.index = pd.MultiIndex.from_tuples([("B", "C0", 0)], names=["u", "v", "key"])
    a["geometry"] = [LineString([(40, 0), (44, 0)])]; a["street_start_length_m"], a["street_end_length_m"] = 40, 44
    a["street_pos_u_m"], a["street_pos_v_m"] = 40, 44
    b = parent.copy(); b.index = pd.MultiIndex.from_tuples([("C0", "C", 0)], names=["u", "v", "key"])
    b["geometry"] = [LineString([(44, 0), (100, 0)])]; b["street_start_length_m"], b["street_end_length_m"] = 44, 100
    b["street_pos_u_m"], b["street_pos_v_m"] = 44, 100
    e2 = gpd.GeoDataFrame(pd.concat([e2, a, b]), geometry="geometry", crs=32632)
    _, out = branches.assign_branch_names(None, e2, s2)
    n = out.set_index("street_id")["branch_name"]
    assert n[2] == "3A-xyz" and n[9] == "3B-xyz"


def test_branch_depth_understands_letters():
    """A prefix with a letter counts as one level."""
    assert branches.branch_depth("4A-3B-xyz") == 2 and branches.branch_depth("xyz") == 0


def test_orientation_comes_from_node_positions_not_from_u_v_order():
    """Reversed directed copies (v -> u) give the same names as the numbering-aligned ones."""
    e, s = _network()
    rev = e.copy()
    rev.index = pd.MultiIndex.from_arrays([rev.index.get_level_values("v"), rev.index.get_level_values("u"),
                                           rev.index.get_level_values("key")], names=["u", "v", "key"])
    rev["geometry"] = rev.geometry.reverse()
    rev["street_pos_u_m"], rev["street_pos_v_m"] = e["street_pos_v_m"].to_numpy(), e["street_pos_u_m"].to_numpy()
    both = pd.concat([rev, e])           # the reversed copy comes first, as in real data
    _, out = branches.assign_branch_names(None, both, s)
    n = {k: (v if isinstance(v, str) else None) for k, v in out.set_index("street_id")["branch_name"].items()}
    assert n[2] == "3-xyz" and n[3] == "6-xyz" and n[4] == "3-3-xyz"
