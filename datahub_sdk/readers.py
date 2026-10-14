# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
Field readers: turn a snapshot file into a list of (time, {field name: 1-D array}).

A file may hold one snapshot (VTK, NumPy, a plain HDF5 file) or a whole time
series (XDMF temporal collection, Exodus II, HDF5 with a time axis). Vector and
tensor fields are reduced to their Euclidean norm per point or cell, so every
field is a scalar array whose length is the number of points (or cells).

    from datahub_sdk.readers import read_snapshots, reader_for
    for t, fields in read_snapshots("out/run.exo"):
        print(t, {k: v.shape for k, v in fields.items()})

Readers and their dependencies (imported lazily):

    vtk      .vtk .vtu .vtp .vts .vtr .vti   VTK (legacy and XML)      package vtk
    npy      .npy .npz                        NumPy arrays              numpy
    hdf5     .h5 .hdf5 .hdf .he5              HDF5 datasets             h5py
    xdmf     .xdmf .xmf                       XDMF 2/3 + HDF5 or XML    h5py (for HDF heavy data)
    exodus   .exo .e .ex2 .exii .gen          Exodus II (netCDF3/4)     scipy (netCDF3) or h5py (netCDF4)

The schema selects a reader with fields.reader (auto = by file extension) and
passes reader options from fields.hdf5 / fields.exodus.
"""
from __future__ import annotations

import fnmatch
import re
try:  # hardened XML parser (no entity expansion, no external entities)
    import defusedxml.ElementTree as ET
except ImportError:  # pragma: no cover - the standard parser still refuses external entities
    import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import numpy as np

Snapshot = tuple[Optional[float], dict[str, np.ndarray]]

from .formats import EXTENSIONS, READERS  # noqa: E402  (no numpy needed there)


class ReaderError(ValueError):
    pass


def reader_for(path: str | Path, reader: str = "auto") -> Optional[str]:
    """Reader id for a file; None if 'auto' and the extension is unknown."""
    if reader != "auto":
        return reader
    return EXTENSIONS.get(Path(str(path)).suffix.lower())


def _scalar(a) -> np.ndarray:
    """Flatten to one value per entity; vectors and tensors become their norm."""
    a = np.asarray(a, dtype=float)
    if a.ndim == 0:
        return a.reshape(1)
    if a.ndim == 1:
        return a
    a = a.reshape(a.shape[0], -1)
    return a[:, 0] if a.shape[1] == 1 else np.linalg.norm(a, axis=1)


def _keep(name: str, names: list[str]) -> bool:
    return not names or any(fnmatch.fnmatchcase(name, n) for n in names)


# ── public API ───────────────────────────────────────────────────────────────

def read_snapshots(path: str | Path, reader: str = "auto", names: Optional[list[str]] = None,
                   options: Optional[dict] = None, root: Optional[str | Path] = None) -> list[Snapshot]:
    """All snapshots in a file, in file order. names: optional field-name filter (globs).
    root: folder that files referenced from the file (XDMF heavy data) must lie in;
    defaults to the file's own folder."""
    path = Path(path)
    rid = reader_for(path, reader)
    if rid is None:
        raise ReaderError(f"no reader for '{path.name}' (set fields.reader or use a known extension)")
    options = options or {}
    fn = {"vtk": _read_vtk, "npy": _read_npy, "hdf5": _read_hdf5, "xdmf": _read_xdmf, "exodus": _read_exodus}.get(rid)
    if fn is None:
        raise ReaderError(f"unknown reader '{rid}'")
    opts = dict(options.get(rid, {}) or {})
    if rid == "xdmf":
        opts["_root"] = Path(root).resolve() if root else path.resolve().parent
    snaps = fn(path, opts)
    names = names or []
    out = []
    for t, fields in snaps:
        f = {k: v for k, v in fields.items() if _keep(k, names) and v.size > 1}
        if f:
            out.append((t, f))
    return out


def companions(path: str | Path, reader: str = "auto") -> list[str]:
    """Other files a snapshot file needs (XDMF heavy-data files), as paths relative to its folder."""
    path = Path(path)
    if reader_for(path, reader) != "xdmf":
        return []
    root = ET.parse(path).getroot()  # nosec B314: defusedxml is used when installed (ml-api, extras)
    out = []
    for di in root.iter():
        if _tag(di) == "DataItem" and (di.get("Format") or "XML").upper() == "HDF":
            f = (di.text or "").strip().split(":", 1)[0]
            if f and f not in out:
                out.append(f)
    return out


