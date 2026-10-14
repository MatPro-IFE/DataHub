#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
make_charts.py - regenerate the quantitative figures of the DataHub technical report.

All inputs are either computed here (sampling designs, Gaussian process) or read
from the synthetic demo data set and the small CSV/JSON files in ../data, which
were exported from a test instance loaded with examples/make_demo_dataset.py.

    python make_charts.py --demo ../../../../demo_data      # writes ../fig07..fig12 (.pdf, .png)
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
from pathlib import Path

import warnings

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import qmc

warnings.filterwarnings("ignore", message=".*balance properties.*")

HERE = Path(__file__).resolve().parent
OUT = HERE.parent
DATA = OUT / "data"

# IFE brand: purple / red / green series (validated all-pairs for colour-vision deficiency),
# purple sequential ramp, IFE text colours. Fira Sans is loaded from ../../brand/fonts.
C1, C2, C3 = "#6B4C9A", "#E20613", "#3E9C5F"
PURPLE = "#411454"
INK, INK2, GRID = "#1D1D1B", "#5E5566", "#E6E1EA"
MARKERS = ["o", "s", "^"]
STYLES = ["-", "--", "-."]
from matplotlib import font_manager as _fm
from matplotlib.colors import LinearSegmentedColormap
for _f in (Path(__file__).resolve().parents[2] / "brand" / "fonts").glob("*.ttf"):
    _fm.fontManager.addfont(str(_f))
PURPLES = LinearSegmentedColormap.from_list("ife_purples", ["#FFFFFF", "#EAE1F6", "#8667B3", PURPLE])

mpl.rcParams.update({
    "font.family": "Fira Sans", "font.size": 9, "axes.edgecolor": INK2, "axes.labelcolor": INK,
    "xtick.color": INK2, "ytick.color": INK2, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.6, "axes.axisbelow": True, "axes.spines.top": False, "axes.spines.right": False,
    "legend.frameon": False, "lines.linewidth": 2, "figure.dpi": 200, "savefig.bbox": "tight",
})


def save(fig, name):
    fig.savefig(OUT / f"{name}.pdf")
    fig.savefig(OUT / f"{name}.png", dpi=200)
    plt.close(fig)


def designs(n, d, seed):
    rng = np.random.default_rng(seed)
    return {
        "Random": rng.random((n, d)),
        "Latin hypercube": qmc.LatinHypercube(d=d, seed=seed).random(n),
        "Sobol (scrambled)": qmc.Sobol(d=d, scramble=True, seed=seed).random(n),
    }


def fig07_sampling():
    """Three 2-D designs of 32 points + discrepancy versus n."""
    fig = plt.figure(figsize=(7.2, 4.6))
    gs = fig.add_gridspec(2, 3, height_ratios=[1, 0.95], hspace=0.55, wspace=0.3)
    for k, (name, pts) in enumerate(designs(32, 2, 3).items()):
        ax = fig.add_subplot(gs[0, k])
        ax.scatter(pts[:, 0], pts[:, 1], s=14, color=[C1, C2, C3][k], marker=MARKERS[k], edgecolor="white", linewidth=0.6, zorder=3)
        ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.set_aspect("equal")
        ax.set_title(name, fontsize=9, color=INK)
        ax.set_xticks([0, 0.5, 1]); ax.set_yticks([0, 0.5, 1])
        ax.set_xlabel("parameter 1 (scaled)", fontsize=8)
        if k == 0:
            ax.set_ylabel("parameter 2 (scaled)", fontsize=8)
    ax = fig.add_subplot(gs[1, :])
    ns = 2 ** np.arange(3, 10)
    for k, name in enumerate(["Random", "Latin hypercube", "Sobol (scrambled)"]):
        vals = np.array([[qmc.discrepancy(designs(n, 4, s)[name]) for n in ns] for s in range(10)])
        ax.plot(ns, vals.mean(0), color=[C1, C2, C3][k], marker=MARKERS[k], markersize=4, linestyle=STYLES[k], label=name)
    ax.set_xscale("log", base=2); ax.set_yscale("log")
    ax.set_xlabel("number of runs n (4 parameters)"); ax.set_ylabel("centred L2 discrepancy")
    ax.legend(loc="lower left", fontsize=8, ncols=3)
    save(fig, "fig07_sampling_designs")


