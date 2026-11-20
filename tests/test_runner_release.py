"""Publication contract: verified GHCR metadata, launcher platform rules, and workflow safety."""
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
sys.path.insert(0, str(REPO / 'tests'))

import contributor_import_smoke as driver
import prepare_contributor as preparation
from contributor_bundle import FORGE_REVISION, json_bytes
from contributor_fixtures import HARNESS_IMAGES, build_source_repo
from prepare_contributor import prepare
from runner_release import (ARCHITECTURES, ReleaseError, assemble, guard_tag, release_document,
                            validate_platform_record, validate_release, verify_child, verify_index,
                            verify_published)

WORKFLOW = REPO / '.github/workflows/runner-release.yml'
PR_WORKFLOW = REPO / '.github/workflows/recipe-evidence.yml'
RELEASE = REPO / 'benchmarks/runner/release.json'
MANIFEST_TYPE = 'application/vnd.oci.image.manifest.v1+json'
INDEX_TYPE = 'application/vnd.oci.image.index.v1+json'
CONFIG = {'amd64': 'sha256:' + '1' * 64, 'arm64': 'sha256:' + '2' * 64}
# Docker's containerd store can report an index digest for the frozen local ID.
# It is recorded separately and never treated as the image config digest.
EXECUTED = 'sha256:' + 'a' * 64
REPOSITORY = 'ghcr.io/example/runner'
HARNESS = 'ghcr.io/vllm-project/guidellm@sha256:' + 'b' * 64


