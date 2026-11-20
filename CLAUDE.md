# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Hermes Agent is a self-improving AI agent by Nous Research. It features a TUI (prompt_toolkit + Rich), a messaging gateway (Telegram, Discord, Slack, WhatsApp, Signal), a skills system (procedural memory), cron scheduling, and multiple terminal backends (local, Docker, SSH, Modal, Daytona, Singularity). It uses the OpenAI-compatible API format with any LLM provider.

## Build & Development Commands

```bash
# Setup
uv venv venv --python 3.11
source venv/bin/activate
uv pip install -e ".[all,dev]"

# Run tests (unit, ~3000 tests, parallelized with pytest-xdist)
python -m pytest tests/ -q

# Run a single test file
python -m pytest tests/test_model_tools.py -q

# Run a specific test
python -m pytest tests/test_model_tools.py::test_function_name -v

# Run tests by directory
python -m pytest tests/tools/ -q        # Tool tests
python -m pytest tests/gateway/ -q       # Gateway tests
python -m pytest tests/hermes_cli/ -q    # CLI tests

# Integration tests (requires API keys, skipped by default via marker)
python -m pytest tests/integration/ -v -m integration

# E2E tests
python -m pytest tests/e2e/ -v

# Web UI (React + Vite, in web/)
cd web && npm install && npm run dev     # Dev server
cd web && npm run build                  # Production build → hermes_cli/web_dist/

# Run the agent
hermes                  # Interactive CLI
hermes gateway start    # Messaging gateway
```

## Architecture

### Core Loop

`run_agent.py` → `AIAgent` class. Synchronous loop: build system prompt → call LLM (OpenAI format) → if tool_calls, dispatch via `tools/registry.py` → append results → loop back. Messages follow `{"role": "system/user/assistant/tool"}` format.

### File Dependency Chain

```
tools/registry.py  ← no deps, imported by all tool files
       ↑
tools/*.py         ← each calls registry.register() at import time
       ↑
model_tools.py     ← triggers tool discovery by importing tool modules
       ↑
run_agent.py, cli.py, batch_runner.py
```

### Key Modules

- **`run_agent.py`** — `AIAgent` class, conversation loop, session persistence
- **`cli.py`** — `HermesCLI`, prompt_toolkit TUI, slash command dispatch via `process_command()`
- **`model_tools.py`** — Tool orchestration, `_discover_tools()`, `handle_function_call()`
- **`toolsets.py`** — Tool groupings/presets per platform (`_HERMES_CORE_TOOLS`)
- **`hermes_state.py`** — SQLite session DB with FTS5 full-text search
- **`hermes_constants.py`** — Zero-dependency module: `get_hermes_home()`, path helpers, platform detection
- **`agent/prompt_builder.py`** — System prompt assembly (identity, skills, context, memory)
- **`agent/context_compressor.py`** — Auto-summarization near token limits
- **`hermes_cli/commands.py`** — Central `COMMAND_REGISTRY` (all slash commands derive from here)
- **`hermes_cli/config.py`** — `DEFAULT_CONFIG`, `OPTIONAL_ENV_VARS`, config migration
- **`hermes_cli/skin_engine.py`** — Data-driven CLI theming
- **`tools/registry.py`** — Central tool registry (schemas, handlers, dispatch)
- **`tools/approval.py`** — Dangerous command detection + user approval
- **`gateway/run.py`** — `GatewayRunner`, platform lifecycle, message routing

### Tools are Self-Registering

Each tool file calls `registry.register()` at import time. To add a tool: create `tools/your_tool.py`, register it, add the import to `model_tools.py`'s `_modules` list, add to a toolset in `toolsets.py`.

### Slash Command Registry

All commands defined as `CommandDef` objects in `hermes_cli/commands.py`. CLI dispatch, gateway dispatch, Telegram menu, Slack mapping, autocomplete, and help text all derive from this single registry. To add a command: add a `CommandDef`, then add handlers in `cli.py` (CLI) and/or `gateway/run.py` (gateway).

### Skills

Bundled skills in `skills/`, optional in `optional-skills/`. Each skill is a directory with `SKILL.md` (frontmatter + instructions). Skills can declare platform restrictions, toolset dependencies, and required env vars.

### Config

User config at `~/.hermes/config.yaml`, secrets at `~/.hermes/.env`. Two config loaders: `load_cli_config()` in `cli.py` (CLI mode), `load_config()` in `hermes_cli/config.py` (subcommands).

## Critical Rules

### Always use `get_hermes_home()` for paths

Import from `hermes_constants`. Never hardcode `~/.hermes` or `Path.home() / ".hermes"` — this breaks profile support. Use `display_hermes_home()` for user-facing messages.

### Tests must not write to `~/.hermes/`

The `_isolate_hermes_home` autouse fixture in `tests/conftest.py` redirects `HERMES_HOME` to a temp dir. When mocking `Path.home()`, also set `HERMES_HOME` env var.

### Don't break prompt caching

Never alter past context, change toolsets, reload memories, or rebuild system prompts mid-conversation. The only exception is context compression.

### Tool handlers return JSON strings

All tool handlers registered with the registry must return a `str` (typically `json.dumps(...)`).

### Don't hardcode cross-tool references in schemas

Tool descriptions must not mention tools from other toolsets by name — they may be unavailable. Add dynamic references in `get_tool_definitions()` in `model_tools.py`.

### Don't use `simple_term_menu`

Rendering bugs in tmux/iTerm2. Use `curses` (stdlib) instead. See `hermes_cli/tools_config.py`.

### Don't use `\033[K` in spinner/display code

Leaks as `?[K` under prompt_toolkit's `patch_stdout`. Use space-padding instead.

## Commit Convention

[Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <description>`

Types: `fix`, `feat`, `docs`, `test`, `refactor`, `chore`
Scopes: `cli`, `gateway`, `tools`, `skills`, `agent`, `install`, `security`, etc.

## Branch Naming

`fix/description`, `feat/description`, `docs/description`, `test/description`, `refactor/description`
