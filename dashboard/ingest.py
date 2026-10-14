# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
dashboard.ingest - schema-driven ingestion of a dataset folder (server side).

One code path for every upload route: the browser upload stages files in a
temporary folder and calls ingest_folder(); the bulk tool calls it for every
sub-folder of a mounted directory.

Bulk use inside the dashboard container:
    python -m dashboard.ingest /mnt/runs --type simulation --workers 8 --tags batch1
    python -m dashboard.ingest /mnt/runs --type simulation --each   # every sub-folder is a dataset
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import requests
from minio import Minio

from datahub_sdk.schema import Schema, SchemaError, load_schema

logger = logging.getLogger(__name__)

# Files of these classes are read into memory for quantity/timing extraction
MAX_TEXT_BYTES = 64 * 1024 * 1024


class IngestError(RuntimeError):
    pass


def db_connect():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "db"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ.get("POSTGRES_DB", "postgres"), user=os.environ.get("POSTGRES_USER_DB", "postgres"),
        password=os.environ.get("POSTGRES_PASSWORD", ""), cursor_factory=psycopg2.extras.RealDictCursor)


def s3_client() -> Minio:
    return Minio(os.environ.get("MINIO_ENDPOINT", "minio:9000"), access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
                 secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
                 secure=os.environ.get("MINIO_SECURE", "false").lower() == "true")


BUCKET = os.environ.get("MINIO_BUCKET", "datahub-data")
ML_API_URL = os.environ.get("ML_API_URL", "http://ml-api:8090")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def list_files(root: Path, schema: Schema) -> list[str]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(root)).replace("\\", "/")
            if not schema.skip(rel) and not any(part.startswith(".") for part in Path(rel).parts):
                out.append(rel)
    return out


def ingest_folder(root: Path | str, schema: Schema, dataset_type: str, *, name: Optional[str] = None,
                  tags: Optional[list[str]] = None, notes: Optional[str] = None, metadata: Optional[dict] = None,
                  allowed_orgs: Optional[list[str]] = None, allowed_groups: Optional[list[str]] = None,
                  uploaded_by: Optional[str] = None,
                  status: str = "completed", skip_existing: bool = True, conn=None, s3=None,
                  trigger_pipeline: bool = True) -> tuple[str, bool]:
    """Register a folder as one dataset. Returns (dataset_id, created)."""
    root = Path(root)
    if not root.is_dir():
        raise IngestError(f"not a folder: {root}")
    schema.dataset_type(dataset_type)  # validates the type
    name = name or root.name
    own_conn = conn is None
    conn = conn or db_connect()
    s3 = s3 or s3_client()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM datasets WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            if skip_existing:
                return str(row["id"]), False
            raise IngestError(f"a dataset named '{name}' already exists")

        files = list_files(root, schema)
        params = schema.extract_parameters(root, files)
        cur.execute("""INSERT INTO datasets (name, dataset_type, status, parameters, metadata, tags, notes,
                                             allowed_orgs, allowed_groups, uploaded_by, schema_hash)
                       VALUES (%s,%s,'registered',%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (name, dataset_type, json.dumps(params, default=str), json.dumps(metadata or {}),
                     tags or [], notes, allowed_orgs or [], allowed_groups or [], uploaded_by, schema.hash))
        ds_id = str(cur.fetchone()["id"])
        conn.commit()

        part_ids: dict[str, str] = {}
        text_cache: dict[Optional[str], list[tuple[str, str, bytes]]] = {}
        wanted = {q.source.get("file_class") for q in schema.quantities}
        if schema.timing:
            wanted.add(schema.timing.get("file_class"))
        total = 0
        try:
            for rel in files:
                path = root / rel
                cls = schema.classify(rel)
                part = schema.part_of(rel, dataset_type)
                part_id = None
                if part:
                    idx, label = part
                    if label not in part_ids:
                        cur.execute("""INSERT INTO dataset_parts (dataset_id, part_index, label) VALUES (%s,%s,%s)
                                       RETURNING id""", (ds_id, idx, label))
                        part_ids[label] = str(cur.fetchone()["id"])
                    part_id = part_ids[label]
                size = path.stat().st_size
                with path.open("rb") as f:
                    head = f.read(4096)
                tval = schema.file_time(rel, head, cls)
                key = f"datasets/{ds_id}/{rel}"
                ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
                s3.fput_object(BUCKET, key, str(path), content_type=ctype)
                cur.execute("""INSERT INTO files (dataset_id, part_id, file_class, relpath, filename, object_key,
                                                  size_bytes, sha256, time_value)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (ds_id, part_id, cls, rel, Path(rel).name, key, size, _sha256(path), tval))
                total += size
                if cls in wanted and size <= MAX_TEXT_BYTES:
                    text_cache.setdefault(part_id, []).append((rel, cls, path.read_bytes()))
            conn.commit()

            # quantities per part (or dataset level when there are no parts)
            for part_id, triples in text_cache.items():
                for q in schema.extract_quantities(triples):
                    cur.execute("""INSERT INTO quantities (dataset_id, part_id, name, value, unit, source)
                                   VALUES (%s,%s,%s,%s,%s,%s)""",
                                (ds_id, part_id, q["name"], q["value"], q["unit"], q["source"]))
            # dataset-level quantities for datasets with parts: aggregate = last part with a value
            if part_ids:
                cur.execute("""INSERT INTO quantities (dataset_id, part_id, name, value, unit, source)
                               SELECT DISTINCT ON (q.name) q.dataset_id, NULL, q.name, q.value, q.unit,
                                      'from part ' || p.label
                               FROM quantities q JOIN dataset_parts p ON p.id = q.part_id
                               WHERE q.dataset_id = %s ORDER BY q.name, p.part_index DESC""", (ds_id,))
            all_triples = [t for v in text_cache.values() for t in v]
            started, finished = schema.extract_timing(all_triples)
            cur.execute("""UPDATE datasets SET status=%s, file_count=%s, total_bytes=%s,
                                  started_at=%s, finished_at=%s WHERE id=%s""",
                        (status, len(files), total, started, finished, ds_id))
            conn.commit()
        except Exception:
            conn.rollback()
            delete_dataset(ds_id, conn=conn, s3=s3)
            raise
        if trigger_pipeline:
            trigger_ml(ds_id)
        return ds_id, True
    finally:
        if own_conn:
            conn.close()


