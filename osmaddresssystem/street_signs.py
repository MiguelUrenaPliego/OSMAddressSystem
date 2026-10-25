"""Street-sign map: which physical sign should exist at each intersection.

Classifies every street into a road **category** (primary/secondary/
tertiary/other, derived from ``street_rank`` - see
``DEFAULT_INTERSECTION_CATEGORY_RANKS``), then every real intersection
(one of ``street_ids.label_streets``' own clustered ``node_id``s touched
by 2+ distinct streets) into an **intersection type**, based on which
categories meet there:

    categories present          label                 rank
    -------------------------------------------------------
    {primary}                   primary-primary        0
    {primary, ...}              primary-secondary       1
    {secondary, tertiary/other} secondary-tertiary      2
    {secondary}                 secondary-secondary     3
    {tertiary, other}           tertiary-other          4
    {tertiary}                  tertiary-tertiary        5
    {other}                     other-other             6

("..." above means "anything worse than primary" - the intersection's
own label only ever names the top category present and the next one
down, regardless of exactly how many worse categories are mixed in.)

A real street sign at a crossing only ever names the road(s) a driver
on the MORE important road wouldn't already know they're on - so by
default only the lower-category streets are listed. The exception is a
"pure" intersection (every road meeting there is the same category,
e.g. primary-primary or secondary-secondary): there's no "more
important" road to omit, so every street gets listed.

``compute_intersections`` does the classification; ``build_street_signs_map``
renders it as a standalone folium map: streets colored by category,
one dot per intersection colored by intersection rank, click a dot for
the sign's own contents.
"""

from __future__ import annotations

from collections import defaultdict

import folium
import geopandas as gpd
import pandas as pd
from shapely.ops import linemerge

from .default_values import DEFAULT_INTERSECTION_CATEGORY_RANKS
from .street_editor import _new_map

_CATEGORY_ORDER = ("primary", "secondary", "tertiary", "other")

# label/rank for a "pure" (single-category) vs. "mixed" (top category
# plus something worse) intersection, keyed by the TOP category present.
_INTERSECTION_TYPES = {
    "primary": {"pure": ("primary-primary", 0), "mixed": ("primary-secondary", 1)},
    "secondary": {"pure": ("secondary-secondary", 3), "mixed": ("secondary-tertiary", 2)},
    "tertiary": {"pure": ("tertiary-tertiary", 5), "mixed": ("tertiary-other", 4)},
    "other": {"pure": ("other-other", 6), "mixed": ("other-other", 6)},
}

# Deliberately darker than a "textbook" red/orange/yellow/gray road-rank
# palette - besides reading better against a hybrid satellite basemap,
# these same colors back the white text on each street's own nameplate
# in a sign popup (see _sign_board_html), where a bright yellow in
# particular gave poor contrast.
_CATEGORY_STYLE = {
    "primary": {"color": "#a10d0d", "weight": 6, "label": "Primary"},
    "secondary": {"color": "#b35900", "weight": 5, "label": "Secondary"},
    "tertiary": {"color": "#a68b00", "weight": 4, "label": "Tertiary"},
    "other": {"color": "#616161", "weight": 2, "label": "Other"},
}

_INTERSECTION_RANK_STYLE = {
    0: {"color": "#800026", "label": "0 – primary-primary"},
    1: {"color": "#e31a1c", "label": "1 – primary-secondary"},
    2: {"color": "#fd8d3c", "label": "2 – secondary-tertiary"},
    3: {"color": "#feb24c", "label": "3 – secondary-secondary"},
    4: {"color": "#41b6c4", "label": "4 – tertiary-other"},
    5: {"color": "#225ea8", "label": "5 – tertiary-tertiary"},
    6: {"color": "#525252", "label": "6 – other-other"},
}