# ── VTK ──────────────────────────────────────────────────────────────────────

def _read_vtk(path: Path, opts: dict) -> list[Snapshot]:
    try:
        import vtk
        from vtk.util.numpy_support import vtk_to_numpy
    except ImportError as exc:  # pragma: no cover
        raise ReaderError("the vtk reader needs the 'vtk' package") from exc
    if path.suffix.lower() == ".vtk":
        r = vtk.vtkDataSetReader(); r.SetFileName(str(path))
        r.ReadAllScalarsOn(); r.ReadAllVectorsOn(); r.ReadAllTensorsOn(); r.ReadAllFieldsOn()
    else:
        r = vtk.vtkXMLGenericDataObjectReader(); r.SetFileName(str(path))
    r.Update()
    grid = r.GetOutput()
    fields: dict[str, np.ndarray] = {}
    for data in (grid.GetPointData(), grid.GetCellData()):
        for i in range(data.GetNumberOfArrays()):
            a = data.GetArray(i)
            if a is not None:
                fields.setdefault(a.GetName() or f"array_{i}", _scalar(vtk_to_numpy(a)))
    t = None
    fd = grid.GetFieldData()
    for key in ("TIME", "TimeValue", "time"):
        arr = fd.GetArray(key) if fd is not None else None
        if arr is not None and arr.GetNumberOfTuples():
            t = float(arr.GetTuple1(0)); break
    return [(t, fields)]


# ── NumPy ────────────────────────────────────────────────────────────────────

def _read_npy(path: Path, opts: dict) -> list[Snapshot]:
    if path.suffix.lower() == ".npz":
        with np.load(path) as z:
            return [(None, {k: _scalar(z[k]) for k in z.files})]
    return [(None, {path.stem.split(".")[0]: _scalar(np.load(path))})]


# ── HDF5 ─────────────────────────────────────────────────────────────────────

def _h5py():
    try:
        import h5py
        return h5py
    except ImportError as exc:  # pragma: no cover
        raise ReaderError("HDF5, XDMF and netCDF4-based Exodus files need the 'h5py' package") from exc


def _read_hdf5(path: Path, opts: dict) -> list[Snapshot]:
    """
    Options (schema fields.hdf5):
      paths:      dataset paths or globs to read (default: every numeric dataset)
      time_axis:  0 if the first axis of each dataset is time (one snapshot per index)
      time_path:  dataset holding the time values for time_axis 0 (optional)
    Without time_axis the file is one snapshot; its time is read from a root
    attribute named time, Time, TIME or t when present.
    Field names are the last path component; on a clash the full path is used.
    """
    h5py = _h5py()
    pats = [p.lstrip("/") for p in (opts.get("paths") or [])]
    time_axis = opts.get("time_axis")
    time_path = (opts.get("time_path") or "").lstrip("/")
    found: dict[str, np.ndarray] = {}
    times = None
    with h5py.File(path, "r") as f:
        def visit(name, obj):
            if isinstance(obj, h5py.Dataset) and obj.dtype.kind in "fiu" and obj.size > 1:
                if name == time_path:
                    return
                if not pats or any(fnmatch.fnmatchcase(name, p) for p in pats):
                    found[name] = obj[()]
        f.visititems(visit)
        if time_path and time_path in f:
            times = np.asarray(f[time_path][()], dtype=float).ravel()
        ftime = next((float(np.ravel(f.attrs[k])[0]) for k in ("time", "Time", "TIME", "t") if k in f.attrs), None)
    base = [n.rsplit("/", 1)[-1] for n in found]
    label = {n: (b if base.count(b) == 1 else n) for n, b in zip(found, base)}
    if time_axis is None:
        return [(ftime, {label[n]: _scalar(a) for n, a in found.items()})]
    if int(time_axis) != 0:
        raise ReaderError("fields.hdf5.time_axis: only 0 (leading axis) is supported")
    steps = {a.shape[0] for a in found.values()}
    if len(steps) > 1:
        raise ReaderError(f"datasets disagree on the number of time steps: {sorted(steps)}")
    n = steps.pop() if steps else 0
    return [(float(times[i]) if times is not None and i < len(times) else float(i),
             {label[k]: _scalar(a[i]) for k, a in found.items()}) for i in range(n)]


# ── XDMF ─────────────────────────────────────────────────────────────────────

