#!/usr/bin/env python3
"""Validate playbook schemas, references, and corrigible hardware profiles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from recipe_evidence import check_hardware_profiles


SCHEMAS = {
    "recipe": "recipe.schema.json",
    "model": "model.schema.json",
    "hardware-profile": "hardware-profile.schema.json",
    "benchmark-run": "benchmark-run.schema.json",
    "benchmark-result": "benchmark-result.schema.json",
}


def load_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("expected a YAML object")
    return value


def load_schema(repo: Path, name: str) -> dict:
    return json.loads((repo / "schema" / SCHEMAS[name]).read_text())


def validate_document(path: Path, value: dict, schema: dict) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"{path}: {error.json_path or '$'}: {error.message}"
        for error in sorted(validator.iter_errors(value), key=lambda error: str(error.json_path))
    ]


def referenced_path(repo: Path, relative_path: str) -> Path | None:
    candidate = (repo / relative_path).resolve()
    try:
        candidate.relative_to(repo.resolve())
    except ValueError:
        return None
    return candidate


def validate_recipe_layout(repo: Path, recipe_path: Path, recipe: dict) -> list[str]:
    errors = []
    parts = recipe_path.relative_to(repo).parts
    # models/<model>/<stack>/<version>/recipes/<hardware>/<workload>/recipe.yaml
    try:
        recipes_index = parts.index("recipes")
        model_id, stack, version = parts[1], parts[2], parts[3]
        hardware_selector, workload = parts[recipes_index + 1], parts[recipes_index + 2]
    except (ValueError, IndexError):
        return [f"{recipe_path}: does not follow the model/stack/version/recipes layout"]
    if recipe.get("model_id") != model_id:
        errors.append(f"{recipe_path}: model_id must match its model directory")
    platform = recipe.get("platform", {})
    if platform.get("stack") != stack or platform.get("version") != version:
        errors.append(f"{recipe_path}: platform.stack/version must match its directory")
    if recipe.get("workload_profile") != workload:
        errors.append(f"{recipe_path}: workload_profile must match its directory")
    profile_path = referenced_path(repo, recipe.get("hardware_profile", ""))
    if not profile_path or not profile_path.is_file():
        errors.append(f"{recipe_path}: hardware_profile does not exist")
    else:
        profile = load_yaml(profile_path)
        if hardware_selector not in {profile_path.stem, profile.get("accelerator_key")}:
            errors.append(f"{recipe_path}: hardware selector must match the profile ID or accelerator_key")
    for run_reference in recipe.get("benchmark_runs", []):
        run_path = recipe_path.parent / run_reference
        if not run_path.is_file():
            errors.append(f"{recipe_path}: benchmark run does not exist: {run_reference}")
    for manifest in recipe.get("deployment", {}).get("manifests", []):
        manifest_path = recipe_path.parent / manifest["path"]
        if not manifest_path.is_file():
            errors.append(f"{recipe_path}: manifest does not exist: {manifest['path']}")
    return errors


def validate_benchmark_run(repo: Path, path: Path, run: dict) -> list[str]:
    errors = []
    profile_path = referenced_path(repo, run["hardware_profile"])
    if not profile_path or not profile_path.is_file():
        return [f"{path}: hardware_profile does not exist"]
    profile = load_yaml(profile_path)
    if run["hardware_profile_revision"] > profile["profile_revision"]:
        errors.append(f"{path}: hardware_profile_revision is newer than the referenced profile")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--cached", action="store_true")
    arguments = parser.parse_args()
    repo = arguments.repo.resolve()
    errors = []
    schemas = {name: load_schema(repo, name) for name in SCHEMAS}

    errors.extend(
        [] if check_hardware_profiles(repo, arguments.base, arguments.head, arguments.cached) == 0
        else ["hardware profile correction validation failed"]
    )
    for path in sorted((repo / "hardware-profiles").glob("*.yaml")):
        profile = load_yaml(path)
        errors.extend(validate_document(path, profile, schemas["hardware-profile"]))
    for path in sorted(repo.glob("models/**/model.yaml")):
        model = load_yaml(path)
        errors.extend(validate_document(path, model, schemas["model"]))
    for path in sorted(repo.glob("models/**/recipes/*/*/recipe.yaml")):
        recipe = load_yaml(path)
        errors.extend(validate_document(path, recipe, schemas["recipe"]))
        errors.extend(validate_recipe_layout(repo, path, recipe))
    for path in sorted(repo.glob("models/**/results/**/run.yaml")):
        run = load_yaml(path)
        errors.extend(validate_document(path, run, schemas["benchmark-run"]))
        errors.extend(validate_benchmark_run(repo, path, run))
    for path in sorted(repo.glob("models/**/results/**/result.json")):
        result = json.loads(path.read_text())
        errors.extend(validate_document(path, result, schemas["benchmark-result"]))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Validated schemas, references, and hardware-profile corrections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
