#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
Generate ANON_KEY and SERVICE_KEY JWT tokens for the DataHub PostgREST setup.

Usage:
    pip install pyjwt
    python generate_jwt.py

Paste the output values into docker/.env.
"""

import secrets
import sys

try:
    import jwt
except ImportError:
    print("PyJWT not installed.  Run:  pip install pyjwt")
    sys.exit(1)

import datetime

# Generate a random secret if one is not already set, or paste your own.
SECRET = secrets.token_hex(32)

now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
ten_years = now + 60 * 60 * 24 * 365 * 10

anon_payload = {
    "role": "anon",
    "iss": "supabase",
    "iat": now,
    "exp": ten_years,
}

service_payload = {
    "role": "service_role",
    "iss": "supabase",
    "iat": now,
    "exp": ten_years,
}

anon_key    = jwt.encode(anon_payload,    SECRET, algorithm="HS256")
service_key = jwt.encode(service_payload, SECRET, algorithm="HS256")

print("=" * 60)
print("Copy these values into  docker/.env")
print("=" * 60)
print()
print(f"PGRST_JWT_SECRET={SECRET}")
print()
print(f"ANON_KEY={anon_key}")
print()
print(f"SERVICE_KEY={service_key}")
print()
print("Keep SERVICE_KEY secret – it has full database access.")