def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _data_item(di, folder: Path, h5cache: dict) -> np.ndarray:
    fmt = (di.get("Format") or "XML").upper()
    dims = [int(x) for x in (di.get("Dimensions") or di.get("Dims") or "").split()] or None
    text = (di.text or "").strip()
    if fmt == "XML":
        arr = np.array(text.split(), dtype=float)
    elif fmt == "HDF":
        fname, _, dpath = text.partition(":")
        h5py = _h5py()
        fpath = (folder / fname).resolve()
        allowed = h5cache.get("__root__")
        if allowed is not None and not fpath.is_relative_to(allowed):
            raise ReaderError(f"XDMF heavy-data path leaves the dataset folder: {fname}")
        if fpath not in h5cache:
            if not fpath.exists():
                raise ReaderError(f"XDMF heavy-data file not found: {fname}")
            h5cache[fpath] = h5py.File(fpath, "r")
        arr = np.asarray(h5cache[fpath][dpath][()], dtype=float)
    else:
        raise ReaderError(f"XDMF DataItem format '{fmt}' is not supported (XML and HDF are)")
    if dims and arr.size == int(np.prod(dims)):
        arr = arr.reshape(dims)
    return arr


def _grid_fields(grid, folder: Path, h5cache: dict) -> dict[str, np.ndarray]:
    """Attributes of a uniform grid, or concatenated over a spatial collection."""
    gtype = (grid.get("GridType") or "Uniform")
    if gtype == "Collection":
        parts: dict[str, list[np.ndarray]] = {}
        for g in grid:
            if _tag(g) == "Grid":
                for k, v in _grid_fields(g, folder, h5cache).items():
                    parts.setdefault(k, []).append(v)
        return {k: np.concatenate(v) for k, v in parts.items()}
    out = {}
    for att in grid:
        if _tag(att) != "Attribute":
            continue
        items = [d for d in att if _tag(d) == "DataItem"]
        if not items:
            continue
        di = items[0]
        if (di.get("ItemType") or "") in ("Function", "HyperSlab", "Coordinates"):
            raise ReaderError(f"XDMF attribute '{att.get('Name')}': DataItem type {di.get('ItemType')} not supported")
        out[att.get("Name") or f"attribute_{len(out)}"] = _scalar(_data_item(di, folder, h5cache))
    return out


def _grid_time(grid) -> Optional[float]:
    for t in grid:
        if _tag(t) == "Time" and t.get("Value") is not None:
            return float(t.get("Value"))
    return None


def _read_xdmf(path: Path, opts: dict) -> list[Snapshot]:
    root = ET.parse(path).getroot()  # nosec B314: defusedxml is used when installed (ml-api, extras)
    domain = next((e for e in root.iter() if _tag(e) == "Domain"), root)
    top = [g for g in domain if _tag(g) == "Grid"]
    if not top:
        raise ReaderError("XDMF file has no Grid")
    h5cache: dict = {"__root__": opts.get("_root") or path.resolve().parent}
    snaps: list[Snapshot] = []
    try:
        for g in top:
            if g.get("GridType") == "Collection" and (g.get("CollectionType") or "").lower() == "temporal":
                steps = [s for s in g if _tag(s) == "Grid"]
                listed = None
                for t in g:  # <Time TimeType="List"><DataItem>t0 t1 ...</DataItem></Time>
                    if _tag(t) == "Time" and (t.get("TimeType") or "").lower() == "list":
                        di = next((d for d in t if _tag(d) == "DataItem"), None)
                        if di is not None:
                            listed = _data_item(di, path.parent, h5cache).ravel()
                for i, s in enumerate(steps):
                    t = _grid_time(s)
                    if t is None and listed is not None and i < len(listed):
                        t = float(listed[i])
                    snaps.append((t, _grid_fields(s, path.parent, h5cache)))
            else:
                snaps.append((_grid_time(g), _grid_fields(g, path.parent, h5cache)))
    finally:
        for k, f in h5cache.items():
            if k != "__root__":
                f.close()
    return snaps


# ── Exodus II ────────────────────────────────────────────────────────────────

def _chars(a) -> list[str]:
    """Decode a netCDF char array (n, len) or an array of fixed-length strings."""
    a = np.asarray(a)
    if a.dtype.kind in "SU" and a.ndim == 2:
        rows = [b"".join(x if isinstance(x, bytes) else str(x).encode() for x in r.tolist()) for r in a]
    elif a.dtype.kind in "SU":
        rows = [x if isinstance(x, bytes) else str(x).encode() for x in np.atleast_1d(a).tolist()]
    else:
        rows = [bytes(np.asarray(r, dtype=np.uint8).tolist()) for r in np.atleast_2d(a)]
    return [r.split(b"\x00", 1)[0].decode(errors="replace").strip() for r in rows]


