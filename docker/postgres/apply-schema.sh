#!/bin/sh

# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

# Applies the DataHub database schema to a running PostgreSQL instance.
# Runs as a one-shot init container after the db service is healthy.
#   1. init.sql        base schema, only on an empty database
#   2. migrations/*.sql idempotent upgrades, applied in name order on every start
set -e

export PGPASSWORD="$POSTGRES_PASSWORD"
PSQL="psql -h db -U postgres -d postgres -v ON_ERROR_STOP=1"

TABLE_COUNT=$($PSQL -tAc "SELECT count(*) FROM information_schema.tables
                           WHERE table_schema='public' AND table_name IN ('datasets','files');")
if [ "$TABLE_COUNT" -ge "2" ]; then
  echo "DataHub base schema present."
else
  echo "Applying DataHub base schema..."
  $PSQL -f /init.sql
fi

for f in /migrations/*.sql; do
  [ -e "$f" ] || continue
  echo "Applying migration $(basename "$f")"
  $PSQL -q -f "$f"
done
echo "DataHub schema up to date."
