"""Configuration validation, defaults, and environment variable binding."""

import json
from dataclasses import dataclass, field, fields
from typing import Any


@dataclass
class Config:
    """Configuration dataclass with all fields having default values."""
    port: int = 8080
    host: str = "localhost"
    log_level: str = "info"
    ttl_default: int = 300
    max_sessions: int = 100


DEFAULT_CONFIG = Config()


def _validate_port(port: int) -> None:
    """Validate port number is in valid range 1-65535."""
    if not isinstance(port, int):
        raise ValueError(f"Port must be an integer, got {type(port).__name__}")
    if port < 1 or port > 65535:
        raise ValueError(f"Port must be between 1 and 65535, got {port}")


def _parse_env_value(field_name: str, value: str) -> Any:
    """Parse environment variable value to appropriate type."""
    if field_name == "port":
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"Port must be an integer, got '{value}'")
    elif field_name == "ttl_default":
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"ttl_default must be an integer, got '{value}'")
    elif field_name == "max_sessions":
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"max_sessions must be an integer, got '{value}'")
    else:
        return value


def load_config(env: dict[str, str], file_path: str | None = None) -> Config:
    """
    Load configuration with priority: env vars > file values > defaults.
    
    Args:
        env: Dictionary of environment variables (keys: PORT, HOST, etc.)
        file_path: Path to JSON config file, or None to skip file loading
        
    Returns:
        Config instance with resolved values
        
    Raises:
        ValueError: On invalid port, malformed JSON, or missing file
    """
    # Start with defaults
    config_values = {
        "port": DEFAULT_CONFIG.port,
        "host": DEFAULT_CONFIG.host,
        "log_level": DEFAULT_CONFIG.log_level,
        "ttl_default": DEFAULT_CONFIG.ttl_default,
        "max_sessions": DEFAULT_CONFIG.max_sessions,
    }
    
    # Load file values if provided
    if file_path is not None:
        try:
            with open(file_path, "r") as f:
                file_data = json.load(f)
        except FileNotFoundError:
            raise ValueError(f"Config file not found: {file_path}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Malformed JSON in config file: {e}")
        
        # Update with file values (keys match field names)
        for key in config_values:
            if key in file_data:
                config_values[key] = file_data[key]
    
    # Apply environment variable overrides (uppercase keys mapped to lowercase fields)
    env_mapping = {
        "PORT": "port",
        "HOST": "host",
        "LOG_LEVEL": "log_level",
        "TTL_DEFAULT": "ttl_default",
        "MAX_SESSIONS": "max_sessions",
    }
    
    for env_key, field_name in env_mapping.items():
        if env_key in env:
            parsed_value = _parse_env_value(field_name, env[env_key])
            config_values[field_name] = parsed_value
    
    # Validate port
    _validate_port(config_values["port"])
    
    # Create and return config
    return Config(
        port=config_values["port"],
        host=config_values["host"],
        log_level=config_values["log_level"],
        ttl_default=config_values["ttl_default"],
        max_sessions=config_values["max_sessions"],
    )
