# Deploying DataHub

This guide covers a production-style installation on a single Linux host. The same Compose file is used for development and production; only the `.env` values, the project schema and the network exposure differ.

## 1. Host sizing

| Load | CPU | RAM | Disk |
|---|---|---|---|
| Evaluation, demo data | 2 cores | 6 GB | 20 GB |
| Project instance (tens of users, 10^4 datasets) | 8 cores | 32 GB | object volume plus 30 % headroom |

PostgreSQL holds only metadata (typically well below 1 % of the object volume). The Neo4j heap is set with `NEO4J_HEAP` (default 1 GB).

## 2. Install

```bash
cp docker/.env.example docker/.env        # replace every <CHANGE_ME>
cd docker && sudo bash setup-dirs.sh      # host directories with container UIDs; creates ../config/schema.yaml
# edit ../config/schema.yaml for your project (docs/SCHEMA_GUIDE.md)
docker compose --env-file .env up -d --build
```

Use a fresh set of secrets for every instance. Never reuse `.env` files between deployments and never commit them.

## 3. Project schema

The file `config/schema.yaml` is mounted read-only into the `dashboard` and `ml-api` containers (`DATAHUB_SCHEMA=/app/config/schema.yaml`). It is validated at start-up; a container with an invalid schema stops with a message naming the offending key. Check a schema before restarting:

```bash
datahub check-schema config/schema.yaml
datahub preview /path/to/example_run --schema config/schema.yaml --type simulation
docker compose --env-file .env restart dashboard ml-api
```

Every dataset records the hash of the schema it was ingested with. After a change to parameters, fields or graph rules, recompute vectors, embeddings and graph edges for existing data:

```bash
curl -X POST -H "Authorization: Bearer $ML_API_TOKEN" http://127.0.0.1:8090/process/all
```

Changes to file classes or quantities apply to new uploads; re-ingest older datasets if they should be reclassified.

## 4. Configuration reference

| Variable | Purpose |
|---|---|
| `DATA_DIR` | Host directory for PostgreSQL, object store, Neo4j and upload staging |
| `POSTGRES_PASSWORD` | Database password |
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `MINIO_BUCKET` | Object store credentials and bucket name |
| `MINIO_BIND`, `MINIO_IMAGE` | Host interface of the S3 API; container image of the S3 server (default `cgr.dev/chainguard/minio:latest`) |
| `DASHBOARD_SECRET_KEY`, `DATAHUB_ADMIN_PASSWORD` | Session signing key; password of the bootstrap `admin` account |
| `DASHBOARD_BIND`, `DASHBOARD_TZ` | Host interface of the dashboard; display time zone |
| `DATAHUB_SITE_NAME` | Name in the browser title (default: `instance.name` from the schema) |
| `DATAHUB_ORGANIZATIONS` | Comma-separated organisations users can belong to |
| `DATAHUB_ORG_EMAIL_DOMAINS` | `domain=Org` pairs used to pre-select the organisation of new users |
| `DATAHUB_ORG_HIDDEN_FILE_CLASSES` | `Org=class1|class2` pairs; schema file classes an organisation may never see |
| `NEO4J_PASSWORD`, `NEO4J_HEAP` | Graph database password and heap size |
| `INGEST_FOLDER` | Optional host folder mounted read-only at `/ingest` for bulk ingestion |
| `ML_API_TOKEN` | Shared secret between dashboard and ml-api (required) |
| `DATAHUB_COOKIE_SECURE` | `true` behind HTTPS: `Secure` session cookie and HSTS |
| `DATAHUB_MIN_PASSWORD_LENGTH` | Minimum password length (default 12) |
| `DATAHUB_MAX_UPLOAD_GB` | Largest single upload in GB (default 200) |
| `DATAHUB_SHARE_MAX_DAYS` | Longest lifetime of a sharing link in days (default 30; `0` disables links) |
| `DATAHUB_PUBLIC_URL` | Public address used in sharing links, e.g. `https://datahub.example.org` (default: the address of the request) |
| `PGRST_JWT_SECRET` | Only for the optional `admin` profile (PostgREST) |

## 5. Groups and sharing links

Project groups are managed on the **Groups** page (or with the client): admins create groups and appoint managers; managers add and remove members. A dataset restricted to groups and organisations is visible to admins, its uploader, members of the listed organisations and members of the listed groups. A group that still restricts datasets cannot be deleted, so that deleting it can never widen access.

