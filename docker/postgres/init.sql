-- SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
--
-- SPDX-License-Identifier: Apache-2.0

-- ============================================================
-- DataHub - PostgreSQL schema (generic core)
--
-- The relational schema is project independent. Everything that is
-- specific to a project (dataset types, parameters, file classes,
-- quantities of interest) is defined in config/schema.yaml and stored
-- as values or JSONB documents here.
--
-- Applied once to an empty database by the db-schema init container.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ── identity and audit ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS datahub_users (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username             TEXT NOT NULL UNIQUE,
    email                TEXT,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('admin', 'uploader', 'viewer')),
    organization         TEXT,               -- validated against DATAHUB_ORGANIZATIONS
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    created_by           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login           TIMESTAMPTZ,
    last_seen_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id       UUID NOT NULL REFERENCES datahub_users(id) ON DELETE CASCADE,
    username      TEXT NOT NULL,
    ip_address    TEXT,
    user_agent    TEXT,
    logged_in_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    logged_out_at TIMESTAMPTZ,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON user_sessions (is_active, logged_in_at DESC);

-- ── catalogue ────────────────────────────────────────────────────
-- One row per uploaded data set (a simulation run, an experiment, a
-- reference file set ...). dataset_type is an id from the project schema.
CREATE TABLE IF NOT EXISTS datasets (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name          TEXT NOT NULL UNIQUE,
    dataset_type  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'completed'
                  CHECK (status IN ('registered', 'running', 'completed', 'failed', 'unknown')),
    parameters    JSONB NOT NULL DEFAULT '{}',   -- extracted by the schema's parameter_sources
    metadata      JSONB NOT NULL DEFAULT '{}',   -- free metadata (schema metadata_fields, notes from tools)
    tags          TEXT[] NOT NULL DEFAULT '{}',
    notes         TEXT,
    allowed_orgs  TEXT[] NOT NULL DEFAULT '{}',  -- empty = visible to every authenticated user
    uploaded_by   TEXT,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    file_count    INTEGER NOT NULL DEFAULT 0,
    total_bytes   BIGINT NOT NULL DEFAULT 0,
    schema_hash   TEXT,                           -- schema version used at ingestion
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    fts_vector    TSVECTOR
);
CREATE INDEX IF NOT EXISTS idx_ds_type    ON datasets (dataset_type);
CREATE INDEX IF NOT EXISTS idx_ds_status  ON datasets (status);
CREATE INDEX IF NOT EXISTS idx_ds_created ON datasets (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ds_tags    ON datasets USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_ds_params  ON datasets USING GIN (parameters);
CREATE INDEX IF NOT EXISTS idx_ds_meta    ON datasets USING GIN (metadata);
CREATE INDEX IF NOT EXISTS idx_ds_name    ON datasets USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_ds_fts     ON datasets USING GIN (fts_vector);

CREATE OR REPLACE FUNCTION datasets_fts_update() RETURNS trigger AS $$
BEGIN
    NEW.fts_vector := to_tsvector('simple',
        coalesce(NEW.name, '') || ' ' || coalesce(NEW.dataset_type, '') || ' ' ||
        coalesce(NEW.notes, '') || ' ' || coalesce(array_to_string(NEW.tags, ' '), '') || ' ' ||
        coalesce(NEW.metadata::text, ''));
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trig_ds_fts ON datasets;
CREATE TRIGGER trig_ds_fts BEFORE INSERT OR UPDATE ON datasets
    FOR EACH ROW EXECUTE FUNCTION datasets_fts_update();

-- Optional sub-level of a data set (stages, load cases, time windows ...),
-- recognised from sub-folder names as configured in the schema.
CREATE TABLE IF NOT EXISTS dataset_parts (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    dataset_id  UUID NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    part_index  INTEGER NOT NULL,
    label       TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}',
    UNIQUE (dataset_id, label)
);
CREATE INDEX IF NOT EXISTS idx_parts_ds ON dataset_parts (dataset_id);

-- Every stored file. file_class is an id from the schema's file_classes.
CREATE TABLE IF NOT EXISTS files (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    dataset_id  UUID NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    part_id     UUID REFERENCES dataset_parts(id) ON DELETE CASCADE,
    file_class  TEXT NOT NULL DEFAULT 'other',
    relpath     TEXT NOT NULL,                    -- path inside the uploaded folder
    filename    TEXT NOT NULL,
    object_key  TEXT NOT NULL,                    -- key in the object store
    size_bytes  BIGINT,
    sha256      TEXT,
    time_value  DOUBLE PRECISION,                 -- simulated/recorded time, if configured
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (dataset_id, relpath)
);
CREATE INDEX IF NOT EXISTS idx_files_ds    ON files (dataset_id);
CREATE INDEX IF NOT EXISTS idx_files_part  ON files (part_id);
CREATE INDEX IF NOT EXISTS idx_files_class ON files (file_class);
CREATE INDEX IF NOT EXISTS idx_files_sha   ON files (sha256);

