#!/usr/bin/env python3
"""Prepare one immutable recipe/platform for the portable Forge runner (no cluster access)."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from contributor_bundle import FORGE_REVISION, IMAGE, WORKLOADS, checksum, json_bytes, json_load, shareable, validate_request
from fetch_forge_evidence import tested_inputs
from recipe_evidence import load_unique_yaml, load_unique_yaml_all

# Host-side release metadata is recorded after publication, not baked into itself.
RELEASE_FILE = Path(__file__).resolve().parents[1] / 'benchmarks/runner/release.json'
MACOS_RUNNER_MODULES = (
    'contributor_bundle.py', 'contributor_security.py', 'fetch_forge_evidence.py',
    'forge_model_identity.py', 'forge_serving_contract.py', 'monitor_contributor.py',
    'normalize_kpis.py', 'recipe_evidence.py', 'run_contributor.py',
)


def released_runner_image():
    """Use the recorded immutable release; no tag resolution or network access."""
    release = json_load(RELEASE_FILE.read_bytes())
    if (not isinstance(release, dict) or type(release.get('schema_version')) is not int or release['schema_version'] != 1
            or release.get('forge_revision') != FORGE_REVISION):
        raise ValueError('runner release metadata is unsupported or uses a different Forge revision')
    image, version = release.get('image', ''), release.get('version', '')
    source_revision = release.get('source_revision', '')
    if (not isinstance(image, str) or not re.fullmatch(IMAGE, image)
            or not isinstance(version, str) or not re.fullmatch(r'v[0-9]+\.[0-9]+(?:\.[0-9]+)?', version)
            or release.get('tag') != image.split('@', 1)[0] + ':' + version
            or not isinstance(source_revision, str) or not re.fullmatch(r'[a-f0-9]{40}', source_revision)):
        raise ValueError('runner release must record a consistent tag, digest, and source revision')
    return image


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
            runner_image: str | None, harness_image: str, output: Path, runner_mode='container'):
    if runner_mode not in {'container', 'macos-arm64'} or (runner_mode == 'container') != bool(runner_image):
        raise ValueError('container mode requires a runner image; macos-arm64 does not use one')
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
    request = {'schema_version': 1, 'run_id': 'contributor-' + uuid.uuid4().hex,
               'recipe_path': recipe_path, 'tested_revision': revision,
               'platform': {'stack': stack, 'version': version}, 'manifest_path': manifest,
               'inputs': {name.removeprefix('inputs/'): checksum(data) for name, data in files.items()},
               'forge_revision': FORGE_REVISION, 'harness_image': harness_image, 'workload': workload}
    request['runner_image' if runner_mode == 'container' else 'runner_mode'] = (
        runner_image if runner_mode == 'container' else runner_mode)
    validate_request(request, files)
    native_runner = {}
    if runner_mode == 'macos-arm64':
        runner_sources = ('tools/prepare_contributor.py', 'tools/bootstrap_macos.py',
                          'benchmarks/runner/requirements.lock',
                          *(f'tools/{name}' for name in MACOS_RUNNER_MODULES))
        for name in runner_sources:
            try:
                committed = git(repo, 'show', f'{revision}:{name}')
            except subprocess.CalledProcessError as error:
                raise ValueError(f'native runner input is not committed: {name}') from error
            if (repo / name).read_bytes() != committed:
                raise ValueError(f'native runner input has uncommitted changes: {name}')
            native_runner[name] = committed
        if Path(__file__).resolve().read_bytes() != native_runner['tools/prepare_contributor.py']:
            raise ValueError('preparation tool differs from committed runner input')
    output.mkdir(parents=True)
    for name, data in files.items():
        target = output / 'playbooks' / name.removeprefix('inputs/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (output / 'request.json').write_bytes(json_bytes(request))
    if runner_mode == 'macos-arm64':
        runner = output / 'runner'
        runner.mkdir()
        (output / 'bootstrap_macos.py').write_bytes(native_runner['tools/bootstrap_macos.py'])
        (runner / 'requirements.lock').write_bytes(native_runner['benchmarks/runner/requirements.lock'])
        tool_dir = runner / 'tools'
        tool_dir.mkdir()
        for name in MACOS_RUNNER_MODULES:
            (tool_dir / name).write_bytes(native_runner[f'tools/{name}'])
        launcher = '''#!/bin/sh
set -eu
case "$0" in
  */*) package_dir=${0%/*}; [ -n "$package_dir" ] || package_dir=/ ;;
  *) package_dir=. ;;
esac
package=$(CDPATH= cd "$package_dir" && pwd -P)
command -v python3.12 >/dev/null 2>&1 || { echo "Python 3.12 is required" >&2; exit 127; }
export PYTHONDONTWRITEBYTECODE=1
exec python3.12 "$package/bootstrap_macos.py" "$@"
'''
    else:
        launcher = '''#!/bin/sh
set -eu
if [ "$#" -ne 4 ]; then
  echo "Usage: $0 KUBECONFIG CONTEXT NAMESPACE NEW_OUTPUT_DIRECTORY" >&2
  exit 2
fi
package=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
kubeconfig=$(realpath -- "$1")
[ -f "$kubeconfig" ] || { echo "Kubeconfig must be a regular file" >&2; exit 2; }
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
  -e KUBECONFIG=/credentials/kubeconfig \\
  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy \\
  -v "$package:/inputs:ro,Z" -v "$kubeconfig:/credentials/kubeconfig:ro" -v "$output:/outputs:Z" \\
  IMAGE --context "$2" --namespace "$3"
'''.replace('  IMAGE ', '  ' + shlex.quote(runner_image) + ' ')
    (output / 'run.sh').write_text(launcher)
    (output / 'run.sh').chmod(0o755)
    monitor_source = (native_runner['tools/monitor_contributor.py'] if runner_mode == 'macos-arm64'
                      else Path(__file__).with_name('monitor_contributor.py').read_bytes())
    (output / 'monitor.py').write_bytes(monitor_source)
    monitor = '''#!/bin/sh
set -eu
package=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$package/monitor.py" "$@"
'''
    (output / 'monitor.sh').write_text(monitor)
    (output / 'monitor.sh').chmod(0o755)
    return request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recipe', help='repository-relative recipe.yaml')
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--stack', choices=['vllm', 'rhoai'], required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--workload', choices=sorted(WORKLOADS), required=True)
    parser.add_argument('--runner', choices=['container', 'macos-arm64'], default='container',
                        help='execution host; container is the Linux default')
    parser.add_argument('--runner-image', help='digest-pinned container override; defaults to benchmarks/runner/release.json')
    parser.add_argument('--harness-image', required=True, help='audited GuideLLM/AIPerf image@sha256:digest')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.runner == 'container':
            runner_image = args.runner_image or released_runner_image()
        else:
            if args.runner_image:
                raise ValueError('--runner-image only applies to the container runner')
            runner_image = None
        request = prepare(args.repo.resolve(), args.recipe, args.stack, args.version, args.workload,
                          runner_image, args.harness_image, args.output, runner_mode=args.runner)
        print(json.dumps({'run_id': request['run_id'], 'package': str(args.output)}))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f'Cannot prepare contributor package: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
