# cli/commands/config.py — A7-RT configuration management

"""Manage A7-RT configuration.

Usage:
    a7-rt config [options]

Options:
    --session=<path>         Show config for specific session
    --init[=<path>]          Initialize .a7 directory (default: current)
    --with-key=<key>         API key to store (use with --init)
    --manager-model=<name>   Set manager model (with --init)
    --subagent-model=<name>  Set subagent model (with --init)
    --models                 List available models
    --providers              List configured providers
    --validate               Validate current configuration
    --help, -h               Show this help message

Examples:
    a7-rt config                            Show current config
    a7-rt config --models                   List available models
    a7-rt config --init --with-key=xxx      Init with API key
    a7-rt config --init --manager-model=gpt-4o --subagent-model=gpt-4o-mini
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def run_config(argv: list[str]) -> int:
    """Run the config command."""
    args = _parse_args(argv)

    if args.get("help"):
        print(_HELP)
        return 0

    # Handle --init: seed .a7 directory
    if args.get("init") is not None:
        target = Path(args["init"]).resolve()
        target.mkdir(parents=True, exist_ok=True)

        key_value = args.get("with_key")
        if key_value is None:
            key_value = os.environ.get("OPENROUTER_API_KEY", "").strip()

        manager_model = args.get("manager_model")
        subagent_model = args.get("subagent_model")

        try:
            from a7_rt_core.core.config import seed_a7_directory

            seed_a7_directory(
                target_path=target,
                with_key=bool(key_value),
                key_value=key_value,
                manager_model=manager_model,
                subagent_model=subagent_model,
            )
            a7_dir = target / ".a7"
            print(f"Initialized A7 config directory: {a7_dir}")

            # List created files
            for item in sorted(a7_dir.iterdir()):
                item_type = "dir" if item.is_dir() else "file"
                print(f"  {item_type}: {item.name}")

            # If models were specified, show configuration hint
            if manager_model or subagent_model:
                print(f"\nConfigured models:")
                if manager_model:
                    print(f"  manager:  {manager_model}")
                if subagent_model:
                    print(f"  subagent: {subagent_model}")
                print("\nNote: Ensure these models are defined in config.toml")

        except Exception as exc:
            print(f"error: failed to initialize .a7: {exc}", file=sys.stderr)
            return 1
        return 0

    # Load config (either from session or default)
    session_path = Path(args["session"]) if args.get("session") else None

    try:
        from a7_rt_core.core.config import (
            MODEL_REGISTRY,
            PROVIDER_DEFAULTS,
            load_config,
        )

        config = load_config(session_path=session_path)
    except Exception as exc:
        print(f"error: failed to load config: {exc}", file=sys.stderr)
        return 1

    # Handle --models
    if args.get("models"):
        print("Available Models")
        print("=" * 50)

        print("\nBuilt-in models:")
        for name, info in sorted(MODEL_REGISTRY.items()):
            roles = ", ".join(info.get("role", ["unknown"]))
            print(f"  {name:20} -> {info['model_id']}")
            print(f"      roles: {roles}, provider: {info.get('provider', 'unknown')}")

        # Custom models from config
        if config.models:
            print("\nCustom models (from config):")
            for name, model_config in sorted(config.models.items()):
                print(f"  {name:20} -> {model_config.model_id}")
                print(f"      provider: {model_config.provider}")

        # Effective models
        print("\nEffective models:")
        print(f"  manager:  {config.get_effective_model('manager')}")
        print(f"  subagent: {config.get_effective_model('subagent')}")
        return 0

    # Handle --providers
    if args.get("providers"):
        print("Configured Providers")
        print("=" * 50)

        for name, info in sorted(PROVIDER_DEFAULTS.items()):
            print(f"\n  {name}:")
            print(f"    base_url: {info['base_url']}")
            print(f"    api_key_env: {info.get('api_key_env', 'N/A')}")

            # Check if key is resolvable
            key = config.resolve_api_key(name)
            status = "available" if key else "not found"
            print(f"    key status: {status}")
        return 0

    # Handle --validate
    if args.get("validate"):
        errors = config.validate()
        if errors:
            print("Configuration Errors")
            print("=" * 50)
            for err in errors:
                print(f"  ✗ {err}")
            print("\nFix these issues in your config.toml or run:")
            print("  a7-rt config --init  # to create a fresh config")
            return 1
        else:
            print("Configuration Valid")
            print("=" * 50)
            print("  ✓ Providers configured")
            print("  ✓ Models configured")
            print("  ✓ Role mappings valid")
            return 0

    # Default: show full config
    print("A7-RT Configuration")
    print("=" * 50)

    if config.session_path:
        print(f"\nSession path: {config.session_path}")
    if config.project_root:
        print(f"Project root: {config.project_root}")

    # Show validation warnings if any
    errors = config.validate()
    if errors:
        print("\n⚠ Configuration Issues:")
        for err in errors:
            print(f"  - {err}")

    print(f"\nModels:")
    print(f"  Manager:  {config.manager_model}")
    print(f"  Subagent: {config.subagent_model}")

    print(f"\nConfigured Providers ({len(config.providers)}):")
    for name, provider in sorted(config.providers.items()):
        key_status = "✓" if provider.resolve_api_key([Path.cwd()]) else "✗"
        print(f"  [{key_status}] {name}: {provider.base_url}")

    print(f"\nConfigured Models ({len(config.models)}):")
    for name, model in sorted(config.models.items()):
        print(f"  - {name}: {model.model_id} (via {model.provider})")

    print(f"\nSession defaults:")
    print(f"  Manager max turns: {config.default_manager_max_turns}")
    print(f"  Drain turn: {config.default_drain_turn}")
    print(f"  Provisional depth: {config.provisional_depth_limit}")

    print(f"\nFeatures:")
    print(f"  Auto-validate: {config.auto_validate}")
    print(f"  Auto-commit: {config.auto_commit}")
    print(f"  ShadowFS: {config.use_shadowfs}")

    return 0


def _parse_args(argv: list[str]) -> dict[str, Any]:
    """Parse config command arguments."""
    args: dict[str, Any] = {
        "session": None,
        "init": None,
        "with_key": None,
        "manager_model": None,
        "subagent_model": None,
        "models": False,
        "providers": False,
        "validate": False,
        "help": False,
    }

    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg in ("--help", "-h"):
            args["help"] = True
            return args

        elif arg == "--models":
            args["models"] = True

        elif arg == "--providers":
            args["providers"] = True

        elif arg.startswith("--session="):
            args["session"] = arg.split("=", 1)[1]

        elif arg == "--session":
            if i + 1 < len(argv):
                i += 1
                args["session"] = argv[i]
            else:
                print("error: --session requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--init"):
            # --init or --init=path
            if "=" in arg:
                args["init"] = arg.split("=", 1)[1]
            else:
                args["init"] = "."

        elif arg.startswith("--with-key="):
            args["with_key"] = arg.split("=", 1)[1]

        elif arg == "--with-key":
            if i + 1 < len(argv):
                i += 1
                args["with_key"] = argv[i]
            else:
                print("error: --with-key requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--manager-model="):
            args["manager_model"] = arg.split("=", 1)[1]

        elif arg == "--manager-model":
            if i + 1 < len(argv):
                i += 1
                args["manager_model"] = argv[i]
            else:
                print("error: --manager-model requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg.startswith("--subagent-model="):
            args["subagent_model"] = arg.split("=", 1)[1]

        elif arg == "--subagent-model":
            if i + 1 < len(argv):
                i += 1
                args["subagent_model"] = argv[i]
            else:
                print("error: --subagent-model requires a value", file=sys.stderr)
                sys.exit(1)

        elif arg == "--validate":
            args["validate"] = True

        else:
            print(f"error: unknown option: {arg}", file=sys.stderr)
            sys.exit(1)

        i += 1

    return args


_HELP = """
Usage: a7-rt config [options]

Manage A7-RT configuration.

Options:
  --session=<path>         Show config for specific session
  --init[=<path>]          Initialize .a7 directory (default: current dir)
  --with-key=<key>         API key to store with --init
  --manager-model=<name>   Set manager model (with --init)
  --subagent-model=<name>  Set subagent model (with --init)
  --models                 List available models
  --providers              List configured providers
  --validate               Validate current configuration
  --help, -h               Show this help message

Examples:
  a7-rt config                            Show current config
  a7-rt config --models                   List available models
  a7-rt config --providers                List API providers
  a7-rt config --validate                 Check configuration
  a7-rt config --init                     Initialize .a7 in current dir
  a7-rt config --init=/path --with-key=xxx --manager-model=gpt-4o
"""
