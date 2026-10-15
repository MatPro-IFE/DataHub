# Third-party components

DataHub's own code is licensed under Apache-2.0 and its report and documentation under CC BY 4.0 (see `NOTICE` and `REUSE.toml`). It builds on the components below, which keep their own licences.

## Included in this repository

| Component | Version | Licence | Location |
|---|---|---|---|
| Bootstrap | 5.3.3 | MIT | `dashboard/static/vendor/bootstrap` |
| Bootstrap Icons | 1.11.3 | MIT | `dashboard/static/vendor/bootstrap-icons` |
| vis-network | 9.1.9 | Apache-2.0 or MIT | `dashboard/static/vendor/vis-network` |
| Swagger UI | 5.17.14 | Apache-2.0 | `dashboard/static/vendor/swagger-ui` |
| Inter, JetBrains Mono | fontsource 5.0 | OFL-1.1 | `dashboard/static/vendor/fonts` |
| Fira Sans | 4 | OFL-1.1 | `docs/technical_note/brand/fonts` |

The licence texts are in `LICENSES/` and next to each component.

## Started as separate containers (not distributed with DataHub)

| Component | Licence | Notes |
|---|---|---|
| PostgreSQL (supabase/postgres image) | PostgreSQL Licence | metadata store |
| pgvector | PostgreSQL Licence | vector extension |
| MinIO (Chainguard build) | AGPL-3.0 | object store, used unmodified over its S3 network API |
| Neo4j Community Edition | GPL-3.0 | knowledge graph, used unmodified over the Bolt protocol |
| PostgREST (optional) | MIT | administration profile |
| Supabase Studio and postgres-meta (optional) | Apache-2.0 | administration profile |

DataHub communicates with these services only over network protocols and does not include or link their code, so their licences do not extend to DataHub. Anyone who modifies and redistributes or offers one of these services takes on the obligations of its licence.

## Python packages installed into the images

| Package | Licence |
|---|---|
| FastAPI, Starlette, uvicorn, Jinja2, python-multipart, itsdangerous | MIT / BSD-3-Clause |
| requests | Apache-2.0 |
| minio (Python client) | Apache-2.0 |
| psycopg2 | LGPL-3.0 with exceptions |
| passlib, bcrypt | BSD / Apache-2.0 |
| neo4j (Python driver) | Apache-2.0 |
| NumPy, SciPy, scikit-learn | BSD-3-Clause |
| h5py | BSD-3-Clause |
| VTK | BSD-3-Clause |
| defusedxml | PSF-2.0 |
| PyYAML | MIT |

Versions are resolved at build time; Dependabot (`.github/dependabot.yml`) proposes updates weekly and CI checks them with pip-audit.