def digest(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def child_manifest(config):
    return json_bytes({'schemaVersion': 2, 'mediaType': MANIFEST_TYPE,
                       'config': {'mediaType': 'application/vnd.oci.image.config.v1+json',
                                  'digest': config, 'size': 1},
                       'layers': [{'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip',
                                   'digest': 'sha256:' + '3' * 64, 'size': 1}]})


def index_document(children):
    return json_bytes({'schemaVersion': 2, 'mediaType': INDEX_TYPE, 'manifests': [
        {'mediaType': MANIFEST_TYPE, 'digest': children[arch], 'size': 1,
         'platform': {'os': 'linux', 'architecture': arch}} for arch in ARCHITECTURES]})


def platform_record(arch, child, executed=None):
    return {'schema_version': 1, 'arch': arch,
            'executed_image_id': executed or EXECUTED,
            'config_digest': CONFIG[arch],
            'manifest_digest': digest(child), 'layer_count': 1,
            'forge_revision': FORGE_REVISION}


def release_v2(**overrides):
    document = {
        'schema_version': 2, 'version': 'v0.2.0',
        'tag': 'ghcr.io/openshift-psap/inference-playbooks-runner:v0.2.0',
        'image': 'ghcr.io/openshift-psap/inference-playbooks-runner@sha256:' + '4' * 64,
        'forge_revision': FORGE_REVISION, 'source_revision': '5' * 40,
        'platforms': {
            'amd64': {'image': 'ghcr.io/openshift-psap/inference-playbooks-runner@sha256:' + '6' * 64},
            'arm64': {'image': 'ghcr.io/openshift-psap/inference-playbooks-runner@sha256:' + '7' * 64},
        },
    }
    document.update(overrides)
    return document


def write_platforms(root: Path):
    for arch in ARCHITECTURES:
        target = root / f'platform-{arch}'
        target.mkdir(parents=True)
        target.joinpath('digests.json').write_bytes(
            json_bytes(platform_record(arch, child_manifest(CONFIG[arch]))))


# Release metadata contract -------------------------------------------------

def test_existing_v1_release_remains_valid():
    release = json.loads(RELEASE.read_text())
    assert release['schema_version'] == 1
    normalized = validate_release(release)
    assert normalized['image'] == release['image'] and normalized['platforms'] == {}


def test_v2_release_records_index_and_distinct_platform_digests(tmp_path, monkeypatch):
    document = release_v2()
    normalized = validate_release(document)
    assert normalized['platforms']['amd64']['image'].endswith('6' * 64)
    path = tmp_path / 'release.json'
    path.write_bytes(json_bytes(document))
    monkeypatch.setattr(preparation, 'RELEASE_FILE', path)
    assert preparation.released_runner_image() == document['image']


@pytest.mark.parametrize('mutation', [
    lambda value: value.update(schema_version=3),
    lambda value: value.update(schema_version=True),
    lambda value: value.update(forge_revision='0' * 40),
    lambda value: value.update(version='latest'),
    lambda value: value.update(tag=value['tag'] + '-wrong'),
    lambda value: value.update(source_revision=None),
    lambda value: value['platforms'].pop('arm64'),
    lambda value: value['platforms'].update(riscv={'image': value['image']}),
    lambda value: value['platforms']['amd64'].update(image=value['image']),
    lambda value: value['platforms']['arm64'].update(
        image=value['platforms']['arm64']['image'].replace('7' * 64, '6' * 64)),
    lambda value: value['platforms']['amd64'].update(image='quay.io/other/runner@sha256:' + '8' * 64),
])
def test_release_contract_rejects_inconsistent_values(mutation):
    document = release_v2()
    mutation(document)
    with pytest.raises(ValueError, match='runner release'):
        validate_release(document)


# Published image and index verification ------------------------------------

def test_verify_published_binds_registry_manifest_to_scanned_config():
    manifest = child_manifest(CONFIG['amd64'])
    record = verify_published(manifest, CONFIG['amd64'], EXECUTED, 'amd64', FORGE_REVISION)
    assert record['manifest_digest'] == digest(manifest)
    assert record['config_digest'] == CONFIG['amd64']
    assert record['executed_image_id'] == EXECUTED
    assert validate_platform_record(record) == record


def test_verify_published_allows_a_classic_store_where_ids_agree():
    manifest = child_manifest(CONFIG['amd64'])
    record = verify_published(manifest, CONFIG['amd64'], CONFIG['amd64'], 'amd64', FORGE_REVISION)
    assert record['executed_image_id'] == record['config_digest']


def test_verify_published_rejects_an_index_or_attestation_with_guidance():
    children = {arch: child_manifest(CONFIG[arch]) for arch in ARCHITECTURES}
    index = index_document({arch: digest(children[arch]) for arch in ARCHITECTURES})
    with pytest.raises(ReleaseError, match='provenance'):
        verify_published(index, CONFIG['amd64'], EXECUTED, 'amd64', FORGE_REVISION)


def test_verify_published_rejects_another_config_or_forge_revision():
    manifest = child_manifest(CONFIG['arm64'])
    with pytest.raises(ReleaseError, match='scanned image config'):
        verify_published(manifest, CONFIG['amd64'], EXECUTED, 'arm64', FORGE_REVISION)
    with pytest.raises(ReleaseError, match='Forge revision'):
        verify_published(manifest, CONFIG['arm64'], EXECUTED, 'arm64', '0' * 40)


@pytest.mark.parametrize('config,executed', [
    ('sha256:abc', EXECUTED),
    (CONFIG['amd64'], 'sha256:abc'),
])
def test_verify_published_rejects_invalid_identities(config, executed):
    with pytest.raises(ReleaseError, match='SHA-256 digest'):
        verify_published(child_manifest(CONFIG['amd64']), config, executed,
                         'amd64', FORGE_REVISION)


def test_verify_index_and_children_bind_the_full_chain():
    children = {arch: child_manifest(CONFIG[arch]) for arch in ARCHITECTURES}
    records = {arch: platform_record(arch, children[arch]) for arch in ARCHITECTURES}
    index = index_document({arch: records[arch]['manifest_digest'] for arch in ARCHITECTURES})
    assert verify_index(index, digest(index), records) == {
        arch: records[arch]['manifest_digest'] for arch in ARCHITECTURES}
    for arch in ARCHITECTURES:
        verify_child(children[arch], records[arch])


@pytest.mark.parametrize('mutation', [
    lambda value: value.update(schemaVersion=1),
    lambda value: value['manifests'][0]['platform'].update(os='windows'),
    lambda value: value['manifests'][0]['platform'].update(architecture='riscv64'),
    lambda value: value['manifests'][0].update(digest='sha256:' + '9' * 64),
    lambda value: value['manifests'].append(copy.deepcopy(value['manifests'][0])),
    lambda value: value['manifests'].pop(),
])
def test_verify_index_rejects_mismatched_or_unsupported_platforms(mutation):
    children = {arch: child_manifest(CONFIG[arch]) for arch in ARCHITECTURES}
    records = {arch: platform_record(arch, children[arch]) for arch in ARCHITECTURES}
    document = json.loads(index_document({arch: records[arch]['manifest_digest'] for arch in ARCHITECTURES}))
    mutation(document)
    index = json_bytes(document)
    with pytest.raises(ReleaseError):
        verify_index(index, digest(index), records)


def test_verify_index_rejects_changed_bytes_with_the_same_document():
    children = {arch: child_manifest(CONFIG[arch]) for arch in ARCHITECTURES}
    records = {arch: platform_record(arch, children[arch]) for arch in ARCHITECTURES}
    index = index_document({arch: records[arch]['manifest_digest'] for arch in ARCHITECTURES})
    with pytest.raises(ReleaseError, match='index digest'):
        verify_index(index + b'\n', digest(index), records)


# Index assembly and candidate recording ------------------------------------

class FakeEngine:
    def __init__(self, index, children, *, tag_exists=False, tag_error=b'not found'):
        self.index, self.children = index, children
        self.tag_exists = tag_exists
        self.tag_error = tag_error
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:4] == ['docker', 'buildx', 'imagetools', 'inspect'] and argv[-1] != '--raw':
            if self.tag_exists:
                return SimpleNamespace(returncode=0, stdout=b'{}', stderr=b'')
            return SimpleNamespace(returncode=1, stdout=b'', stderr=self.tag_error)
        if argv[3] == 'create':
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        if argv[-1] == '--raw' and '@sha256:' not in argv[-2]:
            return SimpleNamespace(returncode=0, stdout=self.index, stderr=b'')
        reference = argv[-2]
        if reference in self.children:
            return SimpleNamespace(returncode=0, stdout=self.children[reference], stderr=b'')
        return SimpleNamespace(returncode=1, stdout=b'', stderr=b'')