class _NC:
    """Minimal read access to netCDF3 (scipy) or netCDF4/HDF5 (h5py) variables."""

    def __init__(self, path: Path):
        with open(path, "rb") as fh:
            magic = fh.read(4)
        self.kind = "nc3" if magic[:3] == b"CDF" else "h5"
        if self.kind == "nc3":
            from scipy.io import netcdf_file
            self.f = netcdf_file(str(path), "r", mmap=False)
            self.vars = self.f.variables
        else:
            self.f = _h5py().File(path, "r")
            self.vars = {k: v for k, v in self.f.items()}

    def has(self, name):
        return name in self.vars

    def get(self, name) -> np.ndarray:
        v = self.vars[name]
        return np.array(v[...] if self.kind == "h5" else v.data)

    def names(self):
        return list(self.vars)

    def close(self):
        self.f.close()


def _read_exodus(path: Path, opts: dict) -> list[Snapshot]:
    """
    Nodal variables become point fields; element variables (optional, schema
    fields.exodus.element_variables: true) are concatenated over element blocks
    in block order. Components named <base>_x, _y, _z are combined into one
    vector field <base> (reduced to its norm) unless combine_vectors is false.
    Time comes from time_whole.
    """
    nc = _NC(path)
    try:
        times = nc.get("time_whole").astype(float).ravel() if nc.has("time_whole") else np.array([])
        nsteps = len(times)
        series: dict[str, np.ndarray] = {}           # name -> (T, n)
        if nc.has("name_nod_var"):
            nod = _chars(nc.get("name_nod_var"))
            if nc.has("vals_nod_var"):                # old layout (time, var, node)
                allv = nc.get("vals_nod_var")
                for i, nm in enumerate(nod):
                    series[nm] = allv[:, i, :]
            for i, nm in enumerate(nod):
                key = f"vals_nod_var{i + 1}"
                if nc.has(key):
                    series[nm] = nc.get(key)
        if opts.get("element_variables") and nc.has("name_elem_var"):
            ev = _chars(nc.get("name_elem_var"))
            blocks = sorted({int(m.group(1)) for m in (re.match(r"vals_elem_var\d+eb(\d+)$", n) for n in nc.names()) if m})
            for i, nm in enumerate(ev):
                chunks = [nc.get(f"vals_elem_var{i + 1}eb{b}") for b in blocks if nc.has(f"vals_elem_var{i + 1}eb{b}")]
                if chunks:
                    series.setdefault(nm, np.concatenate(chunks, axis=1))
        if not series:
            return []
        if opts.get("combine_vectors", True):          # displacement_x/_y/_z -> displacement (norm)
            groups: dict[str, list[str]] = {}
            for nm in series:
                m = re.match(r"^(.*?)[_ ]?([xyzXYZ])$", nm)
                if m and m.group(1):
                    groups.setdefault(m.group(1), []).append(nm)
            for base, comps in groups.items():
                if len(comps) >= 2 and base not in series:
                    stack = np.stack([series.pop(c) for c in sorted(comps)], axis=-1)
                    series[base] = stack
        nsteps = nsteps or min(a.shape[0] for a in series.values())
        return [(float(times[t]) if t < len(times) else float(t),
                 {k: _scalar(np.asarray(a[t], dtype=float)) for k, a in series.items() if t < a.shape[0]})
                for t in range(nsteps)]
    finally:
        nc.close()


def describe(path: str | Path, reader: str = "auto", names=None, options=None) -> str:
    """Human-readable summary used by `datahub fields`."""
    snaps = read_snapshots(path, reader, names, options)
    rid = reader_for(path, reader)
    lines = [f"{Path(path).name}: reader {rid}, {len(snaps)} snapshot(s)"]
    if snaps:
        times = [t for t, _ in snaps]
        if any(t is not None for t in times):
            tt = [t for t in times if t is not None]
            lines.append(f"  time {min(tt):g} to {max(tt):g}")
        for k, v in snaps[0][1].items():
            lines.append(f"  {k:30s} {v.size:8d} values  min {np.nanmin(v):.4g}  max {np.nanmax(v):.4g}")
    dep = companions(path, reader)
    if dep:
        lines.append("  needs: " + ", ".join(dep))
    return "\n".join(lines)
