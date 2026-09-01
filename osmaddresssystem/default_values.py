"""Every tunable hyperparameter default used across osmaddresssystem, in
one place.

Import from here rather than hardcoding a literal at a call site or
function signature, so every knob's default value lives in exactly one
place and stays easy to find and retune later. Every value here remains
fully overridable by passing the corresponding function argument -
nothing here is enforced; it's only where the defaults come from.

This module has no dependencies on the rest of the package (and must
stay that way, to avoid circular imports - name_matching.py,
street_ids.py, and street_direction.py all import from here).
"""

from __future__ import annotations

# --- name_matching.py: names_match/name_similarity ---

# Minimum name_similarity score (0-100) for two names to count as a
# fuzzy match.
DEFAULT_NAME_SIMILARITY_THRESHOLD = 82.0


# --- street_ids.py: highway rank tiers ---

# Lower number = higher priority (more likely to keep its identity/name
# across an intersection). A "_link" variant (a slip road/ramp/connector
# for its base type, e.g. motorway_link for motorway) shares its base
# type's rank rather than sitting a tier below it: a motorway on-ramp is
# still part of the motorway network, not a lesser road in its own right.
#
# Six tiers: trunk, primary, secondary, tertiary, residential/service,
# footway. `motorway`/`motorway_link` share rank 0 with trunk - there's
# no separate tier for it in this scheme, but it's clearly the most
# major road type there is, so it belongs at the very top alongside
# trunk rather than being folded into a lower tier.
#
# Rank 4 ("residential/service") is also DEFAULT_RANK, the fallback for
# any highway value not explicitly listed here: an unrecognized value is
# almost certainly some minor/local road variant, so falling back into
# this drivable-but-minor tier - rather than the bottom "footway" tier -
# matches the existing "unrecognized/missing treated permissively as
# drivable" philosophy documented for `is_drivable` below.
#
# Rank 5 ("footway") is deliberately exactly DEFAULT_NON_DRIVABLE_
# HIGHWAYS's own set (built from it directly below, not duplicated) -
# every highway type that's "footway rank" is exactly the set that's
# also non-drivable, and vice versa.
DEFAULT_HIGHWAY_RANKS = {
    "motorway": 0,
    "motorway_link": 0,
    "trunk": 0,
    "trunk_link": 0,
    "primary": 1,
    "primary_link": 1,
    "secondary": 2,
    "secondary_link": 2,
    "tertiary": 3,
    "tertiary_link": 3,
    "unclassified": 4,
    "residential": 4,
    "living_street": 4,
    "service": 4,
    "road": 4,
    # rank 5 ("footway") entries are appended below, derived from
    # DEFAULT_NON_DRIVABLE_HIGHWAYS, so the two sets can never drift
    # apart.
}
DEFAULT_RANK = 4  # fallback for unrecognized highway types


# --- street_signs.py: compute_intersections / build_street_signs_map ---

# Groups DEFAULT_HIGHWAY_RANKS' numeric street_rank into the four road
# categories street_signs.py classifies intersections by. Any street_rank
# not listed under "primary"/"secondary"/"tertiary" here (e.g. 4:
# unclassified/residential/service, 5: footway) falls back to "other".
# User-configurable: pass a differently-shaped dict of the same three
# keys to street_signs.rank_to_category/compute_intersections to change
# where the category boundaries fall.
DEFAULT_INTERSECTION_CATEGORY_RANKS = {
    "primary": {0, 1},
    "secondary": {2},
    "tertiary": {3},
}


# --- street_ids.py: label_streets ---

# Streets within this many degrees of each other (undirected orientation,
# folded into 0-90 deg) are considered the same street when crossing an
# intersection with other equal-rank streets - rule 3. Used for the
# internal-connector direction match and as the general "is this the
# same direction" tolerance outside of node-level binning - see
# DEFAULT_NODE_DIRECTION_TOLERANCE_DEG for the (looser) node-binning one.
DEFAULT_ANGLE_THRESHOLD_DEG = 10.0

