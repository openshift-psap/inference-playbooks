#!/usr/bin/env python3
"""Check manifest drift without modifying contributor files.

Use --all for publication, recipe directories for targeted CI, or
--base HEAD --cached for pre-commit (checks an isolated staged snapshot).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from jinja2 import TemplateError

from recipe_evidence import affected_recipes, git_lines
from render import COMPONENT_KIND, load_yaml_file, render_recipe


def check_recipe(repo: Path, recipe_dir: Path) -> list[str]:
    """Render in isolation and byte-compare each publishable platform output."""
    recipe = load_yaml_file(recipe_dir / "recipe.yaml")
    outputs = []
    pinned_sources = {Path(platform["pinned_manifest"]) for platform in recipe["platforms"] if platform.get("pinned_manifest")}
    hand_authored = {Path(item["path"]) for item in recipe.get("deployment", {}).get("manifests", []) if item.get("generated") is False}
    for platform in recipe["platforms"]:
        if platform.get("blocked"):
            continue
        directory = f"{platform['stack']}-{platform['version']}"
        if platform.get("pinned_manifest"):
            filename = Path(platform["pinned_manifest"]).name
        else:
            kind = COMPONENT_KIND[(platform["stack"], recipe["serving"]["parallelism"]["mode"])]
            filename = f"{kind.lower()}.yaml"
        outputs.append(Path("manifests") / directory / filename)
    with tempfile.TemporaryDirectory(prefix="playbook-render-") as temporary:
        scratch = Path(temporary)
        relative = recipe_dir.relative_to(repo)
        target = scratch / relative
        shutil.copytree(recipe_dir, target)
        shutil.copytree(repo / "schema", scratch / "schema")
        shutil.copytree(repo / "templates", scratch / "templates")
        model = repo / "models" / recipe["model_id"] / "model.yaml"
        if model.is_file():
            destination = scratch / "models" / recipe["model_id"] / "model.yaml"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(model, destination)
        with contextlib.redirect_stdout(io.StringIO()):
            _, errors = render_recipe(scratch, target / "recipe.yaml")
        if errors:
            return errors
        generated_names = {f"{kind.lower()}.yaml" for kind in COMPONENT_KIND.values()}
        for existing in (recipe_dir / "manifests").glob("*/*.yaml"):
            relative_output = existing.relative_to(recipe_dir)
            if existing.name in generated_names and relative_output not in set(outputs) | pinned_sources | hand_authored:
                errors.append(f"Obsolete generated manifest: {relative / relative_output}; review and remove the obsolete output before regenerating")
        for output in outputs:
            actual = recipe_dir / output
            expected = target / output
            if not actual.is_file() or actual.read_bytes() != expected.read_bytes():
                errors.append(f"Manifest drift: {relative / output}; regenerate with python3 tools/render.py {relative / 'recipe.yaml'}")
        return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipes", nargs="*", type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--cached", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    try:
        if args.cached:
            # Materialize tracked index bytes, not unstaged working-tree content.
            with tempfile.TemporaryDirectory(prefix="playbook-index-") as temporary:
                snapshot = Path(temporary)
                for name in git_lines(repo, ["ls-files", "--cached"]):
                    destination = snapshot / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    content = subprocess.run(["git", "-C", str(repo), "show", f":{name}"], check=True, capture_output=True).stdout
                    destination.write_bytes(content)
                selected = affected_recipes(repo, args.base, args.head, True)["recipes"]
                errors = [error for name in selected for error in check_recipe(snapshot, snapshot / name)]
        else:
            if args.all:
                selected = [path.parent for path in sorted(repo.glob("models/*/recipes/*/recipe.yaml"))]
            elif args.recipes:
                selected = [(repo / path).resolve() for path in args.recipes]
            else:
                selected = [repo / name for name in affected_recipes(repo, args.base, args.head, False)["recipes"]]
            errors = [error for path in selected for error in check_recipe(repo, path)]
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, TemplateError, subprocess.CalledProcessError) as error:
        print(f"Manifest check failed: {error}", file=sys.stderr)
        return 1
    print("Manifest drift check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
