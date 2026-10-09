# Single-node vLLM companions

A companion is another explicit platform in the **same recipe**, not a duplicate
recipe or an undeclared renderer output. Preparation is an input-authoring step:

```bash
python3 tools/prepare_companions.py models/<model>/recipes/<recipe>/recipe.yaml
python3 tools/validate.py --current
python3 tools/render.py models/<model>/recipes/<recipe>/recipe.yaml
python3 tools/check_manifests.py models/<model>/recipes/<recipe>
```

Omit the recipe argument to prepare all canonical recipes. Review the diff before
committing: preparation reserializes recipe YAML and writes the platform override
file, but never commits or renders. Repeating preparation is byte-idempotent.
Unrelated existing override files are not overwritten. `--check` is read-only.
CI/pre-commit validation and drift checks enforce explicit inputs and generated
outputs; the renderer never writes `recipe.yaml` or platform overrides.

## Version and image source of truth

An active single-node non-vLLM source without an explicitly authored vLLM target
requires a companion. Existing explicitly authored vLLM entries are preserved;
this policy does not retroactively regenerate, deduplicate, or reinterpret their
version choices. Multi-node recipes are excluded. Blocked source platforms do
not produce deployable counterparts.

By default the companion uses the **same effective image** and exact engine
resolved by `tools/engine_versions.py`: an image-bound declaration, or the
maintained default-runtime release index. There is no tag guessing, patch-version
carry-forward, latest selection, or automatic image upgrade. Release-index
resolution is not an inspection of that particular container or runtime proof.
The standalone entry gets an image-bound engine declaration pointing to the
original declaration source or the maintained index. Upstream numeric platform
versions use the existing `v` prefix so flag-constraint keys continue to match.

Preparation copies source platform serving overrides (including flag-level args,
environment, image, and resources), not source deployment/benchmark assessments.
Each platform then uses the existing per-platform constraint engine. Required
unsupported flags still fail; optional removals follow the declared constraint
rules. Missing interconnect capabilities are not inferred or newly required by
this policy. The standard Deployment template's prerequisites still apply,
including its `hf-token` Secret reference; preparation does not create credentials
or storage infrastructure.

To explicitly choose a newer engine, first author a reviewed override file with
fully qualified `image`, `image_usage`, and an image-bound `engine` declaration
with HTTPS provenance. Then run:

```bash
python3 tools/prepare_companions.py models/<model>/recipes/<recipe>/recipe.yaml \
  --version <newer-engine-version> --overrides platforms/vllm-newer.yaml
```

Only comparable numeric releases can be selected as newer. Older, incomparable
vendor/development builds, mismatched image/version declarations, and newer
selections without provenance are rejected. Opaque builds can be reused exactly
when their identity fits a platform path. Multiple missing source counterparts
require explicitly authored targets rather than guessing a preferred source.

## Declarative local weights and runtime settings

Keep `serving.model` as the exact checkpoint identity. A pre-populated PVC's
explicit mount contract selects the **runtime local path**, without replacing
that checkpoint metadata or provisioning storage:

```yaml
serving:
  model: example/exact-checkpoint
  served_model_name: example-served-alias
  shared_memory:
    size: 16Gi
  probes:
    startup:
      path: /health
      port: 8000
      failure_threshold: 180
      period_seconds: 10
    readiness:
      path: /v1/models
      port: 8000
      period_seconds: 10
deployment:
  scope: single-node
  storage:
    type: pvc
    pvc:
      name: example-weights
      mount_path: /mnt/models
      model_path: /mnt/models  # Optional; defaults to mount_path, may name a child.
      read_only: true
```

Claim name, class, access mode, and capacity are deployment-selected metadata;
neither template creates a PVC or downloads weights. Model paths must be inside
the mounted path; mounts cannot mask `/dev`, `/proc`, `/sys`, `/tmp`, or the
template cache. Preparation requires an explicitly read-only PVC contract.

TP RHOAI and vLLM Deployment templates render the local model path, distinct
served alias, exact image, resources, probes, weights volume/mount and separate
memory-backed shm. Local-weight RHOAI uses explicit argv, just like Deployment;
raw JSON values remain a single argument. The controller additional-args path
uses shell-safe quoting for non-local RHOAI. **Do not pre-quote JSON in args**:
preparation rejects legacy shell serialization overrides rather than guessing
how to strip them. An absent CPU limit stays absent in explicit local-weight
mode, rather than inventing a default cap below a declared CPU request.

Without these new inputs, existing cache/security/probe/shm defaults remain
compatible. `shared_memory.size` can also explicitly enable shm with TP1; the
legacy default is still 4Gi for TP>1. It is a serving field and can be replaced
by a platform override. Probe overrides and served aliases reuse existing fields.
No new PP, router, or P/D template support is implied.