# Turn deviation (deg, directed - see street_ids._raw_turn_deviation_deg)
# between two consecutive main-path edges at a shared node_id beyond which
# a street_id is split into two for numbering purposes - step 5,
# _split_main_path_at_turns. 0 means the main path keeps going perfectly
# straight, 180 means it fully reverses. Kept separate from
# DEFAULT_ANGLE_THRESHOLD_DEG on purpose: that threshold decides whether
# two edges share an OSM identity across an intersection, a much stricter
# question than whether an already-identified main path should still be
# numbered as one continuous line through a real corner. Default 90 deg
# only splits at a turn sharper than a right angle, roughly a genuine
# corner rather than a gentle curve or normal digitization noise.
DEFAULT_MAIN_PATH_ANGLE_THRESHOLD_DEG = 90.0

# Maximum folded angle difference (deg) between two external edges'
# tangents AT A SHARED NODE_ID for them to be grouped into the same
# direction bin there (phase 2 of _assign_external_street_ids_for_pass),
# and for the length-based reassignment's own endpoint direction match
# (_reassign_short_streets). Deliberately looser than
# DEFAULT_ANGLE_THRESHOLD_DEG: two edges meeting at one real point are
# allowed a wider bend than two edges being judged "the same direction"
# in general (e.g. the internal-connector match, or the parallel-pass
# pre-filter), since node geometry digitization noise is largest right
# at the node itself.
DEFAULT_NODE_DIRECTION_TOLERANCE_DEG = 30.0

# Phase 2's own per-node_id direction assignment (above) already checks
# that two edges' LOCAL tangents at the shared node agree (within
# DEFAULT_NODE_DIRECTION_TOLERANCE_DEG); the union() helper it then calls
# separately re-checks each pair's own WHOLE-EDGE bearing, which exists
# to reject a genuine fork (two edges whose tangents happen to look
# aligned right at the node, then immediately diverge for good - see the
# node-binning regression test in tests/test_street_ids.py). That same
# whole-edge check also rejects a real, gently curving street whenever
# it runs long/curved enough for its own overall bearing to drift past
# tolerance, even though every individual step along it stayed
# straight - a real bug found via real-data testing (two collinear,
# unnamed, same-rank edges meeting cleanly at one node, ~4 deg apart in
# local tangent, refused merging because one of them curved gently over
# its own ~90m and drifted ~30 deg in overall bearing by the far end).
#
# The fix distinguishes the two cases by how much of the LONGER edge's
# own length actually stays close to the straight tangent line from the
# node: a fork diverges almost immediately (low coverage - the test
# regression's synthetic fork covers ~25% at a 15m corridor), a genuine
# curve stays close for most of its own length (the real bug's own
# curving street covers ~81% at the same corridor) - see
# street_ids._tangent_continuation_fraction. This threshold is
# deliberately lenient (not a strict "must hug the tangent almost the
# whole way", and the corridor width used is whatever touching_dist_m
# the caller already passed for the branch-drift check, itself already a
# generous per-node tolerance) - the real-world cases so far separate by
# a wide margin either side of 0.5, so there's no need to demand more.
DEFAULT_TANGENT_CONTINUATION_FRACTION = 0.5

# How far along the LONGER edge, from the shared node, to measure
# _tangent_continuation_fraction's own coverage over - the rest of a
# long edge (past this distance) is ignored entirely, not just
# de-weighted. Demanding coverage over an edge's FULL length instead
# breaks down for real, long, gently curving streets (several hundred
# meters, still gently curving the whole way): even fully ordinary
# curvature exceeds any fixed corridor width once accumulated over
# enough distance, which is exactly the false-negative this whole
# mechanism exists to avoid. Calibrated against real data: a genuine
# fork (this module's own synthetic regression test) covers ~25-65% of
# this window depending on exactly how it's sized, while every real
# curving-street case found so far covers at least ~64%, and the
# window itself is generous enough that a genuine local fork right at
# the node still shows up clearly within it.
DEFAULT_TANGENT_CONTINUATION_WINDOW_M = 100.0

# How many equal-width bins to split each node_id's own [0, 180)
# direction range into for phase 2's per-node_id direction assignment
# (default 6, i.e. 30 deg per bin - 12 bins once the opposite/
# continuation direction is counted too, matching
# DEFAULT_NODE_DIRECTION_TOLERANCE_DEG's own 30 deg). See
# report/street_identity.md "No more than one direction per node_id".
DEFAULT_NUM_NODE_DIRECTIONS = 6

# Graph nodes closer than this (meters, in the graph's own CRS units)
# are treated as the same real intersection - see "Node clustering".
DEFAULT_NODE_CLUSTER_M = 10.0

