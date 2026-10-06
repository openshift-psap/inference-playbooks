# Workload Profile: aiperf-agentx-128k

## Overview

**Multi-user agentic scenario** simulating a large software project being collaboratively developed by multiple users.

Each user session runs **540-turn conversations** with very long initial context (160K tokens) representing project codebase, documentation, and discussion history.

## Workload Characteristics

| Metric | Value | Notes |
|--------|-------|-------|
| **Concurrent Users** | 30 | Multiple developers working simultaneously |
| **Conversation Turns** | 540 | Long multi-turn sessions with project context |
| **Initial Context** | 160K ± 233.6K tokens | Project codebase, docs, prior discussions |
| **Per-Turn Input (ISL)** | 1,500 ± 1,200 tokens | Code snippets, questions, clarifications |
| **Per-Turn Output (OSL)** | 425 ± 825 tokens | Code suggestions, explanations, analysis |
| **Turn Delay** | 15s ± 55s | User think time between requests |
| **Prefix Cache** | 3,000 tokens | Project system prompt (reused) |
| **Max Context** | 262,144 tokens | Full conversational history + codebase |

## Why This Matters

**Agentic workflows** differ from traditional chat:
- **Long initial context**: Users provide full project context (code, docs, requirements)
- **Multi-turn reasoning**: Iterative back-and-forth with persistent state
- **High variance**: Output length varies widely (405-1250 tokens from 425±825)
- **Real think time**: 15s ± 55s simulates actual user delays between requests
- **Prefix reuse**: System prompt and project intro are repeated across all turns

## Optimization Strategy: PD Split

This workload benefits from **Prefill/Decode disaggregation** because:

1. **Prefill bottleneck**: Initial 160K-token context requires massive compute upfront
   - Parallelizable: batch multiple users' prefill phases
   - Long latency: encoding 160K tokens takes time
   - Solution: 3 dedicated prefill pods (24 GPUs) handle all users' prefill phases

2. **Decode bottleneck**: Generating responses one token at a time
   - Sequential: can't parallelize token generation
   - Latency-critical: users wait for first token (TTFT)
   - Solution: 1 dedicated decode pod (8 GPUs) optimized for low-latency token generation

3. **Prefix caching**: Reusing 3K-token project prompt across sessions
   - Cache hit reduces re-encoding of common context
   - EPP scheduler prioritizes high-cache-hit requests
   - Saves prefill compute, reduces pressure on decode pod

## Expected Performance

### Throughput
- ~30-40 requests/sec at steady state (30 users × 1.2 turns/sec)
- Prefill: 200+ tokens/sec per GPU (parallelizable)
- Decode: 1-2 tokens/sec per GPU (sequential)

### Latency
- **TTFT (Time-to-First-Token)**: 500-2000ms (target: minimize this)
- **Token latency**: 50-100ms per subsequent token
- **End-to-End**: 30-60 seconds per turn (includes user think time)

### Resource Utilization
- Prefill pods: 70-85% GPU utilization (bursty, batch-driven)
- Decode pod: 80-95% GPU utilization (steady stream)
- FP8 KV cache: ~100GB for 30 concurrent sessions

## Configuration Rationale

### Why TP8 per pod?
- 550B model requires significant memory per GPU
- TP8 distributes weights and computation across 8 GPUs
- Stays within H200's 141GB HBM per pod

### Why 3:1 prefill:decode split?
- Prefill is compute-heavy, can be parallelized
- Decode is memory-bound, latency-critical
- 3:1 ratio balances throughput and TTFT in this workload

### Why FP8 KV cache?
- 262K max context × 30 users needs enormous KV memory
- FP8 halves KV cache footprint (262K×30×2×float16 → float8)
- Minimal quality loss for this workload type

## Monitoring This Workload

Key metrics to track:
- **Prefill GPU utilization**: Should be bursty (0-100%)
- **Decode GPU utilization**: Should be steady (70-95%)
- **Prefix cache hit rate**: Target >70% (system prompt reuse)
- **TTFT (Time-to-First-Token)**: Target <1000ms
- **Queue depth**: Should stay <10 (healthy scheduling)

## When to Use This Profile

✅ **Ideal for**:
- Multi-user collaborative coding sessions
- Project-aware AI assistants
- Agentic workflows with persistent state
- Long-context RAG systems

❌ **Not ideal for**:
- Short, stateless completions
- Single-turn Q&A
- Very high concurrency (200+ users) — requires more decode capacity
