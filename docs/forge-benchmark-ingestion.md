# Forge benchmark evidence ingestion

`benchmark-ingest.yml` finds merged recipe PRs on a `main` push, or backfills
one merged PR through `workflow_dispatch`. It accepts explicit successful
`psap-forge-bot` comments containing MLflow run URLs from the configured origin.
Failed, ambiguous, stale, unavailable, or unattributed evidence fails closed.
No cluster access is used. Imports open a separate evidence PR, never push to
`main`, and never promote maturity or platform verification.

This standalone ingestion change (PR1) adds the evidence tools, schemas,
offline validator replay, tests, and workflow. It does not add or depend on
site/catalog tooling, change existing recipes, or publish benchmark metrics
to a UI. A future UI change (PR2) can consume this evidence contract to show
individual runs, smoke stat cards, and MLflow/Prow provenance links.

## Required Forge contract

The parent/benchmark run must be `FINISHED`. Parent tags are inherited; child
tags override them. Forge must supply these explicit tags:

| Tag | Meaning |
| --- | --- |
| `playbooks.recipe_path` | Canonical `models/<model>/recipes/<recipe>/recipe.yaml` |
| `playbooks.tested_revision` | Exact 40-character PR head SHA tested by Forge |
| `playbooks.pr_number` | Source recipe PR number |
| `playbooks.hardware_profile` | Exact root-relative profile path |
| `playbooks.hardware_profile_revision` | Profile revision at execution |
| `playbooks.platform_stack` | Recipe platform stack, not the host OS/platform |
| `playbooks.platform_version` | Exact recipe platform version |
| `playbooks.model_revision` | Immutable model revision tested |
| `playbooks.image_digest` | Resolved `sha256:…` image digest |
| `playbooks.command` | Exact reproducible harness/Forge invocation, without secrets |

Optional `playbooks.prow_job_id` and `playbooks.prow_job_url` add execution
links. `playbooks.expected_concurrencies` is a JSON object, for example
`{"guidellm": [1,4,16,32,64,128], "aiperf": [1,4,16]}`. A result is `full-sweep`
only when its points exactly match the explicit intended levels. Otherwise
it is `partial`; ≤2 points, `ci-quick`, and smoke workload keys are `smoke`.

The real PR #34 fixture lacks the required attribution tags. It is usable for
normalizer tests, **not automatic publication**. Its JSON formatting is
compact, but all source values, labels, and unknown KPIs are retained.
Add attribution tags in Forge before enabling production ingestion.
Names/GPU counts are never guessed from paths, cluster aliases, or KPI labels.
KPI model/version labels, when present, must agree with the selected
model/vLLM platform.

Recipe, model, profile, overrides, pinned manifests, and config inputs must
match the tested Git snapshot. Only additions to `benchmark_runs` are ignored
for replay/idempotence. Updating an existing immutable run with different
content is rejected. Distinct workloads within one harness artifact are
rejected rather than merged; use separate benchmark runs/artifacts.

## Runner and secrets

Create protected environment `forge-evidence`, with:

- Variables: `MLFLOW_TRACKING_URI`, `MLFLOW_WORKSPACE` (defaults to
  `forge-sandbox`), optional `MLFLOW_INSECURE_TLS=true` for self-signed TLS.
  Prefer a trusted CA and leave insecure TLS disabled.
- Secrets: read-only `MLFLOW_USERNAME`, `MLFLOW_PASSWORD`.
- Optional repository-level `FORGE_INGEST_RUNNER`: JSON runner labels with MLflow
  network access, e.g. `["self-hosted", "forge-evidence"]`. Default is `ubuntu-latest`.
  A custom runner must provide Bash, Git, and the GitHub CLI (`gh`).
- Optional `FORGE_INGEST_GITHUB_TOKEN`: GitHub App/fine-grained token with
  contents/PR write permission. This is needed to trigger normal PR CI from
  the automated PR; events created with `GITHUB_TOKEN` generally do not trigger
  other workflows. Enable repository permissions for automated PR creation.

Dispatch the workflow from `main`; other refs are deliberately excluded.

Credentials are read from the environment, never CLI arguments or output.
HTTP requests include `X-MLflow-Workspace`, have timeouts/size limits, and
reject redirects to avoid credential leakage. The bot workflow checks out
trusted `main`, never executes code from the recipe PR head, and uses unique
import branches so unrelated pending imports are not overwritten.

## Local/offline use

```sh
python3 tools/normalize_kpis.py tests/fixtures/forge-kpis-pr34-example.json \
  --output /tmp/opencode/forge-normalized --run-id forge-example \
  --deployment-scope single-node --accelerator-key nvidia-h200-x8

# With MLflow credentials in the environment and the tested SHA available in Git:
python3 tools/fetch_forge_evidence.py --run-id <parent-or-child-run-id> \
  --pr-number 34 --tested-revision <PR-head-sha> --tracking-uri <https-uri>

# Alternatively discover the successful bot comment for a merged PR (requires gh):
GITHUB_REPOSITORY=openshift-psap/inference-playbooks \
  python3 tools/ingest_forge.py --pr-number 34

python3 -m pytest tests/test_forge_evidence.py -q
python3 tools/validate.py --current
```

Each harness receives its own `results/forge-<child-id>-<harness>/` containing
unaltered `kpis.json`, normalized `result.json`, and `run.yaml` provenance.
SHA-256 and deterministic offline normalization are checked by the shared
validator in local/pre-commit/CI. Unknown KPIs stay in the raw artifact.
Missing percentiles are null, GuideLLM latency seconds become milliseconds,
and AIPerf scalar tests assemble by intended concurrency. Summary metrics use
the highest observed concurrency, explicitly recorded as `reference_concurrency`.

Raw artifacts must be sanitized **at source**: no private prompts, credentials,
hostnames/IPs. Intake rejects common sensitive fields and obvious IP/credential
strings; this is a guard, not a replacement for evidence-PR review. Cluster
labels must be public aliases. The evidence workflow validates only; UI/catalog
generation and publication are deliberately left to the future PR2 integration.
