# GLM 5.2 FP8 Multi-Node — PP=2 TP=8 via LLMInferenceService

Deploy [zai-org/GLM-5.2-FP8](https://huggingface.co/zai-org/GLM-5.2-FP8)
split across **two 8×H200 nodes** with pipeline parallelism (PP=2) and
tensor parallelism within each node (TP=8), using KServe's
`LLMInferenceService` CRD. The controller creates a LeaderWorkerSet
behind the scenes.

Validated on janus (OCP 4.22, RHOAI 3.5.0 GA, 2×8×H200) with real
FP8 weights from NFS PVC, composite DRA GPU+NIC pairs, RDMA/RoCE.

## Manifest Variants

| Manifest | GPU resource | Use when |
|----------|--------------|----------|
| [`pp2-tp8-llmisvc.yaml`](multi-node-pp/manifests/pp2-tp8-llmisvc.yaml) | `nvidia.com/gpu: "8"` | Standard GPU allocation. Add Multus annotations for RDMA if needed. |
| [`pp2-tp8-llmisvc-composite-dra.yaml`](multi-node-pp/manifests/pp2-tp8-llmisvc-composite-dra.yaml) | `composite.dra.io/gpu-nic-pair: "8"` | Composite DRA driver co-allocates GPU + nearest RDMA NIC. |

The composite-DRA variant sets `nvidia.com/gpu: "0"` to suppress the
auto-insertion by the LLMInferenceServiceConfig template merge.

For **Multus** secondary networks with the standard manifest, add a
pod annotation to both `template` and `worker` specs:
```yaml
metadata:
  annotations:
    k8s.v1.cni.cncf.io/networks: <your-net-attach-def>
```

Verify your composite DRA driver domain before deploying:
```bash
oc get resourceslices -o json | jq -r '.items[].spec.driver' | sort -u | grep composite
```

## Prerequisites

- KServe with `LLMInferenceService` v1alpha2 CRD
- LeaderWorkerSet controller installed (`kubernetes-sigs/lws`)
- 2× nodes with 8× NVIDIA H200 GPUs each
- NVIDIA GPU operator — or composite DRA driver for RDMA variant
- HuggingFace token secret `llm-d-hf-token` (key `HF_TOKEN`)

### Pipeline-Parallel Config Template

RHOAI 3.5 ships `LLMInferenceServiceConfig` templates for data-parallel
and single-node deployments, but **not for pipeline parallelism**. Apply
the PP worker config before deploying:

```bash
CONFIG_PREFIX=$(oc get deploy llmisvc-controller-manager \
  -n redhat-ods-applications \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LLM_INFERENCE_SERVICE_CONFIG_PREFIX")].value}')
CONTROLLER_NAMESPACE=redhat-ods-applications

envsubst < prerequisites/pp-worker-llmisvcconfig.yaml | oc apply -f -
```

Verify:
```bash
oc get llminferenceserviceconfigs -n redhat-ods-applications | grep pipeline
```

To validate the PP wiring on cheap hardware before committing H200s,
use the [L4 validation recipe](prerequisites/l4-validation/).

### MachineConfig Prerequisites (RDMA)

**Unlimited memlock** — required for RDMA memory registration:
```bash
# See: https://github.com/opendatahub-io/llm-d-playbooks/blob/main/03-accelerator-operator-config/common/03-worker-gpu-rdma-config/machineconfig-memlock.yaml
```

### Model Storage

GLM-5.2-FP8 is ~756 GB. Manifests use `storageInitializer.enabled: false`
with a direct PVC mount — no init container, no re-download on restart.

Pre-download weights to a PVC:
```bash
oc apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: glm52-fp8-weights
spec:
  accessModes: [ReadWriteMany]
  resources:
    requests:
      storage: 850Gi
  storageClassName: nfs
---
apiVersion: batch/v1
kind: Job
metadata:
  name: download-glm52-fp8
spec:
  template:
    spec:
      containers:
        - name: download
          image: registry.redhat.io/rhoai/odh-kserve-storage-initializer-rhel9
          args: ["hf://zai-org/GLM-5.2-FP8", "/mnt/models"]
          env:
            - name: HF_TOKEN
              valueFrom:
                secretKeyRef:
                  name: llm-d-hf-token
                  key: HF_TOKEN
          resources:
            requests:
              cpu: 4
              memory: 16Gi
          volumeMounts:
            - name: models
              mountPath: /mnt/models
      volumes:
        - name: models
          persistentVolumeClaim:
            claimName: glm52-fp8-weights
      restartPolicy: Never
  backoffLimit: 2
EOF
```

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

### Key Config Details

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

## Networking: RoCE

The PP config template includes automatic RoCE inference — set
`KSERVE_INFER_ROCE=true` to auto-detect HCAs, GID index, and NCCL
settings. Set `NCCL_IB_GID_INDEX=3` explicitly for SR-IOV VF setups.

## Deploy

```bash
export NAMESPACE=glm52-rhoai
kubectl create namespace ${NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic llm-d-hf-token \
  --from-literal="HF_TOKEN=${HF_TOKEN}" -n ${NAMESPACE} \
  --dry-run=client -o yaml | kubectl apply -f -

# Standard (nvidia.com/gpu):
kubectl apply -n ${NAMESPACE} -f multi-node-pp/manifests/pp2-tp8-llmisvc.yaml

# — OR — Composite DRA (GPU+NIC pairs):
kubectl apply -n ${NAMESPACE} -f multi-node-pp/manifests/pp2-tp8-llmisvc-composite-dra.yaml
```

### Watch Startup

```bash
kubectl get lws -n ${NAMESPACE} -w
kubectl get pods -n ${NAMESPACE} -w
kubectl logs -f -l leaderworkerset.sigs.k8s.io/name -n ${NAMESPACE} --prefix
```

Model loading from NFS takes ~90 min for 756 GB. Ready when:
`oc get llminferenceservice -n ${NAMESPACE}` shows `READY: True`.

## Verify

```bash
kubectl exec -it <head-pod> -n ${NAMESPACE} -- \
  curl -sk https://localhost:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "glm-5.2-fp8",
    "prompt": "Hello",
    "max_tokens": 10
  }' | python3 -m json.tool
```

Expected `system_fingerprint` contains `tp8-pp2`.

## Known Issues

- **NFS weight loading is slow.** 756 GB over NFS takes ~90 min.
  `VLLM_ENGINE_READY_TIMEOUT_S=3600` prevents API server timeout.
- **No MTP with PP.** GLM-5.2's MTP speculative decoding is not
  supported under pipeline parallelism —
  [vllm#44697](https://github.com/vllm-project/vllm/issues/44697).
- **Whole-group restarts.** Any pod failure restarts both nodes.
- **PP config template not shipped by RHOAI.** Must be created
  manually until RHOAI includes it in a future release.

## Compared to Raw LWS

The [raw vLLM recipes](../../vllm/v0.23.0/) use `LeaderWorkerSet` and
`Service` resources directly. The LLMInferenceService approach adds:

- Controller-managed LWS lifecycle (creation, scaling, rollout)
- Automatic service creation and routing
- TLS auto-configuration
- RoCE auto-inference (set `KSERVE_INFER_ROCE=true`)
- Consistent health check management

## Cleanup

```bash
kubectl delete -n ${NAMESPACE} llminferenceservice glm52-pp
```
