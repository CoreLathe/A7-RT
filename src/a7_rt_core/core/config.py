"""
A7-RT Configuration System — Hierarchical config with .a7 directory support.

Configuration hierarchy (highest to lowest priority):
  1. CLI arguments / environment variables
  2. Session-local .a7/config.toml (in session directory)
  3. Project-level .a7/config.toml (if session is within a project)
  4. User-level ~/.a7/config.toml
  5. Built-in defaults

Directory structure:
  .a7/
    config.toml          # Main configuration
    models.toml          # Model definitions and provider settings
    key                  # API key file (optional, can use env var)
    projects/            # Project JSON files storage
      my-project.json
      ...
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in Defaults (minimal - everything else from config.toml)
# ---------------------------------------------------------------------------

DEFAULT_MANAGER_MODEL: str = "default"
DEFAULT_SUBAGENT_MODEL: str = "default"
DEFAULT_MANAGER_MAX_TURNS: int = 25
DEFAULT_DRAIN_TURN: int = 20
DEFAULT_PROVISIONAL_DEPTH: int = 3

# Legacy aliases for compatibility (deprecated - define in config.toml instead)
MODEL_REGISTRY: dict[str, dict[str, Any]] = {}
PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class ModelConfig:
    """Configuration for a specific model instance."""

    name: str
    provider: str
    model_id: str
    max_tokens: int = 4096
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "ModelConfig":
        """Create from dictionary configuration."""
        return cls(
            name=name,
            provider=data.get("provider", "openrouter"),
            model_id=data.get("model_id", name),
            max_tokens=data.get("max_tokens", 4096),
            temperature=data.get("temperature"),
            top_p=data.get("top_p"),
            extra_headers=data.get("extra_headers", {}),
            extra_body=data.get("extra_body", {}),
        )


@dataclass
class ProviderConfig:
    """Configuration for an API provider."""

    name: str
    base_url: str
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    key_file: Optional[str] = None
    timeout: float = 60.0
    max_retries: int = 3
    base_delay: float = 1.0
    inter_call_delay_ms: int = 0  # Milliseconds between API calls (0 = no delay)

    def resolve_api_key(self, search_paths: list[Path]) -> Optional[str]:
        """
        Resolve API key using hierarchy:
        1. Explicitly set api_key
        2. Environment variable
        3. Key file in search paths
        """
        # 1. Explicit key
        if self.api_key:
            return self.api_key

        # 2. Environment variable
        if self.api_key_env:
            key = os.environ.get(self.api_key_env, "").strip()
            if key:
                return key

        # 3. Key file in search paths (check provider-specific key file first)
        if self.key_file:
            for path in search_paths:
                key_path = path / self.key_file
                if key_path.exists():
                    return key_path.read_text(encoding="utf-8").strip()

        # 4. Generic .a7/key file
        for path in search_paths:
            key_path = path / ".a7" / "key"
            if key_path.exists():
                return key_path.read_text(encoding="utf-8").strip()

        return None


@dataclass
class A7Config:
    """Complete A7-RT configuration."""

    # Core settings
    manager_model: str = DEFAULT_MANAGER_MODEL
    subagent_model: str = DEFAULT_SUBAGENT_MODEL

    # Session settings
    default_manager_max_turns: int = DEFAULT_MANAGER_MAX_TURNS
    default_drain_turn: int = DEFAULT_DRAIN_TURN
    provisional_depth_limit: int = DEFAULT_PROVISIONAL_DEPTH

    # Provider configurations (all from config.toml)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)

    # Model configurations (all from config.toml)
    models: dict[str, ModelConfig] = field(default_factory=dict)

    # Feature flags
    auto_validate: bool = True
    auto_commit: bool = True
    use_shadowfs: bool = True

    # Paths
    project_root: Optional[Path] = None
    session_path: Optional[Path] = None

    # Raw config for access to extra fields
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def get_model_config(self, model_name: str) -> Optional[ModelConfig]:
        """Get configuration for a model by name."""
        # Check custom configs first
        if model_name in self.models:
            return self.models[model_name]

        # Try to parse as direct model ID (provider/model format)
        if "/" in model_name:
            parts = model_name.split("/", 1)
            if len(parts) == 2:
                provider, model_id = parts
                return ModelConfig(
                    name=model_name,
                    provider=provider,
                    model_id=model_name,
                )

        return None

    def get_provider_config(self, provider_name: str) -> Optional[ProviderConfig]:
        """Get configuration for a provider."""
        if provider_name in self.providers:
            return self.providers[provider_name]

        return None

    def resolve_api_key(self, provider_name: str) -> Optional[str]:
        """Resolve API key for a provider."""
        provider = self.get_provider_config(provider_name)
        if not provider:
            return None

        search_paths: list[Path] = []

        # Add session path
        if self.session_path:
            search_paths.append(self.session_path)

        # Add project root
        if self.project_root:
            search_paths.append(self.project_root)

        # Add user config directory
        search_paths.append(Path.home() / ".a7")

        # Add core directory (where a7-rt-core lives)
        core_dir = Path(__file__).parent
        search_paths.append(core_dir)

        # Add current working directory
        search_paths.append(Path.cwd())

        return provider.resolve_api_key(search_paths)

    def get_effective_model(self, role: str = "manager") -> str:
        """Get the effective model ID for a role."""
        model_name = self.manager_model if role == "manager" else self.subagent_model

        # Handle "default" sentinel - use first available model or error
        if model_name == "default":
            if self.models:
                model_name = next(iter(self.models.keys()))
            else:
                raise ConfigError(
                    f"No model configured for role '{role}'. "
                    "Set manager_model/subagent_model in config.toml"
                )

        config = self.get_model_config(model_name)
        if config:
            return config.model_id

        return model_name  # Return as-is if not found

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors."""
        errors = []

        # Check at least one provider is configured
        if not self.providers:
            errors.append("No providers configured. Add a [providers.X] section to config.toml")

        # Check at least one model is configured
        if not self.models:
            errors.append("No models configured. Add a [models.X] section to config.toml")

        # Check role models exist
        for role in ["manager", "subagent"]:
            model_name = self.manager_model if role == "manager" else self.subagent_model
            if model_name != "default" and model_name not in self.models:
                # Allow direct provider/model format
                if "/" not in model_name:
                    errors.append(f"Role '{role}' references unknown model: {model_name}")

        return errors


