# Portable contributor benchmarks

Related: [issue #62](https://github.com/openshift-psap/inference-playbooks/issues/62).

The contributor runs a selected, committed recipe with **local pinned Forge** and
returns a versioned evidence archive through any approved sharing medium.
Maintainers validate/import it and optionally upload retained artifacts. The
contributor needs **no AWS credentials, S3 client, Prow/Fournos service, internal
vault, MLflow, or internal artifact access**.

```text
Maintainer prepares package -> contributor deploys/benchmarks -> local bundle
  -> approved sharing medium -> maintainer inspects/replays/reviews
  -> optional maintainer storage upload -> evidence PR
```

## Current boundary

- Linux AMD64 runner image, Python 3.12, OpenShift CLI 4.19.0, and Forge
  `768a285851ade70d0d5737f9bd90564350410469` from PR-322. Source archives and
  base image are pinned/checksummed; Python dependencies are hash-locked.
- Deployment, LeaderWorkerSet, or PVC-backed LLMInferenceService, with one
  self-contained manifest file and optional Service in the same YAML stream.
  P/D bundles, InferenceService, custom DRA allocation, and heterogeneous serving
  image digests are not admitted by this first contract. Unsupported layouts
  fail, rather than being translated into another topology.
- `guidellm-8k1k`: 8000/1000 tokens, concurrent streams 1/4/16/32/64/128,
  300 seconds per stream count. `guidellm-smoke` and `aiperf-smoke` check plumbing
  only. The supplied harness image must be an audited compatible version
  (GuideLLM v0.7.4 or AIPerf 0.12.0), referenced by digest.
- AgentX is deliberately rejected until its external corpus/tokenizer revision
  and checksum acquisition are audited. A date in a dataset name is not an
  immutable-content proof. Do not substitute smoke evidence for AgentX.
- The first live acceptance target is the existing Gemma single-GPU Deployment
  below. Container/synthetic proof does not establish GPU performance, topology
  correctness, or live LWS/LLMInferenceService compatibility.

The maturity-policy work in #62 remains separate. Neither runner nor importer
changes maturity, deployment verification, or image recommendations. In
particular, successful import does not mean `contributor-validated`, independent
`validated`, or production-ready. Review qualifying workload coverage, error
rates/quality, known issues, actual hardware and model provenance before proposing
graduation. Evidence belongs only to the tested platform/checkpoint/workload.

## Maintainer: build and prepare

Use a trusted checkout/toolchain. The selected recipe and deployment inputs must
already be committed, rendered, validated, and clean. Preparation does not run
historical renderers or deploy anything.

```sh
python3 -m pip install -r tools/requirements.txt
python3 tools/validate.py --current
podman build -f benchmarks/runner/Containerfile \
  -t quay.io/YOUR_ORG/playbooks-contributor:reviewed .
```

Publish the reviewed image through your normal process. Resolve that published
image and the compatible harness image to digests; set `RUNNER_IMAGE` and
`HARNESS_IMAGE` to fully qualified `registry/repository@sha256:...` references.
No image is published automatically by the tooling or PR CI.

```sh
python3 tools/prepare_contributor.py \
  models/gemma-4/recipes/h200-x1-tp1-guidellm-8k1k/recipe.yaml \
  --stack vllm --version v0.24.0 --workload guidellm-8k1k \
  --runner-image "$RUNNER_IMAGE" --harness-image "$HARNESS_IMAGE" \
  --output /tmp/opencode/gemma-contributor-package
```

The package includes `run.sh`, `request.json`, and the allowlisted Git input
closure under `playbooks/`. The request records the exact tested commit,
platform, manifest, input checksums, workload, Forge revision, image pins, and
unique run ID. It does not contain a Git checkout, arbitrary recipe scripts,
credentials, existing results, or model weights. Distribute this directory as
a reviewed package; the launcher itself is executable maintainer-supplied code,
unlike a returned evidence archive.

Each prepared package is **one attempt**. Prepare a new package/run ID for a new
attempt; never merge measurements from different attempts into a sweep.

## Contributor: run and share

Prerequisites:

- Podman or Docker, image/model registry access and sufficient cluster egress.
- A self-contained kubeconfig with embedded credentials/certificates and an
  explicit context; external exec authentication plugins are unsupported.
- An existing namespace with appropriate namespaced permissions, NVIDIA GPU
  capacity, working default benchmark-PVC storage, and required controllers.
- Any recipe-referenced local secrets/populated model PVCs. Operators, storage
  infrastructure, model-access permissions, and cluster changes are your
  prerequisites, not automatically installed or modified by this runner.
- Permission to inspect node capacity/software metadata. This is not an
  exclusive GPU reservation or independent proof of interconnect topology.

Review the inputs and expected GPU/time cost before launching:

```sh
./gemma-contributor-package/run.sh \
  /absolute/path/to/kubeconfig EXPLICIT_CONTEXT benchmark-namespace \
  /absolute/path/to/NEW-output-directory
```

The launcher chooses Podman first, otherwise Docker (`CONTAINER_ENGINE` can
select one), uses the caller's UID, mounts inputs/credentials read-only, and
uses Podman keep-id mapping and host networking so cluster proxies work. Proxy environment variables are
forwarded; no AWS environment or home directory is forwarded. The mounted
kubeconfig parent directory must be readable by the container; on SELinux hosts
use a dedicated credential directory with an appropriate container-readable
label. The launcher deliberately does not relabel a shared kubeconfig directory.

The runner creates a private temporary flattened kubeconfig with the requested
context/namespace; it does not edit the original. It preflights permissions and
required PVC/Secret references, invokes Forge's existing deployment/workload/
finalizer flow, and captures serving configuration/readiness/image identity
before and after the workload. The default overall deadline is six hours.
Forge imports and execution use its installation directory, matching its native
launcher so DSL task registration and lookup agree regardless of contributor cwd.
Cleanup targets Forge-created names, not existing model PVCs or secrets. An
uncatchable kill/host failure can still leave resources; inspect local diagnostics
and reconcile only the recorded run-owned resources, without force-deletion.

### Non-root UID admission

For complete Deployment/LWS Pod templates, the runner performs a **Pod**
server-side dry run in the target namespace with the declared service account.
It first preserves the authored UID. Only an explicit `runAsUser` admission
rejection permits a second, UID-omitted candidate, and that candidate must retain
`runAsNonRoot: true` and receive a concrete positive UID. Authentication/network
errors, missing assignments, and root/container-level overrides fail closed.
The runner never infers UID assignment from the presence of OpenShift APIs,
copies unrelated admission mutations, grants SCC permissions, or allows root.

Dry runs use the caller's identity, which can differ from a workload controller's
identity. They are preflight evidence, not a controller-admission guarantee.
The real serving Pod's effective UID (container setting overrides Pod setting)
and service account are checked against the probe **before** benchmarking and
again afterward. `runtime.json` records the decision and admitted security
contexts, and offline import verifies the exact UID transformation. Native
hosted-Forge historical evidence retains its existing validation behavior.
Deployment runtime comparison permits Kubernetes' standard read-only
`kube-api-access-*` mount only after checking its exact projected token,
`kube-root-ca.crt`, and namespace sources. Authored mounts and other additions
still require an exact match; a familiar mount name alone is insufficient.
LLMInferenceService controller-expanded templates are not guessed into complete
Pods; their authored policy remains unchanged and actual serving Pods must show
concrete non-root UIDs before benchmarking.

The output contains:

```text
evidence.tar.gz
evidence.tar.gz.sha256
private-diagnostics/        # LOCAL ONLY; do not share this directory
```

**Share only the archive and checksum sidecar**, after reviewing their contents
for your organization's privacy requirements. Private diagnostics and console
logs can contain endpoints and other identifying details and are never included
automatically. Credentials, prompts/responses, host identifiers, and arbitrary
resource/log dumps are not acceptable published evidence.

Native JSON exports retain numeric metrics. The source-side exporter removes
known host-identity fields and replaces the exact generated private endpoint
with `${ENDPOINT}`. `runtime.json` records original/export SHA-256 values and
redacted field paths; these exports are explicitly sanitized, not described as
byte-identical native originals. Unknown sensitive content blocks collection
instead of being silently published. Review remains necessary.

Bounded adapters address PR-322's local path: admission-aware UID handling above;
AIPerf declaration labels are
also written to its parser's source metadata, and benchmark Jobs may reuse the
serving manifest's unambiguous `HF_TOKEN` Secret reference (key `HF_TOKEN`) for
gated tokenizer access. Only the reference is passed; no Secret contents are read
into the bundle. Forge still owns deployment, workload execution, parsing, and
cleanup. Actual harness invocation settings are recorded in `runtime.json`.

Nonzero exit outcomes distinguish failed, interrupted, and collection-failed
attempts. They can return diagnostic-only bundles, but the maintainer importer
will not admit them as successful benchmark evidence. Very early input errors
or uncatchable kills may prevent archive creation; local data is not uploaded
or erased. Preserve attempts independently.

## Maintainer: inspect, review, import

Work in a clean, trusted feature worktree based on `main`, with the tested commit
available in Git. The importer does not fetch commits, execute bundle code, run
historical renderers, or need GPU/cluster access. Confirm the checksum through
the contributor's approved delivery channel; a self-reported checksum is not
an execution attestation.

```sh
python3 tools/import_contributor.py /received/evidence.tar.gz \
  --checksum sha256:THE_RETURNED_SHA256
```

This is read-only by default: safe bounded archive parsing, canonical input
closure/Git attribution, platform/model identity, captured launch equivalence,
ready serving roles and consistent image digests, privacy guards, and independent
KPI-to-result replay. Source-side native exports remain available for review.
The existing repository normalizer consumes the same authoritative Forge KPI
format as hosted Forge imports; this does not independently re-attest how
contributor-controlled raw measurements or KPIs were produced.

Perform the human privacy/provenance review before applying:

```sh
python3 tools/import_contributor.py /received/evidence.tar.gz \
  --checksum sha256:THE_RETURNED_SHA256 --apply --reviewed
```

Without external storage, sanitized native exports are retained locally in the
run directory. For bulky artifacts, maintainers can instead upload the complete
approved archive to durable S3 storage using their own AWS CLI v2 credentials:

```sh
python3 tools/import_contributor.py /received/evidence.tar.gz \
  --checksum sha256:THE_RETURNED_SHA256 --apply --reviewed \
  --s3-prefix s3://TEAM_OWNED_BUCKET/accepted/contributor-evidence
```

Only this maintainer command uses AWS. Conditional writes prevent overwrite;
S3 SHA-256 confirmation is required. A retry may reuse an existing object only
after verifying its checksum. Bucket access/retention are maintainer policy;
accepted evidence must not expire under an intake lifecycle. Upload failure
preserves the received bundle and does not create repository references.

The importer adds only `results/<run-id>/` files and additive `benchmark_runs`
references. It retains `request.json`, `runtime.json`, `kpis.json`, and
`result.json` for offline CI replay, plus either local native exports or a
durable `s3://` artifact reference with SHA-256. Repeated imports are idempotent;
different bytes under an existing run identity fail. It neither commits/pushes
nor opens/merges PRs or changes maturity. Normal `validate.py`, pre-commit and CI
replay contributor evidence without accessing S3. Replay binds an existing run
to its historical Git snapshot, so later assessment or configuration changes do
not rewrite/invalidate the original measurement or claim that newer inputs were
tested. New imports still require current-input equality; historical evidence
does not qualify an untested upgrade or platform for graduation.

```sh
python3 tools/validate.py --current
python3 tools/catalog.py
git diff --check
# Review changes and open an evidence PR through normal repository policy.
```

## Bundle format v1 and verification

`bundle.json` contains `schema_version: 1`, the immutable request, execution
`outcome`, and an exact file/checksum inventory. Payload files are allowlisted
input snapshots, `kpis.json`, `runtime.json`, `result.json`, and native JSON
exports under `raw/`. Archives are reproducible and contain no filesystem owner
or timestamp metadata. The outer SHA-256 is in the sidecar.

Intake rejects path traversal, duplicate/noncanonical names, symlinks/hardlinks,
devices, executables, sparse files, unsupported payloads, duplicate JSON/YAML
keys, excessive structure depth, mismatched inventories and changed results.
Limits: 1024 files, 32 MiB/member, 256 MiB compressed/expanded intake ceiling;
the producer uses a smaller payload ceiling to leave space for headers.

Local/CI tests use synthetic tooling data, never published model evidence:

```sh
python3 -m pytest tests/test_contributor.py -q
podman build -f benchmarks/runner/Containerfile -t localhost/contributor-runner:test .
podman run --rm --network none --entrypoint python \
  -v "$PWD/tests/contributor_container_smoke.py:/smoke.py:ro,Z" \
  localhost/contributor-runner:test /smoke.py
podman run --rm --network none --entrypoint python -e SMOKE_HARNESS=aiperf \
  -v "$PWD/tests/contributor_container_smoke.py:/smoke.py:ro,Z" \
  localhost/contributor-runner:test /smoke.py
```

The container test invokes the **real pinned Forge CLI and Caliper subprocesses**;
only Kubernetes and benchmark Jobs are simulated. It verifies collection and
finalizers without network or credentials. A live GPU run, published runner
digest, AgentX data contract, and graduation-policy integration are separate
release gates. Updating Forge, the CLI, or dependencies requires rerunning these
proofs; regenerate `requirements.lock` from `requirements.in` deliberately, not
at contributor runtime.
