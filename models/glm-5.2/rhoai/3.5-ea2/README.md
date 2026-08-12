# GLM 5.2 FP8 on RHOAI — LLMInferenceService Recipes

Deployment recipes for
[zai-org/GLM-5.2-FP8](https://huggingface.co/zai-org/GLM-5.2-FP8)
using KServe's `LLMInferenceService` CRD. The controller creates a
LeaderWorkerSet behind the scenes when pipeline parallelism is
configured with a worker template.

These recipes use the same validated vLLM configuration as the
[raw LWS playbooks](../vllm/v0.23.0/), wrapped in the RHOAI/KServe
CRD layer for integration with the Red Hat OpenShift AI serving stack.

| # | Pattern | Guide | Hardware | Key feature |
|---|---------|-------|----------|-------------|
| 1 | Multi-node PP=2 TP=8 | [Deploy guide](multi-node-pp/guides/rhoai-pp2-tp8-deploy.md) | 2× 8×H200 | Controller-managed LWS, full GLM-5.2 serving |
| 2 | L4 validation | [Validation guide](l4-validation/guides/l4-validation.md) | 2× 1×L4 | Pattern validation with Qwen2.5-7B before H200 commit |

## Prerequisites

### Pipeline-Parallel Config Template

RHOAI 3.5.0-ea ships `LLMInferenceServiceConfig` templates for
data-parallel and single-node deployments, but **not for pipeline
parallelism**. A custom PP worker config must be created before
deploying any `LLMInferenceService` with `parallelism.pipeline > 1`.

```bash
# Determine your config prefix and controller namespace:
CONFIG_PREFIX=$(oc get deploy llmisvc-controller-manager \
  -n redhat-ods-applications \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LLM_INFERENCE_SERVICE_CONFIG_PREFIX")].value}')
CONTROLLER_NAMESPACE=redhat-ods-applications

# Review and apply the PP worker config template:
envsubst < prerequisites/pp-worker-llmisvcconfig.yaml | oc apply -f -
```

The template is adapted from the existing data-parallel worker config,
replacing `--data-parallel-*` args with `--pipeline-parallel-size`,
`--nnodes`, `--node-rank`, `--master-addr`, and `--headless`. See
[`prerequisites/pp-worker-llmisvcconfig.yaml`](prerequisites/pp-worker-llmisvcconfig.yaml).

### Other Requirements

- KServe with `LLMInferenceService` v1alpha2 CRD support
- LeaderWorkerSet controller installed (`kubernetes-sigs/lws`)
- vLLM ≥ v0.23.0 (GLM-5.2 model support starts at v0.23.0)
- NVIDIA GPU operator
- HuggingFace token secret `llm-d-hf-token` (key `HF_TOKEN`)

## How It Works

The `LLMInferenceService` manifest specifies `parallelism.pipeline`
and `spec.worker`. The controller:

1. Looks up the `LLMInferenceServiceConfig` named
   `<prefix>config-llm-worker-pipeline-parallel` (the PP template)
2. Merges the template with the user's container spec (user values
   win on conflict)
3. Creates a `LeaderWorkerSet` with the merged head and worker specs
4. Creates a `Service` for routing to the head pod

Model-specific vLLM flags go in the `VLLM_ADDITIONAL_ARGS` env var.
The PP template handles `--pipeline-parallel-size`, `--nnodes`,
`--node-rank`, `--master-addr`, `--headless`, TLS, access logging,
and RoCE inference.

## Image

GLM-5.2 requires vLLM ≥ v0.23.0. The RHOAI-shipped image may not
yet include this version. Manifests override with upstream
`vllm/vllm-openai:v0.23.0` — remove the `image:` field once RHOAI
ships v0.23.0+.

For models that work with the RHOAI vLLM version (e.g. Qwen2.5-7B in
the L4 validation recipe), omit the `image:` field to use the
RHOAI image, which handles OpenShift arbitrary UIDs.

## Compared to Raw LWS

The [raw vLLM recipes](../vllm/v0.23.0/) use `LeaderWorkerSet` and
`Service` resources directly. The LLMInferenceService approach adds:

- Controller-managed LWS lifecycle (creation, scaling, rollout)
- Automatic service creation and routing
- TLS auto-configuration
- RoCE auto-inference (set `KSERVE_INFER_ROCE=true`)
- Consistent health check management

If your RHOAI/KServe version does not support v1alpha2, use the raw
LWS manifests at
[`../vllm/v0.23.0/multi-node-lws/`](../vllm/v0.23.0/multi-node-lws/).