class ConfigError(Exception):
    """Configuration error with helpful message."""

    pass


# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------


def _load_toml(path: Path) -> Optional[dict[str, Any]]:
    """Load TOML file if available."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            logger.debug("tomli not installed, TOML support disabled")
            return None

    if not path.exists():
        return None

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        logger.warning(f"Failed to load TOML from {path}: {e}")
        return None


def _load_json(path: Path) -> Optional[dict[str, Any]]:
    """Load JSON file."""
    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load JSON from {path}: {e}")
        return None


def _load_config_file(path: Path) -> Optional[dict[str, Any]]:
    """Load config from path (TOML preferred, JSON fallback)."""
    # Try TOML first
    if path.suffix == ".toml" or path.suffix == "":
        result = _load_toml(path.with_suffix(".toml"))
        if result is not None:
            return result

    # Try JSON
    if path.suffix == ".json" or path.suffix == "":
        result = _load_json(path.with_suffix(".json"))
        if result is not None:
            return result

    return None


def _merge_configs(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two config dictionaries."""
    result = base.copy()

    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_configs(result[key], value)
        else:
            result[key] = value

    return result


def _find_project_root(start_path: Path, marker: str = ".a7") -> Optional[Path]:
    """
    Walk up from start_path looking for a marker directory or file.
    Stops at filesystem root or home directory.
    """
    current = start_path.resolve()
    home = Path.home()

    while current != current.parent:
        # Check for marker
        if (current / marker).exists():
            return current

        # Stop at home directory (optional - can be removed to search further)
        if current == home:
            break

        current = current.parent

    return None


def _parse_models_config(data: dict[str, Any]) -> dict[str, ModelConfig]:
    """Parse models section from config."""
    models: dict[str, ModelConfig] = {}

    for name, model_data in data.items():
        if isinstance(model_data, dict):
            models[name] = ModelConfig.from_dict(name, model_data)

    return models


