# Glock Deployment Guide

Complete guide for deploying, configuring, and maintaining Glock in production.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Environment Variables & Keys](#environment-variables--keys)
- [Deployment Options](#deployment-options)
  - [Option 1: Docker Compose (Recommended for Small Teams)](#option-1-docker-compose)
  - [Option 2: Railway (Managed Platform)](#option-2-railway)
  - [Option 3: Manual Deployment (AWS/GCP/Self-hosted)](#option-3-manual-deployment)
- [Database Setup](#database-setup)
- [Post-Deployment Verification](#post-deployment-verification)
- [Maintenance Guide](#maintenance-guide)
- [Monitoring & Alerting](#monitoring--alerting)
- [Security Hardening](#security-hardening)
- [Backup & Recovery](#backup--recovery)
- [Troubleshooting](#troubleshooting)

---

## Architecture Overview

Glock uses a **client-orchestrated architecture** (Model B):

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         USER'S MACHINE                                   │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Glock CLI                                                         │  │
│  │   ├─ OrchestrationEngine (runs the full agent loop)               │  │
│  │   ├─ ToolBroker (executes tools locally: read/edit/bash/git)      │  │
│  │   ├─ ContextPacker (40-60% token reduction)                       │  │
│  │   └─ SessionKeyManager (per-session encryption)                   │  │
│  └────────────────────────────────┬──────────────────────────────────┘  │
└───────────────────────────────────┼─────────────────────────────────────┘
                                    │ WebSocket (JWT authenticated)
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         CONTROL PLANE (Your Server)                      │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  Gateway Service (stateless, horizontally scalable)              │    │
│  │   ├─ JWT Authentication                                          │    │
│  │   ├─ LLM Proxy (Anthropic Claude, OpenAI, Google)                │    │
│  │   ├─ Checkpoint Storage (encrypted context snapshots)            │    │
│  │   ├─ Rate Limiting & Metering                                    │    │
│  │   └─ Session Management                                          │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│            │                          │                                  │
│            ▼                          ▼                                  │
│        Redis                     PostgreSQL                              │
│   (sessions, routing,       (users, checkpoints,                        │
│    rate limiting)            usage, audit logs)                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Key Benefits:**
- Server is stateless → easy horizontal scaling
- No per-session server processes → cost efficient
- Tools run locally → secure, no code exposure to server
- Checkpoints → session pause/resume support

---

## Prerequisites

Before deployment, ensure you have:

1. **Domain** (optional but recommended)
   - For production: `api.yourdomain.com`
   - SSL certificate (auto-provisioned on Railway/most platforms)

2. **LLM API Keys** (at least one required)
   - [Anthropic API Key](https://console.anthropic.com/) - Recommended
   - [OpenAI API Key](https://platform.openai.com/) - Optional
   - [Google AI API Key](https://makersuite.google.com/) - Optional

3. **Infrastructure Requirements**
   - PostgreSQL 15+ (or Supabase)
   - Redis 7+
   - Docker (for containerized deployment)

---

## Environment Variables & Keys

### Complete Reference

| Variable | Required | Description | How to Generate |
|----------|----------|-------------|-----------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string | From your database provider |
| `REDIS_URL` | Yes | Redis connection string | From your Redis provider |
| `JWT_SECRET` | Yes | Secret for signing JWT tokens (32+ chars) | `openssl rand -hex 32` |
| `JWT_ISSUER` | No | JWT issuer claim | Default: `glock.dev` |
| `CONTEXT_MASTER_KEY` | Yes | Master key for context encryption (64 hex chars) | `openssl rand -hex 32` |
| `ANTHROPIC_API_KEY` | Yes* | Anthropic Claude API key | From Anthropic Console |
| `OPENAI_API_KEY` | No | OpenAI API key | From OpenAI Platform |
| `GOOGLE_API_KEY` | No | Google AI API key | From Google AI Studio |
| `LOG_LEVEL` | No | Logging level | `debug`, `info`, `warn`, `error` |
| `PLAN_SIGNING_PRIVATE_KEY` | No | Ed25519 key for plan signing | See below |

*At least one LLM provider API key is required.

### Generating Required Keys

#### 1. JWT Secret (Required)
```bash
# Generate a secure 32-byte hex string
openssl rand -hex 32

# Example output: a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6
```

#### 2. Context Master Key (Required)
```bash
# Generate a 32-byte (64 hex char) encryption key
openssl rand -hex 32

# Example output: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
```

#### 3. Plan Signing Keys (Optional - for signed execution plans)
```bash
# Generate Ed25519 key pair
python3 -c "
from nacl.signing import SigningKey
from nacl.encoding import Base64Encoder

key = SigningKey.generate()
print('PLAN_SIGNING_PRIVATE_KEY=' + key.encode(encoder=Base64Encoder).decode())
print('PLAN_SIGNING_PUBLIC_KEY=' + key.verify_key.encode(encoder=Base64Encoder).decode())
"
```

### Sample .env File

```bash
# =============================================================================
# DATABASE (Required)
# =============================================================================
DATABASE_URL=postgresql://glock:your_password@localhost:5432/glock

# =============================================================================
# REDIS (Required)
# =============================================================================
REDIS_URL=redis://localhost:6379

# =============================================================================
# AUTHENTICATION (Required)
# =============================================================================
JWT_SECRET=your-generated-32-char-secret-here
JWT_ISSUER=glock.dev

# Access token expiry (minutes)
ACCESS_TOKEN_EXPIRE_MINUTES=60

# Refresh token expiry (days)
REFRESH_TOKEN_EXPIRE_DAYS=30

# =============================================================================
# ENCRYPTION (Required)
# =============================================================================
CONTEXT_MASTER_KEY=your-64-character-hex-key-here

# =============================================================================
# LLM PROVIDERS (At least one required)
# =============================================================================
ANTHROPIC_API_KEY=sk-ant-api03-...
# OPENAI_API_KEY=sk-...
# GOOGLE_API_KEY=...

# =============================================================================
# OPTIONAL SETTINGS
# =============================================================================
LOG_LEVEL=info

# Rate limiting
RATE_LIMIT_REQUESTS_PER_MINUTE=60
RATE_LIMIT_CONCURRENT_SESSIONS=10

# Session timeouts
WS_HEARTBEAT_INTERVAL_MS=30000
SESSION_IDLE_TIMEOUT_MS=3600000
```

---

## Deployment Options

### Option 1: Docker Compose

Best for: Small teams, self-hosted, development/staging environments.

#### Step 1: Download and Configure

```bash
# Download the latest release
curl -LO https://releases.glock.dev/latest/glock-server.tar.gz
tar -xzf glock-server.tar.gz
cd glock-server

# Or download from the releases page and extract manually

# Copy and configure environment
cp .env.example .env

# Edit .env with your values
nano .env
```

#### Step 2: Generate Required Keys

```bash
# Generate JWT_SECRET
echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env

# Generate CONTEXT_MASTER_KEY
echo "CONTEXT_MASTER_KEY=$(openssl rand -hex 32)" >> .env
```

#### Step 3: Start Services

```bash
# Build and start all services
docker-compose up -d --build

# Check status
docker-compose ps

# View logs
docker-compose logs -f gateway
```

#### Step 4: Verify Deployment

```bash
# Health check
curl http://localhost:8000/health
# Expected: {"status": "healthy", "gateway_id": "gw_xxx", "dev_mode": false}

# Readiness check
curl http://localhost:8000/ready
# Expected: {"status": "ready", "gateway_id": "gw_xxx"}
```

#### Docker Compose for Production

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
      - JWT_ISSUER=${JWT_ISSUER:-glock.dev}
      - CONTEXT_MASTER_KEY=${CONTEXT_MASTER_KEY}
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
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    volumes:
      - redis-data:/data
    command: redis-server --appendonly yes
    deploy:
      resources:
        limits:
          memory: 512M
    restart: unless-stopped

volumes:
  redis-data:
```

```bash
# Deploy production
docker-compose -f docker-compose.prod.yml up -d --build

# Scale gateway
docker-compose -f docker-compose.prod.yml up -d --scale gateway=3
```

---

### Option 2: Railway

Best for: Quick deployment, auto-scaling, managed infrastructure.

#### Step 1: Install Railway CLI

```bash
npm install -g @railway/cli
railway login
```

#### Step 2: Create Project

```bash
# Initialize project
railway init

# Create from Docker image
railway add --docker glock/glock-gateway:latest
```

#### Step 3: Add Services

```bash
# Add Redis
railway add --database redis

# Add PostgreSQL (or use external like Supabase)
railway add --database postgres
```

#### Step 4: Configure Environment Variables

```bash
# Set all required variables
railway variables set \
  DATABASE_URL='${{Postgres.DATABASE_URL}}' \
  REDIS_URL='${{Redis.REDIS_URL}}' \
  JWT_SECRET='your-32-char-secret' \
  JWT_ISSUER='glock.dev' \
  CONTEXT_MASTER_KEY='your-64-char-hex-key' \
  ANTHROPIC_API_KEY='sk-ant-...' \
  LOG_LEVEL='info'
```

#### Step 5: Deploy

```bash
# Deploy
railway up

# Check status
railway status

# View logs
railway logs
```

#### Step 6: Add Custom Domain

```bash
# Add your domain
railway domain add api.yourdomain.com

# Verify SSL
curl https://api.yourdomain.com/health
```

---

### Option 3: Manual Deployment

Best for: AWS, GCP, Azure, or custom infrastructure.

#### Kubernetes Deployment

```yaml
# gateway-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: glock-gateway
spec:
  replicas: 2
  selector:
    matchLabels:
      app: glock-gateway
  template:
    metadata:
      labels:
        app: glock-gateway
    spec:
      containers:
      - name: gateway
        image: your-registry/glock-gateway:latest
        ports:
        - containerPort: 8000
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: glock-secrets
              key: database-url
        - name: REDIS_URL
          valueFrom:
            secretKeyRef:
              name: glock-secrets
              key: redis-url
        - name: JWT_SECRET
          valueFrom:
            secretKeyRef:
              name: glock-secrets
              key: jwt-secret
        - name: CONTEXT_MASTER_KEY
          valueFrom:
            secretKeyRef:
              name: glock-secrets
              key: context-master-key
        - name: ANTHROPIC_API_KEY
          valueFrom:
            secretKeyRef:
              name: glock-secrets
              key: anthropic-api-key
        resources:
          requests:
            cpu: "500m"
            memory: "512Mi"
          limits:
            cpu: "1000m"
            memory: "1Gi"
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 10
```

---

## Database Setup

### Using Supabase (Recommended)

1. Create a project at [supabase.com](https://supabase.com)
2. Get the connection string from Settings → Database
3. Run migrations:

```bash
export DATABASE_URL="postgresql://postgres:[PASSWORD]@[HOST]:5432/postgres"

# Run all migrations in order
for f in infra/supabase/migrations/*.sql; do
  echo "Running $f..."
  psql "$DATABASE_URL" -f "$f"
done
```

### Using Self-hosted PostgreSQL

```bash
# Create database
createdb glock

# Set connection string
export DATABASE_URL="postgresql://glock:password@localhost:5432/glock"

# Run migrations
psql "$DATABASE_URL" -f infra/supabase/migrations/0001_init.sql
psql "$DATABASE_URL" -f infra/supabase/migrations/0002_sessions_tasks.sql
psql "$DATABASE_URL" -f infra/supabase/migrations/0003_plans.sql
psql "$DATABASE_URL" -f infra/supabase/migrations/0004_usage_audit.sql
psql "$DATABASE_URL" -f infra/supabase/migrations/0005_preferences.sql
psql "$DATABASE_URL" -f infra/supabase/migrations/0006_context_checkpoints.sql
```

### Database Schema Overview

| Table | Purpose |
|-------|---------|
| `users` | User accounts and plan tiers |
| `organizations` | Team/enterprise organizations |
| `organization_memberships` | User-org relationships |
| `sessions` | Active and historical sessions |
| `tasks` | Task execution records |
| `context_checkpoints` | Encrypted conversation snapshots |
| `usage_events` | Token usage and metering |
| `audit_logs` | Security and compliance logging |

---

## Post-Deployment Verification

### Health Check Endpoints

```bash
# Basic health
curl https://api.yourdomain.com/health
# Expected: {"status": "healthy", "gateway_id": "gw_xxx"}

# Readiness (checks Redis + DB)
curl https://api.yourdomain.com/ready
# Expected: {"status": "ready", "gateway_id": "gw_xxx"}
```

### Verify WebSocket Connection

```bash
# Using websocat
websocat "wss://api.yourdomain.com/ws/client?token=YOUR_JWT_TOKEN"
```

### Create Test User

```sql
-- In your database
INSERT INTO users (email, name, plan_tier, status)
VALUES ('test@example.com', 'Test User', 'pro', 'active');
```

---

## Maintenance Guide

### Routine Maintenance Tasks

#### Daily
- [ ] Check health endpoints
- [ ] Review error logs for anomalies
- [ ] Verify Redis memory usage

#### Weekly
- [ ] Clean up expired checkpoints
- [ ] Review and archive old sessions
- [ ] Check database connection pool health

#### Monthly
- [ ] Rotate JWT secrets (optional, requires user re-auth)
- [ ] Review and update rate limits
- [ ] Analyze usage patterns
- [ ] Update dependencies

### Cleanup Commands

```bash
# Clean expired checkpoints (run via cron)
psql "$DATABASE_URL" -c "SELECT cleanup_expired_checkpoints();"

# Clean old sessions (older than 30 days)
psql "$DATABASE_URL" -c "
  UPDATE sessions
  SET status = 'archived'
  WHERE status = 'ended'
    AND updated_at < NOW() - INTERVAL '30 days';
"

# Redis cleanup (handled automatically with TTL, but can force)
redis-cli KEYS "sess:*" | head -100  # Preview
```

### Log Rotation

Logs are JSON-structured for easy parsing:

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "info",
  "service": "gateway",
  "session_id": "sess_xyz789",
  "message": "LLM request completed",
  "tokens_used": 1523
}
```

Configure log rotation in your deployment:

```bash
# Docker: logs are rotated by Docker daemon
# Configure in daemon.json:
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "3"
  }
}
```

### Updating the Service

```bash
# Docker Compose - pull latest image and restart
docker-compose pull
docker-compose up -d

# Railway - update to new image version
railway service update --image glock/glock-gateway:v1.2.0

# Kubernetes
kubectl set image deployment/glock-gateway gateway=glock/glock-gateway:v1.2.0
kubectl rollout status deployment/glock-gateway
```

### Rollback Procedure

```bash
# Docker
docker-compose down
docker tag glock-gateway:previous glock-gateway:latest
docker-compose up -d

# Railway
railway rollback

# Kubernetes
kubectl rollout undo deployment/glock-gateway
```

---

## Monitoring & Alerting

### Key Metrics to Monitor

| Metric | Warning | Critical | Description |
|--------|---------|----------|-------------|
| HTTP 5xx Rate | >1% | >5% | Server errors |
| Response Latency p99 | >3s | >10s | Slow requests |
| WebSocket Connections | >80% capacity | >95% | Connection saturation |
| Redis Memory | >70% | >90% | Cache pressure |
| Database Connections | >80% pool | >95% pool | Connection exhaustion |
| LLM Error Rate | >5% | >20% | Provider issues |

### Prometheus Metrics (if enabled)

```bash
# Scrape endpoint
curl http://localhost:8000/metrics
```

### Recommended Alerts

```yaml
# Prometheus AlertManager rules
groups:
  - name: glock
    rules:
      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) > 0.05
        for: 5m
        labels:
          severity: critical

      - alert: HighLatency
        expr: histogram_quantile(0.99, http_request_duration_seconds_bucket) > 5
        for: 5m
        labels:
          severity: warning

      - alert: RedisDown
        expr: redis_up == 0
        for: 1m
        labels:
          severity: critical
```

---

## Security Hardening

### Checklist

- [ ] **TLS Everywhere** - HTTPS/WSS for all external traffic
- [ ] **Strong Secrets** - All keys are randomly generated, 32+ bytes
- [ ] **Secret Rotation** - Plan for periodic key rotation
- [ ] **Rate Limiting** - Enabled and tuned
- [ ] **Authentication** - JWT tokens with short expiry
- [ ] **Authorization** - Session ownership verification
- [ ] **Audit Logging** - All security events logged
- [ ] **Network Isolation** - Database/Redis not publicly accessible
- [ ] **Dependency Updates** - Regular security patches

### JWT Security

```python
# Token configuration (in auth.py)
ACCESS_TOKEN_EXPIRE_MINUTES = 60      # Short-lived access tokens
REFRESH_TOKEN_EXPIRE_DAYS = 30        # Longer refresh tokens
JWT_ALGORITHM = "HS256"               # HMAC-SHA256
```

### Rate Limiting

Default limits (configurable):
- 60 requests per minute per user
- 10 concurrent sessions per user
- 100 LLM requests per minute per user

### Network Security

```bash
# Ensure Redis is not publicly accessible
redis-cli CONFIG GET bind
# Should return: 127.0.0.1 or internal network only

# Ensure PostgreSQL requires SSL in production
# In DATABASE_URL: ?sslmode=require
```

---

## Backup & Recovery

### Backup Strategy

| Component | Frequency | Retention | Method |
|-----------|-----------|-----------|--------|
| PostgreSQL | Daily | 30 days | pg_dump / Supabase automatic |
| Redis | Hourly | 7 days | RDB snapshots |
| Secrets | On change | Versioned | Secret manager |

### PostgreSQL Backup

```bash
# Manual backup
pg_dump "$DATABASE_URL" > backup_$(date +%Y%m%d).sql

# Restore
psql "$DATABASE_URL" < backup_20240115.sql
```

### Redis Backup

```bash
# Trigger RDB save
redis-cli BGSAVE

# Copy RDB file
cp /var/lib/redis/dump.rdb /backups/redis_$(date +%Y%m%d).rdb
```

### Disaster Recovery Steps

1. **Restore Database**
   ```bash
   psql "$DATABASE_URL" < latest_backup.sql
   ```

2. **Deploy Services**
   ```bash
   docker-compose up -d
   ```

3. **Verify Health**
   ```bash
   curl http://localhost:8000/health
   curl http://localhost:8000/ready
   ```

4. **Run Smoke Tests**
   ```bash
   # Test WebSocket connection
   # Test user authentication
   # Test LLM proxy
   ```

5. **Enable Traffic**
   - Update DNS/Load balancer
   - Monitor for errors

---

## Troubleshooting

### Common Issues

#### "Authentication failed" on WebSocket
```bash
# Check JWT_SECRET matches between client config and server
# Verify token hasn't expired
# Check LOG_LEVEL=debug for detailed auth errors
```

#### "Redis connection refused"
```bash
# Check Redis is running
redis-cli ping

# Check REDIS_URL format
# redis://[:password@]host:port[/db]
```

#### "Database connection failed"
```bash
# Test connection
psql "$DATABASE_URL" -c "SELECT 1"

# Check SSL mode for cloud databases
# ?sslmode=require
```

#### "LLM request failed"
```bash
# Check API key is valid
curl https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01"

# Check rate limits haven't been exceeded
```

#### High Memory Usage
```bash
# Check Redis memory
redis-cli INFO memory

# Check for checkpoint accumulation
psql "$DATABASE_URL" -c "
  SELECT COUNT(*),
         SUM(LENGTH(ciphertext_base64)) as total_size
  FROM context_checkpoints;
"

# Run cleanup
psql "$DATABASE_URL" -c "SELECT cleanup_expired_checkpoints();"
```

### Debug Mode

```bash
# Enable debug logging
LOG_LEVEL=debug docker-compose up

# Or for specific container
docker-compose exec gateway env LOG_LEVEL=debug uvicorn ...
```

### Getting Help

- **Bug Reports**: bugs@glock.dev
- **Documentation**: https://docs.glock.dev
- **Email**: support@glock.dev

---

## Appendix: Quick Reference

### Essential Commands

```bash
# Start services
docker-compose up -d

# View logs
docker-compose logs -f gateway

# Check health
curl http://localhost:8000/health

# Connect to database
psql "$DATABASE_URL"

# Connect to Redis
redis-cli -u "$REDIS_URL"

# Run migrations
psql "$DATABASE_URL" -f infra/supabase/migrations/XXXX.sql

# Clean expired data
psql "$DATABASE_URL" -c "SELECT cleanup_expired_checkpoints();"
```

### Environment Variable Quick Reference

```bash
# Required
DATABASE_URL=postgresql://user:pass@host:5432/db
REDIS_URL=redis://host:6379
JWT_SECRET=$(openssl rand -hex 32)
CONTEXT_MASTER_KEY=$(openssl rand -hex 32)
ANTHROPIC_API_KEY=sk-ant-...

# Optional
LOG_LEVEL=info
JWT_ISSUER=glock.dev
ACCESS_TOKEN_EXPIRE_MINUTES=60
REFRESH_TOKEN_EXPIRE_DAYS=30
```
