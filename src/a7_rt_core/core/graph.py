"""
A7-RT Graph Layer

Pure functions over the dependency graph stored in master.json.
No file I/O — Repository handles that. All functions accept plain dicts
(JSON-deserialized) and return plain Python types.

Dependency direction convention (matches master.json / Dependency model):
  dep["from_node"] depends on dep["to_node"]

  e.g. {"from_node": "B", "to_node": "A", "type": "structural"}
  means: B imports A, so A must be built before B.

  In graph terms:  B ──depends──► A  (edge points toward prerequisite)
  In topo order:   A comes before B
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from typing import Any

# ---------------------------------------------------------------------------
# Type aliases (plain dicts from JSON, not Pydantic models)
# ---------------------------------------------------------------------------

NodeDict = dict[str, Any]  # {"node_id": ..., "status": ..., ...}
DepDict = dict[str, Any]  # {"from_node": ..., "to_node": ..., "type": ..., ...}
NodeMap = dict[str, NodeDict]  # node_id → NodeDict
DepList = list[DepDict]


# ---------------------------------------------------------------------------
# Internal adjacency helpers
# ---------------------------------------------------------------------------


def _forward_adj(dependencies: DepList, nodes: NodeMap | None = None) -> dict[str, set[str]]:
    """
    Forward adjacency: node → set of nodes it depends on (prerequisites).
    Edge direction: from_node → to_node.
    All nodes mentioned in deps appear as keys (even if no outgoing edges).

    If nodes is provided, also includes edges from node-level structural_deps.
    """
    adj: dict[str, set[str]] = {}
    for dep in dependencies:
        fn = dep["from_node"]
        tn = dep["to_node"]
        adj.setdefault(fn, set()).add(tn)
        adj.setdefault(tn, set())  # ensure every node has an entry

    # Also process node-level structural_deps if nodes provided
    if nodes:
        for nid, node in nodes.items():
            adj.setdefault(nid, set())
            for dep_nid in node.get("structural_deps", []):
                if dep_nid in nodes:
                    adj[nid].add(dep_nid)
                    adj.setdefault(dep_nid, set())

    return adj


def _reverse_adj(dependencies: DepList, nodes: NodeMap | None = None) -> dict[str, set[str]]:
    """
    Reverse adjacency: node → set of nodes that depend on it (downstream).
    Used for dependents traversal and poison propagation.

    If nodes is provided, also includes edges from node-level structural_deps.
    """
    radj: dict[str, set[str]] = {}
    for dep in dependencies:
        fn = dep["from_node"]
        tn = dep["to_node"]
        radj.setdefault(tn, set()).add(fn)
        radj.setdefault(fn, set())

    # Also process node-level structural_deps if nodes provided
    if nodes:
        for nid, node in nodes.items():
            for dep_nid in node.get("structural_deps", []):
                if dep_nid in nodes:
                    radj.setdefault(dep_nid, set()).add(nid)
                    radj.setdefault(nid, set())

    return radj


# ---------------------------------------------------------------------------
# topo_sort
# ---------------------------------------------------------------------------


def topo_sort(nodes: NodeMap, dependencies: DepList) -> list[str]:
    """
    Return node_ids in topological order: dependencies before dependents.

    Uses Kahn's algorithm (BFS). Only nodes present in *nodes* are included;
    edges that reference unknown node_ids are silently skipped.

    Raises ValueError if a cycle is detected (partial sort is not returned).

    >>> nodes = {"A": {}, "B": {}, "C": {}}
    >>> deps  = [{"from_node": "B", "to_node": "A", "type": "structural"},
    ...          {"from_node": "C", "to_node": "B", "type": "structural"}]
    >>> topo_sort(nodes, deps)
    ['A', 'B', 'C']
    """
    # in_degree[n] = number of prerequisites n has (within the nodes dict)
    in_degree: dict[str, int] = {nid: 0 for nid in nodes}
    # successors[n] = nodes that list n as a prerequisite
    successors: dict[str, list[str]] = {nid: [] for nid in nodes}

    # Process global dependencies array
    for dep in dependencies:
        fn, tn = dep["from_node"], dep["to_node"]
        if fn not in nodes or tn not in nodes:
            continue
        in_degree[fn] += 1
        successors[tn].append(fn)

    # Also process node-level structural_deps (set during seeding)
    for nid, node in nodes.items():
        for dep_nid in node.get("structural_deps", []):
            if dep_nid not in nodes:
                continue
            in_degree[nid] += 1
            successors[dep_nid].append(nid)

    queue: deque[str] = deque(nid for nid, deg in in_degree.items() if deg == 0)
    # Deterministic order within each in-degree tier
    queue = deque(sorted(queue))
    result: list[str] = []

    while queue:
        nid = queue.popleft()
        result.append(nid)
        for successor in sorted(successors[nid]):
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    if len(result) != len(nodes):
        cycle_nodes = sorted(nid for nid in nodes if nid not in set(result))
        raise ValueError(
            f"Cycle detected — topo_sort cannot complete. "
            f"Nodes involved: {cycle_nodes}. Use cycle_check() for details."
        )

    return result


# ---------------------------------------------------------------------------
# dependents
# ---------------------------------------------------------------------------


def dependents(node_id: str, dependencies: DepList, nodes: NodeMap | None = None) -> set[str]:
    """
    Return the set of all node_ids that (transitively) depend on *node_id*.
    Traverses both structural and assumption edges.
    Does not include *node_id* itself.

    This is the downstream closure — every node that would be affected if
    *node_id* changes or is poisoned.

    >>> deps = [{"from_node": "B", "to_node": "A", "type": "structural"},
    ...         {"from_node": "C", "to_node": "B", "type": "structural"}]
    >>> dependents("A", deps) == {"B", "C"}
    True
    >>> dependents("C", deps)
    set()
    """
    radj = _reverse_adj(dependencies, nodes)
    visited: set[str] = set()
    queue: list[str] = [node_id]

    while queue:
        current = queue.pop()
        for downstream in radj.get(current, set()):
            if downstream not in visited:
                visited.add(downstream)
                queue.append(downstream)

    return visited


# ---------------------------------------------------------------------------
# poison_set
# ---------------------------------------------------------------------------


def poison_set(node_id: str, nodes: NodeMap, dependencies: DepList) -> set[str]:
    """
    Return all node_ids that must be poisoned when *node_id* is poisoned.

    A7 invariant: downstream of [☠] is [☠].
    Poison propagates through BOTH structural and assumption edges transitively.

    Does not include *node_id* itself — the caller handles poisoning the source.
    Already-poisoned nodes in the result are intentional: callers should still
    write the tombstone record even for nodes that are already poisoned, to
    capture the specific causal chain.

    Only nodes present in *nodes* are included in the result.

    >>> nodes = {"A": {}, "B": {}, "C": {}, "D": {}}
    >>> deps  = [{"from_node": "B", "to_node": "A", "type": "structural"},
    ...          {"from_node": "C", "to_node": "A", "type": "assumption"},
    ...          {"from_node": "D", "to_node": "B", "type": "structural"}]
    >>> poison_set("A", nodes, deps) == {"B", "C", "D"}
    True
    """
    all_downstream = dependents(node_id, dependencies, nodes)
    # Restrict to nodes we actually track (ignore stale dep references)
    return all_downstream & set(nodes)


# ---------------------------------------------------------------------------
# ancestors
# ---------------------------------------------------------------------------


def ancestors(
    node_id: str, dependencies: DepList, depth: int = 0, nodes: NodeMap | None = None
) -> list[str]:
    """
    Return node_ids that *node_id* (transitively) depends on, up to *depth*
    hops away. Results are in BFS order — closest ancestors first.

    Does not include *node_id* itself.
    Traverses all dependency types (structural and assumption).

    A depth of 0 returns an empty list.
    A depth of 1 returns direct prerequisites only.

    >>> deps = [{"from_node": "B", "to_node": "A", "type": "structural"},
    ...         {"from_node": "C", "to_node": "B", "type": "structural"},
    ...         {"from_node": "D", "to_node": "C", "type": "structural"}]
    >>> ancestors("D", deps, depth=2)
    ['C', 'B']
    """
    if depth == 0:
        return []

    adj = _forward_adj(dependencies, nodes)
    result: list[str] = []
    visited: set[str] = {node_id}
    queue: deque[tuple[str, int]] = deque([(node_id, 0)])

    while queue:
        current, d = queue.popleft()
        if d >= depth:
            continue
        for prereq in adj.get(current, set()):
            if prereq not in visited:
                visited.add(prereq)
                result.append(prereq)
                queue.append((prereq, d + 1))

    return result


# ---------------------------------------------------------------------------
# cycle_check
# ---------------------------------------------------------------------------


def cycle_check(dependencies: DepList) -> list[list[str]]:
    """
    Return all cycles found in the dependency graph.
    Each cycle is a list of node_ids: [..., start, ..., start] where the
    last element repeats the first to show the closing edge.
    Returns an empty list if the graph is acyclic.

    Uses iterative DFS with three-color marking (white/gray/black) to avoid
    Python recursion limits on deep graphs.

    >>> cycle_check([])
    []
    >>> deps = [{"from_node": "B", "to_node": "A", "type": "structural"},
    ...         {"from_node": "A", "to_node": "B", "type": "structural"}]
    >>> len(cycle_check(deps)) > 0
    True
    """
    fwd = _forward_adj(dependencies)
    all_nodes = sorted(fwd)  # deterministic traversal order

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in all_nodes}
    cycles: list[list[str]] = []

    for start in all_nodes:
        if color[start] != WHITE:
            continue

        # Iterative DFS: stack holds (node, iterator_over_neighbors, path_so_far)
        path: list[str] = []
        path_set: set[str] = set()
        stack: list[tuple[str, "Iterator[str]"]] = [(start, iter(sorted(fwd.get(start, set()))))]
        color[start] = GRAY
        path.append(start)
        path_set.add(start)

        while stack:
            node, neighbors = stack[-1]
            try:
                neighbor = next(neighbors)
                if color.get(neighbor, WHITE) == GRAY and neighbor in path_set:
                    # Back edge — extract the cycle
                    cycle_start_idx = path.index(neighbor)
                    cycles.append(path[cycle_start_idx:] + [neighbor])
                elif color.get(neighbor, WHITE) == WHITE and neighbor in color:
                    color[neighbor] = GRAY
                    path.append(neighbor)
                    path_set.add(neighbor)
                    stack.append((neighbor, iter(sorted(fwd.get(neighbor, set())))))
            except StopIteration:
                # Finished all neighbors of this node
                color[node] = BLACK
                path.pop()
                path_set.discard(node)
                stack.pop()

    return cycles


# ---------------------------------------------------------------------------
# ready_nodes
# ---------------------------------------------------------------------------


def ready_nodes(nodes: NodeMap, dependencies: DepList) -> list[str]:
    """
    Return node_ids that are ready for dispatch, in topological order.

    A node is ready when ALL of:
      1. Its status is 'near'.
      2. All structural dependencies have status 'grounded' or 'provisional'.
         (Provisional deps are allowed — the node will be marked provisional
          itself, but can still be dispatched.)
      3. No structural dependency is 'poisoned' or 'near'.
         (Assumption deps are tracked separately; they don't block dispatch.)

    Nodes with no dependencies are ready if they are 'near'.

    >>> nodes = {"A": {"status": "grounded"}, "B": {"status": "near"}}
    >>> deps  = [{"from_node": "B", "to_node": "A", "type": "structural"}]
    >>> ready_nodes(nodes, deps)
    ['B']
    """
    # Collect structural deps per node
    struct_prereqs: dict[str, set[str]] = {nid: set() for nid in nodes}

    # From global dependencies array
    for dep in dependencies:
        if dep.get("type") == "structural":
            fn, tn = dep["from_node"], dep["to_node"]
            if fn in nodes:
                struct_prereqs.setdefault(fn, set()).add(tn)

    # From node-level structural_deps field (set during seeding)
    for nid, node in nodes.items():
        for dep_nid in node.get("structural_deps", []):
            if dep_nid in nodes:
                struct_prereqs[nid].add(dep_nid)

    _DISPATCHABLE = {"grounded", "provisional"}

    ready: list[str] = []
    for nid, node in nodes.items():
        status = node.get("status")
        # Near nodes: need test_author to define contract
        # Provisional nodes: have tests, need builder implementation
        if status not in ("near", "provisional"):
            continue
        prereqs = struct_prereqs.get(nid, set())
        if all(nodes.get(p, {}).get("status") in _DISPATCHABLE for p in prereqs):
            ready.append(nid)

    # Return in topological order so callers get a stable, priority-consistent list
    try:
        ordered = topo_sort(nodes, dependencies)
        ready_set = set(ready)
        return [nid for nid in ordered if nid in ready_set]
    except ValueError:
        # Cycle in graph — return ready nodes in sorted order as fallback
        return sorted(ready)


# ---------------------------------------------------------------------------
# fan_in_nodes
# ---------------------------------------------------------------------------


def fan_in_nodes(nodes: NodeMap, dependencies: DepList, threshold: int = 2) -> list[str]:
    """
    Return node_ids that have >= threshold incoming dependencies (fan-in).

    Fan-in nodes are convergence points in the dependency graph — nodes that
    multiple downstream nodes depend on. These are candidates for analyst audit
    before SEAL to verify interface consistency.

    >>> nodes = {"A": {}, "B": {}, "C": {}, "D": {}}
    >>> deps = [{"from_node": "C", "to_node": "A", "type": "structural"},
    ...         {"from_node": "C", "to_node": "B", "type": "structural"},
    ...         {"from_node": "D", "to_node": "A", "type": "structural"}]
    >>> fan_in_nodes(nodes, deps, threshold=2)
    ['A']
    """
    # Count incoming edges per node (how many nodes depend on this one)
    fan_in_count: dict[str, int] = {nid: 0 for nid in nodes}

    for dep in dependencies:
        tn = dep.get("to_node")
        if tn in fan_in_count:
            fan_in_count[tn] += 1

    # Return nodes with >= threshold incoming edges, sorted for determinism
    return sorted([nid for nid, count in fan_in_count.items() if count >= threshold])


def fan_in_candidates_for_audit(
    nodes: NodeMap,
    dependencies: DepList,
    threshold: int = 2,
    exclude_statuses: set[str] | None = None,
) -> list[dict]:
    """
    Return fan-in nodes with context for auto-triggered analyst dispatch.

    Each entry contains:
        - node_id: the fan-in node
        - prereq_count: number of prerequisites (dependencies)
        - prereq_nodes: list of nodes this one depends on
        - suggested_query: analyst query string

    Filters out nodes with excluded statuses (e.g., grounded, sealed).

    >>> nodes = {"A": {"status": "near"}, "B": {"status": "grounded"},
    ...          "C": {"status": "near"}, "D": {"status": "near"}}
    >>> deps = [{"from_node": "C", "to_node": "A", "type": "structural"},
    ...         {"from_node": "C", "to_node": "B", "type": "structural"}]
    >>> fan_in_candidates_for_audit(nodes, deps)
    [{'node_id': 'A', 'prereq_count': 2, 'prereq_nodes': ['B', 'C'], ...}]
    """
    if exclude_statuses is None:
        exclude_statuses = {"grounded", "sealed", "poisoned"}

    fan_in = fan_in_nodes(nodes, dependencies, threshold)
    radj = _reverse_adj(dependencies)

    candidates: list[dict] = []
    for nid in fan_in:
        node = nodes.get(nid)
        if not node:
            continue

        status = node.get("status", "near")
        if status in exclude_statuses:
            continue

        prereqs = sorted(radj.get(nid, set()) & set(nodes))

        candidates.append(
            {
                "node_id": nid,
                "prereq_count": len(prereqs),
                "prereq_nodes": prereqs,
                "suggested_query": (
                    f"Verify interface consistency: {nid} has "
                    f"{len(prereqs)} downstream dependents ({', '.join(prereqs[:3])}"
                    f"{', …' if len(prereqs) > 3 else ''}). "
                    "Check for type conflicts, mismatched assumptions, and guarantee violations."
                ),
                "target_nodes": [nid] + prereqs,
            }
        )

    return candidates


# ---------------------------------------------------------------------------
# Integration test
