"""
Extracted inline tests for graph.py.
Run standalone: python tests/test_graph.py
"""

import os
import sys

# Allow imports from the parent (a7-rt-core) directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from a7_rt_core.core.graph import (
    DepList,
    NodeMap,
    ancestors,
    cycle_check,
    dependents,
    poison_set,
    ready_nodes,
    topo_sort,
)


def _test_graph() -> None:
    """
    Test all graph functions against a known fixture.

    Graph layout (all structural unless noted):

         A (grounded)
        / \\
       B   C        B←structural─A, C←structural─A
    (grnd) (prov)
        \\ /
         D (near)   D←structural─B, D←assumption─C   ← ready (B grnd, C prov)
         |
         E (near)   E←structural─D                   ← NOT ready (D is near)

    F (near, no deps)  ← ready immediately
    G (near)  G←structural─H                         ← NOT ready (H poisoned)
    H (poisoned)
    """
    ok = "\033[32mOK\033[0m"
    fail = "\033[31mFAIL\033[0m"
    errors: list[str] = []

    def check(label: str, condition: bool) -> None:
        if condition:
            print(f"  [{ok}] {label}")
        else:
            print(f"  [{fail}] {label}")
            errors.append(label)

    # ── Fixture ───────────────────────────────────────────────────────────────

    nodes: NodeMap = {
        "A": {"node_id": "A", "status": "grounded", "type": "feature"},
        "B": {"node_id": "B", "status": "grounded", "type": "feature"},
        "C": {"node_id": "C", "status": "provisional", "type": "feature"},
        "D": {"node_id": "D", "status": "near", "type": "feature"},
        "E": {"node_id": "E", "status": "near", "type": "feature"},
        "F": {"node_id": "F", "status": "near", "type": "feature"},
        "G": {"node_id": "G", "status": "near", "type": "feature"},
        "H": {"node_id": "H", "status": "poisoned", "type": "feature"},
    }

    deps: DepList = [
        {"from_node": "B", "to_node": "A", "type": "structural"},
        {"from_node": "C", "to_node": "A", "type": "structural"},
        {"from_node": "D", "to_node": "B", "type": "structural"},
        {"from_node": "D", "to_node": "C", "type": "assumption"},
        {"from_node": "E", "to_node": "D", "type": "structural"},
        {"from_node": "G", "to_node": "H", "type": "structural"},
    ]

    # ── topo_sort ─────────────────────────────────────────────────────────────

    order = topo_sort(nodes, deps)

    def before(a: str, b: str) -> bool:
        return order.index(a) < order.index(b)

    check("topo: A before B", before("A", "B"))
    check("topo: A before C", before("A", "C"))
    check("topo: B before D", before("B", "D"))
    check("topo: C before D", before("C", "D"))
    check("topo: D before E", before("D", "E"))
    check("topo: all nodes present", set(order) == set(nodes))

    # empty graph
    check("topo: empty nodes", topo_sort({}, []) == [])

    # isolated node
    check("topo: isolated node", topo_sort({"X": {}}, []) == ["X"])

    # cycle detection
    cyclic_deps: DepList = [
        {"from_node": "P", "to_node": "Q", "type": "structural"},
        {"from_node": "Q", "to_node": "P", "type": "structural"},
    ]
    try:
        topo_sort({"P": {}, "Q": {}}, cyclic_deps)
        check("topo: cycle raises ValueError", False)
    except ValueError:
        check("topo: cycle raises ValueError", True)

    # ── dependents ────────────────────────────────────────────────────────────

    check("dependents(A) = {B,C,D,E}", dependents("A", deps) == {"B", "C", "D", "E"})
    check("dependents(B) = {D,E}", dependents("B", deps) == {"D", "E"})
    check("dependents(D) = {E}", dependents("D", deps) == {"E"})
    check("dependents(E) = {} (leaf)", dependents("E", deps) == set())
    check("dependents(F) = {} (isolated)", dependents("F", deps) == set())

    # Assumption edges are traversed — C has assumption dep D
    check("dependents(C) includes D via assumption", "D" in dependents("C", deps))

    # ── poison_set ────────────────────────────────────────────────────────────

    # Poisoning A should cascade to B, C (via structural/structural from A),
    # then D (structural from B, assumption from C), then E (structural from D)
    ps_A = poison_set("A", nodes, deps)
    check("poison_set(A) = {B,C,D,E}", ps_A == {"B", "C", "D", "E"})

    # Poisoning H: only G depends on H
    check("poison_set(H) = {G}", poison_set("H", nodes, deps) == {"G"})

    # Poisoning E (leaf): nothing downstream
    check("poison_set(E) = {}", poison_set("E", nodes, deps) == set())

    # Restricts to known nodes — stale edge to unknown node is ignored
    stale_deps: DepList = [
        {"from_node": "GHOST", "to_node": "A", "type": "structural"},
    ]
    check(
        "poison_set ignores unknown nodes", poison_set("A", nodes, stale_deps) == set()
    )

    # ── ancestors ─────────────────────────────────────────────────────────────

    check("ancestors(E, depth=0) = []", ancestors("E", deps, depth=0) == [])
    check("ancestors(E, depth=1) = [D]", ancestors("E", deps, depth=1) == ["D"])

    anc_E_2 = ancestors("E", deps, depth=2)
    check("ancestors(E, depth=2) has D", "D" in anc_E_2)
    check("ancestors(E, depth=2) has B", "B" in anc_E_2)
    check("ancestors(E, depth=2) has C", "C" in anc_E_2)
    check(
        "ancestors(E, depth=2) D before B and C",
        anc_E_2.index("D") < anc_E_2.index("B")
        and anc_E_2.index("D") < anc_E_2.index("C"),
    )

    anc_E_3 = ancestors("E", deps, depth=3)
    check("ancestors(E, depth=3) includes A", "A" in anc_E_3)
    check("ancestors(E, depth=3) does not include E", "E" not in anc_E_3)

    check("ancestors(F, depth=5) = [] (no deps)", ancestors("F", deps, depth=5) == [])
    check("ancestors(A, depth=5) = [] (root node)", ancestors("A", deps, depth=5) == [])

    # ── cycle_check ───────────────────────────────────────────────────────────

    check("cycle_check: acyclic graph → []", cycle_check(deps) == [])
    check("cycle_check: empty → []", cycle_check([]) == [])

    # Simple two-node cycle
    two_cycle: DepList = [
        {"from_node": "P", "to_node": "Q", "type": "structural"},
        {"from_node": "Q", "to_node": "P", "type": "structural"},
    ]
    cycles = cycle_check(two_cycle)
    check("cycle_check: 2-cycle detected", len(cycles) == 1)
    check("cycle_check: cycle involves P, Q", set(cycles[0]) == {"P", "Q"})

    # Three-node cycle: R→S→T→R
    three_cycle: DepList = [
        {"from_node": "R", "to_node": "S", "type": "structural"},
        {"from_node": "S", "to_node": "T", "type": "structural"},
        {"from_node": "T", "to_node": "R", "type": "structural"},
    ]
    cycles3 = cycle_check(three_cycle)
    check("cycle_check: 3-cycle detected", len(cycles3) >= 1)
    all_cycle_nodes = {n for c in cycles3 for n in c}
    check("cycle_check: all 3 nodes in cycle", {"R", "S", "T"} <= all_cycle_nodes)

    # Self-loop
    self_loop: DepList = [{"from_node": "X", "to_node": "X", "type": "structural"}]
    check("cycle_check: self-loop detected", len(cycle_check(self_loop)) >= 1)

    # ── ready_nodes ───────────────────────────────────────────────────────────

    ready = ready_nodes(nodes, deps)

    check("ready: D is ready (B grnd, C prov)", "D" in ready)
    check("ready: F is ready (no deps)", "F" in ready)
    check("ready: E not ready (D is near)", "E" not in ready)
    check("ready: G not ready (H is poisoned)", "G" not in ready)
    check("ready: A not ready (grounded)", "A" not in ready)

    # D must come before E in any order (D is a prerequisite of E)
    # D is ready, E is not — so only testing D is in the list
    check("ready: returns list", isinstance(ready, list))

    # No near nodes → empty
    all_grounded: NodeMap = {
        "X": {"status": "grounded"},
        "Y": {"status": "grounded"},
    }
    check("ready: no near nodes → []", ready_nodes(all_grounded, []) == [])

    # Node with only assumption dep on near node IS ready (assumption deps don't block)
    assumption_only_nodes: NodeMap = {
        "M": {"status": "grounded"},
        "N": {"status": "near"},
    }
    assumption_only_deps: DepList = [
        {"from_node": "N", "to_node": "M", "type": "assumption"},
    ]
    # N has only an assumption dep — assumption deps don't block dispatch
    # But M is grounded, so N is ready regardless
    check(
        "ready: assumption dep on grounded node → ready",
        "N" in ready_nodes(assumption_only_nodes, assumption_only_deps),
    )

    # near dep via structural blocks, but assumption dep does not
    mixed_nodes: NodeMap = {
        "X": {"status": "near"},
        "Y": {"status": "grounded"},
        "Z": {
            "status": "near"
        },  # structural dep on X (near), assumption dep on Y (grounded)
    }
    mixed_deps: DepList = [
        {"from_node": "Z", "to_node": "X", "type": "structural"},
        {"from_node": "Z", "to_node": "Y", "type": "assumption"},
    ]
    check(
        "ready: structural near dep blocks dispatch",
        "Z" not in ready_nodes(mixed_nodes, mixed_deps),
    )

    # ── Summary ───────────────────────────────────────────────────────────────

    print()
    if errors:
        print(f"\033[31m{len(errors)} test(s) failed:\033[0m")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\033[32mAll graph tests passed.\033[0m")


if __name__ == "__main__":
    print("Running graph tests...")
    _test_graph()
