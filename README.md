# Glock

An AI-powered coding assistant with **client-orchestrated architecture** for secure, scalable, and cost-efficient code assistance.

## Overview

Glock is a CLI tool that provides AI-assisted coding with:

- **Client-side orchestration** - Full agent loop runs locally for cheap horizontal scaling
- **Server as LLM proxy** - Stateless server, no per-session processes
- **Local tool execution** - File operations, git, bash run on your machine
- **Context packing** - 40-60% token reduction via intelligent compression
- **Session checkpoints** - Encrypted checkpoints for pause/resume
- **Session isolation** - Per-session encryption keys and workspace sandboxing

## Architecture (Model B)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            YOUR MACHINE                                  │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Glock CLI                                                         │  │
│  │   ├─ OrchestrationEngine (full agent loop)                        │  │
│  │   ├─ ToolBroker (read/edit/bash/git/grep/glob)                    │  │
│  │   ├─ ContextPacker (token minimizer - 40-60% reduction)           │  │
│  │   ├─ SessionKeyManager (per-session encryption)                   │  │
│  │   └─ Worktree isolation per session                               │  │
│  └────────────────────────────┬──────────────────────────────────────┘  │
└───────────────────────────────┼─────────────────────────────────────────┘
                                │ WebSocket (LLM proxy + checkpoints)
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         CONTROL PLANE                                    │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  Gateway Service (stateless, horizontally scalable)              │    │
│  │   ├─ LLM Proxy (Anthropic, OpenAI, Google)                      │    │
│  │   ├─ Context Rehydrator (checkpoint → full context)             │    │
│  │   ├─ Checkpoint Store (encrypted, per-session keys)             │    │
│  │   ├─ Rate Limits & Metering                                     │    │
│  │   └─ Plan Compiler (returns signed plans)                       │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│            │                          │                                  │
│            ▼                          ▼                                  │
│        Redis                     Postgres                                │
│   (sessions, routing)     (users, checkpoints, usage)                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### Key Differences from Traditional Architecture

| Aspect | Traditional | Glock Model B |
|--------|-------------|---------------|
| Orchestration | Server-side | **Client-side** |
| Per-session processes | Yes (expensive) | **No** |
| Server scaling | Vertical (per session) | **Horizontal (stateless)** |
| Context storage | Full history sent | **Delta + checkpoints** |
| Token efficiency | ~100% | **40-60% reduced** |

## Quick Start

### Installation

```bash
# One-line install
curl -fsSL https://glock.dev/install.sh | bash

# Or with pip
pip install glock-cli
```

### Login & Start

```bash
# Authenticate (opens browser)
glock login

# Start coding in your project
cd my-project
glock

# Or start with a task
glock "Fix the authentication bug in login.py"
```

### CLI Commands

```bash
# Interactive session
glock                           # Start TUI in current directory
glock "Your task here"          # Start with initial prompt

# Authentication
glock login                     # OAuth login flow
glock logout                    # Clear credentials
glock whoami                    # Show current user

# Session management
glock sessions                  # List recent sessions
glock resume <session_id>       # Resume previous session
glock sessions --clean          # Clean up old sessions

# Configuration
glock config                    # Show current config
glock config set model sonnet   # Set default model (fast/standard/advanced)

# Diagnostics
glock doctor                    # Check installation & connectivity
glock version                   # Show version info
```

## Development Setup

### Prerequisites

- Python 3.11+
- Redis
- PostgreSQL (or Supabase)

### Local Development

```bash
# Clone and setup
git clone https://github.com/glock/glock.git
cd glock-final

python -m venv .venv
source .venv/bin/activate

# Install packages
pip install -e ./apps/cli
pip install -e ./apps/server
pip install -e ./packages/shared-protocol
```

### Environment Variables

Create `.env`:

```bash
# Database
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/glock
REDIS_URL=redis://localhost:6379

# Authentication
JWT_SECRET=your-secret-key-at-least-32-characters
JWT_ISSUER=glock.dev

# LLM Providers
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Context Encryption
CONTEXT_MASTER_KEY=<64 character hex string>
```

### Database Setup

```bash
# Run migrations
cd infra/supabase
psql $DATABASE_URL -f migrations/0001_init.sql
psql $DATABASE_URL -f migrations/0002_sessions_tasks.sql
psql $DATABASE_URL -f migrations/0003_plans.sql
psql $DATABASE_URL -f migrations/0004_usage_audit.sql
psql $DATABASE_URL -f migrations/0005_preferences.sql
psql $DATABASE_URL -f migrations/0006_context_checkpoints.sql
```

### Running Services

```bash
# Terminal 1: Start server (Model B - just the gateway)
cd apps/server
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2: Run CLI
cd /path/to/your/project
GLOCK_SERVER_URL=ws://localhost:8000 glock
```

