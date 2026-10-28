# Kimi K3 model storage

The [recipe](../recipe.yaml) uses node-local NVMe storage, not the shared NFS
PVC. The model volume's host path and read-only mount are owned by
[config/lws-patch.yaml](../config/lws-patch.yaml); the in-container checkpoint
path is owned by `serving.model` in the recipe. Both roles in the generated
[LeaderWorkerSet](../manifests/vllm-v0.29.0/leaderworkerset.yaml) use that same
layout. The hostPath uses `Directory`, not `DirectoryOrCreate`, so a missing
cache is not silently created on the root filesystem.

Before deploying:

1. Verify the configured cache is on the mounted local NVMe RAID filesystem on
   **every eligible GPU node**, including after node reboots.
2. Prepare the same Hugging Face [revision
   `f831ab66814297da540d832a5235f8e904f29d06`](https://huggingface.co/moonshotai/Kimi-K3/tree/f831ab66814297da540d832a5235f8e904f29d06)
   on both nodes. Verify the completion marker, all snapshot file sizes, and the
   config and safetensors index; all 96 referenced weight shards must be readable.
   Publish the final checkpoint directory only after preparation succeeds.
3. Confirm the namespace-assigned non-root UID can read the checkpoint. Keep the
   model volume read-only and keep writable runtime caches on the overlay's
   separate emptyDir volumes.
4. Confirm the configured service account is admitted by an SCC allowing this
   hostPath. Readable host files alone do not establish SCC admission; do not
   grant `anyuid` merely to bypass a hostPath restriction.

Preparation runs separately from serving. The older
[PVC download Job](../../../model-ops/download-kimi-k3-pvc.yaml) does not populate
the node-local caches and is not part of this serving deployment. The existing
NFS checkpoint can be retained independently; this recipe does not mount it.

Checkpoint availability does not establish inference or performance validation.
Verify real-checkpoint loading and distributed startup, then record the actual
storage backend and model-loading behavior with benchmark evidence.
