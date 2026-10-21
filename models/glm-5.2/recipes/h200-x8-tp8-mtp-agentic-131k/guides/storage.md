# Persistent model storage

Both the standalone [vLLM Deployment](../manifests/vllm-v0.24.0/deployment.yaml)
and [RHOAI service](../manifests/rhoai-3.5/llminferenceservice.yaml) mount the
same existing PVC, `glm52-weights`, read-only at **/mnt/models**. Download the
checkpoint once with the [model-ops Job](../../../model-ops/download-fp8-pvc.yaml)
before the first serving run, then keep the PVC between runs. Recreated serving
pods use the files already on the claim and do not download them from Hugging
Face. The Job needs the existing `hf-token` Secret (key `HF_TOKEN`); serving
does not need Hugging Face access. `/dev/shm` remains separate from model
storage.

The PVC must be Bound in the target namespace and large enough for the complete
**zai-org/GLM-5.2-FP8** checkpoint plus preparation headroom. The checkpoint
files currently total about 756 GB. The Janus lab example requests 1 TiB on
NFS; other environments should choose storage class, access mode and capacity
for their own storage. Serving manifests do not create or resize claims. Ensure
the PVC is writable by the downloader and readable by the serving pod's
UID/group.

The downloader writes the model files directly to the PVC root. That directory
must contain `config.json`, tokenizer assets, the safetensors index and every
shard referenced by that index, plus all other files required by the selected
checkpoint. Do not substitute `THUDM/GLM-5.2` base weights or another
quantization. Wait for the download Job to complete before launching a serving
run; do not serve a partial download or an external symlink tree.

On Janus, create the claim and populate it once in the namespace used by the
serving job. In other environments, create an equivalent claim and skip the
Janus-specific PVC manifest:

```bash
# Janus lab; use the same namespace as the serving job.
export NAMESPACE=kserve-e2e-perf
oc apply -n "${NAMESPACE}" -f models/glm-5.2/model-ops/glm52-weights-janus.yaml
oc wait -n "${NAMESPACE}" --for=jsonpath='{.status.phase}'=Bound pvc/glm52-weights --timeout=10m
oc apply -n "${NAMESPACE}" -f models/glm-5.2/model-ops/download-fp8-pvc.yaml
oc wait -n "${NAMESPACE}" --for=condition=complete job/download-glm52-weights --timeout=6h
```

The PVC is retained for future runs. A completed Job is also retained, so
applying it again does not trigger another download. If the Job fails, inspect
its logs and events; delete and recreate the Job only after correcting the
cause. Do not delete the PVC during per-run cleanup. To use a different claim,
keep `deployment.storage.pvc.name` in [recipe.yaml](../recipe.yaml), the RHOAI
claim in [config/runtime.yaml](../config/runtime.yaml), and the standalone
claim in [config/vllm-runtime.yaml](../config/vllm-runtime.yaml) synchronized.

For RHOAI, the model URI remains `pvc://glm52-weights`; `spec.model.name` is the
API alias `glm-5.2-fp8`, while `serving.model` retains the quantization-specific
checkpoint identity. Do not confuse the API alias with the on-disk checkpoint.

## Why an explicit RHOAI mount is necessary

