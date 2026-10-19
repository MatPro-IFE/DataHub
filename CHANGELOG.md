# Changelog

## 1.0.0 (2026-10-19)

First public release.

- Project schema (`config/schema.yaml`): dataset types with optional parts, typed parameters and parameter sources, file classes, quantities of interest, timing, field embedding and graph rules; validated at start-up and by `datahub check-schema`
- Generic data model: datasets, parts, files and quantities; one ingestion code path shared by web upload, client and server-side bulk tool
- Dashboard with search (text, tags, type, status, parameter and quantity filters), folder upload with classification preview, downloads and ZIP archives, schema page, user and organisation management, audit logs
- Python client and `datahub` command-line tool (check-schema, preview, fields, upload, search, download, share, get-shared, groups)
- Knowledge graph (Neo4j) with datasets, parts, shared resources and sweeps, and parameter and field similarity edges
- Parameter vectors and incremental-PCA field embeddings in pgvector
- Parameter sweeps with coverage metrics (extent, discrepancy, fill distance) and Sobol, Latin hypercube, random and Gaussian-process suggestions with batch diversity
- Organisations, e-mail domain mapping and hidden file classes configurable per deployment
- Object store image configurable; default Chainguard MinIO build; bucket created by the dashboard and kept private; PostgREST and Studio moved to an optional `admin` profile
- Example schema, synthetic demo data generator and end-to-end smoke test
- Field readers for HDF5, XDMF (temporal collections, spatial collections, HDF5 or inline data) and Exodus II (netCDF classic and netCDF-4), next to VTK and NumPy; `fields.reader: auto` selects by extension; files holding a time series give one snapshot per step; long series are sampled evenly; reader options `fields.hdf5` and `fields.exodus`
- `datahub fields <file>` shows what a reader finds; `datahub_sdk.readers` can be used on its own
- Parameter vectors are versioned by a hash of the parameter definitions only
- Project groups with managers; datasets can be restricted to groups as well as organisations; access can be changed after upload by the uploader or an admin
- Time-limited sharing links with expiry, optional download limit, revocation, public download page, `download_shared()` and `datahub get-shared`; downloads logged with the link
- Database migrations applied automatically by the `db-schema` service (`docker/postgres/migrations/`)
- Demo data include run_001 written as XDMF, Exodus II and HDF5; reader unit tests
- Security hardening after an assessment (docs/SECURITY_ASSESSMENT.md): server-side session revocation, enforced password change, local-only login redirects, same-origin check, login-only API documentation, ml-api token, unprivileged read-only containers, pickle-free derived data, confined XDMF paths, defusedxml, upload limits, token redaction in logs, self-hosted front-end assets with a strict Content-Security-Policy, security scans and Dependabot in CI
- Licensing made explicit: Apache-2.0 for code, CC BY 4.0 for the technical note and documentation, SPDX headers, REUSE.toml, LICENSES/, THIRD_PARTY_NOTICES.md
