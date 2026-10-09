#!/usr/bin/env python3
"""Render Kubernetes manifests from Recipe v4 serving blocks.

Pipeline:
  recipe.yaml + model.yaml + hardware-profile + flag-constraints
    -> resolve constraints
    -> resolve role args
    -> select template (platform.stack, parallelism.mode)
    -> render Jinja2
    -> optional kustomize overlay
    -> manifests/
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

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
from validate import resolve_role_args


TEMPLATE_MAP: dict[tuple[str, str], str] = {
    ("vllm", "tp"): "vllm/deployment.yaml.j2",
    ("vllm", "dp"): "vllm/deployment.yaml.j2",
    ("vllm", "tp+dp"): "vllm/deployment.yaml.j2",
    ("vllm", "pp"): "vllm/lws.yaml.j2",
    ("vllm", "tp+pp"): "vllm/lws.yaml.j2",
    ("rhoai", "tp"): "rhoai/llmisvc.yaml.j2",
    ("rhoai", "pp"): "rhoai/llmisvc-pp.yaml.j2",
    ("rhoai", "tp+pp"): "rhoai/llmisvc-pp.yaml.j2",
}

COMPONENT_KIND: dict[tuple[str, str], str] = {
    ("vllm", "tp"): "Deployment",
    ("vllm", "dp"): "Deployment",
    ("vllm", "tp+dp"): "Deployment",
    ("vllm", "pp"): "LeaderWorkerSet",
    ("vllm", "tp+pp"): "LeaderWorkerSet",
    ("rhoai", "tp"): "LLMInferenceService",
    ("rhoai", "pp"): "LLMInferenceService",
    ("rhoai", "tp+pp"): "LLMInferenceService",
}


def load_yaml_file(path: Path) -> dict:
    text = path.read_text()
    data = load_unique_yaml(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected YAML mapping")
    return data


def sanitize_name(recipe_id: str) -> str:
    """Convert recipe_id to a valid Kubernetes resource name."""
    return re.sub(r"[^a-z0-9-]", "-", recipe_id.lower())[:63]


def select_template(stack: str, mode: str) -> str:
    """Select template path from (platform.stack, parallelism.mode)."""
    key = (stack, mode)
    if key not in TEMPLATE_MAP:
        raise ValueError(
            f"No template for platform={stack}, mode={mode}. "
            f"Supported: {sorted(TEMPLATE_MAP.keys())}"
        )
    return TEMPLATE_MAP[key]



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

    tp = parallelism.get("tp", 1)
    pp = parallelism.get("pp", 1)
    dp = parallelism.get("dp", 1)

    resources = serving.get("resources", {})
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
    kind = COMPONENT_KIND.get((stack, mode), "Deployment")

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
        "tp": tp,
        "pp": pp,
        "dp": dp,
        "gpu_count": tp * dp,
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


def run_kustomize(base_manifest: str, recipe_dir: Path) -> str:
    """Run kustomize build with config/ as overlay over generated base."""
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir)
        base_file = work_dir / "base-manifest.yaml"
        base_file.write_text(base_manifest)

        config_dir = recipe_dir / "config"
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
                        if not str(src).startswith(str(resolved_config)):
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


def render_recipe(
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
    for platform_entry in platforms:
        if platform_entry.get("blocked"):
            continue
        stack = platform_entry.get("stack", "")
        version = platform_entry.get("version", "")

        pinned_ref = platform_entry.get("pinned_manifest")
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
                manifest_dir.mkdir(parents=True, exist_ok=True)
                output_path = manifest_dir / pinned_path.name
                output_path.write_bytes(pinned_bytes)
                print(f"  wrote {output_path.relative_to(repo)} (pinned)")
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

        if constraints:
            constraint_errors = validate_recipe_against_constraints(
                constraints, effective_recipe, model
            )
            if constraint_errors:
                errors.extend(f"{recipe_path}: [{stack}-{version}] {e}" for e in constraint_errors)
                continue

        try:
            template_path = select_template(stack, mode)
        except ValueError as exc:
            errors.append(str(exc))
            continue

        context = build_template_context(effective_recipe, model, constraints, platform_entry)
        rendered = render_template(template_path, context, repo / "templates")

        if effective_serving.get("config_overrides") is True:
            config_dir = recipe_path.parent / "config"
            kustomization = config_dir / "kustomization.yaml"
            if kustomization.is_file():
                try:
                    rendered = run_kustomize(rendered, recipe_path.parent)
                except (RuntimeError, OSError) as exc:
                    errors.append(f"{recipe_path}: kustomize failed: {exc}")
                    continue
            else:
                errors.append(
                    f"{recipe_path}: config_overrides is true but config/kustomization.yaml missing"
                )
                continue

        if not dry_run:
            stack_dir = f"{stack}-{version}" if version else stack
            manifest_dir = recipe_path.parent / "manifests" / stack_dir
            manifest_dir.mkdir(parents=True, exist_ok=True)
            kind = context["kind"]
            filename = f"{kind.lower()}.yaml"
            output_path = manifest_dir / filename
            output_path.write_text(rendered)
            print(f"  wrote {output_path.relative_to(repo)}")

        all_rendered.append(rendered)

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
