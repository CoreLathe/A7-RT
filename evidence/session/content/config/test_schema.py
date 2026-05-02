"""Tests for config.schema - Configuration validation and environment binding."""
import pytest
import tempfile
import os


class TestConfigClass:
    """Tests for the Config dataclass."""
    
    def test_config_has_required_fields(self):
        """Config must have: port, host, log_level, ttl_default, max_sessions."""
        from config.schema import Config
        
        # Check annotations exist
        assert hasattr(Config, '__annotations__')
        annotations = Config.__annotations__
        assert 'port' in annotations
        assert 'host' in annotations
        assert 'log_level' in annotations
        assert 'ttl_default' in annotations
        assert 'max_sessions' in annotations
        
        # Check types
        assert annotations['port'] == int
        assert annotations['host'] == str
        assert annotations['log_level'] == str
        assert annotations['ttl_default'] == int
        assert annotations['max_sessions'] == int
    
    def test_config_defaults_exist(self):
        """All fields must have default values (guarantee)."""
        from config.schema import Config
        
        # Can create without arguments
        config = Config()
        
        assert isinstance(config.port, int)
        assert isinstance(config.host, str)
        assert isinstance(config.log_level, str)
        assert isinstance(config.ttl_default, int)
        assert isinstance(config.max_sessions, int)


class TestDefaultConfig:
    """Tests for DEFAULT_CONFIG constant."""
    
    def test_default_config_exists(self):
        """DEFAULT_CONFIG must exist and be a Config instance."""
        from config.schema import DEFAULT_CONFIG, Config
        
        assert isinstance(DEFAULT_CONFIG, Config)
    
    def test_default_config_has_reasonable_values(self):
        """DEFAULT_CONFIG should have sensible default values."""
        from config.schema import DEFAULT_CONFIG
        
        assert DEFAULT_CONFIG.port > 0
        assert DEFAULT_CONFIG.port <= 65535
        assert DEFAULT_CONFIG.host
        assert DEFAULT_CONFIG.log_level
        assert DEFAULT_CONFIG.ttl_default > 0
        assert DEFAULT_CONFIG.max_sessions > 0


class TestLoadConfig:
    """Tests for load_config function."""
    
    def test_load_config_returns_config(self):
        """load_config must return a Config instance."""
        from config.schema import load_config, Config
        
        config = load_config(env={})
        assert isinstance(config, Config)
    
    def test_load_config_uses_defaults_with_empty_input(self):
        """With no env and no file, should return defaults."""
        from config.schema import load_config, DEFAULT_CONFIG
        
        config = load_config(env={})
        assert config == DEFAULT_CONFIG
    
    def test_load_config_env_overrides_port(self):
        """Environment variable should override default port."""
        from config.schema import load_config
        
        config = load_config(env={"PORT": "9090"})
        assert config.port == 9090
    
    def test_load_config_env_overrides_host(self):
        """Environment variable should override default host."""
        from config.schema import load_config
        
        config = load_config(env={"HOST": "0.0.0.0"})
        assert config.host == "0.0.0.0"
    
    def test_load_config_env_overrides_log_level(self):
        """Environment variable should override default log_level."""
        from config.schema import load_config
        
        config = load_config(env={"LOG_LEVEL": "debug"})
        assert config.log_level == "debug"
    
    def test_load_config_env_overrides_ttl(self):
        """Environment variable should override default ttl_default."""
        from config.schema import load_config
        
        config = load_config(env={"TTL_DEFAULT": "600"})
        assert config.ttl_default == 600
    
    def test_load_config_env_overrides_max_sessions(self):
        """Environment variable should override default max_sessions."""
        from config.schema import load_config
        
        config = load_config(env={"MAX_SESSIONS": "500"})
        assert config.max_sessions == 500
    
    def test_load_config_invalid_port_raises_valueerror(self):
        """Invalid port should raise ValueError."""
        from config.schema import load_config
        
        with pytest.raises(ValueError):
            load_config(env={"PORT": "invalid"})
        
        with pytest.raises(ValueError):
            load_config(env={"PORT": "-1"})
        
        with pytest.raises(ValueError):
            load_config(env={"PORT": "70000"})  # > 65535
    
    def test_load_config_from_file(self, tmp_path):
        """load_config should read from file_path."""
        from config.schema import load_config
        
        config_file = tmp_path / "config.json"
        config_file.write_text('{"port": 8080, "host": "127.0.0.1"}')
        
        config = load_config(env={}, file_path=str(config_file))
        assert config.port == 8080
        assert config.host == "127.0.0.1"
    
    def test_load_config_malformed_file_raises_valueerror(self, tmp_path):
        """Malformed config file should raise ValueError."""
        from config.schema import load_config
        
        config_file = tmp_path / "config.json"
        config_file.write_text('not valid json')
        
        with pytest.raises(ValueError):
            load_config(env={}, file_path=str(config_file))
    
    def test_load_config_env_overrides_file(self, tmp_path):
        """Environment variables override file values (guarantee)."""
        from config.schema import load_config
        
        config_file = tmp_path / "config.json"
        config_file.write_text('{"port": 8080, "host": "127.0.0.1"}')
        
        # Env should override file
        config = load_config(env={"PORT": "9090"}, file_path=str(config_file))
        assert config.port == 9090  # from env, not file
        assert config.host == "127.0.0.1"  # from file
    
    def test_load_config_none_file_path(self):
        """file_path=None should work and use defaults/env only."""
        from config.schema import load_config
        
        config = load_config(env={}, file_path=None)
        assert config is not None
    
    def test_load_config_missing_file_raises_valueerror(self):
        """Non-existent file should raise ValueError."""
        from config.schema import load_config
        
        with pytest.raises(ValueError):
            load_config(env={}, file_path="/nonexistent/path/config.json")