def rank_to_category(
    street_rank, category_ranks: dict[str, set] | None = None
) -> str:
    """Map a numeric ``street_rank`` to a road category name.

    Args:
        street_rank: A street's ``street_rank`` (see
            ``default_values.DEFAULT_HIGHWAY_RANKS``), or ``None``/NaN.
        category_ranks: ``{"primary": {...ranks...}, "secondary": {...},
            "tertiary": {...}}`` - any rank not listed under one of
            these three keys is "other". Defaults to
            ``DEFAULT_INTERSECTION_CATEGORY_RANKS``.

    Returns:
        ``"primary"``, ``"secondary"``, ``"tertiary"``, or ``"other"``.
    """
    category_ranks = category_ranks or DEFAULT_INTERSECTION_CATEGORY_RANKS
    if pd.isna(street_rank):
        return "other"
    rank = int(street_rank)
    for cat in ("primary", "secondary", "tertiary"):
        if rank in category_ranks.get(cat, ()):
            return cat
    return "other"


def classify_intersection_type(categories) -> tuple[str, int]:
    """Classify an intersection from the set of road categories meeting there.

    Args:
        categories: An iterable of category names (``"primary"``,
            ``"secondary"``, ``"tertiary"``, ``"other"``) - one per
            distinct street touching the intersection.

    Returns:
        ``(label, rank)`` - see the module docstring's table.

    Raises:
        ValueError: If ``categories`` is empty.
    """
    categories = set(categories)
    if not categories:
        raise ValueError("categories is empty - not an intersection")
    top = next(cat for cat in _CATEGORY_ORDER if cat in categories)
    kind = "pure" if categories == {top} else "mixed"
    return _INTERSECTION_TYPES[top][kind]


def _street_label(street_id, street_name, street_rank=None) -> str:
    """A street's own sign text: its ``street_name`` if it has one, else
    ``"Street #<street_id>"`` - with its own ``street_rank`` appended in
    parentheses when given."""
    name_or_id = str(street_name) if pd.notna(street_name) and str(street_name).strip() else f"Street #{street_id}"
    if street_rank is None or pd.isna(street_rank):
        return name_or_id
    return f"{name_or_id} ({int(street_rank)})"


def compute_intersections(
    nodes_gdf: gpd.GeoDataFrame,
    edges_gdf: gpd.GeoDataFrame,
    streets_df: pd.DataFrame,
    category_ranks: dict[str, set] | None = None,
) -> gpd.GeoDataFrame:
    """One row per real intersection (node touched by 2+ distinct streets).

    Args:
        nodes_gdf: Nodes, indexed by ``osmid``, with a clustered
            ``node_id`` column (see ``street_ids.label_streets``).
        edges_gdf: Edges (u, v, key)-indexed, with a ``street_id`` column.
        streets_df: One row per ``street_id``, with ``street_rank`` and
            ``street_name`` columns.
        category_ranks: Passed through to ``rank_to_category``.

    Returns:
        A GeoDataFrame (point geometry, same CRS as ``nodes_gdf``) with
        columns: ``node_id``, ``intersection_label``, ``intersection_rank``,
        ``categories`` (sorted, ``;``-joined), ``sign_street_ids``
        (``;``-joined), ``sign_labels`` (list of str, one per signed
        street - its ``street_name`` if it has one else ``"Street
        #<street_id>"``, followed by its own ``street_rank`` in
        parentheses).
    """
    street_rank_map = streets_df.set_index("street_id")["street_rank"]
    street_name_map = streets_df.set_index("street_id")["street_name"]
    street_category = {
        sid: rank_to_category(rank, category_ranks) for sid, rank in street_rank_map.items()
    }

    edges = edges_gdf.reset_index()[["u", "v", "street_id"]].dropna(subset=["street_id"])
    edges["street_id"] = edges["street_id"].astype(int)

    node_id_map = nodes_gdf["node_id"]
    edges["node_u"] = edges["u"].map(node_id_map)
    edges["node_v"] = edges["v"].map(node_id_map)

    long_df = pd.concat(
        [
            edges[["node_u", "street_id"]].rename(columns={"node_u": "node_id"}),
            edges[["node_v", "street_id"]].rename(columns={"node_v": "node_id"}),
        ],
        ignore_index=True,
    ).dropna(subset=["node_id"]).drop_duplicates()
    long_df["node_id"] = long_df["node_id"].astype(nodes_gdf["node_id"].dtype)

    node_to_streets: dict = defaultdict(set)
    for node_id, sid in zip(long_df["node_id"], long_df["street_id"]):
        node_to_streets[node_id].add(int(sid))

    # Representative geometry per cluster: the cluster leader itself if
    # still present, else any other member of the cluster (same
    # real-world point either way).
    rep_geom = nodes_gdf.groupby("node_id")["geometry"].first()

    records = []
    for node_id, sids in node_to_streets.items():
        if len(sids) < 2:
            continue  # a plain pass-through node, not a real intersection
        geom = rep_geom.get(node_id)
        if geom is None:
            continue
        cats_by_street = {sid: street_category.get(sid, "other") for sid in sids}
        categories = set(cats_by_street.values())
        label, rank = classify_intersection_type(categories)
        if len(categories) == 1:
            sign_ids = sorted(sids)
        else:
            top = next(cat for cat in _CATEGORY_ORDER if cat in categories)
            sign_ids = sorted(sid for sid, cat in cats_by_street.items() if cat != top)
        records.append(
            {
                "node_id": node_id,
                "geometry": geom,
                "categories": ";".join(sorted(categories)),
                "intersection_label": label,
                "intersection_rank": rank,
                "sign_street_ids": ";".join(str(s) for s in sign_ids),
                "sign_labels": [
                    _street_label(sid, street_name_map.get(sid), street_rank_map.get(sid))
                    for sid in sign_ids
                ],
            }
        )

    return gpd.GeoDataFrame(records, geometry="geometry", crs=nodes_gdf.crs)


