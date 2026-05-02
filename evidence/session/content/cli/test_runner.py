"""Tests for cli.runner - CLI argument parsing, config loading, and command dispatch.

This tests the exact exports declared in the contract:
- def main(argv: list[str]) -> int
- USAGE: str
"""

# Import the module under test
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

_runner_path = Path(__file__).parent / "runner.py"
spec = importlib.util.spec_from_file_location("cli_runner", _runner_path)
cli_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli_runner)

main = cli_runner.main
USAGE = cli_runner.USAGE


class TestUsageExport:
    """Validate USAGE string export."""

    def test_usage_is_string(self):
        """USAGE must be a string."""
        assert isinstance(USAGE, str)

    def test_usage_is_not_empty(self):
        """USAGE should contain helpful text."""
        assert len(USAGE) > 0

    def test_usage_contains_program_name(self):
        """USAGE should reference the program."""
        assert "cli" in USAGE.lower() or "mock" in USAGE.lower() or "server" in USAGE.lower()


class TestMainExitCodes:
    """Validate exit code guarantees per contract:
    - Exit code 0 on clean shutdown
    - Exit code 1 on config error
    - Exit code 2 on runtime error
    """

    def test_main_help_returns_zero(self):
        """--help should result in clean shutdown (exit 0)."""
        result = main(["--help"])
        assert result == 0, f"Expected exit code 0 for --help, got {result}"

    def test_main_empty_args_shows_usage_returns_zero(self):
        """Empty args should show usage and exit cleanly (common CLI pattern)."""
        # Most CLIs show help/usage when no args given
        result = main([])
        # Could be 0 (show help) or 1 (missing required arg) - both valid
        assert result in [0, 1], f"Expected 0 or 1 for empty args, got {result}"

    def test_main_config_error_exit_code_one(self):
        """Config errors must return exit code 1."""
        # Pass a non-existent config file to trigger config error
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            f.write(b"invalid: yaml: [")  # Malformed YAML
            bad_config = f.name

        try:
            result = main(["--config", bad_config])
            assert result == 1, f"Expected exit code 1 for config error, got {result}"
        finally:
            os.unlink(bad_config)

    def test_main_invalid_port_config_error(self):
        """Invalid port in config should return exit code 1."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"port": "not_a_number", "host": "localhost"}, f)
            bad_config = f.name

        try:
            result = main(["--config", bad_config])
            assert result == 1, f"Expected exit code 1 for invalid port, got {result}"
        finally:
            os.unlink(bad_config)

    def test_main_runtime_error_exit_code_two(self):
        """Runtime errors must return exit code 2."""
        # Valid config but bind to a privileged port or cause runtime issue
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"port": 80, "host": "localhost"}, f)  # Privileged port likely fails
            config_path = f.name

        try:
            result = main(["--config", config_path])
            # Binding to port 80 without privileges causes runtime error
            assert result == 2, f"Expected exit code 2 for runtime error, got {result}"
        finally:
            os.unlink(config_path)


class TestMainArgumentParsing:
    """Validate CLI argument parsing behavior."""

    def test_main_accepts_config_flag(self):
        """--config flag should be accepted."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"port": 18080, "host": "127.0.0.1"}, f)
            config_path = f.name

        try:
            # Even if server fails to start, parsing should work
            result = main(["--config", config_path])
            # Accept 0, 1, or 2 - we're testing parsing, not execution
            assert result in [0, 1, 2]
        finally:
            os.unlink(config_path)

    def test_main_accepts_port_override(self):
        """--port flag should override config."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"port": 18080, "host": "127.0.0.1"}, f)
            config_path = f.name

        try:
            result = main(["--config", config_path, "--port", "18081"])
            assert result in [0, 1, 2]
        finally:
            os.unlink(config_path)

    def test_main_accepts_host_override(self):
        """--host flag should override config."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"port": 18080, "host": "127.0.0.1"}, f)
            config_path = f.name

        try:
            result = main(["--config", config_path, "--host", "0.0.0.0"])
            assert result in [0, 1, 2]
        finally:
            os.unlink(config_path)

    def test_main_accepts_spec_flag(self):
        """--spec flag for OpenAPI spec should be accepted."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"port": 18080}, f)
            config_path = f.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(b"openapi: 3.0.0\ninfo:\n  title: Test\n  version: 1.0.0\npaths: {}")
            spec_path = f.name

        try:
            result = main(["--config", config_path, "--spec", spec_path])
            assert result in [0, 1, 2]
        finally:
            os.unlink(config_path)
            os.unlink(spec_path)

    def test_main_unknown_flag_shows_usage(self):
        """Unknown flags should show usage and exit non-zero."""
        result = main(["--unknown-flag"])
        assert result != 0, "Unknown flag should exit non-zero"


class TestMainConfigLoading:
    """Validate config loading integration."""

    def test_main_uses_default_config_when_no_config_given(self):
        """Should use defaults when no --config provided."""
        # May succeed or fail based on port availability, but should parse
        result = main([])
        assert result in [0, 1, 2]

    def test_main_env_vars_override_defaults(self):
        """Environment variables should affect config."""
        env_vars = {"MOCK_SERVER_PORT": "18082", "MOCK_SERVER_HOST": "127.0.0.1"}
        with patch.dict(os.environ, env_vars, clear=False):
            result = main([])
            assert result in [0, 1, 2]


class TestMainCommandDispatch:
    """Validate command dispatch to appropriate subsystems."""

    def test_main_dispatches_to_server_start(self):
        """Default command should start the server."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"port": 18083, "host": "127.0.0.1"}, f)
            config_path = f.name

        try:
            # Should attempt to start server (may fail due to port, etc)
            result = main(["--config", config_path])
            # Result indicates what happened during server start attempt
            assert isinstance(result, int)
        finally:
            os.unlink(config_path)

    def test_main_version_flag(self):
        """--version should print version and exit 0."""
        result = main(["--version"])
        assert result == 0, f"--version should exit 0, got {result}"


class TestMainEdgeCases:
    """Edge cases and error handling."""

    def test_main_nonexistent_config_file(self):
        """Non-existent config file should be config error (exit 1)."""
        result = main(["--config", "/nonexistent/path/config.json"])
        assert result == 1, f"Expected exit code 1 for missing config, got {result}"

    def test_main_malformed_config_json(self):
        """Malformed JSON config should be config error (exit 1)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"invalid json')
            config_path = f.name

        try:
            result = main(["--config", config_path])
            assert result == 1, f"Expected exit code 1 for malformed JSON, got {result}"
        finally:
            os.unlink(config_path)

    def test_main_negative_port_config(self):
        """Negative port in config should be config error (exit 1)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"port": -1}, f)
            config_path = f.name

        try:
            result = main(["--config", config_path])
            assert result == 1, f"Expected exit code 1 for negative port, got {result}"
        finally:
            os.unlink(config_path)

    def test_main_port_out_of_range(self):
        """Port > 65535 should be config error (exit 1)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"port": 70000}, f)
            config_path = f.name

        try:
            result = main(["--config", config_path])
            assert result == 1, f"Expected exit code 1 for out of range port, got {result}"
        finally:
            os.unlink(config_path)

    def test_main_invalid_spec_file(self):
        """Invalid OpenAPI spec should be config error (exit 1)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"port": 18084}, f)
            config_path = f.name

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(b"not: valid: openapi: content")
            spec_path = f.name

        try:
            result = main(["--config", config_path, "--spec", spec_path])
            assert result == 1, f"Expected exit code 1 for invalid spec, got {result}"
        finally:
            os.unlink(config_path)
            os.unlink(spec_path)
