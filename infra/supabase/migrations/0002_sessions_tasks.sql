-- Glock Database Schema - Migration 0002: Sessions and Tasks
-- This migration creates session and task management tables

-- Sessions table
CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle' CHECK (status IN (
        'idle', 'running', 'paused', 'disconnected', 'ended'
    )),
    workspace_label TEXT,
    repo_fingerprint TEXT,
    repo_root_hint TEXT,
    branch_name TEXT,
    active_task_id UUID,
    last_client_seq_acked BIGINT NOT NULL DEFAULT 0,
    last_server_seq_sent BIGINT NOT NULL DEFAULT 0,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ
);

CREATE INDEX idx_sessions_user_status ON sessions(user_id, status);
CREATE INDEX idx_sessions_org_user ON sessions(org_id, user_id, created_at DESC);
CREATE INDEX idx_sessions_repo ON sessions(repo_fingerprint);
CREATE INDEX idx_sessions_last_seen ON sessions(last_seen_at);

-- Tasks table
CREATE TABLE tasks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'waiting_approval', 'validating',
        'retrying', 'completed', 'failed', 'cancelled'
    )),
    task_type TEXT CHECK (task_type IN (
        'implement', 'debug', 'refactor', 'security', 'deploy',
        'question', 'review', 'test', 'other'
    )),
    complexity TEXT CHECK (complexity IN ('trivial', 'simple', 'moderate', 'complex', 'critical')),
    risk_level TEXT CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    risk_flags JSONB DEFAULT '[]',
    user_prompt TEXT NOT NULL,
    compiled_plan_id UUID,
    retry_count INT NOT NULL DEFAULT 0,
    max_retries INT NOT NULL DEFAULT 2,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    failure_reason TEXT,
    summary TEXT
);

CREATE INDEX idx_tasks_session ON tasks(session_id, created_at DESC);
CREATE INDEX idx_tasks_user_status ON tasks(user_id, status);
CREATE INDEX idx_tasks_status ON tasks(status, created_at);
CREATE INDEX idx_tasks_risk_flags ON tasks USING GIN(risk_flags);

-- Add foreign key for active_task_id after tasks table exists
ALTER TABLE sessions ADD CONSTRAINT fk_sessions_active_task
    FOREIGN KEY (active_task_id) REFERENCES tasks(id) ON DELETE SET NULL;

-- Task attempts (tracks each retry)
CREATE TABLE task_attempts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_no INT NOT NULL,
    plan_id UUID,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    trigger TEXT NOT NULL CHECK (trigger IN ('initial', 'healer_retry', 'manual_retry')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    failure_class TEXT,
    UNIQUE(task_id, attempt_no)
);

CREATE INDEX idx_task_attempts_task ON task_attempts(task_id, attempt_no DESC);

CREATE TRIGGER sessions_updated_at BEFORE UPDATE ON sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER tasks_updated_at BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
