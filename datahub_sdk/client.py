# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
datahub_sdk.client - Python client for the DataHub HTTP API.

The client logs in with a DataHub user account, so every call is subject to
the same role and organisation checks as the web interface.

    from datahub_sdk import DataHubClient
    dh = DataHubClient("http://localhost:8080", "alice", "password")
    ds_id = dh.upload_folder("runs/run_001", dataset_type="simulation", tags=["batch-1"])
    hits = dh.search(param="htc", op="ge", value=800)
    dh.download(hits["items"][0]["id"], "downloads/")
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

import requests


class DataHubError(RuntimeError):
    pass


class DataHubClient:
    def __init__(self, url: Optional[str] = None, username: Optional[str] = None, password: Optional[str] = None,
                 timeout: float = 300, verify: bool | str = True):
        self.url = (url or os.environ.get("DATAHUB_URL", "http://localhost:8080")).rstrip("/")
        self.s = requests.Session()
        self.s.verify = verify
        self.timeout = timeout
        self.user = None
        u = username or os.environ.get("DATAHUB_USER")
        p = password or os.environ.get("DATAHUB_PASSWORD")
        if u and p:
            self.login(u, p)

    @classmethod
    def from_env(cls) -> "DataHubClient":
        """DATAHUB_URL, DATAHUB_USER, DATAHUB_PASSWORD."""
        return cls()

    # ── plumbing ──────────────────────────────────────────────────────────
    def _req(self, method: str, path: str, **kw):
        r = self.s.request(method, f"{self.url}{path}", timeout=kw.pop("timeout", self.timeout), **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:300]
            raise DataHubError(f"{method} {path}: HTTP {r.status_code}: {detail}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r

    # ── session ───────────────────────────────────────────────────────────
    def login(self, username: str, password: str) -> dict:
        self.user = self._req("POST", "/api/login", json={"username": username, "password": password})
        return self.user

    def change_password(self, current: str, new: str) -> dict:
        return self._req("POST", "/api/password", json={"current_password": current, "new_password": new})

    def schema(self) -> dict:
        return self._req("GET", "/api/schema")

    # ── catalogue ─────────────────────────────────────────────────────────
    def search(self, q: str = "", dataset_type: str = "", tag: str = "", status: str = "", param: str = "",
               op: str = "", value=None, quantity: str = "", qop: str = "", qvalue=None,
               limit: int = 50, offset: int = 0) -> dict:
        """Returns {"total": n, "items": [...]}. op/qop: eq ne gt ge lt le."""
        return self._req("GET", "/api/datasets", params={
            "q": q, "type": dataset_type, "tag": tag, "status": status, "param": param, "op": op,
            "value": "" if value is None else value, "quantity": quantity, "qop": qop,
            "qvalue": "" if qvalue is None else qvalue, "limit": limit, "offset": offset})

    def iter_datasets(self, page: int = 200, **filters) -> Iterable[dict]:
        offset = 0
        while True:
            res = self.search(limit=page, offset=offset, **filters)
            yield from res["items"]
            offset += page
            if offset >= res["total"]:
                return

    def get(self, dataset_id: str) -> dict:
        return self._req("GET", f"/api/datasets/{dataset_id}")

    def delete(self, dataset_id: str) -> dict:
        return self._req("DELETE", f"/api/datasets/{dataset_id}")

    # ── upload ────────────────────────────────────────────────────────────
    def upload_folder(self, folder: str | Path, dataset_type: str, name: Optional[str] = None,
                      tags: Optional[list[str]] = None, notes: Optional[str] = None, metadata: Optional[dict] = None,
                      allowed_orgs: Optional[list[str]] = None, allowed_groups: Optional[list[str]] = None,
                      status: str = "completed", workers: int = 4) -> str:
        """Upload a folder (or a single file) as one dataset; returns the dataset id."""
        folder = Path(folder)
        files = [folder] if folder.is_file() else sorted(p for p in folder.rglob("*") if p.is_file())
        base = folder.parent if folder.is_file() else folder
        sid = self._req("POST", "/api/upload/start", data={
            "name": name or (folder.stem if folder.is_file() else folder.name), "dataset_type": dataset_type,
            "tags": ",".join(tags or []), "notes": notes or "", "metadata": json.dumps(metadata or {}),
            "allowed_orgs": ",".join(allowed_orgs or []), "allowed_groups": ",".join(allowed_groups or []),
            "status": status})["upload_id"]
        try:
            def send(p: Path):
                rel = str(p.relative_to(base)).replace("\\", "/")
                with p.open("rb") as fh:
                    self._req("POST", "/api/upload/file", data={"upload_id": sid, "relpath": rel},
                              files={"file": (p.name, fh)})
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                list(pool.map(send, files))
            return self._req("POST", "/api/upload/commit", data={"upload_id": sid}, timeout=3600)["dataset_id"]
        except Exception:
            try:
                self._req("POST", "/api/upload/abort", data={"upload_id": sid})
            except DataHubError:
                pass
            raise

    # ── access: project groups ────────────────────────────────────────────
    def groups(self) -> list[dict]:
        """Groups with members (admins see all, others the groups they belong to)."""
        return self._req("GET", "/api/groups")

    def create_group(self, name: str, description: str = "") -> dict:
        """Admin only."""
        return self._req("POST", "/api/groups", json={"name": name, "description": description})

    def delete_group(self, name: str) -> dict:
        """Admin only; refused while the group still restricts datasets."""
        return self._req("DELETE", f"/api/groups/{name}")

    def add_group_member(self, group: str, username: str, manager: Optional[bool] = None) -> dict:
        """Admins and group managers; only admins can appoint managers."""
        return self._req("POST", f"/api/groups/{group}/members", json={"username": username, "is_manager": manager})

    def remove_group_member(self, group: str, username: str) -> dict:
        return self._req("DELETE", f"/api/groups/{group}/members/{username}")

    def set_access(self, dataset_id: str, allowed_orgs: Optional[list[str]] = None,
                   allowed_groups: Optional[list[str]] = None) -> dict:
        """Change who can see a dataset (uploader or admin). None leaves a list unchanged; [] clears it."""
        return self._req("PATCH", f"/api/datasets/{dataset_id}/access",
                         json={"allowed_orgs": allowed_orgs, "allowed_groups": allowed_groups})

    # ── access: time-limited sharing links ────────────────────────────────
    def share(self, dataset_id: str, days: float = 7, max_downloads: Optional[int] = None, note: str = "") -> dict:
        """Create a sharing link (uploader or admin). Returns {"id", "url", "expires_at", ...};
        the URL is only returned here, keep it."""
        return self._req("POST", f"/api/datasets/{dataset_id}/shares",
                         json={"days": days, "max_downloads": max_downloads, "note": note})

    def shares(self, dataset_id: str) -> list[dict]:
        return self._req("GET", f"/api/datasets/{dataset_id}/shares")

    def revoke_share(self, share_id: str) -> dict:
        return self._req("DELETE", f"/api/shares/{share_id}")

    # ── download ──────────────────────────────────────────────────────────
    def download_file(self, file_id: str, dest: str | Path) -> Path:
        dest = Path(dest)
        r = self.s.get(f"{self.url}/api/download/{file_id}", stream=True, timeout=self.timeout)
        if r.status_code >= 400:
            raise DataHubError(f"download {file_id}: HTTP {r.status_code}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
        return dest

    def download(self, dataset_id: str, dest_dir: str | Path, file_class: Optional[str] = None) -> list[Path]:
        """Download the files of a dataset (optionally one file class) keeping the folder layout."""
        ds = self.get(dataset_id)
        files = ds["files"] + [f for p in ds["parts"] for f in p["files"]]
        out = []
        for f in files:
            if file_class and f["file_class"] != file_class:
                continue
            out.append(self.download_file(f["id"], Path(dest_dir) / ds["name"] / f["relpath"]))
        return out

    def download_archive(self, dataset_id: str, dest: str | Path) -> Path:
        r = self.s.get(f"{self.url}/api/datasets/{dataset_id}/archive.zip", stream=True, timeout=self.timeout)
        if r.status_code >= 400:
            raise DataHubError(f"archive {dataset_id}: HTTP {r.status_code}")
        dest = Path(dest)
        with dest.open("wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
        return dest

    # ── analysis ──────────────────────────────────────────────────────────
    def similar(self, dataset_id: str, n: int = 10) -> list[dict]:
        return self._req("GET", f"/api/similar/params/{dataset_id}", params={"n": n})

    def similar_fields(self, dataset_id: str, field: str = "", n: int = 10) -> list[dict]:
        return self._req("GET", f"/api/similar/fields/{dataset_id}", params={"field": field, "n": n})

    def embed_fields(self, dataset_id: str) -> dict:
        return self._req("POST", f"/api/embed/{dataset_id}", timeout=1800)

    def neighbors(self, dataset_id: str) -> list[dict]:
        return self._req("GET", f"/api/graph/neighbors/{dataset_id}")

    def create_sweep(self, name: str, param_ranges: dict, objective_quantity: Optional[str] = None,
                     objective_goal: Optional[str] = None, description: Optional[str] = None) -> dict:
        return self._req("POST", "/api/sweeps", json={"name": name, "param_ranges": param_ranges,
                         "objective_quantity": objective_quantity, "objective_goal": objective_goal,
                         "description": description})

    def sweep(self, sweep_id: str) -> dict:
        return self._req("GET", f"/api/sweeps/{sweep_id}")

    def link(self, sweep_id: str, dataset_id: str) -> dict:
        return self._req("POST", f"/api/sweeps/{sweep_id}/link/{dataset_id}")

    def suggest(self, sweep_id: str, n: int = 5, method: str = "sobol") -> dict:
        """method: sobol | lhs | random | gp_uncertainty | gp_ei"""
        return self._req("POST", f"/api/sweeps/{sweep_id}/suggest", params={"n": n, "method": method})


def download_shared(link: str, dest_dir: str | Path = ".", archive: bool = False, timeout: float = 300) -> list[Path]:
    """Download a dataset through a sharing link, no account needed.

    archive=True fetches one ZIP (counts as one download); otherwise every file is
    fetched separately (each counts against the link's download limit).
    """
    link = link.rstrip("/")
    s = requests.Session()
    meta = s.get(f"{link}/meta", timeout=timeout)
    if meta.status_code >= 400:
        raise DataHubError(f"sharing link: HTTP {meta.status_code}: {meta.json().get('detail', '') if meta.headers.get('content-type', '').startswith('application/json') else ''}")
    meta = meta.json()
    dest = Path(dest_dir) / meta["name"]
    out = []
    if archive:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = dest.parent / f"{meta['name']}.zip"
        with s.get(f"{link}/archive.zip", stream=True, timeout=timeout) as r:
            if r.status_code >= 400:
                raise DataHubError(f"sharing link archive: HTTP {r.status_code}")
            with target.open("wb") as fh:
                for chunk in r.iter_content(1 << 16):
                    fh.write(chunk)
        return [target]
    for f in meta["files"]:
        target = dest / f["relpath"]
        target.parent.mkdir(parents=True, exist_ok=True)
        with s.get(f"{link}/files/{f['id']}", stream=True, timeout=timeout) as r:
            if r.status_code >= 400:
                raise DataHubError(f"sharing link file {f['relpath']}: HTTP {r.status_code}")
            with target.open("wb") as fh:
                for chunk in r.iter_content(1 << 16):
                    fh.write(chunk)
        out.append(target)
    return out