Sharing links are created by the uploader of a dataset or an admin on the dataset page. Set `DATAHUB_PUBLIC_URL` when the dashboard runs behind a reverse proxy, so that links carry the public address. Admins see all links under **Admin, Sharing links** and can revoke any of them.

## 6. Loading existing data

Small batches: the upload page or `datahub upload <folder> --type <id>`. Many folders from a workstation:

```bash
datahub upload /data/runs --each --type simulation --workers 4 --skip-existing
```

Large archives already on the server: set `INGEST_FOLDER` and run the bulk tool inside the dashboard container. It uses the same code path as the web upload.

```bash
docker compose --env-file .env exec dashboard \
    python -m dashboard.ingest /ingest/runs --each --type simulation --workers 8 --skip-existing
```

## 7. Network exposure

All services bind to `127.0.0.1` by default. For access from other machines:

1. Put a reverse proxy with TLS (nginx, Caddy, Traefik) in front of the dashboard on port 8080, keep `DASHBOARD_BIND=127.0.0.1`, and set `DATAHUB_COOKIE_SECURE=true` and `DATAHUB_PUBLIC_URL`. The proxy must pass the original `Host` (or `X-Forwarded-Host`) header, which the dashboard compares with the `Origin` of state-changing requests.
2. Downloads are streamed by the dashboard after the access check, so the S3 API does not need to be exposed. Sharing links (`/s/...`) are served by the dashboard too; if links should work from outside the network, the dashboard must be reachable there.
3. Keep Neo4j, the MinIO console, ml-api and the optional admin services on loopback. Reach them through an SSH tunnel.

## 8. Optional admin profile

PostgREST and Supabase Studio are not needed for normal operation. For database administration start them explicitly:

```bash
python generate_jwt.py                    # writes PGRST_JWT_SECRET and a service key to paste into .env
docker compose --env-file .env --profile admin up -d
```

The `anon` role has no grants; only the service key can read through PostgREST. Never hand it to end users.

## 9. Object store

The upstream `minio/minio` and `minio/mc` images were removed from Docker Hub and quay.io in September 2026, and the community edition is distributed as source only. Options:

- **Default**: `cgr.dev/chainguard/minio:latest` (runs as UID 65532; `setup-dirs.sh` sets ownership). The free tier publishes only the `latest` tag; pin a digest for long-lived instances: `MINIO_IMAGE=cgr.dev/chainguard/minio@sha256:<digest>`.
- **Build from source**: compile a tagged MinIO release with Go and set `MINIO_IMAGE` to your image.
- **Other S3-compatible servers** (Garage, SeaweedFS, Ceph RGW, a cloud bucket): DataHub uses only the standard S3 API through the `minio` Python client. Point `MINIO_ENDPOINT` at the server, adjust or remove the `minio` service, and keep the bucket private.

The dashboard creates the bucket at start-up and the bucket is never made anonymously readable.

## 10. Backup and restore

| What | How |
|---|---|
| Metadata | `docker compose exec db pg_dump -U postgres -Fc postgres > datahub.dump` |
| Users only | `docker/backup_users.sh > users.sql` |
| Objects | mirror `${DATA_DIR}/minio` or use any S3 sync tool against the bucket |
| Schema | `config/schema.yaml` (keep it under version control in your instance repository) |
| Graph | rebuildable with `POST /process/all` on ml-api (with the `ML_API_TOKEN` bearer token); back up `${DATA_DIR}/neo4j` to avoid recomputation |

## 11. Project lifecycle and decommissioning

A typical project instance runs for the project period plus a short hand-over phase:

1. **Release**: export datasets cleared for publication (ZIP archive with files and metadata JSON) to a repository such as Zenodo and store the DOI in the dataset's notes or tags.
2. **Hand-over**: each organisation downloads its own datasets; the download log documents what was retrieved.
3. **Catalogue export**: `pg_dump` of the metadata, the schema file and a JSON export from the API.
4. **Deletion**: `docker compose down -v` and removal of `${DATA_DIR}`.

## 12. Upgrading

```bash
git pull
docker compose --env-file .env build && docker compose --env-file .env up -d
```

The one-shot `db-schema` service applies `docker/postgres/init.sql` to an empty database and then every file in `docker/postgres/migrations/` in name order, on every start. Migrations are idempotent, so upgrading is `git pull` followed by `up -d`; project groups and sharing links were added this way (`001_groups_and_sharing.sql`), so instances created from an earlier snapshot of the repository pick them up on the next start. After an upgrade that changes field readers, re-run the field embedding of datasets whose files were not readable before.
