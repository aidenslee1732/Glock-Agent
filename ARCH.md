# Glock Architecture

## Table of Contents

- [Deployment Architecture](#deployment-architecture)
- [Message Flow Architecture](#message-flow-architecture)
- [Component Details](#component-details)

---

## Deployment Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              USERS                                               │
│                                                                                  │
│    ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐             │
│    │  User A  │     │  User B  │     │  User C  │     │  User N  │             │
│    │ (macOS)  │     │ (Linux)  │     │ (Windows)│     │   ...    │             │
│    └────┬─────┘     └────┬─────┘     └────┬─────┘     └────┬─────┘             │
│         │                │                │                │                    │
│         ▼                ▼                ▼                ▼                    │
│    ┌──────────────────────────────────────────────────────────────────────┐    │
│    │                         GLOCK CLI (installed locally)                 │    │
│    │                                                                       │    │
│    │  • Orchestration Engine    • Tool Execution    • Context Packing     │    │
│    │  • Session Management      • Encryption        • TUI Display         │    │
│    └───────────────────────────────────┬──────────────────────────────────┘    │
│                                        │                                        │
└────────────────────────────────────────┼────────────────────────────────────────┘
                                         │
                                         │ HTTPS / WebSocket (TLS)
                                         │ JWT Authenticated
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              CLOUD INFRASTRUCTURE                                │
│                                                                                  │
│    ┌────────────────────────────────────────────────────────────────────────┐   │
│    │                         LOAD BALANCER                                   │   │
│    │                    (SSL Termination, Routing)                          │   │
│    └─────────────────────────────────┬──────────────────────────────────────┘   │
│                                      │                                          │
│              ┌───────────────────────┼───────────────────────┐                  │
│              ▼                       ▼                       ▼                  │
│    ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐           │
│    │    Gateway 1    │    │    Gateway 2    │    │    Gateway N    │           │
│    │   (Stateless)   │    │   (Stateless)   │    │   (Stateless)   │           │
│    │                 │    │                 │    │                 │           │
│    │ • Auth (JWT)    │    │ • Auth (JWT)    │    │ • Auth (JWT)    │           │
│    │ • LLM Proxy     │    │ • LLM Proxy     │    │ • LLM Proxy     │           │
│    │ • Checkpoints   │    │ • Checkpoints   │    │ • Checkpoints   │           │
│    │ • Rate Limiting │    │ • Rate Limiting │    │ • Rate Limiting │           │
│    └────────┬────────┘    └────────┬────────┘    └────────┬────────┘           │
│             │                      │                      │                     │
│             └──────────────────────┼──────────────────────┘                     │
│                                    │                                            │
│              ┌─────────────────────┴─────────────────────┐                      │
│              ▼                                           ▼                      │
│    ┌─────────────────┐                        ┌─────────────────┐               │
│    │      Redis      │                        │   PostgreSQL    │               │
│    │                 │                        │                 │               │
│    │ • Sessions      │                        │ • Users         │               │
│    │ • Rate Limits   │                        │ • Checkpoints   │               │
│    │ • Routing       │                        │ • Usage Logs    │               │
│    │ • Pub/Sub       │                        │ • Audit Trail   │               │
│    └─────────────────┘                        └─────────────────┘               │
│                                                                                  │
│                                    │                                            │
│                                    ▼                                            │
│                         ┌─────────────────┐                                     │
│                         │   LLM Provider  │                                     │
│                         │   (Anthropic)   │                                     │
│                         │                 │                                     │
│                         │ • Claude Haiku  │                                     │
│                         │ • Claude Sonnet │                                     │
│                         │ • Claude Opus   │                                     │
│                         └─────────────────┘                                     │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Deployment Summary

| Component | Location | Scaling | Purpose |
|-----------|----------|---------|---------|
| Glock CLI | User's machine | N/A | Orchestration, tools, UI |
| Load Balancer | Cloud | Auto | SSL, routing, health checks |
| Gateway | Cloud | Horizontal (2+ replicas) | Auth, LLM proxy, checkpoints |
| Redis | Cloud | Single/Cluster | Sessions, rate limits, cache |
| PostgreSQL | Cloud | Single/Replicas | Persistent storage |
| LLM Provider | External (Anthropic) | Managed | AI responses |

---

## Message Flow Architecture

### User Sends a Message

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                   CLIENT                                         │
│                              (User's Machine)                                    │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ 1. USER INPUT                                                            │    │
│  │    User types: "Fix the bug in auth.py"                                 │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                         │                                        │
│                                         ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ 2. ORCHESTRATION ENGINE                                          [CLIENT]│    │
│  │    • Adds user message to conversation history                          │    │
│  │    • Prepares context pack (rolling summary, pinned facts)              │    │
│  │    • Builds delta (new messages since last checkpoint)                  │    │
│  │    • Compresses tool results (40-60% token reduction)                   │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                         │                                        │
│                                         ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ 3. TRANSPORT LAYER                                               [CLIENT]│    │
│  │    • Encrypts sensitive context with session key                        │    │
│  │    • Sends LLM_REQUEST over WebSocket                                   │    │
│  │    {                                                                    │    │
│  │      "type": "LLM_REQUEST",                                             │    │
│  │      "context_ref": "cp_abc123",      // Previous checkpoint            │    │
│  │      "delta": { messages, tool_results },                               │    │
│  │      "context_pack": { summary, facts, file_slices },                   │    │
│  │      "tools": [ ... tool definitions ... ]                              │    │
│  │    }                                                                    │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                         │                                        │
└─────────────────────────────────────────┼────────────────────────────────────────┘
                                          │
                                          │ WebSocket (encrypted)
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                   SERVER                                         │
│                              (Cloud Gateway)                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ 4. AUTHENTICATION                                                [SERVER]│    │
│  │    • Validates JWT token                                                │    │
│  │    • Checks user exists and is active                                   │    │
│  │    • Verifies session ownership                                         │    │
│  │    • Checks rate limits (Redis)                                         │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                         │                                        │
│                                         ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ 5. CONTEXT REHYDRATION                                           [SERVER]│    │
│  │    • Loads checkpoint from PostgreSQL (if context_ref provided)         │    │
│  │    • Decrypts checkpoint using session key                              │    │
│  │    • Merges checkpoint + delta = full conversation                      │    │
│  │    • Builds system prompt from context_pack                             │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                         │                                        │
│                                         ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ 6. LLM PROXY                                                     [SERVER]│    │
│  │    • Formats messages for Anthropic API                                 │    │
│  │    • Sends request to Claude                                            │    │
│  │    • Streams response tokens back                                       │    │
│  │    • Tracks token usage for billing                                     │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                         │                                        │
│                                         │ Streaming tokens                       │
│                                         ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ 7. RESPONSE STREAMING                                            [SERVER]│    │
│  │    • Sends LLM_DELTA messages as tokens arrive                          │    │
│  │    { "type": "LLM_DELTA", "content": "I'll fix...", "index": 0 }       │    │
│  │    { "type": "LLM_DELTA", "content": " the bug", "index": 1 }          │    │
│  │    ...                                                                  │    │
│  │    • Sends LLM_RESPONSE_END with tool_calls (if any)                    │    │
│  │    { "type": "LLM_RESPONSE_END", "tool_calls": [...], "tokens": 150 }  │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                         │                                        │
└─────────────────────────────────────────┼────────────────────────────────────────┘
                                          │
                                          │ Streaming WebSocket
                                          ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                   CLIENT                                         │
│                              (User's Machine)                                    │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ 8. DISPLAY STREAMING                                             [CLIENT]│    │
│  │    • Shows tokens as they arrive (real-time typing effect)              │    │
│  │    • Updates TUI with assistant response                                │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                         │                                        │
│                                         ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ 9. TOOL EXECUTION (if tool_calls present)                        [CLIENT]│    │
│  │    • Parses tool calls from response                                    │    │
│  │    • Executes tools LOCALLY:                                            │    │
│  │      - read_file → reads file from disk                                 │    │
│  │      - edit_file → modifies file on disk                                │    │
│  │      - bash → runs command in shell                                     │    │
│  │      - grep/glob → searches filesystem                                  │    │
│  │    • Collects tool results                                              │    │
│  │    • Shows progress in TUI (⟳ Reading file.py ✓)                        │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                         │                                        │
│                                         ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ 10. NEXT TURN (if tools were called)                             [CLIENT]│    │
│  │    • Adds assistant message + tool results to history                   │    │
│  │    • Goes back to step 2 (Orchestration Engine)                         │    │
│  │    • Loop continues until no more tool calls                            │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                         │                                        │
│                                         ▼                                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │ 11. CHECKPOINT (periodically)                                    [CLIENT]│    │
│  │    • Encrypts conversation state with session key                       │    │
│  │    • Sends CONTEXT_CHECKPOINT to server                                 │    │
│  │    • Server stores in PostgreSQL for resume capability                  │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Message Flow Summary

| Step | Location | What Happens |
|------|----------|--------------|
| 1 | CLIENT | User types message |
| 2 | CLIENT | Orchestrator builds context + delta |
| 3 | CLIENT | Sends LLM_REQUEST via WebSocket |
| 4 | SERVER | Validates JWT, checks rate limits |
| 5 | SERVER | Rehydrates context from checkpoint + delta |
| 6 | SERVER | Proxies request to Anthropic Claude |
| 7 | SERVER | Streams response tokens back |
| 8 | CLIENT | Displays streaming response |
| 9 | CLIENT | Executes tools locally (if any) |
| 10 | CLIENT | Loops back for next turn (if tools called) |
| 11 | CLIENT | Saves checkpoint for resume |

---

## Component Details

### Client Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        GLOCK CLI                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────┐  │
│  │  TUI (Display)   │  │  CLI Commands    │  │  Config       │  │
│  │                  │  │                  │  │               │  │
│  │  • Rich console  │  │  • glock login   │  │  • ~/.glock/  │  │
│  │  • Streaming     │  │  • glock logout  │  │  • Credentials│  │
│  │  • Spinners      │  │  • glock version │  │  • Settings   │  │
│  └────────┬─────────┘  └────────┬─────────┘  └───────┬───────┘  │
│           │                     │                    │          │
│           └─────────────────────┼────────────────────┘          │
│                                 ▼                               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                 ORCHESTRATION ENGINE                      │   │
│  │                                                           │   │
│  │  • Manages conversation turns                             │   │
│  │  • Decides when to call tools vs. respond                 │   │
│  │  • Enforces turn limits and token budgets                 │   │
│  │  • Handles force-final-response when approaching limits   │   │
│  └─────────────────────────┬─────────────────────────────────┘   │
│                            │                                    │
│       ┌────────────────────┼────────────────────┐               │
│       ▼                    ▼                    ▼               │
│  ┌──────────┐      ┌──────────────┐      ┌──────────────┐       │
│  │  TOOLS   │      │   CONTEXT    │      │  TRANSPORT   │       │
│  │          │      │   PACKER     │      │              │       │
│  │ read_file│      │              │      │  WebSocket   │       │
│  │ edit_file│      │ • Summarizer │      │  client      │       │
│  │ bash     │      │ • Compressor │      │              │       │
│  │ grep     │      │ • Slicer     │      │  JWT auth    │       │
│  │ glob     │      │ • Delta      │      │              │       │
│  └──────────┘      └──────────────┘      └──────────────┘       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Server Components

```
┌─────────────────────────────────────────────────────────────────┐
│                      GATEWAY SERVICE                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    WebSocket Handler                      │   │
│  │                                                           │   │
│  │  • Accepts connections at /ws/client                      │   │
│  │  • Routes messages by type                                │   │
│  │  • Manages connection lifecycle                           │   │
│  └─────────────────────────┬─────────────────────────────────┘   │
│                            │                                    │
│       ┌────────────────────┼────────────────────┐               │
│       ▼                    ▼                    ▼               │
│  ┌──────────┐      ┌──────────────┐      ┌──────────────┐       │
│  │   AUTH   │      │  LLM HANDLER │      │  CHECKPOINT  │       │
│  │          │      │              │      │    STORE     │       │
│  │ • JWT    │      │ • Rehydrate  │      │              │       │
│  │ • Verify │      │ • Proxy LLM  │      │ • Save       │       │
│  │ • Tokens │      │ • Stream     │      │ • Load       │       │
│  │          │      │ • Track usage│      │ • Encrypt    │       │
│  └──────────┘      └──────────────┘      └──────────────┘       │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                      STORAGE LAYER                        │   │
│  │                                                           │   │
│  │  ┌─────────────┐              ┌─────────────┐            │   │
│  │  │    Redis    │              │  PostgreSQL │            │   │
│  │  │             │              │             │            │   │
│  │  │ • Sessions  │              │ • Users     │            │   │
│  │  │ • Routes    │              │ • Sessions  │            │   │
│  │  │ • Rate lim  │              │ • Checkpts  │            │   │
│  │  │ • Cache     │              │ • Usage     │            │   │
│  │  └─────────────┘              └─────────────┘            │   │
│  │                                                           │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Key Design Decisions

### Why Client-Side Orchestration?

| Benefit | Explanation |
|---------|-------------|
| **Cost** | No per-session server processes = cheaper |
| **Scale** | Server is stateless = easy horizontal scaling |
| **Privacy** | Code never leaves user's machine |
| **Latency** | Tools execute locally = faster |

### Why Delta + Checkpoints?

| Benefit | Explanation |
|---------|-------------|
| **Bandwidth** | Only send new data, not full history |
| **Resume** | Users can pause and resume sessions |
| **Efficiency** | ~86% bandwidth savings for long conversations |

### Why Encrypt Checkpoints?

| Benefit | Explanation |
|---------|-------------|
| **Privacy** | Server can't read conversation content |
| **Security** | Per-session keys = breach isolation |
| **Compliance** | Data encrypted at rest |

---

## Data Flow Diagram

```
USER INPUT          "Fix the bug in auth.py"
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│ CLIENT: Build Request                                            │
│                                                                  │
│  context_pack: {                                                 │
│    rolling_summary: "Working on auth module...",                 │
│    pinned_facts: ["Python 3.11", "FastAPI"],                    │
│    file_slices: [{ path: "auth.py", lines: "1-50" }]            │
│  }                                                               │
│                                                                  │
│  delta: {                                                        │
│    messages: [{ role: "user", content: "Fix the bug..." }],     │
│    tool_results: []                                              │
│  }                                                               │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ SERVER: Process Request                                          │
│                                                                  │
│  1. Verify JWT token                                             │
│  2. Load checkpoint (if exists)                                  │
│  3. Merge: checkpoint + delta = full_messages                    │
│  4. Build: system_prompt + full_messages + tools                 │
│  5. Send to Claude API                                           │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ ANTHROPIC: Generate Response                                     │
│                                                                  │
│  Response: "I'll read the auth.py file to find the bug."        │
│  Tool calls: [{ name: "read_file", args: { path: "auth.py" }}]  │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ CLIENT: Execute Tools                                            │
│                                                                  │
│  read_file("auth.py") → returns file content                    │
│  Display: ⟳ Reading auth.py ✓                                   │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
                         LOOP CONTINUES
                    (until no more tool calls)
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ CLIENT: Final Response                                           │
│                                                                  │
│  "I found the bug on line 45. The password comparison was       │
│   using == instead of secrets.compare_digest(). I've fixed it." │
│                                                                  │
│  Display final response to user                                  │
│  Save checkpoint for resume                                      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Security Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      SECURITY LAYERS                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. TRANSPORT SECURITY                                           │
│     └─ TLS 1.3 for all connections                              │
│                                                                  │
│  2. AUTHENTICATION                                               │
│     └─ JWT tokens (short-lived access + refresh)                │
│     └─ Browser-based OAuth login                                │
│                                                                  │
│  3. SESSION ISOLATION                                            │
│     └─ Per-session encryption keys (HKDF derived)               │
│     └─ Session ownership verification                           │
│     └─ User can only access own sessions                        │
│                                                                  │
│  4. DATA ENCRYPTION                                              │
│     └─ Checkpoints encrypted with AES-256-GCM                   │
│     └─ Server cannot read conversation content                  │
│                                                                  │
│  5. RATE LIMITING                                                │
│     └─ Per-user request limits                                  │
│     └─ Concurrent session limits                                │
│                                                                  │
│  6. CODE ISOLATION                                               │
│     └─ Tools execute on CLIENT only                             │
│     └─ Server never sees source code                            │
│     └─ Path sandboxing to workspace                             │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```