def fixture_release(tmp_path):
    children = {arch: child_manifest(CONFIG[arch]) for arch in ARCHITECTURES}
    references = {arch: REPOSITORY + '@' + digest(children[arch]) for arch in ARCHITECTURES}
    records = {arch: platform_record(arch, children[arch]) for arch in ARCHITECTURES}
    for arch in ARCHITECTURES:
        target = tmp_path / 'platforms' / f'platform-{arch}'
        target.mkdir(parents=True)
        target.joinpath('digests.json').write_bytes(json_bytes(records[arch]))
    index = index_document({arch: records[arch]['manifest_digest'] for arch in ARCHITECTURES})
    engine = FakeEngine(index, {references[arch]: children[arch] for arch in ARCHITECTURES})
    return engine, records


def test_assemble_publishes_verified_children_and_records_candidate(tmp_path):
    engine, records = fixture_release(tmp_path)
    summary = assemble(REPOSITORY, 'v0.2.0', tmp_path / 'platforms', '5' * 40,
                       tmp_path / 'dist', run=engine)
    document = json.loads((tmp_path / 'dist/release.json').read_text())
    assert document == summary['release']
    assert document['image'] == REPOSITORY + '@' + summary['index_digest']
    assert (tmp_path / 'dist/index.json').read_bytes() == engine.index
    assert document['platforms']['amd64']['image'].endswith(records['amd64']['manifest_digest'])
    assert document['source_revision'] == '5' * 40
    create = next(call for call in engine.calls if call[3] == 'create')
    assert create[4:6] == ['-t', REPOSITORY + ':v0.2.0']
    assert all('@sha256:' in reference for reference in create[6:])


def test_assemble_refuses_to_overwrite_an_existing_release_tag(tmp_path):
    write_platforms(tmp_path / 'platforms')
    engine = FakeEngine(b'', {}, tag_exists=True)
    with pytest.raises(ReleaseError, match='refusing to overwrite'):
        assemble(REPOSITORY, 'v0.2.0', tmp_path / 'platforms', '5' * 40,
                 tmp_path / 'dist', run=engine)


def test_assemble_fails_closed_when_tag_state_cannot_be_confirmed(tmp_path):
    write_platforms(tmp_path / 'platforms')
    engine = FakeEngine(b'', {}, tag_error=b'denied: requested access is denied')
    with pytest.raises(ReleaseError, match='cannot confirm the tag is absent'):
        assemble(REPOSITORY, 'v0.2.0', tmp_path / 'platforms', '5' * 40,
                 tmp_path / 'dist', run=engine)
    assert not any(call[3] == 'create' for call in engine.calls)


