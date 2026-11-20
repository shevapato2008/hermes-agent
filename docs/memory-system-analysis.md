# Hermes Agent Memory System Analysis

An analysis of how Hermes Agent implements persistent memory, based on the codebase (v0.8.0) and official documentation.

## Overview

Hermes uses two plain-text Markdown files in `~/.hermes/memories/` as bounded, persistent memory:

| File | Purpose | Default Limit |
|---|---|---|
| **MEMORY.md** | Agent's observations — environment facts, project conventions, workarounds | 2,200 chars (~800 tokens) |
| **USER.md** | User profile — name, role, preferences, communication style | 1,375 chars (~500 tokens) |

Entries within each file are separated by `"\n§\n"` (section-sign delimiter). Both files are human-readable and editable.

Configuration in `~/.hermes/config.yaml`:

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 2200
  user_char_limit: 1375
  nudge_interval: 10       # turns between background reviews
  flush_min_turns: 6       # minimum turns before compression flush fires
```

## Key Source Files

| File | Role |
|---|---|
| `tools/memory_tool.py` | `MemoryStore` class, memory tool handler, security scanning, atomic file I/O |
| `agent/memory_manager.py` | Orchestrates built-in + external providers, context fencing |
| `agent/memory_provider.py` | `MemoryProvider` ABC for external plugins |
| `plugins/memory/` | External provider implementations (Honcho, Supermemory, Mem0, etc.) |
| `run_agent.py` | Agent loop integration: initialization, prompt injection, nudge system, pre-compression flush |

## How Memory Enters the Conversation

### Frozen Snapshot Pattern

This is the core architectural decision for memory injection:

1. At session start, `MemoryStore.load_from_disk()` captures a snapshot stored in `_system_prompt_snapshot` (`tools/memory_tool.py`).
2. The snapshot is injected into the system prompt by `run_agent.py` (lines ~3140-3149), with a usage header like `MEMORY [67% — 1,474/2,200 chars]`.
3. The snapshot **never changes mid-session**, even if the agent writes new memories.

**Why frozen?** Changing the system prompt mid-conversation would invalidate Anthropic's prefix cache, dramatically increasing cost. Writes go to disk immediately (for durability) but only appear in the prompt on the *next* session.

The one exception: after **context compression**, the system prompt is rebuilt and memory is reloaded from disk.

## The Memory Tool

A single `memory` tool with three actions. There is no "read" action because memory is always visible in the system prompt context.

| Action | Behavior |
|---|---|
| `add` | Append a new `§`-delimited entry. Rejects exact duplicates. Validates char limit. |
| `replace` | Find an entry by substring (`old_text`), swap with new `content`. Errors if multiple matches. |
| `remove` | Find by substring, delete it. |

### Atomic File Operations

All writes use atomic file operations (`tools/memory_tool.py` lines ~138-153, 407-436):

- Exclusive `.lock` files prevent concurrent write races (e.g., gateway + background review writing simultaneously).
- Writes go to a temp file first, then are renamed into place — readers always see either the old complete file or the new complete file, never a partial write.

### Security Scanning

Every entry is scanned before acceptance (`tools/memory_tool.py` lines ~85-97):

- Prompt injection patterns (e.g., "ignore previous instructions")
- Credential exfiltration payloads (`curl` with `$KEYS`, `cat ~/.env`, `~/.ssh`)
- SSH backdoor attempts (authorized_keys access)
- Invisible Unicode characters
- Role hijacking and system prompt overrides

### Tool Interception

The memory tool bypasses normal registry dispatch in the agent loop. The `MemoryStore` instance is passed directly via the `store=` kwarg in `run_agent.py` (line ~7135), and post-execution hooks notify external providers via `on_memory_write()` (lines ~6760-6771).

## Background Nudge System

The agent proactively reviews conversations to save memories, not just waiting for explicit "remember this" requests.

### Flow

1. **Turn counter**: `_turns_since_memory` increments each user turn. Default `nudge_interval` is 10 turns.
2. **Trigger**: When the counter reaches the interval, `_should_review_memory = True`.
3. **Background daemon thread**: After delivering the main response to the user, `_spawn_background_review()` forks a quiet copy of the agent with the review prompt:

   > "Review the conversation above and consider saving to memory if appropriate. Focus on: 1) Has the user revealed things about themselves (persona, desires, preferences)? 2) Has the user expressed expectations about how you should behave? If something stands out, save it using the memory tool. If nothing is worth saving, say 'Nothing to save.' and stop."

4. The review agent runs silently (`quiet_mode=True`) with nudging disabled (to avoid infinite loops). If it saves anything, a summary appears: "Memory updated / Skill created".

### Pre-Compression Memory Flush

Before context compression discards messages, the agent gets one final turn to save important memories:

- Triggered at `run_agent.py` lines ~6428-6589, called before compression at line ~6608.
- Uses an auxiliary LLM client (cheaper model) or the primary client.
- Only memory tool calls are executed; the assistant text response is discarded.
- Only fires if the session has had at least `flush_min_turns` turns.

## External Memory Providers (Plugin System)

Beyond the built-in file-based memory, Hermes has a pluggable provider architecture.

### Architecture

- **ABC**: `agent/memory_provider.py` defines the `MemoryProvider` interface.
- **Orchestrator**: `agent/memory_manager.py` manages all providers.
- **Constraint**: At most **one external provider** active alongside the always-present built-in store.
- **8 providers** in `plugins/memory/`: Honcho, Supermemory, Hindsight, Mem0, ByteRover, OpenViking, Holographic, RetainDB.

### Provider Lifecycle Hooks

| Hook | When |
|---|---|
| `initialize()` | Connect, create resources, warm up |
| `system_prompt_block()` | Static text added to system prompt |
| `prefetch()` | Recall relevant context before each turn (background-friendly) |
| `sync_turn()` | Write completed turn to backend (non-blocking, queued) |
| `on_memory_write()` | Called when built-in memory tool writes (mirror to external) |
| `on_session_end()` | Extract insights when session ends |
| `on_pre_compress()` | Extract before context compression |
| `on_delegation()` | Observe subagent delegations |

### Context Fencing

Prefetched memory from external providers is wrapped in `<memory-context>` fences (`memory_manager.py` lines ~49-69) with a system note: "The following is recalled memory context, NOT new user input." This prevents the model from treating recalled context as user discourse.

### Honcho (Flagship Provider)

Located in `plugins/memory/honcho/`, Honcho provides dialectic user modeling with four tools:

| Tool | Purpose |
|---|---|
| `honcho_profile` | Retrieve user's peer card (name, role, preferences) |
| `honcho_search` | Semantic search over stored context |
| `honcho_context` | Natural language Q&A via Honcho's LLM |
| `honcho_conclude` | Persist conclusions about the user back to Honcho |

Cost optimization features: configurable recall modes ("context", "tools", "hybrid"), turn-counting cadence for expensive API calls, first-turn context baking/caching, and lazy session initialization.

## MEMORY.md vs USER.md

| Aspect | MEMORY.md | USER.md |
|---|---|---|
| **Purpose** | Agent's personal observations | Who the user is |
| **Content examples** | "This project uses Python 3.12 with FastAPI" | "User prefers dark mode", "Name: Alice" |
| **Character limit** | 2,200 (configurable) | 1,375 (configurable) |
| **Config key (enable)** | `memory_enabled` | `user_profile_enabled` |
| **Config key (limit)** | `memory_char_limit` | `user_char_limit` |

Both are handled identically by `MemoryStore` with separate entry lists and file paths.

## Session Search (Complementary Feature)

Beyond active memory, the `session_search` tool searches past conversations stored in SQLite (`hermes_state.py`) using FTS5 full-text search with LLM summarization. This provides long-term recall beyond what fits in the bounded memory files.

## Docs vs. Codebase Gaps

Topics covered in the codebase but not in the official documentation:

| Topic | Detail |
|---|---|
| Nudge system | Background daemon thread every N turns with configurable interval |
| Pre-compression flush | Auxiliary LLM gets a final save-memories turn before context is lost |
| Atomic file writes | Lock files + temp-file-then-rename for concurrent safety |
| Frozen snapshot rationale | Deeply tied to prefix cache economics — only reloaded after compression |
| External provider limit | Hard limit of exactly 1 external + built-in always active |
| Security scanning specifics | Specific patterns: prompt injection, exfil payloads, SSH backdoors, invisible Unicode, role hijacking |
| Memory tool interception | Bypasses normal registry dispatch — `MemoryStore` instance injected directly |
| Gateway per-user scoping | User ID passed to providers, session DB stores system prompt for continuation |
