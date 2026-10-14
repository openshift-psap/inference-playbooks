# Handoff: Forge Benchmark Ingestion Pipeline

## Goal

Automatically ingest Forge benchmark results into the inference-playbooks
catalog when a model/recipe PR merges to `main`. The ingestion reads Forge's
`kpis.json` from MLflow, normalizes it into the catalog's chart-ready sweep
format, and updates the benchmark index so the UI displays real performance
data.

## Repository state

- **Working branch:** `integration/catalog-preview` on
  `openshift-psap/inference-playbooks` (contains all recipe PRs + UI PR merged).
- **PR #17 branch:** `pr/ai-hub-model-overview-ui` on `cemigo114/inference-playbooks`
  (head: `c5b2520`), pushed via maintainer access.
- **Main:** `f3f1010`.

### Relevant commits on `integration/catalog-preview`

```
f668c1d feat(ui): show p50/p90/p95/p99 latency percentiles with toggleable series
ceb0f15 feat(ui): group benchmark evidence by harness with labeled sections
525cbd7 feat(ui): add concurrency sweep charts to benchmark tab
eb4a3a4 integration: merge PR #17 — UI
```

### Files modified by the UI chart work

| File | What |
|------|------|
| `schema/benchmark-result.schema.json` | Added `sweep` array, `harness` field, schema_version 1 or 2 |
| `site/app.js` | Sweep charts (Chart.js), harness sections, percentile toggles |
| `site/index.html` | Chart.js v4.4.7 CDN script tag |
| `site/styles.css` | Chart cards, stat cards, harness sections, various polish |
| `tools/catalog.py` | Passes `result.sweep` and `result.harness` through to entries |

## Evidence contract — three supported formats

| Format | Source | Processing |
|--------|--------|------------|
| **Forge `kpis.json`** | MLflow artifact | Normalizer maps KPI IDs → sweep points |
| **GuideLLM output JSON** | Committed or S3-backed | Local normalizer parses raw output |
| **AIPerf output JSON** | Committed or S3-backed | Local normalizer parses raw output |

All three converge on the same chart-ready structure: `result.json` with a
`sweep` array of per-concurrency points.

For the **automated post-merge pipeline**, only Forge `kpis.json` is used.
Raw harness artifact ingestion is for manual/offline benchmarks performed
outside Forge.

## Forge `kpis.json` structure (actual PR #34 example)

A reference copy is saved at `tests/fixtures/forge-kpis-pr34-example.json`.

```json
{
  "schema_version": "2",
  "tests": [
    {
      "run_id": "001__inference_playbooks_test_dir/001__benchmark_ci-quick",
      "labels": {
        "benchmark_tool": "guidellm",
        "benchmark_key": "ci-quick",
        "model_name": "google/gemma-4-26B-A4B-it",
        "product_version": "v0.24.0",
        "deployment_profile": "tp1",
        "gpu_type": "NVIDIA-H200",
        "cluster": "janus",
        ...
      },
      "metadata": { "timestamp": "2026-10-14T15:11:44Z" },
      "kpis": [ ... ]
    }
  ]
}
```

### GuideLLM KPIs — curve format

Each KPI has `is_curve: true` and `values: [[concurrency, metric], ...]`.
**Latencies are in seconds** (must convert to ms for the chart contract).

| KPI ID | Chart field | Unit conversion |
|--------|------------|-----------------|
| `playbooks_ttft_median` | `ttft_ms_p50` | × 1000 |
| `playbooks_ttft_p90` | `ttft_ms_p90` | × 1000 (requested from Forge, not yet emitted) |
| `playbooks_ttft_p95` | `ttft_ms_p95` | × 1000 |
| `playbooks_ttft_p99` | `ttft_ms_p99` | × 1000 |
| `playbooks_itl_median` | `itl_ms_p50` | × 1000 |
| `playbooks_itl_p90` | `itl_ms_p90` | × 1000 (requested from Forge) |
| `playbooks_itl_p95` | `itl_ms_p95` | × 1000 |
| `playbooks_itl_p99` | `itl_ms_p99` | × 1000 |
| `playbooks_tpot_median` | `tpot_ms_p50` | × 1000 |
| `playbooks_tpot_p90` | `tpot_ms_p90` | × 1000 (requested from Forge) |
| `playbooks_tpot_p95` | `tpot_ms_p95` | × 1000 |
| `playbooks_tpot_p99` | `tpot_ms_p99` | × 1000 |
| `playbooks_output_tok_per_sec` | `output_tok_s` | direct |
| `playbooks_total_tok_per_sec` | `total_tok_s` | direct |
| `playbooks_completed_requests` | `total_requests` | direct |
| `playbooks_failed_requests` | `failed_requests` | direct |

