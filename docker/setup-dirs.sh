#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

# setup-dirs.sh – create host storage directories with correct ownership.
#
# Run ONCE before the first `docker compose up` on a fresh machine,
# or after changing DATA_DIR.
#
# Must be run with sudo (or as root) because it needs to chown directories
# to the UIDs used inside the containers.
#
# Usage:
#   sudo bash setup-dirs.sh [DATA_DIR]          # e.g. sudo bash setup-dirs.sh ./data
#   or set DATA_DIR in .env and just run:
#   sudo bash setup-dirs.sh

set -euo pipefail

# ── Resolve DATA_DIR ────────────────────────────────────────────────
# 1. Use argument, 2. read from .env, 3. default
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${1:-}" ]]; then
    DATA_DIR="$1"
elif [[ -f "${SCRIPT_DIR}/.env" ]]; then
    DATA_DIR="$(grep -E '^DATA_DIR=' "${SCRIPT_DIR}/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")"
fi
DATA_DIR="${DATA_DIR:-./data}"

echo "=== DataHub storage setup ==="
echo "DATA_DIR = ${DATA_DIR}"
echo ""

# ── PostgreSQL ─────────────────────────────────────────────────────
# supabase/postgres runs the postgres process as UID 101 / GID 102.
PG_DIR="${DATA_DIR}/postgres"
echo "Creating PostgreSQL data directory: ${PG_DIR}"
mkdir -p "${PG_DIR}"
chown 101:102 "${PG_DIR}"
chmod 700 "${PG_DIR}"
echo "  ✓ ${PG_DIR}  (owner 101:102, mode 700)"

# ── MinIO ──────────────────────────────────────────────────────────
# The default Chainguard MinIO image runs as UID/GID 65532 (nonroot).
MINIO_DIR="${DATA_DIR}/minio"
MINIO_UID="${MINIO_UID:-65532}"
echo "Creating MinIO data directory: ${MINIO_DIR}"
mkdir -p "${MINIO_DIR}"
chown "${MINIO_UID}:${MINIO_UID}" "${MINIO_DIR}"
chmod 750 "${MINIO_DIR}"
echo "  ✓ ${MINIO_DIR}  (owner ${MINIO_UID}, mode 750)"

# ── Neo4j ──────────────────────────────────────────────────────────
# neo4j:5-community runs as UID 7474 / GID 7474 inside the container.
NEO4J_DIR="${DATA_DIR}/neo4j"
echo "Creating Neo4j data directory: ${NEO4J_DIR}"
mkdir -p "${NEO4J_DIR}"
chown 7474:7474 "${NEO4J_DIR}" 2>/dev/null || chown nobody:nogroup "${NEO4J_DIR}" 2>/dev/null || true
chmod 755 "${NEO4J_DIR}"
echo "  ✓ ${NEO4J_DIR}  (mode 755)"

# ── Upload staging and optional ingest folder ─────────────────────
mkdir -p "${DATA_DIR}/staging" "${DATA_DIR}/ingest"
chown 10001:10001 "${DATA_DIR}/staging"   # dashboard runs as uid 10001
chmod 700 "${DATA_DIR}/staging"
echo "  ✓ ${DATA_DIR}/staging, ${DATA_DIR}/ingest"

# ── Project schema ─────────────────────────────────────────────────
if [[ ! -f "${SCRIPT_DIR}/../config/schema.yaml" ]]; then
    cp "${SCRIPT_DIR}/../config/schema.example.yaml" "${SCRIPT_DIR}/../config/schema.yaml"
    echo "  ✓ config/schema.yaml created from the example; edit it for your project"
fi

echo ""
echo "=== Done. You can now run: docker compose --env-file .env up -d ==="
