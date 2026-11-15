"""The must-finish-first graph: blocking, inherited blocking and cycle checks.

Pure functions over plain data (ids and edges), with no database access, so
they can be property-tested directly. SPEC sections 5.1 and 5.2.

An edge A -> B means A must be finished before B can be. There are three kinds:

- child before parent:            child -> parent        ("child of")
- blocker before blocked:         blocker -> blocked     ("blocks")
- inherited blocking:             blocker -> D, for every descendant D of
                                  blocked                ("blocks an ancestor of")

The cycle check runs over ALL tasks, whatever their status (5.2). It is a
different graph from the derive snapshot, which holds open tasks only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

CHILD_OF = "child of"
BLOCKS = "blocks"
BLOCKS_ANCESTOR_OF = "blocks an ancestor of"

# When two kinds of edge join the same pair, report the most direct one.
_RANK = {CHILD_OF: 0, BLOCKS: 1, BLOCKS_ANCESTOR_OF: 2}

Edges = dict[int, dict[int, str]]  # source -> {target: because}


def children_map(parent: Mapping[int, int | None]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for child, par in parent.items():
        if par is not None:
            children.setdefault(par, []).append(child)
    for kids in children.values():
        kids.sort()
    return children


def descendants(task_id: int, children: Mapping[int, list[int]]) -> list[int]:
    """Every task below `task_id`, in depth-first order. Tolerates loops."""
    out: list[int] = []
    seen = {task_id}
    stack = list(reversed(children.get(task_id, [])))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        out.append(node)
        stack.extend(reversed(children.get(node, [])))
    return out


def ancestors(task_id: int, parent: Mapping[int, int | None]) -> list[int]:
    """Parent, grandparent, ... up to the root. Tolerates loops."""
    out: list[int] = []
    seen = {task_id}
    node = parent.get(task_id)
    while node is not None and node not in seen:
        out.append(node)
        seen.add(node)
        node = parent.get(node)
    return out


def _add(edges: Edges, src: int, dst: int, because: str) -> None:
    targets = edges.setdefault(src, {})
    current = targets.get(dst)
    if current is None or _RANK[because] < _RANK[current]:
        targets[dst] = because


def build_edges(parent: Mapping[int, int | None], blocks: Iterable[tuple[int, int]]) -> Edges:
    """The full edge set of SPEC 5.1."""
    edges: Edges = {}
    for child, par in parent.items():
        if par is not None:
            _add(edges, child, par, CHILD_OF)
    children = children_map(parent)
    for blocker, blocked in blocks:
        _add(edges, blocker, blocked, BLOCKS)
        for below in descendants(blocked, children):
            _add(edges, blocker, below, BLOCKS_ANCESTOR_OF)
    return edges


def find_cycle(edges: Edges, start_with: tuple[int, int] | None = None) -> list[int] | None:
    """A loop as [a, b, ..., a], or None if the graph is acyclic.

    If `start_with` (an edge) lies on the loop found, the loop is rotated to
    begin with it, so the change being made reads first.
    """
    white, grey, black = 0, 1, 2
    colour: dict[int, int] = {}
    nodes = set(edges)
    for targets in edges.values():
        nodes.update(targets)

    def rotate(loop: list[int]) -> list[int]:
        if start_with is None:
            return loop + [loop[0]]
        a, b = start_with
        for i, node in enumerate(loop):
            if node == a and loop[(i + 1) % len(loop)] == b:
                loop = loop[i:] + loop[:i]
                break
        return loop + [loop[0]]

    order = sorted(nodes)
    if start_with is not None and start_with[0] in nodes:
        order.remove(start_with[0])
        order.insert(0, start_with[0])
    for root in order:
        if colour.get(root, white) != white:
            continue
        stack: list[tuple[int, list[int]]] = [(root, sorted(edges.get(root, {})))]
        path = [root]
        colour[root] = grey
        while stack:
            node, pending = stack[-1]
            if not pending:
                stack.pop()
                path.pop()
                colour[node] = black
                continue
            nxt = pending.pop(0)
            state = colour.get(nxt, white)
            if state == grey:
                return rotate(path[path.index(nxt) :])
            if state == white:
                colour[nxt] = grey
                path.append(nxt)
                stack.append((nxt, sorted(edges.get(nxt, {}))))
    return None


@dataclass
class Structure:
    """Every task's parent and every blocker link, regardless of status."""

    parent: dict[int, int | None] = field(default_factory=dict)
    blocks: set[tuple[int, int]] = field(default_factory=set)


def check(
    structure: Structure,
    *,
    add_blocks: Iterable[tuple[int, int]] = (),
    set_parent: Mapping[int, int | None] | None = None,
) -> list[tuple[int, str | None]] | None:
    """Apply a proposed change and look for a loop.

    Returns None if the change is safe, otherwise the loop as
    [(id, because), ...] where `because` describes the edge to the next entry
    and the final entry (which repeats the first) has None.
    """
    parent = dict(structure.parent)
    if set_parent:
        parent.update(set_parent)
    new_blocks = list(add_blocks)
    blocks = set(structure.blocks) | set(new_blocks)
    edges = build_edges(parent, blocks)

    start_with: tuple[int, int] | None = None
    if new_blocks:
        start_with = new_blocks[0]
    elif set_parent:
        child, par = next(iter(set_parent.items()))
        if par is not None:
            start_with = (child, par)
    loop = find_cycle(edges, start_with)
    if loop is None:
        return None
    steps: list[tuple[int, str | None]] = []
    for here, there in zip(loop, loop[1:], strict=False):
        steps.append((here, edges[here][there]))
    steps.append((loop[-1], None))
    return steps