## Project Structure

```
glock-final/
├── apps/
│   ├── cli/                      # Client application
│   │   └── src/
│   │       ├── cli/              # Click commands
│   │       ├── tui/              # Terminal UI (Rich)
│   │       ├── session/          # Session host
│   │       ├── orchestrator/     # Full orchestration engine
│   │       ├── context/          # Context packing system
│   │       │   ├── packer.py     # Main coordinator
│   │       │   ├── budget.py     # Token budgets
│   │       │   ├── compressor.py # Tool output compression
│   │       │   ├── slicer.py     # File slicing
│   │       │   ├── summary.py    # Rolling summary
│   │       │   ├── facts.py      # Pinned facts
│   │       │   └── delta.py      # Delta builder
│   │       ├── crypto/           # Session encryption
│   │       ├── tools/            # Local tool execution
│   │       └── transport/        # WebSocket client
│   │
│   └── server/                   # Control plane (stateless)
│       └── src/
│           ├── gateway/          # WebSocket + REST API
│           │   └── ws/
│           │       ├── client_handler.py
│           │       ├── llm_handler.py    # LLM proxy
│           │       └── router.py
│           ├── context/          # Context rehydration
│           ├── planner/          # Plan compilation
│           ├── storage/          # Redis + Postgres + Checkpoints
│           ├── metering/         # Usage tracking
│           └── auth/             # JWT authentication
│
├── packages/
│   └── shared-protocol/          # Shared types & schemas
│
└── infra/
    ├── docker/                   # Dockerfiles
    ├── railway/                  # Railway deployment
    └── supabase/                 # Database migrations
```

## Context Packing System

The ContextPacker achieves 40-60% token reduction through:

### Token Budget Allocation

| Component | Default Tokens | Purpose |
|-----------|----------------|---------|
| System prompt | 2,000 | Base instructions |
| Rolling summary | 4,000 | Session progress |
| Pinned facts | 3,000 | Key information (~30 facts) |
| File context | 15,000 | Relevant code slices |
| Tool results | 8,000 | Compressed outputs |
| Conversation | 10,000 | Recent messages |
| Delta | 5,000 | New since checkpoint |
| Completion reserve | 8,000 | Response headroom |

### Tool Output Compression

| Tool | Limit | Strategy |
|------|-------|----------|
| read_file | 4,000 chars | Keep structure |
| grep | 2,000 chars | Just matches |
| bash | 2,500 chars | Focus on errors |
| glob | 1,500 chars | Just paths |

### Delta Transfer

Instead of sending full context every request:
- First request: Full context → Server stores checkpoint
- Subsequent: Just the delta (new messages/results)
- Server rehydrates: Checkpoint + delta = full context

**Bandwidth savings: ~86%** for long conversations.

## Deployment

### Railway (Recommended)

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login and deploy
railway login
railway link
railway up
```

### Docker Compose

```bash
docker-compose -f infra/docker/docker-compose.yml up -d
```

### Environment Variables (Production)

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection |
| `REDIS_URL` | Yes | Redis connection |
| `JWT_SECRET` | Yes | 32+ character secret |
| `ANTHROPIC_API_KEY` | Yes* | Claude API key |
| `OPENAI_API_KEY` | No | GPT API key |
| `CONTEXT_MASTER_KEY` | Yes | 64-char hex for encryption |

## Security

- **Per-session encryption** - Each session has derived encryption keys (HKDF)
- **AES-256-GCM** - Context checkpoints encrypted at rest
- **Plan signing** - Ed25519 signed execution plans
- **Path sandboxing** - Tools restricted to workspace
- **Prompt injection detection** - Scans for injection attempts
- **Credential scanning** - Prevents accidental exposure

## Supported Models

| Tier | Anthropic | OpenAI | Google |
|------|-----------|--------|--------|
| Fast | Haiku | GPT-4o-mini | Flash |
| Standard | Sonnet 4 | GPT-4o | Pro |
| Advanced | Opus 4.5 | - | - |

## Troubleshooting

```bash
# Check server health
curl http://localhost:8000/health
curl http://localhost:8000/ready

# Debug mode
GLOCK_DEBUG=1 glock

# Check session status
glock sessions

# Clear local state
rm -rf ~/.glock/sessions
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests: `pytest apps/`
5. Submit a pull request

## License

MIT License - see [LICENSE](LICENSE) for details.

## Acknowledgments

Built with:
- [FastAPI](https://fastapi.tiangolo.com/) - Server framework
- [Click](https://click.palletsprojects.com/) - CLI framework
- [Rich](https://rich.readthedocs.io/) - Terminal UI
- [websockets](https://websockets.readthedocs.io/) - WebSocket client
- [cryptography](https://cryptography.io/) - Encryption
