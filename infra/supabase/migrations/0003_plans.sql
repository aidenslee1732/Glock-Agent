-- Glock Database Schema - Migration 0003: Compiled Plans and Validations
-- This migration creates plan storage and validation tracking tables

-- Compiled plans
CREATE TABLE compiled_plans (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    version INT NOT NULL DEFAULT 1,
    mode TEXT NOT NULL CHECK (mode IN ('direct', 'escalated', 'retry')),
    risk_flags JSONB DEFAULT '[]',
    allowed_tools JSONB NOT NULL DEFAULT '[]',
    workspace_scope TEXT,
    edit_scope JSONB DEFAULT '[]',
    validation_steps JSONB DEFAULT '[]',
    approval_requirements JSONB DEFAULT '{}',
    budgets JSONB DEFAULT '{}',
    plan_payload JSONB NOT NULL,
    plan_signature TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    superseded_by UUID REFERENCES compiled_plans(id)
);

CREATE INDEX idx_plans_task ON compiled_plans(task_id, version DESC);
CREATE INDEX idx_plans_session ON compiled_plans(session_id, created_at DESC);
CREATE INDEX idx_plans_expires ON compiled_plans(expires_at);

-- Add foreign key from tasks to compiled_plans
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_plan
    FOREIGN KEY (compiled_plan_id) REFERENCES compiled_plans(id) ON DELETE SET NULL;

-- Add foreign key from task_attempts to compiled_plans
ALTER TABLE task_attempts ADD CONSTRAINT fk_attempts_plan
    FOREIGN KEY (plan_id) REFERENCES compiled_plans(id) ON DELETE SET NULL;

-- Task validations
CREATE TABLE task_validations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_no INT NOT NULL,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'skipped', 'error', 'timeout')),
    tool_name TEXT,
    command_summary TEXT,
    result_summary TEXT,
    failures JSONB DEFAULT '[]',
    raw_result_ref TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_validations_task ON task_validations(task_id, attempt_no);
CREATE INDEX idx_validations_status ON task_validations(status, created_at);
