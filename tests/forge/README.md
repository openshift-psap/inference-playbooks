# Forge CI Integration

Run inference-playbook recipes through the Forge CI framework by posting
`/test forge` comments on pull requests.

## Importing an existing PR322 run before merge

### Maintainer workflow (open PR)

After `benchmark-import-pr.yml` is merged to `main`, a repository maintainer
can select **Import Forge evidence into open PR** in Actions. Supply the open
PR number and the numeric comment ID from its successful Forge report URL
(`...#issuecomment-<id>`). For PR #19's existing successful attempt:

```bash
gh workflow run benchmark-import-pr.yml --ref main \
  -f pr_number=19 -f report_comment_id=6492689346
```

The protected `forge-evidence` environment supplies the MLflow credentials and
evidence App configuration used by the post-merge importer. The App needs
repository contents and pull-request write permissions. The workflow verifies
the triggering maintainer, uses reviewed tooling from `main`, and validates
schema, manifest drift, and catalog generation before publication. Candidate
schemas/templates must match that tooling; merge reviewed changes first.

For same-repository branches, publication creates a signed evidence-only commit
on the existing PR. The commit atomically requires the exact prepared head: if
someone pushes during import, retry the workflow. No branch is force-pushed.
Fork PRs receive a downloadable patch and exact head SHA in the workflow
artifact; the contributor applies and commits that patch. Oversized evidence
also uses patch delivery. A patch is retained before attempting direct
publication, including when App permissions prevent a write.

Select each attempt explicitly. If deployment inputs change, fix the recipe,
rerun Forge, and select the new successful report. Existing result files are
immutable. Re-selecting an already imported execution re-reads its Forge
metadata and KPIs and compares the regenerated evidence with the committed
files. Matching evidence makes no commit; this requires MLflow access but no
GPU rerun. A receipt alone does not establish the selected experiment's results.
Evidence-only commits or unrelated tooling merges
do not require another GPU run. The original tested SHA is preserved. Failures
and delivery details appear in the Actions run summary.

This is an explicit maintainer action, not an automatic reaction to a Forge
comment. Required pre-merge evidence coverage policy is a separate gate.

### Local import

Forge PR322's bundle contract uses a `<model>/recipes/<recipe-folder>` selector
and an explicit recipe-relative `/path manifests/<stack>-<version>/<file>.yaml`.
The opaque-ID examples below describe older Forge versions. Pin the intended
Forge revision when testing an in-flight producer change.

Once the bundle-aware importer is available, an existing successful run can be
imported locally into the recipe branch, including after merging tooling changes
from `main`. Supply the **original recipe SHA recorded by Forge**, not the Forge
code SHA and not the new merge commit:

```bash
python3 tools/fetch_forge_evidence.py \
  --repo /path/to/recipe-checkout \
  --run-id <successful-parent-run-id> --pr-number <recipe-pr-number> \
  --tested-revision <original-tested-recipe-sha> \
  --workspace forge-inference-playbooks --require-parent
python3 tools/validate.py --current
```

The importer reads `MLFLOW_TRACKING_URI`, `MLFLOW_USERNAME` and `MLFLOW_PASSWORD`
from the environment. Review and commit the generated `results/` files and
`benchmark_runs` references in the same recipe PR. The current automated
`benchmark-ingest.yml` workflow still requires a merged PR; this local command
does not. Use trusted importer code when importing another contributor's branch.

Recipe, model, hardware, overrides, configuration and platform manifest inputs
must still match the tested revision. Adding evidence references is allowed;
changing maturity or serving configuration is not silently exempted. Bundle v1
accepts only absent-namespace placement, not legacy serving repairs. Import
currently supports one Deployment, LeaderWorkerSet or LLMInferenceService per
platform bundle and one workload/sweep identity per harness. Unknown bundle
versions, unsupported resource sets and mixed sweeps are rejected.

For bundle-v1 runs, sweep completeness is derived automatically from the
effective workload selection in the recorded execution config, the workload
profiles at the recorded **Forge SHA**, and generated benchmark Jobs. GuideLLM
uses the Job's effective concurrent profile (including applied argument
overrides); AIPerf uses the selected profile's planned concurrency list and
checks each Job against its KPI concurrency. Requested but unapplied comment
values are not treated as effective overrides. Unknown or ambiguous execution
settings fail rather than inventing a complete sweep from observed points.

Each imported run stores a sanitized expectation snapshot, source checksums and
artifact references. Local validation replays it without network access. Missing
points remain incomplete, and smoke profiles retain the `smoke` classification.

## Available workload keys

Workload keys are defined in RHAIIS `workloads.yaml` and selected via the
`/var inference_playbooks.workload` comment directive. Any key can be used
with any recipe — workload compatibility is not enforced by the catalog.

### GuideLLM workloads

Require `data`, `rates`, and `max_seconds` in the workload definition.

| Key | Description |
|-----|-------------|
| `profile1` | 1k/1k tokens, rates [1, 50, 100, 200, 300], 275s per rate |
| `ci-quick` | 1k/1k tokens, rates [1, 5], 60s per rate |

### AI Perf workloads

Use `aiperf_args` passthrough for scenario configuration.

| Key | Description |
|-----|-------------|
| `aiperf-agentx-128k` | AgentX MVP scenario, 128k context cap, concurrency 32 |
| `aiperf-agentx-unlimited` | AgentX MVP scenario, no context cap, concurrency sweep [1, 4, 16, 32] |
| `aiperf-smoke` | Synthetic 128/64 tokens, 10 requests (quick validation) |

## Comment examples

### GuideLLM benchmark

```text
/test forge
/cluster janus
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: kserve-e2e-perf
/var inference_playbooks.workload: profile1
```

### AI Perf AgentX 128k

```text
/test forge
/cluster janus
/exclusive true
/gpu nvidia 8
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: kserve-e2e-perf
/var inference_playbooks.workload: aiperf-agentx-128k
```

### Both workloads sequentially (same serving deployment)

```text
/test forge
/cluster janus
/exclusive true
/gpu nvidia 8
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: kserve-e2e-perf
/var inference_playbooks.workload: profile1,aiperf-agentx-128k
```

### AI Perf concurrency sweep (4 sequential runs)

```text
/test forge
/cluster janus
/exclusive true
/gpu nvidia 8
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: kserve-e2e-perf
/var inference_playbooks.workload: aiperf-agentx-unlimited
```

`aiperf-agentx-unlimited` defines `concurrency: [1, 4, 16, 32]`. Forge
expands this into 4 sequential AI Perf Jobs, each with its own artifact
directory. Cache isolation across runs is handled by AI Perf's built-in
`--cache-bust first_turn_prefix` mechanism.

### Smoke test (quick plumbing validation)

```text
/test forge
/cluster janus
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: kserve-e2e-perf
/var inference_playbooks.workload: aiperf-smoke
```

## Notes

- `/exclusive true` is recommended for AI Perf runs to avoid GPU contention
  during the benchmark.
- `/gpu nvidia 8` (or more) should match the recipe's GPU requirements.
- When running against an in-progress Forge PR, add
  `/var fournos.job.extra_env.PULL_PULL_SHA: <commit-sha>` to pin the Forge
  code version. Once the AI Perf integration merges into `openshift-psap/forge:main`,
  this override is no longer needed.
