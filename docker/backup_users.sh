#!/bin/bash

# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

# backup_users.sh - export DataHub user accounts (data only) as SQL.
#   ./backup_users.sh > users_backup.sql
#   docker compose exec -T db psql -U postgres -d postgres < users_backup.sql   # restore into an empty table
set -euo pipefail
cd "$(dirname "$0")"
docker compose --env-file .env exec -T db pg_dump -U postgres -d postgres --data-only --column-inserts -t public.datahub_users
