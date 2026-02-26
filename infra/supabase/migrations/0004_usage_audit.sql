-- Glock Database Schema - Migration 0004: Usage Events and Audit Logs
-- This migration creates metering and audit tracking tables

-- Usage events (raw, can be TTL'd or archived)
CREATE TABLE usage_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id UUID REFERENCES sessions(id) ON DELETE SET NULL,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    unit TEXT NOT NULL CHECK (unit IN ('count', 'tokens', 'seconds', 'bytes', 'validations')),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_usage_user ON usage_events(user_id, created_at DESC);
CREATE INDEX idx_usage_org ON usage_events(org_id, created_at DESC);
CREATE INDEX idx_usage_type ON usage_events(event_type, created_at);
CREATE INDEX idx_usage_created ON usage_events(created_at);

-- Usage rollups (hourly aggregates)
CREATE TABLE usage_rollups_hourly (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    bucket_hour TIMESTAMPTZ NOT NULL,
    org_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    metric TEXT NOT NULL,
    value NUMERIC NOT NULL,
    dimensions JSONB DEFAULT '{}',
    dimensions_hash TEXT GENERATED ALWAYS AS (md5(dimensions::text)) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(bucket_hour, org_id, user_id, metric, dimensions_hash)
);

CREATE INDEX idx_rollups_user ON usage_rollups_hourly(user_id, bucket_hour DESC);
CREATE INDEX idx_rollups_org ON usage_rollups_hourly(org_id, bucket_hour DESC);
CREATE INDEX idx_rollups_metric ON usage_rollups_hourly(metric, bucket_hour DESC);

-- Audit logs
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id UUID REFERENCES sessions(id) ON DELETE SET NULL,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'system', 'healer', 'runtime')),
    actor_id TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info', 'warn', 'high', 'critical')),
    details JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_user ON audit_logs(user_id, created_at DESC);
CREATE INDEX idx_audit_org ON audit_logs(org_id, created_at DESC);
CREATE INDEX idx_audit_action ON audit_logs(action, created_at);
CREATE INDEX idx_audit_severity ON audit_logs(severity, created_at);

-- Session checkpoints for crash recovery
CREATE TABLE session_checkpoints (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    attempt_no INT,
    checkpoint_type TEXT NOT NULL CHECK (checkpoint_type IN (
        'runtime_state', 'tool_queue', 'plan_progress',
        'validation_progress', 'conversation'
    )),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_checkpoints_session ON session_checkpoints(session_id, created_at DESC);
