# Kimi K3 pinned LLMInferenceService (RHOAI 3.5)

This is a **contributed, unvalidated test configuration**. No deployment or
benchmark has been performed as part of this conversion.

The [recipe](../recipe.yaml) pins the hand-authored
[LLMInferenceService](../manifests/rhoai-3.5/llmisvc.yaml). The renderer preserves
its bytes; it does not merge serving flags or platform overrides into the pin.
Update the pin explicitly when changing recipe settings. Both roles share the
same environment, resources, and storage through YAML anchors.

## Prerequisites and unresolved checks

- RHOAI 3.5 with the `serving.kserve.io/v1alpha2` LLMInferenceService API,
  pipeline-parallel controller/configuration support, and LeaderWorkerSet.
  Confirm the installed CRD supports `spec.parallelism.pipeline` and `spec.worker`.
- Two nodes matching the referenced [RoCE hardware profile](../../../../../hardware-profiles/nvidia-h200-sxm-8x-nvlink-roce-r1.yaml),
  with eight available H200 GPUs and one advertised `rdma/ib` resource per node.
  Confirm HCA/GID values on the target cluster.
- A complete model copy at `/mnt/local/kimi-k3/models/Kimi-K3` on each eligible
  node. The hostPath must already exist. Restrict scheduling to populated nodes
  using cluster-specific placement rules before deployment.
- Verify `docker.io/vllm/vllm-openai:kimi-k3` exists and obtain its digest.
  This is not a claim that the standard RHOAI runtime includes Kimi K3 support.
- Explicitly approved permissions for root and hostPath. The bootstrap modifies
  image site-packages. This manifest does not grant security permissions.
- Confirm KServe resolves the Go-template expressions for namespace, TLS, and
  certificate secret names. The leader enables TLS when controller configuration
  requires it; verify the reconciled probes, certificate mount, and routing.
- The worker uses LWS-injected rank and leader-address environment variables and
  runs headless. Confirm the controller retains these custom commands and does
  not inject incompatible runtime arguments, probes, or model-volume mounts.
- The submitted 8,192-token context limit remains pending contributor confirmation
  for GuideLLM 8K input + 1K output. Do not treat that workload as verified yet.
- If using a newer upstream image, remove/adapt the legacy SITU bootstrap first;
  current vLLM main has a different Humming implementation.

## Local preparation

From the repository root:

```sh
python3 tools/render.py models/kimi-k3/recipes/h200-x8-pp2-tp8-8k1k/recipe.yaml
python3 tools/validate.py --current --require-converted-raw
```

Before applying, use server-side dry-run against the selected cluster and inspect
the reconciled leader and worker pods in a test namespace. The apply target is
`manifests/rhoai-3.5/llmisvc.yaml`, not the vLLM Kustomize overlay. No standalone
Service is included: KServe owns its service/routing resources.

Promote maturity only after performance testing on our infrastructure and
committing the required benchmark run, sanitized raw output, normalized result,
image digest, model revision, hardware-profile revision, and environment details.
