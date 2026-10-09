"""Explicit single-node companions: planning and read-only policy checks.

Rendering imports validation, which calls these checks. Rendering/validation
seams are imported at call time below to avoid a module-initialization cycle.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

from engine_versions import (
    compare_versions,
    engine_errors,
    load_engine_index,
    normalize_version,
    resolve_engine,
)
from recipe_evidence import load_unique_yaml
from render_inputs import (
    canonical_arg_errors,
    config_path,
    declarative_errors,
    platform_config,
)


INDEX_SOURCE = "https://github.com/openshift-psap/inference-playbooks/blob/main/engine-versions/index.yaml"


def read_overrides(directory: Path, reference: str) -> dict:
    path = (directory / reference).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError(f"missing or escaping platform overrides: {reference}")
    value = load_unique_yaml(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected override mapping: {reference}")
    return value


def mapping_errors(repo: Path, recipe: dict, source: dict, effective: dict, path: Path) -> list[str]:
    """Check mapping responsibilities in stable diagnostic order, without writes."""
    errors = _source_overlay_errors(repo, path, recipe, source, effective)
    errors.extend(_declarative_runtime_errors(recipe, source, effective))
    errors.extend(_resource_mapping_errors(effective))
    errors.extend(_argument_environment_errors(effective))
    errors.extend(_topology_mapping_errors(recipe, source, effective))
    errors.extend(_hardware_mapping_errors(repo, recipe, effective))
    return errors


def _source_overlay_errors(repo, path, recipe, source, serving):
    """Pins and unscoped patches cannot establish an automatic counterpart."""
    errors = []
    if source.get("pinned_manifest"):
        errors.append("pinned source requires an explicitly reviewed vLLM platform; cannot infer equivalence")
    reference = platform_config(recipe, source)
    if reference and "config" not in source:
        errors.append("shared config/ overlays are not platform-targeted; provide a reviewed vLLM platform instead")
    elif reference:
        errors.extend(overlay_mapping_errors(repo, path, recipe, source, serving, reference))
    return errors


def _declarative_runtime_errors(recipe, platform, serving):
    """Require the supported storage/runtime contract, not inferred replacements."""
    errors = []
    storage = recipe.get("deployment", {}).get("storage", {})
    pvc = storage.get("pvc", {})
    if storage and (
        storage.get("type") != "pvc"
        or not pvc.get("mount_path")
        or pvc.get("read_only") is not True
    ):
        errors.append("automatic weights mapping requires explicit pre-populated read-only PVC mount_path contract")
    errors.extend(declarative_errors(recipe, serving, platform))
    if serving.get("prefill") or serving.get("router"):
        errors.append("prefill/router mapping is unsupported")
    variant = serving.get("image_usage", {}).get("variant")
    if variant and variant != "cuda":
        errors.append("identified runtime variant is incompatible with the NVIDIA Deployment contract")
    return errors


def _resource_mapping_errors(serving):
    """The single-node Deployment owns TP*DP GPU allocation."""
    errors = []
    for side in ("requests", "limits"):
        values = serving.get("resources", {}).get(side, {})
        extra = set(values) - {"cpu", "memory", "nvidia.com/gpu"}
        if extra:
            errors.append(f"Deployment cannot preserve extra {side} resources: {', '.join(sorted(extra))}")
        gpu_count = serving["parallelism"].get("tp", 1) * serving["parallelism"].get("dp", 1)
        if "nvidia.com/gpu" in values and str(values["nvidia.com/gpu"]) != str(gpu_count):
            errors.append(f"explicit {side} GPU count conflicts with TP*DP")
    return errors


def _argument_environment_errors(serving):
    """Canonical argv and appended env must not conflict with template owners."""
    from validate import PARALLELISM_FLAGS, resolve_role_args

    errors = []
    args = resolve_role_args(serving, "decode", "leader")
    errors.extend(canonical_arg_errors(args))
    if any(arg["flag"] in PARALLELISM_FLAGS for arg in args):
        errors.append("parallelism flags in effective args conflict with template-owned parallelism")
    names = [entry["name"] for entry in serving.get("env", [])]
    if len(names) != len(set(names)) or "HF_TOKEN" in names:
        errors.append("effective env conflicts with appended values or template-owned HF_TOKEN; requires reviewed mapping")
    return errors


def _topology_mapping_errors(recipe, platform, serving):
    """Only an existing source template and node-local TP/DP can be mapped."""
    from render import TEMPLATE_MAP

    errors = []
    parallelism = serving.get("parallelism", {})
    template_key = (platform["stack"], parallelism.get("mode"), recipe["deployment"]["scope"])
    if template_key not in TEMPLATE_MAP:
        errors.append("source platform/topology has no renderer contract; declare reviewed platforms explicitly")
    if parallelism.get("mode") not in ("tp", "dp", "tp+dp") or parallelism.get("pp", 1) != 1:
        errors.append("automatic single-node companions require Deployment-compatible TP/DP (pp=1)")
    if parallelism.get("mode") == "tp" and parallelism.get("dp", 1) != 1:
        errors.append("TP source cannot silently encode data parallelism; use an explicitly supported topology")
    return errors


def _hardware_mapping_errors(repo, recipe, serving):
    """Check declared node capacity without guessing hardware/network facts."""
    errors = []
    parallelism = serving.get("parallelism", {})
    profile_path = (repo / recipe["hardware_profile"]).resolve()
    if not profile_path.is_relative_to(repo.resolve()) or not profile_path.is_file():
        errors.append("hardware profile is missing or escapes the repository")
    else:
        profile = load_unique_yaml(profile_path.read_text())
        accelerators = profile.get("accelerators", {})
        if accelerators.get("vendor", "").lower() != "nvidia":
            errors.append("Deployment template only implements nvidia.com/gpu resources")
        gpu_count = parallelism.get("tp", 1) * parallelism.get("dp", 1)
        if gpu_count > accelerators.get("count_per_node", 0):
            errors.append("TP*DP exceeds the declared single-node GPU capacity")
    return errors


def overlay_mapping_errors(repo: Path, path: Path, recipe: dict, platform: dict,
                           serving: dict, reference: str) -> list[str]:
    """Allow scoped informational overlays; never interpret arbitrary patches."""
    from render import COMPONENT_KIND, run_kustomize, select_template

    try:
        directory = config_path(path.parent, reference)
        customization = _load_local_customization(directory)
        template_key = (
            platform["stack"],
            serving["parallelism"]["mode"],
            recipe["deployment"]["scope"],
        )
        template = select_template(*template_key)
        kind = COMPONENT_KIND[template_key]
        manifest_directory = path.parent / "manifests" / f"{platform['stack']}-{platform['version']}"
        _check_overlay_paths(customization, directory, manifest_directory / f"{kind.lower()}.yaml")
        base = _render_canonical_base(repo, recipe, serving, platform, template)
        rendered = run_kustomize(base, path.parent, reference)
        return _overlay_output_errors(base, rendered, reference)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, yaml.YAMLError) as error:
        return [f"{reference}: cannot verify scoped overlay mapping: {error}"]


def _load_local_customization(directory):
    """Admit local base + patches only; never run generators or remote inputs."""
    customization = load_unique_yaml((directory / "kustomization.yaml").read_text())
    allowed_fields = {"apiVersion", "kind", "resources", "patches"}
    if not isinstance(customization, dict) or set(customization) - allowed_fields:
        raise ValueError("automatic mapping accepts only local base + patches; generators/transformers/custom configuration require review")
    return customization


def _check_overlay_paths(customization, directory, expected_manifest):
    """Keep every resource/patch within its declared ownership boundary."""
    for resource in customization.get("resources", []):
        if (directory / resource).resolve() != expected_manifest.resolve():
            raise ValueError("automatic scoped overlays must reference only their generated base; external/additional resources are blocked")
    for patch in customization.get("patches", []):
        if "path" in patch:
            patch_path = (directory / patch["path"]).resolve()
            if not patch_path.is_relative_to(directory) or not patch_path.is_file():
                raise ValueError("scoped overlay patch is missing or escapes its directory")


def _render_canonical_base(repo, recipe, serving, platform, template):
    """Reuse renderer inputs/constraints without rendering recipe artifacts."""
    from constraints import load_constraints
    from render import build_template_context, render_template

    model_path = repo / "models" / recipe["model_id"] / "model.yaml"
    model = load_unique_yaml(model_path.read_text()) if model_path.exists() else {}
    effective_recipe = {**recipe, "serving": serving}
    context = build_template_context(effective_recipe, model, load_constraints(repo), platform)
    return render_template(template, context, repo / "templates")


def _strip_informational_description(document):
    """Only description is ignored; unknown admission/security metadata stays."""
    metadata = document.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("invalid overlay metadata mapping")
    annotations = metadata.get("annotations", {})
    if not isinstance(annotations, dict):
        raise ValueError("invalid overlay annotations mapping")
    description = annotations.get("kubernetes.io/description")
    if "kubernetes.io/description" in annotations and not isinstance(description, str):
        raise ValueError("informational overlay description must be a string")
    annotations.pop("kubernetes.io/description", None)
    if not annotations:
        metadata.pop("annotations", None)


def _overlay_output_errors(base, rendered, reference):
    """Compare fresh parsed documents, never mutate recipe inputs."""
    before = list(yaml.safe_load_all(base))
    after = list(yaml.safe_load_all(rendered))
    if len(before) != 1 or len(after) != 1 or not isinstance(after[0], dict):
        return [f"{reference}: scoped overlay adds/removes deployment objects; automatic mapping blocked"]
    for document in (before[0], after[0]):
        _strip_informational_description(document)
    if before != after:
        return [f"{reference}: overlay changes runtime/security/object identity; migrate known settings to declarative inputs or review an explicit counterpart"]
    return []


def plan_companion(repo: Path, path: Path, recipe: dict, version: str | None = None,
                   target_overrides: str | None = None) -> tuple[dict, str, dict] | None:
    """Plan one explicit unverified companion, without writes or version guessing."""
    from render import merge_overrides

    explicit_selection = bool(version or target_overrides)
    sources = _candidate_sources(recipe, explicit_selection)
    if not sources:
        return None
    index = load_engine_index(repo)
    missing_source = _resolve_missing_source(path, recipe, sources, index, explicit_selection)
    if missing_source is None:
        return None

    source, source_overrides, resolution = missing_source
    effective_source = merge_overrides(recipe["serving"], source_overrides)
    _require_mapping(repo, path, recipe, source, effective_source)
    selected_version, is_newer = _select_version(resolution, version, target_overrides)
    output = _source_snapshot(source_overrides, effective_source, resolution)
    target_platform = _target_platform(selected_version, target_overrides)
    if target_overrides:
        output, effective_target = _apply_target_overrides(
            path.parent, recipe["serving"], source_overrides, effective_source,
            output, target_platform, is_newer, index,
        )
        _require_mapping(repo, path, recipe, target_platform, effective_target)

    updated = copy.deepcopy(recipe)
    updated["platforms"].append(_unverified_companion(source, target_platform, is_newer))
    return updated, target_platform["overrides"], output


def _candidate_sources(recipe, explicit_selection):
    """Leave multi-node and contributor-owned vLLM platforms untouched."""
    if recipe.get("schema_version") != 4 or recipe.get("deployment", {}).get("scope") != "single-node":
        if explicit_selection:
            raise ValueError("version selection applies only to single-node v4 recipes")
        return []
    sources = [
        platform
        for platform in recipe["platforms"]
        if platform["stack"] != "vllm" and not platform.get("blocked")
    ]
    if not sources:
        if explicit_selection:
            raise ValueError("no active non-vLLM source platform")
        return []
    if not explicit_selection and any(
        platform["stack"] == "vllm" and not platform.get("blocked") and not platform.get("companion")
        for platform in recipe["platforms"]
    ):
        return []
    return sources


def _platform_identity(platform):
    return {"stack": platform["stack"], "version": platform["version"]}


def _matching_counterparts(platforms, source, engine_version):
    matches = []
    for platform in platforms:
        if platform["stack"] != "vllm" or platform.get("blocked"):
            continue
        matches_version = normalize_version(platform["version"]) == engine_version
        if matches_version:
            matches.append(platform)
        elif platform.get("companion", {}).get("source") == _platform_identity(source):
            matches.append(platform)
    return matches


def _resolve_missing_source(path, recipe, sources, index, explicit_selection):
    """Resolve every source in declaration order; never guess between missing ones."""
    missing = []
    for source in sources:
        overrides = read_overrides(path.parent, source["overrides"])
        resolution = resolve_engine(recipe["serving"], overrides, source, index)
        violations = engine_errors(recipe["serving"], overrides, source, index, require_metadata=True)
        if violations:
            raise ValueError(f"{source['stack']}-{source['version']}: {'; '.join(violations)}")
        if resolution["state"] != "resolved":
            raise ValueError(f"{source['stack']}-{source['version']}: {resolution['reason']}")
        matches = _matching_counterparts(recipe["platforms"], source, resolution["version"])
        if matches:
            if explicit_selection:
                raise ValueError("counterpart already exists; edit its explicit platform inputs instead")
            continue
        missing.append((source, overrides, resolution))
    if not missing:
        return None
    if len(missing) != 1:
        raise ValueError("multiple missing source counterparts; declare their vLLM platforms explicitly")
    return missing[0]


def _require_mapping(repo, path, recipe, platform, serving):
    errors = mapping_errors(repo, recipe, platform, serving, path)
    if errors:
        raise ValueError("; ".join(errors))


def _select_version(resolution, version, target_overrides):
    """Exact reuse by default; a newer release needs explicit provenanced inputs."""
    selected = normalize_version(version) if version else resolution["version"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", selected):
        raise ValueError("resolved build cannot be represented as a platform version; declare a reviewed platform explicitly")
    comparison = compare_versions(selected, resolution["version"])
    if comparison not in (0, 1):
        raise ValueError("chosen version must equal the resolved engine or be a comparable, explicitly newer release")
    if comparison == 1 and not target_overrides:
        raise ValueError("newer selection requires --overrides with image-bound engine provenance")
    return selected, comparison == 1


def _source_snapshot(overrides, serving, resolution):
    """Bind copied source overrides to their exact image and engine evidence."""
    output = copy.deepcopy(overrides)
    output["image"] = resolution["image"]
    output["image_usage"] = copy.deepcopy(serving["image_usage"])
    evidence = resolution["evidence"] if resolution["source"] == "declaration" else INDEX_SOURCE
    output["engine"] = {
        "name": "vllm",
        "version": resolution["version"],
        "image": resolution["image"],
        "source": evidence,
    }
    return output


def _target_platform(selected_version, target_overrides):
    """Make explicit paths; match existing upstream constraint-key spellings."""
    # Follow the existing constraint index's v-prefixed upstream platform keys.
    platform_version = selected_version
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", selected_version):
        platform_version = f"v{selected_version}"
    reference = target_overrides or f"platforms/vllm-{platform_version}.yaml"
    if not re.fullmatch(r"platforms/[^/]+\.ya?ml", reference):
        raise ValueError("--overrides must name a file directly under platforms/")
    return {"stack": "vllm", "version": platform_version, "overrides": reference, "config": None}


def _apply_target_overrides(directory, base_serving, source_overrides, effective_source,
                            output, platform, is_newer, index):
    """Merge reviewed target inputs while retaining one copy of appended env."""
    from render import merge_overrides

    target_overrides = read_overrides(directory, platform["overrides"])
    if is_newer and not all(key in target_overrides for key in ("image", "image_usage", "engine")):
        raise ValueError("newer overrides require explicit image, image_usage, and engine")
    effective_target = merge_overrides(effective_source, target_overrides)
    output.update(target_overrides)
    for key in ("args", "resources", "router", "probes", "served_model_name", "shared_memory"):
        if key in effective_target:
            output[key] = copy.deepcopy(effective_target[key])
    if "env" in source_overrides or "env" in target_overrides:
        output["env"] = copy.deepcopy(source_overrides.get("env", []) + target_overrides.get("env", []))
    selected_version = normalize_version(platform["version"])
    resolution_platform = {"stack": "vllm", "version": selected_version}
    resolution = resolve_engine(base_serving, output, resolution_platform, index)
    violations = engine_errors(base_serving, output, resolution_platform, index, require_metadata=True)
    if violations:
        raise ValueError("; ".join(violations))
    if resolution["state"] != "resolved" or resolution["version"] != selected_version:
        raise ValueError("chosen platform version differs from the image-bound engine declaration")
    return output, effective_target


def _unverified_companion(source, target_platform, is_newer):
    """Never inherit source maturity, deployment status, or benchmark evidence."""
    return {
        **target_platform,
        "companion": {
            "source": _platform_identity(source),
            "version_policy": "newer" if is_newer else "same",
        },
        "verification": {
            "maturity": "day-zero",
            "deployment_status": {
                "state": "needs-verification",
                "note": "Prepared configuration only; no deployment or benchmark verification.",
            },
            "benchmark_runs": [],
        },
    }


def companion_errors(repo: Path, path: Path, recipe: dict) -> list[str]:
    """Check discovery, version provenance, and stale source snapshots, read-only."""
    if recipe.get("schema_version") != 4:
        return []
    if recipe.get("deployment", {}).get("scope") != "single-node" and not any(
        p.get("companion") for p in recipe["platforms"]
    ):
        return []
    errors = []
    keys = [
        (platform["stack"], normalize_version(platform["version"]))
        for platform in recipe["platforms"]
    ]
    if len(keys) != len(set(keys)):
        errors.append("duplicate platform stack/version (including v-prefixed aliases)")
    try:
        if plan_companion(repo, path, recipe):
            errors.append("missing explicit single-node vLLM companion; run python3 tools/prepare_companions.py " + str(path))
        for platform in recipe["platforms"]:
            _check_declared_companion(repo, path, recipe, platform, errors)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        errors.append(str(error))
    return errors


def _find_source_platform(platforms, identity):
    for platform in platforms:
        if _platform_identity(platform) == identity:
            return platform
    return None


def _check_declared_companion(repo, path, recipe, platform, errors):
    """Append in order so the outer catch retains diagnostics before an I/O error."""
    from render import merge_overrides

    metadata = platform.get("companion")
    if not metadata:
        return
    if (
        recipe["deployment"]["scope"] != "single-node"
        or platform.get("blocked")
        or platform.get("pinned_manifest")
    ):
        errors.append("automatic companion must be active, unpinned, and single-node")
        return
    source = _find_source_platform(recipe["platforms"], metadata["source"])
    if not source or source.get("blocked"):
        errors.append("companion source is missing or blocked")
        return

    source_overrides = read_overrides(path.parent, source["overrides"])
    target_overrides = read_overrides(path.parent, platform["overrides"])
    index = load_engine_index(repo)
    source_engine = resolve_engine(recipe["serving"], source_overrides, source, index)
    target_engine = resolve_engine(recipe["serving"], target_overrides, platform, index)
    errors.extend(engine_errors(recipe["serving"], source_overrides, source, index, require_metadata=True))
    errors.extend(engine_errors(recipe["serving"], target_overrides, platform, index, require_metadata=True))
    comparison = compare_versions(target_engine["version"], source_engine["version"])
    if (
        source_engine["state"] != "resolved"
        or target_engine["state"] != "resolved"
        or normalize_version(platform["version"]) != target_engine["version"]
    ):
        errors.append("companion and source require resolved, image-bound engine versions matching the platform")
    elif comparison != (0 if metadata["version_policy"] == "same" else 1):
        errors.append("companion version violates explicit same/newer policy")

    effective_source = merge_overrides(recipe["serving"], source_overrides)
    effective_target = merge_overrides(recipe["serving"], target_overrides)
    errors.extend(mapping_errors(repo, recipe, source, effective_source, path))
    errors.extend(mapping_errors(repo, recipe, platform, effective_target, path))
    if metadata["version_policy"] == "same":
        for key in (
            "image", "model", "parallelism", "args", "env", "resources", "port", "decode",
            "served_model_name", "probes", "shared_memory",
        ):
            if effective_source.get(key) != effective_target.get(key):
                errors.append(f"same-version companion has stale/different {key}; review explicit overrides")
