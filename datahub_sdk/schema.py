# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
datahub_sdk.schema - load, validate and apply a DataHub project schema.

The schema (YAML) is the only place where project-specific knowledge lives:
dataset types, parameters, file classes, quantities of interest, field
embedding settings and graph rules. Everything in this module is generic.

Typical use
-----------
    from datahub_sdk.schema import load_schema
    schema = load_schema("config/schema.yaml")
    schema.classify("result.0003.vtk")            # -> "field_snapshot"
    params = schema.extract_parameters(folder)     # -> {"htc": 850.0, ...}
    vec, keys = schema.parameter_vector(params)
"""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

DEFAULT_SCHEMA_PATH = os.environ.get("DATAHUB_SCHEMA", "config/schema.yaml")
PARAM_TYPES = {"float", "int", "bool", "category", "text"}
REDUCERS = {"max", "min", "mean", "last", "first"}


class SchemaError(ValueError):
    """Raised when a schema file is invalid. The message names the location."""


# ── data classes ─────────────────────────────────────────────────────────────

@dataclass
class PartsSpec:
    label: str = "Part"
    folder_pattern: str = ""


@dataclass
class DatasetType:
    id: str
    label: str
    parts: Optional[PartsSpec] = None


@dataclass
class Parameter:
    name: str
    label: str
    type: str = "float"
    unit: str = ""
    range: Optional[tuple[float, float]] = None
    values: list[str] = field(default_factory=list)
    in_vector: bool = True


@dataclass
class FileClass:
    id: str
    label: str
    patterns: list[str]
    time_regex: Optional[str] = None
    time_from_name: Optional[str] = None


@dataclass
class Quantity:
    name: str
    label: str
    unit: str
    source: dict


@dataclass
class Schema:
    raw: dict
    instance_name: str
    description: str
    dataset_types: list[DatasetType]
    parameters: list[Parameter]
    parameter_sources: list[dict]
    file_classes: list[FileClass]
    skip_patterns: list[str]
    quantities: list[Quantity]
    timing: Optional[dict]
    fields: dict
    graph: dict
    metadata_fields: list[dict]

    # ── lookup helpers ────────────────────────────────────────────────────
    @property
    def hash(self) -> str:
        """Short hash of the schema content; stored with derived data for versioning."""
        return hashlib.sha256(json.dumps(self.raw, sort_keys=True).encode()).hexdigest()[:12]

    @property
    def vector_hash(self) -> str:
        """Hash of the parameter definitions only: parameter vectors stay comparable when
        unrelated parts of the schema (file classes, quantities, readers ...) change."""
        return hashlib.sha256(json.dumps(self.raw.get("parameters", []), sort_keys=True).encode()).hexdigest()[:12]

    def dataset_type(self, type_id: str) -> DatasetType:
        for t in self.dataset_types:
            if t.id == type_id:
                return t
        raise SchemaError(f"unknown dataset type '{type_id}'; known: {[t.id for t in self.dataset_types]}")

    def parameter(self, name: str) -> Optional[Parameter]:
        return next((p for p in self.parameters if p.name == name), None)

    def file_class(self, class_id: str) -> Optional[FileClass]:
        return next((c for c in self.file_classes if c.id == class_id), None)

    def file_class_ids(self) -> list[str]:
        return [c.id for c in self.file_classes] + ["other"]

    def to_public_dict(self) -> dict:
        """Schema as JSON for the UI and API."""
        return {
            "schema_version": 1, "hash": self.hash, "instance": {"name": self.instance_name, "description": self.description},
            "dataset_types": [{"id": t.id, "label": t.label, "parts": vars(t.parts) if t.parts else None} for t in self.dataset_types],
            "parameters": [vars(p) | {"range": list(p.range) if p.range else None} for p in self.parameters],
            "file_classes": [vars(c) for c in self.file_classes],
            "quantities": [vars(q) for q in self.quantities],
            "fields": self.fields, "graph": self.graph, "metadata_fields": self.metadata_fields,
        }

    # ── classification ────────────────────────────────────────────────────
    def skip(self, filename: str) -> bool:
        low = Path(filename).name.lower()
        return any(fnmatch.fnmatch(low, p.lower()) for p in self.skip_patterns)

    def classify(self, filename: str) -> str:
        low = Path(filename).name.lower()
        for c in self.file_classes:
            if any(fnmatch.fnmatch(low, p.lower()) for p in c.patterns):
                return c.id
        return "other"

    def file_time(self, filename: str, head: bytes, class_id: str) -> Optional[float]:
        """Simulated time of a snapshot, from its header or its name, if configured."""
        c = self.file_class(class_id)
        if not c:
            return None
        if c.time_regex:
            m = re.search(c.time_regex, head[:4096].decode("latin-1", errors="replace"))
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    pass
        if c.time_from_name:
            m = re.search(c.time_from_name, Path(filename).name)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    pass
        return None

    # ── parts ─────────────────────────────────────────────────────────────
    def part_of(self, relpath: str, type_id: str) -> Optional[tuple[int, str]]:
        """(index, label) of the part a file belongs to, or None for dataset-level files."""
        t = self.dataset_type(type_id)
        if not t.parts or not t.parts.folder_pattern:
            return None
        first = Path(relpath).parts[0] if len(Path(relpath).parts) > 1 else None
        if first is None or not fnmatch.fnmatch(first.lower(), t.parts.folder_pattern.lower()):
            return None
        m = re.search(r"(\d+)(?!.*\d)", first)
        return (int(m.group(1)) if m else 0, first)

    # ── parameters ────────────────────────────────────────────────────────
    def coerce(self, name: str, value: Any) -> Any:
        """Convert a raw value to the declared parameter type (unknown keys pass through)."""
        p = self.parameter(name)
        if value is None:
            return None
        if isinstance(value, str):
            v = value.strip()
        else:
            v = value
        try:
            if p is None:
                if isinstance(v, str):
                    if re.fullmatch(r"[-+]?\d+", v):
                        return int(v)
                    if re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", v):
                        return float(v)
                    if v.lower() in ("true", "false"):
                        return v.lower() == "true"
                return v
            if p.type == "float":
                return float(v)
            if p.type == "int":
                return int(float(v))
            if p.type == "bool":
                if isinstance(v, str):
                    return v.lower() in ("1", "true", "yes", "on", "t")
                return bool(v)
            return str(v)
        except (TypeError, ValueError):
            return v

    def extract_parameters(self, root: Path | str, files: Optional[Iterable[str]] = None) -> dict:
        """Apply parameter_sources to a dataset folder. Returns a flat dict."""
        root = Path(root)
        rel = sorted(files) if files is not None else sorted(
            str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())
        params: dict[str, Any] = {}
        for src in self.parameter_sources:
            kind = src["type"]
            if kind == "folder_name":
                for m in re.finditer(src["regex"], root.name):
                    params[m.group("name")] = m.group("value")
            elif kind in ("json_file", "yaml_file", "key_value_file"):
                for r in rel:
                    if "/" in r and not src.get("recursive"):
                        continue
                    if not fnmatch.fnmatch(Path(r).name.lower(), src["pattern"].lower()):
                        continue
                    text = (root / r).read_text(errors="replace")
                    if kind == "key_value_file":
                        for line in text.splitlines():
                            m = re.match(r"\s*([A-Za-z_][\w.-]*)\s*[:=]\s*(.+?)\s*$", line)
                            if m and not line.lstrip().startswith(("#", ";", "!")):
                                params[m.group(1)] = m.group(2)
                    else:
                        data = json.loads(text) if kind == "json_file" else yaml.safe_load(text)
                        for k in (src.get("path") or "").split("."):
                            if k and isinstance(data, dict):
                                data = data.get(k, {})
                        if isinstance(data, dict):
                            params.update(_flatten(data))
        return {k: self.coerce(k, v) for k, v in params.items()}

    def parameter_vector(self, params: dict) -> tuple[list[float], list[str]]:
        """Encode parameters as a numeric vector: ranged numbers -> [0,1], bool -> 0/1,
        category -> one-hot. Missing values encode as 0. Returns (vector, labels)."""
        vec: list[float] = []
        keys: list[str] = []
        for p in self.parameters:
            if not p.in_vector or p.type == "text":
                continue
            v = params.get(p.name)
            if p.type in ("float", "int"):
                x = _to_float(v)
                if x is None:
                    x = 0.0
                elif p.range:
                    lo, hi = p.range
                    x = (x - lo) / (hi - lo) if hi != lo else 0.0
                vec.append(float(x)); keys.append(p.name)
            elif p.type == "bool":
                vec.append(1.0 if self.coerce(p.name, v) is True else 0.0); keys.append(p.name)
            elif p.type == "category":
                for c in p.values:
                    vec.append(1.0 if str(v) == c else 0.0); keys.append(f"{p.name}={c}")
        return vec, keys

    # ── quantities ────────────────────────────────────────────────────────
    def extract_quantities(self, files: list[tuple[str, str, bytes]]) -> list[dict]:
        """files: list of (relpath, file_class, content). Returns [{name, value, unit}]."""
        out = []
        for q in self.quantities:
            src = q.source
            cand = [f for f in files if f[1] == src.get("file_class")]
            if src.get("pattern"):
                cand = [f for f in cand if fnmatch.fnmatch(Path(f[0]).name.lower(), src["pattern"].lower())]
            for relpath, _, data in cand:
                val = _quantity_from_bytes(src, data)
                if val is not None:
                    out.append({"name": q.name, "value": val, "unit": q.unit, "source": relpath})
                    break
        return out

    def extract_timing(self, files: list[tuple[str, str, bytes]]) -> tuple[Optional[str], Optional[str]]:
        t = self.timing
        if not t:
            return None, None
        start = end = None
        for relpath, cls, data in files:
            if cls != t.get("file_class"):
                continue
            text = data.decode("utf-8", errors="replace")
            for line in text.splitlines():
                if t.get("start_regex") and start is None:
                    m = re.search(t["start_regex"], line)
                    if m:
                        start = _parse_time(m.group(1), t.get("format"))
                if t.get("end_regex"):
                    m = re.search(t["end_regex"], line)
                    if m:
                        end = _parse_time(m.group(1), t.get("format"))
        return start, end


# ── helpers ──────────────────────────────────────────────────────────────────

def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _to_float(v) -> Optional[float]:
    try:
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_time(s: str, fmt: Optional[str]) -> Optional[str]:
    try:
        dt = datetime.strptime(s.strip(), fmt) if fmt else datetime.fromisoformat(s.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def _quantity_from_bytes(src: dict, data: bytes) -> Optional[float]:
    text = data.decode("utf-8", errors="replace")
    try:
        if "json_key" in src:
            obj = json.loads(text)
            for k in str(src["json_key"]).split("."):
                obj = obj[k]
            return _to_float(obj)
        if "regex" in src:
            m = re.search(src["regex"], text, flags=re.MULTILINE)
            return _to_float(m.group(1)) if m else None
        if "csv_column" in src:
            rows = list(csv.DictReader(io.StringIO(text)))
            vals = [_to_float(r.get(src["csv_column"])) for r in rows]
            vals = [v for v in vals if v is not None]
            if not vals:
                return None
            red = src.get("reduce", "last")
            return {"max": max, "min": min, "mean": lambda x: sum(x) / len(x),
                    "last": lambda x: x[-1], "first": lambda x: x[0]}[red](vals)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


# ── loading and validation ───────────────────────────────────────────────────

def _req(d: dict, key: str, where: str):
    if key not in d:
        raise SchemaError(f"{where}: missing required key '{key}'")
    return d[key]


def parse_schema(raw: dict) -> Schema:
    if not isinstance(raw, dict):
        raise SchemaError("schema: top level must be a mapping")
    if raw.get("schema_version", 1) != 1:
        raise SchemaError("schema_version: only version 1 is supported")
    inst = raw.get("instance", {}) or {}

    types = []
    for i, t in enumerate(_req(raw, "dataset_types", "schema")):
        w = f"dataset_types[{i}]"
        parts = t.get("parts")
        types.append(DatasetType(id=_req(t, "id", w), label=t.get("label", t["id"]),
                                 parts=PartsSpec(**parts) if parts else None))
    if not types:
        raise SchemaError("dataset_types: at least one type is required")
    if len({t.id for t in types}) != len(types):
        raise SchemaError("dataset_types: ids must be unique")

    params = []
    for i, p in enumerate(raw.get("parameters", []) or []):
        w = f"parameters[{i}]"
        ptype = p.get("type", "float")
        if ptype not in PARAM_TYPES:
            raise SchemaError(f"{w}: type must be one of {sorted(PARAM_TYPES)}")
        rng = p.get("range")
        if rng is not None and (len(rng) != 2 or float(rng[1]) <= float(rng[0])):
            raise SchemaError(f"{w}: range must be [min, max] with max > min")
        if ptype == "category" and not p.get("values"):
            raise SchemaError(f"{w}: category parameters need 'values'")
        params.append(Parameter(name=_req(p, "name", w), label=p.get("label", p["name"]), type=ptype,
                                unit=p.get("unit", ""), range=(float(rng[0]), float(rng[1])) if rng else None,
                                values=[str(v) for v in p.get("values", [])], in_vector=p.get("in_vector", True)))

    sources = raw.get("parameter_sources", []) or []
    for i, s in enumerate(sources):
        w = f"parameter_sources[{i}]"
        kind = _req(s, "type", w)
        if kind == "folder_name":
            rx = re.compile(_req(s, "regex", w))
            if not {"name", "value"} <= set(rx.groupindex):
                raise SchemaError(f"{w}: regex needs named groups 'name' and 'value'")
        elif kind in ("json_file", "yaml_file", "key_value_file"):
            _req(s, "pattern", w)
        else:
            raise SchemaError(f"{w}: unknown type '{kind}'")

    classes = []
    for i, c in enumerate(raw.get("file_classes", []) or []):
        w = f"file_classes[{i}]"
        for key in ("time_regex", "time_from_name"):
            if c.get(key):
                re.compile(c[key])
        classes.append(FileClass(id=_req(c, "id", w), label=c.get("label", c["id"]),
                                 patterns=list(_req(c, "patterns", w)),
                                 time_regex=c.get("time_regex"), time_from_name=c.get("time_from_name")))
    class_ids = {c.id for c in classes} | {"other"}

    quantities = []
    for i, q in enumerate(raw.get("quantities", []) or []):
        w = f"quantities[{i}]"
        src = _req(q, "source", w)
        if src.get("file_class") not in class_ids:
            raise SchemaError(f"{w}.source.file_class: unknown class '{src.get('file_class')}'")
        if sum(k in src for k in ("json_key", "regex", "csv_column")) != 1:
            raise SchemaError(f"{w}.source: give exactly one of json_key, regex, csv_column")
        if "regex" in src:
            re.compile(src["regex"])
        if "csv_column" in src and src.get("reduce", "last") not in REDUCERS:
            raise SchemaError(f"{w}.source.reduce: one of {sorted(REDUCERS)}")
        quantities.append(Quantity(name=_req(q, "name", w), label=q.get("label", q["name"]),
                                   unit=q.get("unit", ""), source=src))

    from .formats import READERS
    fields = {"file_class": None, "reader": "auto", "names": [], "pca_components": 16, "max_snapshots": 20,
              "hdf5": {}, "exodus": {}}
    fields.update(raw.get("fields", {}) or {})
    if fields["file_class"] and fields["file_class"] not in class_ids:
        raise SchemaError(f"fields.file_class: unknown class '{fields['file_class']}'")
    if fields["reader"] not in READERS:
        raise SchemaError(f"fields.reader: one of {', '.join(READERS)}")
    h5 = fields.get("hdf5") or {}
    if not isinstance(h5, dict) or set(h5) - {"paths", "time_axis", "time_path"}:
        raise SchemaError("fields.hdf5: allowed keys are paths, time_axis, time_path")
    if h5.get("time_axis") not in (None, 0):
        raise SchemaError("fields.hdf5.time_axis: 0 or omitted")
    ex = fields.get("exodus") or {}
    if not isinstance(ex, dict) or set(ex) - {"element_variables", "combine_vectors"}:
        raise SchemaError("fields.exodus: allowed keys are element_variables, combine_vectors")
    fields["hdf5"], fields["exodus"] = h5, ex
    fields["max_snapshots"] = max(1, int(fields["max_snapshots"]))
    fields["pca_components"] = max(1, min(50, int(fields["pca_components"])))

    graph = {"shared_resources": [], "parameter_edge_threshold": 0.85, "field_edge_threshold": 0.80, "neighbours": 50}
    graph.update(raw.get("graph", {}) or {})

    return Schema(raw=raw, instance_name=inst.get("name", "DataHub"), description=inst.get("description", ""),
                  dataset_types=types, parameters=params, parameter_sources=sources, file_classes=classes,
                  skip_patterns=list(raw.get("skip_patterns", []) or []), quantities=quantities,
                  timing=raw.get("timing"), fields=fields, graph=graph,
                  metadata_fields=list(raw.get("metadata_fields", []) or []))


def load_schema(path: Optional[str | Path] = None) -> Schema:
    p = Path(path or DEFAULT_SCHEMA_PATH)
    if not p.exists():
        raise SchemaError(f"schema file not found: {p} (copy config/schema.example.yaml to config/schema.yaml)")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise SchemaError(f"{p}: invalid YAML: {e}") from e
    return parse_schema(raw)


if __name__ == "__main__":  # python -m datahub_sdk.schema config/schema.yaml
    import sys
    s = load_schema(sys.argv[1] if len(sys.argv) > 1 else None)
    v, k = s.parameter_vector({})
    print(f"OK  {s.instance_name}: {len(s.dataset_types)} dataset types, {len(s.parameters)} parameters "
          f"({len(k)}-dim vector), {len(s.file_classes)} file classes, {len(s.quantities)} quantities, hash {s.hash}")