@pytest.mark.parametrize('message', [
    b'ERROR: ghcr.io/example/runner:v0.2.0: not found\n',
    b'manifest unknown',
    b'no such manifest: v0.2.0',
])
def test_guard_tag_allows_only_a_confirmed_absent_tag(message):
    result = SimpleNamespace(returncode=1, stdout=b'', stderr=message)
    guard_tag(REPOSITORY + ':v0.2.0', run=lambda *args, **kwargs: result)


def test_guard_tag_refuses_an_existing_tag():
    result = SimpleNamespace(returncode=0, stdout=b'...', stderr=b'')
    with pytest.raises(ReleaseError, match='refusing to overwrite'):
        guard_tag(REPOSITORY + ':v0.2.0', run=lambda *args, **kwargs: result)


@pytest.mark.parametrize('message', [
    b'denied: requested access to the resource is denied',
    b'Get "https://ghcr.io/v2/": net/http: TLS handshake timeout',
    b'',
])
def test_guard_tag_fails_closed_on_unconfirmed_absence(message):
    result = SimpleNamespace(returncode=1, stdout=b'', stderr=message)
    with pytest.raises(ReleaseError, match='cannot confirm the tag is absent'):
        guard_tag(REPOSITORY + ':v0.2.0', run=lambda *args, **kwargs: result)


@pytest.mark.parametrize('reference', [REPOSITORY, REPOSITORY + '@sha256:' + '4' * 64])
def test_guard_tag_requires_a_tag_without_a_digest(reference):
    with pytest.raises(ReleaseError, match='tagged reference'):
        guard_tag(reference, run=lambda *args, **kwargs: SimpleNamespace(returncode=1))


def test_assemble_rejects_missing_platform_records(tmp_path):
    with pytest.raises(ReleaseError, match='missing or invalid platform'):
        assemble(REPOSITORY, 'v0.2.0', tmp_path / 'absent', '5' * 40,
                 tmp_path / 'dist', run=FakeEngine(b'', {}))


def test_release_document_never_invents_digests():
    with pytest.raises(ReleaseError, match='version'):
        release_document(REPOSITORY, 'latest', 'sha256:' + '4' * 64,
                         {arch: {'manifest_digest': 'sha256:' + str(index) * 64}
                          for index, arch in enumerate(ARCHITECTURES)}, '5' * 40)


# Workflow safety -----------------------------------------------------------

def workflow():
    # PyYAML resolves the unquoted `on` key as True under YAML 1.1.
    document = yaml.safe_load(WORKFLOW.read_text())
    document['on'] = document.get('on', document.get(True))
    return document


def steps_of(document, job):
    return document['jobs'][job]['steps']


def test_release_workflow_is_manual_main_only_and_never_pr_driven():
    document = workflow()
    assert set(document['on']) == {'workflow_dispatch'}, 'release must not run on push or pull_request'
    assert document['permissions'] == {'contents': 'read'}
    for job in ('guard', 'publish-platform', 'assemble-index'):
        condition = document['jobs'][job].get('if', '')
        assert 'refs/heads/main' in condition and 'openshift-psap/inference-playbooks' in condition
    assert document['jobs']['publish-platform']['needs'] == 'guard'
    assert document['jobs']['assemble-index']['needs'] == 'publish-platform'


def test_release_workflow_permissions_and_pinned_actions_are_scoped():
    document = workflow()
    for job in ('publish-platform', 'assemble-index'):
        assert document['jobs'][job]['permissions'] == {'contents': 'read', 'packages': 'write'}
    assert 'packages' not in document['jobs']['guard'].get('permissions', {})
    for job in document['jobs'].values():
        for step in job['steps']:
            if 'uses' in step:
                assert re.search(r'@[0-9a-f]{40}$', step['uses']), step['uses']
            if 'actions/checkout@' in step.get('uses', ''):
                assert step['with']['persist-credentials'] is False


