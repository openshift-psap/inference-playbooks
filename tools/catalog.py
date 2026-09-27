#!/usr/bin/env python3
"""Generate the model catalog from validated recipes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

# Import helpers from validate.py
from validate import load_yaml, contained_path

# Option universe constants from mockup-data.support.js
# These define what options are OFFERED in the UI (including unpublished/blocked)

HARDWARE = [
    {"id": "nvidia-h200-x8", "label": "H200", "recommended": True, "blocked_reason": None},
    {"id": "nvidia-b200-x8", "label": "B200", "recommended": False, "blocked_reason": "No validated NVFP4 recipe published yet."},
    {"id": "amd-mi355x-x8", "label": "MI355X", "recommended": False, "blocked_reason": "No validated MXFP4 recipe published yet."},
]

STACKS = [
    {"id": "vllm", "group": "Standalone", "label": "vLLM v0.28.0", "sub": "upstream image · tracks recipes.vllm.ai", "recommended": False, "blocked_reason": "Standalone upstream vLLM recipe has not been published for this model yet."},
    {"id": "rhaiis", "group": "Standalone", "label": "RHAIIS 3.6-fast2", "sub": "packaged Red Hat AI Inference Server", "recommended": False, "blocked_reason": "RHAIIS standalone recipe has not been published for this model yet."},
    {"id": "rhoai", "group": "Platform", "label": "RHOAI 3.5", "sub": "KServe LLMInferenceService", "recommended": True, "blocked_reason": None},
    {"id": "llmd", "group": "Platform", "label": "llm-d", "sub": "prefix-aware routing · P/D · KV tiering", "recommended": False, "blocked_reason": "llm-d is planned: prefix-cache-aware routing, prefill/decode disaggregation and KV tiering. No llm-d recipe exists yet."},
]

TOPOLOGY = [
    {"id": "single", "label": "single-node", "blocked_reason": None},
    {"id": "multi", "label": "multi-node", "blocked_reason": "No validated multi-node recipe published for this hardware yet."},
    {"id": "pd", "label": "multi-node · P/D disaggregated", "blocked_reason": "Large-scale P/D disaggregation recipe not published yet."},
]

# Workload profile display labels
WORKLOAD_LABELS = {
    "guidellm-8k1k": {"label": "GuideLLM 8K/1K", "sub": "8K in · 1K out"},
    "aiperf-agentx-128k": {"label": "AIPerf AgentX 128K", "sub": "131K · tools · MTP"},
}

# Default color palette for icon_bg (deterministic hash-based selection)
DEFAULT_COLORS = [
    "#4B2E83", "#76B900", "#0066CC", "#D32F2F", "#F57C00",
    "#388E3C", "#7B1FA2", "#0097A7", "#C2185B", "#5E35B1",
]


def hash_color(model_id: str) -> str:
    """Deterministic color selection from palette based on model_id.

    Uses md5 rather than the builtin hash(), which is salted per process and
    would produce non-deterministic output across runs.
    """
    return DEFAULT_COLORS[int(hashlib.md5(model_id.encode()).hexdigest(), 16) % len(DEFAULT_COLORS)]


def discover(repo: Path) -> tuple[dict[str, dict], dict[Path, dict], dict[str, dict], dict[str, dict]]:
    """Discover models, recipes, hardware profiles, and runs/results."""
    models = {}
    recipes = {}
    hw_profiles = {}
    runs = {}
    results = {}

    # Load models
    for model_path in repo.glob("models/*/model.yaml"):
        try:
            model = load_yaml(model_path)
            model_id = model.get("model_id")
            if model_id:
                models[model_id] = model
        except (OSError, ValueError, yaml.YAMLError):
            continue

    # Load hardware profiles
    for hw_path in (repo / "hardware-profiles").glob("*.yaml"):
        try:
            profile = load_yaml(hw_path)
            accel_key = profile.get("accelerator_key")
            if accel_key:
                hw_profiles[accel_key] = profile
                hw_profiles[hw_path.stem] = profile  # also index by stem
        except (OSError, ValueError, yaml.YAMLError):
            continue

    # Discover recipes (models/**/recipes/*/*/*/recipe.yaml)
    for recipe_path in repo.glob("models/**/recipes/*/*/*/recipe.yaml"):
        try:
            recipe = load_yaml(recipe_path)
            recipes[recipe_path] = recipe
        except (OSError, ValueError, yaml.YAMLError):
            continue

    # Discover runs (models/**/results/*/run.yaml)
    for run_path in repo.glob("models/**/results/*/run.yaml"):
        try:
            run = load_yaml(run_path)
            runs[run_path] = run
        except (OSError, ValueError, yaml.YAMLError):
            continue

    # Discover results (models/**/results/*/result.json)
    for result_path in repo.glob("models/**/results/*/result.json"):
        try:
            result = json.loads(result_path.read_text())
            results[result_path] = result
        except (OSError, json.JSONDecodeError):
            continue

    return models, recipes, hw_profiles, runs, results


def blocked_view(reason: str) -> dict:
    """Generate a blocked/pending view."""
    return {
        "blocked": True,
        "reason": reason,
        "header_specs": [{"k": "Status", "v": "Recipe pending"}],
    }


def render_view(recipe: dict, recipe_path: Path, hw_profile: dict, repo: Path) -> dict:
    """Render a VIEW dict from a recipe's display block."""
    display = recipe.get("display", {})

    # Header specs
    header_specs = display.get("header_specs", [])

    # Configure section
    configure = {}

    # Image table
    configure["image_table"] = display.get("image_table", [])

    # Artifact table
    configure["artifact_table"] = display.get("artifact_table", [])

    # Flags
    vllm_args = display.get("vllm_args", [])
    configure["flags_rows"] = [
        {"flag": arg.get("flag", ""), "value": arg.get("value", ""), "why": arg.get("why", "")}
        for arg in vllm_args
    ]
    configure["flags_lede"] = display.get("flags_lede", "")
    configure["arg_note"] = display.get("arg_note")

    # vllm_serve command composition
    vllm_serve_lines = []
    for arg in vllm_args:
        flag = arg.get("flag", "")
        value = arg.get("value", "")
        if value:
            # Quote JSON values
            if value.startswith("{"):
                vllm_serve_lines.append(f"  {flag} '{value}'")
            else:
                vllm_serve_lines.append(f"  {flag} {value}")
        else:
            vllm_serve_lines.append(f"  {flag}")

    checkpoint = next((spec["v"] for spec in header_specs if spec.get("k") == "Checkpoint"), "")
    configure["vllm_serve"] = f"vllm serve {checkpoint} \\\n" + " \\\n".join(vllm_serve_lines) if vllm_serve_lines else ""

    # Manifest
    manifests = recipe.get("deployment", {}).get("manifests", [])
    if manifests:
        manifest_path = contained_path(recipe_path.parent, manifests[0].get("path", ""))
        if manifest_path and manifest_path.is_file():
            configure["manifest"] = {
                "name": manifest_path.name,
                "body": manifest_path.read_text(),
            }
        else:
            configure["manifest"] = {"name": "", "body": ""}
    else:
        configure["manifest"] = {"name": "", "body": ""}

    # Quick start (placeholder - recipes don't have this in display yet)
    quick_start = display.get("quick_start", {})

    # Benchmark
    benchmark_data = display.get("benchmark", {})
    benchmark = {
        "provenance": benchmark_data.get("provenance", ""),
        "harness_lede": benchmark_data.get("harness_lede", ""),
        "harness_drawer": benchmark_data.get("harness_drawer", {}),
        "rows": benchmark_data.get("rows", []),
        "notes": benchmark_data.get("notes", []),
        "issues": benchmark_data.get("issues", []),
    }

    # Disconnected
    disconnected = display.get("disconnected", {})

    # Advanced
    advanced = display.get("advanced", {})

    # Files (placeholder)
    files = {}

    return {
        "blocked": False,
        "header_specs": header_specs,
        "quick_start": quick_start,
        "configure": configure,
        "benchmark": benchmark,
        "disconnected": disconnected,
        "advanced": advanced,
        "files": files,
    }


