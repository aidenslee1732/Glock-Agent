# Deployment Guide

This guide covers deploying Glock to production environments.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Railway Deployment](#railway-deployment)
- [Manual Deployment](#manual-deployment)
- [Supabase Setup](#supabase-setup)
- [Environment Variables](#environment-variables)
- [Scaling](#scaling)
- [Monitoring](#monitoring)
- [Security Hardening](#security-hardening)

## Architecture Overview

Production deployment consists of:

```
┌─────────────────────────────────────────────────────────────────────┐
│                         RAILWAY                                      │
│                                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐              │
│  │   Gateway    │  │   Gateway    │  │   Runtime    │              │
│  │   Service    │  │   Service    │  │    Host      │              │
│  │   (2 replicas)│  │             │  │              │              │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘              │
│         │                 │                 │                       │
│         └────────┬────────┴────────┬────────┘                       │
│                  │                 │                                │
│         ┌────────▼────────┐ ┌──────▼───────┐                       │
│         │     Redis       │ │   Healer     │                       │
│         │   (Railway)     │ │   Worker     │                       │
│         └─────────────────┘ └──────────────┘                       │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │      Supabase       │
                         │    (PostgreSQL)     │
                         └─────────────────────┘
```

## Prerequisites

- [Railway CLI](https://docs.railway.app/develop/cli) installed
- [Supabase](https://supabase.com) account
- Domain for SSL (e.g., `api.glock.dev`)
- Ed25519 key pair for plan signing

### Generate Signing Keys

```bash
# Generate Ed25519 key pair
python -c "
from nacl.signing import SigningKey
from nacl.encoding import Base64Encoder

key = SigningKey.generate()
print('Private key:', key.encode(encoder=Base64Encoder).decode())
print('Public key:', key.verify_key.encode(encoder=Base64Encoder).decode())
"
```

Save the private key securely - you'll need it for `PLAN_SIGNING_PRIVATE_KEY`.

## Railway Deployment

### 1. Create Railway Project

```bash
# Login to Railway
railway login

# Create new project
railway init

# Link to existing repo
railway link
```

### 2. Configure Services

Create `railway.toml` in project root:

```toml
[build]
builder = "dockerfile"

[deploy]
numReplicas = 2
healthcheckPath = "/health"
healthcheckTimeout = 10
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

### 3. Add Services

```bash
# Gateway service
railway add --service gateway-service

# Runtime host
railway add --service runtime-host

# Healer worker
railway add --service healer-worker

# Metering worker
railway add --service metering-worker

# Redis
railway add --database redis

# Or link external Postgres
railway variables set DATABASE_URL="postgresql://..."
```

### 4. Set Environment Variables

```bash
# Gateway service
railway variables set -s gateway-service \
  DATABASE_URL='${{Postgres.DATABASE_URL}}' \
  REDIS_URL='${{Redis.REDIS_URL}}' \
  JWT_SECRET='your-32-char-secret-here' \
  JWT_ISSUER='glock.dev' \
  PLANNER_SERVICE_URL='http://planner-service.railway.internal:8000' \
  HEALER_WORKER_URL='http://healer-worker.railway.internal:8000' \
  RUNTIME_HOST_URL='http://runtime-host.railway.internal:9000' \
  PLAN_SIGNING_PRIVATE_KEY='base64-encoded-private-key' \
  ANTHROPIC_API_KEY='sk-ant-...' \
  LOG_LEVEL='info'

# Runtime host
railway variables set -s runtime-host \
  REDIS_URL='${{Redis.REDIS_URL}}' \
  GATEWAY_URL='ws://gateway-service.railway.internal:8000' \
  POOL_INITIAL_SIZE='5' \
  POOL_TARGET_SIZE='20' \
  POOL_MAX_SIZE='50' \
  LOG_LEVEL='info'

# Healer worker
railway variables set -s healer-worker \
  DATABASE_URL='${{Postgres.DATABASE_URL}}' \
  REDIS_URL='${{Redis.REDIS_URL}}' \
  HEALER_MAX_RETRIES='3' \
  LOG_LEVEL='info'

# Metering worker
railway variables set -s metering-worker \
  DATABASE_URL='${{Postgres.DATABASE_URL}}' \
  REDIS_URL='${{Redis.REDIS_URL}}' \
  METERING_BATCH_SIZE='100' \
  LOG_LEVEL='info'
```

### 5. Deploy

```bash
# Deploy all services
railway up

# Check deployment status
railway status

# View logs
railway logs -s gateway-service
```

### 6. Configure Domain

```bash
# Add custom domain
railway domain add api.glock.dev -s gateway-service

# Verify SSL
curl https://api.glock.dev/health
```

## Manual Deployment

For non-Railway deployments (AWS, GCP, self-hosted).

### Docker Compose Production

Create `docker-compose.prod.yml`:

```yaml
version: '3.8'

services:
  gateway:
    build:
      context: .
      dockerfile: infra/docker/gateway.Dockerfile
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=redis://redis:6379
      - JWT_SECRET=${JWT_SECRET}
      - JWT_ISSUER=${JWT_ISSUER}
      - PLAN_SIGNING_PRIVATE_KEY=${PLAN_SIGNING_PRIVATE_KEY}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - LOG_LEVEL=info
    depends_on:
      - redis
    deploy:
      replicas: 2
      resources:
        limits:
          cpus: '1'
          memory: 1G
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  runtime-host:
    build:
      context: .
      dockerfile: infra/docker/runtime-host.Dockerfile
    environment:
      - REDIS_URL=redis://redis:6379
      - GATEWAY_URL=ws://gateway:8000
      - POOL_INITIAL_SIZE=5
      - POOL_TARGET_SIZE=20
      - POOL_MAX_SIZE=50
    depends_on:
      - redis
      - gateway
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 4G

  healer-worker:
    build:
      context: .
      dockerfile: infra/docker/gateway.Dockerfile
    command: python -m apps.server.src.healer.main
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=redis://redis:6379
      - HEALER_MAX_RETRIES=3
    depends_on:
      - redis

  metering-worker:
    build:
      context: .
      dockerfile: infra/docker/gateway.Dockerfile
    command: python -m apps.server.src.metering.main
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=redis://redis:6379
      - METERING_BATCH_SIZE=100
    depends_on:
      - redis

  redis:
    image: redis:7-alpine
    volumes:
      - redis-data:/data
    command: redis-server --appendonly yes
    deploy:
      resources:
        limits:
          memory: 512M

volumes:
  redis-data:
```

Deploy:

```bash
# Build and start
docker-compose -f docker-compose.prod.yml up -d --build

# Scale gateway
docker-compose -f docker-compose.prod.yml up -d --scale gateway=3
```

### Kubernetes Deployment

See `infra/k8s/` for Kubernetes manifests (Helm charts coming soon).

## Supabase Setup

### 1. Create Project

1. Go to [supabase.com](https://supabase.com)
2. Create new project
3. Note the connection string from Settings → Database

### 2. Run Migrations

```bash
# Set connection string
export DATABASE_URL="postgresql://postgres:[PASSWORD]@[HOST]:5432/postgres"

# Run migrations in order
psql $DATABASE_URL -f infra/supabase/migrations/0001_init.sql
psql $DATABASE_URL -f infra/supabase/migrations/0002_sessions_tasks.sql
psql $DATABASE_URL -f infra/supabase/migrations/0003_plans.sql
psql $DATABASE_URL -f infra/supabase/migrations/0004_usage_audit.sql
psql $DATABASE_URL -f infra/supabase/migrations/0005_preferences.sql
```

### 3. Configure Row Level Security (Optional)

For multi-tenant deployments:

```sql
-- Enable RLS
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;

-- Create policies
CREATE POLICY "Users can only see their sessions"
ON sessions FOR ALL
USING (user_id = auth.uid());

CREATE POLICY "Users can only see their tasks"
ON tasks FOR ALL
USING (user_id = auth.uid());
```

## Environment Variables

### Complete Reference

| Variable | Service | Required | Description |
|----------|---------|----------|-------------|
| `DATABASE_URL` | Gateway, Workers | Yes | PostgreSQL connection string |
| `REDIS_URL` | All | Yes | Redis connection string |
| `JWT_SECRET` | Gateway | Yes | JWT signing secret (32+ chars) |
| `JWT_ISSUER` | Gateway | No | JWT issuer claim |
| `PLAN_SIGNING_PRIVATE_KEY` | Gateway | Yes | Ed25519 private key (base64) |
| `ANTHROPIC_API_KEY` | Gateway | Yes* | Anthropic API key |
| `OPENAI_API_KEY` | Gateway | Yes* | OpenAI API key |
| `LITELLM_API_BASE` | Gateway | No | LiteLLM proxy URL |
| `LITELLM_MASTER_KEY` | Gateway | No | LiteLLM master key |
| `LOG_LEVEL` | All | No | `debug`, `info`, `warn`, `error` |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | Gateway | No | Default: 60 |
| `RATE_LIMIT_CONCURRENT_SESSIONS` | Gateway | No | Default: 10 |
| `WS_HEARTBEAT_INTERVAL_MS` | Gateway | No | Default: 30000 |
| `SESSION_IDLE_TIMEOUT_MS` | Gateway | No | Default: 3600000 |
| `POOL_INITIAL_SIZE` | Runtime Host | No | Default: 5 |
| `POOL_TARGET_SIZE` | Runtime Host | No | Default: 20 |
| `POOL_MAX_SIZE` | Runtime Host | No | Default: 50 |
| `HEALER_MAX_RETRIES` | Healer | No | Default: 3 |
| `METERING_BATCH_SIZE` | Metering | No | Default: 100 |

*At least one LLM provider API key is required.

## Scaling

### Horizontal Scaling

**Gateway Service:**
- Stateless, scale freely
- Use Railway's auto-scaling or K8s HPA
- Recommended: 2-4 replicas minimum

**Runtime Host:**
- Stateful (manages runtime pool)
- Scale by adding more hosts
- Each host manages its own pool

**Workers:**
- Single replica per type usually sufficient
- Scale based on queue depth

### Vertical Scaling

**Gateway:**
- CPU: 0.5-1 vCPU per replica
- Memory: 512MB-1GB per replica

**Runtime Host:**
- CPU: 1-2 vCPU (depends on pool size)
- Memory: 2-4GB (50-100MB per runtime)

**Redis:**
- Memory: 256MB-1GB (depends on session count)

### Auto-scaling Configuration (Railway)

```toml
[deploy]
numReplicas = 2

[deploy.autoscaling]
enabled = true
minReplicas = 2
maxReplicas = 10
targetCPUPercent = 70
targetMemoryPercent = 80
```

## Monitoring

### Health Endpoints

```bash
# Gateway health
GET /health
# Returns: {"status": "healthy", "version": "1.0.0"}

# Gateway readiness
GET /ready
# Returns: {"ready": true, "redis": true, "postgres": true}

# Gateway metrics
GET /metrics
# Returns: Prometheus-format metrics
```

### Recommended Metrics

1. **Request Rate** - Requests per second by endpoint
2. **Latency** - p50, p95, p99 response times
3. **Error Rate** - 4xx and 5xx responses
4. **WebSocket Connections** - Active connections
5. **Session Count** - Active sessions per gateway
6. **Runtime Pool** - Warm/busy/draining counts
7. **Queue Depth** - Healer and metering queues

### Logging

JSON-structured logs for easy parsing:

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "info",
  "service": "gateway",
  "request_id": "req_abc123",
  "session_id": "sess_xyz789",
  "message": "Task completed",
  "duration_ms": 1523
}
```

### Alerting Recommendations

| Alert | Condition | Severity |
|-------|-----------|----------|
| High Error Rate | >5% 5xx in 5min | Critical |
| High Latency | p99 >5s for 5min | Warning |
| Low Warm Pool | <3 warm runtimes | Warning |
| Redis Down | Health check failing | Critical |
| DB Connection Pool | >90% utilized | Warning |
| Memory Pressure | >85% memory used | Warning |

## Security Hardening

### Network Security

1. **TLS Everywhere**
   - Use HTTPS/WSS for all external traffic
   - Internal services can use HTTP within VPC

2. **IP Allowlisting**
   - Restrict database access to service IPs
   - Use Railway's private networking

3. **Rate Limiting**
   - Configure per-user limits
   - Implement DDoS protection (Cloudflare)

### Secret Management

```bash
# Use Railway's built-in secret management
railway variables set JWT_SECRET=$(openssl rand -hex 32) --secret

# Or use external secret manager
# AWS Secrets Manager, HashiCorp Vault, etc.
```

### Audit Logging

Enable audit logging for compliance:

```sql
-- All actions logged to audit_logs table
-- Query recent security events:
SELECT * FROM audit_logs
WHERE severity IN ('warn', 'high', 'critical')
ORDER BY created_at DESC
LIMIT 100;
```

### Security Checklist

- [ ] TLS certificates configured
- [ ] JWT secret is random 32+ characters
- [ ] Database credentials rotated
- [ ] API keys stored securely (not in code)
- [ ] Rate limiting enabled
- [ ] Audit logging enabled
- [ ] Plan signing keys generated and stored securely
- [ ] No debug logging in production
- [ ] Health endpoints don't leak sensitive info

## Rollback Procedure

### Railway

```bash
# List deployments
railway deployments

# Rollback to previous
railway rollback
```

### Docker

```bash
# Tag releases
docker tag glock-gateway:latest glock-gateway:v1.0.0

# Rollback
docker-compose -f docker-compose.prod.yml down
docker tag glock-gateway:v0.9.0 glock-gateway:latest
docker-compose -f docker-compose.prod.yml up -d
```

## Disaster Recovery

### Backup Strategy

1. **Database**: Supabase automatic backups + point-in-time recovery
2. **Redis**: Enable AOF persistence
3. **Secrets**: Store in external secret manager with backup

### Recovery Steps

1. Restore database from backup
2. Deploy services
3. Verify health endpoints
4. Run smoke tests
5. Enable traffic

## Support

For deployment issues:
- GitHub Issues: https://github.com/glock/glock/issues
- Discord: https://discord.gg/glock
- Email: support@glock.dev
