# API, Python client and command line

All access goes through the dashboard, so every call is checked against the user's role, organisation and project groups. Scripts log in with a normal user account; use a dedicated account per automated tool.

## Python client

```python
from datahub_sdk import DataHubClient

dh = DataHubClient("https://datahub.example.org", "alice", "password")   # or DATAHUB_URL/USER/PASSWORD
schema = dh.schema()                                                       # active project schema

ds_id = dh.upload_folder("runs/run_001", dataset_type="simulation", tags=["batch-1"],
                         metadata={"operator": "alice"}, allowed_orgs=[])
ds = dh.get(ds_id)                         # parameters, parts, files, quantities

hits = dh.search(param="htc", op="ge", value=800, quantity="peak_temperature", qop="lt", qvalue=700)
for d in dh.iter_datasets(dataset_type="simulation", tag="batch-1"):
    print(d["name"], d["parameters"])

dh.download(ds_id, "downloads/", file_class="time_series")   # keep folder layout
dh.download_archive(ds_id, "run_001.zip")                     # all files + metadata JSON

dh.embed_fields(ds_id)                     # PCA embedding of snapshot files
dh.similar(ds_id, n=5)                     # nearest datasets by parameters
dh.similar_fields(ds_id, field="temperature")
dh.neighbors(ds_id)                        # knowledge-graph neighbours

sw = dh.create_sweep("study A", {"htc": [300, 1800], "load": [0.02, 0.28]},
                     objective_quantity="peak_temperature", objective_goal="minimize")
dh.link(sw["id"], ds_id)
dh.suggest(sw["id"], n=8, method="gp_ei")  # sobol | lhs | random | gp_uncertainty | gp_ei

# project groups and access
dh.groups()                                          # your groups (admins: all)
dh.create_group("project-a", "work package 2")       # admin
dh.add_group_member("project-a", "bob")              # admin or group manager
dh.add_group_member("project-a", "alice", manager=True)   # admin appoints managers
ds2 = dh.upload_folder("runs/run_002", "simulation", allowed_groups=["project-a"])
dh.set_access(ds2, allowed_orgs=[], allowed_groups=["project-a"])   # uploader or admin

# time-limited sharing links (uploader or admin)
link = dh.share(ds2, days=7, max_downloads=20, note="external reviewer")
print(link["url"])                                   # returned once; only a hash is stored
dh.shares(ds2); dh.revoke_share(link["id"])
```

Anyone with a link can download without an account:

```python
from datahub_sdk import download_shared
download_shared("https://datahub.example.org/s/<token>", "downloads/")              # file by file
download_shared("https://datahub.example.org/s/<token>", "downloads/", archive=True) # one ZIP
```

## Command line

```bash
export DATAHUB_URL=https://datahub.example.org DATAHUB_USER=alice DATAHUB_PASSWORD=...
datahub upload runs/run_001 --type simulation --tags batch-1
datahub upload runs/ --each --type simulation --workers 4 --skip-existing
datahub search --type simulation --param htc --op ge --value 800
datahub download <dataset-id> --out downloads/
datahub upload runs/run_002 --type simulation --groups project-a
datahub share <dataset-id> --days 7 --max-downloads 20 --note "external reviewer"
datahub get-shared https://datahub.example.org/s/<token> --out downloads/ [--zip]
datahub groups
datahub fields results/run.exo          # what the field reader finds in a file (no server needed)
```

For very large local collections, ingest on the server without HTTP transfer (mount the folder as `INGEST_FOLDER`):

```bash
docker compose exec dashboard python -m dashboard.ingest /ingest --each --type simulation --workers 8 --tags batch-1 [--groups project-a]
```

## HTTP endpoints (session cookie from `POST /api/login`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/login`, `/api/password` | Log in (JSON), change own password |
| GET | `/api/schema` | Active project schema |
| GET | `/api/datasets` | Search: `q, type, status, tag, param, op, value, quantity, qop, qvalue, limit, offset` |
| GET / DELETE | `/api/datasets/{id}` | Dataset with parts, files, quantities / delete (owner or admin) |
| GET | `/api/datasets/{id}/archive.zip` | All visible files and a metadata JSON |
| GET | `/api/download/{file_id}` | One file (logged) |
| POST | `/api/upload/start`, `/file`, `/commit`, `/abort` | Three-phase folder upload |
| POST | `/api/upload/preview` | Classify file paths without uploading |
| GET | `/api/similar/params/{id}`, `/api/similar/fields/{id}` | Similarity search |
| POST | `/api/embed/{id}`, `/api/process/{id}` | Field embedding, re-run graph and vector processing |
| GET | `/api/graph/overview`, `/api/graph/neighbors/{id}` | Knowledge graph |
| GET / POST | `/api/sweeps`, `/api/sweeps/{id}`, `/api/sweeps/{id}/link/{ds}`, `/api/sweeps/{id}/suggest` | Parameter sweeps |
| PATCH | `/api/datasets/{id}/access` | Set `allowed_orgs` and `allowed_groups` (uploader or admin) |
| GET / POST / DELETE | `/api/groups`, `/api/groups/{name}` | List groups; create and delete (admin) |
| POST / DELETE | `/api/groups/{name}/members`, `/api/groups/{name}/members/{user}` | Membership (admin or group manager) |
| GET / POST | `/api/datasets/{id}/shares` | List and create sharing links (uploader or admin) |
| DELETE | `/api/shares/{id}` | Revoke a link |
| GET | `/api/admin/shares` | All links (admin) |

Public routes of a sharing link (no session; the token is the credential):

| Method | Path | Purpose |
|---|---|---|
| GET | `/s/{token}` | Page with parameters, quantities and files |
| GET | `/s/{token}/meta` | Metadata and file list as JSON (not counted) |
| GET | `/s/{token}/files/{file_id}` | One file (counts as one download) |
| GET | `/s/{token}/archive.zip` | All files and metadata (counts as one download) |

Expired, revoked or used-up links answer HTTP 410; unknown tokens 404, and repeated failures from one address are rate limited.

Interactive documentation of every route is served at `/api/docs`.
