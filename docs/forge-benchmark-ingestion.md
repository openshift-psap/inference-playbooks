# Forge benchmark evidence ingestion

`benchmark-ingest.yml` finds merged recipe PRs on a `main` push, or backfills
one merged PR through `workflow_dispatch`. It selects only the latest explicit
successful `psap-forge-bot[bot]` report, ordered by GitHub `created_at` then ID.
The primary `[mlflow results]` or `[MLFlow Test Results]` link must identify a
parent run on the configured origin/workspace. Nested run/log links are ignored.
Failed, ambiguous, stale, unavailable, or unattributed evidence fails closed.
No cluster access is used. Imports open a separate evidence PR, never push to
`main`, and never promote maturity or platform verification.

Current Forge executions publish consolidated KPIs on the selected parent at
`01__test/<ordinal>__postprocessing/kpis/kpis.json`. Import these without
enumerating children; ignore `historical_data` archives, never substitute them.
Legacy executions without consolidated parent KPIs retain direct-child intake.
The selected execution must match the final recipe PR head. For tagless current
executions, use recorded `FORGE_FOREIGN_TESTING_PULL_PULL_SHA`, not the Forge
checkout's `PULL_PULL_SHA`. This is the CI-requested/tested recipe context, not
an independent attestation of the actual checkout. A missing/malformed primary link,
empty execution, attribution mismatch, unsupported/malformed artifact, or
permission/network error fails ingestion. **There is no fallback to older
executions.** No trusted success report and automation evidence PRs remain no-ops.

Successful complete ingestion writes one positive `forge-import.json` receipt
beside its first output `run.yaml`. The parent must be `FINISHED`; for legacy
direct-child discovery, any nonterminal direct child blocks admission before
writes. Terminal `FAILED`/`KILLED` children remain ineligible. The receipt binds the entire normalized positive output set,
source PR/revision, origin/workspace/parent identity, recipe ownership, and Git
blob identities for each raw/result/run file. It stores no empty-run outcomes.

Repeat ingestion builds one local lookup from committed recipe references and
anchored receipts. A selected admitted execution is a successful no-op with no
MLflow requests or KPI replay. Receipt/source files must be committed and clean;
blob identities, references, metadata, and current deployment attribution must
agree. Unreferenced receipts cannot authorize a skip. Full CI integrity/replay
validation remains a separate gate; admission receipts assume reviewed importer
output and attest a completed discovery snapshot, not future server immutability.
Legacy evidence without a receipt requires normal selected-execution discovery
and validation once before admission; immutable evidence and original comment
provenance are preserved. Direct `fetch_forge_evidence.py` still accepts child
links but does not create ingestion receipts.

The standalone ingestion change (PR1) adds the evidence tools, schemas,
offline validator replay, tests, and workflow without depending on site/catalog
tooling or changing existing recipes. The catalog/UI change (PR2) consumes this
contract to show individual runs, smoke stat cards, and MLflow/Prow provenance
links. It adds a site-build check after ingestion validation; generated bundles
remain build artifacts and are not included in the evidence import PR.
The separate Pages workflow builds and publishes reviewed evidence after merge
to `main`; ingestion itself does not publish the site or promote recipe maturity.

## Execution metadata

Consolidated parent imports do not require custom `playbooks.*` tags. When these
tags are incomplete, intake reads existing parent artifacts to populate the
unchanged benchmark-run fields:

- `01__test/000__ci_metadata/fournos_fjob.yaml`: foreign recipe PR number and
  requested/tested recipe SHA from `spec.env`, checked against the final PR SHA.
- `01__test/001__inference_playbooks_test_dir/config.yaml`: explicit recipe and
  manifest selection, matched to the tested recipe, model, profile, and platform.
- `01__test/run.log`: the recorded common Forge test invocation. It is not
  reconstructed from configuration.

Within that test directory, the serving inputs are fixed by kind:

| Kind | Launch configuration | Runtime digest source |
| --- | --- | --- |
| Deployment | `src/<unique-name>-deployment.yaml`, `vllm` container arguments | Matching ready serving-container status in `01__test/run.log` |
| LeaderWorkerSet | `src/<unique-name>-lws.yaml`, nested leader/worker templates and shell `vllm serve` arguments | `<ordinal>__capture_lws_state/artifacts/leaderworkerset.pods.yaml` |
| LLMInferenceService | `src/<unique-service-name>.yaml`, `spec.model`, template/worker configuration and captured serving-pod arguments | `<ordinal>__capture_llmisvc_state/artifacts/llminferenceservice.pods.yaml` |

