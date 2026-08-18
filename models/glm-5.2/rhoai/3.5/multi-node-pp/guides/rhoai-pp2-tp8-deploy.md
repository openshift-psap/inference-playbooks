# GLM 5.2 FP8 Multi-Node — PP=2 TP=8 via LLMInferenceService

Deploy GLM-5.2-FP8 split across **two 8×H200 nodes** with pipeline
parallelism (PP=2) and tensor parallelism within each node (TP=8),
using KServe's `LLMInferenceService` CRD. The controller creates a
LeaderWorkerSet behind the scenes.

Validated on janus (OCP 4.22, RHOAI 3.5.0 GA, 2×8×H200) with real
FP8 weights from NFS PVC, composite DRA GPU+NIC pairs, RDMA/RoCE.

## Manifest Variants

| Manifest | Cross-node comms | GPU resource | Use when |
|----------|-----------------|--------------|----------|
| `pp2-tp8-llmisvc-pod-net.yaml` | TCP (pod network) | `nvidia.com/gpu: "8"` | No RDMA / no composite DRA driver |
| `pp2-tp8-llmisvc-composite-dra.yaml` | RDMA (GPU+NIC pairs) | `composite.dra.io/gpu-nic-pair: "8"` | Composite DRA driver installed |

The composite-DRA variant co-allocates each GPU with its nearest RDMA
NIC, ensuring NCCL uses RDMA for cross-node PP activation transfer.
It sets `nvidia.com/gpu: "0"` to suppress the `nvidia.com/gpu`
auto-insertion by the LLMInferenceServiceConfig template merge.

**NOTE**: The composite DRA driver domain is cluster-specific
(`composite.dra.io` vs `composite.dra`). Check your cluster:
```bash
oc get resourceslices -o json | jq -r '.items[].spec.driver' | sort -u | grep composite
```

## Prerequisites

- KServe with `LLMInferenceService` v1alpha2 CRD
- LeaderWorkerSet controller installed
- 2× nodes with 8× NVIDIA H200 GPUs each
- NVIDIA GPU operator — or composite DRA driver for RDMA variant

### Pipeline-Parallel Config Template

RHOAI 3.5 ships data-parallel and single-node
`LLMInferenceServiceConfig` templates but not pipeline-parallel.
Create the PP worker config by copying from the DP config:

```bash
CONFIG_PREFIX=$(oc get deploy llmisvc-controller-manager \
  -n redhat-ods-applications \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LLM_INFERENCE_SERVICE_CONFIG_PREFIX")].value}')
CONTROLLER_NAMESPACE=redhat-ods-applications

# Copy from the DP worker config (preserves shell variable escaping):
python3 -c "
import json, subprocess, re
result = subprocess.run(['oc', 'get', 'llminferenceserviceconfig',
  '${CONFIG_PREFIX}config-llm-worker-data-parallel',
  '-n', '${CONTROLLER_NAMESPACE}', '-o', 'json'],
  capture_output=True, text=True)
obj = json.loads(result.stdout)
obj['metadata']['name'] = '${CONFIG_PREFIX}config-llm-worker-pipeline-parallel'
for k in ['uid','resourceVersion','creationTimestamp','generation','ownerReferences']:
    obj['metadata'].pop(k, None)
obj.pop('status', None)
# Replace DP args with PP args in head and worker commands...
# See prerequisites/pp-worker-llmisvcconfig.yaml for the full script
print(json.dumps(obj))
" | oc apply -f -
```

Or apply the pre-built template:
```bash
envsubst < ../../../prerequisites/pp-worker-llmisvcconfig.yaml | oc apply -f -
```

Verify:
```bash
oc get llminferenceserviceconfigs -n redhat-ods-applications | grep pipeline
```

### MachineConfig Prerequisites

For RDMA (composite-DRA variant):

1. **Unlimited memlock** — required for RDMA memory registration:
   ```bash
   # See: https://github.com/opendatahub-io/llm-d-playbooks/blob/main/03-accelerator-operator-config/common/03-worker-gpu-rdma-config/machineconfig-memlock.yaml
   ```

2. **Topology manager best-effort** — required to allocate 8 GPU+NIC
   pairs across NUMA boundaries:
   ```bash
   oc apply -f - <<'EOF'
   apiVersion: machineconfiguration.openshift.io/v1
   kind: KubeletConfig
   metadata:
     name: topology-best-effort
   spec:
     machineConfigPoolSelector:
       matchLabels:
         pools.operator.machineconfiguration.openshift.io/gpu-worker: ""
     kubeletConfig:
       topologyManagerPolicy: "best-effort"
   EOF
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

## Networking: RoCE

The PP config template includes automatic RoCE inference — set
`KSERVE_INFER_ROCE=true` to auto-detect HCAs, GID index, and NCCL
settings. Set `NCCL_IB_GID_INDEX=3` explicitly for SR-IOV VF setups.

Both manifests set `KSERVE_INFER_ROCE=true` and `NCCL_IB_GID_INDEX=3`.

## Deploy

```bash
export NAMESPACE=glm52-rhoai
kubectl create namespace ${NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic llm-d-hf-token \
  --from-literal="HF_TOKEN=${HF_TOKEN}" -n ${NAMESPACE} \
  --dry-run=client -o yaml | kubectl apply -f -

# Pod network (TCP):
kubectl apply -n ${NAMESPACE} -f ../manifests/pp2-tp8-llmisvc-pod-net.yaml

# — OR — Composite DRA (RDMA, GPU+NIC pairs):
kubectl apply -n ${NAMESPACE} -f ../manifests/pp2-tp8-llmisvc-composite-dra.yaml
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
  Use local NVMe storage for faster loading if available.
- **No MTP with PP.** GLM-5.2's MTP speculative decoding is not
  supported under pipeline parallelism —
  [vllm#44697](https://github.com/vllm-project/vllm/issues/44697).
- **Whole-group restarts.** Any pod failure restarts both nodes.
- **PP config template not shipped by RHOAI.** Must be created
  manually until RHOAI includes it in a future release.
- **Composite DRA driver startup order.** If the composite DRA driver
  starts before the NVIDIA GPU DRA driver, it sees 0 GPUs and
  publishes 0 composite devices. Restart the composite DRA driver
  pod after the GPU driver is ready —
  [composite-dra-driver#75](https://github.com/openshift-psap/composite-dra-driver/issues/75).
- **Memory limit.** Set `limits.memory: 1024Gi` to allow page cache
  prefetch of 756 GB weights. The default 512Gi from the template
  causes OOM during weight loading.

## Cleanup

```bash
kubectl delete -n ${NAMESPACE} llminferenceservice glm52-pp
```
