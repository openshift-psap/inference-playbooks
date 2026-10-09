#!/usr/bin/env python3
"""Validate playbook schemas, references, and corrigible hardware profiles."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from constraints import load_constraints, validate_recipe_against_constraints
from recipe_evidence import check_hardware_profiles, load_unique_yaml, load_unique_yaml_all
from engine_versions import baseline_images, engine_errors, load_engine_index, resolve_engine
from jsonschema.exceptions import ValidationError


SCHEMAS = {
    "recipe": "recipe.schema.json",
    "recipe-notes": "recipe-notes.schema.json",
    "model": "model.schema.json",
    "hardware-profile": "hardware-profile.schema.json",
    "benchmark-run": "benchmark-run.schema.json",
    "benchmark-result": "benchmark-result.schema.json",
    "platform-overrides": "platform-overrides.schema.json",
}

PARALLELISM_FLAGS = frozenset({
    "--tensor-parallel-size",
    "--pipeline-parallel-size",
    "--data-parallel-size",
    "--num-scheduler-steps",
})


def load_yaml(path: Path) -> dict:
    """Load a YAML mapping from disk."""
    value = load_unique_yaml(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("expected a YAML object")
    return value


def load_schema(repo: Path, name: str) -> dict:
    """Load one named JSON Schema from the repository schema directory."""
    return json.loads((repo / "schema" / SCHEMAS[name]).read_text())


def load_schema_registry(repo: Path) -> Registry:
    """Register repository schemas locally so external $refs never need network access."""
    resources = []
    for path in sorted((repo / "schema").glob("*.schema.json")):
        schema = json.loads(path.read_text())
        Draft202012Validator.check_schema(schema)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def validate_document(path: Path, value: dict, schema: dict, registry: Registry | None = None) -> list[str]:
    """Return JSON Schema validation errors for one document."""
    validator = Draft202012Validator(schema, format_checker=FormatChecker(), registry=registry or Registry())
    return [
        f"{path}: {error.json_path or '$'}: {error.message}"
        for error in sorted(validator.iter_errors(value), key=lambda error: str(error.json_path))
    ]


def contained_path(root: Path, relative_path: object) -> Path | None:
    """Resolve a path only when it stays within the supplied root directory."""
    if not isinstance(relative_path, str):
        return None
    try:
        candidate = (root / relative_path).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate



def validate_raw_manifest_intake(repo: Path, recipe_schema: dict, require_converted: bool = False) -> list[str]:
    """Check initial-release raw submissions without requiring a recipe yet."""
    errors = []
    properties = recipe_schema["properties"]
    for directory in sorted(repo.glob("models/**/raw-manifest")):
        if not directory.is_dir():
            continue
        parts = directory.relative_to(repo).parts
        if (
            len(parts) != 5 or parts[0] != "models" or parts[2] != "recipes"
            or parts[4] != "raw-manifest"
            or not re.fullmatch(properties["model_id"]["pattern"], parts[1])
            or not re.fullmatch(properties["recipe_id"]["pattern"], parts[3])
        ):
            errors.append(f"{directory}: raw-manifest must be under models/<model>/recipes/<recipe>/raw-manifest")
            continue
        if require_converted and not (directory.parent / "recipe.yaml").is_file():
            errors.append(f"{directory}: maintainer conversion required before merge: recipe.yaml is missing")
        manifest_count = 0
        for path in sorted(directory.rglob("*")):
            if path.is_dir():
                continue
            if not path.resolve().is_relative_to(repo.resolve()):
                errors.append(f"{path}: raw-manifest file escapes the repository")
                continue
            if path.suffix.lower() not in {".yaml", ".yml", ".json"}:
                if path.name != "README.md":
                    errors.append(f"{path}: raw-manifest accepts YAML/JSON and optional README.md only")
                continue
            manifest_count += 1
            try:
                text = path.read_text()
                if path.suffix.lower() == ".json":
                    documents = [json.loads(text)]
                    if not isinstance(documents[0], dict):
                        raise ValueError("JSON file must contain an object")
                else:
                    documents = [document for document in load_unique_yaml_all(text) if document is not None]
            except (OSError, UnicodeError, ValueError, yaml.YAMLError, json.JSONDecodeError) as error:
                errors.append(f"{path}: cannot parse raw manifest: {error}")
                continue
            if not documents or any(not isinstance(document, dict) for document in documents):
                errors.append(f"{path}: raw manifest must contain at least one YAML object document")
        if not manifest_count:
            errors.append(f"{directory}: raw-manifest needs at least one YAML or JSON file")
    return errors


def resolve_spec_source(repo: Path, recipe_path: Path, recipe: dict, reference: str) -> bool:
    """Check a reader spec's file and JSON Pointer without copying its value."""
    source_name, separator, pointer = reference.partition("#")
    if not separator or not pointer.startswith("/"):
        return False
    if source_name == "recipe.yaml":
        value = recipe
    else:
        if source_name == "hardware_profile":
            source_path = contained_path(repo, recipe.get("hardware_profile"))
        elif source_name == "model.yaml":
            source_path = repo / "models" / recipe.get("model_id", "") / "model.yaml"
        elif source_name.startswith("config/"):
            source_path = contained_path(recipe_path.parent, source_name)
            if source_path and not source_path.is_relative_to((recipe_path.parent / "config").resolve()):
                return False
        else:
            return False
        if not source_path or not source_path.is_file():
            return False
        try:
            value = load_yaml(source_path)
        except (OSError, ValueError, yaml.YAMLError):
            return False
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False
    return True