def build_catalog(repo: Path) -> dict:
    """Build the full catalog from discovered resources."""
    models_data, recipes_data, hw_profiles, runs_data, results_data = discover(repo)

    catalog_models = []

    for model_id, model in sorted(models_data.items()):
        # Presentation defaults
        presentation = model.get("presentation", {})
        icon_letter = presentation.get("icon_letter")
        if not icon_letter:
            # First alnum of name
            name = model.get("name", "")
            icon_letter = next((c for c in name if c.isalnum()), "?")

        icon_bg = presentation.get("icon_bg")
        if not icon_bg:
            icon_bg = hash_color(model_id)

        provider = presentation.get("provider")
        if not provider:
            hf_id = model.get("huggingface_id", "")
            if "/" in hf_id:
                provider = hf_id.split("/")[0]
            else:
                provider = ""

        tags = presentation.get("tags", [])

        # Find all recipes for this model
        model_recipes = {}
        for recipe_path, recipe in recipes_data.items():
            if recipe.get("model_id") == model_id:
                # Parse recipe path to extract components
                parts = recipe_path.relative_to(repo).parts
                # models/<model>/<stack>/<version>/recipes/<hardware>/<workload>/<mode>/recipe.yaml
                if len(parts) >= 9 and parts[0] == "models" and parts[4] == "recipes":
                    stack = recipe.get("platform", {}).get("stack", "")
                    workload = recipe.get("workload_profile", "")

                    # Get hardware accelerator_key
                    hw_path_str = recipe.get("hardware_profile", "")
                    hw_path = contained_path(repo, hw_path_str)
                    hw_key = None
                    if hw_path and hw_path.is_file():
                        try:
                            hw_profile = load_yaml(hw_path)
                            hw_key = hw_profile.get("accelerator_key", hw_path.stem)
                        except (OSError, ValueError, yaml.YAMLError):
                            hw_key = hw_path.stem

                    # Determine topology from deployment.scope
                    scope = recipe.get("deployment", {}).get("scope", "")
                    topo = "single" if scope == "single-node" else "multi" if scope == "multi-node" else "single"

                    if hw_key and stack and workload:
                        view_key = f"{stack}|{hw_key}|{topo}|{workload}"
                        model_recipes[view_key] = (recipe, recipe_path, hw_profile if hw_path else {})

        # Determine if model is validated (any recipe has maturity in {validated, production})
        validated = any(
            recipe.get("maturity") in {"validated", "production"}
            for recipe, _, _ in model_recipes.values()
        )

        # Build selectors from option universe
        selectors = {
            "stacks": [
                {
                    "id": s["id"],
                    "group": s["group"],
                    "label": s["label"],
                    "sub": s["sub"],
                    "blocked": s["blocked_reason"] is not None,
                    "status": None,
                    "reason": s["blocked_reason"],
                }
                for s in STACKS
            ],
            "hardware": [
                {
                    "id": h["id"],
                    "label": h["label"],
                    "recommended": h["recommended"],
                }
                for h in HARDWARE
            ],
            "topology": [
                {
                    "id": t["id"],
                    "label": t["label"],
                }
                for t in TOPOLOGY
            ],
            "profiles": [
                {
                    "id": wid,
                    "label": winfo["label"],
                    "sub": winfo["sub"],
                }
                for wid, winfo in WORKLOAD_LABELS.items()
            ],
        }

        # Default combo (must be non-blocked)
        default = {
            "stack": "rhoai",
            "hw": "nvidia-h200-x8",
            "topo": "single",
            "profile": "guidellm-8k1k",
        }

        # Build views for all combos in the option universe
        views = {}
        for stack in STACKS:
            for hw in HARDWARE:
                for topo in TOPOLOGY:
                    for profile_id in WORKLOAD_LABELS.keys():
                        view_key = f"{stack['id']}|{hw['id']}|{topo['id']}|{profile_id}"

                        # Check if a recipe exists for this combo
                        if view_key in model_recipes:
                            recipe, recipe_path, hw_profile = model_recipes[view_key]
                            views[view_key] = render_view(recipe, recipe_path, hw_profile, repo)
                        else:
                            # Blocked view - determine reason
                            reason = None
                            if stack["blocked_reason"]:
                                reason = stack["blocked_reason"]
                            elif hw["blocked_reason"]:
                                reason = hw["blocked_reason"]
                            elif topo["blocked_reason"]:
                                reason = topo["blocked_reason"]
                            else:
                                # Default reason
                                stack_label = stack["label"]
                                hw_label = hw["label"]
                                topo_label = topo["label"]
                                profile_label = WORKLOAD_LABELS[profile_id]["label"]
                                reason = f"No published recipe for {stack_label} · {hw_label} · {topo_label} · {profile_label} yet."

                            views[view_key] = blocked_view(reason)

        catalog_models.append({
            "id": model_id,
            "name": model.get("name", ""),
            "provider": provider,
            "icon_bg": icon_bg,
            "icon_letter": icon_letter,
            "validated": validated,
            "tags": tags,
            "selectors": selectors,
            "default": default,
            "views": views,
        })

    return {
        "schema": 1,
        "models": catalog_models,
    }


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Generate model catalog")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository root")
    parser.add_argument("--out", type=Path, default=Path("catalog/catalog.json"), help="Output path")
    parser.add_argument("--check", action="store_true", help="Check for drift vs committed file")
    args = parser.parse_args()

    catalog = build_catalog(args.repo)

    # Deterministic JSON output
    output = json.dumps(catalog, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    if args.check:
        # Compare against existing file
        if args.out.is_file():
            existing = args.out.read_text()
            if existing != output:
                print(f"ERROR: Catalog has drifted from {args.out}", file=sys.stderr)
                print("Run: python3 tools/catalog.py --out catalog/catalog.json", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"ERROR: {args.out} does not exist", file=sys.stderr)
            sys.exit(1)
    else:
        # Write output
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output)


if __name__ == "__main__":
    main()
