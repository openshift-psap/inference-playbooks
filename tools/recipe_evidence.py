#!/usr/bin/env python3
"""CI helpers for corrigible recipe evidence inputs.

This module intentionally contains no GitHub Actions-specific behavior. The
same commands are used by local pre-commit hooks and CI.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import yaml

PROFILE_ROOT = "hardware-profiles"


def git_lines(repo: Path, arguments: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [line for line in result.stdout.splitlines() if line]


def changed_paths(repo: Path, base: str, head: str, cached: bool) -> list[tuple[str, list[str]]]:
    arguments = ["diff", "--name-status", "-M"]
    if cached:
        arguments.append("--cached")
        # The staged index is the right-hand side of this comparison. Passing
        # a second revision would compare two commits and ignore staged files.
        arguments.append(base)
    else:
        arguments.extend([base, head])
    changes = []
    for line in git_lines(repo, arguments):
        fields = line.split("\t")
        changes.append((fields[0], fields[1:]))
    return changes


def profile_paths(changes: Iterable[tuple[str, list[str]]]) -> Iterable[tuple[str, str]]:
    for status, paths in changes:
        for path in paths:
            if Path(path).parent.as_posix() == PROFILE_ROOT and path.endswith(".yaml"):
                yield status, path


def git_file(repo: Path, revision: str, path: str) -> str:
    object_name = f":{path}" if revision == ":" else f"{revision}:{path}"
    return subprocess.run(
        ["git", "-C", str(repo), "show", object_name],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def load_profile(repo: Path, revision: str, path: str) -> dict:
    profile = yaml.safe_load(git_file(repo, revision, path))
    if not isinstance(profile, dict):
        raise ValueError("profile must be a YAML object")
    return profile


def expected_accelerator_key(profile: dict) -> str:
    accelerators = profile.get("accelerators", {})
    if not isinstance(accelerators, dict):
        return ""
    vendor = str(accelerators.get("vendor", "")).lower()
    model = re.sub(r"[^a-z0-9]+", "-", str(accelerators.get("model", "")).lower()).strip("-")
    count = accelerators.get("count_per_node")
    return f"{vendor}-{model}-x{count}" if vendor and model and isinstance(count, int) else ""


def identity(profile: dict) -> dict:
    return {
        "profile_id": profile.get("profile_id"),
        "accelerator_key": profile.get("accelerator_key")
    }


def profile_errors(path: str, profile: dict) -> list[str]:
    errors = []
    if profile.get("profile_id") != Path(path).stem:
        errors.append("profile_id must match the profile filename stem")
    if not isinstance(profile.get("profile_revision"), int) or profile["profile_revision"] < 1:
        errors.append("profile_revision must be a positive integer")
    expected_key = expected_accelerator_key(profile)
    if profile.get("accelerator_key") != expected_key:
        errors.append(f"accelerator_key must be {expected_key!r} (vendor, model, and count only)")
    if not isinstance(profile.get("correction_log"), list):
        errors.append("correction_log must be a list")
    return errors


def check_hardware_profiles(repo: Path, base: str, head: str, cached: bool) -> int:
    violations = []
    for status, path in profile_paths(changed_paths(repo, base, head, cached)):
        if status.startswith(("D", "R")):
            violations.append(f"{status}\t{path}: profile files may not be deleted or renamed")
            continue
        candidate_revision = ":" if cached else head
        try:
            candidate = load_profile(repo, candidate_revision, path)
            errors = profile_errors(path, candidate)
            if errors:
                violations.extend(f"{status}\t{path}: {error}" for error in errors)
                continue
            if status.startswith("M"):
                previous = load_profile(repo, base, path)
                if identity(previous) != identity(candidate):
                    violations.append(f"{status}\t{path}: profile_id and accelerator_key require a new profile file")
                elif candidate["profile_revision"] <= previous.get("profile_revision", 0):
                    violations.append(f"{status}\t{path}: profile_revision must increase for a correction")
                elif not any(
                    isinstance(entry, dict)
                    and entry.get("revision") == candidate["profile_revision"]
                    and isinstance(entry.get("summary"), str)
                    and entry["summary"].strip()
                    for entry in candidate["correction_log"]
                ):
                    violations.append(f"{status}\t{path}: correction_log needs a summary for the new revision")
        except (subprocess.CalledProcessError, ValueError, yaml.YAMLError) as error:
            violations.append(f"{status}\t{path}: {error}")
    if violations:
        print("Hardware profiles must be valid, stable identities; corrections require a revision and log entry.", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    return 0


def recipe_directories(repo: Path) -> set[str]:
    return {
        str(path.parent.relative_to(repo))
        for path in repo.glob("models/**/recipes/*/*/*/recipe.yaml")
    }


def affected_recipes(repo: Path, base: str, head: str, cached: bool) -> dict[str, object]:
    recipes = recipe_directories(repo)
    changed = changed_paths(repo, base, head, cached)
    global_change = any(
        any(path.startswith(prefix) for prefix in ("tools/", "schema/", ".github/workflows/"))
        for _, paths in changed
        for path in paths
    )
    if global_change:
        return {"all": True, "recipes": sorted(recipes)}

    affected = {
        recipe
        for recipe in recipes
        for _, paths in changed
        for path in paths
        if path == recipe or path.startswith(f"{recipe}/")
    }
    changed_profiles = {path for _, path in profile_paths(changed)}
    for recipe in recipes:
        recipe_text = (repo / recipe / "recipe.yaml").read_text()
        if any(profile in recipe_text for profile in changed_profiles):
            affected.add(recipe)
    return {"all": False, "recipes": sorted(affected)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--head", default="HEAD", help="comparison head; ignored with --cached")
    parser.add_argument("--cached", action="store_true", help="compare the staged index with --base")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check-hardware-profiles")
    subparsers.add_parser("affected-recipes")
    arguments = parser.parse_args()
    repo = arguments.repo.resolve()

    if arguments.command == "check-hardware-profiles":
        return check_hardware_profiles(repo, arguments.base, arguments.head, arguments.cached)
    print(json.dumps(affected_recipes(repo, arguments.base, arguments.head, arguments.cached)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