def _dedupe_icon_only(entries: list[dict]) -> list[dict]:
    """No repeated sign: among type-3 (icon-only) entries sharing the
    same icon at one sign, keep only the nearest - two different POIs
    with the same icon (e.g. two schools too far apart to have been
    merged by ``poi_signs.merge_nearby_pois``) would otherwise show the
    same icon twice in a row. Type 1/2 entries are shown by name, so a
    repeated icon there is never actually redundant - left untouched."""
    nearest_by_icon: dict[str, dict] = {}
    for e in entries:
        icon = e["poi"].icon
        if icon not in nearest_by_icon or e["distance_m"] < nearest_by_icon[icon]["distance_m"]:
            nearest_by_icon[icon] = e
    return list(nearest_by_icon.values())


def poi_signs_needed(entries: list[dict]) -> int:
    """Physical sign count for one street's POI entries at one intersection.

    A type-1 or type-2 entry (see ``poi_signs.py``'s module docstring)
    always needs its own plate, but every type-3 (icon-only) entry
    shares a SINGLE combined plate instead of one each - matching how a
    real signpost would group a cluster of "nearby amenity" icons into
    one panel rather than a plate per icon.
    """
    if not entries:
        return 0
    count = sum(1 for e in entries if e["sign_type"] in (1, 2))
    if any(e["sign_type"] == 3 for e in entries):
        count += 1
    return count


def _poi_row_html(text: str) -> str:
    """HTML of one POI row on a street sign board.

    Args:
        text: Row text.

    Returns:
        The HTML.
    """
    return (
        "<div style='background:#0b4a24;color:#fff;padding:5px 10px;margin-top:3px;"
        "border:1px solid #000;border-radius:2px;font-size:12px;"
        "display:flex;justify-content:space-between;align-items:center;'>"
        f"<span>{text}</span><span style='margin-left:8px;'>&#9654;</span></div>"
    )


