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
  Choose capacities for checkpoint/download headroom and JIT artifacts after
  inspecting the checkpoint. Capacities, access modes and storage class were
  not supplied. Ensure the claims can mount on the B200 node, filesystem
  permissions allow the admitted pod UID to write, and storage can tolerate
  rolling replacement pods. Reuse requires serial deployment if access or
  cache writer constraints do not permit overlap.
- Container-registry and Hugging Face access, plus PyPI/package-host access
  for the startup dependency. An empty PVC is a cache, not a prepopulated
  local model path. This configuration is not disconnected-ready.
- Inspect namespace SCC admission for both the upstream init image and main
  image. No SCC grants or privileged access are created by this recipe.

## Prepare and deploy

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
