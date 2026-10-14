# Examples

| File | Purpose |
|---|---|
| `make_demo_dataset.py` | Synthetic data set matching `config/schema.example.yaml`: simulation runs with two stages, sweep cases with parameters in the folder name, experiments with images and measurements, and a reference mesh |
| `smoke_test.py` | End-to-end test of a running instance through the Python client: upload, classification, parameters, quantities, search, download integrity, embeddings, similarity, graph, sweeps and organisation-based access control |

```bash
pip install -e ".[demo]"
python examples/make_demo_dataset.py --out demo_data
# optional: bulk-ingest the sweep cases server side (INGEST_FOLDER=demo_data in docker/.env)
docker compose -f docker/docker-compose.yml --env-file docker/.env exec dashboard \
    python -m dashboard.ingest /ingest/sweep --each --type simulation --workers 4
set -a; . docker/.env; set +a
python examples/smoke_test.py --data demo_data          # add --upload-sweep to upload the cases over HTTP instead
```
