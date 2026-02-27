# Glock AI Coding Assistant - Architecture Diagram

## High-Level Architecture (Model B: Client-Orchestrated)

```mermaid
graph TB
    subgraph "User Machine"
        CLI[Glock CLI]
        subgraph "CLI Components"
            ORCH[OrchestrationEngine]
            TOOLS[ToolBroker]
            CTX[ContextPacker]
            SESS[SessionManager]
            TUI[Terminal UI]
            CRYPTO[CryptoManager]
            PLAN[PlanValidator]
            STORAGE[LocalStorage]
        end
        
        CLI --> ORCH
        CLI --> TUI
        ORCH --> TOOLS
        ORCH --> CTX
        ORCH --> SESS
        ORCH --> CRYPTO
        ORCH --> PLAN
        ORCH --> STORAGE
    end
    
    subgraph "Control Plane (Cloud)"
        GW[Gateway Service]
        subgraph "Gateway Components"
            PROXY[LLM Proxy]
            REHY[Context Rehydrator]
            CHKPT[Checkpoint Store]
            METER[Rate Limiter & Metering]
            PLANNER[Plan Compiler]
            HEALER[Self Healer]
        end
        
        GW --> PROXY
        GW --> REHY
        GW --> CHKPT
        GW --> METER
        GW --> PLANNER
        GW --> HEALER
        
        REDIS[(Redis)]
        POSTGRES[(PostgreSQL)]
        
        GW --> REDIS
        GW --> POSTGRES
    end
    
    subgraph "External Services"
        ANTHROPIC[Anthropic Claude]
        OPENAI[OpenAI GPT]
        GOOGLE[Google Gemini]
    end
    
    CLI <--> GW
    PROXY --> ANTHROPIC
    PROXY --> OPENAI
    PROXY --> GOOGLE
    
    REDIS -.-> |Sessions, Routing| GW
    POSTGRES -.-> |Users, Checkpoints, Usage| GW
```

## Detailed Component Architecture

### Client-Side Components (CLI)

```mermaid
graph TD
    subgraph "CLI Application"
        MAIN[main.py]
        
        subgraph "Core Orchestration"
            ORCH[OrchestrationEngine]
            ENGINE[engine.py]
        end
        
        subgraph "Tool Execution"
            BROKER[ToolBroker]
            RUNTIME[Runtime Registry]
        end
        
        subgraph "Context Management"
            PACKER[ContextPacker]
            COMPRESSOR[Compressor]
            SLICER[Context Slicer]
            FACTS[Facts Extractor]
            SUMMARY[Summarizer]
            BUDGET[Token Budget]
            DELTA[Delta Calculator]
        end
        
        subgraph "Session & Security"
            SESSION[SessionManager]
            CRYPTO[CryptoManager]
            CAPSULE[Session Capsule]
        end
        
        subgraph "Transport & Protocol"
            TRANSPORT[Transport Layer]
            VALIDATION[Plan Validator]
            PROTOCOL[Shared Protocol]
        end
        
        subgraph "User Interface"
            TUI[Terminal UI]
            CLI_CMD[CLI Commands]
        end
        
        subgraph "Storage"
            STORAGE[Local Storage]
            WORKTREE[Git Worktree]
        end
    end
    
    MAIN --> ORCH
    MAIN --> TUI
    ORCH --> ENGINE
    ORCH --> BROKER
    ORCH --> PACKER
    ORCH --> SESSION
    ORCH --> TRANSPORT
    
    BROKER --> RUNTIME
    
    PACKER --> COMPRESSOR
    PACKER --> SLICER
    PACKER --> FACTS
    PACKER --> SUMMARY
    PACKER --> BUDGET
    PACKER --> DELTA
    
    SESSION --> CRYPTO
    SESSION --> CAPSULE
    
    TRANSPORT --> VALIDATION
    TRANSPORT --> PROTOCOL
    
    TUI --> CLI_CMD
    
    SESSION --> STORAGE
    STORAGE --> WORKTREE
```

### Server-Side Components (Gateway)

```mermaid
graph TD
    subgraph "Gateway Service"
        MAIN_GW[main.py]
        
        subgraph "API Layer"
            WS[WebSocket Handler]
            API[REST API]
            PROTOCOL_GW[Protocol Handler]
        end
        
        subgraph "Core Services"
            PROXY[LLM Proxy]
            REHYDRATOR[Context Rehydrator]
            CHECKPOINT[Checkpoint Manager]
        end
        
        subgraph "Business Logic"
            PLANNER[Plan Compiler]
            METERING[Usage Metering]
            HEALER[Self Healer]
        end
        
        subgraph "Data Layer"
            STORAGE_GW[Storage Manager]
            CONTEXT_MGR[Context Manager]
        end
        
        subgraph "External Dependencies"
            REDIS_CLIENT[Redis Client]
            PG_CLIENT[PostgreSQL Client]
            LLM_CLIENTS[LLM Clients]
        end
    end
    
    MAIN_GW --> WS
    MAIN_GW --> API
    WS --> PROTOCOL_GW
    API --> PROTOCOL_GW
    
    PROTOCOL_GW --> PROXY
    PROTOCOL_GW --> REHYDRATOR
    PROTOCOL_GW --> CHECKPOINT
    
    PROXY --> LLM_CLIENTS
    REHYDRATOR --> CONTEXT_MGR
    CHECKPOINT --> STORAGE_GW
    
    PLANNER --> METERING
    HEALER --> STORAGE_GW
    
    STORAGE_GW --> REDIS_CLIENT
    STORAGE_GW --> PG_CLIENT
    CONTEXT_MGR --> REDIS_CLIENT
```

