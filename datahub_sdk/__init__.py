# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
datahub_sdk - schema engine, Python client and command-line tools for DataHub.

    from datahub_sdk import DataHubClient, load_schema
    client = DataHubClient("https://datahub.example.org", "alice", "secret")
    ds = client.upload_folder("runs/run_001", dataset_type="simulation", tags=["demo"])
"""
from .schema import Schema, SchemaError, load_schema, parse_schema

__all__ = ["Schema", "SchemaError", "load_schema", "parse_schema", "DataHubClient", "download_shared"]
__version__ = "1.0.0"


def __getattr__(name):  # lazy import so the schema engine has no HTTP dependencies
    if name in ("DataHubClient", "download_shared"):
        from . import client
        return getattr(client, name)
    raise AttributeError(name)
