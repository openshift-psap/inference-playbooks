#!/usr/bin/env python3
"""Materialize explicit single-node vLLM platform inputs; never render or commit."""

import argparse
import json
from pathlib import Path

import yaml

from companions import companion_errors, plan_companion
from engine_versions import normalize_version
from recipe_evidence import load_unique_yaml
from validate import load_schema_registry, validate_document


def prepare(repo: Path, path: Path, check: bool = False, version: str | None = None,
            overrides: str | None = None) -> list[str]:
    recipe = load_unique_yaml(path.read_text())
    registry = load_schema_registry(repo)
    schema = json.loads((repo / "schema/recipe.schema.json").read_text())
    errors = validate_document(path, recipe, schema, registry)
    if errors:
        return errors
    plan = plan_companion(repo, path, recipe, version, overrides)
    if not plan:
        return companion_errors(repo, path, recipe)
    if check:
        return [f"{path}: missing explicit companion; run python3 tools/prepare_companions.py {path}"]
    updated, reference, inputs = plan
    keys = [(p["stack"], normalize_version(p["version"])) for p in updated["platforms"]]
    if len(keys) != len(set(keys)):
        return [f"{path}: companion would duplicate an existing platform stack/version; review blocked or aliased entries"]
    destination = path.parent / reference
    if not destination.resolve().is_relative_to(path.parent.resolve()):
        return [f"{path}: companion override destination escapes the recipe directory"]
    override_schema = json.loads((repo / "schema/platform-overrides.schema.json").read_text())
    errors.extend(validate_document(destination, inputs, override_schema, registry))
    errors.extend(validate_document(path, updated, schema, registry))
    if destination.exists() and not overrides and load_unique_yaml(destination.read_text()) != inputs:
        errors.append(f"refusing to overwrite unrelated overrides: {destination}")
    if any(p["overrides"] == reference for p in recipe["platforms"]):
        errors.append(f"override path already belongs to a platform: {reference}")
    if errors:
        return errors
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(inputs, sort_keys=False))
    path.write_text(yaml.safe_dump(updated, sort_keys=False))
    print(f"Prepared {path}: vllm-{updated['platforms'][-1]['version']} (unverified)")
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipes", nargs="*", type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="store_true", help="Read-only missing/stale companion check")
    parser.add_argument("--version", help="Explicit engine version; never selects latest")
    parser.add_argument("--overrides", help="Reviewed target overrides with image-bound engine metadata")
    args = parser.parse_args()
    repo = args.repo.resolve()
    if (args.version or args.overrides) and len(args.recipes) != 1:
        parser.error("explicit target selection requires exactly one recipe")
    paths = [p.resolve() for p in args.recipes] or sorted(repo.glob("models/*/recipes/*/recipe.yaml"))
    errors = []
    for path in paths:
        try:
            if not path.is_relative_to(repo):
                raise ValueError("recipe must be inside --repo")
            errors.extend(prepare(repo, path, args.check, args.version, args.overrides))
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
            errors.append(f"{path}: {error}")
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
