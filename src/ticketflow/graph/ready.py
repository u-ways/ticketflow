"""Ready-set, cycle and stagger computations (ADR-0008).

Pure functions over node states and edges. Edges run (upstream, downstream):
``(a, b)`` means b depends on a. No model is ever consulted here.
"""

import graphlib
from collections.abc import Collection, Iterable, Mapping, Sequence

from ticketflow.domain.errors import DependencyCycle
from ticketflow.domain.model import NodeState


def detect_cycles(edges: Collection[tuple[str, str]]) -> None:
    """Raise :class:`DependencyCycle` if the edge set contains a cycle."""
    graph: dict[str, set[str]] = {}
    for upstream, downstream in edges:
        graph.setdefault(downstream, set()).add(upstream)
        graph.setdefault(upstream, set())
    sorter = graphlib.TopologicalSorter(graph)
    try:
        sorter.prepare()
    except graphlib.CycleError as exc:
        cycle = exc.args[1] if len(exc.args) > 1 else []
        raise DependencyCycle(
            "dependency cycle detected: " + " -> ".join(str(n) for n in cycle)
        ) from exc


def newly_ready(
    nodes: Mapping[str, NodeState], edges: Collection[tuple[str, str]]
) -> tuple[str, ...]:
    """Blocked nodes whose upstream edges are all resolved (Merged).

    The only place graph structure matters (ADR-0006). Deterministic order.
    """
    upstreams: dict[str, set[str]] = {}
    for upstream, downstream in edges:
        upstreams.setdefault(downstream, set()).add(upstream)

    ready = [
        node_id
        for node_id, state in nodes.items()
        if state is NodeState.BLOCKED
        and all(nodes.get(up) is NodeState.MERGED for up in upstreams.get(node_id, set()))
    ]
    return tuple(sorted(ready))


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
