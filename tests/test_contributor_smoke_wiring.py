"""Regression coverage for the container/host and native macOS smoke wiring."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tests'))

import contributor_import_smoke as driver
import contributor_macos_smoke as native
from contributor_fixtures import HARNESS_IMAGES, SMOKE_WORKLOADS, build_source_repo
from prepare_contributor import prepare


def test_native_smoke_passes_the_prepared_package_arguments():
    command = native.smoke_command('/python3', '/smoke.py', Path('/pkg'), Path('/out'))
    assert command[1] == '/smoke.py'
    assert command[2:4] == ['--request', '/pkg/request.json']
    assert command[4:6] == ['--inputs', '/pkg/playbooks']
    assert command[6:8] == ['--output', '/out']
    # The workload now comes from the prepared request, never an environment hook.
    assert 'SMOKE_HARNESS' not in ' '.join(command)


@pytest.mark.parametrize('workload', SMOKE_WORKLOADS)
def test_native_smoke_prepares_a_committed_package_per_workload(tmp_path, workload):
    repo, _, _ = build_source_repo(tmp_path)
    native.commit_native_runner_sources(repo)
    package = tmp_path / f'package-{workload}'
    request = prepare(repo, native.RECIPE, 'vllm', 'v0.24.0', workload, None,
                      HARNESS_IMAGES[workload], package, runner_mode='macos-arm64')
    assert request['runner_mode'] == 'macos-arm64' and 'runner_image' not in request
    assert request['workload'] == workload
    assert (package / 'request.json').is_file()
    assert (package / 'runner/requirements.lock').is_file()
    assert (package / 'playbooks' / request['recipe_path']).is_file()
    assert str(package / 'request.json') in native.smoke_command('/python3', '/smoke.py', package, tmp_path / 'out')


def test_runtime_execution_is_frozen_to_the_inspected_image_id(monkeypatch):
    config_id = 'sha256:' + 'c' * 64
    monkeypatch.setattr(driver, 'inspect', lambda engine, image, template: config_id)
    runtime = driver.immutable_image_id('podman', 'localhost/contributor-runner:ci')
    assert runtime == config_id
    command = driver.container_command('podman', runtime, '/pkg', '/out', '/smoke.py', 'arm64')
    assert runtime in command
    assert 'localhost/contributor-runner:ci' not in command, 'a mutable tag must not select runtime bytes'


def test_linux_podman_mounts_keep_options_after_a_second_colon():
    command = driver.container_command('podman', 'localhost/runner:ci', '/pkg', '/out',
                                       '/smoke.py', 'amd64', platform='linux')
    volumes = [command[index + 1] for index, argument in enumerate(command) if argument == '-v']
    assert volumes == ['/pkg:/inputs:ro,Z', '/out:/outputs:Z', '/smoke.py:/smoke.py:ro,Z']


def test_linux_docker_mounts_do_not_request_selinux_relabeling():
    command = driver.container_command('docker', 'localhost/runner:ci', '/pkg', '/out',
                                       '/smoke.py', 'amd64', platform='linux')
    volumes = [command[index + 1] for index, argument in enumerate(command) if argument == '-v']
    assert volumes == ['/pkg:/inputs:ro', '/out:/outputs', '/smoke.py:/smoke.py:ro']


def test_immutable_image_id_accepts_podman_bare_hex(monkeypatch):
    monkeypatch.setattr(driver, 'inspect', lambda engine, image, template: 'd' * 64)
    assert driver.immutable_image_id('podman', 'localhost/runner:ci') == 'sha256:' + 'd' * 64


@pytest.mark.parametrize('value', ['', 'sha256:' + 'g' * 64, 'sha256:abc'])
def test_immutable_image_id_rejects_unverifiable_metadata(monkeypatch, value):
    monkeypatch.setattr(driver, 'inspect', lambda engine, image, template: value)
    with pytest.raises(SystemExit):
        driver.immutable_image_id('docker', 'localhost/runner:ci')


def test_local_manifest_digest_reads_engine_specific_metadata(monkeypatch):
    digest = 'sha256:' + 'e' * 64
    monkeypatch.setattr(driver, 'inspect', lambda engine, image, template: digest)
    assert driver.local_manifest_digest('podman', 'localhost/runner:ci') == digest
    monkeypatch.setattr(driver, 'inspect',
                        lambda engine, image, template: '["localhost/runner@' + digest + '"]')
    assert driver.local_manifest_digest('docker', 'localhost/runner:ci') == digest
    monkeypatch.setattr(driver, 'inspect', lambda engine, image, template: '[]')
    assert driver.local_manifest_digest('docker', 'localhost/runner:ci') is None


def test_request_reference_uses_the_manifest_digest_when_available(monkeypatch):
    digest = 'sha256:' + 'f' * 64
    monkeypatch.setattr(driver, 'local_manifest_digest', lambda engine, image: digest)
    reference, source = driver.request_runner_image('podman', 'localhost/runner:ci')
    assert reference == 'localhost/runner@' + digest
    assert source == 'local-manifest-digest'


def test_request_reference_never_presents_a_config_id_as_a_manifest_digest(monkeypatch):
    config_id = 'sha256:' + 'c' * 64

    def inspect(engine, image, template):
        return config_id if template == '{{.Id}}' else '[]'

    monkeypatch.setattr(driver, 'inspect', inspect)
    reference, source = driver.request_runner_image('docker', 'localhost/runner:ci')
    assert reference == driver.SYNTHETIC_RUNNER_IMAGE
    assert source == 'synthetic-fixture'
    assert config_id.split(':')[1] not in reference
