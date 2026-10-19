# GLM-5.3 H100 prefill/decode — converted intake

[Recipe](../recipe.yaml) · [Deployment bundle](../manifests/llm-d-v0.11.0/disaggregated.yaml)
· [Original source and prerequisites](../raw-manifest/README.md)

This is a **day-zero, unverified** conversion of PR #33. The original intake
is preserved. The llm-d platform version identifies the router chart release;
the vLLM version in the contributor's digest-pinned image is **unknown**.
Private registry access and the startup patch remain explicit prerequisites.
No public replacement image or benchmark result is asserted.

The pinned bundle contains the DisaggregatedSet, PodMonitor and startup patch
ConfigMap. Its asymmetric topology, startup commands, per-rank environment,
resources, security contexts, sidecar and storage bindings are authoritative.
The recipe summarizes shared settings and decode parallelism. Prefill and
decode must use the same patched image for NIXL compatibility.

## Rebuild and review

From the repository root:

```bash
recipe=models/glm-5.3/recipes/h100-x64-pd-pcp8dcp8-ep32-dp16tp2-agentx-275k
kustomize build --load-restrictor LoadRestrictionsNone "$recipe/config" > "$recipe/manifests/llm-d-v0.11.0/disaggregated.yaml"
python3 tools/render.py "$recipe/recipe.yaml"
python3 tools/validate.py --current --require-converted-raw
```

The local Kustomize inputs are all inside this recipe. Conversion only removes
the submitted hostname exclusions/preferences and qualifies the BusyBox image;
GPU/cache-readiness affinity remains. `config/` is the conversion source, not
an overlay applied on top of the renderer's pinned output. To roll back a
conversion change, restore the prior reviewed config and regenerate the pin;
the original intake remains available under `raw-manifest/`.

## Before deploying

Follow the [original prerequisite list](../raw-manifest/README.md#prerequisites).
Review namespace, service account, cache-readiness labels, PVC/subPath, JIT
hostPath, RoCE NADs/gateways and the privileged route-install image on the
target cluster. Host memory requests are not proof of available host capacity.
No cluster is modified by conversion or rendering.

Apply the pinned bundle only after that review. Install the router separately
with [the supplied precise-prefix values](../raw-manifest/router.values.yaml).
The approximate-prefix values remain an optional alternative, not a second
router to install alongside the default. The conversion does not change chart
parameters or treat contributor performance comments as benchmark evidence.
