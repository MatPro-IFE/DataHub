#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
smoke_test.py - end-to-end test of a fresh DataHub instance with the example schema and demo data.

Prerequisites
  * the stack is running with config/schema.yaml copied from config/schema.example.yaml
  * demo data:  python examples/make_demo_dataset.py --out demo_data
  * the sweep cases are bulk-ingested server side (optional, see README), or pass --upload-sweep
Environment
  DATAHUB_URL (default http://localhost:8080), DATAHUB_ADMIN_PASSWORD (bootstrap password),
  DATAHUB_NEW_PASSWORD (optional), DATAHUB_ORGANIZATIONS (e.g. OrgA,OrgB) for the access test.

    python examples/smoke_test.py --data demo_data
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datahub_sdk import DataHubClient  # noqa: E402
from datahub_sdk.client import DataHubError  # noqa: E402

FAILED = 0


def check(ok, msg):
    global FAILED
    print(("  PASS  " if ok else "  FAIL  ") + msg)
    FAILED += 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="demo_data")
    ap.add_argument("--upload-sweep", action="store_true", help="upload sweep cases through the API instead of bulk ingest")
    a = ap.parse_args()
    data = Path(a.data)
    url = os.environ.get("DATAHUB_URL", "http://localhost:8080")
    boot = os.environ["DATAHUB_ADMIN_PASSWORD"]
    new = os.environ.get("DATAHUB_NEW_PASSWORD", boot + "-x")

    print("[1] authentication")
    dh = DataHubClient(url)
    try:
        info = dh.login("admin", boot)
        if info["must_change_password"]:
            dh.change_password(boot, new)
        pw = new
    except DataHubError:
        dh.login("admin", new); pw = new
    check(dh.user is not None, "admin login over the JSON API")
    schema = dh.schema()
    check(len(schema["dataset_types"]) >= 1, f"schema '{schema['instance']['name']}' served, version {schema['hash']}")

    print("[2] uploads through the API")
    ids = {}
    tag = "smoke-" + uuid.uuid4().hex[:5]
    for folder in sorted((data / "simulations").iterdir()):
        ids[folder.name] = dh.upload_folder(folder, "simulation", tags=["demo", tag])
    for folder in sorted((data / "experiments").iterdir()):
        ids[folder.name] = dh.upload_folder(folder, "experiment", tags=["demo", tag], metadata={"site": "Lab A"})
    for folder in sorted((data / "reference").iterdir()):
        ids[folder.name] = dh.upload_folder(folder, "reference", tags=["demo", tag])
    if a.upload_sweep:
        for folder in sorted((data / "sweep").iterdir()):
            ids[folder.name] = dh.upload_folder(folder, "simulation", tags=["demo", "sweep"])
    check(len(ids) >= 3, f"{len(ids)} datasets uploaded")

    run = dh.get(ids["run_001"])
    check(len(run["parts"]) == 2, f"run_001 has {len(run['parts'])} parts from sub-folders")
    classes = {f["file_class"] for p in run["parts"] for f in p["files"]} | {f["file_class"] for f in run["files"]}
    check({"field_snapshot", "time_series", "summary", "input", "log"} <= classes, f"file classes {sorted(classes)}")
    check(len(run["quantities"]) >= 2 and all(len(p["quantities"]) >= 2 for p in run["parts"]),
          f"quantities per part and per dataset: {[ (q['name'], round(q['value'], 1)) for q in run['quantities']]}")
    check(run["started_at"] is not None and run["finished_at"] is not None, "run timing parsed from the log")
    check(isinstance(run["parameters"].get("htc"), float) and isinstance(run["parameters"].get("cooling"), bool),
          "parameters typed by the schema")
    snap = [f for p in run["parts"] for f in p["files"] if f["file_class"] == "field_snapshot"]
    check(all(f["time_value"] is not None for f in snap), "snapshot times read from file headers")

    print("[3] search")
    res = dh.search(param="htc", op="ge", value=800)
    check(all(d["parameters"]["htc"] >= 800 for d in res["items"]) and res["total"] > 0, f"parameter filter htc >= 800: {res['total']} hits")
    res = dh.search(quantity="peak_temperature", qop="gt", qvalue=700)
    check(res["total"] > 0, f"quantity filter peak_temperature > 700: {res['total']} hits")
    n_tag = dh.search(tag=tag)["total"]
    check(n_tag == sum(1 for k in ids if not k.startswith("case")), f"tag filter: {n_tag} datasets")
    check(dh.search(dataset_type="experiment")["total"] >= 1, "type filter")

    print("[4] download and integrity")
    f0 = snap[0]
    with tempfile.TemporaryDirectory() as tmp:
        p = dh.download_file(f0["id"], Path(tmp) / "x.vtk")
        check(hashlib.sha256(p.read_bytes()).hexdigest() == f0["sha256"], "downloaded file matches stored SHA-256")
        z = dh.download_archive(ids["exp_01"], Path(tmp) / "exp.zip")
        names = zipfile.ZipFile(z).namelist()
        check(any(n.endswith("datahub_metadata.json") for n in names) and any(n.endswith("photo.png") for n in names),
              f"dataset archive with metadata ({len(names)} entries)")

    print("[5] similarity, embeddings, graph")
    sim_ids = [i for k, i in ids.items() if k.startswith("run_")]
    for _ in range(2):  # second pass embeds datasets buffered before the first PCA fit
        emb = [dh.embed_fields(i) for i in sim_ids]
    n_emb = sum(1 for e in emb for x in e["embeddings"] if "n_components" in x)
    check(n_emb > 0, f"{n_emb} field embeddings (fields: {sorted({x['field'] for e in emb for x in e['embeddings']})})")
    sp = dh.similar(ids["run_001"], n=3)
    check(len(sp) > 0, f"parameter neighbours: {[(s['name'], round(s['score'], 3)) for s in sp]}")
    sf = dh.similar_fields(ids["run_001"], n=3)
    check(len(sf) > 0, f"field neighbours: {[(s['name'], s['field'], round(s['score'], 3)) for s in sf]}")
    nb = dh.neighbors(ids["run_001"])
    check(any(n["kind"] == "resource" for n in nb) and any(n["kind"] == "part" for n in nb),
          f"graph neighbours include shared resource and parts ({len(nb)})")

    if (data / "formats").exists():
        print("[5b] field readers: XDMF + HDF5, Exodus II, plain HDF5")
        ref = {x["field"]: x for x in dh.embed_fields(ids["run_001"])["embeddings"]}
        for fmt in ("xdmf", "exodus", "hdf5"):
            folder = data / "formats" / f"run_001_{fmt}"
            if not folder.exists():
                continue
            fid = ids[folder.name] = dh.upload_folder(folder, "simulation", tags=["demo", tag, "formats"])
            r = dh.embed_fields(fid)
            fields = sorted({x["field"] for x in r["embeddings"] if "n_components" in x})
            check(fields == ["displacement", "temperature"] and not r.get("errors") and len(r["embeddings"]) == 4,
                  f"{fmt}: {fields} embedded in 2 parts, {max(x.get('snapshots', 0) for x in r['embeddings'])} snapshots each, no read errors")
            sf = {(x["name"], x["field"]): x["score"] for x in dh.similar_fields(fid, field="temperature", n=20)}
            score = sf.get(("run_001", "temperature"))
            check(score is not None and score > 0.999, f"{fmt}: temperature embedding matches the VTK original (cosine {score})")

    print("[6] parameter sweep")
    sw = dh.create_sweep(f"smoke {tag}", {"htc": [300, 1800], "load": [0.02, 0.28], "scale": [0.7, 1.3]},
                         objective_quantity="peak_temperature", objective_goal="minimize")
    cases = [d for d in dh.iter_datasets(q="case")] if not a.upload_sweep else [{"id": v} for k, v in ids.items() if k.startswith("case")]
    for d in cases:
        dh.link(sw["id"], d["id"])
    for k, i in ids.items():
        if k.startswith("run_"):
            dh.link(sw["id"], i)
    det = dh.sweep(sw["id"])
    cov = det["coverage"]
    check(det["coverage"]["n_linked"] == len(det["datasets"]),
          f"coverage: {cov['n_inside']} runs inside, extent {cov['extent_score']:.2f}, discrepancy {cov['discrepancy']}, fill {cov['fill_distance']}")
    for m in ("sobol", "lhs", "gp_uncertainty", "gp_ei"):
        r = dh.suggest(sw["id"], 4, m)
        check(len(r["suggestions"]) == 4, f"{m}: 4 suggestions (method used: {r['method']})")

    print("[7] access control")
    orgs = [o for o in os.environ.get("DATAHUB_ORGANIZATIONS", "").split(",") if o]
    s = requests.Session()
    s.post(f"{url}/login", data={"username": "admin", "password": pw})
    if len(orgs) >= 2:
        secret = dh.upload_folder(data / "experiments" / "exp_02", "experiment", name=f"restricted-{tag}",
                                  allowed_orgs=[orgs[0]], tags=[tag])
        vname, vpw = f"viewer_{tag}", "Viewer-pass-123"
        s.post(f"{url}/admin/users", data={"username": vname, "password": vpw, "role": "viewer", "organization": orgs[1]})
        v = DataHubClient(url, vname, vpw)
        v.change_password(vpw, vpw + "x")
        try:
            v.get(secret); ok = False
        except DataHubError as e:
            ok = "403" in str(e)
        check(ok, f"{orgs[1]} viewer gets 403 on an {orgs[0]}-only dataset")
        check(v.get(ids["run_001"])["id"] == ids["run_001"], "viewer can open an unrestricted dataset")
        check(all(d["id"] != secret for d in v.iter_datasets(tag=tag)), "restricted dataset hidden from viewer search")
        try:
            v.upload_folder(data / "reference" / "mesh_block_coarse", "reference", name=f"x-{tag}"); ok = False
        except DataHubError as e:
            ok = "403" in str(e)
        check(ok, "viewer cannot upload")
        check(dh.get(secret)["id"] == secret, "admin can open the restricted dataset")
        dh.delete(secret)
        try:
            dh.get(secret); ok = False
        except DataHubError as e:
            ok = "404" in str(e)
        check(ok, "deleted dataset is gone")

    print("[8] project groups and sharing links")
    if len(orgs) >= 2:
        group = f"proj-{tag}"
        dh.create_group(group, "smoke-test project")
        mname, oname, pwd = f"member_{tag}", f"outsider_{tag}", "Member-pass-123"
        s.post(f"{url}/admin/users", data={"username": mname, "password": pwd, "role": "uploader", "organization": orgs[1]})
        s.post(f"{url}/admin/users", data={"username": oname, "password": pwd, "role": "viewer", "organization": orgs[1]})
        m = DataHubClient(url, mname, pwd); m.change_password(pwd, pwd + "x")
        o = DataHubClient(url, oname, pwd); o.change_password(pwd, pwd + "x")
        dh.add_group_member(group, mname)

        def denied(fn, code="403"):
            try:
                fn(); return False
            except DataHubError as e:
                return code in str(e)
        gds = m.upload_folder(data / "experiments" / "exp_03", "experiment", name=f"group-{tag}",
                              allowed_groups=[group], tags=[tag])
        check(m.get(gds)["allowed_groups"] == [group], "group member uploads a dataset restricted to the group")
        check(denied(lambda: o.get(gds)), "user outside the group (same organisation) gets 403")
        check(all(d["id"] != gds for d in o.iter_datasets(tag=tag)), "group dataset hidden from outsider search")
        check(denied(lambda: m.upload_folder(data / "reference" / "mesh_block_coarse", "reference",
                                             name=f"y-{tag}", allowed_groups=["no-such-group"]), "400"),
              "unknown group refused at upload")
        dh.add_group_member(group, mname, manager=True)
        m.add_group_member(group, oname)
        check(o.get(gds)["id"] == gds, "group manager adds a member, who can then open the dataset")
        m.remove_group_member(group, oname)
        check(denied(lambda: o.get(gds)), "removed member loses access")
        check(denied(lambda: dh.delete_group(group), "409"), "group that still restricts a dataset cannot be deleted")
        m.set_access(gds, allowed_orgs=[orgs[1]], allowed_groups=[])
        check(o.get(gds)["id"] == gds, "uploader widens access to the organisation")
        m.set_access(gds, allowed_orgs=[], allowed_groups=[group])
        check(denied(lambda: o.get(gds)) and denied(lambda: o.share(gds)), "access narrowed again; outsider cannot share")

        link = m.share(gds, days=1, max_downloads=2, note="reviewer")
        anon = requests.Session()
        page = anon.get(link["url"])
        check(page.status_code == 200 and f"group-{tag}" in page.text and page.headers.get("Referrer-Policy") == "no-referrer",
              "sharing link opens without login (no-referrer, noindex)")
        from datahub_sdk import download_shared
        with tempfile.TemporaryDirectory() as tmp:
            z = download_shared(link["url"], tmp, archive=True)[0]
            check(any(n.endswith("measurements.csv") for n in zipfile.ZipFile(z).namelist()), "archive through the link (download 1 of 2)")
            meta = anon.get(link["url"] + "/meta").json()
            f = next(x for x in meta["files"] if x["relpath"] == "measurements.csv")
            body = anon.get(f"{link['url']}/files/{f['id']}").content
            check(hashlib.sha256(body).hexdigest() == f["sha256"], "file through the link matches its SHA-256 (download 2 of 2)")
            r3 = anon.get(f"{link['url']}/files/{f['id']}")
            check(r3.status_code == 410, f"third download refused: HTTP {r3.status_code}")
        info = next(x for x in m.shares(gds) if x["id"] == link["id"])
        check(info["download_count"] == 2 and info["status"] == "used up", f"link status '{info['status']}', {info['download_count']} downloads logged")
        l2 = m.share(gds, days=1)
        m.revoke_share(l2["id"])
        check(anon.get(l2["url"] + "/meta").status_code == 410, "revoked link refused (HTTP 410)")
        l3 = m.share(gds, days=1e-5)
        import time; time.sleep(2)
        check(anon.get(l3["url"] + "/meta").status_code == 410, "expired link refused (HTTP 410)")
        check(denied(lambda: m.share(gds, days=10000), "400"), "lifetime above the configured maximum refused")
        check(anon.get(link["url"][:-6] + "abcdef/meta").status_code in (404, 429), "unknown token refused")
        m.set_access(gds, allowed_groups=[])
        dh.delete(gds)
        dh.delete_group(group)
        check(group not in [g["name"] for g in dh.groups()], "group deleted after its dataset")

    print("[9] security controls")
    r = requests.post(f"{url}/login", data={"username": "admin", "password": pw, "next": "//evil.example/x"},
                      allow_redirects=False)
    check(r.headers.get("location") == "/", f"open redirect after login refused (Location: {r.headers.get('location')})")
    check(requests.get(f"{url}/api/docs").status_code in (401, 307) and requests.get(f"{url}/api/openapi.json").status_code == 401,
          "API documentation requires login")
    csp = requests.get(f"{url}/login").headers.get("content-security-policy", "")
    check("http" not in csp and "jsdelivr" not in csp and "googleapis" not in csp, "CSP allows no third-party origins")
    c = requests.Session(); c.post(f"{url}/api/login", json={"username": "admin", "password": pw})
    saved = dict(c.cookies); c.get(f"{url}/logout")
    check(requests.get(f"{url}/api/datasets", cookies=saved).status_code == 401, "session cookie is useless after logout")
    tmp_user, tmp_pw = f"tmp_{tag}", "Temporary-pass-123"
    s.post(f"{url}/admin/users", data={"username": tmp_user, "password": tmp_pw, "role": "viewer"})
    t = requests.Session(); info = t.post(f"{url}/api/login", json={"username": tmp_user, "password": tmp_pw}).json()
    check(info.get("must_change_password") and t.get(f"{url}/api/datasets").status_code == 403,
          "temporary password only allows changing the password")
    check(t.post(f"{url}/api/password", json={"current_password": tmp_pw, "new_password": "short"}).status_code == 400,
          "password shorter than the minimum refused")
    x = s.post(f"{url}/groups", data={"name": f"x-{tag}"}, headers={"Origin": "https://evil.example"}, allow_redirects=False)
    check(x.status_code == 403, f"cross-site POST refused (HTTP {x.status_code})")
    ml = os.environ.get("ML_API_URL", "http://127.0.0.1:8090")
    try:
        code = requests.get(f"{ml}/sweeps", timeout=5).status_code
        check(code == 401, f"ml-api refuses requests without the shared token (HTTP {code})")
    except requests.RequestException:
        print("  SKIP  ml-api not reachable from here")
    for rel in ("../../etc/passwd", "a/../../b"):
        u = s.post(f"{url}/api/upload/start", data={"name": f"trav-{tag}", "dataset_type": "reference"}).json()["upload_id"]
        r = s.post(f"{url}/api/upload/file", data={"upload_id": u, "relpath": rel}, files={"file": ("f", b"x")})
        s.post(f"{url}/api/upload/abort", data={"upload_id": u})
        check(r.status_code == 400, f"path traversal in upload refused: {rel}")

    print("\nRESULT:", "all checks passed" if FAILED == 0 else f"{FAILED} check(s) failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
