# cli/commands/seed.py — Add nodes to an existing A7-RT session

"""Add nodes to an existing A7-RT session.

Usage:
    python -m cli seed <session-path> [options]

Options:
    --nodes=<json>           Node definition as JSON (repeatable)
    --nodes-file=<path>      JSON file containing node list
    --spec=<source>          Batch specification (file or JSON)
    --deps=<json>            Dependency edge as JSON (repeatable)
    --deps-file=<path>       JSON file containing array of dependency edges

Examples:
    python -m cli seed /tmp/my-session --nodes='{"id":"auth","type":"feature"}'
    python -m cli seed /tmp/my-session --nodes-file=nodes.json
    python -m cli seed /tmp/my-session --deps='{"from":"api","to":"auth","type":"structural"}'
    python -m cli seed /tmp/my-session --spec=nodes.json --deps-file=deps.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from a7_rt_core.core.models import Dependency, DependencyType


def run_seed(argv: list[str]) -> int:
    """Run the seed command."""
    args = _parse_args(argv)

    if args.get("help"):
        print(_HELP)
        return 0

    session_path_str = args.get("session_path")
    if not session_path_str:
        print("error: session path is required", file=sys.stderr)
        print("Usage: python -m cli seed <session-path> [options]", file=sys.stderr)
        return 1

    session_path = Path(session_path_str).resolve()
    master_path = session_path / "master.json"

    if not master_path.exists():
        print(f"error: no session found at {session_path}", file=sys.stderr)
        print("Run 'python -m cli init' first.", file=sys.stderr)
        return 1

    # Load existing master
    try:
        with open(master_path) as f:
            master = json.load(f)
    except Exception as exc:
        print(f"error: cannot read master.json: {exc}", file=sys.stderr)
        return 1

    # Determine default stage (first active, or first available)
    stages = master.get("stages", {})
    active_stages = [s for s in stages.values() if s.get("status") == "active"]
    if active_stages:
        default_stage_id = active_stages[0]["stage_id"]
    else:
        default_stage_id = list(stages.keys())[0] if stages else "stage-1"

    # Collect nodes to add with their target stages
    nodes_to_add: list[tuple[str, dict[str, Any]]] = []  # (stage_id, node_def)
    stages_needed: set[str] = set()

    # From --nodes
    for node_json in args.get("nodes", []):
        try:
            node = json.loads(node_json)
            node_stage = node.pop("stage", None)
            target_stage = node_stage or default_stage_id
            nodes_to_add.append((target_stage, node))
            stages_needed.add(target_stage)
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON in --nodes: {exc}", file=sys.stderr)
            return 1

    # From --nodes-file
    if args.get("nodes_file"):
        file_path = Path(args["nodes_file"])
        try:
            with open(file_path) as f:
                file_nodes = json.load(f)
                if isinstance(file_nodes, list):
                    for node in file_nodes:
                        node_stage = node.pop("stage", None)
                        target_stage = node_stage or default_stage_id
                        nodes_to_add.append((target_stage, node))
                        stages_needed.add(target_stage)
                else:
                    node_stage = file_nodes.pop("stage", None)
                    target_stage = node_stage or default_stage_id
                    nodes_to_add.append((target_stage, file_nodes))
                    stages_needed.add(target_stage)
        except Exception as exc:
            print(f"error: cannot read nodes file: {exc}", file=sys.stderr)
            return 1

    # From --spec
    if args.get("spec"):
        spec_source = args["spec"]
        if spec_source == "-":
            # Read from stdin
            try:
                spec_data = sys.stdin.read()
                spec_nodes = json.loads(spec_data)
                if isinstance(spec_nodes, list):
                    for node in spec_nodes:
                        node_stage = node.pop("stage", None)
                        target_stage = node_stage or default_stage_id
                        nodes_to_add.append((target_stage, node))
                        stages_needed.add(target_stage)
                else:
                    node_stage = spec_nodes.pop("stage", None)
                    target_stage = node_stage or default_stage_id
                    nodes_to_add.append((target_stage, spec_nodes))
                    stages_needed.add(target_stage)
            except Exception as exc:
                print(f"error: cannot read spec from stdin: {exc}", file=sys.stderr)
                return 1
        else:
            # Read from file
            spec_path = Path(spec_source)
            try:
                with open(spec_path) as f:
                    spec_nodes = json.load(f)
                    if isinstance(spec_nodes, list):
                        for node in spec_nodes:
                            node_stage = node.pop("stage", None)
                            target_stage = node_stage or default_stage_id
                            nodes_to_add.append((target_stage, node))
                            stages_needed.add(target_stage)
                    else:
                        node_stage = spec_nodes.pop("stage", None)
                        target_stage = node_stage or default_stage_id
                        nodes_to_add.append((target_stage, spec_nodes))
                        stages_needed.add(target_stage)
            except Exception as exc:
                print(f"error: cannot read spec file: {exc}", file=sys.stderr)
                return 1

    if not nodes_to_add and not args.get("deps") and not args.get("deps_file"):
        print(
            "error: no nodes or dependencies specified. Use --nodes, --nodes-file, --spec, --deps, or --deps-file.",
            file=sys.stderr,
        )
        return 1

    # Ensure all required stages exist (bypass single-active-stage restriction)
    from a7_rt_core.core.models import Stage, StageStatus

    for stage_id in stages_needed:
        if stage_id not in master.get("stages", {}):
            stage = Stage(
                stage_id=stage_id,
                project_id=master["project"]["project_id"],
                name=stage_id,
                status=StageStatus.ACTIVE,
            )
            master.setdefault("stages", {})[stage_id] = stage.model_dump(mode="json")
            master["project"].setdefault("stage_ids", []).append(stage_id)
            print(f"Created stage: {stage_id}")

    # Continue with in-memory master (stages already created above)
    existing_nodes = master.get("nodes", {})
    added_count = 0
    skipped_count = 0
    added_node_ids: list[str] = []

    for target_stage, node in nodes_to_add:
        # Restore stage field for debugging/info
        node["stage"] = target_stage
        node_id = node.get("id") or node.get("node_id")
        if not node_id:
            print(f"warning: skipping node without id: {node}", file=sys.stderr)
            continue

        if node_id in existing_nodes:
            print(f"warning: node '{node_id}' already exists, skipping", file=sys.stderr)
            skipped_count += 1
            continue

        # Normalize node structure
        normalized = {
            "node_id": node_id,
            "type": node.get("type", "feature"),
            "stage_id": target_stage,
            "status": "near",
            "description": node.get("description", ""),
            "interface": {
                "exports": node.get("interface", {}).get("exports", [])
                if node.get("interface")
                else node.get("exports", []),
                "assumptions": node.get("interface", {}).get("assumptions", [])
                if node.get("interface")
                else node.get("assumptions", []),
                "raises": node.get("interface", {}).get("raises", [])
                if node.get("interface")
                else node.get("raises", []),
                "guarantees": node.get("interface", {}).get("guarantees", [])
                if node.get("interface")
                else node.get("guarantees", []),
            },
            "structural_deps": node.get("deps") or node.get("structural_deps", []),
            "assumption_deps": node.get("assumption_deps", []),
            "content_file": f"{node_id}.py",
            "committed_files": [],
        }

        existing_nodes[node_id] = normalized
        added_count += 1
        added_node_ids.append(node_id)

    # Update stage node_ids for newly added nodes
    for node_id in added_node_ids:
        node = existing_nodes.get(node_id)
        if node:
            stage_id = node.get("stage_id", default_stage_id)
            if stage_id in master.get("stages", {}):
                stage = master["stages"][stage_id]
                if node_id not in stage.get("node_ids", []):
                    stage.setdefault("node_ids", []).append(node_id)

    # Save master with nodes
    master["nodes"] = existing_nodes

    try:
        with open(master_path, "w") as f:
            json.dump(master, f, indent=2)
    except Exception as exc:
        print(f"error: cannot write master.json: {exc}", file=sys.stderr)
        return 1

    print(f"Added {added_count} node(s) to session: {session_path}")
    for node_id in added_node_ids:
        print(f"  - {node_id}")
    if skipped_count:
        print(f"Skipped {skipped_count} existing node(s)")

    # Collect and add dependencies
    deps_to_add: list[dict[str, Any]] = []

    # From --deps-file
    if args.get("deps_file"):
        deps_path = Path(args["deps_file"])
        try:
            with open(deps_path) as f:
                file_deps = json.load(f)
                if isinstance(file_deps, list):
                    deps_to_add.extend(file_deps)
                else:
                    print(
                        "error: --deps-file must contain a JSON array of dependency objects.",
                        file=sys.stderr,
                    )
                    return 1
        except Exception as exc:
            print(f"error: cannot read deps file: {exc}", file=sys.stderr)
            return 1

    # From --deps
    for dep_json in args.get("deps", []):
        try:
            dep = json.loads(dep_json)
            deps_to_add.append(dep)
        except json.JSONDecodeError as exc:
            print(f"error: invalid JSON in --deps: {exc}", file=sys.stderr)
            return 1

    # Add dependencies to master
    deps_added = 0
    deps_skipped = 0

    for dep_data in deps_to_add:
        from_node = dep_data.get("from")
        to_node = dep_data.get("to")
        dep_type_str = dep_data.get("type", "structural")

        if not from_node or not to_node:
            print(
                f"warning: skipping dependency without 'from' or 'to': {dep_data}",
                file=sys.stderr,
            )
            deps_skipped += 1
            continue

        # Check that referenced nodes exist
        if from_node not in existing_nodes:
            print(
                f"warning: dependency from '{from_node}' references unknown node, skipping",
                file=sys.stderr,
            )
            deps_skipped += 1
            continue
        if to_node not in existing_nodes:
            print(
                f"warning: dependency to '{to_node}' references unknown node, skipping",
                file=sys.stderr,
            )
            deps_skipped += 1
            continue

        # Parse dependency type
        try:
            dep_type = DependencyType(dep_type_str.upper())
        except ValueError:
            dep_type = DependencyType.STRUCTURAL

        # Create and add dependency
        dependency = Dependency(
            from_node=from_node,
            to_node=to_node,
            type=dep_type,
        )

        master.setdefault("dependencies", [])
        master["dependencies"].append(dependency.model_dump(mode="json"))
        deps_added += 1
        print(f"  → Dependency: {from_node} → {to_node}")

    # Save master with dependencies
    if deps_added > 0 or deps_skipped > 0:
        try:
            with open(master_path, "w") as f:
                json.dump(master, f, indent=2)
        except Exception as exc:
            print(f"error: cannot write master.json: {exc}", file=sys.stderr)
            return 1

    if deps_added > 0:
        print(f"Added {deps_added} dependency edge(s)")

    return 0


def _parse_args(argv: list[str]) -> dict[str, Any]:
    """Parse seed command arguments."""
    args: dict[str, Any] = {
        "session_path": None,
        "nodes": [],
        "nodes_file": None,
        "spec": None,
        "deps": [],
        "deps_file": None,
        "help": False,
    }

    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg in ("--help", "-h"):
            args["help"] = True
            return args

        elif arg.startswith("--nodes="):
            args["nodes"].append(arg.split("=", 1)[1])

        elif arg == "--nodes":
            if i + 1 < len(argv):
                i += 1
                args["nodes"].append(argv[i])
            else:
                print("error: --nodes requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--nodes-file="):
            args["nodes_file"] = arg.split("=", 1)[1]

        elif arg == "--nodes-file":
            if i + 1 < len(argv):
                i += 1
                args["nodes_file"] = argv[i]
            else:
                print("error: --nodes-file requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--deps="):
            args["deps"].append(arg.split("=", 1)[1])

        elif arg == "--deps":
            if i + 1 < len(argv):
                i += 1
                args["deps"].append(argv[i])
            else:
                print("error: --deps requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--deps-file="):
            args["deps_file"] = arg.split("=", 1)[1]

        elif arg == "--deps-file":
            if i + 1 < len(argv):
                i += 1
                args["deps_file"] = argv[i]
            else:
                print("error: --deps-file requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--spec="):
            args["spec"] = arg.split("=", 1)[1]

        elif arg == "--spec":
            if i + 1 < len(argv):
                i += 1
                args["spec"] = argv[i]
            else:
                print("error: --spec requires a value", file=sys.stderr)
                sys.exit(1)

        elif not arg.startswith("-"):
            if args["session_path"] is None:
                args["session_path"] = arg
            else:
                print(f"error: unexpected argument: {arg}", file=sys.stderr)
                sys.exit(1)

        else:
            print(f"error: unknown option: {arg}", file=sys.stderr)
            sys.exit(1)

        i += 1

    return args


_HELP = """
Usage: python -m cli seed <session-path> [options]

Add nodes to an existing A7-RT session.

Arguments:
  session-path             Path to A7-RT session (required, must contain master.json)

Options:
  --nodes=<json>           Node definition as JSON (repeatable)
  --nodes-file=<path>      JSON file containing array of nodes
  --spec=<source>          Batch spec: file path, JSON string, or "-" for stdin
  --deps=<json>            Dependency edge as JSON (repeatable)
  --deps-file=<path>       JSON file containing array of dependency edges
  --help, -h               Show this help message

Node JSON format:
  {"id": "node-id", "type": "feature|glue|test", "description": "...", "deps": ["..."]}

Dependency JSON format:
  {"from": "node-a", "to": "node-b", "type": "structural"}

Examples:
  python -m cli seed /tmp/my-session --nodes='{"id":"auth","type":"feature"}'
  python -m cli seed /tmp/my-session --nodes-file=nodes.json
  python -m cli seed /tmp/my-session --deps='{"from":"api","to":"auth","type":"structural"}'
  python -m cli seed /tmp/my-session --spec=nodes.json --deps-file=deps.json
  cat nodes.json | python -m cli seed /tmp/my-session --spec=- --deps-file=deps.json
"""
