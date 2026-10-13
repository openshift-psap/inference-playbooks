"""Bounded, version-audited TP-local/DP-node startup contract."""

from pathlib import Path

import yaml
from jsonschema import ValidationError

from engine_versions import engine_errors, load_engine_index, normalize_version, resolve_engine
from recipe_evidence import load_unique_yaml
from render_inputs import canonical_arg_errors, platform_config


# Only this upstream contract has been audited. No implicit newer-version range.
AUDITED_ENGINE_VERSIONS = {"0.24.0"}
SOURCE = "https://github.com/vllm-project/vllm/blob/v0.24.0/docs/serving/data_parallel_deployment.md"

# TP-local/DP-node: one LWS group, size=DP, TP GPUs/pod; vLLM owns GPU ranks.
# Leader: DP start rank 0/default, one API process/internal LB; workers: headless,
# local DP=1, start rank=LWS_WORKER_INDEX (not --data-parallel-rank/external LB).
# Pod IP + controller leader DNS; LeaderCreated avoids readiness startup deadlock.
# HTTP probes are leader-only; shared weights require explicit read-only multi-node
# PVC access. Pins/overlays/alternate rank modes are outside this audited contract.
# Versioned flag/headless/controller evidence (source audit, not runtime validation):
# https://github.com/vllm-project/vllm/blob/v0.24.0/vllm/engine/arg_utils.py
# https://github.com/vllm-project/vllm/blob/v0.24.0/vllm/entrypoints/cli/serve.py
# https://github.com/vllm-project/vllm/blob/v0.24.0/vllm/entrypoints/openai/cli_args.py
# https://github.com/kubernetes-sigs/lws/blob/v0.7.0/api/leaderworkerset/v1/leaderworkerset_types.go
# https://github.com/kubernetes-sigs/lws/blob/v0.7.0/pkg/utils/pod/pod_utils.go
# https://github.com/kubernetes-sigs/lws/blob/v0.7.0/pkg/controllers/pod_controller.go
# https://github.com/kubernetes-sigs/lws/blob/v0.7.0/pkg/utils/controller/controller_utils.go


def is_distributed_dp(recipe: dict, platform: dict) -> bool:
    return (platform.get("stack") == "vllm" and recipe.get("deployment", {}).get("scope") == "multi-node"
            and recipe.get("serving", {}).get("parallelism", {}).get("mode") == "tp+dp")


