# Multi-node vLLM: TP within nodes, DP across nodes

Selection uses `(stack, mode, deployment.scope)`; unsupported/missing tuples fail,
never fall back to Deployment. This path requires image-bound, source-resolved
**vLLM 0.24.0**, with matching platform version; other builds need a contract audit.

```yaml
serving:
  # Declare exact image, image_usage and engine provenance separately.
  parallelism: {mode: tp+dp, tp: 8, dp: 2}
deployment:
  scope: multi-node
```

This renders one LWS group (`replicas: 1`, `size: 2`): two node-sized pods,
**8 GPUs per pod**, one local DP rank per node. Required hostname anti-affinity
separates nodes; operators select eligible hardware. vLLM owns GPU/process ranks,
not one physical GPU per LWS index. Single-node TP+DP keeps TP*DP GPUs in Deployment.
PP/TP+PP and RHOAI choices stay unchanged. Multi-node pure TP/DP is unsupported;
multi-node recipes never receive [single-node companions](single-node-companions.md).

## Startup, routing and inputs

Both roles use TP, global DP, local DP=1, `mp` executor/DP backends, leader Pod IP
and RPC port **13345**—not PP `nnodes`, `node-rank` or pipeline-size flags.

| Role | Contract |
|---|---|
| Leader | Default DP rank 0; no start-rank flag; `--api-server-count 1`; HTTP on serving port/IP family. |
| Worker | `--headless --data-parallel-start-rank "$LWS_WORKER_INDEX"`; no HTTP server/probes/fixed listener. |
| Discovery | Downward-API Pod IP/`VLLM_HOST_IP`; workers resolve injected `LWS_LEADER_ADDRESS`. Group/rank bounds checked. |
| Routing | Leader-only API Service in `leaderworkerset.yaml`; LWS owns private headless DNS. `LeaderCreated` avoids startup deadlock. |

Internal LB dispatches across DP engines. **Do not set `--data-parallel-rank`**
(external LB), or API-node start rank (may infer hybrid LB). Workers start at LWS
index 1; headless start rank does not infer hybrid LB in the audited version.

Checkpoint/local path, alias, env, CPU/memory and shm apply to both pods;
`decode.leader_args`/`worker_args` remain separate and must be engine-compatible.
GPU requests/limits must equal **TP per pod**. Extra device resources, rank/LB/env
overrides, hidden CLI configs, alternate protocols/backends and EP are blocked.
An explicit CPU request without a limit gets no invented lower cap.

HTTP probes apply only to the API leader. vLLM supervises headless workers;
process exits recreate the group, but no worker HTTP readiness is claimed.
A shared weights PVC must be explicitly read-only with `ReadOnlyMany` or
`ReadWriteMany`; class/name/capacity are deployment-selected. No PVC/downloader
is created. Shm stays separate, with declared size or legacy TP>1 default 4Gi.

## Prerequisites and limits

Require LWS v0.7.0 controller behavior, eligible GPU nodes/device allocation,
`hf-token` Secret, cluster DNS and bidirectional Pod TCP connectivity including
dynamic internal ports. API port must not conflict with 13345. No NIC/IP/RDMA,
NetworkPolicy or host-network configuration is inferred.

Use `config: null` (or no active overlay); this path rejects pins/runtime overlays.
No hybrid/external LB, router chart, P/D, wide-EP, TP spanning nodes, extra local DP
ranks or arbitrary versions. Rendering/source audit is not runtime or benchmark
verification; [independent assessments](single-node-companions.md#mapping-and-verification) still apply.

## Versioned upstream evidence

- [vLLM DP deployment](https://github.com/vllm-project/vllm/blob/v0.24.0/docs/serving/data_parallel_deployment.md): internal-LB/headless topology.
- [Engine CLI/config](https://github.com/vllm-project/vllm/blob/v0.24.0/vllm/engine/arg_utils.py): flags and headless/hybrid inference.
- [Serve dispatch](https://github.com/vllm-project/vllm/blob/v0.24.0/vllm/entrypoints/cli/serve.py): headless launch/start index.
- [Frontend flags](https://github.com/vllm-project/vllm/blob/v0.24.0/vllm/entrypoints/openai/cli_args.py): API count/headless registration.
- [LWS API](https://github.com/kubernetes-sigs/lws/blob/v0.7.0/api/leaderworkerset/v1/leaderworkerset_types.go): env/startup policy.
- [Env injection](https://github.com/kubernetes-sigs/lws/blob/v0.7.0/pkg/utils/pod/pod_utils.go) and [worker ordinals](https://github.com/kubernetes-sigs/lws/blob/v0.7.0/pkg/controllers/pod_controller.go): workers 1..size-1.
- [Discovery Service](https://github.com/kubernetes-sigs/lws/blob/v0.7.0/pkg/utils/controller/controller_utils.go): headless, `publishNotReadyAddresses: true`.
