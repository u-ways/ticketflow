"""Ready-set, cycle and stagger computations (ADR-0008).

Pure functions over node states and edges. Edges run (upstream, downstream):
``(a, b)`` means b depends on a. No model is ever consulted here.
"""

import graphlib
from collections.abc import Collection, Iterable, Mapping, Sequence

from ticketflow.domain.errors import DependencyCycle
from ticketflow.domain.model import NodeState


def _prepared_sorter(
    edges: Collection[tuple[str, str]], extra_nodes: Collection[str] = ()
) -> graphlib.TopologicalSorter[str]:
    """A prepared TopologicalSorter; ``prepare()`` rejects cycles (ADR-0008)."""
    graph: dict[str, set[str]] = {node_id: set() for node_id in extra_nodes}
    for upstream, downstream in edges:
        graph.setdefault(downstream, set()).add(upstream)
        graph.setdefault(upstream, set())
    sorter: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter(graph)
    try:
        sorter.prepare()
    except graphlib.CycleError as exc:
        cycle = exc.args[1] if len(exc.args) > 1 else []
        raise DependencyCycle(
            "dependency cycle detected: " + " -> ".join(str(n) for n in cycle)
        ) from exc
    return sorter


def detect_cycles(edges: Collection[tuple[str, str]]) -> None:
    """Raise :class:`DependencyCycle` if the edge set contains a cycle."""
    _prepared_sorter(edges)


def newly_ready(
    nodes: Mapping[str, NodeState], edges: Collection[tuple[str, str]]
) -> tuple[str, ...]:
    """Blocked nodes whose upstream edges are all resolved (Merged).

    The dynamic ready-set on ``TopologicalSorter.prepare()/get_ready()/done()``
    (ADR-0008): cycle rejection is structurally part of computing the set — a
    cyclic graph raises :class:`DependencyCycle`, never a silent stall. The
    only place graph structure matters (ADR-0006). Deterministic order.
    """
    sorter = _prepared_sorter(edges, extra_nodes=nodes.keys())
    candidates: list[str] = []
    while sorter.is_active():
        frontier = sorter.get_ready()
        if not frontier:
            break
        for node_id in frontier:
            state = nodes.get(node_id)
            if state is NodeState.MERGED:
                # Resolved upstream: unblocks its dependents in the next round.
                sorter.done(node_id)
            elif state is NodeState.BLOCKED:
                candidates.append(node_id)
            # Any other state keeps its subtree blocked: never marked done.
    return tuple(sorted(candidates))


def blocked_on_escalated(
    nodes: Mapping[str, NodeState], edges: Collection[tuple[str, str]]
) -> dict[str, str]:
    """Map each Blocked dependent of an Escalated node to its root cause.

    Dependents stay Blocked with a ``blocked_reason`` naming the escalated
    ancestor, so the board shows root cause rather than a silent stall
    (ADR-0006, spec §12.4). Transitive: the *escalated* ancestor is named,
    not the intermediate blocked one.
    """
    downstreams: dict[str, set[str]] = {}
    for upstream, downstream in edges:
        downstreams.setdefault(upstream, set()).add(downstream)

    reasons: dict[str, str] = {}
    for node_id, state in sorted(nodes.items()):
        if state is not NodeState.ESCALATED:
            continue
        stack = [node_id]
        while stack:
            current = stack.pop()
            for dependent in sorted(downstreams.get(current, set())):
                if nodes.get(dependent) is NodeState.BLOCKED and dependent not in reasons:
                    reasons[dependent] = node_id
                    stack.append(dependent)
    return reasons


def stagger_order(
    ready: Sequence[tuple[str, tuple[str, ...]]],
    *,
    in_flight_scopes: Iterable[tuple[str, ...]],
) -> list[tuple[str, tuple[str, ...]]]:
    """Order ready nodes so scope-overlapping ones dispatch last (ADR-0008).

    A scheduling bias, never a block: every input node is returned. Nodes
    whose hints overlap an in-flight node's hints sort after the rest; the
    original order is otherwise preserved. Nodes without hints carry no
    signal and are not penalized (spec §12.6).
    """
    flight = [scope for scopes in in_flight_scopes for scope in scopes]

    def overlaps(scopes: tuple[str, ...]) -> bool:
        return any(_paths_overlap(scope, active) for scope in scopes for active in flight)

    return sorted(ready, key=lambda item: overlaps(item[1]))


def _paths_overlap(a: str, b: str) -> bool:
    """True when one normalized path is a segment-prefix of the other."""
    parts_a = _normalize(a)
    parts_b = _normalize(b)
    shorter, longer = sorted((parts_a, parts_b), key=len)
    return longer[: len(shorter)] == shorter


def _normalize(path: str) -> tuple[str, ...]:
    segments = [seg for seg in path.split("/") if seg]
    while segments and segments[-1] in ("*", "**"):
        segments.pop()
    return tuple(segments)
