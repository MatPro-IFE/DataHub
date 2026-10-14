#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
make_demo_dataset.py - generate a synthetic data set that matches config/schema.example.yaml.

The data describe a fictitious parametric cooling study of a heated block. All
values come from closed-form expressions; they have no physical meaning beyond
being smooth functions of the parameters. Use them to try every DataHub feature
without real project data.

Output (default ./demo_data):
  simulations/run_<NNN>/                    parameters.json, run.log, stage_1/, stage_2/
      stage_<k>/field_<NNNN>.vtk            legacy VTK snapshots (point fields temperature, displacement)
      stage_<k>/probes.csv                  probe time series
      stage_<k>/summary.json                scalar results
  sweep/case<NNN>__htc=..__load=..__cooling=..__scale=..
                                            parameters in the folder name, run.cfg, flat files
  experiments/exp_<NN>/                     measurements.csv, photo.png, summary.json
  reference/mesh_block_coarse/              block_coarse.msh
  formats/run_001_<fmt>/                    run_001 again with fields as XDMF + HDF5, Exodus II or
                                            plain HDF5 (one file per snapshot), to test the field readers
                                            (needs h5py; skipped with --no-formats)

    python examples/make_demo_dataset.py --out demo_data --runs 6 --cases 32 --experiments 3
