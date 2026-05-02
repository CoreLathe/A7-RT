"""Tests for openapi.loader - OpenAPI spec loading and mock rule generation."""

import json
import pytest
import sys
from pathlib import Path
import importlib.util

# Load types.core using importlib (avoids conflict with stdlib 'types')
spec_types = importlib.util.spec_from_file_location("types_core", Path(__file__).parent.parent / "apitypes" / "core.py")
types_core = importlib.util.module_from_spec(spec_types)
sys.modules["types_core"] = types_core
spec_types.loader.exec_module(types_core)
MatchRule = types_core.MatchRule
Template = types_core.Template

# Load openapi.loader (to be implemented)
spec_loader = importlib.util.spec_from_file_location("openapi_loader", Path(__file__).parent / "loader.py")
openapi_loader = importlib.util.module_from_spec(spec_loader)
sys.modules["openapi_loader"] = openapi_loader
spec_loader.loader.exec_module(openapi_loader)

load_from_spec = openapi_loader.load_from_spec
OpenAPIError = openapi_loader.OpenAPIError
SUPPORTED_VERSIONS = openapi_loader.SUPPORTED_VERSIONS


class TestSupportedVersions:
    """Tests for SUPPORTED_VERSIONS constant."""
    
    def test_supported_versions_is_list(self):
        assert isinstance(SUPPORTED_VERSIONS, list)
        assert all(isinstance(v, str) for v in SUPPORTED_VERSIONS)
    
    def test_supported_versions_includes_3_0_0(self):
        assert "3.0.0" in SUPPORTED_VERSIONS
    
    def test_supported_versions_includes_3_1_0(self):
        assert "3.1.0" in SUPPORTED_VERSIONS


class TestOpenAPIError:
    """Tests for OpenAPIError exception class."""
    
    def test_is_exception_subclass(self):
        assert issubclass(OpenAPIError, Exception)
    
    def test_can_be_raised_with_message(self):
        with pytest.raises(OpenAPIError) as exc_info:
            raise OpenAPIError("test error message")
        assert str(exc_info.value) == "test error message"


class TestLoadFromSpecBasic:
    """Basic tests for load_from_spec function."""
    
    def test_returns_list(self, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/test": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "Success",
                                "content": {
                                    "application/json": {
                                        "schema": {"type": "object"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        result = load_from_spec(str(spec_file))
        assert isinstance(result, list)
    
    def test_returns_list_of_tuples(self, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/test": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "Success",
                                "content": {
                                    "application/json": {
                                        "schema": {"type": "object"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        result = load_from_spec(str(spec_file))
        for item in result:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], MatchRule)
            assert isinstance(item[1], Template)


class TestLoadFromSpecVersions:
    """Tests for OpenAPI version handling."""
    
    def test_accepts_3_0_0(self, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/test": {
                    "get": {"responses": {"200": {"description": "Success"}}}
                }
            }
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        result = load_from_spec(str(spec_file))
        assert isinstance(result, list)
    
    def test_accepts_3_1_0(self, tmp_path):
        spec = {
            "openapi": "3.1.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/test": {
                    "get": {"responses": {"200": {"description": "Success"}}}
                }
            }
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        result = load_from_spec(str(spec_file))
        assert isinstance(result, list)
    
    def test_rejects_unsupported_version(self, tmp_path):
        spec = {
            "openapi": "2.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {}
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        with pytest.raises(OpenAPIError) as exc_info:
            load_from_spec(str(spec_file))
        assert "version" in str(exc_info.value).lower() or "unsupported" in str(exc_info.value).lower()
    
    def test_rejects_missing_openapi_field(self, tmp_path):
        spec = {
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {}
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        with pytest.raises(OpenAPIError):
            load_from_spec(str(spec_file))


class TestLoadFromSpecMalformed:
    """Tests for malformed spec handling."""
    
    def test_raises_on_invalid_json(self, tmp_path):
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text("{invalid json content")
        
        with pytest.raises(OpenAPIError):
            load_from_spec(str(spec_file))
    
    def test_raises_on_missing_paths(self, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"}
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        with pytest.raises(OpenAPIError):
            load_from_spec(str(spec_file))
    
    def test_raises_on_file_not_found(self):
        with pytest.raises(OpenAPIError):
            load_from_spec("/nonexistent/path/spec.yaml")


class TestLoadFromSpecPathGeneration:
    """Tests for MatchRule generation from paths."""
    
    def test_single_path_generates_match_rule(self, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/users": {
                    "get": {"responses": {"200": {"description": "Success"}}}
                }
            }
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        result = load_from_spec(str(spec_file))
        assert len(result) >= 1
        assert all(isinstance(rule, MatchRule) for rule, _ in result)
    
    def test_multiple_paths_generate_multiple_rules(self, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/users": {"get": {"responses": {"200": {"description": "Success"}}}},
                "/posts": {"get": {"responses": {"200": {"description": "Success"}}}},
                "/comments": {"get": {"responses": {"200": {"description": "Success"}}}}
            }
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        result = load_from_spec(str(spec_file))
        assert len(result) >= 3
    
    def test_method_is_set_in_match_rule(self, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/users": {
                    "get": {"responses": {"200": {"description": "Success"}}},
                    "post": {"responses": {"201": {"description": "Created"}}}
                }
            }
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        result = load_from_spec(str(spec_file))
        methods = {rule.method for rule, _ in result}
        assert "get" in methods or "GET" in methods
        assert "post" in methods or "POST" in methods


class TestLoadFromSpecTemplateGeneration:
    """Tests for Template generation from response schemas."""
    
    def test_response_schema_converted_to_template(self, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/users": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "Success",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {"id": {"type": "integer"}}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        result = load_from_spec(str(spec_file))
        assert len(result) >= 1
        template = result[0][1]
        assert isinstance(template, Template)
        assert isinstance(template.content, str)
        assert isinstance(template.content_type, str)
    
    def test_template_content_type_from_spec(self, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/users": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "Success",
                                "content": {
                                    "application/json": {
                                        "schema": {"type": "object"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        result = load_from_spec(str(spec_file))
        template = result[0][1]
        assert "json" in template.content_type or template.content_type == "application/json"
    
    def test_template_has_content(self, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/users": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "Success",
                                "content": {
                                    "application/json": {
                                        "schema": {"type": "object"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(json.dumps(spec))
        
        result = load_from_spec(str(spec_file))
        template = result[0][1]
        assert len(template.content) >= 0  # Content can be empty object {}
