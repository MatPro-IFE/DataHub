#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""Draw the relational data-model figure (fig02_datamodel) with orthogonal connectors."""
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

OUT = Path(__file__).resolve().parent.parent
NAVY, TEAL, AMBER, RED = "#411454", "#6B4C9A", "#5E5566", "#E20613"
FILL = {NAVY: "#F4EFFA", TEAL: "#EAE1F6", AMBER: "#F1EEF2", RED: "#FDE7E8"}
EDGE = "#5E5566"
from matplotlib import font_manager as _fm
for _f in (Path(__file__).resolve().parents[2] / "brand" / "fonts").glob("*.ttf"):
    _fm.fontManager.addfont(str(_f))
plt.rcParams["font.family"] = "Fira Sans"

W, ROW, HEAD = 2.15, 0.17, 0.22
X = [0.05, 2.75, 5.45]
boxes = {}


def table(key, col, top, title, rows, color):
    x = X[col]
    h = HEAD + ROW * len(rows) + 0.06
    ax.add_patch(FancyBboxPatch((x, top - h), W, h, boxstyle="square,pad=0", fc=FILL[color], ec=color, lw=1))
    ax.add_patch(FancyBboxPatch((x, top - HEAD), W, HEAD, boxstyle="square,pad=0", fc=color, ec=color, lw=1))
    ax.text(x + W / 2, top - HEAD / 2, title, ha="center", va="center", color="white", fontsize=7.6, weight="bold")
    for i, r in enumerate(rows):
        ax.text(x + 0.06, top - HEAD - ROW * (i + 0.5) - 0.02, r, va="center", fontsize=6.9, color="#1D1D1B")
    boxes[key] = (x, top - h, W, h)


def mid_y(k):
    x, y, w, h = boxes[k]
    return y + h / 2


def line(pts, label=None, lpos=None, dashed=False, both=False):
    xs, ys = zip(*pts)
    ax.plot(xs, ys, color=EDGE, lw=0.9, ls="--" if dashed else "-", solid_capstyle="butt")
    ax.annotate("", xy=pts[-1], xytext=pts[-2], arrowprops=dict(arrowstyle="-|>", color=EDGE, lw=0.9, mutation_scale=7))
    if both:
        ax.annotate("", xy=pts[0], xytext=pts[1], arrowprops=dict(arrowstyle="-|>", color=EDGE, lw=0.9, mutation_scale=7))
    if label:
        ax.text(*lpos, label, fontsize=6.4, color=EDGE, ha="center", va="bottom")


fig, ax = plt.subplots(figsize=(7.95, 4.13))
ax.set_xlim(0, 7.95); ax.set_ylim(0.72, 4.85); ax.axis("off")

table("sweeps", 0, 4.8, "parameter_sweeps", ["name, description", "param_ranges (JSONB)", "objective quantity, goal", "datasets linked via sweep_datasets"], TEAL)
table("sugg", 0, 3.55, "sweep_suggestions", ["suggested_params (JSONB)", "method: sobol | lhs | random |", "  gp_uncertainty | gp_ei", "score, used, dataset_id"], TEAL)
table("users", 0, 2.45, "identity, access and audit", ["datahub_users: role, organisation,", "  bcrypt hash", "datahub_groups, group_members", "  (is_manager)", "share_links: token hash, expiry,", "  download limit, revoked_at", "user_sessions, download_logs", "(no grants for the anon role)"], RED)

table("ds", 1, 4.8, "datasets", ["id (uuid), name (unique)", "dataset_type (schema id), status", "parameters (JSONB), metadata (JSONB)", "tags[], notes, fts_vector", "allowed_orgs[], allowed_groups[]", "uploaded_by", "started_at, finished_at", "schema_hash"], NAVY)
table("pv", 1, 2.85, "param_vectors", ["vec vector, keys[]", "schema_hash"], TEAL)
table("fe", 1, 2.1, "field_embeddings", ["part_id, field_name, n_points", "embedding vector, n_components", "explained_var"], TEAL)

table("parts", 2, 4.8, "dataset_parts", ["part_index, label", "metadata (JSONB)"], NAVY)
table("files", 2, 4.05, "files", ["part_id, file_class (schema id)", "relpath, filename, object_key", "size_bytes, sha256", "time_value, metadata (JSONB)"], NAVY)
table("qty", 2, 2.9, "quantities", ["part_id, name (schema id)", "value, unit, source"], NAVY)

dx, dy, dw, dh = boxes["ds"]
# sweeps <-> datasets through sweep_datasets
line([(X[0] + W, mid_y("sweeps")), (X[1], mid_y("sweeps"))], "n:m", ((X[0] + W + X[1]) / 2, mid_y("sweeps") + 0.01), both=True)
bx, by, bw, bh = boxes["sweeps"]
line([(X[0] + W / 2, by), (X[0] + W / 2, boxes["sugg"][1] + boxes["sugg"][3])], "1:n", (X[0] + W / 2 + 0.18, by - 0.2))
# datasets -> parts, files, quantities via right gutter
gx = X[1] + W + 0.25
ax.plot([X[1] + W, gx, gx], [4.55, 4.55, mid_y("qty")], color=EDGE, lw=0.9)
for k in ("parts", "files", "qty"):
    line([(gx, mid_y(k)), (X[2], mid_y(k))])
ax.text(gx + 0.03, 4.57, "1:n", fontsize=6.4, color=EDGE, ha="left", va="bottom")
# parts -> files, quantities (optional) on right side
gr = X[2] + W + 0.12
ax.plot([X[2] + W, gr, gr], [mid_y("parts"), mid_y("parts"), mid_y("qty")], color=EDGE, lw=0.9, ls="--")
for k in ("files", "qty"):
    line([(gr, mid_y(k)), (X[2] + W, mid_y(k))], dashed=True)
ax.text(gr + 0.02, mid_y("parts") + 0.03, "optional", fontsize=6.0, color=EDGE, ha="left")
# datasets -> pv, fe
line([(X[1] + W / 2, dy), (X[1] + W / 2, boxes["pv"][1] + boxes["pv"][3])], "1:1", (X[1] + W / 2 + 0.15, dy - 0.2))
px, py, pw, ph = boxes["pv"]
gl = X[1] - 0.12
ax.plot([X[1], gl, gl], [dy + 0.2, dy + 0.2, mid_y("fe")], color=EDGE, lw=0.9)
line([(gl, mid_y("fe")), (X[1], mid_y("fe"))])
ax.text(gl - 0.03, mid_y("fe") + 0.02, "1:n", fontsize=6.0, color=EDGE, ha="right", va="bottom")

fig.savefig(OUT / "fig02_datamodel.pdf", bbox_inches="tight")
fig.savefig(OUT / "fig02_datamodel.png", dpi=200, bbox_inches="tight")
print("written")
