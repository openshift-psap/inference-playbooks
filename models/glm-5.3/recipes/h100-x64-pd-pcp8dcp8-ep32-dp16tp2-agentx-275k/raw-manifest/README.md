# GLM-5.3 P/D on 8x (8x H100) RoCE: PCP8xDCP8 prefill, DP16xTP2 decode

Raw manifest submission. Source: `project-cb-lab/curvebender-tools`,
`deployments/glm-5.3/rits-roce-h100-canary` at commit
`aed915a4ebf2d8fc39d526388cc430f399179bc6`. The manifests are copied
verbatim except as noted under [Changes from the source](#changes-from-the-source).

## Summary

| Field | Value |
| --- | --- |
| Model | `zai-org/GLM-5.3` (served name `zai-org/GLM-5.3`, FP8 KV cache) |
| Stack | llm-d: vLLM in the `llm-d-cuda` image + llm-d router (EPP) `v0.11.0` + `llm-d-router-disagg-sidecar:v0.10.0` |
| vLLM version | **needs review**: the vLLM bundled in `llm-d-cuda` build `sha-bbeed78` (pinned by digest), plus one startup patch (vllm#58625) |
| Hardware | 8 nodes x 8x NVIDIA H100 80GB HBM3 (64 GPUs), 8x RoCE rails per node (`mlx5_1`..`mlx5_8`, GPUDirect RDMA) |
| Workload | Agentic long context, `--max-model-len 275000`. Closest profile: `aiperf-agentx-unlimited-context` (**needs review**) |
| Deployment | Multi-node prefill/decode disaggregation as one `DisaggregatedSet` with two LeaderWorkerSet roles |
| Image | `us.icr.io/llmd/llm-d-cuda@sha256:43f1297937b36cd973a2cde4d5137a3ffaebc1de78c8e28c97e8a17515e07d1c` (**needs review**: private IBM Cloud registry; needs an equivalent public llm-d image) |

### Roles

| Role | Nodes | Parallelism | Notes |
| --- | --- | --- | --- |
| prefill | 4 (1 LWS group of 4) | DP4 x PCP8 (DCP8), TP1, EP32 | DeepEP high-throughput, DeepGEMM MoE, eager mode, MTP1, one DP rank per pod on port 8000. KV connector: `MultiConnector` = `NixlConnector` (kv_producer) + `OffloadingConnector` (`TieringOffloadingSpec`, 512 GiB host-RAM tier per rank out of `/dev/shm`). Publishes KV cache events over ZMQ on 5557. |
| decode | 4 (1 LWS group of 4) | DP16 x TP2, EP32 | DeepEP low-latency, DeepGEMM MoE, CUDA graphs, MTP3, `max_num_seqs=8` per rank. `NixlConnector` kv_consumer. 4 local DP ranks per pod on 8200-8203 behind the llm-d routing-proxy sidecar on 8000-8003. |

### Router

`router.values.yaml` (default): llm-d-router-standalone Helm values. The EPP
always disaggregates, filters prefill by prefix-cache affinity using
`precise-prefix-cache-producer` fed by the prefill pods' KV events (hashed at
512-token blocks = `--block-size 64` x DCP8), and picks decode by active
requests.

`router-approx.values.yaml`: A/B alternative using the approximate prefix
index (`approx-prefix-cache-producer`, LRU sized for GPU + CPU tier).

## Files

| File | Purpose |
| --- | --- |
| `kustomization.yaml` | Entry point (`kubectl apply -k`) for the set, PodMonitor and patch ConfigMap |
| `disaggregatedset.yaml` | Prefill + decode roles |
| `vllm-startup-patches.configmap.yaml` | Startup patch run before `vllm serve` in both roles (adds `--release-weight-page-cache`, vllm#58625) |
| `podmonitor.yaml` | Prometheus scrape of every vLLM rank |
| `router.values.yaml` | EPP + Envoy Helm values (precise prefix index) |
| `router-approx.values.yaml` | EPP + Envoy Helm values (approx prefix index, alternative) |

## Deploy

```bash
kubectl apply -k .
helm upgrade --install glm53-e2e \
  oci://ghcr.io/llm-d/charts/llm-d-router-standalone --version v0.11.0 \
  -n glm52-canary -f router.values.yaml
kubectl port-forward -n glm52-canary svc/glm53-e2e-epp 8000:80
```

## Prerequisites

- Controllers/CRDs: DisaggregatedSet controller (`disaggregatedset.x-k8s.io/v1`),
  LeaderWorkerSet controller, Prometheus operator (for `PodMonitor`).
- Gateway API Inference Extension CRDs for the router chart (`InferencePool`).
- Namespace `glm52-canary` and ServiceAccount `glm-5-2` allowed to run
  privileged pods (the vLLM containers and the route-install init container
  run privileged as root with `IPC_LOCK`, `SYS_RAWIO`, `NET_ADMIN`).
- Model weights for `zai-org/GLM-5.3` on a ReadOnlyMany PVC
  `glm52-canary-model-cache-rox-pvc`, under subPath `rits-models` at
  `models/zai-org/GLM-5.3`.
- Secret `llm-d-hf-token` (key `HF_TOKEN`) for the EPP tokenizer.
- RoCE networking: 8 `NetworkAttachmentDefinition`s `deepep-ll-port-1`..`-8`
  (one per rail, interfaces `net1`..`net8`), rail gateways `10.<rail>.0.1`,
  `nvidia.com/roce_gdr` device plugin resource (8 per node), `/dev/infiniband`
  on the host, RoCE GID index 3. The `install-crossrail-routes` init container
  adds per-rail default routes (tables 100-107) so a NIC on rail i can reach
  rail j on a peer.
- NVSHMEM IBGDA (DeepEP low-latency) and GDRCopy available on the nodes.
- Host memory: 1500 Gi requested per pod; prefill uses up to 1000 Gi of
  `/dev/shm`, 512 GiB of which is the CPU KV offload tier.
- Node-local hostPath `/mnt/local/jit-cache-llm-d-cuda-ubuntu-sha-bbeed78`
  for JIT kernel caches (created if missing).

## Cluster-specific values (**needs review**)

These are specific to the RITS RoCE H100 OpenShift cluster and need to be
parameterized or removed during conversion:

- Node affinity: `kubernetes.io/hostname` exclusions/preferences, and the
  `rits.node-model-cache-enabled` / `llm-d.ai/glm53-cache=ready` node labels.
- Namespace `glm52-canary`, ServiceAccount `glm-5-2`, PVC name and subPath.
- `quay.io/dagray/rdma-tools:tiny` init image for the route install.
- Envoy image override (`docker.io/envoyproxy/envoy:distroless-v1.38.4`) and
  `--concurrency 16`, needed on this cluster for image-age policy and nofile
  limits.

## Notes

- Prefill startup can take up to ~45 min (startupProbe allows 2700 s); decode
  up to 90 min (5400 s) on a cold JIT cache.
- `PYTHONHASHSEED=0` on prefill seeds vLLM's block-hash chain; the EPP's
  precise prefix producer relies on the same seed.
- `KV_BLOCK_SIZE x DCP_SIZE` (64 x 8 = 512) must equal `blockSizeTokens` in
  `router.values.yaml`.
- `vllm_version` is a NIXL compatibility-hash factor, so prefill and decode
  must run the same image digest.

## Changes from the source

- `patches/10-release-weight-page-cache.py` is inlined as
  `vllm-startup-patches.configmap.yaml` (raw-manifest accepts YAML/JSON only);
  `kustomization.yaml` lists that file under `resources` instead of using a
  `configMapGenerator`. The rendered ConfigMap is identical.
- Some comments refer to sibling directories in the source repository
  (`../deployment-pcp-offloading`, `../baseline-tp`, `../justfile`) that are
  not part of this submission.
