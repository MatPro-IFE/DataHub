-- SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
--
-- SPDX-License-Identifier: Apache-2.0

-- DataHub migration 001: project groups and time-limited sharing links.
-- Idempotent: safe to run on every start (apply-schema.sh runs all migrations in order).
SET client_min_messages = warning;

-- ── project groups ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS datahub_groups (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name         TEXT NOT NULL UNIQUE CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{1,62}$'),
    description  TEXT,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id    UUID NOT NULL REFERENCES datahub_groups(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES datahub_users(id) ON DELETE CASCADE,
    is_manager  BOOLEAN NOT NULL DEFAULT FALSE,   -- managers add and remove members
    added_by    TEXT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_gm_user ON group_members (user_id);

-- datasets restricted to groups (by group name); empty = no group restriction
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS allowed_groups TEXT[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_ds_groups ON datasets USING GIN (allowed_groups);

-- ── sharing links ────────────────────────────────────────────────
-- Only the SHA-256 of the token is stored; the token itself is shown once.
CREATE TABLE IF NOT EXISTS share_links (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    token_hash      TEXT NOT NULL UNIQUE,
    dataset_id      UUID NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    max_downloads   INTEGER CHECK (max_downloads IS NULL OR max_downloads > 0),
    download_count  INTEGER NOT NULL DEFAULT 0,
    hidden_classes  TEXT[] NOT NULL DEFAULT '{}',  -- file classes hidden from the creator stay hidden
    note            TEXT,
    revoked_at      TIMESTAMPTZ,
    revoked_by      TEXT,
    last_used_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_share_ds ON share_links (dataset_id);

ALTER TABLE download_logs ADD COLUMN IF NOT EXISTS share_link_id UUID;

-- ── privileges for the new tables ────────────────────────────────
ALTER TABLE datahub_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_members  ENABLE ROW LEVEL SECURITY;
ALTER TABLE share_links    ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON datahub_groups, group_members, share_links FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT ALL ON datahub_groups, group_members, share_links TO service_role;
    END IF;
END $$;

CREATE OR REPLACE VIEW dataset_summary AS
SELECT d.id, d.name, d.dataset_type, d.status, d.tags, d.allowed_orgs, d.uploaded_by,
       d.created_at, d.started_at, d.finished_at, d.file_count, d.total_bytes,
       (SELECT count(*) FROM dataset_parts p WHERE p.dataset_id = d.id) AS part_count,
       (SELECT jsonb_object_agg(q.name, q.value) FROM quantities q
         WHERE q.dataset_id = d.id AND q.part_id IS NULL) AS quantities,
       d.allowed_groups
FROM datasets d;
