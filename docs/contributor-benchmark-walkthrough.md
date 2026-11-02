# Run your first contributor benchmark

This walkthrough takes you from a checkout to a shareable benchmark result using
the portable Forge runner introduced in [PR #70](https://github.com/openshift-psap/inference-playbooks/pull/70).
The worked example deploys Gemma 4 on one H200 GPU, checks the pipeline with
GuideLLM, then shows how to run the full 8K-input/1K-output workload.

## Understand the handoff

```text
Contributor                           Maintainer
-----------                           ----------
Committed recipe + platform
    -> prepare immutable package
    -> run on your cluster
    -> review evidence for privacy
    -> archive + checksum ----------> validate without writing
                                      -> review provenance and metrics
                                      -> import and open evidence PR
```

The **runner image** contains Forge and the supporting tools and runs on your
machine. The **harness image** contains GuideLLM or AIPerf and runs in your
cluster. The **serving image** comes from the recipe and serves the model in
your cluster. You do not need to install Forge locally or build the runner.

Preparation is offline with respect to the cluster. Running deploys a workload
and consumes cluster resources. Import is a separate maintainer operation;
a successful run does not automatically publish results or promote recipe maturity.
No AWS, MLflow, or internal artifact credentials are needed by the contributor.

## 1. Get the tools and choose the example

Use a Linux AMD64 runner host with Git, Python, and Podman or Docker. Run these
commands in a new checkout:

```sh
git clone https://github.com/openshift-psap/inference-playbooks.git
cd inference-playbooks
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r tools/requirements.txt
python3 tools/validate.py --current

RECIPE=models/gemma-4/recipes/h200-x1-tp1-guidellm-8k1k/recipe.yaml
HARNESS_IMAGE=ghcr.io/vllm-project/guidellm@sha256:15372f9f3f407366495470706b7a07f64addcce5d134a49cab64149b44ab1233
RUNS="$HOME/playbooks-runs"
mkdir -p "$RUNS"
```

Keep this terminal open for subsequent commands. The harness digest above is
the GuideLLM v0.7.4 Linux AMD64 image used by the documented live smoke example.
The runner digest is selected automatically from
[`release.json`](../benchmarks/runner/release.json).

Before using this example, check the
[recipe](../models/gemma-4/recipes/h200-x1-tp1-guidellm-8k1k/recipe.yaml) and
[rendered manifest](../models/gemma-4/recipes/h200-x1-tp1-guidellm-8k1k/manifests/vllm-v0.24.0/deployment.yaml):

- Your existing namespace must have capacity for the manifest's one H200 GPU,
  CPU, and memory requests, and access to the serving and harness images.
- The model must be accessible. This manifest references Secret `hf-token`, key
  `HF_TOKEN`, and uses the `default` service account.
- The namespace must admit the authored non-root UID `65534`. The runner does
  not grant SCC permissions or change the authored UID to bypass admission.
- Supply a self-contained kubeconfig, with embedded credentials/certificates,
  readable by your container user. External exec authentication is unsupported.
  On SELinux hosts, the kubeconfig needs an appropriate container-readable label.
- Confirm [cluster permissions](contributor-benchmarks.md#cluster-permissions)
  with your administrator. GuideLLM uses shared emptyDir/sidecar result collection;
  AIPerf needs working default PVC provisioning for results.

For different hardware, select a matching recipe and its declared platform.
If configuration changes are needed, edit the source recipe/overrides, render,
validate, and commit them before preparing. Keep the tested commit available for
maintainer review. Do not edit the generated package.

## 2. Prepare a smoke-test package

```sh
PACKAGE_DIR="$RUNS/gemma-smoke-package-001"
OUTPUT_DIR="$RUNS/gemma-smoke-output-001"

python3 tools/prepare_contributor.py "$RECIPE" \
  --stack vllm --version v0.24.0 \
  --workload guidellm-smoke \
  --harness-image "$HARNESS_IMAGE" \
  --output "$PACKAGE_DIR"
```

The command prints JSON containing a generated `run_id` and package path. Both
the package and output paths must be new; do not create either directory yourself.
The parent `$RUNS` directory already exists from step 1.

| Argument | Meaning |
| --- | --- |
| Recipe path | Repository-relative `recipe.yaml` with committed, clean inputs. |
| `--stack` / `--version` | An existing platform entry and its rendered manifest. |
| `--workload` | The benchmark definition; smoke checks plumbing, not performance. |
| `--harness-image` | Compatible, cluster-pullable benchmark image pinned by digest. |
| `--output` | New package directory, separate from the later run output directory. |

Inspect the pinned request:

```sh
python3 -m json.tool "$PACKAGE_DIR/request.json"
```

The package contains `run.sh`, `monitor.sh`, `monitor.py`, `request.json`, and
`playbooks/`. If moving it to another runner host, copy the **whole directory**
and preserve executable bits. It contains the input snapshot, not credentials,
model weights, or a Git checkout. One package represents one execution attempt.

## 3. Run against your cluster

Replace the three values below with your dedicated kubeconfig, an explicit
context from that file, and an existing namespace. If your cluster needs a proxy,
set its proxy environment variables in this terminal before launching.

```sh
KUBECONFIG_FILE=/absolute/path/to/benchmark.kubeconfig
CONTEXT=your-cluster-context
NAMESPACE=your-benchmark-namespace

"$PACKAGE_DIR/run.sh" "$KUBECONFIG_FILE" "$CONTEXT" "$NAMESPACE" "$OUTPUT_DIR"
```

The launcher pulls the pinned runner image and mounts the package and kubeconfig
read-only. Podman is preferred; prefix the command with `CONTAINER_ENGINE=docker`
to select Docker. Keep the runner terminal alive until completion. The Forge
test phase has a six-hour default timeout; preflight and evidence collection can
take additional time.

In another terminal, watch the **same output directory** using local Python 3.9+:

```sh
"$HOME/playbooks-runs/gemma-smoke-package-001/monitor.sh" \
  "$HOME/playbooks-runs/gemma-smoke-output-001" --watch
```

Omit `--watch` for a single snapshot. The monitor shows phase, elapsed time,
latest Forge task, last observed readiness, and final outcome. Ctrl-C here stops
only the monitor. Its exit code is not the benchmark's success status.

## 4. Interpret and return the result

For the runner, exit code `0` means success; `1` means an unsuccessful attempt.
The printed JSON distinguishes `failed`, `interrupted`, and `collection-failed`.
An unsuccessful bundle is diagnostic-only, not importable successful evidence.

```text
gemma-smoke-output-001/
  evidence.tar.gz             # share after privacy review
  evidence.tar.gz.sha256      # share with the archive
  progress.json              # local monitoring only
  private-diagnostics/       # local logs/reports; do not send
```

Review the archive for sensitive content before sharing. Automated sanitization
does not replace human review; see the
[privacy contract](contributor-benchmarks.md#privacy-and-review).
Send only the archive and checksum sidecar through your team's approved delivery
channel. Include a short handoff message, for example:

```text
Recipe: models/gemma-4/recipes/h200-x1-tp1-guidellm-8k1k/recipe.yaml
Platform: vllm v0.24.0
Workload: guidellm-smoke (plumbing check only)
Run ID / tested revision: copied from request.json
Source PR or branch: include if recipe inputs changed
Outcome: copied from runner output
Files: evidence.tar.gz and evidence.tar.gz.sha256
Privacy review: completed
```

## 5. Run the performance workload separately

Once the smoke check succeeds, prepare a **fresh package** for the full workload:

```sh
python3 tools/prepare_contributor.py "$RECIPE" \
  --stack vllm --version v0.24.0 \
  --workload guidellm-8k1k \
  --harness-image "$HARNESS_IMAGE" \
  --output "$RUNS/gemma-8k1k-package-001"

"$RUNS/gemma-8k1k-package-001/run.sh" \
  "$KUBECONFIG_FILE" "$CONTEXT" "$NAMESPACE" \
  "$RUNS/gemma-8k1k-output-001"
```

This workload uses 8000 input / 1000 output tokens and concurrent streams
1, 4, 16, 32, 64, and 128, with 300 seconds per stream count. Deployment, model
loading, and collection add time. Monitor and return its evidence using the same
steps as above, substituting the new package/output paths. Never relabel smoke
results as full workload coverage.

## Maintainer: turn the handoff into an evidence PR

Use a clean feature worktree based on `main`, with Python dependencies installed
and the tested commit available in Git. If the contributor changed inputs,
review and integrate those source changes first: the import worktree's recipe
inputs must match the tested inputs.

```sh
git switch -c evidence/contributor-gemma-run
CHECKSUM=$(cat /received/evidence.tar.gz.sha256)
python3 tools/import_contributor.py /received/evidence.tar.gz \
  --checksum "$CHECKSUM"
```

This first command validates without writing. Confirm the checksum through the
delivery channel; it checks integrity, not independent execution attestation.
Review validated input snapshots, runtime identity, KPIs, normalized results,
and sanitized native exports using a trusted archive viewer. Then apply:

```sh
python3 tools/import_contributor.py /received/evidence.tar.gz \
  --checksum "$CHECKSUM" --apply --reviewed
python3 tools/validate.py --current
python3 tools/catalog.py
git diff --check
git status --short
git diff
```

The importer adds `results/<run-id>/` and `benchmark_runs` references. Review and
stage the intended files, commit, push your feature branch, and open a PR against
`main`. Import does not commit or publish anything and does not change maturity.
See the [maintainer reference](contributor-benchmarks.md#maintainer-validate-review-and-import)
for replay, repeat imports, and optional S3 retention.

## Troubleshooting your first attempt

| Symptom | What to do next |
| --- | --- |
| Package/output directory already exists | Prepare a new package with a new path/run ID and choose a new output path. Do not reuse an attempt. |
| Inputs are not committed and clean | Render and validate source changes, commit them, then prepare again. |
| Kubeconfig or admission preflight fails | Check explicit context, embedded credentials, file access, RBAC, and authored UID requirements. Correct source/configuration before a fresh attempt. |
| Model or benchmark never becomes ready | Inspect local private diagnostics for image pulls, model access, scheduling, and storage failures. |
| Monitor stops updating | Check the runner process and private logs; stage age is not a heartbeat. |
| Benchmark ran but outcome is `collection-failed` | Inspect local collection diagnostics. Do not treat the partial attempt as successful evidence. |
| Runner interrupted or host crashed | Ask an administrator to reconcile recorded run-owned resources before retrying. Some interruption paths bypass cleanup; early failures may leave no archive. |
| Import rejects the tested revision or inputs | Obtain the contributor's exact source commit and matching inputs; do not edit the archive to make it pass. |

For other supported resource kinds, AIPerf, cleanup guarantees, and current
limitations, use the [portable contributor benchmark reference](contributor-benchmarks.md#reference).
