# Portable contributor benchmarks

Contributors prepare a pinned recipe package themselves, run it on their own
cluster, and return **two files**: `evidence.tar.gz` and
`evidence.tar.gz.sha256`. Maintainers review and import the results; they do not
need to prepare a package or run infrastructure for the contributor.

Contributors need **no AWS, S3, MLflow, Prow/Fournos, or internal artifact access**.
This is separate from [manually applying benchmark Jobs](../benchmarks/README.md).
Related: [issue #62](https://github.com/openshift-psap/inference-playbooks/issues/62).

## Contributor: prepare, run, and return evidence

### 1. Prepare your package

Start from a trusted checkout of this repository. Select a recipe and platform
whose requirements match your cluster. The selected recipe and inputs must be
rendered, validated, committed, and clean. Preparation does not deploy anything.
If someone has already supplied a complete package, skip to step 2.

Install the preparation dependencies in a local Python environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r tools/requirements.txt
python3 tools/validate.py --current
```

Use a reviewed runner image by digest, or build it yourself. This local-build
example uses Podman and does **not** require publishing the runner image:

```sh
podman build -f benchmarks/runner/Containerfile \
  -t localhost/playbooks-contributor:reviewed .
RUNNER_IMAGE=$(podman image inspect localhost/playbooks-contributor:reviewed \
  --format '{{index .RepoDigests 0}}')
```

For Docker, use a published runner by digest, or publish your build and resolve
its registry digest. An image ID is not a substitute for a manifest digest.
When sharing a package with another machine, use a runner reference that machine
can pull; the local Podman image above is only available on your build host.

Set `HARNESS_IMAGE` to an audited compatible GuideLLM v0.7.4 or AIPerf 0.12.0 image
by digest. Unlike the local runner, the harness image must be pullable by your
cluster. For the GuideLLM example, this is the Linux AMD64 v0.7.4 image used in
the live smoke test:

```sh
HARNESS_IMAGE=ghcr.io/vllm-project/guidellm@sha256:15372f9f3f407366495470706b7a07f64addcce5d134a49cab64149b44ab1233
PACKAGE_DIR="$HOME/playbooks-runs/gemma-package-001"
python3 tools/prepare_contributor.py \
  models/gemma-4/recipes/h200-x1-tp1-guidellm-8k1k/recipe.yaml \
  --stack vllm --version v0.24.0 --workload guidellm-smoke \
  --runner-image "$RUNNER_IMAGE" --harness-image "$HARNESS_IMAGE" \
  --output "$PACKAGE_DIR"
```

`guidellm-smoke` selects Forge's `ci-quick` plumbing check, not performance
validation. For the full 8K/1K sweep, prepare a separate package with
`--workload guidellm-8k1k`. The package directory must not already exist.

Keep the **complete directory**: `run.sh`, `monitor.sh`, `monitor.py`,
`request.json`, and `playbooks/`. Preserve the shell scripts' executable bits.
The scripts are trusted checkout code; the returned evidence archive is data only.
The request pins the tested commit, platform, workload, Forge revision, images,
input checksums, and unique run ID. A package contains no Git checkout, credentials,
model weights, or existing results. One package is one attempt.

### 2. Check cluster prerequisites

Review `request.json` and the selected manifest under `playbooks/`. Do not edit
the generated package; make any necessary changes in the source checkout instead.

- Use a Linux AMD64 machine with Podman or Docker and access to the runner image.
- Provide a self-contained kubeconfig with embedded credentials/certificates and
  an explicit context. External exec authentication plugins are unsupported.
- Use an existing namespace with enough NVIDIA GPU capacity, required controllers,
  image/model access, and any manifest-referenced Secrets or populated model PVCs.
- Allow the cluster to reach the required image registries and model/tokenizer
  downloads. Operators, permissions, storage, and network setup are prerequisites;
  the runner does not install or repair them.
- For GuideLLM, this pin uses shared emptyDir/sidecar result collection; no default
  results-PVC storage class is needed. AIPerf creates a results PVC and needs
  working default PVC provisioning. Model-storage requirements are separate.
- Have the [cluster permissions listed below](#cluster-permissions). Preflight
  checks are partial; passing them does not guarantee workload or collection access.

**Gemma example below:** the serving manifest needs one H200 GPU and a Secret
named `hf-token` with key `HF_TOKEN`. It uses the `default` service account and
fixed non-root UID **65534**, which must be admissible in the target namespace.
If these requirements cannot be met, correct the source recipe/configuration,
render, validate, and commit the changes, then prepare a fresh package. Keep
`runAsNonRoot: true`; do not rewrite packaged inputs or grant SCC privileges to
make the test run. Include your source branch/commit or PR when returning evidence
so reviewers can obtain the exact tested revision.

On SELinux hosts, use a dedicated kubeconfig directory with a container-readable
label. The launcher does not relabel that credential directory. Its parent
directory and kubeconfig file must be readable by your container user.

### 3. Run once

Use the package path from step 1 (or the package you received), your kubeconfig,
context, and namespace. The output directory must **not already exist**.

```sh
"$HOME/playbooks-runs/gemma-package-001/run.sh" \
  /absolute/path/to/kubeconfig EXPLICIT_CONTEXT benchmark-namespace \
  "$HOME/playbooks-runs/gemma-output-001"
```

Podman is preferred when both engines exist; set `CONTAINER_ENGINE=docker` to
override. The launcher uses your UID, host networking, and inherited proxy
variables. It does not mount your home directory or forward AWS credentials.
The default overall deadline is six hours; actual runtime depends on the workload.

### 4. Monitor from another terminal (optional)

The package includes a read-only monitor requiring only local **Python 3.9+**:

```sh
"$HOME/playbooks-runs/gemma-package-001/monitor.sh" \
  "$HOME/playbooks-runs/gemma-output-001" --watch
```

Use the same output directory you passed to `run.sh`; it is where progress is
written, not a new destination. Omit `--watch` for one snapshot. No kubeconfig,
cluster CLI, credentials, or extra Python packages are needed for monitoring.
Ctrl-C stops only the monitor, not the test.
The monitor's exit status indicates whether monitoring worked, not whether the
benchmark succeeded or its evidence was accepted.

It shows the recorded phase, elapsed/stage time, latest native Forge task,
last observed serving readiness, benchmark configuration, and final execution
outcome. Readiness is an observation, not continuous health monitoring. Stage age
is not a heartbeat, and no overall completion percentage is guessed. An abruptly
killed runner can leave a stale snapshot; inspect its process/private logs then.
A successful execution still needs separate maintainer import validation/review.

### 5. Review and return the result

```text
gemma-output-001/
  evidence.tar.gz
  evidence.tar.gz.sha256
  progress.json              # local status only; do not return this file
  private-diagnostics/       # keep local; do not share this directory
```

**After privacy review, share only the archive and checksum sidecar** through
your approved delivery channel. See [privacy and review](#privacy-and-review).

For `run.sh`, exit code **0** means success; **1** means an unsuccessful attempt.
The printed JSON and bundle `outcome` distinguish `failed`, `interrupted`, and
`collection-failed`. Inspect `private-diagnostics/` locally on failure. Failed
bundles are diagnostic-only and cannot be imported as successful evidence.
Very early failures or an uncatchable kill may produce no archive.

**For another attempt, prepare a fresh package/run ID and use a new output
directory.** Keep each attempt separate. After a crash, ask your administrator
to reconcile recorded run-owned resources; never force-delete unrelated objects.

## Maintainer: validate, review, and import

Use a clean, trusted feature worktree based on `main`, with the tested commit
available in Git. Confirm the checksum through the contributor's delivery channel;
a supplied checksum is an integrity check, **not an execution attestation**.
If the contributor changed the recipe, review those source changes too. New
imports require the recipe inputs in your worktree to match the tested inputs.

```sh
CHECKSUM=$(cat /received/evidence.tar.gz.sha256)
python3 tools/import_contributor.py /received/evidence.tar.gz \
  --checksum "$CHECKSUM"
```

This is read-only by default. It validates archive safety, the input closure
against Git, launch/runtime identity, image pins, privacy patterns, and KPI replay.
It prints a summary; it does **not** perform human review or extract files for you.
Inspect the validated input snapshots, `runtime.json`, `kpis.json`, `result.json`,
and sanitized native exports under `raw/` with a trusted archive viewer. Do not
execute bundle content or blindly extract an unvalidated archive.

After human privacy/provenance review:

```sh
python3 tools/import_contributor.py /received/evidence.tar.gz \
  --checksum "$CHECKSUM" --apply --reviewed
python3 tools/validate.py --current
python3 tools/catalog.py
git diff --check
```

Review the diff and open an evidence PR. The importer adds `results/<run-id>/`
and additive `benchmark_runs` references; it does not commit, push, open PRs, or
change maturity. Repeating an identical import is safe; different evidence under
the same run ID is rejected. Ordinary local/CI validation replays retained
evidence offline against its historical inputs, not newer recipe configurations.

### Optional: maintainer S3 retention

Instead of retaining native exports as individual local files, add a durable S3
destination to the **initial** reviewed apply command:

```sh
python3 tools/import_contributor.py /received/evidence.tar.gz \
  --checksum "$CHECKSUM" --apply --reviewed \
  --s3-prefix s3://TEAM_OWNED_BUCKET/accepted/contributor-evidence
```

Only this step needs maintainer AWS CLI v2 credentials. Conditional writes and
S3 SHA-256 confirmation prevent silent overwrite. Upload failures preserve the
received bundle and do not create repository references. Keep accepted evidence
under durable retention, not an expiring intake policy.

## Reference

### Supported inputs and evidence limits

- Runner: Linux AMD64, Python 3.12, OpenShift CLI 4.19.0, Forge PR #322 commit
  `93b85db135425516eb000de62d2483c62cdacacc`. Archives/base image are pinned and
  checksummed; Python dependencies are hash-locked.
- One platform and one self-contained manifest containing one Deployment,
  LeaderWorkerSet, or PVC-backed LLMInferenceService. Authored Services, companion
  objects, P/D, InferenceService, custom DRA, LLMInferenceService hostPath volumes,
  and heterogeneous serving image digests are unsupported.
- `guidellm-8k1k`: 8000/1000 tokens, concurrent streams 1/4/16/32/64/128,
  300 seconds per stream count. `guidellm-smoke` and `aiperf-smoke` check plumbing
  only. AgentX remains rejected until its external corpus/tokenizer revision and
  checksum acquisition are audited; smoke evidence is not AgentX evidence.
- Synthetic/container tests do not establish GPU performance or live distributed
  compatibility. Import never grants independent validation, production readiness,
  deployment verification, or recipe maturity. Evidence covers only tested inputs.

### Cluster permissions

Ask your cluster administrator to check permissions for the selected kind/harness:

- Cluster scope: get the selected namespace; list nodes for capacity/software facts.
- Namespace scope: get/list/create/delete the serving resource, Services, Pods,
  Jobs, and PVCs. Current preflight requires PVC permissions even for GuideLLM.
- Read controller ownership chains: ReplicaSets and Deployments; StatefulSets for
  LWS and applicable controller-expanded workloads. Allow readiness watches and
  rollout/status reads for the selected serving resource.
- Read referenced PVCs/Secret metadata, Pod logs and relevant events; create
  `pods/exec` sessions for result collection. Benchmark helpers use apply, so allow
  the required create/patch operations for their Jobs, helper Pods, and PVCs.
- Workload service accounts must be able to pull images and satisfy admission
  policy. Deployment/LWS Pod server-side dry runs use the caller's identity, which
  may differ from controller admission. Preflight is not a complete RBAC audit.

### Launch and admission contract

The runner flattens kubeconfig into a private temporary file without editing the
original. Forge selects `<model>/recipes/<recipe-folder>` and a recipe-relative
`manifests/<platform>/<file>.yaml`, not an opaque `recipe_id` search.

A read-only adapter serves Forge's Git lookups from the package's checksummed
snapshot. It does not fetch Git or independently attest the contributor's source;
maintainer import verifies the exact closure against the real tested commit.
Forge's source/submission checks still allow only placement into an omitted
namespace. It preserves authored serving names and refuses preexisting conflicts.

**Serving resources** use create-only requests, UID-owned Pod capture, and
UID-preconditioned cleanup. Endpoint discovery does not synthesize a Service.
These guarantees do **not** extend to all benchmark helper resources: the pinned
harness helpers still use apply and name-based cleanup. Existing model PVCs and
Secrets are prerequisites, not cleanup targets.

Deployment/LWS Pod probes preserve authored UIDs. UID rejection fails without a
UID-omitted retry; correct and commit the recipe before preparing another package.
The runner neither grants SCC permissions nor allows root. Actual Pod UIDs and
service accounts must match the probe before and after benchmarking. Controller-
expanded LLMInferenceService templates are not guessed into complete Pods, but
their actual serving Pods must still show concrete non-root UIDs.

Offline import verifies the unchanged launch. Runtime comparison permits only
Kubernetes' validated standard read-only `kube-api-access-*` mount in addition to
authored mounts, including OpenShift's exact standard service-CA projection when
present. Benchmark Jobs can reuse an unambiguous `HF_TOKEN` Secret
reference with key `HF_TOKEN`; Secret values are not copied into the archive.

### Privacy and review

Private diagnostics, console logs, and HTML reports can contain cluster endpoints
or identifying details. Keep them local. Never publish credentials, private
prompts/responses, host identifiers, or arbitrary resource/log dumps as evidence.

Native JSON exports retain numeric metrics but remove recognized host-identity
fields and replace the generated private endpoint with `${ENDPOINT}`.
`runtime.json` records original/export checksums and redacted paths; these are
sanitized exports, not byte-identical originals. **Detected prohibited fields or
patterns block collection; unrecognized sensitive content may remain, so review
is required.** Contributor-controlled evidence is not independent attestation.

### Local HTML reports

Forge generates `*__postprocessing/kpis/kpis.html` under `private-diagnostics/`.
This pin includes AIPerf concurrency charts grouped by explicit `sweep_id` labels;
per-run scalar KPIs remain unchanged. KPI HTML generation is separate from the
disabled plugin `visualize` step. Reports are not in the shareable archive;
separate sharing requires privacy review.

### Legacy packages and archive format

Bundles pinned to `768a285851ade70d0d5737f9bd90564350410469` remain importable and
replayable under historical launch rules, including bounded UID omission. New
packages use the current pin; this runner refuses old-pin execution. Keep the
old reviewed runner for an unfinished attempt, or prepare a fresh package/run ID.
Never relabel historical measurements.

Older packages have no monitor helper/progress snapshots. From a trusted checkout,
`python3 tools/monitor_contributor.py /path/to/output` can still show their final
reported bundle outcome after checksum verification; it does not reconstruct
unrecorded live phases or perform importer validation.

Bundle v1 contains `bundle.json` (request, outcome, exact checksum inventory),
allowlisted input snapshots, replay files, and native JSON exports. Intake rejects
unsafe paths/types, executable content, duplicates, excessive structure, changed
results, and inventory mismatches. Limits: 1024 files, 32 MiB/member, 256 MiB
compressed/expanded intake ceiling; the producer leaves space for archive headers.

### Developer verification

Run from the repository root. These tests use synthetic data, not model evidence:

```sh
python3 -m pytest tests/test_contributor.py -q
podman build -f benchmarks/runner/Containerfile -t localhost/contributor-runner:test .
podman run --rm --network none --entrypoint python \
  -v "$PWD/tests/contributor_container_smoke.py:/smoke.py:ro,z" \
  localhost/contributor-runner:test /smoke.py
podman run --rm --network none --entrypoint python -e SMOKE_HARNESS=aiperf \
  -v "$PWD/tests/contributor_container_smoke.py:/smoke.py:ro,z" \
  localhost/contributor-runner:test /smoke.py
```

Both smoke tests invoke the real Forge CLI and Caliper; only Kubernetes and
benchmark Jobs are simulated. Rerun these proofs after pin/CLI/dependency changes.
Regenerate `requirements.lock` from `requirements.in` deliberately, not at runtime.