def _sign_board_html(label: str, category: str, poi_entries: list[dict] | None = None) -> str:
    """One street's own sign: a nameplate, plus (if any) a row per POI
    direction entry - type 1/2 entries get their own row (name + icon,
    with distance for type 1); every type-3 (icon-only) entry is merged
    into one shared row, see ``poi_signs_needed``."""
    plate_color = _CATEGORY_STYLE[category]["color"]
    rows_html = ""
    if poi_entries:
        detailed = sorted(
            (e for e in poi_entries if e["sign_type"] in (1, 2)),
            key=lambda e: (e["sign_type"], e["distance_m"]),
        )
        icon_only = sorted(
            _dedupe_icon_only([e for e in poi_entries if e["sign_type"] == 3]),
            key=lambda e: e["distance_m"],
        )
        rows = []
        for e in detailed:
            poi = e["poi"]
            if e["sign_type"] == 1:
                text = f"{poi.icon} {poi.name} — {e['distance_m'] / 1000:.1f} km"
            else:
                text = f"{poi.icon} {poi.name}"
            rows.append(_poi_row_html(text))
        if icon_only:
            rows.append(_poi_row_html(" ".join(e["poi"].icon for e in icon_only)))
        rows_html = "".join(rows)
    return (
        f"<div style='font-family:sans-serif;min-width:170px;'>"
        f"<div style='background:{plate_color};color:#fff;font-weight:bold;padding:6px 10px;"
        f"border:2px solid #000;border-radius:4px;text-align:center;'>{label}</div>"
        f"{rows_html}</div>"
    )


def _sign_popup_html(row, street_rank_map: dict, poi_signs: dict | None, category_ranks=None) -> str:
    """HTML of the popup showing the street sign boards at an intersection.

    Args:
        row: Intersection row with ``node_id`` and ``sign_street_ids``.
        street_rank_map: Street rank per street id.
        poi_signs: POI direction signs per node, or None.
        category_ranks: Optional road-category rank thresholds.

    Returns:
        The popup HTML.
    """
    sign_ids = [int(s) for s in row["sign_street_ids"].split(";") if s]
    node_id = row["node_id"]

    # Cross-street de-dup, scoped to this one node_id: a type-3 (icon-
    # only) POI can independently qualify at more than one of THIS
    # node's own street boards (e.g. one school reachable via street 28,
    # a different school - same icon - via street 29, both at the same
    # intersection) - only the nearest of those survives, dropped from
    # every OTHER street's board here. This is on top of (not instead
    # of) _dedupe_icon_only's own within-one-board de-dup below, which
    # still collapses same-icon repeats within whichever single board
    # ends up keeping them.
    all_type3 = []
    if poi_signs:
        for street_id in sign_ids:
            all_type3.extend(e for e in poi_signs.get((node_id, street_id), []) if e["sign_type"] == 3)
    kept_type3_ids = {id(e) for e in _dedupe_icon_only(all_type3)}

    boards = []
    for street_id, label in zip(sign_ids, row["sign_labels"]):
        category = rank_to_category(street_rank_map.get(street_id), category_ranks)
        entries = poi_signs.get((node_id, street_id)) if poi_signs else None
        if entries is not None:
            entries = [e for e in entries if e["sign_type"] != 3 or id(e) in kept_type3_ids]
        boards.append(_sign_board_html(label, category, entries))
    header = (
        f"<div style='font-size:11px;color:#555;margin-bottom:4px;'>"
        f"{row['intersection_label']} (rank {row['intersection_rank']})</div>"
    )
    return f"<div>{header}<div style='display:flex;flex-direction:column;gap:6px;'>{''.join(boards)}</div></div>"


_PANEL_STYLE = (
    "background: white; color: #111; padding: 10px 12px; border: 1px solid #333;"
    "border-radius: 4px; font-size: 12px; line-height: 1.5; max-width: 260px;"
)


def _category_legend_panel(category_counts: dict[str, int]) -> str:
    """HTML of the road-category legend.

    Args:
        category_counts: Number of streets per road category.

    Returns:
        The legend HTML.
    """
    rows = "".join(
        '<div><span style="display:inline-block;width:18px;height:4px;'
        'background:{color};border:1px solid #000;vertical-align:middle;'
        'margin-right:6px;"></span>{label} ({count})</div>'.format(
            color=s["color"], label=s["label"], count=category_counts.get(cat, 0)
        )
        for cat, s in _CATEGORY_STYLE.items()
    )
    total = sum(category_counts.values())
    return f"""
<div id="osmSignsRoadLegend" style="{_PANEL_STYLE}">
    <b>Road category</b> ({total} streets)
    <hr style="margin:6px 0;">
    {rows}
</div>
"""


