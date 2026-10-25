"""Fast igraph-based routing, weighted to prefer higher-rank roads.

Builds a small igraph ``Graph`` from a labeled ``edges_gdf`` (same shape
``street_ids.label_streets`` produces), with a ``length_routing`` edge
weight - each edge's real ``length`` multiplied by a per-category factor
(see ``DEFAULT_ROUTING_CATEGORY_FACTORS``) so the shortest WEIGHTED path
tends to follow a primary road even when a shorter but lower-rank detour
exists, the same way a real driver would.

``nearest_targets_distance`` answers, for a whole batch of candidate
source nodes at once, "what's the shortest routing-weighted path from
this node to ANY of these target nodes, and what's the real (unweighted)
distance along that specific route?" - the two-call trick
(``street_signs.py``'s own module docstring has the terminology; see
this module's own docstring below) that makes computing this for every
node/POI pair in a subregion cheap: one igraph distance matrix call
(``mode="in"``, treating the batch of targets as sources on the reversed
graph) picks the nearest target per candidate node, then one
``get_shortest_paths`` call per candidate node (there are far fewer
candidate nodes than there are POI/edge pairs) retrieves that specific
route's real edge lengths.
"""

from __future__ import annotations

import igraph as ig
import numpy as np
import pandas as pd

DEFAULT_ROUTING_CATEGORY_FACTORS = {
    "primary": 0.5,
    "secondary": 0.85,
    "tertiary": 1.0,
    "other": 2.0,
}


class RoutingGraph:
    """An igraph ``Graph`` built from ``edges_gdf``, plus the node-id maps
    needed to translate real (OSM) node ids to/from igraph vertex indices.

    Attributes:
        graph: A directed ``igraph.Graph`` with edge attributes
            ``length`` (real meters) and ``length_routing`` (weighted).
        node_to_vertex: Real node id (``edges_gdf``'s own ``u``/``v``
            values) -> igraph vertex index.
        vertex_to_node: The reverse mapping, as a list indexed by vertex.
    """

    def __init__(self, graph: ig.Graph, node_to_vertex: dict, vertex_to_node: list):
        """Wrap an igraph graph with node-id lookups.

        Args:
            graph: The igraph routing graph.
            node_to_vertex: Map from OSM node id to igraph vertex index.
            vertex_to_node: List mapping vertex index to OSM node id.
        """
        self.graph = graph
        self.node_to_vertex = node_to_vertex
        self.vertex_to_node = vertex_to_node

    def vertex_of(self, node_id):
        """Return the igraph vertex index of an OSM node id.

        Args:
            node_id: OSM node id.

        Returns:
            The vertex index, or None if the node is not in the graph.
        """
        return self.node_to_vertex.get(node_id)


def build_routing_graph(
    edges_gdf: pd.DataFrame,
    street_category: dict,
    category_factors: dict[str, float] | None = None,
) -> RoutingGraph:
    """Build a ``RoutingGraph`` from labeled, directed edges.

    Args:
        edges_gdf: Edge table (u, v, key)-indexed, with ``street_id`` and
            ``length`` columns - drivable edges only (non-drivable/
            private edges have no ``street_id`` and are skipped, same as
            everywhere else in this package).
        street_category: ``{street_id: category}`` (``"primary"``/
            ``"secondary"``/``"tertiary"``/``"other"``), e.g. from
            ``{sid: street_signs.rank_to_category(r) for sid, r in ...}``.
        category_factors: Per-category ``length_routing`` multiplier.
            Defaults to ``DEFAULT_ROUTING_CATEGORY_FACTORS``.

    Returns:
        A ``RoutingGraph`` ready for ``nearest_targets_distance``.
    """
    category_factors = category_factors or DEFAULT_ROUTING_CATEGORY_FACTORS

    edges = edges_gdf.reset_index()[["u", "v", "street_id", "length"]].dropna(
        subset=["street_id"]
    )
    edges["street_id"] = edges["street_id"].astype(int)

    node_ids = pd.unique(pd.concat([edges["u"], edges["v"]], ignore_index=True))
    node_to_vertex = {node_id: i for i, node_id in enumerate(node_ids)}
    vertex_to_node = list(node_ids)

    factors = edges["street_id"].map(street_category).map(
        lambda cat: category_factors.get(cat, 1.0)
    )
    length_routing = edges["length"].astype(float) * factors.astype(float)

    g = ig.Graph(
        n=len(node_ids),
        edges=list(zip(edges["u"].map(node_to_vertex), edges["v"].map(node_to_vertex))),
        directed=True,
    )
    g.es["length"] = edges["length"].astype(float).tolist()
    g.es["length_routing"] = length_routing.tolist()
    g.es["street_id"] = edges["street_id"].tolist()

    return RoutingGraph(g, node_to_vertex, vertex_to_node)