# Distance (meters) within which two edges are considered "close" for
# the parallel-street merge/strip features. Disabled (0) by default -
# opt in per call.
DEFAULT_PARALLEL_DIST_M = 10.0

# Maximum difference (degrees, undirected/folded) between two edges'
# overall bearings for the parallel-street merge to consider them
# "parallel".
DEFAULT_PARALLEL_ANGLE_THRESHOLD_DEG = 20.0

# Maximum distance (meters), measured between two edges' own CENTRAL
# segments (not their whole geometries - see DEFAULT_CENTER_TANGENT_
# FRACTIONS), for the final street_id-level parallel pass to join two
# street_ids that already TOUCH (share a raw node). See "Parallel pass:
# joining touching or nearby street_ids" in report/street_identity.md.
DEFAULT_PARALLEL_PASS_TOUCHING_DIST_M = 15.0

# Same as DEFAULT_PARALLEL_PASS_TOUCHING_DIST_M, but for two street_ids
# that do NOT touch at all - deliberately tighter, since there's no
# shared node backing up the "same street" judgment, only proximity and
# direction.
DEFAULT_PARALLEL_PASS_NON_TOUCHING_DIST_M = 5.0

# Maximum number of hops through the street graph within which a
# parallel-merge/strip candidate's endpoints must be reachable from each
# other.
DEFAULT_PARALLEL_MAX_HOPS = 8

# Maximum folded angle difference (deg) between two edges' tangents at a
# shared node for _enforce_max_two_per_node to treat them as a straight
# through-continuation - deliberately more generous than
# DEFAULT_ANGLE_THRESHOLD_DEG since it's a last-resort tie-break at an
# already-anomalous node, not the primary rule-3 same-street decision.
DEFAULT_FORK_ANGLE_THRESHOLD_DEG = 40.0

# How much worse (deg) than the single straightest qualifying pair at a
# >2-edge node another pair's own angle diff may be and still be treated
# as "roughly equally straight" for the length tie-break in
# _enforce_max_two_per_node.
DEFAULT_FORK_NEAR_TIE_MARGIN_DEG = 10.0

# highway types no car can legally/physically drive on. Everything else
# (including unrecognized/missing highway values, treated permissively)
# counts as drivable.
DEFAULT_NON_DRIVABLE_HIGHWAYS = {
    "footway",
    "path",
    "steps",
    "pedestrian",
    "cycleway",
    "bridleway",
    "corridor",
    "track",
}

# Rank 5 ("footway") is exactly DEFAULT_NON_DRIVABLE_HIGHWAYS - derived
# here, not duplicated as a second, separately-maintained literal list,
# so the two sets can never drift apart (a highway type that's "footway
# rank" is always exactly the set that's also non-drivable).
DEFAULT_HIGHWAY_RANKS.update({highway: 5 for highway in DEFAULT_NON_DRIVABLE_HIGHWAYS})

# OSM access values that mean "not a real public street" - a private
# driveway/access road, even if physically drivable and otherwise
# indistinguishable from a normal residential street, isn't part of the
# public street network and should never end up with a street_id.
DEFAULT_PRIVATE_ACCESS_VALUES = {"private"}

# OSM `service` tag values that mean "this is inside a parking lot, not
# a real street" - a parking aisle is tagged `highway=service` with
# `service=parking_aisle`, and is physically/legally just like a private
# driveway even though it's not marked `access=private`: not part of the
# public street network, no matter how it's otherwise tagged. Mirrors
# DEFAULT_PRIVATE_ACCESS_VALUES exactly, just keyed off the `service`
# tag instead of `access`.
DEFAULT_PARKING_SERVICE_VALUES = {"parking_aisle"}

# Share of a whole street_id's own combined length that must be covered
# (per-edge, within parallel_dist_m and parallel_max_hops of a longer
# street_id) for it to be stripped entirely. Not quite a literal 1.0
# (100%) to tolerate floating-point/geometry-union noise at the edges.
DEFAULT_STREET_ABSORB_COVERAGE_FRACTION = 0.999

# The two interpolation fractions the "is this edge parallel to that
# one" check reads its direction between - the tangent at the edge's
# own center, not its noise-prone endpoints.
DEFAULT_CENTER_TANGENT_FRACTIONS = (0.4, 0.6)