def _intersection_legend_panel(
    rank_node_counts: dict[int, int], rank_sign_counts: dict[int, int]
) -> str:
    """HTML of the intersection-rank legend.

    Args:
        rank_node_counts: Number of intersections per rank.
        rank_sign_counts: Number of signs per rank.

    Returns:
        The legend HTML.
    """
    rows = "".join(
        '<div><span style="display:inline-block;width:12px;height:12px;border-radius:50%;'
        'background:{color};border:1px solid #000;vertical-align:middle;'
        'margin-right:6px;"></span>{label} '
        '<span style="color:#666;">({nodes} node(s), {signs} sign(s))</span></div>'.format(
            color=s["color"],
            label=s["label"],
            nodes=rank_node_counts.get(rank, 0),
            signs=rank_sign_counts.get(rank, 0),
        )
        for rank, s in _INTERSECTION_RANK_STYLE.items()
    )
    total_nodes = sum(rank_node_counts.values())
    return f"""
<div id="osmSignsIntersectionLegend" style="{_PANEL_STYLE}">
    <b>Intersection rank</b> ({total_nodes} nodes)
    <hr style="margin:6px 0;">
    {rows}
</div>
"""


def _cumulative_legend_panel(
    rank_node_counts: dict[int, int], rank_sign_counts: dict[int, int]
) -> str:
    """A budget-planning table: if signage is only installed at
    intersections of rank 0..k (rank 0 = most important, see the module
    docstring's table), how many intersections and physical name-plates
    does that take, for every cutoff k from 0 (signs only) up to 6
    (every intersection signed)."""
    rows = []
    cum_nodes = 0
    cum_signs = 0
    for rank in sorted(_INTERSECTION_RANK_STYLE):
        cum_nodes += rank_node_counts.get(rank, 0)
        cum_signs += rank_sign_counts.get(rank, 0)
        range_label = "0" if rank == 0 else f"0–{rank}"
        rows.append(
            f"<tr><td style='padding-right:10px;'>ranks {range_label}</td>"
            f"<td style='text-align:right;padding-right:10px;'>{cum_nodes}</td>"
            f"<td style='text-align:right;'>{cum_signs}</td></tr>"
        )
    rows_html = "".join(rows)
    return f"""
<div id="osmSignsCumulativeLegend" style="{_PANEL_STYLE}">
    <b>Signage if installed up to rank...</b>
    <hr style="margin:6px 0;">
    <table style="border-collapse:collapse;">
        <tr style="font-weight:bold;"><td style="padding-right:10px;">cutoff</td>
            <td style="text-align:right;padding-right:10px;">nodes</td>
            <td style="text-align:right;">signs</td></tr>
        {rows_html}
    </table>
</div>
"""


def _poi_signs_legend_panel(poi_signs: dict) -> str:
    """Total physical POI-direction signs needed (see ``poi_signs_needed``),
    across every (node, street) sign that got at least one POI entry."""
    total_signs = sum(poi_signs_needed(entries) for entries in poi_signs.values())
    total_nodes = len({node_id for node_id, _street_id in poi_signs})
    return f"""
<div id="osmSignsPoiLegend" style="{_PANEL_STYLE}">
    <b>POI direction signs</b>
    <hr style="margin:6px 0;">
    <div>{total_signs} sign(s) needed, at {total_nodes} intersection(s)</div>
</div>
"""


def _stats_panels_html(panels: list[str]) -> str:
    """Stacks every stats panel vertically in one fixed-position column,
    top-right of the map, below one another (rather than side by side)."""
    panels_html = "".join(panels)
    return f"""
<div id="osmSignsStatsPanels" style="position: fixed; top: 20px; right: 20px; z-index: 9999;
     display: flex; flex-direction: column; gap: 10px;">
    {panels_html}
</div>
"""


