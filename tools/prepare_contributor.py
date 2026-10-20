#!/usr/bin/env python3
"""Prepare one immutable recipe/platform for the portable Forge runner (no cluster access)."""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

from contributor_bundle import FORGE_REVISION, WORKLOADS, checksum, json_bytes, shareable, validate_request
from fetch_forge_evidence import tested_inputs
from recipe_evidence import load_unique_yaml, load_unique_yaml_all


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True).stdout


def source_files(repo, recipe_path, revision, folder):
    """Exact allowlisted Git input closure, shared with the trusted importer."""
    recipe = load_unique_yaml(git(repo, 'show', f'{revision}:{recipe_path}').decode())
    parent = Path(recipe_path).parent
    roots = [recipe_path, recipe['hardware_profile'], f'models/{recipe["model_id"]}/model.yaml',
             str(parent / 'platforms'), str(parent / 'config'), folder]
    files = {}
    for line in git(repo, 'ls-tree', '-r', revision, '--', *roots).decode().splitlines():
        mode_type, name = line.split('\t')
        if not name.endswith(('.yaml', '.yml', '.json')):
            continue
        if mode_type.split()[0] != '100644':
            raise ValueError('inputs must be regular non-executable files')
        raw = git(repo, 'show', f'{revision}:{name}')
        for document in load_unique_yaml_all(raw.decode()):
            shareable(document)
        files['inputs/' + name] = raw
    return files


def prepare(repo: Path, recipe_path: str, stack: str, version: str, workload: str,
            runner_image: str, harness_image: str, output: Path):
    revision = git(repo, 'rev-parse', 'HEAD').decode().strip()
    path, recipe = tested_inputs(repo, recipe_path, revision)
    if output.exists():
        raise ValueError('output directory already exists; choose a new package path')
    folder = str(path.parent.relative_to(repo) / 'manifests' / f'{stack}-{version}')
    manifest_paths = [p for p in git(repo, 'ls-tree', '-r', '--name-only', revision, '--', folder).decode().splitlines()
                      if p.endswith('.yaml')]
    if len(manifest_paths) != 1:
        raise ValueError('initial runner requires one self-contained YAML manifest per platform')
    files = source_files(repo, recipe_path, revision, folder)
    for name, raw in files.items():
        if (repo / name.removeprefix('inputs/')).read_bytes() != raw:
            raise ValueError('package inputs must be committed and clean')
    manifest = manifest_paths[0]
    documents = [d for d in load_unique_yaml_all(files['inputs/' + manifest].decode()) if d]
    primaries = [d for d in documents if d.get('kind') in {'Deployment', 'LeaderWorkerSet', 'LLMInferenceService'}]
    if len(primaries) != 1 or any(d.get('kind') not in {'Deployment', 'LeaderWorkerSet', 'LLMInferenceService', 'Service'} for d in documents):
        raise ValueError('unsupported manifest shape; requires one serving resource and optional Service')
    if primaries[0]['kind'] == 'LLMInferenceService' and not primaries[0]['spec']['model']['uri'].startswith('pvc://'):
        raise ValueError('standalone LLMInferenceService requires an existing populated PVC; no internal model-cache provisioning')
    request = {'schema_version': 1, 'run_id': 'contributor-' + uuid.uuid4().hex,
               'recipe_path': recipe_path, 'tested_revision': revision,
               'platform': {'stack': stack, 'version': version}, 'manifest_path': manifest,
               'inputs': {name.removeprefix('inputs/'): checksum(data) for name, data in files.items()},
               'forge_revision': FORGE_REVISION, 'runner_image': runner_image,
               'harness_image': harness_image, 'workload': workload}
    validate_request(request, files)
    output.mkdir(parents=True)
    for name, data in files.items():
        target = output / 'playbooks' / name.removeprefix('inputs/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (output / 'request.json').write_bytes(json_bytes(request))
    launcher = '''#!/bin/sh
set -eu
if [ "$#" -ne 4 ]; then
  echo "Usage: $0 KUBECONFIG CONTEXT NAMESPACE NEW_OUTPUT_DIRECTORY" >&2
  exit 2
fi
package=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
kubeconfig=$(realpath -- "$1")
credentials=$(dirname -- "$kubeconfig")
config_name=$(basename -- "$kubeconfig")
output=$(realpath -m -- "$4")
mkdir -- "$output"
chmod 700 "$output"
engine=${CONTAINER_ENGINE:-}
if [ -z "$engine" ]; then
  if command -v podman >/dev/null 2>&1; then engine=podman; else engine=docker; fi
fi
userns=
if [ "$(basename -- "$engine")" = podman ]; then userns=--userns=keep-id; fi
exec "$engine" run --rm --init --network host --user "$(id -u):$(id -g)" \\
  ${userns:+"$userns"} \\
  --cap-drop ALL --security-opt no-new-privileges \\
  -e "KUBECONFIG=/credentials/$config_name" \\
  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy \\
  -v "$package:/inputs:ro,Z" -v "$credentials:/credentials:ro" -v "$output:/outputs:Z" \\
  IMAGE --context "$2" --namespace "$3"
'''.replace('  IMAGE ', '  ' + shlex.quote(runner_image) + ' ')
    (output / 'run.sh').write_text(launcher)
    (output / 'run.sh').chmod(0o755)
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recipe', help='repository-relative recipe.yaml')
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--stack', choices=['vllm', 'rhoai'], required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--workload', choices=sorted(WORKLOADS), required=True)
    parser.add_argument('--runner-image', required=True, help='published runner image@sha256:digest')
    parser.add_argument('--harness-image', required=True, help='audited GuideLLM/AIPerf image@sha256:digest')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        request = prepare(args.repo.resolve(), args.recipe, args.stack, args.version, args.workload,
                          args.runner_image, args.harness_image, args.output)
        print(json.dumps({'run_id': request['run_id'], 'package': str(args.output)}))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f'Cannot prepare contributor package: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
