# Kimi K3 model storage

The [recipe](../recipe.yaml) and generated
[LeaderWorkerSet](../manifests/vllm-v0.29.0/leaderworkerset.yaml) use the
pre-populated, 2 TiB RWX claim
`kserve-e2e-perf/inference-playbooks-model-kimi-k3-9fcb53074b` on Janus's
`nfs` StorageClass. The cluster currently marks this claim as populated from
`hf://moonshotai/Kimi-K3`. Both LWS roles mount it read-only at `/models`; the
checkpoint must be available at `/models/Kimi-K3`.

The recipe does not create or populate the PVC or provision NFS. Before deploying
elsewhere, create/populate a shared RWX claim, then update both
`deployment.storage.pvc.name` in the recipe and `claimName` in
[`config/lws-patch.yaml`](../config/lws-patch.yaml). Keep the claim pre-populated
and accessible to both nodes; the model weights are mounted read-only.

`HF_HOME` points to the separate writable `/home/nobody/.cache` emptyDir, not
the read-only model claim. The recipe uses the namespace's `default` service
account, which must be authorized for `anyuid` because the image runs as root.
The PVC itself does not require a hostPath mount or hostPath SCC.

Verify the claim is Bound/RWX, confirm checkpoint files and read permissions,
and measure aggregate NFS read throughput and simultaneous loading on both
nodes. NFS availability does not establish performance equivalent to local
NVMe. Record the storage backend and observed loading behavior with benchmark
evidence.
