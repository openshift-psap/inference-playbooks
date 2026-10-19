# Nemotron-3-Ultra H200 prefill/decode — v4 conversion

[Recipe](../recipe.yaml) · [Deployment bundle](../manifests/vllm-v0.26.0/pd.yaml)
· [Original deployment guide](../../../vllm/v0.26.0/multi-node/guides/deployment-guide.md)
· [Router values](../../../vllm/v0.26.0/multi-node/charts/router/values.yaml)

This **day-zero, unverified** v4 recipe preserves PR #23's two LeaderWorkerSets:
three prefill TP8 replicas and one decode TP8 replica. The pinned bundle is
authoritative for exact startup commands, environment, per-role ports and
resources, conditional MoE patch, security contexts and host bindings. Recipe
serving fields summarize shared settings and role differences. No benchmark
evidence or deployment verification is inherited from the original prose.

## Rebuild and review

From the repository root:

```bash
recipe=models/nemotron-3-ultra-550b/recipes/h200-x32-pd-3p1d-tp8-agentx-128k
kustomize build --load-restrictor LoadRestrictionsNone "$recipe/config" > "$recipe/manifests/vllm-v0.26.0/pd.yaml"
python3 tools/render.py "$recipe/recipe.yaml"
python3 tools/validate.py --current --require-converted-raw
```

The local Kustomize inputs reference the preserved legacy manifests in this
model. Only the short vLLM image name is qualified to `docker.io`; pod behavior
is otherwise preserved. `config/` is the conversion source, not an extra overlay
on the pin. Rollback consists of restoring reviewed config and regenerating;
the original layout and v3 recipe are retained for provenance, not catalog input.

## Before deploying

Review the original guide and router values. The LWS controller, a privileged
`llm-d-modelserver` service account, site-provided `rdma-discovery-script`
ConfigMap, RDMA device plugin, `/dev/infiniband`, GDRCopy and a populated
hostPath model cache are required. Conversion does not provision these.
The hardware profile records RDMA prerequisites without guessing transport or
bandwidth. Pod memory requests are requirements, not measured host capacity.

The decode routing-proxy sidecar is part of the bundle. The router Helm release
is separate; its chart revision must be reviewed and pinned before deployment.
No cluster is changed by conversion or rendering.