def test_release_workflow_scans_and_smokes_the_frozen_id_before_pushing():
    document = workflow()
    steps = steps_of(document, 'publish-platform')
    runs = [step.get('run', '') for step in steps]
    build = next(index for index, run in enumerate(runs) if 'Containerfile' in run)
    freeze = next(index for index, run in enumerate(runs) if 'Freeze the exact image' in steps[index].get('name', ''))
    scan = next(index for index, run in enumerate(runs) if 'tools/scan_runner_image.py' in run)
    smoke = next(index for index, run in enumerate(runs) if 'tests/contributor_import_smoke.py' in run)
    cleanup = next(index for index, run in enumerate(runs)
                   if 'contributor_guidellm_cleanup_smoke.py' in run)
    recheck = next(index for index, run in enumerate(runs) if 'Re-check' in steps[index].get('name', ''))
    guard = next(index for index, run in enumerate(runs) if 'guard-tag' in run)
    push = next(index for index, run in enumerate(runs) if 'docker push' in run)
    verify = next(index for index, run in enumerate(runs) if 'verify-published' in run)
    assert build < scan < smoke < cleanup < recheck < guard < push < verify
    # The frozen ID itself is architecture-checked before any publication step.
    assert '{{.Architecture}}' in runs[freeze] and '"$image_id"' in runs[freeze]
    # The pushed manifest must be one image, not an attestation index.
    assert '--provenance=false' in runs[build] and '--sbom=false' in runs[build]
    # Execution and scanning select the frozen ID, never the mutable build tag.
    assert '"$IMAGE_ID"' in runs[scan] and 'release-${{ matrix.arch }}' not in runs[scan]
    assert '"$IMAGE_ID"' in runs[smoke] and 'release-${{ matrix.arch }}' not in runs[smoke]
    assert '"$IMAGE_ID"' in runs[cleanup]
    assert '"$IMAGE_ID"' in runs[recheck]
    # Publication identity is the exported config digest, recorded separately.
    assert '--identity-output' in runs[scan] and 'CONFIG_DIGEST' in runs[scan + 1]
    assert '--config-digest "$CONFIG_DIGEST"' in runs[verify]
    assert '--executed-image-id "$IMAGE_ID"' in runs[verify]
    assert 'guard-tag' in runs[guard]
    for job in ('guard', 'assemble-index'):
        assert all('docker push' not in step.get('run', '') for step in steps_of(document, job))


def test_release_workflow_records_only_a_candidate_artifact():
    document = workflow()
    steps = steps_of(document, 'assemble-index')
    assert any('tools/runner_release.py' in step.get('run', '') and 'assemble' in step.get('run', '')
               for step in steps)
    upload = next(step for step in steps if step.get('uses', '').startswith('actions/upload-artifact@'))
    assert upload['with']['name'] == 'runner-release-candidate'
    assert upload['with']['path'] == 'dist/'
    text = WORKFLOW.read_text()
    assert 'git commit' not in text and 'git push' not in text and 'git tag' not in text
    assert 'git diff --exit-code -- benchmarks/runner/release.json' in text
    assert 'no default changed' in text


def test_release_workflow_proves_each_architecture_natively():
    document = workflow()
    matrix = document['jobs']['publish-platform']['strategy']['matrix']['include']
    assert {entry['arch']: entry['runs-on'] for entry in matrix} == {
        'amd64': 'ubuntu-latest', 'arm64': 'ubuntu-24.04-arm'}
    runs = '\n'.join(step.get('run', '') for step in steps_of(document, 'publish-platform'))
    assert '--arch "${{ matrix.arch }}"' in runs
    assert 'guidellm-smoke aiperf-smoke' in runs
    assert 'dev.inference-playbooks.forge-revision' in runs


# Generated launcher platform rules -----------------------------------------

def fake_command(path: Path, body: str):
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def package(tmp_path):
    repo, _, _ = build_source_repo(tmp_path / 'src')
    output = tmp_path / 'package'
    prepare(repo, 'models/test/recipes/test-recipe/recipe.yaml', 'vllm', 'v0.24.0',
            'guidellm-smoke', 'ghcr.io/example/runner@sha256:' + 'a' * 64, HARNESS, output)
    return output


