#!/usr/bin/env python3
"""Materialize explicit single-node vLLM platform inputs; never render or commit."""

import argparse
from pathlib import Path

import yaml

from companion_inputs import prepare


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