def distributed_dp_errors(repo: Path, recipe: dict,
                          serving: dict, platform: dict) -> list[str]:
    if not is_distributed_dp(recipe, platform) or platform.get("blocked"):
        return []
    from validate import resolve_role_args

    errors = []
    parallelism = serving["parallelism"]
    tp, dp = parallelism.get("tp", 1), parallelism.get("dp", 1)
    if dp < 2 or parallelism.get("pp", 1) != 1:
        errors.append("distributed tp+dp requires DP>=2 and PP=1: one DP rank per node, TP within each pod")
    if platform.get("pinned_manifest"):
        errors.append("distributed-DP pinned startup is not covered by the audited TP-local/DP-node template")
    if platform_config(recipe, platform):
        errors.append("distributed-DP overlays can change rank/routing/security; use declarative inputs and config:null")
    if serving.get("router") or serving.get("prefill"):
        errors.append("distributed-DP internal LB does not implement router/P-D configuration")
    if serving.get("port", 8000) == 13345:
        errors.append("distributed-DP API port conflicts with its audited RPC port 13345")
    try:
        index = load_engine_index(repo)
        # serving has already been merged with this platform's image metadata.
        errors.extend(engine_errors(serving, {}, platform, index, require_metadata=True))
        engine = resolve_engine(serving, {}, platform, index)
        if engine["state"] == "resolved" and normalize_version(platform["version"]) != engine["version"]:
            errors.append("distributed-DP platform version must match the resolved image-bound engine version")
        if engine["state"] != "resolved" or engine["version"] not in AUDITED_ENGINE_VERSIONS:
            errors.append(f"distributed-DP requires an image-bound resolved engine in {sorted(AUDITED_ENGINE_VERSIONS)}; "
                          f"got {engine['state']} {engine['version']}; audit that version's internal-LB/headless contract first ({SOURCE})")
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, ValidationError) as error:
        errors.append(f"distributed-DP engine provenance: {error}")
    owned = {"--tensor-parallel-size", "--pipeline-parallel-size", "--nnodes", "--node-rank", "--master-addr",
             "--master-port", "--distributed-executor-backend", "--headless", "--api-server-count", "--host", "--port",
             "--model", "--served-model-name", "--enable-expert-parallel", "--all2all-backend", "--enable-elastic-ep",
             "--config", "--grpc", "--uds", "--enable-ssl-refresh"}
    for role in ("leader", "worker"):
        args = resolve_role_args(serving, "decode", role)
        errors.extend(canonical_arg_errors(args))
        for arg in args:
            flag = arg["flag"]
            if flag in owned or flag.startswith(("--data-parallel-", "--enable-ep-", "--ssl-")):
                errors.append(f"distributed-DP {role} arg {flag} overrides template-owned TP/DP/internal-LB startup or enables unsupported EP")
    names = [e["name"] for e in serving.get("env", [])]
    reserved = {"HF_TOKEN", "POD_IP", "VLLM_HOST_IP", "LWS_LEADER_ADDRESS", "LWS_WORKER_INDEX", "LWS_GROUP_SIZE",
                "RANK", "LOCAL_RANK", "WORLD_SIZE", "LOCAL_WORLD_SIZE", "VLLM_RUST_FRONTEND_PATH",
                "VLLM_ENABLE_V1_MULTIPROCESSING", "VLLM_USE_V1", "CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES"}
    if len(names) != len(set(names)) or any(n in reserved or n.startswith("VLLM_DP_") for n in names):
        errors.append("distributed-DP env conflicts with template/controller-owned address, credentials, or vLLM ranks")
    for side in ("requests", "limits"):
        values = serving.get("resources", {}).get(side, {})
        if set(values) - {"cpu", "memory", "nvidia.com/gpu"}:
            errors.append(f"distributed-DP cannot silently drop extra {side} resources")
        if "nvidia.com/gpu" in values and str(values["nvidia.com/gpu"]) != str(tp):
            errors.append(f"distributed-DP {side} GPU allocation must equal per-pod TP={tp}, not global TP*DP={tp*dp}")
    profile = (repo / recipe["hardware_profile"]).resolve()
    if not profile.is_relative_to(repo.resolve()) or not profile.is_file():
        errors.append("distributed-DP needs an explicit existing hardware profile")
    else:
        try:
            accelerators = load_unique_yaml(profile.read_text())["accelerators"]
            if accelerators["vendor"].lower() != "nvidia" or tp > accelerators["count_per_node"]:
                errors.append("distributed-DP template requires NVIDIA GPUs with TP fitting each declared node")
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
            errors.append(f"distributed-DP hardware profile is incomplete/invalid: {error}")
    variant = serving.get("image_usage", {}).get("variant")
    if variant and variant != "cuda":
        errors.append("distributed-DP NVIDIA template cannot map a non-CUDA runtime variant")
    storage = recipe.get("deployment", {}).get("storage", {})
    pvc = storage.get("pvc", {})
    if storage and (storage.get("type") != "pvc" or not pvc.get("mount_path") or pvc.get("read_only") is not True):
        errors.append("distributed-DP weights need the explicit pre-populated read-only PVC contract")
    if pvc and not set(pvc.get("access_modes", [])) & {"ReadOnlyMany", "ReadWriteMany"}:
        errors.append("one shared weights PVC across DP nodes requires declared ReadOnlyMany or ReadWriteMany access; no mode is inferred")
    return errors