def run_launcher(tmp_path, package, *, platform, engine_name='podman', info=0, explicit_engine=False):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    fake_command(bin_dir / 'uname', f'echo {platform}\n')
    capture = tmp_path / 'engine-args.json'
    engine = fake_command(bin_dir / engine_name, f'''if [ "${{1:-}}" = info ]; then exit {info}; fi
python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@" > {capture}
''')
    kubeconfig = tmp_path / 'acceptance.kubeconfig'
    kubeconfig.write_text('synthetic config\n')
    output = tmp_path / 'output'
    environment = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
    environment.pop('CONTAINER_ENGINE', None)
    if explicit_engine:
        environment['CONTAINER_ENGINE'] = str(engine)
    result = subprocess.run([str(package / 'run.sh'), str(kubeconfig), 'context', 'benchmark', str(output)],
                            capture_output=True, text=True, env=environment)
    return result, capture, output


def test_launcher_selects_podman_and_drops_macos_incompatible_flags(tmp_path, package):
    result, capture, output = run_launcher(tmp_path, package, platform='Darwin')
    assert result.returncode == 0, result.stderr
    arguments = json.loads(capture.read_text())
    assert '--userns=keep-id' in arguments
    assert f'{os.getuid()}:{os.getgid()}' in arguments
    assert not any(argument.startswith('--network') for argument in arguments), \
        'host networking would name the podman machine namespace, not the Mac'
    mounts = [arguments[index + 1] for index, arg in enumerate(arguments) if arg == '-v']
    assert all(',Z' not in mount for mount in mounts), 'SELinux relabeling does not apply to virtiofs'
    assert any(mount.endswith(':/inputs:ro') for mount in mounts)
    assert any(mount.endswith(':/outputs') for mount in mounts)
    assert output.is_dir() and (output.stat().st_mode & 0o777) == 0o700


def test_launcher_rejects_docker_on_macos_with_a_podman_specific_error(tmp_path, package):
    result, capture, output = run_launcher(tmp_path, package, platform='Darwin',
                                           engine_name='docker', explicit_engine=True)
    assert result.returncode == 2
    assert 'Podman' in result.stderr and 'Docker Desktop' in result.stderr
    assert not capture.exists(), 'no engine may run before the macOS engine check'
    assert not output.exists()


def test_launcher_reports_an_unreachable_podman_machine(tmp_path, package):
    result, _, _ = run_launcher(tmp_path, package, platform='Darwin', info=1)
    assert result.returncode == 2
    assert 'podman machine start' in result.stderr


def test_launcher_keeps_linux_docker_and_host_networking(tmp_path, package):
    result, capture, _ = run_launcher(tmp_path, package, platform='Linux', engine_name='docker',
                                      explicit_engine=True)
    assert result.returncode == 0, result.stderr
    arguments = json.loads(capture.read_text())
    assert '--network=host' in arguments
    assert '--userns=keep-id' not in arguments
    mounts = [arguments[index + 1] for index, arg in enumerate(arguments) if arg == '-v']
    assert any(mount.endswith(':/inputs:ro,Z') for mount in mounts)
    # The writable output mount needs its relabel option after a second colon:
    # '<target>,Z' would mount a path literally named '<target>,Z' and leave
    # /outputs missing, so the container cannot write its evidence.
    assert any(mount.endswith(':/outputs:Z') for mount in mounts), \
        'output mount options must follow a colon, not the container path'


def test_launcher_uses_portable_path_resolution(package):
    script = (package / 'run.sh').read_text()
    assert 'realpath -m' not in script
    assert 'pwd -P' in script
    subprocess.run(['sh', '-n', str(package / 'run.sh')], check=True)


def test_container_smoke_driver_applies_the_same_macos_mount_rule(tmp_path):
    command = driver.container_command('podman', 'localhost/runner:ci', tmp_path / 'p', tmp_path / 'o',
                                       tmp_path / 's', 'arm64', platform='darwin')
    assert '--userns=keep-id' in command
    assert all(',Z' not in argument for argument in command)
    with pytest.raises(SystemExit, match='Podman'):
        driver.container_command('docker', 'localhost/runner:ci', tmp_path / 'p', tmp_path / 'o',
                                 tmp_path / 's', 'arm64', platform='darwin')


def test_macos_acceptance_gate_refuses_non_apple_hardware():
    if sys.platform == 'darwin':
        pytest.skip('this check is for non-macOS development hosts')
    result = subprocess.run([sys.executable, str(REPO / 'tests/contributor_macos_podman_smoke.py'),
                             '--image', 'ghcr.io/example/runner@sha256:' + 'a' * 64],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'requires Apple Silicon macOS' in result.stderr