def trigger_ml(ds_id: str, wait: bool = False):
    """Ask the ml-api to build graph nodes, parameter vector and similarity edges."""
    def run():
        try:
            tok = os.environ.get("ML_API_TOKEN", "")
            requests.post(f"{ML_API_URL}/process/{ds_id}", timeout=300,
                          headers={"Authorization": f"Bearer {tok}"} if tok else {})
        except Exception as exc:
            logger.warning("ml-api processing of %s failed: %s", ds_id, exc)
    if wait:
        run()
    else:
        threading.Thread(target=run, daemon=True).start()


def delete_dataset(ds_id: str, conn=None, s3=None):
    own = conn is None
    conn = conn or db_connect()
    s3 = s3 or s3_client()
    try:
        for obj in s3.list_objects(BUCKET, prefix=f"datasets/{ds_id}/", recursive=True):
            s3.remove_object(BUCKET, obj.object_name)
        cur = conn.cursor()
        cur.execute("DELETE FROM datasets WHERE id = %s", (ds_id,))
        conn.commit()
        try:
            requests.post(f"{ML_API_URL}/graph/delete/{ds_id}", timeout=30)
        except Exception:
            pass
    finally:
        if own:
            conn.close()


# ── bulk command line ────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="Ingest dataset folders into DataHub (server side).")
    ap.add_argument("path", help="a dataset folder, or a folder of dataset folders with --each")
    ap.add_argument("--type", required=True, dest="dataset_type", help="dataset type id from the schema")
    ap.add_argument("--each", action="store_true", help="treat every sub-folder of PATH as one dataset")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--tags", default="", help="comma-separated tags")
    ap.add_argument("--orgs", default="", help="comma-separated organisations allowed to see the data (empty = all)")
    ap.add_argument("--groups", default="", help="comma-separated project groups allowed to see the data")
    ap.add_argument("--uploaded-by", default="bulk-ingest")
    ap.add_argument("--schema", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    try:
        schema = load_schema(a.schema)
        schema.dataset_type(a.dataset_type)
    except SchemaError as e:
        sys.exit(f"schema error: {e}")
    root = Path(a.path)
    folders = sorted(p for p in root.iterdir() if p.is_dir()) if a.each else [root]
    if a.limit:
        folders = folders[: a.limit]
    tags = [t for t in a.tags.split(",") if t]
    orgs = [o for o in a.orgs.split(",") if o]
    groups = [g for g in a.groups.split(",") if g]
    if groups:
        c = db_connect(); cur = c.cursor()
        cur.execute("SELECT name FROM datahub_groups WHERE name = ANY(%s)", (groups,))
        missing = set(groups) - {r["name"] for r in cur.fetchall()}
        c.close()
        if missing:
            sys.exit(f"unknown group(s): {', '.join(sorted(missing))}")
    print(f"{len(folders)} dataset folder(s), type '{a.dataset_type}', {a.workers} workers, schema {schema.hash}")
    if a.dry_run:
        for f in folders[:20]:
            files = list_files(f, schema)
            print(f"  {f.name}: {len(files)} files, parameters {schema.extract_parameters(f, files)}")
        return

    done = created = errors = 0
    t0 = time.time()
    lock = threading.Lock()
    tls = threading.local()

    def work(folder: Path):
        if not hasattr(tls, "conn"):
            tls.conn, tls.s3 = db_connect(), s3_client()
        res = ingest_folder(folder, schema, a.dataset_type, tags=tags, allowed_orgs=orgs,
                            allowed_groups=groups, uploaded_by=a.uploaded_by, conn=tls.conn, s3=tls.s3, trigger_pipeline=False)
        if res[1]:
            trigger_ml(res[0], wait=True)
        return res

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(work, f): f for f in folders}
        for fut in as_completed(futs):
            with lock:
                done += 1
                try:
                    ds_id, new = fut.result()
                    created += int(new)
                except Exception as exc:
                    errors += 1
                    print(f"\n  ERROR {futs[fut].name}: {exc}")
                rate = done / max(time.time() - t0, 1e-6)
                print(f"\r  {done}/{len(folders)}  new={created}  errors={errors}  {rate:.1f}/s", end="", flush=True)
    print(f"\nDone in {time.time() - t0:.0f} s: {created} ingested, {done - created - errors} skipped, {errors} errors")


if __name__ == "__main__":
    main()
