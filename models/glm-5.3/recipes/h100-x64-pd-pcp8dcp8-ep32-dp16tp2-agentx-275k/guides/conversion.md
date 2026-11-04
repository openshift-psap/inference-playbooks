# Offline conversion review

This is a **day-zero draft**, not a validated deployment. Source:
`project-cb-lab/curvebender-tools` commit
`aed915a4ebf2d8fc39d526388cc430f399179bc6`, directory
`deployments/glm-5.3/rits-roce-h100-canary`.

## Ownership

- `../raw-manifest/` preserves sanitized intake. Do not deploy from it after conversion.
- `../recipe.yaml` owns catalog identity and a summary of the serving configuration.
- `../config/` owns the complete executable configuration, including both roles,
  the patch ConfigMap, PodMonitor and Helm values. The template renderer cannot
  represent this deployment's asymmetric parallelism.
- `../manifests/pinned/bundle.yaml` is the Kustomize snapshot of config inputs.
  The schema requires pins under `manifests/`; this is a documented exception
  to normal template-generated manifest ownership. Rebuild it, do not hand-edit.
- `../manifests/llm-d-v0.11.0/bundle.yaml` is the renderer's verbatim copy of the pin.

The recipe's top-level TP2/DP16 describes **decode only**, not all 64 GPUs.
Prefill uses four 8-GPU nodes (DP4, PCP8, DCP8, TP1, EP32); decode uses another
four (DP16, TP2, EP32). The `v0.11.0` platform version identifies the default
router chart/EPP, not the vLLM engine or the v0.10.0 routing sidecar.

## Evidence needed before merge

1. Upstream model metadata is verified from
   [config.json at ba32f8c19c8738e2c57c71798c601d477a7bf844](https://huggingface.co/zai-org/GLM-5.3/resolve/ba32f8c19c8738e2c57c71798c601d477a7bf844/config.json):
   `glm_moe_dsa`, `GlmMoeDsaForCausalLM`, FP8 E4M3 with dynamic activations
   and 128x128 weight blocks (selected modules excluded). Confirm that the
   deployed PVC contains the intended model revision; upstream metadata does
   not establish the contents of that volume. FP8 KV cache is independent.
2. Inspect the pinned serving image/build record for the vLLM version/commit.
   Add `serving.engine` with `name: vllm`, the exact version, matching image
   reference and an HTTPS source that substantiates that version. Do not use
   the llm-d build tag as the vLLM version.
3. Verify a maintained public equivalent supports all submitted flags,
   MultiConnector/TieringOffloadingSpec, KV events and the startup patch.
   Update both roles and recipe metadata together only after verification.
   A changed build also requires review of the versioned JIT-cache directory.
4. Confirm the provisional `aiperf-agentx-unlimited-context` workload label.
   No benchmark metrics or maturity upgrade are implied.
5. Confirm physical GPU topology, memory capacity, RoCE routing and eligibility
   on the target hardware. The new profile records submission requirements,
   not independently measured capacity.

The missing custom-image engine declaration intentionally prevents full
validation; do not hide it using placeholder versions or a
blocked platform simply to make CI green.

## Cluster preparation

Review the [submitted prerequisites](../raw-manifest/README.md#prerequisites).
Before deploying, adapt the namespace, service account/SCC permissions, PVC,
model subPath, eight NADs, RoCE resource, rail gateways and host cache path in
`config/`. Label only eligible healthy nodes with
`rits.node-model-cache-enabled=""` and `llm-d.ai/glm53-cache=ready`, or replace
both roles' selectors with the target cluster's eligibility labels. The
source-cluster hostname exclusions and warm-cache preferences were removed.
Do not assume those exclusions are automatically enforced elsewhere.

The default EPP needs `llm-d-hf-token`/`HF_TOKEN`; private image access must be
configured on the service account if the original image is retained. No
credentials belong in this repository. Operators/CRDs must already exist.

## Rebuild locally

Run from this recipe directory, using local Kustomize and Python dependencies:

```bash
kustomize build config > manifests/pinned/bundle.yaml
```

Run the Python renderer and checks from the repository root:

```bash
python3 tools/render.py models/glm-5.3/recipes/h100-x64-pd-pcp8dcp8-ep32-dp16tp2-agentx-275k/recipe.yaml
python3 tools/check_manifests.py models/glm-5.3/recipes/h100-x64-pd-pcp8dcp8-ep32-dp16tp2-agentx-275k
python3 tools/validate.py --current --require-converted-raw
```

Check the Kustomize snapshot against the pin as well: the generic drift check
only compares the renderer output with the pin, not config inputs with the pin.
Helm values are installed separately and are not included in the Kustomize bundle.

## Deployment after verification

From this recipe directory, after rebuilding and satisfying prerequisites:

```bash
kubectl apply -f manifests/llm-d-v0.11.0/bundle.yaml
helm upgrade --install glm53-e2e \
  oci://ghcr.io/llm-d/charts/llm-d-router-standalone --version v0.11.0 \
  -n glm52-canary -f config/router.values.yaml
kubectl port-forward -n glm52-canary svc/glm53-e2e-epp 8000:80
```

These commands have not been executed during conversion. The Helm chart creates
its own networking resources; no author-supplied Service is stored in the bundle.
The approximate values preserve their original chart/build combination for
reference; they are not an isolated A/B test or a verified drop-in replacement.
