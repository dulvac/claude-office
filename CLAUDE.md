# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Claude Office Visualizer transforms Claude Code operations into a real-time pixel art office simulation. A "boss" character (main Claude agent) manages work, spawns "employee" agents (subagents), and orchestrates tasks visually.

**Core design principle:** The backend is the source of truth. The frontend is a "dumb" renderer that displays whatever state it receives via WebSocket.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed system architecture and component reference.

## Commands

```bash
# Root
make install       # Install backend + frontend + hooks deps
make install-all   # install + hooks-install into Claude Code
make dev-tmux      # Run in tmux (recommended) - backend :8000, frontend :3000
make dev-tmux-kill # Kill tmux session
make checkall      # Lint, typecheck, test all components
make simulate      # Run event simulation
make gen-types     # Regenerate frontend types from backend Pydantic models
make clean-db      # Delete SQLite database (backend/visualizer.db)

# Backend (from backend/)
make dev           # uvicorn with hot reload on :8000
make checkall      # fmt → lint → typecheck → test (ruff + pyright + pytest)
uv run pytest tests/test_file.py::test_name  # Single test

# Frontend (from frontend/)
make dev           # next dev --turbo on :3000
make checkall      # fmt → lint → typecheck → build → test (prettier + eslint + tsc + vitest)
bun run test:watch # Vitest in watch mode

# Hooks (from hooks/)
./install.sh       # Install hooks into Claude Code
./uninstall.sh     # Remove hooks

# Docker
make docker-build  # Build image
make docker-up     # Start container (serves at :8000)
make docker-down   # Stop container
```

## Development Workflow

**Preferred:** Use `make dev-tmux` - creates separate windows for backend/frontend.
- Read logs: `tmux capture-pane -t claude-office:backend -p`
- Switch windows: `Ctrl-b n` / `Ctrl-b p`
- Hot reload enabled on both servers

**Debugging:** Hook logs at `~/.claude/claude-office-hooks.log` (enable with `CLAUDE_OFFICE_DEBUG=1`)

## Architecture

**Data flow:** Claude Code → Hooks (HTTP POST) → Backend (FastAPI) → Frontend (WebSocket)

- **Hooks** (`hooks/src/claude_office_hooks/`): Lightweight — only pass along JSON payloads from Claude Code, never read files or do heavy processing. Heavy extraction (JSONL transcripts, token usage) belongs in the backend.
- **Backend** (`backend/app/`): FastAPI + SQLite. Core logic lives in `core/state_machine.py` (event→state transforms, agent lifecycle, desk assignments). Events are routed through `core/event_processor.py` to domain-specific handlers in `core/handlers/`.
- **Frontend** (`frontend/src/`): Next.js + PixiJS + Zustand + XState. All game state in a single Zustand store (`stores/gameStore.ts`). Agent lifecycle (arrival, departure, work) managed by XState machines in `machines/`. Movement uses A* pathfinding (`systems/pathfinding.ts`).

**Type generation:** Frontend TypeScript types in `types/generated.ts` are auto-generated from backend Pydantic models. Run `make gen-types` after changing backend models. A pre-commit hook catches type drift automatically.

**Zustand selector pattern** — use individual selectors to avoid re-render loops:
```typescript
// Good
const isConnected = useGameStore((state) => state.isConnected);
const agents = useGameStore(selectAgents);
// Bad - new object ref every render
const { boss, agents } = useGameStore((state) => ({ boss: state.boss, agents: state.agents }));
```

**Agent IDs** use format `subagent_{tool_use_id}` to correlate start/stop events. Teammate agent IDs use format `teammate_{name}`.

## Agent Teams Support

The visualizer supports Claude Code's experimental Agent Teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`). Teammates appear as employees in the lead's office with inter-agent messaging visualized as conversations.

**Detection flow:**
1. Lead uses `TeamCreate` tool → backend registers team
2. Lead uses `Agent` tool with `team_name` in `tool_input` → hook extracts team context, backend pre-registers teammate
3. Teammate's `SessionStart` → matched to pending registration, routed to lead's office
4. Fallback: `TeammateIdle`/`TaskCompleted` events provide late discovery if early detection missed

**Key files:**
- `backend/app/core/team_registry.py` — Session correlation registry
- `backend/app/core/handlers/team_handler.py` — Event routing/rewriting
- `hooks/src/claude_office_hooks/event_mapper.py` — Extracts `team_name`/`teammate_name` from Agent tool input and `teammate_spawned` response

**SendMessage fields:** Real Claude Code uses `message` and `content` (not `text`). The handler checks all variants.

## Project Skills

- **/office-sprite** - Generate office furniture sprites
- **/character-sprite** - Generate character sprite sheets
- **/desk-accessory** - Generate tintable desk items

See `.claude/skills/*/SKILL.md` for details.

## Workflow Guidelines

**Commit after every batch of work:** Always commit after completing each logical unit.

**Use subagents for validation:** Spawn a Bash subagent to run `make checkall` and commit:
```
"Run 'make checkall' from the project root. If successful, commit with message: '<message>'"
```

## Version Management

**Keep all version locations in sync** when bumping versions:

| Location | File |
|----------|------|
| Root package | `pyproject.toml` |
| Backend | `backend/pyproject.toml` |
| Hooks | `hooks/pyproject.toml` |
| Hooks CLI | `hooks/src/claude_office_hooks/main.py` (`__version__`) |
| Frontend package | `frontend/package.json` |
| Frontend display | `frontend/src/app/page.tsx` (header badge) |
