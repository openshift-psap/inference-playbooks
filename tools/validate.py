#!/usr/bin/env python3
"""Validate playbook schemas, references, and corrigible hardware profiles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from recipe_evidence import check_hardware_profiles, load_unique_yaml


SCHEMAS = {
    "recipe": "recipe.schema.json",
    "model": "model.schema.json",
    "hardware-profile": "hardware-profile.schema.json",
    "benchmark-run": "benchmark-run.schema.json",
    "benchmark-result": "benchmark-result.schema.json",
}


def load_yaml(path: Path) -> dict:
    """Load a YAML mapping from disk."""
    value = load_unique_yaml(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("expected a YAML object")
    return value


def load_schema(repo: Path, name: str) -> dict:
    """Load one named JSON Schema from the repository schema directory."""
    return json.loads((repo / "schema" / SCHEMAS[name]).read_text())


def validate_document(path: Path, value: dict, schema: dict) -> list[str]:
    """Return JSON Schema validation errors for one document."""
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"{path}: {error.json_path or '$'}: {error.message}"
        for error in sorted(validator.iter_errors(value), key=lambda error: str(error.json_path))
    ]


def contained_path(root: Path, relative_path: object) -> Path | None:
    """Resolve a path only when it stays within the supplied root directory."""
    if not isinstance(relative_path, str):
        return None
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def validate_recipe_layout(repo: Path, recipe_path: Path, recipe: dict, runs_by_path: dict[Path, dict]) -> list[str]:
    """Validate recipe layout, local references, and linked benchmark runs."""
    errors = []
    parts = recipe_path.relative_to(repo).parts
    # models/<model>/<stack>/<version>/recipes/<hardware>/<workload>/<mode>/recipe.yaml
    if len(parts) != 9 or parts[0] != "models" or parts[4] != "recipes":
        return [f"{recipe_path}: does not follow the model/stack/version/recipes layout"]
    model_id, stack, version = parts[1:4]
    hardware_selector, workload, deployment_mode = parts[5:8]
    if recipe.get("model_id") != model_id:
        errors.append(f"{recipe_path}: model_id must match its model directory")
    platform = recipe.get("platform", {})
    if not isinstance(platform, dict):
        platform = {}
    if platform.get("stack") != stack or platform.get("version") != version:
        errors.append(f"{recipe_path}: platform.stack/version must match its directory")
    if recipe.get("workload_profile") != workload:
        errors.append(f"{recipe_path}: workload_profile must match its directory")
    if recipe.get("deployment_mode") != deployment_mode:
        errors.append(f"{recipe_path}: deployment_mode must match its directory")
    profile_path = contained_path(repo, recipe.get("hardware_profile"))
    if not profile_path or not profile_path.is_file():
        errors.append(f"{recipe_path}: hardware_profile does not exist")
    else:
        try:
            profile = load_yaml(profile_path)
            if hardware_selector not in {profile_path.stem, profile.get("accelerator_key")}:
                errors.append(f"{recipe_path}: hardware selector must match the profile ID or accelerator_key")
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{recipe_path}: cannot load hardware_profile: {error}")
    run_references = recipe.get("benchmark_runs", [])
    if not isinstance(run_references, list):
        run_references = []
    for run_reference in run_references:
        run_path = contained_path(recipe_path.parent, run_reference)
        if not run_path:
            errors.append(f"{recipe_path}: benchmark run escapes the recipe directory: {run_reference}")
            continue
        if not run_path.is_file():
            errors.append(f"{recipe_path}: benchmark run does not exist: {run_reference}")
        elif run := runs_by_path.get(run_path):
            if run.get("recipe_id") != recipe.get("recipe_id"):
                errors.append(f"{recipe_path}: benchmark run recipe_id does not match the recipe")
            deployment = recipe.get("deployment", {})
            if isinstance(deployment, dict) and run.get("deployment_scope") != deployment.get("scope"):
                errors.append(f"{recipe_path}: benchmark run deployment_scope does not match deployment.scope")
        else:
            errors.append(f"{recipe_path}: benchmark run was not indexed: {run_reference}")
    deployment = recipe.get("deployment", {})
    manifests = deployment.get("manifests", []) if isinstance(deployment, dict) else []
    if not isinstance(manifests, list):
        manifests = []
    for manifest in manifests:
        manifest_path = contained_path(recipe_path.parent, manifest.get("path") if isinstance(manifest, dict) else None)
        if not manifest_path:
            errors.append(f"{recipe_path}: manifest escapes the recipe directory")
            continue
        if not manifest_path.is_file():
            errors.append(f"{recipe_path}: manifest does not exist: {manifest.get('path')}")
    return errors


def validate_benchmark_run(repo: Path, path: Path, run: dict) -> tuple[list[str], Path | None]:
    """Validate one run's profile revision and normalized result reference."""
    errors = []
    profile_path = contained_path(repo, run.get("hardware_profile"))
    if not profile_path or not profile_path.is_file():
        return [f"{path}: hardware_profile does not exist"], None
    try:
        profile = load_yaml(profile_path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        return [f"{path}: cannot load hardware_profile: {error}"], None
    profile_revision = profile.get("profile_revision")
    if isinstance(profile_revision, int) and run.get("hardware_profile_revision", 0) > profile_revision:
        errors.append(f"{path}: hardware_profile_revision is newer than the referenced profile")
    result_path = contained_path(path.parent, run.get("result"))
    if not result_path:
        errors.append(f"{path}: result escapes the run directory")
    elif not result_path.is_file():
        errors.append(f"{path}: normalized result does not exist: {run.get('result')}")
    return errors, result_path


def main() -> int:
    """Validate repository documents, references, and optional Git-diff rules."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--cached", action="store_true")
    parser.add_argument("--current", action="store_true", help="validate current files without a profile-diff comparison")
    arguments = parser.parse_args()
    if arguments.cached and arguments.current:
        parser.error("--cached and --current cannot be combined")
    if not arguments.cached and not arguments.current and (not arguments.base or not arguments.head):
        parser.error("pass --current, --cached, or both --base and --head")
    repo = arguments.repo.resolve()
    errors = []
    schemas = {name: load_schema(repo, name) for name in SCHEMAS}

    if not arguments.current:
        base = arguments.base or "HEAD"
        head = arguments.head or "HEAD"
        errors.extend(
            [] if check_hardware_profiles(repo, base, head, arguments.cached) == 0
            else ["hardware profile correction validation failed"]
        )
    for path in sorted((repo / "hardware-profiles").glob("*.yaml")):
        try:
            profile = load_yaml(path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{path}: cannot load YAML: {error}")
            continue
        errors.extend(validate_document(path, profile, schemas["hardware-profile"]))
    for path in sorted(repo.glob("models/**/model.yaml")):
        try:
            model = load_yaml(path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{path}: cannot load YAML: {error}")
            continue
        errors.extend(validate_document(path, model, schemas["model"]))
    runs_by_id: dict[str, tuple[Path, dict]] = {}
    runs_by_path: dict[Path, dict] = {}
    expected_results: dict[Path, tuple[Path, dict]] = {}
    for path in sorted(repo.glob("models/**/results/**/run.yaml")):
        try:
            run = load_yaml(path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{path}: cannot load YAML: {error}")
            continue
        document_errors = validate_document(path, run, schemas["benchmark-run"])
        errors.extend(document_errors)
        if document_errors:
            continue
        run_id = run.get("run_id")
        if isinstance(run_id, str):
            if run_id in runs_by_id:
                errors.append(f"{path}: duplicate run_id also used by {runs_by_id[run_id][0]}")
            else:
                runs_by_id[run_id] = (path, run)
        runs_by_path[path.resolve()] = run
        run_errors, result_path = validate_benchmark_run(repo, path, run)
        errors.extend(run_errors)
        if result_path:
            if result_path in expected_results:
                errors.append(f"{path}: normalized result is already referenced by {expected_results[result_path][0]}")
            else:
                expected_results[result_path] = (path, run)
    for path in sorted(repo.glob("models/**/recipes/*/*/*/recipe.yaml")):
        try:
            recipe = load_yaml(path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{path}: cannot load YAML: {error}")
            continue
        document_errors = validate_document(path, recipe, schemas["recipe"])
        errors.extend(document_errors)
        if document_errors:
            continue
        errors.extend(validate_recipe_layout(repo, path, recipe, runs_by_path))
    for path in sorted(repo.glob("models/**/results/**/result.json")):
        try:
            result = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"{path}: cannot load JSON: {error}")
            continue
        document_errors = validate_document(path, result, schemas["benchmark-result"])
        errors.extend(document_errors)
        if document_errors:
            continue
        expected = expected_results.get(path.resolve())
        if not expected:
            errors.append(f"{path}: normalized result is not referenced by a benchmark run")
        elif result.get("run_id") != expected[1].get("run_id"):
            errors.append(f"{path}: run_id does not match its referencing benchmark run")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Validated schemas, references, and hardware-profile corrections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