Only the known `src/` and test directory are listed to resolve variable launch
names and capture ordinals. There is no recursive runtime-artifact search or
alternate digest-source fallback. LWS selects `vllm`, `vllm-leader`, or
`vllm-worker` containers from its role templates. LLMInferenceService selects
the `main` container running vLLM, not router, sidecar, or init containers.
Declared revisions come from serving arguments or `VLLM_ADDITIONAL_ARGS`;
omission uses the accepted `main` default. Model-less LWS workers share the
leader's model load. LLMInferenceService supports HF and PVC model URIs; PVC
contents, cache preparation, and weight revisions are not independently checked.
The recipe and ordinary run model labels supply checkpoint identity. A local
`/mnt/models` path or `spec.model.name` request alias is not treated as an HF ID.

Serving roles must provide ready runtime image digests. Different serving-role
digests are explicitly unsupported by the existing single-`image_digest` contract;
no role is silently preferred. Missing or ambiguous required records reject
intake. LLMInferenceService prefill-role layouts remain unsupported.

Any supplied attribution tags must agree with acquired execution values. No
execution logs/configuration are committed: run-level provenance uses the existing
parent MLflow URL, while raw KPIs and their existing checksum remain unchanged.

The existing model identity checks are unchanged: new KPI `served_model` labels
identify the exact served checkpoint, alongside declared base model/quantization.
Historical base-only labels do not establish a different quantized checkpoint.

### Existing tagged execution contract

Legacy tagged intake remains supported. The parent/benchmark run must be
`FINISHED`. Parent tags are inherited; child tags override them. Its tags are:

| Tag | Meaning |
| --- | --- |
| `playbooks.recipe_path` | Canonical `models/<model>/recipes/<recipe>/recipe.yaml` |
| `playbooks.tested_revision` | Exact 40-character PR head SHA tested by Forge |
| `playbooks.pr_number` | Source recipe PR number |
| `playbooks.hardware_profile` | Exact root-relative profile path |
| `playbooks.hardware_profile_revision` | Profile revision at execution |
| `playbooks.platform_stack` | Recipe platform stack, not the host OS/platform |
| `playbooks.platform_version` | Exact recipe platform version |
| `playbooks.model_revision` | Declared model revision tested: `main`, tag, branch, or commit hash |
| `playbooks.image_digest` | Resolved `sha256:…` image digest |
| `playbooks.command` | Exact reproducible harness/Forge invocation, without secrets |

The model repository is the tested recipe's `serving.model`. Its declared
revision is recorded exactly as supplied, without resolving it to a commit hash.
Tagged intake supplies `main` explicitly for the default revision. Tagless
execution intake uses the serving arguments' default when omitted. Identifiers must be
nonempty, contain no whitespace or control characters, and pass the shared
artifact privacy guard. A Hugging Face repository with its declared revision
is sufficient model identity; a model commit pin is not required.

Optional `playbooks.prow_job_id` and `playbooks.prow_job_url` add execution
links. `playbooks.expected_concurrencies` is a JSON object, for example
`{"guidellm": [1,4,16,32,64,128], "aiperf": [1,4,16]}`. A result is `full-sweep`
only when its points exactly match the explicit intended levels. Otherwise
it is `partial`; ≤2 points, `ci-quick`, and smoke workload keys are `smoke`.

The real PR #34 execution tested recipe SHA `eecf4808…`, not its final PR head
`e4fddc2…`. It is ineligible for import, regardless of tag availability or the
separate Forge SHA. Its fixture is usable for normalizer tests, **not automatic
publication**. Its JSON formatting is
compact, but all source values, labels, and unknown KPIs are retained.
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

# Alternatively select the latest successful bot report for a merged PR (requires gh):
GITHUB_REPOSITORY=openshift-psap/inference-playbooks \
  python3 tools/ingest_forge.py --pr-number 34

python3 -m pytest tests/test_forge_evidence.py -q
python3 tools/validate.py --current
```

Each harness receives its own `results/forge-<artifact-run-id>-<harness>/` containing
unaltered `kpis.json`, normalized `result.json`, and `run.yaml` provenance.
For consolidated parent KPIs, the artifact run ID and both existing MLflow run-ID
fields identify the parent; GuideLLM and AIPerf retain separate harness outputs.
SHA-256 and deterministic offline normalization are checked by the shared
validator in local/pre-commit/CI. Unknown KPIs stay in the raw artifact.
Missing percentiles are null, GuideLLM latency seconds become milliseconds,
and AIPerf scalar tests assemble by intended concurrency. Summary metrics use
the highest observed concurrency, explicitly recorded as `reference_concurrency`.

Raw artifacts must be sanitized **at source**: no private prompts, credentials,
hostnames/IPs. Intake rejects common sensitive fields and obvious IP/credential
strings; this is a guard, not a replacement for evidence-PR review. Cluster
labels must be public aliases. With PR2 applied, the evidence workflow validates
and checks catalog/site generation before opening the import PR. The UI displays
only referenced committed runs; smoke and partial evidence are labeled rather
than presented as full sweeps. Synthetic sweep fixtures are test-only, not
published model evidence.