def extent(pts):
    """DataHub extent score: mean over dimensions of (observed range / defined range)."""
    return float(np.mean(pts.max(0) - pts.min(0)))


_PROBE = qmc.Sobol(d=3, scramble=True, seed=0).random(1024)


def fill_distance(pts):
    """Largest distance from a probe point in the unit cube to its nearest design point."""
    d = np.sqrt(((_PROBE[:, None, :] - pts[None, :, :]) ** 2).sum(-1)).min(1)
    return float(d.max())


def fig08_coverage():
    ns = np.arange(2, 65, 2)
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.8), gridspec_kw={"wspace": 0.3})
    for ax, (metric, label) in zip(axes, [(extent, "extent score (higher is wider)"),
                                          (fill_distance, "fill distance (lower is better)")]):
        for k, name in enumerate(["Random", "Latin hypercube", "Sobol (scrambled)"]):
            vals = np.array([[metric(designs(n, 3, s)[name]) for n in ns] for s in range(20)])
            m, lo, hi = vals.mean(0), np.percentile(vals, 10, 0), np.percentile(vals, 90, 0)
            ax.fill_between(ns, lo, hi, color=[C1, C2, C3][k], alpha=0.12, linewidth=0)
            ax.plot(ns, m, color=[C1, C2, C3][k], linestyle=STYLES[k], label=name)
        ax.set_xlabel("linked runs in sweep (3 parameters)"); ax.set_ylabel(label)
    axes[0].set_ylim(0, 1.02); axes[1].set_ylim(0, None)
    axes[1].legend(loc="upper right", fontsize=8)
    save(fig, "fig08_coverage")


def _batch(cand, acq, n, min_dist=0.15):
    picks = []
    for i in np.argsort(-acq):
        if all(abs(cand[i] - p) >= min_dist for p in picks):
            picks.append(cand[i])
        if len(picks) == n:
            break
    return np.array(picks)


def fig09_gp():
    """1-D illustration of the two Gaussian-process suggestion methods with batch spacing."""
    from scipy.stats import norm
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import RBF, ConstantKernel, WhiteKernel
    f = lambda x: np.sin(6 * x) + 0.4 * x
    X = np.array([0.05, 0.18, 0.3, 0.42, 0.85, 0.93])[:, None]
    y = f(X).ravel()
    gp = GaussianProcessRegressor(ConstantKernel() * RBF(0.15) + WhiteKernel(1e-4), n_restarts_optimizer=3,
                                  random_state=0).fit(X, y)
    xs = np.linspace(0, 1, 400)[:, None]
    mu, sd = gp.predict(xs, return_std=True)
    cand = qmc.Sobol(d=1, scramble=True, seed=1).random(256).ravel()
    cm, cs = gp.predict(cand[:, None], return_std=True)
    best = y.min()                                    # objective: minimise
    z = (best - cm) / np.maximum(cs, 1e-9)
    ei = (best - cm) * norm.cdf(z) + cs * norm.pdf(z)
    picks = {"gp_uncertainty": _batch(cand, cs, 3), "gp_ei": _batch(cand, ei, 3)}
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.9), sharey=True, gridspec_kw={"wspace": 0.08})
    for ax, (name, pk), col in zip(axes, picks.items(), [C2, C3]):
        ax.fill_between(xs.ravel(), mu - 2 * sd, mu + 2 * sd, color=C1, alpha=0.15, linewidth=0, label="GP mean ± 2σ")
        ax.plot(xs, mu, color=C1)
        ax.plot(xs, f(xs), color=INK2, linewidth=1, linestyle="--", label="unknown response")
        ax.scatter(X, y, color=INK, s=22, zorder=4, label="existing runs")
        for i, p in enumerate(pk):
            ax.axvline(p, color=col, linewidth=1.4, label="suggested batch" if i == 0 else None)
        ax.set_title(name, fontsize=9, color=INK)
        ax.set_xlabel("parameter (scaled)")
    axes[0].set_ylabel("objective quantity")
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncols=4, fontsize=8, bbox_to_anchor=(0.5, 1.06))
    save(fig, "fig09_bayesian_suggestion")


