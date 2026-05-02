"""CLI argument parsing, config loading, and command dispatch."""

import argparse
import os
import sys
from pathlib import Path

# Import dependencies
from config import load_config
from config.schema import Config
from system import initialize_system
from openapi import OpenAPIError


USAGE = """Mock Server CLI

Usage: python -m cli.runner [OPTIONS]

Options:
  --config PATH    Path to JSON config file
  --port PORT      Override config port (1-65535)
  --host HOST      Override config host
  --spec PATH      Path to OpenAPI spec file
  --help           Show this help message and exit
  --version        Show version and exit

Environment Variables:
  PORT             Server port (overrides config file)
  HOST             Server host (overrides config file)
  LOG_LEVEL        Logging level (overrides config file)
  TTL_DEFAULT      Default session TTL (overrides config file)
  MAX_SESSIONS     Maximum sessions (overrides config file)

Exit Codes:
  0  Clean shutdown
  1  Config error
  2  Runtime error
"""


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="mock-server",
        description="Mock HTTP server with OpenAPI support",
        add_help=False,
    )
    
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to JSON config file"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override config port (1-65535)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Override config host"
    )
    parser.add_argument(
        "--spec",
        type=str,
        default=None,
        help="Path to OpenAPI spec file"
    )
    parser.add_argument(
        "--help",
        action="store_true",
        help="Show help message"
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version"
    )
    
    return parser.parse_known_args(argv)


def main(argv: list[str]) -> int:
    """
    Main entry point for the CLI.
    
    Args:
        argv: Command line arguments (excluding program name)
        
    Returns:
        Exit code: 0=clean, 1=config error, 2=runtime error
    """
    # Parse arguments
    try:
        args, remaining = _parse_args(argv)
    except SystemExit as e:
        # argparse exits on --help or errors
        if e.code == 0:
            print(USAGE)
            return 0
        return 1
    
    # Handle --help
    if args.help:
        print(USAGE)
        return 0
    
    # Handle --version
    if args.version:
        print("mock-server 1.0.0")
        return 0
    
    # Check for unknown arguments (remaining contains unparsed args)
    # argparse with parse_known_args doesn't error on unknown args,
    # so we need to check manually
    for arg in argv:
        if arg.startswith("--") and arg not in [
            "--config", "--port", "--host", "--spec", "--help", "--version"
        ]:
            print(f"Unknown option: {arg}")
            print(USAGE)
            return 1
    
    # Load configuration
    try:
        # Build environment dict from actual environment
        env_vars = {}
        for key in ["PORT", "HOST", "LOG_LEVEL", "TTL_DEFAULT", "MAX_SESSIONS"]:
            if key in os.environ:
                env_vars[key] = os.environ[key]
        
        config = load_config(env_vars, args.config)
        
    except ValueError as e:
        print(f"Config error: {e}")
        return 1
    except Exception as e:
        print(f"Config error: {e}")
        return 1
    
    # Apply CLI overrides
    if args.port is not None:
        try:
            if args.port < 1 or args.port > 65535:
                print(f"Config error: Port must be between 1 and 65535, got {args.port}")
                return 1
            # Create new config with overridden port
            config = Config(
                port=args.port,
                host=config.host,
                log_level=config.log_level,
                ttl_default=config.ttl_default,
                max_sessions=config.max_sessions,
            )
        except Exception as e:
            print(f"Config error: {e}")
            return 1
    
    if args.host is not None:
        config = Config(
            port=config.port,
            host=args.host,
            log_level=config.log_level,
            ttl_default=config.ttl_default,
            max_sessions=config.max_sessions,
        )
    
    # Validate spec file exists if provided
    if args.spec is not None:
        if not Path(args.spec).exists():
            print(f"Config error: Spec file not found: {args.spec}")
            return 1
    
    # Initialize and run system
    try:
        context = initialize_system(config, args.spec)
        
        # Run the server (this blocks until shutdown)
        from http import run_server
        run_server(context.server)
        
        return 0
        
    except OpenAPIError as e:
        print(f"Config error: OpenAPI spec error: {e}")
        return 1
    except IOError as e:
        # Port binding failure or other I/O error
        print(f"Runtime error: {e}")
        return 2
    except Exception as e:
        # Check if it's a runtime vs config error
        error_str = str(e).lower()
        if "port" in error_str and ("bind" in error_str or "permission" in error_str):
            print(f"Runtime error: {e}")
            return 2
        elif "config" in error_str or "file" in error_str:
            print(f"Config error: {e}")
            return 1
        else:
            print(f"Runtime error: {e}")
            return 2