def _parse_providers_config(data: dict[str, Any]) -> dict[str, ProviderConfig]:
    """Parse providers section from config."""
    providers: dict[str, ProviderConfig] = {}

    for name, provider_data in data.items():
        if isinstance(provider_data, dict):
            providers[name] = ProviderConfig(
                name=name,
                base_url=provider_data.get("base_url", ""),
                api_key=provider_data.get("api_key"),
                api_key_env=provider_data.get("api_key_env"),
                key_file=provider_data.get("key_file"),
                timeout=provider_data.get("timeout", 60.0),
                max_retries=provider_data.get("max_retries", 3),
                base_delay=provider_data.get("base_delay", 1.0),
                inter_call_delay_ms=provider_data.get("inter_call_delay_ms", 0),
            )

    return providers


def load_config(
    session_path: Optional[Path | str] = None,
    project_root: Optional[Path | str] = None,
    cli_overrides: Optional[dict[str, Any]] = None,
) -> A7Config:
    """
    Load configuration from hierarchy.

    Args:
        session_path: Path to session directory (for local .a7/config.toml)
        project_root: Explicit project root (auto-detected if not provided)
        cli_overrides: Dictionary of CLI argument overrides

    Returns:
        Merged A7Config instance
    """
    import os

    raw_config: dict[str, Any] = {}

    # 0. Load from A7_CONFIG_PATH environment variable if set (replaces user config)
    config_path_env = os.environ.get("A7_CONFIG_PATH")
    user_config_dir: Optional[Path] = None
    if config_path_env:
        env_config_path = Path(config_path_env)
        env_config = _load_config_file(env_config_path)
        if env_config:
            logger.debug(f"Loaded config from A7_CONFIG_PATH: {env_config_path}")
            raw_config = _merge_configs(raw_config, env_config)
        # Skip user config when A7_CONFIG_PATH is set - env config acts as user config replacement

    # 1. Load user-level config (~/.a7/config.toml) only if A7_CONFIG_PATH not set
    if not config_path_env:
        user_config_dir = Path.home() / ".a7"
        user_config = _load_config_file(user_config_dir / "config")
        if user_config:
            logger.debug(f"Loaded user config from {user_config_dir}")
            raw_config = _merge_configs(raw_config, user_config)

    # 2. Auto-detect project root if not provided
    resolved_session_path: Optional[Path] = None
    if session_path:
        resolved_session_path = Path(session_path).resolve()

    resolved_project_root: Optional[Path] = None
    if project_root:
        resolved_project_root = Path(project_root).resolve()
    elif resolved_session_path:
        resolved_project_root = _find_project_root(resolved_session_path)

    # 3. Load project-level config (.a7/config.toml in project root)
    if resolved_project_root:
        project_config = _load_config_file(resolved_project_root / ".a7" / "config")
        if project_config:
            logger.debug(f"Loaded project config from {resolved_project_root / '.a7'}")
            raw_config = _merge_configs(raw_config, project_config)

    # 4. Load session-local config (.a7/config.toml in session directory)
    if resolved_session_path:
        session_config = _load_config_file(resolved_session_path / ".a7" / "config")
        if session_config:
            logger.debug(f"Loaded session config from {resolved_session_path / '.a7'}")
            raw_config = _merge_configs(raw_config, session_config)

    # 5. Load models from config.toml [models.X] sections and models.toml files
    models_config: dict[str, ModelConfig] = {}

    # Parse [models.X] from merged config (hierarchy: user -> project -> session)
    if "models" in raw_config:
        models_config.update(_parse_models_config(raw_config["models"]))

    # Also load separate models.toml files (lower priority, don't override)
    for config_dir in [user_config_dir, resolved_project_root, resolved_session_path]:
        if config_dir is None:
            continue
        if config_dir:
            models_data = _load_config_file(Path(config_dir) / ".a7" / "models")
            if models_data:
                # Only add models not already defined in config.toml
                for name, model_data in models_data.items():
                    if name not in models_config:
                        models_config[name] = ModelConfig.from_dict(name, model_data)

    # 6. Apply CLI overrides (highest priority)
    if cli_overrides:
        raw_config = _merge_configs(raw_config, cli_overrides)

    # Build final config
    config = A7Config(
        manager_model=raw_config.get("manager_model", DEFAULT_MANAGER_MODEL),
        subagent_model=raw_config.get("subagent_model", DEFAULT_SUBAGENT_MODEL),
        default_manager_max_turns=raw_config.get(
            "default_manager_max_turns", DEFAULT_MANAGER_MAX_TURNS
        ),
        default_drain_turn=raw_config.get("default_drain_turn", 20),
        provisional_depth_limit=raw_config.get("provisional_depth_limit", 3),
        providers=_parse_providers_config(raw_config.get("providers", {})),
        models=models_config,
        auto_validate=raw_config.get("auto_validate", True),
        auto_commit=raw_config.get("auto_commit", True),
        use_shadowfs=raw_config.get("use_shadowfs", True),
        project_root=resolved_project_root,
        session_path=resolved_session_path,
        _raw=raw_config,
    )

    return config


