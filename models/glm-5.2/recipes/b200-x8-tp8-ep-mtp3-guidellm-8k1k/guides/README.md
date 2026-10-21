# GLM-5.2 NVFP4-FP8 on one eight-B200 node

This [contributed recipe](../recipe.yaml) consolidates Yuchen Fama's
[RHOAI 3.5 submission (#4)](https://github.com/openshift-psap/inference-playbooks/pull/4)
and [RHOAI 3.4 submission (#3)](https://github.com/openshift-psap/inference-playbooks/pull/3).
It is not benchmark-validated, accuracy-validated or production-hardened.
GuideLLM 8K/1K is a planned benchmark target, not a published result.

| Platform | Configuration ownership | Final artifact |
|---|---|---|
| RHOAI 3.5 | Shared serving block + platform override + Kustomize overlay | [Generated LLMI](../manifests/rhoai-3.5/llminferenceservice.yaml) |
| RHOAI 3.4 | Hand-authored pin, preserved from PR #3 | [Pinned LLMI](../manifests/rhoai-3.4/glm-5-2-aggregated-b200-llmisvc.yaml) |

Both artifacts intentionally use the same resource name. Select exactly one
platform for a namespace; do not apply both as concurrent services there.

## Prerequisites

- The matching RHOAI/KServe `LLMInferenceService` v1alpha2 API, all referenced
  `LLMInferenceServiceConfig` objects and a configured inference gateway.
- A node labelled `nvidia.com/gpu.product: NVIDIA-B200`, eight free GPUs,
  NVIDIA GPU operator and sufficient allocatable CPU, host memory and local
  ephemeral storage for the [recipe's resource requests](../recipe.yaml).
  Requests are contributed sizing, not independently established minimums.
  The [hardware profile](../../../../../hardware-profiles/nvidia-b200-8x-r1.yaml)
  records only supplied GPU type/count; driver, CUDA and topology need review.
- Secret `llm-d-hf-token`, key `HF_TOKEN`, in the deployment namespace with
  access to the checkpoint. Use an approved credential provisioning path;
  never commit tokens.
- Writable PVCs `glm-5-2-hf-cache` and `glm-5-2-jit-cache` in that namespace.
  Choose capacity, access mode and storage class for the target cluster, and
  ensure both claims mount on the B200 node and are writable by the admitted
  pod UID. The HF cache is prepopulated before deployment; the JIT cache is
  filled at runtime.
- Container-registry and Hugging Face access, plus PyPI/package-host access
  for the startup dependency. The runtime keeps `HF_HUB_OFFLINE=0`; a warmed
  cache avoids transferring the full checkpoint again while its model revision
  is current, but this configuration is not disconnected-ready.
- Inspect namespace SCC admission for both the upstream init image and main
  image. No SCC grants or privileged access are created by this recipe.

## Prepare and deploy

### Prepopulate the HF cache

The RHOAI serving manifests and Forge benchmark manifest mount
`glm-5-2-hf-cache` at
`/var/cache/huggingface` as `HF_HUB_CACHE`. Before the first run, populate that
claim with the [download Job](../../../model-ops/download-nvfp4-hf-cache.yaml).
It downloads `hf://RedHatAI/GLM-5.2-NVFP4-FP8` into this PVC using the
`llm-d-hf-token` Secret (`HF_TOKEN`). The
[example PVC](../../../model-ops/glm52-hf-cache-example.yaml) requests 1 TiB
with `ReadWriteOnce` and uses the cluster's default storage class; configure it
for the target B200 storage. The inspected model snapshot was about 447 GB
[at this Hub revision](https://huggingface.co/RedHatAI/GLM-5.2-NVFP4-FP8/tree/68629bdc58d2aa3398f4a32848839be938aad785),
but the recipe follows the model's default revision, so confirm current size,
quota, access and permissions before creating the claim. For node-local
storage, schedule the Job on the B200 node that will serve the model.

From the repository root, check for the claim first. Reuse it if it is Bound,
large enough and mountable by the serving pod. Apply the example PVC only if
the claim does not exist:

```bash
export NAMESPACE=your-namespace
oc get -n "${NAMESPACE}" pvc glm-5-2-hf-cache
# Run only if the PVC is missing.
oc apply -n "${NAMESPACE}" -f models/glm-5.2/model-ops/glm52-hf-cache-example.yaml
oc wait -n "${NAMESPACE}" --for=jsonpath='{.status.phase}'=Bound pvc/glm-5-2-hf-cache --timeout=10m
oc apply -n "${NAMESPACE}" -f models/glm-5.2/model-ops/download-nvfp4-hf-cache.yaml
oc wait -n "${NAMESPACE}" --for=condition=complete job/download-glm52-nvfp4-hf-cache --timeout=6h
```

The Job checks that `config.json` exists and must complete before serving
starts. If it fails, inspect its logs and events, correct the cause, then
delete and recreate the Job. Completed Jobs are retained, so applying one again
does not rerun the download. Keep the HF cache PVC between runs; the separate
`glm-5-2-jit-cache` PVC is populated at runtime. HF access remains enabled, so
a cache miss or changed default model revision can still download files; this
recipe is not offline-ready.

From the repository root, install the documented tooling dependencies and
Kustomize, then run:

```bash
python3 tools/validate.py --current --require-converted-raw
python3 tools/render.py models/glm-5.2/recipes/b200-x8-tp8-ep-mtp3-guidellm-8k1k/recipe.yaml
python3 tools/check_manifests.py models/glm-5.2/recipes/b200-x8-tp8-ep-mtp3-guidellm-8k1k
```

Both artifacts default to `replicas: 0`. Prepare a deployment copy of the
selected final artifact, set `spec.replicas: 1` only after capacity and
prerequisites are checked, then perform server-side dry-run before applying
it in the intended namespace. Do not hand-edit the generated 3.5 artifact.
Permanent 3.5 Kubernetes changes belong in `config/rhoai-3.5.yaml`; serving
changes belong in the recipe or platform override, followed by regeneration.

### RHOAI 3.5

The overlay keeps the contributed init container, cache layout, versioned
config refs, resource name, probes and scaling default. Serving flags are
generated from the recipe, rather than duplicated in its launch command.
The source's sequence/speculation arithmetic is resolved to a capture size
of 256; update it consistently if changing sequence count or MTP tokens.
The adapter requires whitespace-free argument values; speculative JSON is
compact and is split without `eval`, preserving JSON quotes.

TLS flags and probe schemes follow KServe's `.GlobalConfig.EnableTLS` during
controller reconciliation. With TLS enabled, certificates come from the
referenced base config at `/var/run/kserve/tls`. With TLS disabled, both
startup and probes use HTTP. Inspect the reconciled pod and workload Service
before inferring gateway compatibility. Explicit `storageInitializer.enabled:
false` preserves cache-based loading instead of downloading to `/mnt/models`.
The converted configuration has not been deployed or tested on either TLS
setting; the contributor's earlier smoke report applies to the raw input.

### RHOAI 3.4

The pin is byte-for-byte PR #3's input. Shared flags, overlays and platform
override edits do **not** alter it. Its HTTP-only startup can conflict with
TLS-enabled inherited probes/gateway settings, and its inherited startup
budget may be too short for cold download/JIT. Check the installed configs
and resolved pod before using it. Any correction to this pin is a separate
reviewed deployment change, not an automatic consequence of 3.5 conversion.

## Verification and benchmark evidence

For each platform separately, inspect admission, init completion, effective
image version, cache writes, model load, GPU allocation, cold-start time,
resolved TLS/probes and gateway routing. Verify actual response content from
`/v1/models` and `/v1/chat/completions`, not only the HTTP status. Then run the
shared GuideLLM workload with full model/image/hardware/environment provenance
and record sanitized raw output plus normalized results before promoting
maturity. No performance or accuracy claim is inherited across platforms.

See [reader notes](notes.yaml) and [original inputs/provenance](../raw-manifest/README.md).
