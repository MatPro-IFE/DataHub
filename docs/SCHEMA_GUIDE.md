# Writing a project schema

A DataHub instance is configured for one project by a single YAML file, `config/schema.yaml`. The code contains no project knowledge: dataset types, parameters, file classes, quantities of interest, field embeddings and graph rules all come from this file. Start from `config/schema.example.yaml` (a fictitious cooling study) and replace its content.

Validate a schema before deploying it:

```bash
pip install -e .
datahub check-schema config/schema.yaml
datahub preview /path/to/one/run --schema config/schema.yaml --type simulation
```

`preview` shows the parameters, the class and part of every file and the quantities that would be extracted, without uploading anything.

## Sections

| Section | Purpose |
|---|---|
| `instance` | Name and description shown in the interface |
| `dataset_types` | Kinds of upload (simulation run, experiment, reference data ...). Optional `parts` turns matching sub-folders (glob `folder_pattern`) into a sub-level such as stages, load cases or time windows |
| `parameters` | Inputs describing a dataset. `type` is `float`, `int`, `bool`, `category` or `text`. `range` scales numbers to [0, 1] in the similarity vector and is the default sweep range. `values` lists categories (one-hot encoded). `in_vector: false` excludes a parameter from similarity |
| `parameter_sources` | Where values come from, applied in order (later wins): `json_file` / `yaml_file` (optionally a `path` into the document), `key_value_file` (`key = value` lines), `folder_name` (regex with named groups `name` and `value`) |
| `file_classes` | Ordered rules mapping file-name globs to a class id. Optional `time_regex` (searched in the first 4 kB) or `time_from_name` give the simulated or recorded time of a file. Unmatched files are `other` |
| `skip_patterns` | Files never stored (lock files, temporary files) |
| `quantities` | Named scalar results. Source is one of `json_key` (dotted path), `regex` (first group) or `csv_column` with `reduce: max/min/mean/first/last`, read from files of a given class. Extracted per part and per dataset |
| `timing` | Optional start and end time from a log file |
| `fields` | Field embeddings: snapshot `file_class`, `reader` (`auto` or one of the readers below), `names` (globs; empty = all arrays), `pca_components` (at most 50), `max_snapshots` per part (longer series are sampled evenly), reader options `hdf5` and `exodus` |
| `graph` | `shared_resources`: parameters whose values identify shared inputs (a mesh, a material table); datasets with the same value are linked through a resource node. Edge thresholds and neighbour count for similarity edges |
| `metadata_fields` | Free-text fields offered on the upload form |

## Field readers

`fields.reader: auto` picks the reader from the file extension, so one snapshot class can mix formats. Files that hold a whole time series give one snapshot per step. Vector and tensor arrays are reduced to their norm per point or cell. Check what a reader finds with `datahub fields path/to/file`.

| Reader | Extensions | What is read | Time |
|---|---|---|---|
| `vtk` | `.vtk .vtu .vtp .vts .vtr .vti` | point and cell arrays | `TIME` field data, or the file class `time_regex` |
| `npy` | `.npy .npz` | one field per array | file class rule |
| `hdf5` | `.h5 .hdf5 .hdf .he5` | numeric datasets (`hdf5.paths`, globs; default all) | root attribute `time`; with `hdf5.time_axis: 0` the leading axis is time and `hdf5.time_path` names the time dataset |
| `xdmf` | `.xdmf .xmf` | node and cell `Attribute`s of uniform grids and spatial collections, data inline (`XML`) or in HDF5 (`HDF`) | `Time Value` per grid of a temporal collection, or a `TimeType="List"` |
| `exodus` | `.exo .e .ex2 .exii .gen` | nodal variables (element variables with `exodus.element_variables: true`); `name_x/_y/_z` components are combined into one vector field unless `exodus.combine_vectors: false` | `time_whole` |

HDF5 files referenced by an XDMF file of the same dataset are fetched with it and not read again on their own, so `*.h5` can safely be part of the same file class. Exodus files in the classic netCDF format are read with SciPy, netCDF-4 (HDF5-based) files with h5py. XDMF `Function` and `HyperSlab` data items and the `Binary` format are not supported.

```yaml
fields:
  file_class: field_snapshot
  reader: auto
  names: ["temperature", "velocity*"]
  pca_components: 16
  max_snapshots: 20
  hdf5:   {paths: ["results/*"], time_axis: 0, time_path: "results/time"}
  exodus: {element_variables: false, combine_vectors: true}
```

## Changing a schema on a running instance

1. Edit `config/schema.yaml` and validate it with `datahub check-schema`.
2. Restart `dashboard` and `ml-api` (`docker compose restart dashboard ml-api`).
3. Rebuild derived data: `curl -X POST -H "Authorization: Bearer $ML_API_TOKEN" http://127.0.0.1:8090/process/all` on the server (parameter vectors and graph). Field embeddings are rebuilt per dataset from the dataset page or `POST /api/embed/{id}`.

Parameter vectors are versioned by a hash of the `parameters` section only, so changes elsewhere in the schema (file classes, readers, quantities) do not make existing vectors incomparable. Stored files are never touched by a schema change. Parameters and quantities are extracted at upload; datasets uploaded under an older schema keep their values (the schema version is stored with every dataset). To re-extract them, download and upload again, or re-ingest from the original folders.

## Tips

- Keep parameter names stable; they are keys in the stored parameter documents and in sweeps.
- Give every numeric parameter a realistic `range`; otherwise its raw value dominates the cosine similarity.
- Prefer machine-readable summaries (`json_key`) over regular expressions on free text.
- Use dataset types rather than tags for things that change how data are processed.
