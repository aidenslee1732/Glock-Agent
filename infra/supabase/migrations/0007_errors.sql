-- Migration: Create errors table for centralized error tracking
-- This table stores all errors for debugging and monitoring

CREATE TABLE IF NOT EXISTS errors (
    id TEXT PRIMARY KEY,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    stack_trace TEXT,
    severity TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('critical', 'error', 'warning')),
    component TEXT,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    session_id UUID REFERENCES sessions(id) ON DELETE SET NULL,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    request_id TEXT,
    context JSONB DEFAULT '{}',
    resolved BOOLEAN DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,
    resolved_by UUID REFERENCES users(id) ON DELETE SET NULL,
    resolution_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_errors_created_at ON errors(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_errors_severity ON errors(severity);
CREATE INDEX IF NOT EXISTS idx_errors_component ON errors(component);
CREATE INDEX IF NOT EXISTS idx_errors_user_id ON errors(user_id);
CREATE INDEX IF NOT EXISTS idx_errors_session_id ON errors(session_id);
CREATE INDEX IF NOT EXISTS idx_errors_error_type ON errors(error_type);
CREATE INDEX IF NOT EXISTS idx_errors_resolved ON errors(resolved);

-- Composite index for common queries
CREATE INDEX IF NOT EXISTS idx_errors_severity_component ON errors(severity, component, created_at DESC);

-- Enable RLS
ALTER TABLE errors ENABLE ROW LEVEL SECURITY;

-- Policy: Only admins can view errors (for security - errors may contain sensitive info)
CREATE POLICY "Admins can view errors"
    ON errors FOR SELECT
    TO authenticated
    USING (
        EXISTS (
            SELECT 1 FROM users
            WHERE users.id = auth.uid()
            AND users.role = 'admin'
        )
    );

-- Policy: System can insert errors (no auth required for error logging)
CREATE POLICY "System can insert errors"
    ON errors FOR INSERT
    WITH CHECK (true);

-- Add comment
COMMENT ON TABLE errors IS 'Centralized error tracking table for debugging and monitoring';
COMMENT ON COLUMN errors.severity IS 'Error severity: critical, error, or warning';
COMMENT ON COLUMN errors.component IS 'Component where the error occurred (e.g., hooks, llm_handler)';
COMMENT ON COLUMN errors.context IS 'Additional context as JSON (request params, environment, etc.)';