-- Named scalar results (quantities of interest), one row per value.
CREATE TABLE IF NOT EXISTS quantities (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    dataset_id   UUID NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    part_id      UUID REFERENCES dataset_parts(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    value        DOUBLE PRECISION NOT NULL,
    unit         TEXT,
    source       TEXT,
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_q_ds   ON quantities (dataset_id);
CREATE INDEX IF NOT EXISTS idx_q_name ON quantities (name, value);

-- ── derived data (ml-api) ────────────────────────────────────────
-- Parameter vectors: layout defined by the schema (keys stored per row).
-- No fixed dimension, so a schema change only requires re-vectorising.
CREATE TABLE IF NOT EXISTS param_vectors (
    dataset_id   UUID PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    vec          vector NOT NULL,
    keys         TEXT[] NOT NULL,
    schema_hash  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Field embeddings: PCA coefficients of spatial fields, per part (or dataset).
CREATE TABLE IF NOT EXISTS field_embeddings (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    dataset_id    UUID NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    part_id       UUID REFERENCES dataset_parts(id) ON DELETE CASCADE,
    field_name    TEXT NOT NULL,
    n_points      INTEGER NOT NULL,
    embedding     vector NOT NULL,
    n_components  INTEGER NOT NULL,
    explained_var DOUBLE PRECISION,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fe_unique ON field_embeddings (dataset_id, coalesce(part_id, '00000000-0000-0000-0000-000000000000'::uuid), field_name);
CREATE INDEX IF NOT EXISTS idx_fe_field ON field_embeddings (field_name, n_points);

-- ── parameter sweeps ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS parameter_sweeps (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name               TEXT NOT NULL,
    description        TEXT,
    param_ranges       JSONB NOT NULL,           -- {"param": [min, max], ...}
    objective_quantity TEXT,                     -- a quantity name from the schema
    objective_goal     TEXT CHECK (objective_goal IN ('minimize', 'maximize')),
    created_by         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS sweep_datasets (
    sweep_id   UUID NOT NULL REFERENCES parameter_sweeps(id) ON DELETE CASCADE,
    dataset_id UUID NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    linked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sweep_id, dataset_id)
);
CREATE TABLE IF NOT EXISTS sweep_suggestions (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    sweep_id         UUID NOT NULL REFERENCES parameter_sweeps(id) ON DELETE CASCADE,
    suggested_params JSONB NOT NULL,
    method           TEXT NOT NULL CHECK (method IN ('sobol', 'lhs', 'random', 'gp_uncertainty', 'gp_ei')),
    score            DOUBLE PRECISION,
    used             BOOLEAN NOT NULL DEFAULT FALSE,
    dataset_id       UUID REFERENCES datasets(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sugg_sweep ON sweep_suggestions (sweep_id);

-- ── audit ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS download_logs (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id       UUID REFERENCES datahub_users(id) ON DELETE SET NULL,
    username      TEXT NOT NULL,
    dataset_id    UUID,
    file_id       UUID,
    object_key    TEXT,
    filename      TEXT,
    size_bytes    BIGINT,
    downloaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dl_time ON download_logs (downloaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_dl_ds   ON download_logs (dataset_id);

-- ── convenience view ─────────────────────────────────────────────
CREATE OR REPLACE VIEW dataset_summary AS
SELECT d.id, d.name, d.dataset_type, d.status, d.tags, d.allowed_orgs, d.uploaded_by,
       d.created_at, d.started_at, d.finished_at, d.file_count, d.total_bytes,
       (SELECT count(*) FROM dataset_parts p WHERE p.dataset_id = d.id) AS part_count,
       (SELECT jsonb_object_agg(q.name, q.value) FROM quantities q
         WHERE q.dataset_id = d.id AND q.part_id IS NULL) AS quantities
FROM datasets d;

-- ── privileges ───────────────────────────────────────────────────
-- Applications connect as the database owner. The optional PostgREST
-- layer is for administrators only: the anonymous role gets nothing and
-- row-level security is enabled on every table.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['datahub_users','user_sessions','datasets','dataset_parts','files',
                             'quantities','param_vectors','field_embeddings','parameter_sweeps',
                             'sweep_datasets','sweep_suggestions','download_logs'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    END LOOP;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT USAGE ON SCHEMA public TO service_role;
        GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
        GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
    END IF;
END $$;
