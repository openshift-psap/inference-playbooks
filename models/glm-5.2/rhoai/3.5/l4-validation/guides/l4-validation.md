# L4 Validation — LLMInferenceService + Pipeline Parallelism

Validate the LLMInferenceService → LWS → pipeline parallel wiring on
cheap L4 hardware before committing H200 nodes to the full GLM-5.2
recipe.

Uses Qwen2.5-7B-Instruct across 2 nodes with PP=2, TP=1 — the
smallest meaningful PP topology.

## Prerequisites

- KServe with `LLMInferenceService` v1alpha2 CRD
- LeaderWorkerSet controller installed
- 2 nodes with at least 1× NVIDIA L4 GPU (24 GB) each

### Pipeline-Parallel Config Template

Apply the PP worker config before deploying (see
[prerequisites README](../../README.md#pipeline-parallel-config-template)):

```bash
CONFIG_PREFIX=$(oc get deploy llmisvc-controller-manager \
  -n redhat-ods-applications \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LLM_INFERENCE_SERVICE_CONFIG_PREFIX")].value}')
CONTROLLER_NAMESPACE=redhat-ods-applications

envsubst < ../../prerequisites/pp-worker-llmisvcconfig.yaml | oc apply -f -
```

## Deploy

```bash
export NAMESPACE=pp-validation
kubectl create namespace ${NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -n ${NAMESPACE} -f ../manifests/pp2-tp1-llmisvc-qwen.yaml
```

## Validation Checklist

Run each check in order. If any fails, see
[Troubleshooting](#troubleshooting) below.

### 1. LWS Created

```bash
kubectl get lws -n ${NAMESPACE}
```

Expected: one LWS resource.

If no LWS appears, check the LLMInferenceService status:
```bash
kubectl get llminferenceservice -n ${NAMESPACE} -o yaml
```

`PresetsCombined: False` with `ConfigNotFound` means the PP config
template is missing — apply it per the prerequisites above.

### 2. Pods Running

```bash
kubectl get pods -n ${NAMESPACE} -o wide
```

Expected: 2 pods (head + worker), both `Running`, scheduled on
**different nodes**.

### 3. Worker Joined

```bash
kubectl logs -l leaderworkerset.sigs.k8s.io/name -n ${NAMESPACE} --prefix
```

Worker logs should show `PP rank 1` and successful connection to head.

### 4. API Serving

```bash
kubectl exec -it <head-pod> -n ${NAMESPACE} -- \
  curl -sk https://localhost:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen2.5-7B-Instruct",
    "prompt": "The capital of France is",
    "max_tokens": 10
  }' | python3 -m json.tool
```

Expected: valid completion with `system_fingerprint` containing `pp2`.

### 5. LLMInferenceService Ready

```bash
kubectl get llminferenceservice -n ${NAMESPACE}
```

Expected: `READY: True`.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `PresetsCombined: False` / `ConfigNotFound` | PP config template missing | Apply `prerequisites/pp-worker-llmisvcconfig.yaml` |
| No LWS created | LLMInferenceService CRD not installed | `kubectl get crd llminferenceservices.serving.kserve.io` |
| Pods pending | Not enough GPU nodes or scheduling constraints | Check `kubectl describe pod` events |
| Worker NCCL timeout | Network connectivity between nodes | Check pod-to-pod connectivity |
| Head startup probe failing | Model download slow or port mismatch | Check logs, verify port 8000 |
| `getpwuid` error | Using upstream vLLM image with OpenShift arbitrary UID | Remove `image:` override to use RHOAI image |

## What This Validates

- [x] LLMInferenceService controller creates LWS from PP config
- [x] PP config template provides correct vLLM serve command
- [x] `$(LWS_LEADER_ADDRESS)` and `$(LWS_WORKER_INDEX)` env injection works
- [x] vLLM `--headless` worker joins via PP
- [x] Head serves API, worker has no HTTP endpoint
- [x] Controller-created Service routes to head

## Next Steps

Once all checks pass, deploy the full GLM-5.2 recipe:
[rhoai-pp2-tp8-deploy.md](../../multi-node-pp/guides/rhoai-pp2-tp8-deploy.md)

## Cleanup

```bash
kubectl delete -n ${NAMESPACE} -f ../manifests/pp2-tp1-llmisvc-qwen.yaml
kubectl delete namespace ${NAMESPACE}
```
