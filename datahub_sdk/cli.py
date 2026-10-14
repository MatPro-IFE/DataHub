# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
datahub - command-line tool.

    datahub check-schema config/schema.yaml
    datahub preview  path/to/run --schema config/schema.yaml --type simulation
    datahub fields   path/to/result.xdmf [--schema config/schema.yaml]    (what the field reader sees)
    datahub upload   path/to/run --type simulation --tags a,b            (DATAHUB_URL/USER/PASSWORD)
    datahub upload   path/to/runs --each --type simulation --workers 4
    datahub search   --type simulation --param htc --op ge --value 800
    datahub download <dataset-id> --out downloads/
    datahub share    <dataset-id> --days 7 --max-downloads 20 --note "for reviewer"
    datahub get-shared https://datahub.example.org/s/<token> --out downloads/   (no account needed)
    datahub groups
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .schema import SchemaError, load_schema


def main(argv=None):
    ap = argparse.ArgumentParser(prog="datahub", description="DataHub command-line tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check-schema", help="validate a project schema file")
    c.add_argument("schema")

    p = sub.add_parser("preview", help="show how a folder would be ingested (no upload)")
    p.add_argument("folder"); p.add_argument("--schema", default=None); p.add_argument("--type", required=True)

    fl = sub.add_parser("fields", help="show the fields and time steps a reader finds in a snapshot file")
    fl.add_argument("file"); fl.add_argument("--schema", default=None)
    fl.add_argument("--reader", default=None, help="override the schema reader (auto, vtk, npy, hdf5, xdmf, exodus)")

    u = sub.add_parser("upload", help="upload a folder (or each sub-folder with --each)")
    u.add_argument("folder"); u.add_argument("--type", required=True); u.add_argument("--each", action="store_true")
    u.add_argument("--tags", default=""); u.add_argument("--orgs", default=""); u.add_argument("--notes", default="")
    u.add_argument("--groups", default="", help="comma-separated project groups allowed to see the data")
    u.add_argument("--workers", type=int, default=4, help="parallel datasets with --each")
    u.add_argument("--skip-existing", action="store_true")

    s = sub.add_parser("search", help="search datasets")
    for k in ("q", "type", "tag", "param", "op", "value", "quantity", "qop", "qvalue"):
        s.add_argument(f"--{k}", default="")
    s.add_argument("--limit", type=int, default=20)

    d = sub.add_parser("download", help="download all files of a dataset")
    d.add_argument("dataset_id"); d.add_argument("--out", default="."); d.add_argument("--file-class", default=None)

    sh = sub.add_parser("share", help="create a time-limited sharing link for a dataset")
    sh.add_argument("dataset_id"); sh.add_argument("--days", type=float, default=7)
    sh.add_argument("--max-downloads", type=int, default=None); sh.add_argument("--note", default="")

    gs = sub.add_parser("get-shared", help="download a dataset through a sharing link (no login)")
    gs.add_argument("link"); gs.add_argument("--out", default="."); gs.add_argument("--zip", action="store_true")

    sub.add_parser("groups", help="list your project groups")

    a = ap.parse_args(argv)

    if a.cmd == "get-shared":
        from .client import download_shared
        paths = download_shared(a.link, a.out, archive=a.zip)
        print(f"{len(paths)} file(s) written under {Path(a.out).resolve()}")
        return

    if a.cmd == "check-schema":
        try:
            sch = load_schema(a.schema)
        except SchemaError as e:
            sys.exit(f"INVALID: {e}")
        vec, keys = sch.parameter_vector({})
        print(f"OK  {sch.instance_name}: {len(sch.dataset_types)} dataset types, {len(sch.parameters)} parameters "
              f"({len(keys)}-dim vector), {len(sch.file_classes)} file classes, {len(sch.quantities)} quantities, "
              f"hash {sch.hash}")
        return

    if a.cmd == "preview":
        sch = load_schema(a.schema)
        root = Path(a.folder)
        files = [str(f.relative_to(root)).replace("\\", "/") for f in sorted(root.rglob("*")) if f.is_file()]
        print(f"dataset '{root.name}'  type {a.type}")
        print("parameters:", json.dumps(sch.extract_parameters(root, files), default=str))
        counts = {}
        triples = []
        for f in files:
            if sch.skip(f):
                continue
            cls = sch.classify(f); part = sch.part_of(f, a.type)
            key = (part[1] if part else "(dataset)", cls)
            counts[key] = counts.get(key, 0) + 1
            if (root / f).stat().st_size < 64 << 20:
                triples.append((f, cls, (root / f).read_bytes()))
        for (part, cls), n in sorted(counts.items()):
            print(f"  {part:20s} {cls:20s} {n:5d} file(s)")
        print("quantities:", sch.extract_quantities(triples))
        return

    if a.cmd == "fields":
        from .readers import ReaderError, describe
        F = load_schema(a.schema).fields if (a.schema or Path("config/schema.yaml").exists()) else {}
        try:
            print(describe(a.file, a.reader or F.get("reader", "auto"), F.get("names") or [],
                           {"hdf5": F.get("hdf5") or {}, "exodus": F.get("exodus") or {}}))
        except ReaderError as e:
            sys.exit(f"ERROR: {e}")
        return

    from .client import DataHubClient
    dh = DataHubClient.from_env()
    if dh.user is None:
        sys.exit("set DATAHUB_URL, DATAHUB_USER and DATAHUB_PASSWORD")

    if a.cmd == "upload":
        root = Path(a.folder)
        folders = sorted(p for p in root.iterdir() if p.is_dir()) if a.each else [root]
        tags = [t for t in a.tags.split(",") if t]; orgs = [o for o in a.orgs.split(",") if o]
        groups = [g for g in a.groups.split(",") if g]
        existing = set()
        if a.skip_existing:
            existing = {d["name"] for d in dh.iter_datasets(dataset_type=a.type)}

        def one(f: Path):
            if f.name in existing:
                return f.name, "skipped"
            return f.name, dh.upload_folder(f, a.type, tags=tags, allowed_orgs=orgs, allowed_groups=groups,
                                            notes=a.notes or None,
                                            workers=1 if a.each else 4)
        with ThreadPoolExecutor(max_workers=a.workers if a.each else 1) as pool:
            for fut in as_completed([pool.submit(one, f) for f in folders]):
                try:
                    print("%s  %s" % fut.result())
                except Exception as exc:
                    print(f"ERROR {exc}")
    elif a.cmd == "search":
        res = dh.search(q=a.q, dataset_type=a.type, tag=a.tag, param=a.param, op=a.op, value=a.value,
                        quantity=a.quantity, qop=a.qop, qvalue=a.qvalue, limit=a.limit)
        print(f"{res['total']} dataset(s)")
        for d in res["items"]:
            print(f"  {d['id']}  {d['dataset_type']:12s} {d['name']}")
    elif a.cmd == "download":
        for pth in dh.download(a.dataset_id, a.out, a.file_class):
            print(pth)
    elif a.cmd == "share":
        link = dh.share(a.dataset_id, a.days, a.max_downloads, a.note)
        print(link["url"])
        print(f"expires {link['expires_at']}" + (f", at most {link['max_downloads']} downloads" if link.get("max_downloads") else ""))
    elif a.cmd == "groups":
        for g in dh.groups():
            members = ", ".join(m["username"] + ("*" if m["is_manager"] else "") for m in g["members"])
            print(f"{g['name']:24s} {g['datasets']:5d} datasets  members: {members or '-'}")


if __name__ == "__main__":
    main()