def read_vtk_scalar(path, name="temperature"):
    lines = Path(path).read_text().split("\n")
    n = int(next(l for l in lines if l.startswith("POINT_DATA")).split()[1])
    i = next(k for k, l in enumerate(lines) if l.startswith(f"SCALARS {name}")) + 2
    return np.array(lines[i:i + n], dtype=float)


def fig10_pca(demo: Path):
    from sklearn.decomposition import PCA
    runs = sorted(glob.glob(str(demo / "sweep" / "case*")))
    X = np.vstack([[read_vtk_scalar(f) for f in sorted(glob.glob(r + "/field_*.vtk"))] for r in runs])
    pca = PCA(n_components=16).fit(X)
    ev = pca.explained_variance_ratio_
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    ev = ev[:8]; ev = ev[ev > 1e-12]
    k = np.arange(1, len(ev) + 1)
    ax.bar(k, ev, color=C1, width=0.6)
    ax.set_yscale("log"); ax.set_xticks(k)
    cum = np.cumsum(pca.explained_variance_ratio_)
    ax.text(0.98, 0.92, f"cumulative: 1 PC {cum[0]:.3f} · 2 PCs {cum[1]:.4f} · 3 PCs {cum[2]:.4f}",
            transform=ax.transAxes, ha="right", fontsize=8, color=INK)
    ax.set_xlabel(f"principal component (temperature field, {X.shape[0]} snapshots × {X.shape[1]} nodes)")
    ax.set_ylabel("explained variance ratio (log)")
    ax.set_ylim(ev.min() * 0.3, 20)
    save(fig, "fig10_pca_variance")


def fig11_similarity():
    d = json.load(open(DATA / "param_vectors_demo.json"))
    htc = [float(r["parameters"]["htc"]) for r in d]
    order = np.argsort(htc)
    V = np.array([json.loads(d[i]["vec"]) if isinstance(d[i]["vec"], str) else d[i]["vec"] for i in order])
    Vn = V / np.linalg.norm(V, axis=1, keepdims=True)
    S = Vn @ Vn.T
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.2), gridspec_kw={"width_ratios": [1.15, 1], "wspace": 0.55})
    ax = axes[0]
    im = ax.imshow(S, cmap=PURPLES, vmin=S.min(), vmax=1, interpolation="nearest")
    ax.set_xlabel("case (sorted by htc)"); ax.set_ylabel("case (sorted by htc)"); ax.grid(False)
    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03); cb.set_label("cosine similarity", fontsize=8)
    ax = axes[1]
    tri = S[np.triu_indices(len(S), 1)]
    ax.hist(tri, bins=24, color=C1, edgecolor="white", linewidth=0.6)
    ax.axvline(0.85, color=C2, linewidth=1.5)
    ax.text(0.85, ax.get_ylim()[1] * 0.92, "  edge threshold 0.85", color=INK, fontsize=8)
    ax.set_xlabel("pairwise cosine similarity"); ax.set_ylabel("case pairs")
    save(fig, "fig11_param_similarity")


def fig12_ingest():
    rows = list(csv.DictReader(open(DATA / "bulk_ingest_benchmark.csv")))
    w = [int(r["workers"]) for r in rows]
    rate = [int(r["runs"]) / float(r["seconds"]) for r in rows]
    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    ax.bar([str(x) for x in w], rate, color=C1, width=0.55)
    for x, r in zip([str(x) for x in w], rate):
        ax.text(x, r + 0.1, f"{r:.1f}", ha="center", color=INK, fontsize=8)
    ax.set_xlabel("parallel upload workers"); ax.set_ylabel("datasets ingested per second")
    ax.set_ylim(0, max(rate) * 1.25)
    save(fig, "fig12_ingest_throughput")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", default="demo_data", help="folder created by examples/make_demo_dataset.py")
    a = ap.parse_args()
    fig07_sampling(); fig08_coverage(); fig09_gp(); fig10_pca(Path(a.demo)); fig11_similarity(); fig12_ingest()
    print("figures written to", OUT)
