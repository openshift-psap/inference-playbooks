# Kimi K3 model storage

The [recipe](../recipe.yaml) and generated
[LeaderWorkerSet](../manifests/vllm-v0.29.0/leaderworkerset.yaml) use the
existing 2 TiB RWX claim
`kserve-e2e-perf/inference-playbooks-model-kimi-k3-9fcb53074b` on Janus's
`nfs` StorageClass. Both LWS roles mount it read-only at `/models`; the complete
checkpoint must be at `/models/Kimi-K3`.

The separate [model-ops Job](../../../model-ops/download-kimi-k3-pvc.yaml)
populates an empty claim from the public `moonshotai/Kimi-K3` repository at
the pinned Hugging Face [revision
`f831ab66814297da540d832a5235f8e904f29d06`](https://huggingface.co/moonshotai/Kimi-K3/tree/f831ab66814297da540d832a5235f8e904f29d06).
It writes the checkpoint to the serving path and verifies config, safetensors
index, and all 96 non-empty shards. It does not create the PVC or run as part of
the serving deployment.

On 2026-10-20, a read-only inspection on Janus confirmed the claim is `Bound`,
contains `/models/Kimi-K3`, and has the pinned revision
`f831ab66814297da540d832a5235f8e904f29d06`: all 119 Hugging Face metadata
files referenced that revision, and all 96 index-referenced weight shards were
present and non-empty. The checkpoint occupied about 1.5 TiB. Reuse this claim;
do not run the downloader or create a duplicate claim on Janus. A temporary
read-only inspector was removed after the check. Janus's SCC subject review
also admitted both rendered roles with the namespace-assigned UID
`1000770000`. The repository is public, so the downloader does not need an HF
Secret.

For a new environment, provision an equivalent RWX claim first, then update
`deployment.storage.pvc.name` in the recipe, `claimName` in the model-ops Job, and
`claimName` in [config/lws-patch.yaml](../config/lws-patch.yaml) together. Keep
the claim writable by the preparation Job and readable by the serving pods. Do
not add the Job to the serving Kustomize overlay.

For a new, empty claim, use the same namespace as the serving deployment. First
change `claimName` in the Job to that claim; do not use the existing Janus claim
until its contents are verified:

~~~bash
export NAMESPACE=kserve-e2e-perf
export PVC_NAME=NEW_EMPTY_PVC
oc wait -n "${NAMESPACE}" --for=jsonpath='{.status.phase}'=Bound "pvc/${PVC_NAME}" --timeout=10m

# Replace NEW_EMPTY_PVC and set the Job's claimName to that same empty claim.
oc apply -n "${NAMESPACE}" -f models/kimi-k3/model-ops/download-kimi-k3-pvc.yaml
oc wait -n "${NAMESPACE}" --for=condition=complete job/download-kimi-k3-model --timeout=6h
oc logs -n "${NAMESPACE}" job/download-kimi-k3-model
~~~

A completed Job is retained and applying it again does not start another
download. Keep the PVC between serving runs. Do not clear model data during
per-run cleanup. If a download stops partway through, inspect the claim and
remove the incomplete `/models/Kimi-K3` directory before recreating the Job.

`HF_HOME` uses the separate writable `/tmp/hf-home` directory in the Job. The
serving overlay keeps its writable `/home/nobody/.cache` emptyDir separate from
the read-only weights claim and sets both LWS roles to run as non-root. It uses
the default service account and does not require `anyuid` or a `hostPath` mount.
Janus does not authorize that service account for `anyuid`, so this overlay uses
the namespace-assigned UID instead.

Verify that the claim is Bound/RWX, has enough capacity, and exposes all
checkpoint files with read permissions. Janus's file-content check did not test
simultaneous reads from both nodes or measure aggregate NFS throughput and model
loading time; verify those as part of the serving run.
NFS availability does not establish performance equivalent to local NVMe;
record the storage backend and observed loading behavior with benchmark evidence.
