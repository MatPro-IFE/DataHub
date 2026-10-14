# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
DataHub ml-api - knowledge graph, parameter vectors, field embeddings, similarity and sweeps.

Everything project-specific comes from the project schema (config/schema.yaml).

Routes
  GET  /health, /schema
  POST /process/{id}            graph node + parameter vector + similarity edges for one dataset
  POST /process/all             re-run /process for every dataset (after a schema change)
  POST /embed/{id}              PCA embedding of spatial fields (schema 'fields' section)
  GET  /similar/params/{id}     nearest datasets by parameter vector (cosine)
  GET  /similar/fields/{id}     nearest datasets by field embedding (cosine)
  GET  /graph/overview, /graph/neighbors/{id};  POST /graph/delete/{id}
  POST /sweeps;  GET /sweeps, /sweeps/{id}
  POST /sweeps/{id}/link?dataset_id=, /sweeps/{id}/coverage, /sweeps/{id}/suggest?n=&method=
  GET  /sweeps/{id}/suggestions;  POST /sweeps/{id}/suggestions/{sid}/use?dataset_id=
"""
from __future__ import annotations

import hmac
import io
import json
import logging
import os
import posixpath
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import numpy as np
import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from minio import Minio
from neo4j import GraphDatabase
from pydantic import BaseModel
from sklearn.decomposition import IncrementalPCA

from datahub_sdk.readers import companions, read_snapshots, reader_for

from datahub_sdk.schema import load_schema

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ml-api")

SCHEMA = load_schema()
_DB = dict(host=os.environ.get("POSTGRES_HOST", "db"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
           dbname=os.environ.get("POSTGRES_DB", "postgres"), user=os.environ.get("POSTGRES_USER", "postgres"),
           password=os.environ.get("POSTGRES_PASSWORD", ""))
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "datahub-data")
NEO4J = (os.environ.get("NEO4J_URI", "bolt://neo4j:7687"), os.environ.get("NEO4J_USER", "neo4j"),
         os.environ.get("NEO4J_PASSWORD", ""))
G = SCHEMA.graph


def _db():
    return psycopg2.connect(**_DB, cursor_factory=psycopg2.extras.RealDictCursor)


def _s3():
    return Minio(os.environ.get("MINIO_ENDPOINT", "minio:9000"), access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
                 secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
                 secure=os.environ.get("MINIO_SECURE", "false").lower() == "true")


def _neo():
    return GraphDatabase.driver(NEO4J[0], auth=(NEO4J[1], NEO4J[2]))


def _vec(v) -> np.ndarray:
    """pgvector values arrive as '[1,2,3]' text through psycopg2."""
    if isinstance(v, str):
        return np.array([float(x) for x in v.strip("[]").split(",") if x.strip()])
    return np.asarray(v, dtype=float)


def _vstr(v) -> str:
    return "[" + ",".join(f"{float(x):.8g}" for x in v) + "]"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        with _neo() as d, d.session() as s:
            s.run("CREATE CONSTRAINT ds_id IF NOT EXISTS FOR (n:Dataset) REQUIRE n.id IS UNIQUE")
            s.run("CREATE CONSTRAINT res_key IF NOT EXISTS FOR (n:Resource) REQUIRE n.key IS UNIQUE")
            s.run("CREATE CONSTRAINT sweep_id IF NOT EXISTS FOR (n:Sweep) REQUIRE n.id IS UNIQUE")
    except Exception as exc:
        logger.warning("Neo4j not ready: %s", exc)
    yield


app = FastAPI(title="DataHub ml-api", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# Every caller except the health check must present the shared token (ML_API_TOKEN), so that
# other processes on the internal network cannot bypass the dashboard's access control.
ML_API_TOKEN = os.environ.get("ML_API_TOKEN", "")
if not ML_API_TOKEN:
    logger.warning("ML_API_TOKEN is not set: the ml-api accepts unauthenticated requests")


@app.middleware("http")
async def require_token(request, call_next):
    if ML_API_TOKEN and request.url.path != "/health":
        sent = request.headers.get("authorization", "")
        if not hmac.compare_digest(sent.encode(), f"Bearer {ML_API_TOKEN}".encode()):
            return JSONResponse({"detail": "unauthorised"}, status_code=401)
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "schema": SCHEMA.hash}


@app.get("/schema")
def schema():
    return SCHEMA.to_public_dict()


# ── dataset processing ───────────────────────────────────────────────────────

def _dataset(ds_id: str) -> dict:
    conn = _db(); cur = conn.cursor()
    cur.execute("SELECT id, name, dataset_type, status, parameters, tags FROM datasets WHERE id=%s", (ds_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="dataset not found")
    cur.execute("SELECT id, part_index, label FROM dataset_parts WHERE dataset_id=%s", (ds_id,))
    parts = [dict(r) for r in cur.fetchall()]
    conn.close()
    d = dict(row); d["parts"] = parts
    return d


def _similar_edges(ds_id: str):
    """SIMILAR_PARAMS edges from this dataset to its nearest neighbours above the threshold."""
    conn = _db(); cur = conn.cursor()
    cur.execute("""SELECT o.dataset_id, 1 - (o.vec <=> s.vec) AS score
                   FROM param_vectors o, param_vectors s
                   WHERE s.dataset_id = %s AND o.dataset_id <> s.dataset_id AND o.schema_hash = s.schema_hash
                     AND vector_dims(o.vec) = vector_dims(s.vec)
                   ORDER BY o.vec <=> s.vec LIMIT %s""", (ds_id, int(G["neighbours"])))
    rows = cur.fetchall(); conn.close()
    with _neo() as d, d.session() as s:
        s.run("MATCH (a:Dataset {id:$id})-[r:SIMILAR_PARAMS]->() DELETE r", id=ds_id)
        for r in rows:
            score = float(r["score"]) if r["score"] is not None else 0.0
            if score >= float(G["parameter_edge_threshold"]):
                s.run("""MATCH (a:Dataset {id:$a}), (b:Dataset {id:$b})
                         MERGE (a)-[r:SIMILAR_PARAMS]->(b) SET r.score=$score""",
                      a=ds_id, b=str(r["dataset_id"]), score=score)


@app.post("/process/all")
def process_all():
    conn = _db(); cur = conn.cursor()
    cur.execute("SELECT id FROM datasets ORDER BY created_at")
    ids = [str(r["id"]) for r in cur.fetchall()]; conn.close()
    for i in ids:
        _process(i, edges=False)
    for i in ids:
        _similar_edges(i)
    return {"processed": len(ids), "schema": SCHEMA.hash}


@app.post("/process/{ds_id}")
def process(ds_id: str):
    return _process(ds_id)


def _process(ds_id: str, edges: bool = True):
    ds = _dataset(ds_id)
    params = ds["parameters"] or {}
    vec, keys = SCHEMA.parameter_vector(params)
    conn = _db(); cur = conn.cursor()
    if vec and any(vec):
        cur.execute("""INSERT INTO param_vectors (dataset_id, vec, keys, schema_hash) VALUES (%s,%s::vector,%s,%s)
                       ON CONFLICT (dataset_id) DO UPDATE SET vec=EXCLUDED.vec, keys=EXCLUDED.keys,
                       schema_hash=EXCLUDED.schema_hash, created_at=NOW()""", (ds_id, _vstr(vec), keys, SCHEMA.vector_hash))
    else:
        cur.execute("DELETE FROM param_vectors WHERE dataset_id=%s", (ds_id,))
    conn.commit(); conn.close()

    numeric = {k: v for k, v in params.items() if isinstance(v, (int, float, bool, str)) and len(str(v)) < 200}
    with _neo() as d, d.session() as s:
        s.run("""MERGE (n:Dataset {id:$id}) SET n.name=$name, n.type=$type, n.status=$status, n.tags=$tags,
                 n.params=$params""", id=ds_id, name=ds["name"], type=ds["dataset_type"], status=ds["status"],
              tags=ds["tags"] or [], params=json.dumps(numeric, default=str))
        s.run("MATCH (n:Dataset {id:$id})-[r:HAS_PART|USES]->() DELETE r", id=ds_id)
        for p in ds["parts"]:
            s.run("""MATCH (n:Dataset {id:$id}) MERGE (p:Part {id:$pid}) SET p.label=$label, p.index=$idx
                     MERGE (n)-[:HAS_PART]->(p)""", id=ds_id, pid=str(p["id"]), label=p["label"], idx=p["part_index"])
        for res in G.get("shared_resources", []):
            val = params.get(res["parameter"])
            if val not in (None, ""):
                s.run("""MATCH (n:Dataset {id:$id}) MERGE (r:Resource {key:$key}) SET r.kind=$kind, r.value=$val
                         MERGE (n)-[:USES {parameter:$param}]->(r)""", id=ds_id, key=f"{res['label']}:{val}",
                      kind=res["label"], val=str(val), param=res["parameter"])
    if edges:
        _similar_edges(ds_id)
    return {"dataset_id": ds_id, "vector_dims": len(vec), "keys": keys}


@app.post("/graph/delete/{ds_id}")
def graph_delete(ds_id: str):
    with _neo() as d, d.session() as s:
        s.run("MATCH (n:Dataset {id:$id}) OPTIONAL MATCH (n)-[:HAS_PART]->(p:Part) DETACH DELETE n, p", id=ds_id)
        s.run("MATCH (r:Resource) WHERE NOT (r)<-[:USES]-() DELETE r")
    return {"deleted": ds_id}


# ── field embeddings ─────────────────────────────────────────────────────────

# The PCA state is stored as plain arrays (.npz, loaded with allow_pickle=False), never as a
# pickle, so that write access to the object store cannot be turned into code execution here.
_PCA_ARRAYS = ("components_", "mean_", "var_", "singular_values_", "explained_variance_",
               "explained_variance_ratio_")
_PCA_SCALARS = ("n_samples_seen_", "noise_variance_", "n_components_", "n_features_in_")


def _pca_prefix(field: str, n: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", field).strip(".") or "field"
    return f"_derived/pca/{safe}/n{n}/"


def _get_npz(mc, key):
    try:
        resp = mc.get_object(MINIO_BUCKET, key); data = resp.read(); resp.close(); resp.release_conn()
    except Exception:
        return None
    return np.load(io.BytesIO(data), allow_pickle=False)


def _pca_state(mc, field: str, n: int):
    prefix = _pca_prefix(field, n)
    pca = None
    z = _get_npz(mc, prefix + "basis.npz")
    if z is not None:
        pca = IncrementalPCA(n_components=int(z["n_components_"]))
        for k in _PCA_ARRAYS:
            setattr(pca, k, np.asarray(z[k], dtype=float))
        pca.n_samples_seen_ = int(z["n_samples_seen_"])
        pca.noise_variance_ = float(z["noise_variance_"])
        pca.n_components_ = int(z["n_components_"])
        pca.n_features_in_ = int(z["n_features_in_"])
        if "batch_size_" in z.files and int(z["batch_size_"]) > 0:
            pca.batch_size_ = int(z["batch_size_"])
    zb = _get_npz(mc, prefix + "buffer.npz")
    buf = np.asarray(zb["buffer"], dtype=float) if zb is not None else None
    return pca, buf, prefix


def _put_npz(mc, key, **arrays):
    b = io.BytesIO(); np.savez(b, **arrays); d = b.getvalue()
    mc.put_object(MINIO_BUCKET, key, io.BytesIO(d), len(d))


def _save_pca_state(mc, prefix, pca, buf):
    if pca is not None:
        _put_npz(mc, prefix + "basis.npz", **{k: getattr(pca, k) for k in _PCA_ARRAYS},
                 **{k: np.asarray(getattr(pca, k)) for k in _PCA_SCALARS},
                 batch_size_=np.asarray(getattr(pca, "batch_size_", 0)))
    if buf is not None:
        _put_npz(mc, prefix + "buffer.npz", buffer=buf)
    else:
        try:
            mc.remove_object(MINIO_BUCKET, prefix + "buffer.npz")
        except Exception:
            pass


@app.post("/embed/{ds_id}")
def embed(ds_id: str):
    """PCA embedding of the spatial fields of a dataset, one per part and field.

    Snapshot files are the files of class fields.file_class. Each is read with the
    schema's reader (auto = by extension: VTK, NumPy, HDF5, XDMF, Exodus); files that
    hold a time series contribute one snapshot per step. Heavy-data files referenced
    by an XDMF file are fetched alongside it and not read on their own.
    """
    F = SCHEMA.fields
    fcls = F.get("file_class")
    if not fcls:
        raise HTTPException(status_code=400, detail="schema has no 'fields.file_class'")
    k_req = int(F["pca_components"]); max_snap = int(F["max_snapshots"])
    conn = _db(); cur = conn.cursor()
    cur.execute("""SELECT part_id, object_key, relpath, file_class, time_value FROM files WHERE dataset_id=%s
                   ORDER BY part_id NULLS FIRST, time_value NULLS LAST, relpath""", (ds_id,))
    all_files = cur.fetchall()
    conn.close()
    by_rel = {r["relpath"]: r for r in all_files}
    groups: dict = {}
    for r in all_files:
        if r["file_class"] == fcls:
            groups.setdefault(r["part_id"], []).append(r)
    mc = _s3(); results = []; errors = []
    for part_id, rows in groups.items():
        with tempfile.TemporaryDirectory() as tmp:
            def fetch(rel):
                dest = Path(tmp) / rel
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    mc.fget_object(MINIO_BUCKET, by_rel[rel]["object_key"], str(dest))
                return dest
            # XDMF heavy data: fetch companions and do not read them as snapshots themselves
            skip = set()
            for r in rows:
                if reader_for(r["relpath"], F["reader"]) == "xdmf":
                    local = fetch(r["relpath"])
                    for dep in companions(local, "xdmf"):
                        rel = posixpath.normpath(posixpath.join(posixpath.dirname(r["relpath"]), dep))
                        if rel in by_rel:
                            fetch(rel); skip.add(rel)
            snaps: list = []
            for r in rows:
                if r["relpath"] in skip or reader_for(r["relpath"], F["reader"]) is None:
                    continue
                try:
                    for t, fields in read_snapshots(fetch(r["relpath"]), F["reader"], F.get("names") or [],
                                                    {"hdf5": F.get("hdf5") or {}, "exodus": F.get("exodus") or {}},
                                                    root=tmp):
                        snaps.append((t if t is not None else r["time_value"], len(snaps), fields))
                except Exception as exc:
                    logger.warning("cannot read %s: %s", r["relpath"], exc)
                    errors.append({"file": r["relpath"], "error": str(exc)[:200]})
        if not snaps:
            continue
        snaps.sort(key=lambda x: (x[0] is None, x[0] if x[0] is not None else 0, x[1]))
        if len(snaps) > max_snap:                      # evenly spaced subset of long series
            idx = np.unique(np.linspace(0, len(snaps) - 1, max_snap).round().astype(int))
            snaps = [snaps[i] for i in idx]
        per_field: dict[str, list[np.ndarray]] = {}
        for _, _, fields in snaps:
            for name, arr in fields.items():
                per_field.setdefault(name, []).append(arr)
        for field, arrs in per_field.items():
            if len({a.size for a in arrs}) != 1:
                errors.append({"field": field, "error": "snapshot sizes differ"}); continue
            X = np.vstack(arrs); n = X.shape[1]; k = min(k_req, n)
            pca, buf, prefix = _pca_state(mc, field, n)
            buf = X if buf is None else np.vstack([buf, X])
            if buf.shape[0] >= k:
                pca = pca or IncrementalPCA(n_components=k)
                pca.partial_fit(buf); buf = None
            _save_pca_state(mc, prefix, pca, buf)
            if pca is None:
                results.append({"part_id": part_id, "field": field, "status": f"buffered {0 if buf is None else buf.shape[0]}/{k} snapshots"})
                continue
            e = pca.transform(X).mean(axis=0)
            c = _db(); cc = c.cursor()
            cc.execute("""DELETE FROM field_embeddings WHERE dataset_id=%s AND part_id IS NOT DISTINCT FROM %s
                          AND field_name=%s""", (ds_id, part_id, field))
            cc.execute("""INSERT INTO field_embeddings (dataset_id, part_id, field_name, n_points, embedding,
                          n_components, explained_var) VALUES (%s,%s,%s,%s,%s::vector,%s,%s)""",
                       (ds_id, part_id, field, n, _vstr(e), int(pca.n_components_), float(np.sum(pca.explained_variance_ratio_))))
            c.commit(); c.close()
            results.append({"part_id": part_id, "field": field, "snapshots": len(arrs), "n_components": int(pca.n_components_),
                            "explained_variance": round(float(np.sum(pca.explained_variance_ratio_)), 4)})
    _field_edges(ds_id)
    return {"dataset_id": ds_id, "embeddings": results, "errors": errors}


def _field_edges(ds_id: str):
    conn = _db(); cur = conn.cursor()
    cur.execute("""SELECT DISTINCT ON (o.dataset_id, s.field_name) o.dataset_id AS other, s.field_name,
                          1 - (o.embedding <=> s.embedding) AS score
                   FROM field_embeddings s JOIN field_embeddings o
                     ON o.field_name = s.field_name AND o.n_points = s.n_points
                    AND vector_dims(o.embedding) = vector_dims(s.embedding) AND o.dataset_id <> s.dataset_id
                   WHERE s.dataset_id = %s
                   ORDER BY o.dataset_id, s.field_name, o.embedding <=> s.embedding""", (ds_id,))
    rows = cur.fetchall(); conn.close()
    with _neo() as d, d.session() as s:
        s.run("MATCH (a:Dataset {id:$id})-[r:SIMILAR_FIELDS]->() DELETE r", id=ds_id)
        for r in rows:
            if r["score"] is not None and float(r["score"]) >= float(G["field_edge_threshold"]):
                s.run("""MATCH (a:Dataset {id:$a}), (b:Dataset {id:$b})
                         MERGE (a)-[e:SIMILAR_FIELDS {field:$f}]->(b) SET e.score=$sc""",
                      a=ds_id, b=str(r["other"]), f=r["field_name"], sc=float(r["score"]))


# ── similarity queries ───────────────────────────────────────────────────────

@app.get("/similar/params/{ds_id}")
def similar_params(ds_id: str, n: int = 10):
    conn = _db(); cur = conn.cursor()
    cur.execute("""SELECT d.id AS dataset_id, d.name, d.dataset_type, 1 - (o.vec <=> s.vec) AS score
                   FROM param_vectors s JOIN param_vectors o ON o.dataset_id <> s.dataset_id
                        AND o.schema_hash = s.schema_hash AND vector_dims(o.vec) = vector_dims(s.vec)
                   JOIN datasets d ON d.id = o.dataset_id
                   WHERE s.dataset_id = %s ORDER BY o.vec <=> s.vec LIMIT %s""", (ds_id, n))
    rows = [dict(r, dataset_id=str(r["dataset_id"]), score=float(r["score"] or 0)) for r in cur.fetchall()]
    conn.close()
    return rows


@app.get("/similar/fields/{ds_id}")
def similar_fields(ds_id: str, field: str = "", n: int = 10):
    conn = _db(); cur = conn.cursor()
    cur.execute("""SELECT DISTINCT ON (d.id) d.id AS dataset_id, d.name, d.dataset_type, s.field_name AS field,
                          1 - (o.embedding <=> s.embedding) AS score
                   FROM field_embeddings s JOIN field_embeddings o
                     ON o.field_name = s.field_name AND o.n_points = s.n_points AND o.dataset_id <> s.dataset_id
                    AND vector_dims(o.embedding) = vector_dims(s.embedding)
                   JOIN datasets d ON d.id = o.dataset_id
                   WHERE s.dataset_id = %s AND (%s = '' OR s.field_name = %s)
                   ORDER BY d.id, o.embedding <=> s.embedding""", (ds_id, field, field))
    rows = sorted((dict(r, dataset_id=str(r["dataset_id"]), score=float(r["score"] or 0)) for r in cur.fetchall()),
                  key=lambda r: -r["score"])[:n]
    conn.close()
    return rows


# ── graph views ──────────────────────────────────────────────────────────────

@app.get("/graph/overview")
def graph_overview(limit: int = 500):
    nodes, edges = {}, []
    with _neo() as d, d.session() as s:
        for rec in s.run("MATCH (n:Dataset) RETURN n LIMIT $l", l=limit):
            n = rec["n"]; nodes[n["id"]] = {"id": n["id"], "label": n.get("name"), "kind": "dataset",
                                            "type": n.get("type"), "status": n.get("status")}
        for rec in s.run("""MATCH (a:Dataset)-[r]->(b) WHERE a.id IN $ids
                            RETURN a.id AS a, type(r) AS t, r.score AS score, r.field AS field,
                                   b.id AS bid, b.key AS bkey, labels(b) AS bl, b.kind AS bkind, b.value AS bval""",
                         ids=list(nodes)):
            if rec["t"] == "HAS_PART":
                continue
            target = rec["bid"] or rec["bkey"]
            if "Resource" in rec["bl"] and target not in nodes:
                nodes[target] = {"id": target, "label": f"{rec['bkind']}: {rec['bval']}", "kind": "resource"}
            if target in nodes:
                edges.append({"source": rec["a"], "target": target, "type": rec["t"],
                              "score": rec["score"], "field": rec["field"]})
    return {"nodes": list(nodes.values()), "edges": edges}


@app.get("/graph/neighbors/{ds_id}")
def graph_neighbors(ds_id: str):
    out, seen = [], set()
    with _neo() as d, d.session() as s:
        for rec in s.run("""MATCH (a:Dataset {id:$id})-[r]-(b) RETURN type(r) AS rel, r.score AS score, r.field AS field,
                            labels(b) AS labels, b.id AS id, b.key AS key, b.name AS name, b.label AS label,
                            b.kind AS kind, b.value AS value ORDER BY coalesce(r.score, 0) DESC""", id=ds_id):
            key = (rec["rel"], rec["id"] or rec["key"], rec["field"])
            if key in seen:
                continue
            seen.add(key)
            if "Dataset" in rec["labels"]:
                out.append({"kind": "dataset", "id": rec["id"], "name": rec["name"], "relation": rec["rel"],
                            "score": rec["score"], "field": rec["field"]})
            elif "Resource" in rec["labels"]:
                out.append({"kind": "resource", "id": rec["key"], "name": f"{rec['kind']}: {rec['value']}", "relation": rec["rel"]})
            elif "Part" in rec["labels"]:
                out.append({"kind": "part", "id": rec["id"], "name": rec["label"], "relation": rec["rel"]})
    return out


# ── parameter sweeps ─────────────────────────────────────────────────────────

class SweepIn(BaseModel):
    name: str
    description: Optional[str] = None
    param_ranges: dict[str, list[float]]
    objective_quantity: Optional[str] = None
    objective_goal: Optional[str] = None
    created_by: Optional[str] = None


@app.post("/sweeps")
def sweep_create(body: SweepIn):
    for k, r in body.param_ranges.items():
        p = SCHEMA.parameter(k)
        if not p or p.type not in ("float", "int", "bool"):
            raise HTTPException(status_code=400, detail=f"'{k}' is not a numeric schema parameter")
        if len(r) != 2 or r[1] <= r[0]:
            raise HTTPException(status_code=400, detail=f"range of '{k}' must be [min, max] with max > min")
    if body.objective_quantity and body.objective_quantity not in {q.name for q in SCHEMA.quantities}:
        raise HTTPException(status_code=400, detail="objective_quantity must be a quantity from the schema")
    conn = _db(); cur = conn.cursor()
    cur.execute("""INSERT INTO parameter_sweeps (name, description, param_ranges, objective_quantity, objective_goal, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                (body.name, body.description, json.dumps(body.param_ranges), body.objective_quantity,
                 body.objective_goal if body.objective_quantity else None, body.created_by))
    row = dict(cur.fetchone()); conn.commit(); conn.close()
    with _neo() as d, d.session() as s:
        s.run("MERGE (w:Sweep {id:$id}) SET w.name=$n", id=str(row["id"]), n=row["name"])
    return json.loads(json.dumps(row, default=str))


