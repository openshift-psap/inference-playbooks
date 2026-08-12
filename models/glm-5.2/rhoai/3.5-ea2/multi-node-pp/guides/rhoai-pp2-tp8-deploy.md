# GLM 5.2 FP8 Multi-Node — PP=2 TP=8 via LLMInferenceService

Deploy GLM-5.2-FP8 split across **two 8×H200 nodes** with pipeline
parallelism (PP=2) and tensor parallelism within each node (TP=8),
using KServe's `LLMInferenceService` CRD. The controller creates a
LeaderWorkerSet behind the scenes.

This is the RHOAI-stack equivalent of the
[raw LWS deployment](../../../vllm/v0.23.0/multi-node-lws/guides/multi-node-pp2-tp8.md).
Same vLLM config, wrapped in the KServe CRD layer.

## Prerequisites

- KServe with `LLMInferenceService` v1alpha2 CRD
- LeaderWorkerSet controller installed
- 2× nodes with 8× NVIDIA H200 GPUs each
- NVIDIA GPU operator with `nvidia.com/gpu` resource

### Pipeline-Parallel Config Template

RHOAI 3.5.0-ea ships data-parallel and single-node
`LLMInferenceServiceConfig` templates but not pipeline-parallel.
Apply the PP worker config before deploying:

```bash
CONFIG_PREFIX=$(oc get deploy llmisvc-controller-manager \
  -n redhat-ods-applications \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LLM_INFERENCE_SERVICE_CONFIG_PREFIX")].value}')
CONTROLLER_NAMESPACE=redhat-ods-applications

envsubst < ../../../prerequisites/pp-worker-llmisvcconfig.yaml | oc apply -f -
```

Verify it exists:
```bash
oc get llminferenceserviceconfigs -n redhat-ods-applications | grep pipeline
```

### Model Storage

GLM-5.2-FP8 is ~756 GB. The default KServe storage initializer uses
emptyDir, which re-downloads on every restart. For production:

**Option A — PVC (recommended):**

Pre-download weights to a PVC and change the manifest:
```yaml
model:
  uri: pvc://glm52-weights
```

**Option B — hostPath:**

Pre-download to each node (see
[model-ops](../../../glm-5/model-ops/)) and mount as a hostPath
volume.

**Option C — HuggingFace download (testing only):**

Leave `uri: hf://zai-org/GLM-5.2-FP8` as-is. Expect ~30 min download
per node on first start.

### Image

GLM-5.2 requires vLLM ≥ v0.23.0. The manifest overrides with upstream
`vllm/vllm-openai:v0.23.0`. Remove the `image:` field once RHOAI
ships v0.23.0+ in its vLLM image.

## Networking: RoCE

Same as the
[raw LWS guide](../../../vllm/v0.23.0/multi-node-lws/guides/multi-node-pp2-tp8.md#networking-roce).

The PP config template includes automatic RoCE inference — set
`KSERVE_INFER_ROCE=true` to auto-detect HCAs, GID index, and NCCL
settings. Or uncomment the manual RDMA env blocks in both head and
worker containers.

## Deploy

```bash
export NAMESPACE=glm52-rhoai
kubectl create namespace ${NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic llm-d-hf-token \
  --from-literal="HF_TOKEN=${HF_TOKEN}" -n ${NAMESPACE} \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -n ${NAMESPACE} -f ../manifests/pp2-tp8-llmisvc.yaml
```

### Watch Startup

The controller creates an LWS, which creates two pods (head +
worker). Monitor:

```bash
# LWS creation
kubectl get lws -n ${NAMESPACE} -w

# Pod scheduling and startup
kubectl get pods -n ${NAMESPACE} -w

# Head pod logs (model loading, ~12 min with cached weights)
kubectl logs -f -l leaderworkerset.sigs.k8s.io/name -n ${NAMESPACE} --prefix
```

Ready when head logs: `Application startup complete.`

## Verify

```bash
# Find the service created by the controller
kubectl get svc -n ${NAMESPACE}

# Test from inside the head pod (TLS enabled by default)
kubectl exec -it <head-pod> -n ${NAMESPACE} -- \
  curl -sk https://localhost:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "glm-5.2-fp8",
    "prompt": "Hello",
    "max_tokens": 10
  }' | python3 -m json.tool
```

Tool calling works with the same patterns as the
[single-node guide](../../../vllm/v0.23.0/single-node/guides/practical-guide.md#step-3-verify-tool-calling).

## How the Manifest Works

The manifest does **not** override the vLLM serve command. The PP
config template provides the full command including
`--pipeline-parallel-size`, `--nnodes`, `--node-rank`, `--master-addr`,
and `--headless` for the worker. Model-specific flags go in
`VLLM_ADDITIONAL_ARGS`:

```yaml
env:
  - name: VLLM_ADDITIONAL_ARGS
    value: >-
      --kv-cache-dtype fp8
      --tool-call-parser glm47
      --reasoning-parser glm45
      --enable-auto-tool-choice
      --skip-mm-profiling
      --trust-remote-code
      --distributed-timeout-seconds 1200
```

The controller merges the PP template with the manifest's container
spec. User values (resources, env, probes) override template defaults.

## What the Controller Manages

| Concern | Raw LWS | LLMInferenceService |
|---------|---------|---------------------|
| LWS creation | You write it | Controller creates it |
| Service | You write it | Controller creates it |
| Group size | `size: 2` in LWS spec | Derived from `parallelism.pipeline` |
| Restart policy | Explicit in LWS | Controller default |
| TLS | Manual | Auto-configured |
| RoCE inference | Manual env vars | `KSERVE_INFER_ROCE=true` |
| Rolling updates | Manual LWS config | `rolloutStrategy` field |

## Known Limits

- **No MTP with PP.** GLM-5.2's MTP speculative decoding is not
  supported under pipeline parallelism —
  [vllm#44697](https://github.com/vllm-project/vllm/issues/44697).
- **Whole-group restarts.** Any pod failure restarts both nodes (~12
  min recovery).
- **Weights download twice** (once per node) unless backed by shared
  storage.
- **PP config template not shipped by RHOAI.** Must be created
  manually until RHOAI includes it in a future release.

## Cleanup

```bash
kubectl delete -n ${NAMESPACE} -f ../manifests/pp2-tp8-llmisvc.yaml
```