Curve `values` array: `[[concurrency_float, metric_float], ...]`. The
concurrency is `intended_concurrency`, not `measured_concurrency`.

### AIPerf KPIs — scalar format

Each KPI has `is_curve: false` and a single `value`. One test entry per
concurrency level. A full sweep produces multiple test entries with
`labels.concurrency` distinguishing them.

| KPI ID | Chart field | Unit |
|--------|------------|------|
| `aiperf_time_to_first_token_avg` | `ttft_ms_avg` | ms (already ms) |
| `aiperf_time_to_first_token_p95` | `ttft_ms_p95` | ms |
| `aiperf_inter_token_latency_avg` | `itl_ms_avg` | ms |
| `aiperf_inter_token_latency_p95` | `itl_ms_p95` | ms |
| `aiperf_output_token_throughput_avg` | `output_tok_s` | tok/s |
| `aiperf_request_count_avg` | `total_requests` | count |
| `aiperf_request_throughput_avg` | `rps` | req/s |

**p90 and p99 will be added by Forge when requested.** The normalizer
should handle their absence gracefully.

### Missing p90

Current Forge GuideLLM output emits p1, median (p50), p95, p99, p999.
**p90 has been requested.** The normalizer must treat missing p90 as null
(not zero), and the chart renders without it until Forge adds it.

## MLflow access

### Connection details

- **Tracking URI:** `https://mlflow.apps.psap-automation.ibm.rhperfscale.org`
- **Workspace routing:** HTTP header `X-MLflow-Workspace: forge-sandbox`
  (not `MLFLOW_WORKSPACE` env var — the standard MLflow Python client does
  not pick it up; use `requests` with the header directly)
- **Auth:** Basic auth. Credentials at `/home/thibrahi/creds/mlflow`.
- **TLS:** Self-signed cert, requires `verify=False` / `-k`.
- **Experiment:** `forge-inference-playbooks` (ID `470`).

### API endpoints used

```
GET /api/2.0/mlflow/runs/get?run_id=<ID>
GET /api/2.0/mlflow/artifacts/list?run_id=<ID>&path=<path>
GET /get-artifact?run_id=<ID>&path=<artifact_path>
POST /api/2.0/mlflow/runs/search  (body: experiment_ids, filter, order_by)
```

All requests require the `X-MLflow-Workspace: forge-sandbox` header and
Basic auth.

### Key MLflow identifiers (PR #34 example)

| Identifier | Value |
|---|---|
| Experiment ID | `470` |
| Parent run | `6280232dd3c94d30a8382ae49188e575` |
| GuideLLM child | `3e2f1c6430024d6ea71fc7bcd4eb7a0f` |
| AIPerf child | `52202456e544458aa5382fe58cff6126` |
| `kpis.json` path | `01__test/002__postprocessing/kpis/kpis.json` |
| Prow job ID | `2110384840191774720` |

## What to build

### 1. KPI normalizer (`tools/normalize_kpis.py`)