@app.get("/sweeps")
def sweep_list():
    conn = _db(); cur = conn.cursor()
    cur.execute("""SELECT s.*, (SELECT count(*) FROM sweep_datasets l WHERE l.sweep_id = s.id) AS n_datasets
                   FROM parameter_sweeps s ORDER BY created_at DESC""")
    rows = cur.fetchall(); conn.close()
    return json.loads(json.dumps([dict(r) for r in rows], default=str))


def _sweep(sweep_id: str) -> dict:
    conn = _db(); cur = conn.cursor()
    cur.execute("SELECT * FROM parameter_sweeps WHERE id::text=%s", (sweep_id,))
    sw = cur.fetchone()
    if not sw:
        conn.close(); raise HTTPException(status_code=404, detail="sweep not found")
    sw = dict(sw)
    cur.execute("""SELECT d.id, d.name, d.dataset_type, d.status, d.parameters,
                          (SELECT q.value FROM quantities q WHERE q.dataset_id = d.id AND q.part_id IS NULL
                             AND q.name = %s LIMIT 1) AS objective
                   FROM sweep_datasets l JOIN datasets d ON d.id = l.dataset_id WHERE l.sweep_id = %s
                   ORDER BY l.linked_at""", (sw["objective_quantity"] or "", sw["id"]))
    sw["datasets"] = [dict(r, id=str(r["id"])) for r in cur.fetchall()]
    cur.execute("SELECT * FROM sweep_suggestions WHERE sweep_id=%s ORDER BY created_at DESC, score DESC NULLS LAST", (sw["id"],))
    sw["suggestions"] = [dict(r) for r in cur.fetchall()]
    conn.close()
    return sw


