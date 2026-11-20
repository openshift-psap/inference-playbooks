#!/usr/bin/env python3
"""Apple Silicon acceptance gate for the generated Podman launcher.

GitHub's arm64 macOS runners cannot start a VM (Apple's Virtualization
Framework does not provide nested virtualization there), so a hosted CI job
cannot create a podman machine. Run this on real Apple Silicon hardware with the
Podman CLI and an initialized, running podman machine.

The proof has two parts:

1. the generated ``run.sh`` accepts only Podman on macOS and starts the pinned
   image with working read-only package/kubeconfig mounts and a writable output
   directory (the synthetic kubeconfig points at a closed port, so the run is
   expected to fail in cluster preflight rather than in the engine boundary);
2. ``CONTAINER_ENGINE=docker`` is rejected before any engine runs.

It prints the observed macOS, Podman, and image versions. Do not treat an
unrecorded version combination as verified.
"""
import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
sys.path.insert(0, str(REPO / 'tests'))

from contributor_fixtures import HARNESS_IMAGES, build_source_repo
from prepare_contributor import prepare

RECIPE = 'models/test/recipes/test-recipe/recipe.yaml'


def command_output(*argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def kubeconfig(path: Path):
    """Self-contained config for a closed local port; never contacts a real cluster."""
    path.write_text('''apiVersion: v1
kind: Config
clusters:
- name: acceptance
  cluster:
    server: https://127.0.0.1:1
    insecure-skip-tls-verify: true
contexts:
- name: acceptance
  context: {cluster: acceptance, user: acceptance, namespace: default}
current-context: acceptance
users:
- name: acceptance
  user: {token: synthetic-not-a-credential}
''')
    path.chmod(0o600)


def run_launcher(launcher, kubeconfig_path, output, environment):
    started = time.monotonic()
    result = subprocess.run([str(launcher), str(kubeconfig_path), 'acceptance', 'benchmark', str(output)],
                            capture_output=True, text=True, env=environment, timeout=300)
    return result, time.monotonic() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True,
                        help='published digest-pinned linux/arm64 runner image (for example a release child digest)')
    parser.add_argument('--workload', default='guidellm-smoke', choices=('guidellm-smoke', 'aiperf-smoke'))
    args = parser.parse_args()

    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('this acceptance gate requires Apple Silicon macOS')
    podman = shutil.which('podman')
    if podman is None:
        raise SystemExit('Podman CLI is required; Docker Desktop and Colima are not supported')
    machine_arch = command_output(podman, 'info', '--format', '{{.Host.Arch}}')
    if machine_arch != 'arm64':
        raise SystemExit(f'the running podman machine reports {machine_arch}, expected arm64')

    with tempfile.TemporaryDirectory(prefix='contributor-macos-podman-') as temporary:
        root = Path(temporary).resolve()
        repo, _, _ = build_source_repo(root / 'fixture')
        package = root / 'package'
        prepare(repo, RECIPE, 'vllm', 'v0.24.0', args.workload, args.image,
                HARNESS_IMAGES[args.workload], package)
        launcher = package / 'run.sh'
        assert launcher.is_file() and launcher.stat().st_mode & stat.S_IXUSR, 'generated launcher is not executable'
        script = launcher.read_text()
        assert 'realpath -m' not in script, 'launcher relies on GNU-only path resolution'

        config = root / 'acceptance.kubeconfig'
        kubeconfig(config)

        # Default selection must choose Podman on macOS; the closed API port
        # exercises engine startup, mounts, UID mapping, and progress writing.
        output = root / 'output'
        environment = {key: value for key, value in os.environ.items() if key != 'CONTAINER_ENGINE'}
        result, elapsed = run_launcher(launcher, config, output, environment)
        progress = output / 'progress.json'
        if not progress.is_file():
            raise SystemExit(f'launcher never reached the container: {result.stderr.strip()}')
        state = json.loads(progress.read_text())
        if state.get('outcome') != 'failed':
            raise SystemExit(f'expected a preflight failure against the closed port, got {state.get("outcome")!r}')
        owned = {path.stat().st_uid for path in (output, progress, output / 'private-diagnostics')}
        if owned != {os.getuid()}:
            raise SystemExit(f'container output is not owned by the invoking user: {sorted(owned)}')
        combined = result.stdout + result.stderr
        for marker in ('permission denied', 'invalid option', 'cannot connect to podman',
                       'no such file or directory', 'Error: mounting'):
            if marker.lower() in combined.lower():
                raise SystemExit(f'launcher failed at the engine/mount boundary: {marker}')

        # The same generated launcher must refuse a non-Podman engine on macOS.
        rejection_output = root / 'rejected-output'
        rejected, _ = run_launcher(launcher, config, rejection_output,
                                   {**environment, 'CONTAINER_ENGINE': 'docker'})
        if rejected.returncode != 2 or 'Podman' not in rejected.stderr:
            raise SystemExit('launcher did not reject Docker on macOS with a Podman-specific error')
        if rejection_output.exists():
            raise SystemExit('launcher created an output directory before rejecting the engine')

    print(json.dumps({
        'result': 'PASS',
        'macos': platform.mac_ver()[0],
        'machine': platform.machine(),
        'podman': command_output(podman, '--version'),
        'podman_machine_arch': machine_arch,
        'image': args.image,
        'workload': args.workload,
        'preflight_seconds': round(elapsed, 1),
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
