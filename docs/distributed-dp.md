# Multi-node vLLM: TP within nodes, DP across nodes

This is a bounded rendering convention, not arbitrary distributed-rank support.
Template selection is `(stack, parallelism.mode, deployment.scope)` with no
Deployment fallback for unsupported/missing scopes.

```yaml
serving:
  # Supply the exact image, image_usage, and image-bound engine provenance.
  parallelism:
    mode: tp+dp
    tp: 8
    dp: 2
deployment:
  scope: multi-node
```

This means **two node-sized pods, eight GPUs per pod**, one local DP rank per node.
It does not mean 16 GPUs per pod or one physical GPU per DP rank. vLLM owns its
GPU/process/TP ranks. The LWS group includes the leader, so `size: 2` means one
leader and one worker; `replicas: 1` means one global DP group. Required pod
anti-affinity on `kubernetes.io/hostname` places these pods on distinct nodes.
Deploy onto eligible nodes matching the declared hardware profile; the renderer
does not infer node selectors, NICs, hardware facts, or actual free capacity.

Single-node `tp+dp` remains a Deployment with TP*DP GPUs per pod. Existing
PP/TP+PP templates and RHOAI choices are unchanged. Multi-node vLLM pure `tp`
or `dp`, missing scope, and unsupported tuples fail explicitly. This feature does
not make multi-node recipes eligible for automatic single-node companions.

## Version-audited startup and routing

Only source-resolved, image-bound **vLLM 0.24.0** is currently enabled for this new
path. `platform.version` must match that engine. No image-tag guessing, latest
selection, carry-forward to newer versions, or opaque vendor-build ordering is
used. An unaudited version gets a diagnostic before artifacts are written.
The existing engine resolver remains the source of truth. Container inspection
and runtime/benchmark verification are separate from metadata resolution.

Both roles use `--tensor-parallel-size TP`, multiprocessing executor/DP backends,
`--data-parallel-size DP`, `--data-parallel-size-local 1`, the leader's Pod IP,
and DP RPC port 13345. They do **not** use the PP template's `--nnodes`,
`--node-rank`, `--master-addr` or `--pipeline-parallel-size` flags.

- **Leader:** default DP start rank zero (no explicit start-rank flag),
  `--api-server-count 1`, HTTP on `serving.port`; vLLM internal LB owns dispatch
  across all DP engines. HTTP wildcard binding follows the runtime Pod IP family.
- **Worker:** `--headless` and `--data-parallel-start-rank LWS_WORKER_INDEX`,
  no HTTP/API server. This is a DP rank, not a physical GPU rank.
- **Address:** leader uses downward-API Pod IP; workers resolve the controller's
  `LWS_LEADER_ADDRESS` to the appropriate IP family. `VLLM_HOST_IP` also uses
  downward-API Pod IP. DNS resolution waits for the controller-owned headless
  Service; no hostnames/IPs/NICs are guessed into recipe inputs.
- **Controller:** explicit `startupPolicy: LeaderCreated` avoids waiting for API
  readiness before launching the headless ranks that API startup needs. Runtime
  checks enforce LWS group size and worker index bounds.
- **Client routing:** a leader-only HTTP Service is included in the same generated
  `leaderworkerset.yaml` artifact. The LWS controller owns its private headless
  discovery Service. There is no llm-d/external router chart or hybrid/external LB.

Do not use `--data-parallel-rank`: in this version it enables external LB.
Likewise, setting start rank on a non-headless API node can infer hybrid LB.
The template deliberately avoids both. On headless secondary nodes, upstream
engine-args code does not infer hybrid LB from start rank.

## Declarative input ownership and prerequisites

Serving checkpoint metadata stays in `serving.model`; a declared PVC local path
is passed to both roles. Alias, env, CPU/memory resources and shm are retained.
`serving.decode.leader_args` and `worker_args` are resolved separately. Engine
configuration must remain compatible across ranks; vLLM owns its cross-rank
validation. Structured TP/DP/address/LB flags cannot be overridden by role args.
Unknown hidden CLI configs, alternate API protocols, rank env overrides, EP and
custom distributed backends are outside this startup contract and are blocked.