"""
from __future__ import annotations

import argparse
import json
import struct
import warnings
import zlib
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore", message=".*balance properties.*")
NX, NY, NZ = 9, 7, 5
N_FRAMES = 6
T_END = 600.0
GEOMS = ["block", "plate", "cylinder"]


def grid():
    x = np.linspace(-0.05, 0.05, NX); y = np.linspace(-0.10, 0.10, NY); z = np.linspace(-0.04, 0.04, NZ)
    X, Y, Z = np.meshgrid(x, y, z, indexing="ij")
    pts = np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])
    idx = lambda i, j, k: (i * NY + j) * NZ + k
    cells = [[idx(i, j, k), idx(i + 1, j, k), idx(i + 1, j + 1, k), idx(i, j + 1, k),
              idx(i, j, k + 1), idx(i + 1, j, k + 1), idx(i + 1, j + 1, k + 1), idx(i, j + 1, k + 1)]
             for i in range(NX - 1) for j in range(NY - 1) for k in range(NZ - 1)]
    return pts, np.array(cells)


def fields(pts, t, p):
    x, y, z = pts.T
    x0 = 0.03 * (p["load"] - 0.15) / 0.15
    r2 = ((x - x0) / 0.05) ** 2 + (z / 0.04) ** 2
    tau = 60.0 + 4.0e4 / p["htc"] * (1.4 if p["cooling"] else 1.0) * p["scale"]
    shape = (1.0 - 0.35 * r2) * (1.0 + 0.1 * np.sin(np.pi * y / 0.1))
    if p["cooling"]:
        shape *= 1.0 - 0.25 * (y + 0.1) / 0.2
    wave = 15.0 * np.sin(np.pi * t / T_END) * np.cos(2 * np.pi * z / 0.08) * p["htc"] / 1500.0
    T = 300.0 + (p["inlet_temperature"] - 300.0) * np.exp(-t / tau) * shape + wave
    s = 2.4e-5 * (p["inlet_temperature"] - T) * p["scale"]
    U = np.column_stack([s * x * (1 + p["load"]), s * y * 0.5, s * z - p["load"] * 1e-4 * (1 - r2)])
    return T, U, tau


def write_vtk(path, pts, cells, t, T, U):
    with path.open("w") as f:
        f.write(f"# vtk DataFile Version 3.0\nDataHub demo snapshot\nASCII\nDATASET UNSTRUCTURED_GRID\n")
        f.write(f"FIELD FieldData 1\nTIME 1 1 double\n{t:.6f}\nPOINTS {len(pts)} float\n")
        np.savetxt(f, pts, fmt="%.6e")
        f.write(f"CELLS {len(cells)} {len(cells) * 9}\n")
        np.savetxt(f, np.column_stack([np.full(len(cells), 8), cells]), fmt="%d")
        f.write(f"CELL_TYPES {len(cells)}\n"); np.savetxt(f, np.full(len(cells), 12), fmt="%d")
        f.write(f"POINT_DATA {len(pts)}\nSCALARS temperature float 1\nLOOKUP_TABLE default\n")
        np.savetxt(f, T, fmt="%.5f")
        f.write("VECTORS displacement float\n"); np.savetxt(f, U, fmt="%.6e")


def write_run_files(folder: Path, p: dict, rng, noise=0.0):
    folder.mkdir(parents=True, exist_ok=True)
    pts, cells = grid()
    peak = 0.0
    for n, t in enumerate(np.linspace(0, T_END, N_FRAMES)):
        T, U, tau = fields(pts, t, p)
        peak = max(peak, float(T.max()))
        write_vtk(folder / f"field_{n:04d}.vtk", pts, cells, t, T, U)
    probes = pts[rng.choice(len(pts), 4, replace=False)]
    ts = np.linspace(0, T_END, 61)
    rows = ["time," + ",".join(f"probe_{i+1}" for i in range(4))]
    for t in ts:
        T, _, _ = fields(probes, t, p)
        T = T + rng.normal(0, noise, 4) if noise else T
        rows.append(f"{t:.1f}," + ",".join(f"{v:.3f}" for v in T))
    (folder / "probes.csv").write_text("\n".join(rows) + "\n")
    _, _, tau = fields(pts[:1], 0, p)
    tcool = float(tau * np.log(max((p["inlet_temperature"] - 300.0) / 100.0, 1.01)))
    (folder / "summary.json").write_text(json.dumps({"peak_temperature": round(peak, 3), "cooling_time": round(tcool, 2)}, indent=2))


def stage_series(p):
    pts, cells = grid()
    times = np.linspace(0, T_END, N_FRAMES)
    out = [fields(pts, t, p)[:2] for t in times]
    return pts, cells, times, out


def write_xdmf(folder: Path, p: dict):
    """XDMF 3 temporal collection with heavy data in fields.h5."""
    import h5py
    pts, cells, times, out = stage_series(p)
    folder.mkdir(parents=True, exist_ok=True)
    n, m = len(pts), len(cells)
    with h5py.File(folder / "fields.h5", "w") as f:
        f["mesh/points"] = pts; f["mesh/cells"] = cells
        for i, (T, U) in enumerate(out):
            f[f"step{i}/temperature"] = T; f[f"step{i}/displacement"] = U
    steps = "".join(
        f'''<Grid Name="step{i}" GridType="Uniform"><Time Value="{t:g}"/>
  <Topology TopologyType="Hexahedron" NumberOfElements="{m}"><DataItem Format="HDF" DataType="Int" Dimensions="{m} 8">fields.h5:/mesh/cells</DataItem></Topology>
  <Geometry GeometryType="XYZ"><DataItem Format="HDF" Dimensions="{n} 3">fields.h5:/mesh/points</DataItem></Geometry>
  <Attribute Name="temperature" Center="Node" AttributeType="Scalar"><DataItem Format="HDF" Dimensions="{n}">fields.h5:/step{i}/temperature</DataItem></Attribute>
  <Attribute Name="displacement" Center="Node" AttributeType="Vector"><DataItem Format="HDF" Dimensions="{n} 3">fields.h5:/step{i}/displacement</DataItem></Attribute>
</Grid>
''' for i, t in enumerate(times))
    (folder / "fields.xdmf").write_text(f'''<?xml version="1.0"?>
<Xdmf Version="3.0"><Domain><Grid Name="series" GridType="Collection" CollectionType="Temporal">
{steps}</Grid></Domain></Xdmf>
''')


def write_exodus(folder: Path, p: dict):
    """Exodus II (netCDF3, 64-bit offset) with nodal variables temperature, displacement_x/y/z."""
    from scipy.io import netcdf_file
    pts, cells, times, out = stage_series(p)
    folder.mkdir(parents=True, exist_ok=True)
    f = netcdf_file(str(folder / "result.exo"), "w", version=2)
    f.title = b"DataHub demo"
    f.createDimension("time_step", None)          # the record dimension must come first
    for d, v in (("len_string", 33), ("len_name", 33), ("four", 4), ("num_dim", 3), ("num_nodes", len(pts)),
                 ("num_elem", len(cells)), ("num_el_blk", 1), ("num_el_in_blk1", len(cells)),
                 ("num_nod_per_el1", 8), ("num_nod_var", 4)):
        f.createDimension(d, v)
    for i, ax in enumerate("xyz"):
        f.createVariable(f"coord{ax}", "d", ("num_nodes",))[:] = pts[:, i]
    con = f.createVariable("connect1", "i", ("num_el_in_blk1", "num_nod_per_el1")); con[:] = cells + 1
    con.elem_type = b"HEX8"
    names = f.createVariable("name_nod_var", "c", ("num_nod_var", "len_name"))
    names[:] = np.zeros((4, 33), "S1")
    for i, nm in enumerate(["temperature", "displacement_x", "displacement_y", "displacement_z"]):
        names[i, :len(nm)] = list(nm)
    tw = f.createVariable("time_whole", "d", ("time_step",))
    vals = [f.createVariable(f"vals_nod_var{i + 1}", "d", ("time_step", "num_nodes")) for i in range(4)]
    for k, (t, (T, U)) in enumerate(zip(times, out)):
        tw[k] = t; vals[0][k] = T
        for c in range(3):
            vals[c + 1][k] = U[:, c]
    f.close()


def write_hdf5(folder: Path, p: dict):
    """One plain HDF5 file per snapshot."""
    import h5py
    _, _, times, out = stage_series(p)
    folder.mkdir(parents=True, exist_ok=True)
    for i, (t, (T, U)) in enumerate(zip(times, out)):
        with h5py.File(folder / f"field_{i:04d}.h5", "w") as f:
            f["fields/temperature"] = T; f["fields/displacement"] = U; f.attrs["time"] = t


def png(path: Path, w=64, h=48, seed=0):
    rng = np.random.default_rng(seed)
    img = (np.clip(np.outer(np.linspace(0, 1, h), np.linspace(0.3, 1, w)) + rng.normal(0, 0.05, (h, w)), 0, 1) * 255).astype(np.uint8)
    raw = b"".join(b"\x00" + bytes(r) for r in img)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="demo_data")
    ap.add_argument("--runs", type=int, default=6, help="simulation runs with two stages each")
    ap.add_argument("--cases", type=int, default=32, help="sweep cases with parameters in the folder name")
    ap.add_argument("--experiments", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-formats", action="store_true", help="skip the XDMF / Exodus / HDF5 copies of run_001")
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    out = Path(a.out)
    t0 = datetime(2026, 1, 5, 8, 0, 0)

    for i in range(1, a.runs + 1):
        p = dict(inlet_temperature=float(rng.uniform(650, 780)), htc=float(rng.uniform(300, 1800)),
                 load=float(rng.uniform(0.02, 0.28)), scale=float(rng.uniform(0.7, 1.3)),
                 cooling=bool(rng.integers(0, 2)), geometry=GEOMS[i % 3], mesh_file="block_coarse.msh")
        run = out / "simulations" / f"run_{i:03d}"
        run.mkdir(parents=True, exist_ok=True)
        (run / "parameters.json").write_text(json.dumps(p, indent=2))
        (run / "run.log").write_text(f"{t0:%Y-%m-%d %H:%M:%S} solver start\n{t0 + timedelta(minutes=9):%Y-%m-%d %H:%M:%S} solver end\n")
        for k in (1, 2):
            pk = dict(p, inlet_temperature=p["inlet_temperature"] - 40 * (k - 1))
            write_run_files(run / f"stage_{k}", pk, rng)
        if i == 1 and not a.no_formats:
            for fmt, writer in (("xdmf", write_xdmf), ("exodus", write_exodus), ("hdf5", write_hdf5)):
                dst = out / "formats" / f"run_001_{fmt}"
                dst.mkdir(parents=True, exist_ok=True)
                (dst / "parameters.json").write_text(json.dumps(p, indent=2))
                try:
                    for k in (1, 2):
                        writer(dst / f"stage_{k}", dict(p, inlet_temperature=p["inlet_temperature"] - 40 * (k - 1)))
                except ImportError:
                    print(f"  skipping formats/{dst.name}: h5py not installed")
        t0 += timedelta(hours=1)

    try:
        from scipy.stats import qmc
        u = qmc.Sobol(d=4, scramble=True, seed=a.seed).random(a.cases)
    except Exception:
        u = rng.random((a.cases, 4))
    for n in range(a.cases):
        p = dict(inlet_temperature=720.0, htc=round(300 + 1500 * u[n, 0]), load=round(0.02 + 0.26 * u[n, 1], 3),
                 cooling=bool(u[n, 2] > 0.5), scale=round(0.7 + 0.6 * u[n, 3], 2), geometry=GEOMS[n % 3])
        name = f"case{n + 1:03d}__htc={p['htc']}__load={p['load']}__cooling={str(p['cooling']).lower()}__scale={p['scale']}"
        folder = out / "sweep" / name
        write_run_files(folder, p, rng)
        (folder / "run.cfg").write_text(f"inlet_temperature = {p['inlet_temperature']}\ngeometry = {p['geometry']}\nmesh_file = block_coarse.msh\n")

    for e in range(1, a.experiments + 1):
        p = dict(inlet_temperature=float(rng.uniform(680, 760)), htc=float(rng.uniform(400, 1400)),
                 load=0.1, scale=1.0, cooling=bool(e % 2), geometry="block")
        folder = out / "experiments" / f"exp_{e:02d}"
        write_run_files(folder, p, rng, noise=2.0)
        for f in folder.glob("field_*.vtk"):
            f.unlink()   # experiments have measurements, not fields
        (folder / "measurements.csv").write_text((folder / "probes.csv").read_text())
        (folder / "probes.csv").unlink()
        (folder / "parameters.json").write_text(json.dumps(p, indent=2))
        png(folder / "photo.png", seed=e)

    ref = out / "reference" / "mesh_block_coarse"
    ref.mkdir(parents=True, exist_ok=True)
    pts, cells = grid()
    (ref / "block_coarse.msh").write_text(f"# demo mesh\nnodes {len(pts)}\nelements {len(cells)}\n")
    (ref / "parameters.json").write_text(json.dumps({"mesh_file": "block_coarse.msh", "geometry": "block"}))
    print(f"Demo data written to {out.resolve()}")


if __name__ == "__main__":
    main()
