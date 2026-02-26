-- Glock Database Schema - Migration 0005: Preferences and Auth
-- This migration creates user preferences and auth token tables

-- User preferences (learned + explicit)
CREATE TABLE user_preferences (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE UNIQUE,
    org_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    prefs JSONB DEFAULT '{}',
    confidence JSONB DEFAULT '{}',
    sources_count JSONB DEFAULT '{}',
    learning_enabled BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_prefs_user ON user_preferences(user_id);

-- Preference observations (for traceability)
CREATE TABLE preference_observations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id UUID REFERENCES tasks(id) ON DELETE SET NULL,
    observation_type TEXT NOT NULL,
    signal_strength NUMERIC NOT NULL DEFAULT 1.0,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pref_obs_user ON preference_observations(user_id, created_at DESC);
CREATE INDEX idx_pref_obs_type ON preference_observations(observation_type, created_at);

-- Refresh tokens (for self-managed auth)
CREATE TABLE refresh_tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    device_label TEXT,
    client_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

CREATE INDEX idx_tokens_user ON refresh_tokens(user_id, created_at DESC);
CREATE INDEX idx_tokens_hash ON refresh_tokens(token_hash);

CREATE TRIGGER user_preferences_updated_at BEFORE UPDATE ON user_preferences
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