## Data Flow Architecture

```mermaid
sequenceDiagram
    participant U as User
    participant CLI as Glock CLI
    participant ORCH as Orchestrator
    participant CTX as ContextPacker
    participant GW as Gateway
    participant LLM as LLM Provider
    participant DB as Database
    
    U->>CLI: glock "Fix the bug"
    CLI->>ORCH: Initialize session
    ORCH->>CTX: Pack context (40-60% reduction)
    CTX-->>ORCH: Compressed context
    
    ORCH->>GW: WebSocket: LLM request + context
    GW->>DB: Store checkpoint (encrypted)
    GW->>LLM: Proxy request
    LLM-->>GW: Response with plan
    GW->>GW: Validate & sign plan
    GW-->>ORCH: Signed execution plan
    
    ORCH->>ORCH: Execute tools locally
    ORCH->>CTX: Update context delta
    CTX-->>ORCH: New compressed context
    
    loop Until task complete
        ORCH->>GW: Continue with delta
        GW->>LLM: Next iteration
        LLM-->>GW: Next steps
        GW-->>ORCH: Next plan
        ORCH->>ORCH: Execute locally
    end
    
    ORCH->>GW: Session complete
    GW->>DB: Final checkpoint
    ORCH-->>CLI: Results
    CLI-->>U: Show results
```

## Security & Isolation Architecture

```mermaid
graph TB
    subgraph "Security Layers"
        subgraph "Session Isolation"
            SESS_KEY[Per-Session Encryption Keys]
            WORKTREE[Git Worktree Isolation]
            SANDBOX[Process Sandboxing]
        end
        
        subgraph "Data Protection"
            ENCRYPT[AES-256 Encryption]
            SIGNING[Plan Signing & Validation]
            KEYS[Key Management]
        end
        
        subgraph "Network Security"
            TLS[TLS/WSS Transport]
            AUTH[JWT Authentication]
            RATE[Rate Limiting]
        end
        
        subgraph "Access Control"
            RBAC[Role-Based Access]
            SCOPE[Tool Scope Limiting]
            AUDIT[Audit Logging]
        end
    end
    
    SESS_KEY --> ENCRYPT
    WORKTREE --> SANDBOX
    ENCRYPT --> SIGNING
    SIGNING --> KEYS
    
    TLS --> AUTH
    AUTH --> RATE
    
    RBAC --> SCOPE
    SCOPE --> AUDIT
```

## Deployment Architecture

```mermaid
graph TB
    subgraph "Production Environment"
        subgraph "Load Balancer"
            LB[nginx/ALB]
        end
        
        subgraph "Gateway Cluster"
            GW1[Gateway Instance 1]
            GW2[Gateway Instance 2]
            GW3[Gateway Instance N]
        end
        
        subgraph "Data Layer"
            REDIS_CLUSTER[Redis Cluster]
            PG_CLUSTER[PostgreSQL Cluster]
        end
        
        subgraph "Monitoring"
            METRICS[Metrics Collection]
            LOGS[Log Aggregation]
            ALERTS[Alerting]
        end
    end
    
    subgraph "Client Machines"
        CLI1[User CLI 1]
        CLI2[User CLI 2]
        CLI3[User CLI N]
    end
    
    CLI1 --> LB
    CLI2 --> LB
    CLI3 --> LB
    
    LB --> GW1
    LB --> GW2
    LB --> GW3
    
    GW1 --> REDIS_CLUSTER
    GW1 --> PG_CLUSTER
    GW2 --> REDIS_CLUSTER
    GW2 --> PG_CLUSTER
    GW3 --> REDIS_CLUSTER
    GW3 --> PG_CLUSTER
    
    GW1 --> METRICS
    GW2 --> LOGS
    GW3 --> ALERTS
```

## Key Architectural Principles

### 1. Client-Orchestrated Model
- **Full agent loop runs on client**: Orchestration, tool execution, context management
- **Server as stateless proxy**: Only handles LLM requests and checkpoint storage
- **Horizontal scaling**: No per-session server processes needed

### 2. Context Optimization
- **Token reduction**: 40-60% reduction through intelligent compression
- **Delta updates**: Only send context changes, not full history
- **Smart packing**: Facts extraction, summarization, and slicing

### 3. Security First
- **Per-session encryption**: Unique keys for each session
- **Workspace isolation**: Git worktrees prevent cross-contamination
- **Plan validation**: Server-signed execution plans prevent malicious code

### 4. Performance & Cost
- **Local execution**: File operations, git, bash run on user machine
- **Minimal server resources**: Stateless design enables cheap scaling
- **Efficient transport**: WebSocket with binary protocol for speed

### 5. Developer Experience
- **Zero configuration**: Works out of the box in any directory
- **Session continuity**: Encrypted checkpoints for pause/resume
- **Rich TUI**: Interactive terminal interface with real-time updates

## Technology Stack

### Client (CLI)
- **Language**: Python 3.11+
- **UI**: Rich TUI library
- **Crypto**: cryptography library (AES-256)
- **Transport**: WebSocket client
- **Tools**: Native OS tools (git, bash, etc.)

### Server (Gateway)
- **Framework**: FastAPI + uvicorn
- **Protocol**: WebSocket + REST API
- **Database**: PostgreSQL for persistence
- **Cache**: Redis for sessions
- **Deployment**: Docker + Kubernetes

### Shared
- **Protocol**: JSON Schema validated messages
- **Security**: JWT tokens, TLS encryption
- **Monitoring**: Structured logging, metrics

This architecture enables Glock to provide powerful AI coding assistance while maintaining security, performance, and cost-effectiveness through its innovative client-orchestrated design.