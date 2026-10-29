"""Shared topology checks for street-ID edits and their browser affordances."""


def split_refs(edges, ref: str, node: int) -> list[str]:
    """Return the proper branch leaving node, or explain why no split exists.

    ``edges`` is the undirected, ref-indexed table for one current street.
    Removing the junction must separate the selected branch from a remainder.
    A loop returning to the same junction is therefore not a splittable branch.
    """
    if ref not in edges.index or node not in (int(edges.loc[ref, 'u']), int(edges.loc[ref, 'v'])):
        raise ValueError('the selected road does not leave this node')
    at_node = [r for r, row in edges.iterrows() if node in (int(row.u), int(row.v))]
    if len(at_node) < 2:
        raise ValueError('the street leaves this node by one road only: there is nothing to split')
    adjacent = {}
    for row in edges.itertuples():
        u, v = int(row.u), int(row.v)
        if node not in (u, v):
            adjacent.setdefault(u, set()).add(v)
            adjacent.setdefault(v, set()).add(u)
    selected = edges.loc[ref]
    far = int(selected.v) if int(selected.u) == node else int(selected.u)
    component, todo = set(), [far]
    while todo:
        n = todo.pop()
        if n not in component:
            component.add(n)
            todo.extend(adjacent.get(n, set()) - component)
    take = [r for r, row in edges.iterrows() if int(row.u) in component or int(row.v) in component]
    if not take or len(take) == len(edges):
        raise ValueError('this branch is the whole street: there is nothing to split off')
    sid = int(edges.loc[ref, 'street_id'])
    validate_path(edges, take, sid)
    validate_path(edges, set(edges.index) - set(take), sid)
    return take


def validate_path(edges, refs, street_id: int) -> None:
    """Reject branching, disconnected and cyclic results before recording edits."""
    degrees, adjacent = {}, {}
    for row in edges.loc[list(refs)].itertuples():
        u, v = int(row.u), int(row.v)
        for n, other in ((u, v), (v, u)):
            degrees[n] = degrees.get(n, 0) + 1
            adjacent.setdefault(n, set()).add(other)
    for n, degree in sorted(degrees.items()):
        if degree > 2:
            raise ValueError(f'id {street_id} would have {degree} roads at node {n}: a street has 2 at most')
    if not adjacent:
        raise ValueError('there are no roads left to join')
    seen, todo = set(), [next(iter(adjacent))]
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo.extend(adjacent[n] - seen)
    if len(seen) != len(adjacent):
        raise ValueError(f'id {street_id} would contain disconnected roads: a street must be one path')
    if len(refs) != len(adjacent) - 1:
        raise ValueError(f'id {street_id} would form a loop: a street must be a simple path')