@app.get("/sweeps/{sweep_id}")
def sweep_get(sweep_id: str):
    return json.loads(json.dumps(_sweep(sweep_id), default=str))


@app.post("/sweeps/{sweep_id}/link")
def sweep_link(sweep_id: str, dataset_id: str):
    conn = _db(); cur = conn.cursor()
    cur.execute("INSERT INTO sweep_datasets (sweep_id, dataset_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (sweep_id, dataset_id))
    conn.commit(); conn.close()
    with _neo() as d, d.session() as s:
        s.run("MATCH (a:Dataset {id:$a}) MERGE (w:Sweep {id:$w}) MERGE (a)-[:BELONGS_TO]->(w)", a=dataset_id, w=sweep_id)
    return {"linked": dataset_id}


def _points(sw: dict):
    """Scaled [0,1] coordinates of linked datasets inside the sweep ranges, plus objective values."""
    keys = list(sw["param_ranges"])
    lo = np.array([sw["param_ranges"][k][0] for k in keys], float)
    hi = np.array([sw["param_ranges"][k][1] for k in keys], float)
    X, y = [], []
    for d in sw["datasets"]:
        row = []
        for k in keys:
            v = SCHEMA.coerce(k, (d["parameters"] or {}).get(k))
            try:
                row.append(float(v))
            except (TypeError, ValueError):
                row = None; break
        if row is not None:
            X.append(row); y.append(d["objective"])
    X = np.array(X, float).reshape(-1, len(keys))
    return keys, (X - lo) / (hi - lo), lo, hi, np.array([np.nan if v is None else v for v in y], float)


@app.post("/sweeps/{sweep_id}/coverage")
def sweep_coverage(sweep_id: str):
    from scipy.stats import qmc
    sw = _sweep(sweep_id)
    keys, U, lo, hi, _ = _points(sw)
    inside = U[np.all((U >= 0) & (U <= 1), axis=1)] if len(U) else U
    dims = {}
    for j, k in enumerate(keys):
        col = inside[:, j] if len(inside) else np.array([])
        dims[k] = {"range": [float(lo[j]), float(hi[j])],
                   "observed": [float(lo[j] + col.min() * (hi[j] - lo[j])), float(lo[j] + col.max() * (hi[j] - lo[j]))] if len(col) else None,
                   "extent": float(col.max() - col.min()) if len(col) else 0.0,
                   "n_unique": int(len(np.unique(np.round(col, 6))))}
    disc = float(qmc.discrepancy(inside)) if len(inside) >= 2 else None
    fill = None
    if len(inside):
        probe = qmc.Sobol(d=len(keys), scramble=True, seed=0).random(1024)
        dist = np.sqrt(((probe[:, None, :] - inside[None, :, :]) ** 2).sum(-1)).min(1)
        fill = float(dist.max())
    return {"n_linked": len(sw["datasets"]), "n_inside": int(len(inside)), "dimensions": dims,
            "extent_score": float(np.mean([v["extent"] for v in dims.values()])) if dims else 0.0,
            "discrepancy": disc, "fill_distance": fill}


@app.post("/sweeps/{sweep_id}/suggest")
def sweep_suggest(sweep_id: str, n: int = 5, method: str = "sobol"):
    from scipy.stats import norm, qmc
    sw = _sweep(sweep_id)
    keys, U, lo, hi, y = _points(sw)
    d = len(keys); n = max(1, min(n, 100))
    rng = np.random.default_rng()
    used = method
    scores = [None] * n
    ok = ~np.isnan(y) if len(y) else np.array([], bool)
    if method.startswith("gp") and (not sw["objective_quantity"] or ok.sum() < 5):
        used = "sobol"
    if used in ("sobol", "lhs", "random"):
        # space-filling candidates, then greedily keep those farthest from existing runs
        m = max(4 * n, 64)
        cand = (qmc.Sobol(d=d, scramble=True).random(m) if used == "sobol" else
                qmc.LatinHypercube(d=d).random(m) if used == "lhs" else rng.random((m, d)))
        picks = _maximin(cand, U, n)
    else:
        from sklearn.gaussian_process import GaussianProcessRegressor
        from sklearn.gaussian_process.kernels import RBF, ConstantKernel, WhiteKernel
        Xo, yo = U[ok], y[ok]
        sign = -1.0 if sw["objective_goal"] == "maximize" else 1.0
        ym, ys = yo.mean(), yo.std() or 1.0
        gp = GaussianProcessRegressor(ConstantKernel() * RBF(length_scale=[0.3] * d) + WhiteKernel(1e-3),
                                      normalize_y=False, n_restarts_optimizer=2, random_state=0)
        gp.fit(Xo, sign * (yo - ym) / ys)
        cand = qmc.Sobol(d=d, scramble=True).random(1024)
        mu, sd = gp.predict(cand, return_std=True)
        if method == "gp_ei":
            best = (sign * (yo - ym) / ys).min()
            z = (best - mu) / np.maximum(sd, 1e-9)
            acq = (best - mu) * norm.cdf(z) + sd * norm.pdf(z)
        else:
            used, acq = "gp_uncertainty", sd
        picks, scores = _batch_by_acq(cand, acq, n, min_dist=0.15)
    conn = _db(); cur = conn.cursor(); saved = []
    for i, u in enumerate(picks):
        params = {k: float(lo[j] + u[j] * (hi[j] - lo[j])) for j, k in enumerate(keys)}
        for k in keys:
            p = SCHEMA.parameter(k)
            if p and p.type == "int":
                params[k] = int(round(params[k]))
            elif p and p.type == "bool":
                params[k] = params[k] >= 0.5
        cur.execute("""INSERT INTO sweep_suggestions (sweep_id, suggested_params, method, score)
                       VALUES (%s,%s,%s,%s) RETURNING id""", (sw["id"], json.dumps(params), used, scores[i]))
        saved.append({"id": str(cur.fetchone()["id"]), "params": params, "score": scores[i]})
    conn.commit(); conn.close()
    return {"method": used, "suggestions": saved}


def _maximin(cand: np.ndarray, existing: np.ndarray, n: int) -> list[np.ndarray]:
    pts = [e for e in existing] if len(existing) else []
    out = []
    for _ in range(n):
        if pts:
            dmin = np.sqrt(((cand[:, None, :] - np.array(pts)[None]) ** 2).sum(-1)).min(1)
            i = int(np.argmax(dmin))
        else:
            i = 0
        out.append(cand[i]); pts.append(cand[i]); cand = np.delete(cand, i, axis=0)
    return out


def _batch_by_acq(cand, acq, n, min_dist):
    """Greedy batch: highest acquisition first, skipping candidates closer than min_dist to a pick."""
    order = np.argsort(-acq); picks, scores = [], []
    for i in order:
        if all(np.linalg.norm(cand[i] - p) >= min_dist for p in picks):
            picks.append(cand[i]); scores.append(float(acq[i]))
        if len(picks) == n:
            break
    return picks, scores


@app.get("/sweeps/{sweep_id}/suggestions")
def sweep_suggestions(sweep_id: str):
    return json.loads(json.dumps(_sweep(sweep_id)["suggestions"], default=str))


@app.post("/sweeps/{sweep_id}/suggestions/{sid}/use")
def sweep_use(sweep_id: str, sid: str, dataset_id: Optional[str] = None):
    conn = _db(); cur = conn.cursor()
    cur.execute("UPDATE sweep_suggestions SET used=TRUE, dataset_id=%s WHERE id=%s AND sweep_id=%s",
                (dataset_id or None, sid, sweep_id))
    conn.commit(); conn.close()
    return {"used": sid}
