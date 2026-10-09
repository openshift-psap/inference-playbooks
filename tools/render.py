#!/usr/bin/env python3
"""Render Kubernetes manifests from Recipe v4 serving blocks.

Pipeline:
  recipe.yaml + model.yaml + hardware-profile + flag-constraints
    -> resolve constraints
    -> resolve role args
    -> select template (platform.stack, parallelism.mode, deployment.scope)
    -> render Jinja2
    -> optional kustomize overlay
    -> manifests/
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "templates"

sys.path.insert(0, str(REPO_ROOT / "tools"))
from constraints import (
    _build_recipe_context,
    evaluate_constraints,
    load_constraints,
    validate_recipe_against_constraints,
)
from recipe_evidence import load_unique_yaml
from render_inputs import config_path, declarative_errors, platform_config, probe_context
from distributed_dp import distributed_dp_errors, is_distributed_dp
from validate import resolve_role_args


TEMPLATE_MAP: dict[tuple[str, str, str], str] = {
    ("vllm", "tp", "single-node"): "vllm/deployment.yaml.j2",
    ("vllm", "dp", "single-node"): "vllm/deployment.yaml.j2",
    ("vllm", "tp+dp", "single-node"): "vllm/deployment.yaml.j2",
    ("vllm", "tp+dp", "multi-node"): "vllm/dp-lws.yaml.j2",
    **{(stack, mode, scope): template
       for stack, mode, template in (
           ("vllm", "pp", "vllm/lws.yaml.j2"),
           ("vllm", "tp+pp", "vllm/lws.yaml.j2"),
           ("rhoai", "tp", "rhoai/llmisvc.yaml.j2"),
           ("rhoai", "pp", "rhoai/llmisvc-pp.yaml.j2"),
           ("rhoai", "tp+pp", "rhoai/llmisvc-pp.yaml.j2"))
       for scope in ("single-node", "multi-node")},
}

COMPONENT_KIND = {key: {
    "vllm/deployment.yaml.j2": "Deployment",
    "vllm/lws.yaml.j2": "LeaderWorkerSet",
    "vllm/dp-lws.yaml.j2": "LeaderWorkerSet",
    "rhoai/llmisvc.yaml.j2": "LLMInferenceService",
    "rhoai/llmisvc-pp.yaml.j2": "LLMInferenceService",
}[template] for key, template in TEMPLATE_MAP.items()}


def load_yaml_file(path: Path) -> dict:
    text = path.read_text()
    data = load_unique_yaml(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected YAML mapping")
    return data


def sanitize_name(recipe_id: str) -> str:
    """Convert recipe_id to a valid Kubernetes resource name."""
    return re.sub(r"[^a-z0-9-]", "-", recipe_id.lower())[:63]


def select_template(stack: str, mode: str, scope: str) -> str:
    """Select a supported scope explicitly; never fall back to Deployment."""
    key = (stack, mode, scope)
    if key not in TEMPLATE_MAP:
        raise ValueError(
            f"No template for platform={stack}, mode={mode}, scope={scope}. "
            f"Supported: {sorted(TEMPLATE_MAP.keys())}"
        )
    return TEMPLATE_MAP[key]


def component_kind(stack: str, mode: str, scope: str) -> str:
    select_template(stack, mode, scope)
    return COMPONENT_KIND[(stack, mode, scope)]



def apply_constraint_flags(
    args: list[dict],
    removes: list[dict],
    forces: list[dict],
) -> list[dict]:
    """Apply constraint removes and forces to an args list."""
    remove_flags = {r["flag"] for r in removes}
    result = [a for a in args if a.get("flag") not in remove_flags]
    existing_flags = {a.get("flag") for a in result}
    for force in forces:
        flag = force["flag"]
        if flag in existing_flags:
            result = [
                {**a, "value": force["value"]} if a.get("flag") == flag else a
                for a in result
            ]
        else:
            result.append({
                "flag": flag,
                "value": force["value"],
                "required": True,
                "why": "; ".join(force["reasons"]),
            })
    return result


def build_template_context(
    recipe: dict,
    model: dict,
    constraints: dict | None,
    platform: dict | None = None,
) -> dict:
    """Build template rendering context from recipe + model + constraints."""
    serving = recipe["serving"]
    parallelism = serving["parallelism"]
    if platform is None:
        platform = recipe.get("platforms", [{}])[0]
    stack = platform.get("stack", "")
    mode = parallelism["mode"]
    scope = recipe.get("deployment", {}).get("scope")
    distributed_dp = is_distributed_dp(recipe, platform)

    tp = parallelism.get("tp", 1)
    pp = parallelism.get("pp", 1)
    dp = parallelism.get("dp", 1)

    resources = copy.deepcopy(serving.get("resources", {}))
    if "requests" not in resources:
        resources["requests"] = {}
    if "limits" not in resources:
        resources["limits"] = {}

    env = serving.get("env", [])
    port = serving.get("port", 8000)
    name = sanitize_name(recipe["recipe_id"])
    pvc = recipe.get("deployment", {}).get("storage", {}).get("pvc", {})
    pvc_name = pvc.get("name", f"{name}-weights")

    role_name = "decode"
    kind = component_kind(stack, mode, scope)

    if kind == "LeaderWorkerSet" or (stack == "rhoai" and pp > 1):
        leader_args = resolve_role_args(serving, role_name, "leader")
        worker_args = resolve_role_args(serving, role_name, "worker")
        args = resolve_role_args(serving, role_name, "leader")
    else:
        args = resolve_role_args(serving, role_name, "leader")
        leader_args = args
        worker_args = args

    if constraints:
        recipe_context = _build_recipe_context(recipe, model)
        recipe_context["platform"] = {"stack": stack, "version": platform.get("version", "")}
        removes, forces, _ = evaluate_constraints(constraints, recipe_context)
        args = apply_constraint_flags(args, removes, forces)
        leader_args = apply_constraint_flags(leader_args, removes, forces)
        worker_args = apply_constraint_flags(worker_args, removes, forces)

    replicas = 1

    return {
        "name": name,
        "pvc_name": pvc_name,
        "image": serving["image"],
        "model": serving["model"],
        "model_path": pvc.get("model_path", pvc.get("mount_path", serving["model"])),
        "served_model_name": serving.get("served_model_name", serving["model"]),
        "weights": pvc if pvc.get("mount_path") else {},
        "shared_memory": serving.get("shared_memory", {}).get("size", "4Gi"),
        "use_shared_memory": tp > 1 or bool(serving.get("shared_memory")),
        "probes": probe_context(serving, stack),
        "image_usage": serving.get("image_usage", {}),
        "tp": tp,
        "pp": pp,
        "dp": dp,
        "gpu_count": tp if distributed_dp else tp * dp,
        "distributed_dp": distributed_dp,
        "scope": scope,
        "api_service_name": name[:59] + "-api",
        "replicas": replicas,
        "args": args,
        "leader_args": leader_args,
        "worker_args": worker_args,
        "env": env,
        "port": port,
        "resources": resources,
        "recipe_id": recipe["recipe_id"],
        "kind": kind,
        "stack": stack,
        "mode": mode,
        "router": serving.get("router", {}),
    }


def render_template(template_path: str, context: dict, template_dir: Path = TEMPLATE_DIR) -> str:
    """Render a Jinja2 template with the given context."""
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["shellquote"] = shlex.quote
    template = env.get_template(template_path)
    return template.render(**context)


def run_kustomize(base_manifest: str, recipe_dir: Path, reference: str = "config") -> str:
    """Run kustomize build with config/ as overlay over generated base."""
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir)
        base_file = work_dir / "base-manifest.yaml"
        base_file.write_text(base_manifest)

        config_dir = config_path(recipe_dir, reference)
        resolved_config = config_dir.resolve()
        for item in config_dir.iterdir():
            if not item.is_file():
                continue
            if item.name == "kustomization.yaml":
                kustomization = yaml.safe_load(item.read_text()) or {}
                resources = kustomization.get("resources", [])
                new_resources = []
                for res in resources:
                    if "../manifests/" in res or res.startswith("../manifests"):
                        new_resources.append("base-manifest.yaml")
                    else:
                        src = (config_dir / res).resolve()
                        if not src.is_relative_to(resolved_config):
                            raise ValueError(f"resource path escapes config/: {res}")
                        if src.is_file():
                            dest = work_dir / src.name
                            shutil.copy2(src, dest)
                            new_resources.append(src.name)
                        else:
                            new_resources.append(res)
                kustomization["resources"] = new_resources
                if not new_resources:
                    kustomization["resources"] = ["base-manifest.yaml"]
                (work_dir / "kustomization.yaml").write_text(
                    yaml.dump(kustomization, default_flow_style=False)
                )
            else:
                shutil.copy2(item, work_dir / item.name)

        if not (work_dir / "kustomization.yaml").is_file():
            kustomization = {"resources": ["base-manifest.yaml"]}
            (work_dir / "kustomization.yaml").write_text(
                yaml.dump(kustomization, default_flow_style=False)
            )

        result = subprocess.run(
            ["kustomize", "build", str(work_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"kustomize build failed:\n{result.stderr}"
            )
        return result.stdout


def merge_overrides(serving: dict, overrides: dict) -> dict:
    """Merge platform overrides into a copy of the serving block."""
    merged = dict(serving)
    if "image" in overrides:
        merged["image"] = overrides["image"]
        if overrides["image"] != serving.get("image"):
            merged.pop("engine", None)
            merged.pop("image_usage", None)
    if "engine" in overrides:
        merged["engine"] = overrides["engine"]
    if "image_usage" in overrides:
        merged["image_usage"] = overrides["image_usage"]
    if "served_model_name" in overrides:
        merged["served_model_name"] = overrides["served_model_name"]
    if "resources" in overrides:
        merged["resources"] = overrides["resources"]
    if "router" in overrides:
        merged["router"] = overrides["router"]
    if "probes" in overrides:
        base_probes = dict(merged.get("probes", {}))
        base_probes.update(overrides["probes"])
        merged["probes"] = base_probes
    if "shared_memory" in overrides:
        merged["shared_memory"] = overrides["shared_memory"]
    if "env" in overrides:
        base_env = list(merged.get("env", []))
        base_env.extend(overrides["env"])
        merged["env"] = base_env
    if "args" in overrides:
        base_args = list(merged.get("args", []))
        override_flags = {a["flag"] for a in overrides["args"] if isinstance(a, dict)}
        base_args = [a for a in base_args if a.get("flag") not in override_flags]
        base_args.extend(overrides["args"])
        merged["args"] = base_args
    return merged


def platform_output_path(recipe: dict, platform: dict) -> Path:
    """The single declared artifact path for a platform (possibly multi-document)."""
    directory = f"{platform['stack']}-{platform['version']}"
    if platform.get("pinned_manifest"):
        filename = Path(platform['pinned_manifest']).name
    else:
        kind = component_kind(platform['stack'], recipe['serving']['parallelism']['mode'], recipe['deployment']['scope'])
        filename = f"{kind.lower()}.yaml"
    return Path("manifests") / directory / filename


def render_recipe(repo: Path, recipe_path: Path, dry_run: bool = False) -> tuple[str, list[str]]:
    """Normal local rendering materializes eligible inputs after isolated preflight.

    Read-only callers use dry_run or guard explicit inputs before rendering a
    scratch snapshot. Existing targets/assessments are never regenerated.
    """
    if dry_run:
        return _render_explicit_recipe(repo, recipe_path, dry_run=True)
    from companion_inputs import materialize, preparation_plan
    from companions import plan_companion

    recipe = load_yaml_file(recipe_path)
    try:
        if not plan_companion(repo, recipe_path, recipe):
            return _render_explicit_recipe(repo, recipe_path)
        plan, errors = preparation_plan(repo, recipe_path)
        if errors:
            return "", errors
        if recipe_path.is_symlink():
            return "", [f"{recipe_path}: automatic companion materialization cannot overwrite a symlinked recipe input"]
        original_inputs = recipe_path.read_bytes()
        output_paths = [
            platform_output_path(plan[0], platform)
            for platform in plan[0]["platforms"] if not platform.get("blocked")
        ]
        for output in output_paths:
            if not (recipe_path.parent / output).resolve().is_relative_to(recipe_path.parent.resolve()):
                return "", [f"{recipe_path}: manifest destination escapes the recipe directory: {output}"]
        relative_recipe = recipe_path.relative_to(repo)
        with tempfile.TemporaryDirectory(prefix="playbook-companion-") as temporary:
            scratch = Path(temporary)
            staged_path = scratch / relative_recipe
            shutil.copytree(recipe_path.parent, staged_path.parent, symlinks=True)
            for directory in ("schema", "templates", "engine-versions", "hardware-profiles"):
                shutil.copytree(repo / directory, scratch / directory, symlinks=True)
            model = repo / "models" / recipe["model_id"] / "model.yaml"
            if model.is_file():
                staged_model = scratch / model.relative_to(repo)
                staged_model.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(model, staged_model)
            for reference in [recipe_path.name, plan[1], *output_paths]:
                if not (staged_path.parent / reference).resolve().is_relative_to(staged_path.parent.resolve()):
                    return "", [f"{recipe_path}: staged input/output symlink escapes the recipe directory: {reference}"]
            materialize(staged_path, plan)
            with contextlib.redirect_stdout(io.StringIO()):
                rendered, errors = _render_explicit_recipe(scratch, staged_path)
            if errors:
                return "", [error.replace(str(scratch), str(repo)) for error in errors]
            outputs = {}
            for output in output_paths:
                outputs[output] = (staged_path.parent / output).read_bytes()
            # Recheck source/collisions after potentially slow overlay preflight;
            # never overwrite a concurrently supplied entry or verified assessment.
            current_plan, errors = preparation_plan(repo, recipe_path)
            if errors:
                return "", errors
            if recipe_path.read_bytes() != original_inputs or current_plan != plan:
                return "", [f"{recipe_path}: companion inputs changed during render; review and retry"]
            for output in output_paths:
                if not (recipe_path.parent / output).resolve().is_relative_to(recipe_path.parent.resolve()):
                    return "", [f"{recipe_path}: manifest destination changed to escape the recipe directory; review and retry"]
            materialize(recipe_path, plan)
            for relative_output, content in outputs.items():
                output = recipe_path.parent / relative_output
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
                print(f"  wrote {output.relative_to(repo)}")
            print(f"  materialized vllm-{plan[0]['platforms'][-1]['version']} companion (unverified)")
            return rendered, []
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, TemplateError) as error:
        return "", [f"{recipe_path}: {error}"]


def _render_explicit_recipe(
    repo: Path,
    recipe_path: Path,
    dry_run: bool = False,
) -> tuple[str, list[str]]:
    """Render a single v4 recipe for all platforms.

    Returns (rendered_yaml, errors).
    Errors are non-empty if rendering cannot proceed.
    """
    errors: list[str] = []
    recipe = load_yaml_file(recipe_path)

    if recipe.get("schema_version") != 4:
        return "", [f"{recipe_path}: not a v4 recipe (schema_version={recipe.get('schema_version')})"]

    serving = recipe.get("serving")
    if not isinstance(serving, dict):
        return "", [f"{recipe_path}: missing serving block"]

    from companions import companion_errors
    policy_errors = companion_errors(repo, recipe_path, recipe)
    if policy_errors:
        return "", [f"{recipe_path}: {error}" for error in policy_errors]

    if serving.get("prefill"):
        return "", [f"{recipe_path}: prefill/decode disaggregated serving not yet supported by renderer"]
    if serving.get("router", {}).get("strategy"):
        return "", [f"{recipe_path}: router configuration not yet supported by renderer"]

    model_path = repo / "models" / recipe.get("model_id", "") / "model.yaml"
    if model_path.is_file():
        model = load_yaml_file(model_path)
    else:
        model = {}

    try:
        constraints = load_constraints(repo)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        errors.append(f"flag-constraints: {exc}")
        constraints = None

    platforms = recipe.get("platforms", [])
    if not isinstance(platforms, list) or not platforms:
        return "", [f"{recipe_path}: no platforms defined"]

    all_rendered: list[str] = []
    pending_outputs: list[tuple[Path, bytes, bool]] = []
    for platform_entry in platforms:
        if platform_entry.get("blocked"):
            continue
        stack = platform_entry.get("stack", "")
        version = platform_entry.get("version", "")

        pinned_ref = platform_entry.get("pinned_manifest")
        if pinned_ref and is_distributed_dp(recipe, platform_entry):
            errors.append(f"{recipe_path}: distributed-DP pinned startup is outside the audited template contract")
            continue
        if pinned_ref:
            pinned_path = recipe_path.parent / pinned_ref
            if not pinned_path.is_file():
                errors.append(f"{recipe_path}: pinned_manifest does not exist: {pinned_ref}")
                continue
            pinned_bytes = pinned_path.read_bytes()
            rendered = pinned_bytes.decode("utf-8")
            if not dry_run:
                stack_dir = f"{stack}-{version}" if version else stack
                manifest_dir = recipe_path.parent / "manifests" / stack_dir
                output_path = manifest_dir / pinned_path.name
                pending_outputs.append((output_path, pinned_bytes, True))
            all_rendered.append(rendered)
            continue

        mode = serving.get("parallelism", {}).get("mode", "")

        effective_serving = serving
        overrides_ref = platform_entry.get("overrides")
        if overrides_ref:
            overrides_path = recipe_path.parent / overrides_ref
            if not overrides_path.is_file():
                errors.append(f"{recipe_path}: override file does not exist: {overrides_ref}")
                continue
            try:
                overrides = load_yaml_file(overrides_path)
                if overrides:
                    effective_serving = merge_overrides(serving, overrides)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                errors.append(f"{recipe_path}: cannot load overrides {overrides_ref}: {exc}")
                continue

        effective_recipe = dict(recipe)
        effective_recipe["serving"] = effective_serving
        effective_recipe["platform"] = {"stack": stack, "version": version}

        input_errors = declarative_errors(recipe, effective_serving, platform_entry)
        input_errors.extend(distributed_dp_errors(repo, recipe, effective_serving, platform_entry))
        if input_errors:
            errors.extend(f"{recipe_path}: [{stack}-{version}] {error}" for error in input_errors)
            continue

        if constraints:
            constraint_errors = validate_recipe_against_constraints(
                constraints, effective_recipe, model
            )
            if constraint_errors:
                errors.extend(f"{recipe_path}: [{stack}-{version}] {e}" for e in constraint_errors)
                continue

        try:
            template_path = select_template(stack, mode, recipe.get("deployment", {}).get("scope"))
        except ValueError as exc:
            errors.append(str(exc))
            continue

        context = build_template_context(effective_recipe, model, constraints, platform_entry)
        rendered = render_template(template_path, context, repo / "templates")

        reference = platform_config(recipe, platform_entry)
        if reference:
            try:
                rendered = run_kustomize(rendered, recipe_path.parent, reference)
            except (RuntimeError, OSError, ValueError) as exc:
                errors.append(f"{recipe_path}: kustomize failed: {exc}")
                continue

        if not dry_run:
            stack_dir = f"{stack}-{version}" if version else stack
            manifest_dir = recipe_path.parent / "manifests" / stack_dir
            kind = context["kind"]
            filename = f"{kind.lower()}.yaml"
            output_path = manifest_dir / filename
            pending_outputs.append((output_path, rendered.encode("utf-8"), False))

        all_rendered.append(rendered)

    if not errors:
        for output_path, _, _ in pending_outputs:
            if not output_path.resolve().is_relative_to(recipe_path.parent.resolve()):
                errors.append(f"{recipe_path}: manifest destination escapes the recipe directory: {output_path}")
    if not errors:
        for output_path, content, pinned in pending_outputs:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(content)
            print(f"  wrote {output_path.relative_to(repo)}" + (" (pinned)" if pinned else ""))
    return "\n---\n".join(all_rendered), errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "recipes",
        nargs="*",
        type=Path,
        help="Recipe paths to render (default: all v4 recipes)",
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render to stdout without writing files",
    )
    args = parser.parse_args()
    repo = args.repo.resolve()

    if args.recipes:
        recipe_paths = [p.resolve() for p in args.recipes]
    else:
        recipe_paths = sorted(
            repo.glob("models/*/recipes/*/recipe.yaml")
        )

    all_errors: list[str] = []
    rendered_count = 0

    for recipe_path in recipe_paths:
        try:
            recipe = load_yaml_file(recipe_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            all_errors.append(f"{recipe_path}: {exc}")
            continue

        if recipe.get("schema_version") != 4:
            continue

        print(f"Rendering {recipe_path.relative_to(repo)}...")
        rendered, errors = render_recipe(repo, recipe_path, dry_run=args.dry_run)

        if errors:
            all_errors.extend(errors)
        elif rendered:
            rendered_count += 1
            if args.dry_run:
                print(rendered)

    if all_errors:
        print("\nErrors:", file=sys.stderr)
        for err in all_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    print(f"\nRendered {rendered_count} recipe(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
