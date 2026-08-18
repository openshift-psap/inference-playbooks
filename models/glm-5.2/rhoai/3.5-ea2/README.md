# GLM 5.2 FP8 on RHOAI — LLMInferenceService Recipes

Deployment recipes for
[zai-org/GLM-5.2-FP8](https://huggingface.co/zai-org/GLM-5.2-FP8)
using KServe's `LLMInferenceService` CRD. The controller creates a
LeaderWorkerSet behind the scenes when pipeline parallelism is
configured with a worker template.

Validated on janus (OCP 4.22, RHOAI 3.5.0 GA, 2×8×H200, composite
DRA, RDMA/RoCE) and psap-dra-ocp2 (OCP 4.22, RHOAI 3.5.0-ea.2,
2×L4, pod network).

| # | Pattern | Guide | Hardware | Key feature |
|---|---------|-------|----------|-------------|
| 1 | Multi-node PP=2 TP=8 | [Deploy guide](multi-node-pp/guides/rhoai-pp2-tp8-deploy.md) | 2× 8×H200 | Controller-managed LWS, full GLM-5.2 serving |
| 2 | L4 validation | [Validation guide](l4-validation/guides/l4-validation.md) | 2× 1×L4 | Pattern validation with Qwen2.5-7B before H200 commit |

## Prerequisites

### Pipeline-Parallel Config Template

RHOAI 3.5 ships `LLMInferenceServiceConfig` templates for
data-parallel and single-node deployments, but **not for pipeline
parallelism**. A custom PP worker config must be created by copying
from the existing DP worker config (preserves shell variable escaping
for the RoCE auto-inference script):

```bash
CONFIG_PREFIX=$(oc get deploy llmisvc-controller-manager \
  -n redhat-ods-applications \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LLM_INFERENCE_SERVICE_CONFIG_PREFIX")].value}')
CONTROLLER_NAMESPACE=redhat-ods-applications

envsubst < prerequisites/pp-worker-llmisvcconfig.yaml | oc apply -f -
```

See [`prerequisites/pp-worker-llmisvcconfig.yaml`](prerequisites/pp-worker-llmisvcconfig.yaml)
and the [deploy guide](multi-node-pp/guides/rhoai-pp2-tp8-deploy.md#pipeline-parallel-config-template)
for the full creation procedure.

### Other Requirements

- KServe with `LLMInferenceService` v1alpha2 CRD support
- LeaderWorkerSet controller installed (`kubernetes-sigs/lws`)
- NVIDIA GPU operator or composite DRA driver
- HuggingFace token secret `llm-d-hf-token` (key `HF_TOKEN`)
- Model weights pre-loaded to a PVC (~756 GB)
- MachineConfig: unlimited memlock (for RDMA)
- MachineConfig: topology manager best-effort (for 8 GPU+NIC pairs)

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

## Key Config Details

- **`storageInitializer.enabled: false`** — PVC mounted directly at
  `/mnt/models`. No init container, no re-download on restart.
- **`VLLM_ENGINE_READY_TIMEOUT_S=3600`** — prevents API server
  timeout during slow NFS weight loading (~90 min for 756 GB).
- **`KSERVE_INFER_ROCE=true`** — auto-discovers RDMA HCAs and sets
  NCCL_IB_HCA, NCCL_IB_GID_INDEX, NVSHMEM, UCX env vars.
- **`limits.memory: 1024Gi`** — allows page cache prefetch of 756 GB
  weights without hitting cgroup memory limit.
- **No image override** — RHOAI 3.5 GA ships vLLM v0.24.0, which
  supports GLM-5.2.

## Compared to Raw LWS

The [raw vLLM recipes](../../vllm/v0.23.0/) use `LeaderWorkerSet` and
`Service` resources directly. The LLMInferenceService approach adds:

- Controller-managed LWS lifecycle (creation, scaling, rollout)
- Automatic service creation and routing
- TLS auto-configuration
- RoCE auto-inference (set `KSERVE_INFER_ROCE=true`)
- Consistent health check management

If your RHOAI/KServe version does not support v1alpha2, use the raw
LWS manifests at
[`../../vllm/v0.23.0/multi-node-lws/`](../../vllm/v0.23.0/multi-node-lws/).