## Platform-owned overlays and migration

Optional `platforms[].config` names a Kustomize directory under `config/`;
`config: null` explicitly means **no overlay**. If omitted, legacy
`serving.config_overrides: true` still selects shared `config/`. Every prepared
vLLM companion gets `config: null`, never the source's overlay selection.

For automatic mapping, legacy unscoped overlays are blocked. Explicitly scoped
overlays are built in isolation and must leave the canonical runtime and object
identity unchanged. Only the informational `kubernetes.io/description` annotation
may differ. Unknown labels/annotations can drive admission/security/network
policy and are not assumed harmless. Extra resources, generators, transformers,
remote inputs, changed runtime/security, and escaping paths are blocked. This is
an output-contract check, **not arbitrary JSON-patch translation**. Explicitly
authored platform configurations still use the normal renderer's Kustomize path.

To migrate already reviewed runtime overlays:

1. Put their known settings into canonical fields: existing alias/probes/image,
   explicit PVC mount/model/read-only fields, and `shared_memory.size`.
2. Remove superseded runtime patches from the active Kustomization. Do not keep
   both a declarative volume and an append-volume patch. Preserve source history
   and attribution; this command does not rewrite or delete patches for you.
3. Scope any remaining platform-owned overlay with `platforms[].config`, or use
   `null` when none remains. Custom identified RHOAI images now render directly;
   a redundant image-only patch may be retained if it does not change output.
4. Remove redundant controller-specific shell pre-quoting overrides: canonical
   args contain raw JSON, and the template owns serialization.
5. Prepare, validate, render, and check drift. Review both platform manifests.

Unknown custom runtime/security patches require an explicitly reviewed target,
not an automatic claim that they can be mapped.

## Independent verification

Every prepared entry contains:

```yaml
verification:
  maturity: day-zero
  deployment_status:
    state: needs-verification
    note: Prepared configuration only; no deployment or benchmark verification.
  benchmark_runs: []
```

Optional `platforms[].verification` is a complete independent assessment; an
absent assessment means **no platform-local assessment**, never fallback to
recipe-wide `maturity`, `deployment.status`, or `benchmark_runs`. Existing
recipe-wide metadata retains its legacy meaning. Consumers must not label a new
counterpart validated because its recipe or source platform was validated.

Platform `validated`/`production` requires `deployment_status.state: verified`
(with date/method) and non-empty platform benchmark references. Each referenced
run must be inside the recipe and indexed by the normal validator, with matching
recipe ID, deployment scope, hardware profile, and explicit identity:

```yaml
platform:
  stack: vllm
  version: v1.2.3  # Example only; must match the platform actually benchmarked.
```

Runs lacking `run.platform` remain usable as legacy recipe-wide evidence but
cannot establish platform-local verification. No source benchmarks are copied.
This metadata is not a promotion command; collect actual evidence before changing
the independent assessment.

## Mapping blockers and reviewed escape path

Automatic preparation fails without writing if the engine is unresolved or if
the current Deployment template cannot preserve the source's contract:

- pinned manifests, unscoped legacy overlays, or scoped overlays with unknown
  metadata/runtime/security changes;
- storage other than an explicit pre-populated read-only PVC contract;
- prefill/router configuration, unsupported source templates, PP topology;
- non-NVIDIA GPU resource mapping, incompatible runtime variants, extra device
  resource keys, conflicting explicit GPU counts, or TP*DP beyond
  the declared node capacity;
- conflicting effective env (including template-owned `HF_TOKEN`) or parallelism
  flags instead of the structured parallelism contract.

Legacy generic Deployment defaults remain unchanged. Recognized declarative
storage, alias, probes and shm are rendered explicitly; unknown source requirements
are not replaced by defaults. Shared RHOAI overlays are **never automatically
applied to Deployment**.

For a blocked mapping, explicitly author and review the vLLM platform and its
own pinned manifest if needed. Give that target its own unverified assessment
using the initialization above; do not copy source deployment/benchmark status.
Existing renderer limitations still apply to explicitly authored targets.
Do not blindly reuse a kind-specific shared overlay: select each platform's own
`config` explicitly and review its output.
Review checkpoint/alias, MTP JSON quoting, parallelism, cache paths, storage,
probes, shared memory, resources, security, and prerequisites before claiming
equivalence. This policy does not implement overlay translation, a downloader,
Forge integration, P/D, or wide-EP support.

Prepared `companion` metadata binds the target to its source stack/version and
`same`/`newer` policy. Validation and rendering detect source/version drift;
same-version targets must preserve serving inputs. Stale targets require reviewed
input updates, not silent regeneration. Configuration derivation is never
deployment or benchmark validation.