# OSM's boolean-ish tag values (tunnel, bridge, ...) that count as
# "false"/unset. Anything else counts as set.
OSM_FALSY_TAG_VALUES = {"no", "false", "0", ""}

# A street_id whose own main-direction path (Rule 3's weighted-diameter
# path, computed by _compute_main_direction) is shorter than this
# (meters) is "too short": every one of its edges - main-path and
# non-main alike - is individually reassigned to whichever other,
# eligible street_id is geometrically nearest to it and itself has a
# valid (>= this) main-direction path length. See "Redistributing
# too-short streets" in report/street_identity.md.
DEFAULT_MIN_STREET_LENGTH_M = 50.0

# A non-drivable edge (footway, cycleway, ...) whose own length falls
# within the parallel-pass distance thresholds of the drivable network
# for at least this fraction of its length is donated the drivable
# street_id with the largest total nearby length directly - see
# "Donating a drivable street's id to nearby non-drivable furniture" in
# report/street_identity.md.
DEFAULT_NON_DRIVABLE_COVERAGE_FRACTION = 0.9

# A node's candidate-matching workload is at most O(degree^2), and
# street graph nodes almost never have more than a handful of incident
# edges, so per-node work is tiny. Below this many nodes, process-pool
# startup cost dwarfs the work itself - only parallelize past this size.
DEFAULT_PARALLEL_NODE_THRESHOLD = 20_000


# --- street_direction.py: assign_street_directionality ---

# Percentage of a street's own length its two endpoints' distance-from-
# origin difference must be under, for that street to be classified
# "transversal" (radial mode) rather than "radial".
DEFAULT_TRANSVERSAL_ROAD_PCT = 10.0

# "radial" (classify/number relative to an origin point) or "grid"
# (classify/number relative to a fixed compass direction).
DEFAULT_NUMBERING_MODE = "radial"

# Compass bearing (0 = north, clockwise) grid mode's "primary" axis runs
# along.
DEFAULT_DIRECTION_DEG = 0.0


# --- numbering.py: assign_house_numbers/add_admin_columns/geocode_points ---

# Meters of cumulative street length assigned to each house number step,
# per SIDE of the street (see "Left/right numbering" in
# report/street_numbering.md) - even numbers on the right, odd on the
# left, each side's own numbers a fixed number_divisor_m apart along the
# street, looking in the direction of travel (the street's own numbering
# direction - see street_direction.assign_street_directionality). Twice
# DEFAULT_NUMBER_DIVISOR_M's old (single-sequence) value, since a real
# address scheme's two interleaved sequences (evens/odds) reach the same
# numbering density per side as one unified sequence used to.
DEFAULT_NUMBER_DIVISOR_M = 20.0

# Administrative levels tracked by add_admin_columns/geocode_points, from
# coarsest to finest. Each is filled from whichever polygon in the AOI ->
# subregion chain was resolved at (or nearest to) that level, and left
# None if no polygon in the chain matches it, rather than repeating a
# coarser level's value into a finer column.
DEFAULT_ADMIN_LEVELS = ("country", "province", "city", "neighbourhood")


# --- subregions.py: find_subregions ---

# place=* values, one level finer than a town/city boundary, that
# find_subregions treats as candidate subregions.
DEFAULT_SUBREGION_PLACE_VALUES = ("suburb", "quarter", "neighbourhood", "village")

# Minimum fraction of a candidate subregion's own area that must fall
# inside the AOI for it to count as a genuine subdivision, rather than a
# coarser boundary the AOI itself sits inside, or a neighbouring same-
# level unit that only touches the AOI's own border.
DEFAULT_SUBREGION_CONTAINMENT_FRACTION = 0.5

# Minimum fraction of the AOI's own area that its found subregions must
# jointly cover for them to be used at all. OSM's own admin/place
# boundaries are rarely mapped exhaustively - a large city might have
# only two of its five real districts mapped as polygons. Splitting on
# those two would silently drop every street in the other three, so
# find_subregions only splits when its candidates jointly account for at
# least this much of the AOI. Below that, the AOI is processed whole
# instead - a caller always gets full coverage, at the cost of a coarser
# split when OSM's own data is itself incomplete.
DEFAULT_SUBREGION_COVERAGE_FRACTION = 0.95
