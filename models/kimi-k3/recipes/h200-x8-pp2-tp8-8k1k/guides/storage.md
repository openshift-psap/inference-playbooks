# Kimi K3 storage alternatives

The [recipe](../recipe.yaml) and generated
[LeaderWorkerSet](../manifests/vllm-v0.29.0/leaderworkerset.yaml) currently use
hostPath storage: stage a complete checkpoint at
`/mnt/local/kimi-k3/models/Kimi-K3` on both nodes. Restrict scheduling to
populated nodes and verify mount permissions and checkpoint integrity.

Model weights do not inherently require node-local storage. A pre-populated
ReadWriteMany (RWX) PVC accessible to both nodes can replace the model hostPath
volume, provided it presents the complete checkpoint at `/models/Kimi-K3` and
has sufficient capacity and aggregate read throughput for concurrent loading.
Concurrent read throughput determines loading behavior; do not assume load
times equivalent to local NVMe.

To use RWX, replace the `model` volume's `hostPath` with
`persistentVolumeClaim: {claimName: <existing-rwx-pvc>}` in both roles via
[`config/lws-patch.yaml`](../config/lws-patch.yaml), update the recipe's storage
declaration to match the PVC configuration, and rerender. These manifests do
not provision a PVC or storage backend.

Verify mount permissions, checkpoint integrity, and simultaneous loading on
both nodes. Consider read-only weight mounts and separate writable per-pod
cache storage rather than sharing `/models/hf` between serving processes.
Record the storage backend and loading behavior when collecting benchmark
evidence.
