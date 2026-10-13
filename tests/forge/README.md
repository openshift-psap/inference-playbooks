# Forge CI Integration

Run inference-playbook recipes through the Forge CI framework by posting
`/test forge` comments on pull requests.

## Available workload keys

Workload keys are defined in RHAIIS `workloads.yaml` and selected via the
`/var inference_playbooks.workload` comment directive. Any key can be used
with any recipe — workload compatibility is not enforced by the catalog.

### GuideLLM workloads

Require `data`, `rates`, and `max_seconds` in the workload definition.

| Key | Description |
|-----|-------------|
| `profile1` | 1k/1k tokens, rates [1, 50, 100, 200, 300], 275s per rate |
| `ci-quick` | 1k/1k tokens, rates [1, 5], 60s per rate |

### AI Perf workloads

Use `aiperf_args` passthrough for scenario configuration.

| Key | Description |
|-----|-------------|
| `aiperf-agentx-128k` | AgentX MVP scenario, 128k context cap, concurrency 32 |
| `aiperf-agentx-unlimited` | AgentX MVP scenario, no context cap, concurrency sweep [1, 4, 16, 32] |
| `aiperf-smoke` | Synthetic 128/64 tokens, 10 requests (quick validation) |

## Comment examples

### GuideLLM benchmark

```text
/test forge
/cluster janus
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: kserve-e2e-perf
/var inference_playbooks.workload: profile1
```

### AI Perf AgentX 128k

```text
/test forge
/cluster janus
/exclusive true
/gpu nvidia 8
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: kserve-e2e-perf
/var inference_playbooks.workload: aiperf-agentx-128k
```

### Both workloads sequentially (same serving deployment)

```text
/test forge
/cluster janus
/exclusive true
/gpu nvidia 8
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: kserve-e2e-perf
/var inference_playbooks.workload: profile1,aiperf-agentx-128k
```

### AI Perf concurrency sweep (4 sequential runs)

```text
/test forge
/cluster janus
/exclusive true
/gpu nvidia 8
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: kserve-e2e-perf
/var inference_playbooks.workload: aiperf-agentx-unlimited
```

`aiperf-agentx-unlimited` defines `concurrency: [1, 4, 16, 32]`. Forge
expands this into 4 sequential AI Perf Jobs, each with its own artifact
directory. Cache isolation across runs is handled by AI Perf's built-in
`--cache-bust first_turn_prefix` mechanism.

### Smoke test (quick plumbing validation)

```text
/test forge
/cluster janus
/var inference_playbooks.recipe: <recipe-id>
/var inference_playbooks.namespace: kserve-e2e-perf
/var inference_playbooks.workload: aiperf-smoke
```

## Notes

- `/exclusive true` is recommended for AI Perf runs to avoid GPU contention
  during the benchmark.
- `/gpu nvidia 8` (or more) should match the recipe's GPU requirements.
- When running against an in-progress Forge PR, add
  `/var fournos.job.extra_env.PULL_PULL_SHA: <commit-sha>` to pin the Forge
  code version. Once the AI Perf integration merges into `openshift-psap/forge:main`,
  this override is no longer needed.
