# Nemotron-3-Ultra 550B FP8 — Multi-Node Deployment Guide

Optimized deployment pattern for [NVIDIA Nemotron-3-Ultra-550B-A55B-FP8-block](https://huggingface.co/nvidia/Nemotron-3-Ultra-550B-A55B-FP8-block) on multi-GPU clusters using vLLM v0.26.0 with Prefill/Decode disaggregation (PD split).

## Configuration Summary

| # | Pattern | Manifest | Workload | Notes |
|---|---------|----------|----------|-------|
| 1 | Multi-node, Prefill TP8 + Decode TP8, 3:1 split | [Decode](multi-node/manifests/nemotron-3-ultra-550b-h200-pd-decode.yaml) + [Prefill](multi-node/manifests/nemotron-3-ultra-550b-h200-pd-prefill.yaml) | Long-context serving (262K tokens) | 3 prefill pods + 1 decode pod; EPP scheduler for dynamic load balancing; prefix caching enabled; FP8 KV cache |

## Key vLLM Settings

- **Context**: 262,144 tokens (very long context)
- **GPU Memory Utilization**: 90%
- **Max Concurrent Sequences**: 64
- **KV Cache**: FP8 (halves cache memory)
- **Prefix Caching**: Enabled (reuse computation for repeated prefixes)
- **Tensor Parallelism**: TP8 per pod (requires 8 GPUs per pod)

## Pod Configuration

### Prefill Pods (×3)
- Replicas: 3
- Tensor Parallelism: TP8 (8 GPUs per pod)
- Total GPUs: 24
- Role: Processes input tokens, generates KV cache
- Deployed via LeaderWorkerSet (LWS)

### Decode Pod (×1)
- Replicas: 1
- Tensor Parallelism: TP8 (8 GPUs per pod)
- Total GPUs: 8
- Role: Generates output tokens one at a time
- Deployed via LeaderWorkerSet (LWS)

### Total Resources
- **GPUs**: 32 (24 prefill + 8 decode)
- **Nodes**: Minimum 4 (if 8 GPUs/node)
- **Architecture**: Prefill/Decode disaggregation with EPP scheduler

## Deployment Prerequisites

- Kubernetes/OpenShift cluster with at least 4 nodes
- 8 GPUs per node (A100 80GB, H100, or better)
- vLLM v0.26.0 container image
- HuggingFace token secret (`hf-token`) for gated model access
- llm-d router and GAIE scheduler (for EPP load balancing)
- RDMA/RoCE network (optional, for inter-pod communication)

## Which Pattern?

- Need to serve very long contexts (100K+ tokens) → **Pattern 1** (this configuration)
- Want balanced throughput/latency → Use EPP scheduler config for dynamic weighting
- Running on 4+ nodes with 8 GPUs each → This is the right choice

## Files

- `nemotron-3-ultra-550b-h200-pd-prefill.yaml` — Prefill deployment (3 replicas, TP8)
- `nemotron-3-ultra-550b-h200-pd-decode.yaml` — Decode deployment (1 replica, TP8)
- `nemotron-3-ultra-550b-h200-pd-epp-configmap.yaml` — EPP scheduler weights for PD load balancing

## Deployment Steps

1. **Create HF token secret**:
   ```bash
   kubectl create secret generic hf-token --from-literal=HF_TOKEN=hf_xxx
   ```

2. **Apply EPP ConfigMap**:
   ```bash
   kubectl apply -f nemotron-3-ultra-550b-h200-pd-epp-configmap.yaml
   ```

3. **Deploy prefill pods**:
   ```bash
   kubectl apply -f nemotron-3-ultra-550b-h200-pd-prefill.yaml
   ```

4. **Deploy decode pod**:
   ```bash
   kubectl apply -f nemotron-3-ultra-550b-h200-pd-decode.yaml
   ```

5. **Verify health**:
   ```bash
   kubectl get pods -l architecture=pd
   kubectl logs -f -l llm-d.ai/role=prefill
   kubectl logs -f -l llm-d.ai/role=decode
   ```

## Performance Notes

- **Prefill**: Optimized for batch processing, handles context encoding in parallel
- **Decode**: Optimized for sequential token generation with low latency
- **EPP Scheduler**: Dynamically routes requests based on prefix cache hit rate, KV cache pressure, and queue depth
- **FP8 KV Cache**: Reduces memory usage by 50% with minimal quality loss
- **Expected Throughput**: 400-600 tokens/sec depending on context and load

## Troubleshooting

If pods fail to schedule:
- Check cluster has 32+ GPUs total
- Verify node affinity rules in manifests
- Check `kubectl get nodes` and `nvidia-smi` on each node

If inference is slow:
- Monitor `max-num-seqs` usage (limit is 64)
- Check prefill/decode ratio (3:1 is tuned for balanced latency)
- Review EPP ConfigMap weights if requests are unevenly distributed

## References

- [vLLM Documentation](https://docs.vllm.ai/)
- [NVIDIA Nemotron Model Card](https://huggingface.co/nvidia/Nemotron-3-Ultra-550B-A55B-FP8-block)
- [llm-d Router/GAIE](https://github.com/opendatahub-io/llm-d)