def validate_recipe_notes(repo: Path, recipe_path: Path, recipe: dict, schema: dict, registry: Registry) -> list[str]:
    """Validate the optional reader notes and their local references."""
    reference = recipe.get("notes")
    if reference is None:
        return []
    notes_path = contained_path(recipe_path.parent, reference)
    if not notes_path or not notes_path.is_relative_to((recipe_path.parent / "guides").resolve()):
        return [f"{recipe_path}: notes path escapes guides/: {reference}"]
    if not notes_path.is_file():
        return [f"{recipe_path}: notes file does not exist: {reference}"]
    try:
        notes = load_yaml(notes_path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        return [f"{recipe_path}: cannot load notes: {error}"]
    if isinstance(notes.get("features"), list):
        return [f"{notes_path}: features must be an object with known keys"
                " (e.g. tool_calling, prefix_caching, reasoning_parser),"
                " not a list; see schema/examples/recipe-notes.yaml"]
    errors = validate_document(notes_path, notes, schema, registry)
    if errors:
        return errors
    for spec in notes.get("profile", {}).get("specs", []):
        if not resolve_spec_source(repo, recipe_path, recipe, spec["source"]):
            errors.append(f"{notes_path}: spec source does not resolve: {spec['source']}")
    for decision in notes.get("decisions", []):
        for evidence in decision.get("evidence", []):
            evidence_path = contained_path(recipe_path.parent, evidence)
            if not evidence_path or not evidence_path.is_relative_to((recipe_path.parent / "results").resolve()) or not evidence_path.is_file():
                errors.append(f"{notes_path}: decision evidence does not exist: {evidence}")
    for image_choice in notes.get("image_choices", []):
        if recipe.get("maturity") in {"validated", "production"} and image_choice["status"]["state"] == "needs-verification":
            errors.append(f"{notes_path}: validated recipe cannot recommend an unverified image")
    return errors


def validate_v4_recipe(repo: Path, recipe_path: Path, recipe: dict, registry: Registry | None = None) -> list[str]:
    """Validate v4-specific serving block constraints beyond JSON Schema."""
    errors: list[str] = []
    serving = recipe.get("serving", {})
    if not isinstance(serving, dict):
        return errors

    parallelism = serving.get("parallelism", {})
    if isinstance(parallelism, dict):
        mode = parallelism.get("mode")
        pp = parallelism.get("pp", 1)
        dp = parallelism.get("dp", 1)
        tp = parallelism.get("tp", 1)
        if mode == "tp" and (pp > 1 or dp > 1):
            extra = []
            if pp > 1:
                extra.append(f"pp={pp}")
            if dp > 1:
                extra.append(f"dp={dp}")
            errors.append(
                f"{recipe_path}: parallelism mode 'tp' is incompatible with {', '.join(extra)}"
            )
        if mode == "pp" and (tp > 1 or dp > 1):
            extra = []
            if tp > 1:
                extra.append(f"tp={tp}")
            if dp > 1:
                extra.append(f"dp={dp}")
            errors.append(
                f"{recipe_path}: parallelism mode 'pp' is incompatible with {', '.join(extra)}"
            )
        if mode == "dp" and (tp > 1 or pp > 1):
            extra = []
            if tp > 1:
                extra.append(f"tp={tp}")
            if pp > 1:
                extra.append(f"pp={pp}")
            errors.append(
                f"{recipe_path}: parallelism mode 'dp' is incompatible with {', '.join(extra)}"
            )
        if mode == "tp+pp" and dp > 1:
            errors.append(
                f"{recipe_path}: parallelism mode 'tp+pp' is incompatible with dp={dp}"
            )
        if mode == "tp+dp" and pp > 1:
            errors.append(
                f"{recipe_path}: parallelism mode 'tp+dp' is incompatible with pp={pp}"
            )

    args = serving.get("args", [])
    if isinstance(args, list):
        seen_flags: set[str] = set()
        for arg in args:
            if not isinstance(arg, dict):
                continue
            flag = arg.get("flag")
            if not isinstance(flag, str):
                continue
            if flag in PARALLELISM_FLAGS:
                errors.append(
                    f"{recipe_path}: serving.args must not contain parallelism flag {flag}; "
                    "use serving.parallelism instead"
                )
            if flag in seen_flags:
                errors.append(f"{recipe_path}: duplicate flag in serving.args: {flag}")
            seen_flags.add(flag)

    if serving.get("config_overrides") is True:
        kustomization = recipe_path.parent / "config" / "kustomization.yaml"
        if not kustomization.is_file():
            errors.append(
                f"{recipe_path}: config_overrides is true but config/kustomization.yaml does not exist"
            )

    router = serving.get("router", {})
    if isinstance(router, dict) and router.get("values"):
        values_path = contained_path(recipe_path.parent, router["values"])
        if not values_path or not values_path.is_file():
            errors.append(
                f"{recipe_path}: serving.router.values file does not exist: {router['values']}"
            )

    # --- Role block validation (Phase 3) ---
    errors.extend(_validate_role_blocks(recipe_path, serving))

    # --- Platform override file validation ---
    errors.extend(_validate_platform_overrides(repo, recipe_path, recipe, registry))

    return errors


def _extract_flags(arg_list: list | None) -> list[str]:
    """Extract flag strings from an arg list, skipping non-dict entries."""
    if not isinstance(arg_list, list):
        return []
    return [
        a["flag"] for a in arg_list
        if isinstance(a, dict) and isinstance(a.get("flag"), str)
    ]


def _validate_role_blocks(recipe_path: Path, serving: dict) -> list[str]:
    """Validate decode/prefill role blocks within a serving block."""
    errors: list[str] = []
    universal_flags = set(_extract_flags(serving.get("args")))

    for role_name in ("decode", "prefill"):
        role = serving.get(role_name)
        if role is None:
            continue
        if not isinstance(role, dict):
            continue

        if role_name == "prefill":
            router = serving.get("router", {})
            is_pd = isinstance(router, dict) and router.get("strategy") in (
                "prefix", "disaggregated", "prefill-decode",
            )
            if not is_pd:
                errors.append(
                    f"{recipe_path}: serving.prefill requires a P/D router strategy "
                    "(prefix, disaggregated, or prefill-decode)"
                )

        exclude_flags: set[str] = set()
        for entry in role.get("exclude", []):
            if not isinstance(entry, dict):
                continue
            flag = entry.get("flag")
            if not isinstance(flag, str):
                continue
            if flag not in universal_flags:
                errors.append(
                    f"{recipe_path}: serving.{role_name}.exclude references "
                    f"flag {flag} which does not exist in serving.args"
                )
            exclude_flags.add(flag)

        role_args_flags: set[str] = set()
        for flag in _extract_flags(role.get("args")):
            if flag in role_args_flags:
                errors.append(
                    f"{recipe_path}: duplicate flag in serving.{role_name}.args: {flag}"
                )
            role_args_flags.add(flag)

        for flag in role_args_flags:
            if flag in universal_flags and flag not in exclude_flags:
                errors.append(
                    f"{recipe_path}: flag {flag} appears in both serving.args and "
                    f"serving.{role_name}.args without an exclude entry"
                )

        leader_flags: set[str] = set()
        for flag in _extract_flags(role.get("leader_args")):
            if flag in leader_flags:
                errors.append(
                    f"{recipe_path}: duplicate flag in serving.{role_name}.leader_args: {flag}"
                )
            leader_flags.add(flag)

        worker_flags: set[str] = set()
        for flag in _extract_flags(role.get("worker_args")):
            if flag in worker_flags:
                errors.append(
                    f"{recipe_path}: duplicate flag in serving.{role_name}.worker_args: {flag}"
                )
            worker_flags.add(flag)

        overlap = leader_flags & worker_flags
        for flag in sorted(overlap):
            errors.append(
                f"{recipe_path}: flag {flag} appears in both "
                f"serving.{role_name}.leader_args and serving.{role_name}.worker_args"
            )

    return errors


def _validate_platform_overrides(repo: Path, recipe_path: Path, recipe: dict, registry: Registry | None = None) -> list[str]:
    """Validate platform override files referenced by platforms entries."""
    errors: list[str] = []
    platforms = recipe.get("platforms", [])
    if not isinstance(platforms, list):
        return errors
    for entry in platforms:
        if not isinstance(entry, dict):
            continue
        if entry.get("blocked"):
            stack = entry.get("stack", "?")
            version = entry.get("version", "?")
            reason = entry.get("reason", "no reason given")
            print(f"  info: {recipe_path}: platform {stack}-{version} blocked: {reason}")
        pinned_ref = entry.get("pinned_manifest")
        if isinstance(pinned_ref, str):
            pinned_path = contained_path(recipe_path.parent, pinned_ref)
            if not pinned_path:
                errors.append(f"{recipe_path}: pinned_manifest escapes recipe directory: {pinned_ref}")
            elif not pinned_path.is_file():
                errors.append(f"{recipe_path}: pinned_manifest does not exist: {pinned_ref}")
            else:
                stack = entry.get("stack", "?")
                version = entry.get("version", "?")
                reason = entry.get("pinned_reason", "no reason given")
                print(f"  info: {recipe_path}: platform {stack}-{version} pinned: {reason}")
        overrides_ref = entry.get("overrides")
        if not isinstance(overrides_ref, str):
            continue
        overrides_path = contained_path(recipe_path.parent, overrides_ref)
        if not overrides_path:
            errors.append(f"{recipe_path}: platform override escapes recipe directory: {overrides_ref}")
            continue
        if not overrides_path.is_file():
            errors.append(f"{recipe_path}: platform override file does not exist: {overrides_ref}")
            continue
        try:
            overrides = load_yaml(overrides_path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{recipe_path}: cannot load platform override {overrides_ref}: {error}")
            continue
        override_schema_path = repo / "schema" / "platform-overrides.schema.json"
        if override_schema_path.is_file():
            try:
                override_schema = json.loads(override_schema_path.read_text())
                effective_registry = registry or load_schema_registry(repo)
                override_errors = validate_document(overrides_path, overrides, override_schema, effective_registry)
                errors.extend(override_errors)
            except (OSError, json.JSONDecodeError) as error:
                errors.append(f"{recipe_path}: cannot load platform override schema: {error}")
    return errors


def resolve_role_args(serving: dict, role_name: str, position: str = "leader") -> list[dict]:
    """Resolve final arg list for a role+position.

    1. Start with serving.args (universal)
    2. Remove entries listed in role.exclude
    3. Add role.args (role-specific)
    4. Add role.leader_args or role.worker_args (position-specific)

    Returns ordered list of {flag, value?, required, why} dicts.
    """
    universal = list(serving.get("args", []))

    role = serving.get(role_name)
    if not isinstance(role, dict):
        return universal

    exclude_flags = {
        entry["flag"]
        for entry in role.get("exclude", [])
        if isinstance(entry, dict) and isinstance(entry.get("flag"), str)
    }
    result = [arg for arg in universal if arg.get("flag") not in exclude_flags]

    role_args = role.get("args", [])
    if isinstance(role_args, list):
        result.extend(role_args)

    position_key = f"{position}_args"
    position_args = role.get(position_key, [])
    if isinstance(position_args, list):
        result.extend(position_args)

    return result


def validate_recipe_layout(repo: Path, recipe_path: Path, recipe: dict, runs_by_path: dict[Path, dict]) -> list[str]:
    """Validate recipe layout, local references, and linked benchmark runs."""
    errors = []
    parts = recipe_path.relative_to(repo).parts
    if len(parts) != 5 or parts[0] != "models" or parts[2] != "recipes" or parts[4] != "recipe.yaml":
        return [f"{recipe_path}: does not follow models/<model-id>/recipes/<recipe-id>/recipe.yaml layout"]
    model_id = parts[1]
    recipe_id_dir = parts[3]
    if recipe.get("model_id") != model_id:
        errors.append(f"{recipe_path}: model_id '{recipe.get('model_id')}' must match its model directory '{model_id}'")
    model_yaml = repo / "models" / model_id / "model.yaml"
    if not model_yaml.is_file():
        errors.append(f"{recipe_path}: models/{model_id}/model.yaml does not exist")
    profile_path = contained_path(repo, recipe.get("hardware_profile"))
    if not profile_path or not profile_path.is_file():
        errors.append(f"{recipe_path}: hardware_profile does not exist: {recipe.get('hardware_profile')}")
    else:
        try:
            load_yaml(profile_path)
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
    if recipe.get("maturity") in {"validated", "production"} and deployment.get("status", {}).get("state") == "needs-verification":
        errors.append(f"{recipe_path}: validated recipe cannot have an unverified deployment")
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
    for i, artifact in enumerate(run.get("artifacts", [])):
        if not isinstance(artifact, dict):
            continue
        artifact_path = artifact.get("path")
        artifact_uri = artifact.get("uri")
        if artifact_path:
            resolved = contained_path(path.parent, artifact_path)
            if not resolved:
                errors.append(f"{path}: artifact[{i}] path escapes the run directory: {artifact_path}")
            elif not resolved.is_file():
                errors.append(f"{path}: artifact[{i}] local file does not exist: {artifact_path}")
        elif artifact_uri:
            if not artifact.get("checksum"):
                errors.append(f"{path}: artifact[{i}] external URI requires a checksum: {artifact_uri}")
    return errors, result_path


def main() -> int:
    """Validate repository documents, references, and optional Git-diff rules."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--cached", action="store_true")
    parser.add_argument("--current", action="store_true", help="validate current files without a profile-diff comparison")
    parser.add_argument("--require-converted-raw", action="store_true", help="fail if a raw-manifest submission lacks recipe.yaml")
    arguments = parser.parse_args()
    if arguments.cached and arguments.current:
        parser.error("--cached and --current cannot be combined")
    if not arguments.cached and not arguments.current and (not arguments.base or not arguments.head):
        parser.error("pass --current, --cached, or both --base and --head")
    repo = arguments.repo.resolve()
    errors = []
    schemas = {name: load_schema(repo, name) for name in SCHEMAS}
    registry = load_schema_registry(repo)
    try:
        engine_index = load_engine_index(repo)
    except (OSError, ValueError, yaml.YAMLError, ValidationError) as error:
        errors.append(f"engine-versions/index.yaml: {error}")
        engine_index = {"releases": [], "variants": []}
    errors.extend(validate_raw_manifest_intake(repo, schemas["recipe"], arguments.require_converted_raw))

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
        errors.extend(validate_document(path, profile, schemas["hardware-profile"], registry))
    for path in sorted(repo.glob("models/**/model.yaml")):
        try:
            model = load_yaml(path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{path}: cannot load YAML: {error}")
            continue
        errors.extend(validate_document(path, model, schemas["model"], registry))
    runs_by_id: dict[str, tuple[Path, dict]] = {}
    runs_by_path: dict[Path, dict] = {}
    expected_results: dict[Path, tuple[Path, dict]] = {}
    for path in sorted(repo.glob("models/**/results/**/run.yaml")):
        try:
            run = load_yaml(path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{path}: cannot load YAML: {error}")
            continue
        document_errors = validate_document(path, run, schemas["benchmark-run"], registry)
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
    try:
        flag_constraints = load_constraints(repo)
    except (OSError, ValueError, yaml.YAMLError) as error:
        errors.append(f"flag-constraints.yaml: {error}")
        flag_constraints = None

    models_by_id: dict[str, dict] = {}
    for path in sorted(repo.glob("models/*/model.yaml")):
        try:
            model_data = load_yaml(path)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        mid = model_data.get("model_id")
        if isinstance(mid, str):
            models_by_id[mid] = model_data

    recipe_ids: dict[str, Path] = {}
    for path in sorted(repo.glob("models/*/recipes/*/recipe.yaml")):
        try:
            recipe = load_yaml(path)
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{path}: cannot load YAML: {error}")
            continue
        document_errors = validate_document(path, recipe, schemas["recipe"], registry)
        errors.extend(document_errors)
        if document_errors:
            continue
        rid = recipe.get("recipe_id")
        if isinstance(rid, str):
            if rid in recipe_ids:
                errors.append(f"{path}: duplicate recipe_id '{rid}' also used by {recipe_ids[rid]}")
            else:
                recipe_ids[rid] = path
        errors.extend(validate_v4_recipe(repo, path, recipe, registry))
        errors.extend(validate_recipe_layout(repo, path, recipe, runs_by_path))
        errors.extend(validate_recipe_notes(repo, path, recipe, schemas["recipe-notes"], registry))
        previous_images = None if arguments.current else baseline_images(repo, path, arguments.base or "HEAD")
        for platform in recipe["platforms"]:
            if platform.get("blocked"):
                continue
            override_path = contained_path(path.parent, platform["overrides"])
            if not override_path or not override_path.is_file():
                continue  # The platform validator reports missing/escaping paths.
            try:
                overrides = load_yaml(override_path)
                override_errors = validate_document(override_path, overrides, schemas["platform-overrides"], registry)
                if override_errors:
                    continue
                resolution = resolve_engine(recipe["serving"], overrides, platform, engine_index)
                require_metadata = previous_images is not None and previous_images.get((platform["stack"], platform["version"])) != resolution["image"]
                engine_violations = engine_errors(recipe["serving"], overrides, platform, engine_index, require_metadata)
                errors.extend(f"{path}: [{platform['stack']}-{platform['version']}] {violation}" for violation in engine_violations)
                if resolution["state"] == "unknown" and not engine_violations:
                    message = f"{path}: [{platform['stack']}-{platform['version']}] {resolution['reason']}"
                    print(f"Warning: {message}; legacy engine remains unknown", file=sys.stderr)
            except (OSError, ValueError, yaml.YAMLError) as error:
                errors.append(f"{path}: cannot resolve engine: {error}")
        if flag_constraints is not None:
            model_data = models_by_id.get(recipe.get("model_id", ""), {})
            platforms = recipe.get("platforms", [])
            if isinstance(platforms, list):
                for platform_entry in platforms:
                    if not isinstance(platform_entry, dict) or platform_entry.get("blocked"):
                        continue
                    stack = platform_entry.get("stack", "?")
                    version = platform_entry.get("version", "?")
                    effective_recipe = dict(recipe)
                    effective_recipe["platform"] = {"stack": stack, "version": version}
                    overrides_ref = platform_entry.get("overrides")
                    if isinstance(overrides_ref, str):
                        overrides_path = contained_path(path.parent, overrides_ref)
                        if overrides_path and overrides_path.is_file():
                            try:
                                from render import merge_overrides
                                overrides_data = load_yaml(overrides_path)
                                if overrides_data:
                                    effective_recipe["serving"] = merge_overrides(
                                        recipe.get("serving", {}), overrides_data
                                    )
                            except (
                                OSError,
                                ValueError,
                                yaml.YAMLError,
                                KeyError,
                                TypeError,
                            ) as error:
                                errors.append(
                                    f"{path}: [{stack}-{version}] cannot apply platform overrides for constraint check: {error}"
                                )
                                continue
                    constraint_errors = validate_recipe_against_constraints(
                        flag_constraints, effective_recipe, model_data
                    )
                    errors.extend(
                        f"{path}: [{stack}-{version}] {e}" for e in constraint_errors
                    )
    for path in sorted(repo.glob("models/**/results/**/result.json")):
        try:
            result = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"{path}: cannot load JSON: {error}")
            continue
        document_errors = validate_document(path, result, schemas["benchmark-result"], registry)
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