def _full_street_geometry(edges_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Dissolve EVERY edge sharing a ``street_id`` - main-path or not -
    into one display geometry per street.

    ``pipeline.streets_gdf_from_edges`` (what ``streets_gdf`` normally
    comes from) dissolves ONLY ``is_main_direction`` edges, since that's
    the single through-line house numbering is measured along. But a
    short non-main spur can be the only edge physically connecting an
    otherwise-main piece to the rest of its own street (a real OSM edge,
    correctly carrying that street_id, just not picked as part of the
    numbered main line) - dropping it from the map makes two pieces of
    the SAME street look disconnected even though they share a street_id
    and a real edge. This is display-only: numbering itself was already
    computed upstream from the main-path edges alone and is untouched.

    Args:
        edges_gdf: Edge table (u, v, key)-indexed, with ``street_id``.

    Returns:
        One row per ``street_id`` with a dissolved (and, where the
        pieces connect end to end, line-merged) geometry, in
        ``edges_gdf``'s own CRS.
    """
    all_edges = edges_gdf[edges_gdf["street_id"].notna()]
    dissolved = all_edges[["street_id", "geometry"]].dissolve(by="street_id").reset_index()
    dissolved["geometry"] = dissolved["geometry"].apply(
        lambda geom: linemerge(geom) if geom.geom_type == "MultiLineString" else geom
    )
    return dissolved


def build_street_signs_map(
    streets_gdf: gpd.GeoDataFrame,
    intersections_gdf: gpd.GeoDataFrame,
    out_html_path: str,
    category_ranks: dict[str, set] | None = None,
    zoom_start: int = 15,
    poi_signs: dict | None = None,
    pois: list | None = None,
    edges_gdf: gpd.GeoDataFrame | None = None,
) -> folium.Map:
    """Render the street-signs map: streets by category, dots by intersection rank.

    Args:
        streets_gdf: One row per street (``street_rank`` and geometry
            columns required - see ``pipeline.streets_gdf_from_edges``).
        intersections_gdf: From ``compute_intersections``.
        out_html_path: Path to write the standalone HTML map to.
        category_ranks: Passed through to ``rank_to_category`` (only
            used here to resolve each street's own display color).
        zoom_start: Initial zoom level.
        poi_signs: Optional, from ``poi_signs.compute_poi_signs`` -
            ``{(node_id, street_id): [entry, ...]}``. When given, each
            intersection's popup gets the POI direction entries added to
            the relevant street's own board (see ``_sign_board_html``),
            and a POI-signs-needed stats panel is added.
        pois: The full POI list (``poi_signs.PointOfInterest``), drawn
            on the map (markers for points, outline-only for polygons)
            regardless of whether they ended up with any signed entry.
            Only meaningful together with ``poi_signs``.
        edges_gdf: Optional - when given, streets are drawn using EVERY
            edge of their own street_id (see ``_full_street_geometry``),
            not just ``streets_gdf``'s own main-path-only geometry, so a
            short non-main spur that's the only real edge connecting two
            pieces of the same street doesn't show up as a gap on the
            map. Display-only; doesn't touch numbering.

    Returns:
        The folium Map object (also already saved to ``out_html_path``).

    Raises:
        ValueError: If ``streets_gdf`` is empty.
    """
    if streets_gdf.empty:
        raise ValueError("streets_gdf is empty, nothing to render")

    if edges_gdf is not None:
        # Every street_id in streets_gdf came from at least one main-path
        # edge in edges_gdf, so it's guaranteed a row here too - a plain
        # inner merge is safe, no fallback to the main-path-only geometry
        # needed.
        full_geom = _full_street_geometry(edges_gdf)[["street_id", "geometry"]]
        streets_render_base = streets_gdf.drop(columns="geometry").merge(
            full_geom, on="street_id", how="left"
        )
        streets_gdf = gpd.GeoDataFrame(streets_render_base, geometry="geometry", crs=edges_gdf.crs)

    streets_wgs84 = streets_gdf.to_crs(4326)
    bounds = streets_wgs84.total_bounds  # minx, miny, maxx, maxy
    center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]

    fmap, _tile_layers = _new_map(center, zoom_start=zoom_start)

    # One GeoJson layer for every street, rather than one folium.PolyLine
    # (several lines of JS each) per street - the difference between a
    # few thousand individual Leaflet layer objects and one compact JSON
    # payload is the main reason this map used to be slow to load,
    # especially for a subregion the size of Douala's (thousands of
    # streets/intersections). Per-feature style/tooltip travel as plain
    # GeoJSON properties instead of being baked into per-feature JS.
    def _street_style(row):
        """Folium style for a street by its road category.

        Args:
            row: Street feature row.

        Returns:
            Style dict with colour, weight and opacity.
        """
        category = rank_to_category(row.get("street_rank"), category_ranks)
        s = _CATEGORY_STYLE[category]
        return {"color": s["color"], "weight": s["weight"], "opacity": 0.9}

    streets_render = streets_wgs84.copy()
    streets_render["style"] = streets_render.apply(_street_style, axis=1)
    streets_render["tooltip"] = streets_render.apply(
        lambda row: f"rank {row.get('street_rank')} – {row.get('street_name') or '(unnamed)'} "
        f"– #{row.get('street_id')}",
        axis=1,
    )
    folium.GeoJson(
        streets_render[["geometry", "style", "tooltip"]].to_json(),
        name="streets",
        style_function=lambda f: f["properties"]["style"],
        tooltip=folium.GeoJsonTooltip(fields=["tooltip"], aliases=[""], labels=False, sticky=True),
    ).add_to(fmap)

    if pois:
        pois_group = folium.FeatureGroup(name="points of interest", show=True)
        for poi in pois:
            if poi.is_polygon:
                folium.GeoJson(
                    poi.geometry.__geo_interface__,
                    style_function=lambda _f: {"color": "#ff00ff", "weight": 3, "fillOpacity": 0},
                    tooltip=poi.name,
                ).add_to(pois_group)
            else:
                folium.Marker(
                    location=(poi.geometry.y, poi.geometry.x),
                    tooltip=f"{poi.icon} {poi.name}",
                    icon=folium.DivIcon(html=f"<div style='font-size:22px;'>{poi.icon}</div>"),
                ).add_to(pois_group)
        pois_group.add_to(fmap)

    street_rank_map = streets_gdf.set_index("street_id")["street_rank"].to_dict()

    if not intersections_gdf.empty:
        intersections_wgs84 = intersections_gdf.to_crs(4326)
        # Same batching as the streets layer above: one GeoJson layer for
        # every intersection dot instead of one folium.CircleMarker (+ one
        # folium.Popup, itself several more lines of inline JS) each -
        # popup content still travels as fully custom HTML (see
        # _sign_popup_html), just as a GeoJSON property string instead of
        # a baked-in per-marker script.
        intersections_render = intersections_wgs84.copy()
        intersections_render["fill_color"] = [
            _INTERSECTION_RANK_STYLE[int(r)]["color"] for r in intersections_gdf["intersection_rank"]
        ]
        intersections_render["popup_html"] = [
            _sign_popup_html(row, street_rank_map, poi_signs, category_ranks)
            for _, row in intersections_gdf.iterrows()
        ]
        folium.GeoJson(
            intersections_render[["geometry", "fill_color", "popup_html"]].to_json(),
            name="intersections",
            marker=folium.CircleMarker(radius=6, color="#000000", weight=1, fill=True, fill_opacity=0.95),
            style_function=lambda f: {"fillColor": f["properties"]["fill_color"]},
            popup=folium.GeoJsonPopup(fields=["popup_html"], labels=False, max_width=300),
        ).add_to(fmap)

    category_counts: dict[str, int] = defaultdict(int)
    for r in streets_gdf["street_rank"]:
        category_counts[rank_to_category(r, category_ranks)] += 1

    rank_node_counts: dict[int, int] = defaultdict(int)
    rank_sign_counts: dict[int, int] = defaultdict(int)
    for _, row in intersections_gdf.iterrows():
        rank = int(row["intersection_rank"])
        rank_node_counts[rank] += 1
        rank_sign_counts[rank] += len(row["sign_labels"])

    panels = [
        _category_legend_panel(category_counts),
        _intersection_legend_panel(rank_node_counts, rank_sign_counts),
        _cumulative_legend_panel(rank_node_counts, rank_sign_counts),
    ]
    if poi_signs:
        panels.append(_poi_signs_legend_panel(poi_signs))
    fmap.get_root().html.add_child(folium.Element(_stats_panels_html(panels)))

    if pois:
        folium.LayerControl(collapsed=False).add_to(fmap)

    fmap.save(out_html_path)
    return fmap