`serving.resources` applies independently to both pod roles. Explicit GPU
requests/limits must equal per-pod TP; extra device resource keys are blocked,
not discarded. An explicit CPU request without a CPU limit is not given an
invented lower default cap. Standard default CPU/memory and security settings
otherwise remain the template's documented defaults.

Declared HTTP probes apply to the **API leader**. Headless workers have no HTTP
endpoint, so projecting `/health` probes onto them would be incorrect. Their
engine process is supervised by vLLM; exits trigger LWS group recreation. No
worker HTTP readiness or independently validated engine readiness is claimed.

An optional shared pre-populated weights PVC must be explicitly read-only and
declare multi-node access (`ReadOnlyMany` or `ReadWriteMany`), not an inferred
universal storage mode. Claim name/class/capacity remain deployment-selected.
Both pods mount it at the declared path; no PVC or downloader is created.
Memory-backed shm is separate and uses the declared size (legacy TP default 4Gi).

Prerequisites include an LWS controller providing the v0.7.0 API/env/startup/
headless-Service behavior, GPU device allocation, the template's `hf-token`
Secret, eligible nodes, cluster DNS and bidirectional Pod-network TCP connectivity
including vLLM's dynamically selected internal ports. HTTP port cannot conflict
with RPC port 13345. No NetworkPolicy, RDMA or host-network discovery/configuration
is invented; no non-RDMA performance or live deployment claim is made.

The new path requires `config: null` (or no active overlay) and rejects pins or
runtime/security overlays rather than allowing them to undo its rank/routing/
placement contract. Existing single-node companion scoped-overlay support is
unchanged. P/D, router charts, wide-EP policy, hybrid/external LB, arbitrary TP
spanning nodes, additional local DP ranks, and other versions remain out of scope.

## Official source audit

The implementation was checked against these versioned upstream sources:

- [vLLM v0.24.0 DP deployment docs](https://github.com/vllm-project/vllm/blob/v0.24.0/docs/serving/data_parallel_deployment.md)
  — internal-LB multi-node/headless example and single HTTP entrypoint.
- [vLLM v0.24.0 engine CLI/config](https://github.com/vllm-project/vllm/blob/v0.24.0/vllm/engine/arg_utils.py)
  — DP size/local/start/address/RPC/backend flags; external-LB rank flag;
  start-rank hybrid inference excluded when `headless=True`.
- [vLLM v0.24.0 serve dispatch](https://github.com/vllm-project/vllm/blob/v0.24.0/vllm/entrypoints/cli/serve.py)
  — headless API count zero, headless engine launch and start index.
- [vLLM v0.24.0 frontend flags](https://github.com/vllm-project/vllm/blob/v0.24.0/vllm/entrypoints/openai/cli_args.py)
  — `--headless` and `--api-server-count` registration.
- [LWS v0.7.0 API](https://github.com/kubernetes-sigs/lws/blob/v0.7.0/api/leaderworkerset/v1/leaderworkerset_types.go)
  — injected leader address, group size, worker index, and LeaderCreated policy.
- [LWS v0.7.0 env injection](https://github.com/kubernetes-sigs/lws/blob/v0.7.0/pkg/utils/pod/pod_utils.go)
  and [worker StatefulSet creation](https://github.com/kubernetes-sigs/lws/blob/v0.7.0/pkg/controllers/pod_controller.go)
  — leader address from controller DNS, worker index from pod ordinal, workers
  start at ordinal one and number `size - 1` (not one index per physical GPU).
- [LWS v0.7.0 controller discovery Service](https://github.com/kubernetes-sigs/lws/blob/v0.7.0/pkg/utils/controller/controller_utils.go)
  — private headless Service with `publishNotReadyAddresses: true`.

These establish the source-backed startup contract, not deployed or benchmarked
validity. Existing independent platform verification requirements still apply;
no verification or benchmark evidence is manufactured by rendering.