Reads a Forge `kpis.json` and produces one `result.json` per harness (or a
combined result with both harnesses' sweeps).

**GuideLLM normalization:**
- Group curve KPIs by concurrency (X values from any curve KPI).
- For each concurrency point, extract the mapped metrics.
- Convert latency seconds → milliseconds.
- Round concurrency floats to integers.
- Output a `sweep` array sorted by concurrency.

**AIPerf normalization:**
- Group test entries by `labels.concurrency`.
- For each concurrency level, extract scalar KPI values.
- AIPerf latencies are already in ms.
- Output a `sweep` array sorted by concurrency.

**Both produce:**
```json
{
  "schema_version": 2,
  "run_id": "<forge_run_id>",
  "deployment_scope": "single-node",
  "accelerator_key": "<from labels>",
  "harness": "<guidellm|aiperf>",
  "metrics": { ... },  // summary at reference concurrency
  "sweep": [
    {"concurrency": 1, "ttft_ms_p50": ..., "ttft_ms_p90": ..., ...},
    {"concurrency": 4, ...},
    ...
  ]
}
```

### 2. MLflow fetcher (`tools/fetch_forge_evidence.py`)

Given a PR number or MLflow run ID:
1. Connect to MLflow with workspace header.
2. Fetch parent run metadata (status, tags, params).
3. List child runs to identify benchmark runs.
4. Download `kpis.json` from the postprocessing artifacts.
5. Extract attribution labels (model, hardware, platform, recipe).
6. Run the normalizer.
7. Output a provenance record with MLflow links.

### 3. Evidence record schema

Extend `benchmark-run.schema.json` to support Forge-backed evidence.
New fields alongside existing ones:

```yaml
schema_version: 2  # new version for Forge evidence
evidence_source: forge-kpis  # or "local-guidellm", "local-aiperf"
forge:
  mlflow_tracking_uri: https://mlflow.apps.psap-automation.ibm.rhperfscale.org
  mlflow_workspace: forge-sandbox
  mlflow_experiment_id: "470"
  mlflow_parent_run_id: "6280232dd3c94d30a8382ae49188e575"
  mlflow_benchmark_run_id: "3e2f1c6430024d6ea71fc7bcd4eb7a0f"
  mlflow_run_url: "https://mlflow.apps.psap-automation.ibm.rhperfscale.org/#/experiments/470/runs/3e2f1c6430024d6ea71fc7bcd4eb7a0f/artifacts?workspace=forge-sandbox"
  prow_job_id: "2110384840191774720"
  prow_job_url: "https://gcs.ci.openshift.org/..."
  pr_number: 34
  pr_comment_id: 6171624960
  kpis_artifact_path: "01__test/002__postprocessing/kpis/kpis.json"
  kpis_checksum: "sha256:..."
  completeness: full-sweep | smoke | partial
```

The `evidence_source` field distinguishes Forge ingestion from manual
evidence. The `forge` block provides full provenance.

### 4. Ingestion workflow (`.github/workflows/benchmark-ingest.yml`)

Trigger: `push` to `main` with path filters on `models/`, `hardware-profiles/`.
Also supports `workflow_dispatch` for backfill.

Steps:
1. Identify merged PR(s) from the pushed commits.
2. Find successful `psap-forge-bot` comments on those PRs.
3. Parse MLflow run IDs from the comment body.
4. For each run: fetch metadata, download `kpis.json`, normalize.
5. Verify recipe/configuration attribution matches the merged recipe.
6. Create/update an evidence-import PR with:
   - Normalized `result.json` under the recipe's `results/` directory.
   - Provenance `run.yaml` with Forge block.
   - Updated `benchmark_runs` reference in `recipe.yaml`.
7. Evidence PR merge triggers the existing CI + catalog rebuild.

**Runner requirements:** Network access to MLflow (or a pre-fetched artifact
cache). Read-only MLflow credentials as a GitHub secret. No cluster access
needed.

### 5. UI updates

The current `site/app.js` already renders sweep data from
`entry.evidence[].result.sweep`. It groups by harness and shows
p50/p90/p95/p99 with toggles.

Add to each harness section:
- **"View run in MLflow"** link using `forge.mlflow_run_url` from the
  evidence record.
- **"View Forge execution"** link using `forge.mlflow_parent_run_id`.
- Expandable provenance showing tested revision, cluster, Prow job.

### 6. Cleanup

- Remove fabricated sweep data from
  `models/glm-5.2/recipes/h200-x8-tp8-guidellm-8k1k-contributor/results/`.
  Keep it only as `tests/fixtures/sample-sweep-guidellm.json`.
- Remove the v1 sweep data from legacy v3 paths.
- The `tests/fixtures/forge-kpis-pr34-example.json` file contains the real
  PR #34 `kpis.json` for test development.

## Smoke vs. full sweep

PR #34's execution used `ci-quick` (GuideLLM, 2 concurrency levels: C=1, C=5)
and `aiperf-smoke` (AIPerf, 1 concurrency level: C=1). These are CI validation
runs, not full performance sweeps.

The normalizer must record `completeness: smoke` for these and the UI should
display them appropriately (stat cards only, no sweep chart for ≤2 points).

Full sweep runs use workload keys like `guidellm-8k1k` (C=1,4,16,32,64,128)
and `aiperf-agentx-128k` (multiple concurrency levels, 1800s duration).

## Integration preview script

`tools/integration-preview.sh` rebuilds the `integration/catalog-preview`
branch from scratch by merging recipe PRs + the UI PR. Re-run it to test
changes. It auto-resolves trivial README/render.py conflicts.

Usage:
```sh
bash tools/integration-preview.sh --no-push   # local only
bash tools/integration-preview.sh --serve     # serve on localhost:8138
```

## Test plan

- Normalize PR #34's actual `kpis.json` → verify sweep array has correct
  units and concurrency values.
- Test with a smoke run (2 points) → verify UI shows stat cards but no
  charts (needs >1 point for lines).
- Test with fabricated full sweep fixture → verify all 4 percentiles render.
- Test missing p90 → verify chart renders with 3 percentiles.
- Test duplicate ingestion → verify no changes.
- Test stale revision → verify rejection.
- Test MLflow unavailable → verify graceful failure.
- Test AIPerf multi-concurrency → verify sweep assembly from multiple test
  entries.