def nearest_targets_distance(
    routing_graph: RoutingGraph,
    source_node_ids: list,
    target_node_ids: list,
) -> dict:
    """For every source node, the routing-weighted-shortest route to the
    nearest of ``target_node_ids``, reported as real (unweighted) distance.

    Args:
        routing_graph: From ``build_routing_graph``.
        source_node_ids: Candidate node ids to route FROM (e.g. every
            intersection inside a POI's buffer).
        target_node_ids: Node ids to route TO (e.g. the network node(s)
            nearest a point POI, or every node just outside a polygon
            POI's boundary - see ``poi_signs.polygon_boundary_target_nodes``).

    Returns:
        ``{source_node_id: (real_distance_m, first_edge_street_id)}`` -
        only for sources that can actually reach a target; a source with
        no path to any target is omitted. ``first_edge_street_id`` is
        the ``street_id`` of the edge the route LEAVES the source node
        on - the street whose own sign (if any) this route belongs to.
    """
    g = routing_graph.graph
    target_vs = [routing_graph.vertex_of(t) for t in target_node_ids if routing_graph.vertex_of(t) is not None]
    if not target_vs:
        return {}

    source_pairs = [
        (nid, routing_graph.vertex_of(nid))
        for nid in source_node_ids
        if routing_graph.vertex_of(nid) is not None
    ]
    if not source_pairs:
        return {}
    source_ids = [p[0] for p in source_pairs]
    source_vs = [p[1] for p in source_pairs]

    # One distance-matrix call: mode="in" treats edges as reversed, so
    # this gives, for each (target, source) pair, the routing-weighted
    # distance of the real directed path FROM source TO target - i.e.
    # exactly what we want, computed as a multi-source search rooted at
    # the (few) targets instead of one search per (many) source.
    dist_matrix = np.array(
        g.distances(source=target_vs, target=source_vs, weights="length_routing", mode="in")
    )
    # A source that is ITSELF one of the targets (the POI's nearest
    # network node happens to be this very intersection) would otherwise
    # win its own argmin with a trivial zero-length "path" - discard
    # those entries so each source instead routes to the nearest OTHER
    # target, exactly like a real signpost still points further down the
    # road even from the junction right at the destination's own doorstep.
    self_target_mask = np.equal.outer(target_vs, source_vs)
    dist_matrix = np.where(self_target_mask, np.inf, dist_matrix)

    best_target_idx = dist_matrix.argmin(axis=0)
    best_dist = dist_matrix[best_target_idx, np.arange(len(source_vs))]

    results = {}
    for i, (source_id, source_v) in enumerate(zip(source_ids, source_vs)):
        if not np.isfinite(best_dist[i]):
            continue  # no OTHER target reachable from this source
        target_v = target_vs[best_target_idx[i]]
        paths = g.get_shortest_paths(
            source_v, to=target_v, weights="length_routing", mode="out", output="epath"
        )
        epath = paths[0]
        if not epath:
            continue
        real_distance = sum(g.es[e]["length"] for e in epath)
        first_street_id = g.es[epath[0]]["street_id"]
        results[source_id] = (real_distance, first_street_id)
    return results
