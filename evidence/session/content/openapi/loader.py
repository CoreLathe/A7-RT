"""Load OpenAPI spec and generate mock rules from paths and response schemas."""

import json
from pathlib import Path
from typing import Any

import importlib.util
import sys

# Load types from apitypes.core (guard against re-loading)
if "types_core" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("types_core", Path(__file__).parent.parent / "apitypes" / "core.py")
    _types_core = importlib.util.module_from_spec(_spec)
    sys.modules["types_core"] = _types_core
    _spec.loader.exec_module(_types_core)
MatchRule = sys.modules["types_core"].MatchRule
Template = sys.modules["types_core"].Template


SUPPORTED_VERSIONS: list[str] = ["3.0.0", "3.1.0"]


class OpenAPIError(Exception):
    """Exception raised for unsupported OpenAPI versions or malformed specs."""
    pass


def load_from_spec(spec_path: str) -> list[tuple[MatchRule, Template]]:
    """
    Load an OpenAPI specification and generate mock rules.
    
    Args:
        spec_path: Path to the OpenAPI JSON spec file
        
    Returns:
        List of tuples (MatchRule, Template) for each path/operation combination
        
    Raises:
        OpenAPIError: For unsupported versions, missing fields, or malformed specs
    """
    path = Path(spec_path)
    
    # Check file exists
    if not path.exists():
        raise OpenAPIError(f"Spec file not found: {spec_path}")
    
    # Read and parse JSON
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        spec = json.loads(content)
    except json.JSONDecodeError as e:
        raise OpenAPIError(f"Invalid JSON: {e}")
    except Exception as e:
        raise OpenAPIError(f"Error reading spec file: {e}")
    
    # Validate spec is a dict
    if not isinstance(spec, dict):
        raise OpenAPIError("Spec must be a JSON object")
    
    # Check openapi version field
    openapi_version = spec.get("openapi")
    if openapi_version is None:
        raise OpenAPIError("Missing required field: 'openapi'")
    
    if openapi_version not in SUPPORTED_VERSIONS:
        raise OpenAPIError(f"Unsupported OpenAPI version: {openapi_version}")
    
    # Check paths field
    paths = spec.get("paths")
    if paths is None:
        raise OpenAPIError("Missing required field: 'paths'")
    
    if not isinstance(paths, dict):
        raise OpenAPIError("'paths' must be an object")
    
    results: list[tuple[MatchRule, Template]] = []
    
    # Process each path
    for path_url, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        
        # Convert path params {id} to wildcards *
        path_pattern = _convert_path_to_pattern(path_url)
        
        # Process each operation (get, post, put, delete, etc.)
        for method, operation in path_item.items():
            # Skip non-operation fields like parameters, summary, etc.
            if method.startswith("x-") or method == "parameters":
                continue
            
            if not isinstance(operation, dict):
                continue
            
            # Only process HTTP methods
            if method not in ["get", "post", "put", "delete", "patch", "head", "options", "trace"]:
                continue
            
            # Create MatchRule
            match_rule = MatchRule(
                path_pattern=path_pattern,
                method=method.upper()
            )
            
            # Create Template from response schema
            template = _create_template_from_operation(operation)
            
            results.append((match_rule, template))
    
    return results


def _convert_path_to_pattern(path_url: str) -> str:
    """
    Convert an OpenAPI path to a match pattern.
    
    Replaces path parameters like {id} with wildcards *.
    
    Args:
        path_url: The OpenAPI path (e.g., /users/{id})
        
    Returns:
        Pattern string with wildcards (e.g., /users/*)
    """
    import re
    # Replace {param} with *
    pattern = re.sub(r"\{[^}]+\}", "*", path_url)
    return pattern


