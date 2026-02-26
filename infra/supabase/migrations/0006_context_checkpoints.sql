-- Model B: Context Checkpoints
-- Encrypted context storage for client-orchestrated architecture

-- Context checkpoints table
-- Stores encrypted conversation context for efficient resume and delta transfers
CREATE TABLE IF NOT EXISTS context_checkpoints (
    id TEXT PRIMARY KEY,                          -- "cp_..." format
    session_id TEXT NOT NULL,                     -- Session this checkpoint belongs to
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES context_checkpoints(id) ON DELETE SET NULL,

    -- Encryption
    enc_alg TEXT NOT NULL DEFAULT 'aes-256-gcm',  -- Encryption algorithm
    nonce_base64 TEXT NOT NULL,                   -- Base64-encoded nonce
    ciphertext_base64 TEXT NOT NULL,              -- Base64-encoded encrypted payload

    -- Metadata (not encrypted)
    payload_hash TEXT NOT NULL,                   -- SHA-256 hash for verification
    token_count INT NOT NULL DEFAULT 0,           -- Token count at this checkpoint
    turn_count INT NOT NULL DEFAULT 0,            -- Conversation turn count
    is_full BOOLEAN NOT NULL DEFAULT false,       -- Full snapshot vs delta

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,

    -- Constraints
    CONSTRAINT valid_token_count CHECK (token_count >= 0),
    CONSTRAINT valid_turn_count CHECK (turn_count >= 0)
);

-- Index for session lookups (most common query pattern)
CREATE INDEX idx_context_checkpoints_session
    ON context_checkpoints(session_id, created_at DESC);

-- Index for user lookups (for user data management)
CREATE INDEX idx_context_checkpoints_user
    ON context_checkpoints(user_id, created_at DESC);

-- Index for expiration cleanup
CREATE INDEX idx_context_checkpoints_expires
    ON context_checkpoints(expires_at)
    WHERE expires_at IS NOT NULL;

-- Index for finding full snapshots
CREATE INDEX idx_context_checkpoints_full
    ON context_checkpoints(session_id, created_at DESC)
    WHERE is_full = true;

-- Index for parent chain traversal
CREATE INDEX idx_context_checkpoints_parent
    ON context_checkpoints(parent_id)
    WHERE parent_id IS NOT NULL;

-- Enable Row Level Security
ALTER TABLE context_checkpoints ENABLE ROW LEVEL SECURITY;

-- Policy: Users can only access their own checkpoints
CREATE POLICY context_checkpoints_user_isolation ON context_checkpoints
    FOR ALL
    USING (user_id = auth.uid());

-- Comment on table
COMMENT ON TABLE context_checkpoints IS
    'Model B: Encrypted context checkpoints for client-orchestrated sessions. '
    'Each checkpoint stores encrypted conversation state with delta chain support.';

-- Comments on columns
COMMENT ON COLUMN context_checkpoints.id IS 'Checkpoint ID (cp_xxx format)';
COMMENT ON COLUMN context_checkpoints.session_id IS 'Session this checkpoint belongs to';
COMMENT ON COLUMN context_checkpoints.parent_id IS 'Parent checkpoint for delta chains (NULL for full snapshots)';
COMMENT ON COLUMN context_checkpoints.enc_alg IS 'Encryption algorithm (default: aes-256-gcm)';
COMMENT ON COLUMN context_checkpoints.nonce_base64 IS 'Base64-encoded encryption nonce';
COMMENT ON COLUMN context_checkpoints.ciphertext_base64 IS 'Base64-encoded encrypted payload';
COMMENT ON COLUMN context_checkpoints.payload_hash IS 'SHA-256 hash of decrypted payload for verification';
COMMENT ON COLUMN context_checkpoints.token_count IS 'Estimated token count at this checkpoint';
COMMENT ON COLUMN context_checkpoints.turn_count IS 'Number of conversation turns at this checkpoint';
COMMENT ON COLUMN context_checkpoints.is_full IS 'True if this is a full snapshot, false for delta';
COMMENT ON COLUMN context_checkpoints.expires_at IS 'Checkpoint expiration time (default: 24 hours)';

-- Function to clean up expired checkpoints
CREATE OR REPLACE FUNCTION cleanup_expired_checkpoints()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    WITH deleted AS (
        DELETE FROM context_checkpoints
        WHERE expires_at < NOW()
        RETURNING id
    )
    SELECT COUNT(*) INTO deleted_count FROM deleted;

    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- Comment on function
COMMENT ON FUNCTION cleanup_expired_checkpoints IS
    'Removes expired context checkpoints. Call periodically via cron or scheduled job.';