The downstream controller's
[disabled-initializer path](https://github.com/red-hat-data-services/kserve/blob/881aa93dc885f9bbb57fe020defafab48bd93fd7/pkg/controller/v1alpha2/llmisvc/workload_storage.go#L97-L104)
returns early when `storageInitializer.enabled=false`, **before** its automatic
PVC attachment. The TP template disables that
initializer, so `model.uri: pvc://...` alone does not mount the weights.

The recipe keeps downloads disabled and supplies the explicit root mount.
This is consistent with the controller's
normal PVC mount semantics: read-only, chosen PVC/subpath and model mount path
([workload_storage.go:253-267](https://github.com/red-hat-data-services/kserve/blob/881aa93dc885f9bbb57fe020defafab48bd93fd7/pkg/controller/v1alpha2/llmisvc/workload_storage.go#L253-L267)).

The inherited default template uses separate ephemeral `/models` for
`HF_HUB_CACHE` and `/home` for HOME, not the durable weight PVC
([config-llm-template.yaml:184-200,232-255](https://github.com/red-hat-data-services/kserve/blob/881aa93dc885f9bbb57fe020defafab48bd93fd7/config/llmisvcconfig/config-llm-template.yaml#L184-L255)).
Installed presets must provide writable runtime/cache directories under the
deployment's security policy. Ensure model files are readable and runtime/cache
directories are writable by the allocated UID and groups.

## Deployment adaptation and preparation checklist

1. Verify the existing `glm52-weights` claim is Bound in the target namespace,
   has enough capacity, and its attachment/access policy fits the serving
   deployment. The generic hardware profile is not PVC inventory.
2. Verify the existing `hf-token` Secret can read the gated checkpoint, then
   run the model-ops Job and wait for completion before launching serving. No
   credentials belong in Git.
3. Establish file readability under the serving runtime's approved UID/group.
   For a different claim, update the recipe PVC name and both platform
   references together. Serving manifests do not create or resize a PVC; the
   Janus-specific claim example is separate from the serving recipe.
4. If the checkpoint is stored in a subdirectory rather than the PVC root,
   adapt the RHOAI `pvc://` model URI and both read-only mount `subPath` values
   to the same directory. Do not rely on external hub-cache symlinks.
5. Keep namespace omitted in serving manifests and select it at deployment
   time. The model-ops Job uses the namespace passed to `oc apply`.
6. Regenerate after deployment-specific adaptation rather than hand-edit outputs:

   ```bash
   python3 tools/validate.py --current --require-converted-raw
   python3 tools/render.py models/glm-5.2/recipes/h200-x8-tp8-mtp-agentic-131k/recipe.yaml
   python3 tools/check_manifests.py models/glm-5.2/recipes/h200-x8-tp8-mtp-agentic-131k
   ```

Before deployment, verify PVC population and controller presets. Rendering
does not verify storage, schedulability, GLM/MTP correctness or AgentX
performance.

## API alias and probes

The RHOAI alias and probes are set by [config/runtime.yaml](../config/runtime.yaml)
and described by `serving.served_model_name` and `serving.probes` in
[recipe.yaml](../recipe.yaml). [config/vllm-runtime.yaml](../config/vllm-runtime.yaml)
patches the standalone alias and probe settings not honored by the current
template. Keep these patches synchronized with the recipe when adapting either configuration.
Cold-load duration and serving behavior remain unvalidated.

**RHOAI probe scheme follows the controller:** the downstream default
runtime adds SSL args and HTTPS probe schemes conditionally on
`GlobalConfig.EnableTLS`. The overlay omits the probe scheme so the controller's
strategic merge retains its preset scheme while overriding paths, ports and
budgets. Do not disable controller TLS or force HTTP merely to adapt probes.
Verify transport-consistent server and probe behavior with the installed
controller presets, particularly after upgrades.

## Standalone rendering and deployment

- [recipe.yaml](../recipe.yaml) owns the shared image/engine, serving flags, TP
  and resources. [platforms/vllm-v0.24.0.yaml](../platforms/vllm-v0.24.0.yaml)
  identifies the standalone runtime; it does not inherit RHOAI's shell-prequoted
  speculative-config override. The template passes raw JSON as one argv token.
- [config/kustomization.yaml](../config/kustomization.yaml) applies each runtime
  patch only to its matching resource kind. The Deployment patch sets the local
  model path, read-only PVC mount, startup budget, alias, probes and shared
  memory, removes the unused HF token dependency and the template's undeclared
  CPU limit.
- Render after changing inputs. The entire
  [vLLM Deployment](../manifests/vllm-v0.24.0/deployment.yaml) is generated;
  edit recipe inputs and runtime patches rather than the generated manifest.
- The standalone manifest uses HTTP probes and has no Service, Route, TLS
  termination or authentication resource. Use namespace-scoped port-forwarding
  for initial checks or separately reviewed networking for exposure; RHOAI
  routing/security policy does not apply automatically.
- Its non-root UID and security context must be accepted by the namespace's
  approved SCC policy, with readable weights and writable cache/temp mounts.
  The template's fixed UID is not guaranteed to fit an OpenShift namespace's allocated UID
  range. Adapt security through a reviewed Deployment-targeted patch; no SCC grant is provided here.
- Rendering and drift checks prove reproducible generation, not upstream model
  support or serving correctness. Independently verify
  checkpoint loading, probes, required and automatic tool calls with MTP enabled,
  and benchmark behavior on each platform before claiming validation.