def _create_template_from_operation(operation: dict[str, Any]) -> Template:
    """
    Create a Template from an OpenAPI operation.
    
    Args:
        operation: The OpenAPI operation object
        
    Returns:
        A Template with example content based on responses
    """
    responses = operation.get("responses", {})
    
    # Find a successful response (200-299 status codes preferred)
    success_response = None
    content_type = "application/json"
    example_content = "{}"
    
    # Look for 200 first, then any 2xx, then first available
    if "200" in responses:
        success_response = responses["200"]
    elif "201" in responses:
        success_response = responses["201"]
    elif "204" in responses:
        success_response = responses["204"]
        example_content = ""
    else:
        # Try to find any 2xx response or just the first one
        for status_code, response in responses.items():
            if isinstance(status_code, str) and status_code.isdigit():
                code = int(status_code)
                if 200 <= code < 300:
                    success_response = response
                    break
        
        if success_response is None and responses:
            # Use first available response
            first_key = next(iter(responses))
            success_response = responses[first_key]
    
    if success_response and isinstance(success_response, dict):
        # Extract content type and example from response
        content = success_response.get("content", {})
        
        if content:
            # Prefer application/json, then first available
            if "application/json" in content:
                media_type = content["application/json"]
                content_type = "application/json"
            else:
                first_content_type = next(iter(content))
                media_type = content[first_content_type]
                content_type = first_content_type
            
            # Get example from media type
            if isinstance(media_type, dict):
                if "example" in media_type:
                    example = media_type["example"]
                    example_content = json.dumps(example)
                elif "examples" in media_type:
                    examples = media_type["examples"]
                    if isinstance(examples, dict) and examples:
                        first_example = next(iter(examples.values()))
                        if isinstance(first_example, dict) and "value" in first_example:
                            example_content = json.dumps(first_example["value"])
                        else:
                            example_content = json.dumps(first_example)
                elif "schema" in media_type:
                    # Generate example from schema
                    schema = media_type["schema"]
                    example_content = _generate_example_from_schema(schema)
    
    return Template(content=example_content, content_type=content_type)


def _generate_example_from_schema(schema: dict[str, Any]) -> str:
    """
    Generate a JSON example from a schema.
    
    Args:
        schema: JSON schema object
        
    Returns:
        JSON string example
    """
    example = _generate_value_from_schema(schema)
    return json.dumps(example)


def _generate_value_from_schema(schema: dict[str, Any]) -> Any:
    """
    Generate a value from a schema definition.
    
    Args:
        schema: JSON schema object
        
    Returns:
        Generated value matching the schema
    """
    if not isinstance(schema, dict):
        return None
    
    schema_type = schema.get("type")
    
    if schema_type == "object":
        result = {}
        properties = schema.get("properties", {})
        for prop_name, prop_schema in properties.items():
            result[prop_name] = _generate_value_from_schema(prop_schema)
        return result
    
    elif schema_type == "array":
        items = schema.get("items", {})
        return [_generate_value_from_schema(items)]
    
    elif schema_type == "string":
        enum_values = schema.get("enum")
        if enum_values and isinstance(enum_values, list):
            return enum_values[0]
        
        format_type = schema.get("format", "")
        if format_type == "date-time":
            return "2024-01-01T00:00:00Z"
        elif format_type == "date":
            return "2024-01-01"
        elif format_type == "email":
            return "user@example.com"
        elif format_type == "uri":
            return "https://example.com"
        
        # Use example if provided
        if "example" in schema:
            return schema["example"]
        if "default" in schema:
            return schema["default"]
        
        return "string"
    
    elif schema_type == "integer":
        if "example" in schema:
            return schema["example"]
        if "default" in schema:
            return schema["default"]
        return 0
    
    elif schema_type == "number":
        if "example" in schema:
            return schema["example"]
        if "default" in schema:
            return schema["default"]
        return 0.0
    
    elif schema_type == "boolean":
        if "example" in schema:
            return schema["example"]
        if "default" in schema:
            return schema["default"]
        return False
    
    elif "enum" in schema:
        enum_values = schema["enum"]
        if isinstance(enum_values, list) and enum_values:
            return enum_values[0]
        return None
    
    elif "oneOf" in schema or "anyOf" in schema:
        variants = schema.get("oneOf") or schema.get("anyOf")
        if isinstance(variants, list) and variants:
            return _generate_value_from_schema(variants[0])
        return None
    
    elif "allOf" in schema:
        all_of = schema.get("allOf", [])
        result = {}
        for sub_schema in all_of:
            if isinstance(sub_schema, dict):
                sub_value = _generate_value_from_schema(sub_schema)
                if isinstance(sub_value, dict):
                    result.update(sub_value)
        return result
    
    elif "$ref" in schema:
        # Reference - we can't resolve it without full spec, return empty object
        return {}
    
    # Default: empty object
    return {}