# ---------------------------------------------------------------------------
# Config Creation / Seeding
# ---------------------------------------------------------------------------


def create_minimal_config(
    manager_model: str = "default", subagent_model: str = "default"
) -> dict[str, Any]:
    """Create minimal configuration dictionary."""
    return {
        "manager_model": manager_model,
        "subagent_model": subagent_model,
        "default_manager_max_turns": DEFAULT_MANAGER_MAX_TURNS,
        "default_drain_turn": DEFAULT_DRAIN_TURN,
        "provisional_depth_limit": DEFAULT_PROVISIONAL_DEPTH,
        "auto_validate": True,
        "auto_commit": True,
        "use_shadowfs": True,
    }


def create_default_config() -> dict[str, Any]:
    """Create default configuration dictionary (deprecated, use create_minimal_config)."""
    return create_minimal_config()


def create_default_models() -> dict[str, Any]:
    """Create default models configuration (deprecated - define in config.toml instead)."""
    return {}


def seed_a7_directory(
    target_path: Path,
    with_key: bool = False,
    key_value: Optional[str] = None,
    manager_model: Optional[str] = None,
    subagent_model: Optional[str] = None,
) -> None:
    """
    Seed a .a7 directory at the target path.

    Creates:
        .a7/config.toml (minimal, user-configured)
        .a7/projects/ (directory)
        .a7/key (if with_key=True)
    """
    a7_dir = target_path / ".a7"
    a7_dir.mkdir(parents=True, exist_ok=True)

    # Create minimal config.toml
    config_path = a7_dir / "config.toml"
    if not config_path.exists():
        config_toml = """\
# A7-RT Configuration
# ===================
# Uncomment and configure ONE provider section below.
# Then uncomment and configure at least ONE model section.
# Finally set manager_model and subagent_model to reference your models.

# Role-to-model mapping (references [models.X] sections below)
# manager_model = "claude-sonnet"
# subagent_model = "claude-haiku"

# ============================================================
# PROVIDER OPTIONS (uncomment ONE)
# ============================================================

# --- OpenRouter (recommended - multiple providers) ---
# [providers.openrouter]
# base_url = "https://openrouter.ai/api/v1"
# api_key = "sk-or-..."  # Or use api_key_env = "OPENROUTER_API_KEY"

# --- Anthropic (direct) ---
# [providers.anthropic]
# base_url = "https://api.anthropic.com/v1"
# api_key = "sk-ant-..."

# --- OpenAI ---
# [providers.openai]
# base_url = "https://api.openai.com/v1"
# api_key = "sk-..."

# --- Ollama (local) ---
# [providers.ollama]
# base_url = "http://localhost:11434/v1"
# api_key = "ollama"  # Required but ignored

# ============================================================
# MODEL OPTIONS (uncomment and link to provider above)
# ============================================================

# [models.claude-sonnet]
# provider = "openrouter"  # Must match [providers.X] above
# model_id = "anthropic/claude-sonnet-4-6"
# max_tokens = 4096

# [models.claude-haiku]
# provider = "openrouter"
# model_id = "anthropic/claude-haiku-4-5"
# max_tokens = 4096

# [models.gpt-4o]
# provider = "openai"
# model_id = "gpt-4o"
# max_tokens = 4096

# [models.llama3]
# provider = "ollama"
# model_id = "llama3.1:latest"
# max_tokens = 4096
"""
        config_path.write_text(config_toml, encoding="utf-8")
        logger.info(f"Created {config_path}")

    # Create projects directory
    projects_dir = a7_dir / "projects"
    projects_dir.mkdir(exist_ok=True)

    # Create key file if requested
    if with_key and key_value:
        key_path = a7_dir / "key"
        key_path.write_text(key_value.strip(), encoding="utf-8")
        key_path.chmod(0o600)  # Restrict permissions
        logger.info(f"Created {key_path}")


