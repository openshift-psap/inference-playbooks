"""Prove container-produced contributor evidence imports through the real importer.

Builds a committed synthetic recipe, prepares a package with the same tools a
contributor uses, runs the built image to produce ``evidence.tar.gz`` with
synthetic jobs, then runs the maintainer importer's dry-run plan against that
exact archive and the committed Git revision. No cluster, GPU, or metrics claim.

Runtime execution is frozen to the inspected image ID. The request records a
local manifest digest only when the engine exposes one; otherwise it uses an
explicit fixture reference instead of presenting a config ID as a digest.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
sys.path.insert(0, str(REPO / 'tests'))

from contributor_bundle import WORKLOADS, checksum, read_archive
from contributor_fixtures import HARNESS_IMAGES, SMOKE_WORKLOADS, build_source_repo
from import_contributor import import_bundle
from prepare_contributor import prepare

# A locally built image has no registry manifest in docker; the request still
# needs a digest-pinned reference, so this value is explicitly a fixture
# placeholder that does not claim to identify the built image.
SYNTHETIC_RUNNER_IMAGE = 'quay.io/example/runner@sha256:' + 'a' * 64


def engine_name(explicit):
    if explicit:
        return explicit
    configured = os.environ.get('CONTAINER_ENGINE')
    if configured:
        return configured
    for candidate in ('podman', 'docker'):
        if shutil.which(candidate):
            return candidate
    raise SystemExit('no container engine found; pass --engine')


def inspect(engine, image, template):
    return subprocess.run([engine, 'image', 'inspect', '--format', template, image],
                          check=True, capture_output=True, text=True).stdout.strip()


def image_reference_name(image):
    """Repository part of a tagged reference; image IDs and digests are rejected."""
    name, separator, tag = image.rpartition(':')
    if not separator or not tag or '@' in image or '/' not in name:
        raise SystemExit(f'expected a tagged image reference, got {image}')
    return name


def immutable_image_id(engine, image):
    """Config ID of the exact local image; this is what execution is frozen to."""
    value = inspect(engine, image, '{{.Id}}')
    digest = value.removeprefix('sha256:')
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise SystemExit(f'cannot resolve an immutable image ID for {image}')
    return 'sha256:' + digest


def local_manifest_digest(engine, image):
    """Manifest digest only when the engine exposes a real one for this image."""
    if os.path.basename(engine) == 'podman':
        value = inspect(engine, image, '{{.Digest}}')
        return value if re.fullmatch(r'sha256:[a-f0-9]{64}', value) else None
    try:
        references = json.loads(inspect(engine, image, '{{json .RepoDigests}}') or '[]')
    except ValueError:
        return None
    for reference in references:
        _, separator, digest = reference.rpartition('@')
        if separator and re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
            return digest
    return None


def request_runner_image(engine, image):
    """Return (reference, attribution) without ever presenting a config ID as a digest."""
    digest = local_manifest_digest(engine, image)
    if digest is None:
        return SYNTHETIC_RUNNER_IMAGE, 'synthetic-fixture'
    return image_reference_name(image) + '@' + digest, 'local-manifest-digest'


def container_command(engine, image, package, output, script, arch, platform=None):
    platform = platform or sys.platform
    podman = os.path.basename(engine) in {'podman', 'podman-remote'}
    if platform == 'darwin' and not podman:
        raise SystemExit('macOS requires the Podman CLI with a running podman machine; '
                         'Docker Desktop, Colima, and other VM-based Docker engines are not supported')
    command = [engine, 'run', '--rm', '--init', '--network', 'none',
               '--user', f'{os.getuid()}:{os.getgid()}',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges']
    # Make the requested architecture explicit for both engines. On a native
    # runner this is a no-op; under emulation it also selects the userland.
    command += ['--arch', arch] if podman else ['--platform', 'linux/' + arch]
    if podman:
        command.append('--userns=keep-id')
    # SELinux relabeling does not apply across the podman machine's virtiofs
    # boundary; see the generated launcher for the same rule.
    label = ',Z' if podman and platform != 'darwin' else ''
    command += ['-e', 'SMOKE_EXPECTED_ARCH=' + arch,
                '-v', f'{package}:/inputs:ro{label}',
                '-v', f'{output}:/outputs{label}',
                '-v', f'{script}:/smoke.py:ro{label}',
                '--entrypoint', 'python', image, '/smoke.py',
                '--request', '/inputs/request.json', '--inputs', '/inputs/playbooks', '--output', '/outputs']
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True,
                        help='tagged built image; execution is frozen to its resolved immutable ID')
    parser.add_argument('--workload', required=True, choices=SMOKE_WORKLOADS)
    parser.add_argument('--arch', required=True, choices=('amd64', 'arm64'),
                        help='expected image architecture; checked inside the container')
    parser.add_argument('--engine', help='container engine binary (default: CONTAINER_ENGINE or podman/docker)')
    args = parser.parse_args()
    engine = engine_name(args.engine)
    runtime_image = immutable_image_id(engine, args.image)
    reference, attribution = request_runner_image(engine, args.image)

    with tempfile.TemporaryDirectory(prefix='contributor-import-') as temporary:
        work = Path(temporary)
        repo, root, _ = build_source_repo(work)
        package = work / 'package'
        request = prepare(repo, 'models/test/recipes/test-recipe/recipe.yaml', 'vllm', 'v0.24.0',
                          args.workload, reference, HARNESS_IMAGES[args.workload], package)
        recipe_path = root / 'recipe.yaml'
        original_recipe = recipe_path.read_bytes()

        # The repository keeps source files private; make only the mounted copy
        # readable so the container never depends on the host umask.
        script = work / 'smoke.py'
        script.write_bytes((REPO / 'tests/contributor_container_smoke.py').read_bytes())
        script.chmod(0o644)

        output = work / 'output'
        output.mkdir()
        result = subprocess.run(container_command(engine, runtime_image, package, output, script, args.arch),
                                capture_output=True, text=True)
        if result.returncode:
            print(result.stdout, file=sys.stdout)
            print(result.stderr, file=sys.stderr)
            raise SystemExit(f'container smoke failed for {args.workload} on {args.arch}')

        archive = output / 'evidence.tar.gz'
        digest = (output / 'evidence.sha256').read_text().strip()
        if checksum(archive.read_bytes()) != digest:
            raise SystemExit('returned checksum does not identify the produced archive')
        envelope, files = read_archive(archive, digest)
        if envelope['request'] != request:
            raise SystemExit('container archive records a different request than the prepared package')
        if not any(name.startswith('raw/') for name in files):
            raise SystemExit('container archive has no native harness export')

        report = import_bundle(repo, archive, digest)
        workload = WORKLOADS[args.workload]
        run_id = f"{request['run_id']}-{workload['harness']}"
        assert report['applied'] is False, 'dry-run import must not write'
        assert report['run_id'] == run_id, report
        assert report['completeness'] == 'smoke', report
        assert report['destination'] == str(root / 'results' / run_id), report
        assert not (root / 'results').exists(), 'dry-run import wrote results'
        assert recipe_path.read_bytes() == original_recipe, 'dry-run import modified the recipe'

    print(json.dumps({'workload': args.workload, 'harness': workload['harness'], 'arch': args.arch,
                      'engine': os.path.basename(engine), 'run_id': run_id,
                      'completeness': report['completeness'], 'runtime_image': runtime_image,
                      'request_runner_image': reference, 'runner_image_source': attribution,
                      'checksum': digest}))


if __name__ == '__main__':
    main()
