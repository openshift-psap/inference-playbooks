"""Shared explicit companion-input planning/writing seam; no rendering or commits."""

import json
from pathlib import Path

import yaml

from companions import companion_errors, plan_companion
from engine_versions import normalize_version
from recipe_evidence import load_unique_yaml


def preparation_plan(repo: Path, path: Path, version: str | None = None,
                     overrides: str | None = None) -> tuple[tuple | None, list[str]]:
    """Validate the complete input change without writes, including collisions."""
    # Validation imports renderer seams; defer imports to avoid initialization cycles.
    from validate import load_schema_registry, validate_document

    recipe = load_unique_yaml(path.read_text())
    registry = load_schema_registry(repo)
    schema = json.loads((repo / "schema/recipe.schema.json").read_text())
    errors = validate_document(path, recipe, schema, registry)
    if errors:
        return None, errors
    plan = plan_companion(repo, path, recipe, version, overrides)
    if not plan:
        return None, companion_errors(repo, path, recipe)
    updated, reference, inputs = plan
    keys = [(p["stack"], normalize_version(p["version"])) for p in updated["platforms"]]
    if len(keys) != len(set(keys)):
        return None, [f"{path}: companion would duplicate an existing platform stack/version; review blocked or aliased entries"]
    destination = path.parent / reference
    if not destination.resolve().is_relative_to(path.parent.resolve()):
        return None, [f"{path}: companion override destination escapes the recipe directory"]
    override_schema = json.loads((repo / "schema/platform-overrides.schema.json").read_text())
    errors.extend(validate_document(destination, inputs, override_schema, registry))
    errors.extend(validate_document(path, updated, schema, registry))
    if destination.exists() and not overrides and load_unique_yaml(destination.read_text()) != inputs:
        errors.append(f"refusing to overwrite unrelated overrides: {destination}")
    if any(p["overrides"] == reference for p in recipe["platforms"]):
        errors.append(f"override path already belongs to a platform: {reference}")
    return (None, errors) if errors else (plan, [])


def materialize(path: Path, plan: tuple) -> None:
    """Write only the validated explicit entry/overrides; never reset assessments."""
    updated, reference, inputs = plan
    destination = path.parent / reference
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(inputs, sort_keys=False))
    path.write_text(yaml.safe_dump(updated, sort_keys=False))


def prepare(repo: Path, path: Path, check: bool = False, version: str | None = None,
            overrides: str | None = None) -> list[str]:
    plan, errors = preparation_plan(repo, path, version, overrides)
    if errors or plan is None:
        return errors
    if check:
        return [f"{path}: missing explicit companion inputs; run python3 tools/render.py {path} locally"]
    materialize(path, plan)
    print(f"Prepared {path}: vllm-{plan[0]['platforms'][-1]['version']} (unverified)")
    return []
