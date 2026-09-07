This file provides guidance to AI coding agents working with this repository.

## Current Planning Gate — 2026-09-06

The user has withdrawn the previous governance implementation and spec/tasks.
The active replacement scope is ONLY sections 3 (context compression),
4 (explicit memory and Dream), and 6 (evaluation) of the Feishu technical plan,
document I2gWde9xOoz8n3xKwI6cRsognpb, revision 70.
Read spec/README.md and spec/TASKS.md. On 2026-09-06 the user replaced H01 with
an LLM-invoked memory_save tool and confirmed H02-H05. No input-intent service
or separate memory-classification model is planned. Follow minimal-change reuse.
The user has now explicitly authorized implementation of this revision. Development must
complete N00/N01/N01A evaluation capabilities, long-context and memory-dependent
baseline tasks before changing production context or memory behavior.
Keep work in MyCoder on codex/agent-governance; do not reset main, commit or push.
The older roadmap paragraph below is preserved for provenance but is superseded
by this gate, including its S01-S06 and T00-T17 references and continuation rules.
Retired code/spec artifacts are under .superpowers/sdd/RETIRED-governance-v1-20260906;
they are not active implementation requirements or reusable completed work.

## MyCoder Development Scope

All development for the lifecycle, context governance, security policy,
human approval, evaluation, and settings roadmap must take place in **MyCoder**,
at `E:\Code\Agent\MyCoder` (`origin`: `https://github.com/kytorio/MyCoder.git`).
The original `E:\Code\Agent\nanobot` checkout and the nanobot study workspace are
research references only, not implementation targets. Keep the existing `nanobot/`
package and CLI names unless a separate rename is explicitly requested.

Before implementation, read [spec/README.md](spec/README.md), the mandatory
[development boundary](spec/S00-development-boundary.md), the relevant S01-S06
spec, and its task under [spec/TASKS.md](spec/TASKS.md). These documents define
scope, interfaces, reusable code, task dependencies, and acceptance checks.
Update the affected spec and dependent tasks before changing their boundaries.
As of 2026-09-06, all planned Goal-specific changes are withdrawn by the user.
Preserve existing Goal behavior/data; generic Stop lifecycle work remains in scope.
S03 and T12/T12A/T13/T14 are withdrawn records, not implementation tasks.

The development branch is `codex/agent-governance`, created from MyCoder `main`
at `455533169d5a641300dd63d260b1ff5543c4093c`. Continue on that branch;
do not recreate/reset it, develop directly on `main`, or automatically merge/pull
`upstream/main` as part of this roadmap. The upstream contribution workflow below
is background guidance, not authorization to change the pinned baseline or push.
Preserve the existing uncommitted `webui/package-lock.json` change and do not
include it in unrelated commits. All T00-T17 implementation tasks remain unstarted
at spec import time. Later status updates must reflect actual progress; completion
requires the task's own tests and acceptance checks to have been completed.

## Project Overview

nanobot is a lightweight, open-source AI agent framework written in Python with a React/TypeScript WebUI. It centers around a small agent loop that receives messages from chat channels, invokes an LLM provider, executes tools, and manages session memory.

## Development Commands

```bash
# Python: run single test / lint
pytest tests/test_openai_api.py::test_function -v
ruff check nanobot/

# Strict type checking (matches CI)
uv sync --all-extras --dev
uv run --no-sync python -m scripts.install_channel_dependencies --all-channels
uv run --no-sync basedpyright

# WebUI: dev server (proxies API/WS to gateway :8765), build, test
# Build outputs to ../nanobot/web/dist (bundled into the Python wheel)
cd webui && bun run dev      # or NANOBOT_API_URL=... bun run dev
cd webui && bun run build
cd webui && bun run test

# Gateway
nanobot gateway
```

## High-Level Architecture

### Core Data Flow

Messages flow through an async `MessageBus` (`nanobot/bus/queue.py`) that decouples chat channels from the agent core:

1. **Channels** (`nanobot/channels/`) receive messages from external platforms and publish `InboundMessage` events to the bus.
2. **`AgentLoop`** (`nanobot/agent/loop.py`) consumes inbound messages, builds context, and coordinates the turn.
3. **`AgentRunner`** (`nanobot/agent/runner.py`) handles the actual LLM conversation loop: send messages to the provider, receive tool calls, execute tools, and stream responses.
4. Responses are published as `OutboundMessage` events back to the appropriate channel.

### Key Subsystems

- **Agent Loop** (`nanobot/agent/loop.py`, `runner.py`): The core processing engine. `AgentLoop` manages session keys, hooks, and context building. `AgentRunner` executes the multi-turn LLM conversation with tool execution.
- **LLM Providers** (`nanobot/providers/`): Provider implementations (Anthropic, OpenAI-compatible, OpenAI Responses API, Azure, Bedrock, GitHub Copilot, OpenAI Codex, etc.) built on a common base (`base.py`). Includes image generation (`image_generation.py`) and audio transcription (`transcription.py`). `factory.py` and `registry.py` handle instantiation and model discovery.
- **Channels** (`nanobot/channels/`): Platform integrations (Telegram, Discord, Slack, Feishu, Matrix, WhatsApp, QQ, WeChat, WeCom, DingTalk, Email, MoChat, MS Teams, WebSocket, Mattermost). `manager.py` discovers and coordinates them. Channels are self-contained packages auto-discovered via `pkgutil` scanning.
- **Tools** (`nanobot/agent/tools/`): Agent capabilities exposed to the LLM: filesystem (read/write/edit/list), shell execution (with sandbox backends), web search/fetch, MCP servers, cron, notebook editing, subagent spawning, long-running tasks / sustained goals (`long_task.py`), image generation, and self-modification. Tools are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Memory** (`nanobot/agent/memory.py`): Session history persistence with Dream two-phase memory consolidation. Uses atomic writes with fsync for durability.
- **Session Management** (`nanobot/session/`): Per-session history, context compaction, TTL-based auto-compaction (`manager.py`), and sustained goal state tracking (`goal_state.py`).
- **Config** (`nanobot/config/schema.py`, `loader.py`): Pydantic-based configuration loaded from `~/.nanobot/config.json`. Supports camelCase aliases for JSON compatibility.
- **WebUI** (`webui/`): Vite-based React SPA that talks to the gateway over a WebSocket multiplex protocol. The dev server proxies `/api`, `/webui`, `/auth`, and WebSocket traffic to the gateway.
- **API Server** (`nanobot/api/server.py`): OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) for programmatic access.
- **Command Router** (`nanobot/command/`): Slash command routing and built-in command handlers.
- **Heartbeat** (`nanobot/templates/HEARTBEAT.md`): Periodic task list checked via `cron` jobs (legacy dedicated service removed).
- **Pairing** (`nanobot/pairing/`): DM sender approval store with persistent pairing codes per channel.
- **Skills** (`nanobot/skills/`): Built-in skill definitions (cron, github, image-generation, etc.) loaded into agent context.
- **Security** (`nanobot/security/`): PTH file guard and other security measures activated at CLI entry.

### Entry Points

- **CLI**: `nanobot/cli/commands.py`
- **Python SDK**: `nanobot/nanobot.py`

## Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

## Contribution Flow

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for contribution flow and PR guidelines.

## Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored).
- pytest with `asyncio_mode = "auto"`.

## Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Channel base / new channel template: `nanobot/channels/base.py`
- Tool registry: `nanobot/agent/tools/registry.py`
- WebUI dev proxy config: `webui/vite.config.ts`
- Tests mirror the `nanobot/` package structure.