# ---------------------------------------------------------------------------
# CLI Helpers
# ---------------------------------------------------------------------------


def add_config_arguments(parser: Any) -> None:
    """
    Add config-related arguments to an argument parser.
    Compatible with both argparse and manual argv parsing.
    """
    # These are the argument definitions for documentation purposes
    _arg_defs = [
        ("--config", "Path to explicit config file"),
        ("--model", "Manager model (overrides config)"),
        ("--subagent-model", "Subagent model (overrides config)"),
        ("--provider", "API provider to use"),
        ("--api-key", "API key (overrides env/file)"),
    ]

    # When using argparse:
    # for arg, help_text in _arg_defs:
    #     parser.add_argument(arg, help=help_text)


def parse_model_arg(arg: str, config: A7Config) -> str:
    """
    Parse a model argument, resolving aliases from config.

    Args:
        arg: Model name or alias (e.g., "claude-sonnet", "anthropic/claude-sonnet-4-6")
        config: Current configuration

    Returns:
        Resolved model ID suitable for API calls
    """
    # Check if it's already a full model ID
    if "/" in arg and arg not in config.models:
        return arg

    # Look up in config models
    model_config = config.get_model_config(arg)
    if model_config:
        return model_config.model_id

    # Return as-is (might be a direct model ID we don't know about)
    return arg


# ---------------------------------------------------------------------------
# Backward Compatibility
# ---------------------------------------------------------------------------


def load_api_key(core_dir: Optional[Path] = None) -> str:
    """
    Backward-compatible API key loading.

    Tries (in order):
    1. OPENROUTER_API_KEY environment variable
    2. core_dir/openrouter-key file
    3. .a7/key in various locations
    """
    # Try environment first
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key

    # Try core_dir
    if core_dir:
        key_file = core_dir / "openrouter-key"
        if key_file.exists():
            return key_file.read_text(encoding="utf-8").strip()

    # Try loading via config system
    config = load_config()
    key = config.resolve_api_key("openrouter")
    if key:
        return key

    return ""


def get_default_models() -> tuple[str, str]:
    """Backward-compatible default model getter."""
    config = load_config()
    return (config.manager_model, config.subagent_model)


# ---------------------------------------------------------------------------
# Module Testing
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # Simple test when run directly
    logging.basicConfig(level=logging.DEBUG)

    print("Testing A7 Config System")
    print("=" * 50)

    # Test config loading
    config = load_config()
    print("\nDefault config loaded:")
    print(f"  Manager model: {config.manager_model}")
    print(f"  Subagent model: {config.subagent_model}")
    print(f"  Max turns: {config.default_manager_max_turns}")

    # Test model resolution
    print("\nModel resolution:")
    for name in ["claude-sonnet-4-6", "claude-opus-4", "kimi-k2.5", "unknown/model"]:
        resolved = config.get_effective_model("manager" if "opus" in name else "subagent")
        print(f"  {name} -> {resolved}")

    # Test API key resolution
    key = config.resolve_api_key("openrouter")
    print(f"\nAPI key resolved: {'Yes (masked)' if key else 'No'}")
    if key:
        print(f"  Key preview: {key[:8]}...{key[-4:]}")

    print("\nConfig system test complete.")
