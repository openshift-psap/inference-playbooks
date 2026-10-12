"""Shared declarative input contracts for TP/Deployment rendering."""

import re
import json
from pathlib import Path, PurePosixPath


def platform_config(recipe: dict, platform: dict) -> str | None:
    if "config" in platform:
        return platform["config"]
    return "config" if recipe["serving"].get("config_overrides") else None


def canonical_arg_errors(args: list[dict]) -> list[str]:
    errors = []
    for arg in args:
        if arg["flag"] == "--speculative-config":
            try:
                if not isinstance(json.loads(arg.get("value", "")), dict):
                    raise ValueError("not an object")
            except (ValueError, TypeError):
                errors.append("--speculative-config requires a raw JSON object, not shell pre-quoting; migrate serialization overrides to canonical args")
    return errors


def config_path(recipe_dir: Path, reference: str) -> Path:
    path = (recipe_dir / reference).resolve()
    root = (recipe_dir / "config").resolve()
    if not root.is_relative_to(recipe_dir.resolve()) or not path.is_relative_to(root):
        raise ValueError(f"overlay escapes config/: {reference}")
    if not (path / "kustomization.yaml").is_file():
        raise ValueError(f"overlay missing {reference}/kustomization.yaml")
    return path


def declarative_errors(recipe: dict, serving: dict, platform: dict) -> list[str]:
    errors = []
    storage = recipe.get("deployment", {}).get("storage", {})
    pvc = storage.get("pvc", {})
    mount = pvc.get("mount_path")
    if mount:
        model = pvc.get("model_path", mount)
        for path in (mount, model):
            if not path.startswith("/") or path == "/" or any(p in (".", "..", "") for p in path[1:].split("/")):
                errors.append("PVC mount/model paths must be normalized absolute non-root paths")
        if storage.get("type") != "pvc" or not pvc.get("name") or "read_only" not in pvc:
            errors.append("local weights require explicit storage.type=pvc, name, mount_path, and read_only")
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", pvc.get("name", "")) or len(pvc.get("name", "")) > 253:
            errors.append("mounted PVC name must be a Kubernetes DNS subdomain")
        if not PurePosixPath(model).is_relative_to(PurePosixPath(mount)):
            errors.append("PVC model_path must be inside mount_path")
        for reserved in ("/dev", "/proc", "/sys", "/tmp", "/home/nobody/.cache"):
            if PurePosixPath(mount).is_relative_to(reserved) or PurePosixPath(reserved).is_relative_to(mount):
                errors.append(f"PVC mount_path conflicts with template/runtime path {reserved}")
    if mount or serving.get("shared_memory"):
        supported = platform["stack"] == "rhoai" and serving["parallelism"]["mode"] == "tp" or (
            platform["stack"] == "vllm" and serving["parallelism"]["mode"] in ("tp", "dp", "tp+dp"))
        if not supported:
            errors.append("declarative PVC/shared memory is supported only by TP RHOAI and vLLM Deployment/distributed-DP templates")
    return errors


def probe_context(serving: dict, stack: str) -> dict:
    port = serving.get("port", 8000)
    defaults = {
        "startup": {"periodSeconds": 30, "failureThreshold": 60},
        "liveness": {"periodSeconds": 30, "timeoutSeconds": 10, "failureThreshold": 5},
        "readiness": {"periodSeconds": 10, "timeoutSeconds": 5},
    } if stack == "vllm" else {"startup": {"failureThreshold": 720, "periodSeconds": 10}}
    result = {}
    names = list(defaults) + [n for n in serving.get("probes", {}) if n not in defaults]
    fields = {"failure_threshold": "failureThreshold", "period_seconds": "periodSeconds",
              "timeout_seconds": "timeoutSeconds", "initial_delay_seconds": "initialDelaySeconds"}
    for name in names:
        overrides = serving.get("probes", {}).get(name, {})
        options = dict(defaults.get(name, {}))
        options.update({fields[k]: v for k, v in overrides.items() if k in fields})
        result[name] = {"path": overrides.get("path", "/health"), "port": overrides.get("port", port), "options": options}
    return result
