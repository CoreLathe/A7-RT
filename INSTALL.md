# Installation Guide

## Quick Install

```bash
# Clone the repository
git clone https://github.com/CoreLathe/A7-RT.git
cd A7-RT

# Install
pip install .

# Or for development (editable install)
pip install -e .
```

## Configuration

A7-RT uses a hierarchical configuration system (highest to lowest priority):

1. CLI arguments / environment variables
2. Session-local `.a7/config.toml` (in session directory)
3. Project-level `.a7/config.toml` (if session is within a project)
4. User-level `~/.a7/config.toml`
5. Built-in defaults

### Quick Setup

```bash
# Option 1: Environment variable (quickest)
export OPENROUTER_API_KEY="sk-or-..."

# Option 2: Initialize config directory
a7-rt config --init --with-key="sk-or-..."
```

### Manual Config File

Create `~/.a7/config.toml`:

```toml
# Role-to-model mapping
manager_model = "claude-sonnet"
subagent_model = "claude-haiku"

# Provider configuration
[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"  # Or use: api_key = "sk-or-..."

# Model definitions
[models.claude-sonnet]
provider = "openrouter"
model_id = "anthropic/claude-sonnet-4-6"
max_tokens = 4096

[models.claude-haiku]
provider = "openrouter"
model_id = "anthropic/claude-haiku-4-5"
max_tokens = 4096
```

See the full example in `src/a7_rt_core/data/config.toml.example` for more providers (Anthropic, OpenAI, Moonshot, Ollama, LM Studio).

## Verify Installation

```bash
# Check CLI is available
a7-rt --help

# Test data access
python -c "from a7_rt_core.data import get_roles_dir; print(get_roles_dir())"

# Create a test session
export OPENROUTER_API_KEY="sk-test"
a7-rt init /tmp/test-session
```

## Requirements

- Python 3.10+
- OpenRouter API key (or other configured provider)

See [README.md](README.md) for full documentation.
