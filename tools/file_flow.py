"""Advisory placement/ownership checks for newly introduced playbook files.

Read Git data only: never run a snapshot's renderer, scripts, or Kustomize.
This is a review aid, not another schema validator or a dependency-graph audit.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys

import yaml

from recipe_evidence import git_file, load_unique_yaml


SOURCE_ROOTS = {'.github', 'benchmarks', 'catalog', 'docs', 'engine-versions',
                'schema', 'site', 'templates', 'tests', 'tools'}
ROOT_FILES = {'README.md', 'CONTRIBUTING.md', 'AGENTS.md', 'LICENSE', 'OWNERS'}
RECIPE_FILE = re.compile(r'^(models/[^/]+/recipes/[^/]+)/(.+)$')


def introduced_file_warnings(repo: Path, base: str, head: str, cached: bool = False) -> list[tuple[str, str]]:
    """Classify additions/rename destinations against the exact head or staged index."""
    args = ['git', '-C', str(repo), 'diff', '--name-only', '--diff-filter=A', '--no-renames', '-z']
    args.extend(['--cached', base] if cached else [base, head])
    output = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    paths = sorted(path for path in output.split('\0') if path)
    revision = ':' if cached else head
    documents = {}

    def document(relative):
        if relative not in documents:
            try:
                value = load_unique_yaml(git_file(repo, revision, relative))
                documents[relative] = value if isinstance(value, dict) else {}
            except (subprocess.CalledProcessError, ValueError, yaml.YAMLError):
                # Required documents still fail the normal validator. Missing or
                # malformed ownership metadata cannot silently authorize a file.
                documents[relative] = {}
        return documents[relative]

    def reference(root, value):
        if not isinstance(value, str) or posixpath.isabs(value):
            return None
        path = posixpath.normpath(posixpath.join(root, value))
        return path if path.startswith(root + '/') else None

    owners = {}

    def recipe_owners(root, recipe):
        if root in owners:
            return owners[root]
        # Reuse the current renderer's kind map, never code from the Git snapshot.
        # Import lazily because render.py also imports validator helpers.
        from render import COMPONENT_KIND
        declared, generated = set(), set()
        serving = recipe.get('serving', {})
        mode = serving.get('parallelism', {}).get('mode')
        for platform in recipe.get('platforms', []):
            for key in ('overrides', 'pinned_manifest', 'config'):
                if path := reference(root, platform.get(key)):
                    declared.add(path)
            if platform.get('blocked'):
                continue
            folder = f"{root}/manifests/{platform.get('stack')}-{platform.get('version')}"
            if pin := platform.get('pinned_manifest'):
                generated.add(folder + '/' + PurePosixPath(pin).name)
            elif kind := COMPONENT_KIND.get((platform.get('stack'), mode)):
                generated.add(folder + '/' + kind.lower() + '.yaml')
        for manifest in recipe.get('deployment', {}).get('manifests', []):
            if manifest.get('generated') is False and (path := reference(root, manifest.get('path'))):
                declared.add(path)
        for value in (recipe.get('notes'), serving.get('router', {}).get('values')):
            if path := reference(root, value):
                declared.add(path)
        for value in recipe.get('benchmark_runs', []):
            if not (run_path := reference(root, value)):
                continue
            declared.add(run_path)
            directory = posixpath.dirname(run_path)
            run = document(run_path)
            for value in [run.get('result'), 'forge-import.json',
                          *(artifact.get('path') for artifact in run.get('artifacts', []))]:
                if path := reference(directory, value):
                    declared.add(path)
        owners[root] = declared, generated
        return owners[root]

    def reason(path):
        parts = PurePosixPath(path).parts
        if parts[0] == 'hardware-profiles':
            return None if len(parts) == 2 and path.endswith('.yaml') else (
                'Hardware profiles belong in flat hardware-profiles/<profile>.yaml files.')
        if path.startswith('tests/forge/') and PurePosixPath(path).suffix in {'.yaml', '.yml', '.json'}:
            return 'Operational Forge data is outside the v4 recipe/schema flow; review its ownership and CI coverage.'
        if parts[0] != 'models':
            if (parts[0] in SOURCE_ROOTS or path in ROOT_FILES or
                    len(parts) == 1 and path.startswith('.')):
                return None
            return 'File is outside the documented repository roots; review its placement and validation owner.'
        if re.fullmatch(r'models/[^/]+/model\.yaml', path) or path.endswith('.md'):
            return None
        match = RECIPE_FILE.fullmatch(path)
        if not match:
            return 'Model execution/data files should use a canonical v4 recipe; shared helpers need explicit validation ownership.'
        root, relative = match.groups()
        if relative == 'recipe.yaml' or relative.startswith('raw-manifest/'):
            return None  # Raw-only intake has its own existing syntax/conversion gate.
        recipe = document(root + '/recipe.yaml')
        if recipe.get('schema_version') != 4:
            return 'File has no canonical v4 recipe owner; use raw-manifest/ for initial-release intake.'
        declared, generated = recipe_owners(root, recipe)
        if path in declared or path in generated:
            return None
        if relative.startswith('manifests/'):
            return 'Manifest is not emitted by the renderer or explicitly pinned/hand-authored; keep inputs in config/ or benchmarks/ and review drift coverage.'
        if relative.startswith('config/') and recipe.get('serving', {}).get('config_overrides') is True:
            return None
        if relative.startswith('benchmarks/'):
            return None
        if relative.startswith('guides/') and PurePosixPath(path).suffix not in {'.yaml', '.yml', '.json'}:
            return None
        return 'File is outside declared recipe inputs/evidence; link it from its owner or use the intended config/, benchmarks/, or guides/ flow.'

    warnings = []
    for path in paths:
        try:
            message = reason(path)
        except (AttributeError, KeyError, TypeError, ValueError):
            message = 'Cannot establish ownership from recipe metadata; review placement (schema validation remains separate).'
        if message:
            warnings.append((path, message))
    return warnings


def report_file_warning(path: str, message: str) -> None:
    """Emit one safe GitHub annotation or a readable local advisory."""
    def escape(value):
        return value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        property_path = escape(path).replace(':', '%3A').replace(',', '%2C')
        print(f'::warning file={property_path},title=Playbook file flow::{escape(message)}', file=sys.stderr)
    else:
        print(f'Warning: {path!r}: {message}', file=sys.stderr)
