# Deployment Guide: Nemotron-3-Ultra 550B Multi-Node PD Split

## Quick Start

```bash
# 1. Create namespace
kubectl create namespace serveit

# 2. Create HF token (gated model)
kubectl create secret generic llm-d-hf-token \
  --from-literal=HF_TOKEN=hf_your_token \
  -n serveit

# 3. Apply scheduler config
helm install nemotron-pd-router ./llm-d-router/config/charts/llm-d-router-gateway \
  -n <namespace> --values charts/router/values.yaml

# 4. Deploy prefill (3 pods, 24 GPUs total)
kubectl apply -f nemotron-3-ultra-550b-h200-pd-prefill.yaml

# 5. Deploy decode (1 pod, 8 GPUs)
kubectl apply -f nemotron-3-ultra-550b-h200-pd-decode.yaml

# 6. Monitor startup
kubectl get pods -n serveit -l architecture=pd -w
```

## Architecture: Prefill/Decode Split (PD)

```
Client Request
    ↓
Prefill Pods (×3, TP8 each)
├─ Pod 1: Encode input tokens → KV cache
├─ Pod 2: Encode input tokens → KV cache
└─ Pod 3: Encode input tokens → KV cache
    ↓ (KV state transfer via NIXL)
Decode Pod (×1, TP8)
├─ Generate output tokens sequentially
└─ Return to client
```

## Why This Split?

**Prefill phase** (processing input tokens):
- Compute-intensive
- Highly parallelizable
- Benefits from batching

**Decode phase** (generating output one token at a time):
- Memory-bound
- Sequential processing
- Benefits from low latency

By splitting, each phase is optimized independently.

## Tensor Parallelism (TP8)

Each pod uses **8 GPUs** via tensor parallelism:
- Model weights are split across 8 GPUs
- Computations are parallelized across them
- Each pod requires contiguous GPU allocation
- Supports both NVLink and RDMA communication

## EPP Scheduler Configuration

The llm-d router chart (`charts/router/values.yaml`) configures dynamic
load balancing with P/D disaggregated scheduling profiles:

- `prefix-cache-scorer` weight 3 — high weight for cache hits
- `kv-cache-utilization-scorer` weight 2 — balance KV pressure
- `queue-scorer` weight 2 — distribute queue depth
- `active-request-scorer` weight 2 — track active requests
- `prefix-based-pd-decider` — runs the prefill stage only when there are
  uncached tokens (`nonCachedTokens: 16`)

This means:
- Requests with high prefix cache hit rates are prioritized
- Load spreads across 3 prefill pods based on pressure
- Decode pod is single, acts as bottleneck (intentional for latency)

## Pod Resource Requirements

Each leader pod requests (matches the LWS manifests):
- **CPUs**: 45 cores
- **Memory**: 1711Gi
- **GPUs**: 8 (TP8) plus one `rdma/ib` device

## Monitoring

### Pod Status
```bash
kubectl get pods -n <namespace> -l architecture=pd -o wide
```

### View Logs
```bash
# Prefill logs
kubectl logs -f -n <namespace> -l llm-d.ai/role=prefill --all-containers=true

# Decode logs
kubectl logs -f -n serveit -l llm-d.ai/role=decode --all-containers=true
```

### Health Checks
- **Startup Probe**: Checks `/v1/models` every 30s, allows up to 120 failures
  (~60 min) for the 550B cold model load
- **Readiness Probe**: Checks `/v1/models` every 5s, fails after 3 failures
- **Liveness Probe**: Checks `/health` every 10s, restarts after 3 failures

Both prefill and decode containers define the same probe set (prefill targets
port 8000, decode targets port 8200 behind the routing sidecar).

### Inference Health
```bash
# Test prefill pod
kubectl exec -it <prefill-pod> -n serveit -- \
  curl -s http://localhost:8000/health | jq .

# Test decode pod
kubectl exec -it <decode-pod> -n serveit -- \
  curl -s http://localhost:8000/health | jq .
```

## Inference via API

Once pods are running:

```bash
# Port-forward to decode pod (exposed interface)
kubectl port-forward -n serveit svc/decode-tp8 8000:8000 &

# Query vLLM OpenAI API
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "RedHatAI/NVIDIA-Nemotron-3-Ultra-550B-A55B-FP8-block",
    "messages": [{"role": "user", "content": "What is tensor parallelism?"}],
    "temperature": 0.7,
    "max_tokens": 512
  }'
```

## Troubleshooting

### Pods stuck in Pending
```bash
kubectl describe pod <pod-name> -n serveit
```
- Check: node selectors, GPU availability, disk space
- Common: Not enough GPUs on nodes, nodes not labeled

### vLLM startup slow
- Normal: Can take 10-30 minutes for 550B model
- Check: HF token is valid (model access denied = stuck pull)
- Check: Disk space for model cache

### Inference timeouts
- Prefill bottleneck: Try reducing concurrent requests
- Decode bottleneck: Single pod is intentional, use load balancer in front
- Check: Network latency between pods (RDMA preferred)

### High latency
- Check: GPU utilization (nvidia-smi in pod)
- Check: Context length (262K is maximum)
- Check: Batch size vs available KV cache

## Performance Tuning

### Increase Throughput
- Add more prefill replicas (costs GPU memory)
- Increase `max-num-seqs` beyond 64 (uses more KV cache)
- Use batch API instead of streaming

### Decrease Latency
- Keep single decode pod (avoid distributed bottleneck)
- Use prefix caching for repeated prompts
- Use smaller context windows if possible

### Optimize for Specific Workload
Edit the scorer weights in `charts/router/values.yaml` (under `router.epp.pluginsCustomConfig`) and re-run `helm upgrade`:
- **High cache hits**: Increase `prefixCacheWeight`
- **Memory-bound**: Increase `kvCacheWeight`
- **High concurrency**: Increase `activeRequestWeight`

## Next Steps

1. Verify all pods are running: `kubectl get pods -n serveit`
2. Check logs for errors: `kubectl logs -n serveit --tail=100 -l architecture=pd`
3. Run inference test (see API example above)
4. Monitor performance with Prometheus/Grafana if available
5. Tune EPP weights based on your workload characteristics
