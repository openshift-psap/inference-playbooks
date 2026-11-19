"""Architecture pins for the runner image; no container engine or network needed.

These tests cover the build-time contract only. Real per-architecture execution
is proven by the CI matrix and the container smoke tests.
"""
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'benchmarks/runner'))

import verify_arch


PLATFORMS = REPO / 'benchmarks/runner/platforms.json'
CONTAINERFILE = REPO / 'benchmarks/runner/Containerfile'
WORKFLOW = REPO / '.github/workflows/recipe-evidence.yml'

# Published AMD64 client: must stay byte-identical to the previous runner pin.
AMD64_URL = 'https://mirror.openshift.com/pub/openshift-v4/clients/ocp/4.19.0/openshift-client-linux-4.19.0.tar.gz'
AMD64_SHA256 = 'c90254d7ededb6da4f6d3911fbe6d7d2e35654a480d684f5d2e502e8be7cd6fd'
ARM64_URL = ('https://mirror.openshift.com/pub/openshift-v4/aarch64/clients/ocp/4.19.0/'
             'openshift-client-linux-arm64-rhel9-4.19.0.tar.gz')
ARM64_SHA256 = '45425ad184d0f12f34d7038ed7061880ec5bb083356f4d1c735047e961c0042b'


def elf_binary(machine, *, elf_class=2, encoding=1, size=64):
    header = bytearray(size)
    header[:4] = b'\x7fELF'
    header[4] = elf_class
    header[5] = encoding
    header[6] = 1
    header[18:20] = int(machine).to_bytes(2, 'little' if encoding == 1 else 'big')
    return bytes(header)


def write_archive(path, members):
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def fake_sources(tmp_path, arch, binary, *, url=None, member_name='oc'):
    archive = tmp_path / f'{arch}-client.tar.gz'
    write_archive(archive, [(member_name, binary)])
    sources = {'ocp_version': '4.19.0', 'platforms': {arch: {
        'url': url or archive.as_uri(),
        'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'archive_member': 'oc', 'notes': 'fixture',
    }}}
    return sources


class Probe:
    def __init__(self, returncode=0, stdout='Client Version: 4.19.0\nKustomize Version: v5.5.0\n'):
        self.calls = []
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr='')

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return self.result


def test_source_table_pins_both_linux_architectures():
    sources = verify_arch.load_sources(PLATFORMS)
    assert sources['ocp_version'] == '4.19.0'
    assert set(sources['platforms']) == {'amd64', 'arm64'}
    amd64, arm64 = sources['platforms']['amd64'], sources['platforms']['arm64']
    assert (amd64['url'], amd64['sha256'], amd64['archive_member']) == (AMD64_URL, AMD64_SHA256, 'oc')
    assert (arm64['url'], arm64['sha256'], arm64['archive_member']) == (ARM64_URL, ARM64_SHA256, 'oc')
    # Different binaries, one fixed pin each; never the same archive twice.
    assert len({entry['url'] for entry in sources['platforms'].values()}) == 2
    assert len({entry['sha256'] for entry in sources['platforms'].values()}) == 2


def test_source_table_records_the_rhel9_libc_requirement():
    arm64 = verify_arch.load_sources(PLATFORMS)['platforms']['arm64']
    assert 'glibc' in arm64['notes'] and '2.34' in arm64['notes']
    assert 'bookworm' in CONTAINERFILE.read_text(), 'base userland must satisfy the ARM64 client'


def test_architecture_contract_maps_elf_machines():
    assert verify_arch.ARCHITECTURES == {
        'amd64': {'machine': 'x86_64', 'elf_machine': 62},
        'arm64': {'machine': 'aarch64', 'elf_machine': 183},
    }


@pytest.mark.parametrize('entry', [
    {'url': AMD64_URL, 'sha256': AMD64_SHA256.upper(), 'archive_member': 'oc'},
    {'url': AMD64_URL, 'sha256': AMD64_SHA256[:-1], 'archive_member': 'oc'},
    {'url': 'http://mirror.example/clients.tar.gz', 'sha256': AMD64_SHA256, 'archive_member': 'oc'},
    {'url': AMD64_URL, 'sha256': AMD64_SHA256},
    {'url': AMD64_URL, 'sha256': AMD64_SHA256, 'archive_member': 'bin/oc'},
    {'url': AMD64_URL, 'sha256': AMD64_SHA256, 'archive_member': ''},
    {'machine': 'aarch64', 'elf_machine': 183, 'url': AMD64_URL,
     'sha256': AMD64_SHA256, 'archive_member': 'oc'},
    {'machine': 'x86_64', 'elf_machine': 183, 'url': AMD64_URL,
     'sha256': AMD64_SHA256, 'archive_member': 'oc'},
])
def test_source_table_rejects_unpinned_or_inconsistent_entries(tmp_path, entry):
    table = {'schema_version': 1, 'ocp_version': '4.19.0', 'platforms': {'amd64': entry}}
    path = tmp_path / 'platforms.json'
    path.write_text(json.dumps(table))
    with pytest.raises(verify_arch.PlatformError):
        verify_arch.load_sources(path)


@pytest.mark.parametrize('table', [
    {'schema_version': 2, 'ocp_version': '4.19.0', 'platforms': {}},
    {'schema_version': 1, 'platforms': {'amd64': {}}},
    {'schema_version': 1, 'ocp_version': 'latest', 'platforms': {'amd64': {}}},
    {'schema_version': 1, 'ocp_version': '4.19.0', 'platforms': {}},
    {'schema_version': 1, 'ocp_version': '4.19.0', 'platforms': {'armv7': {
        'machine': 'armv7l', 'elf_machine': 40, 'url': AMD64_URL,
        'sha256': AMD64_SHA256, 'archive_member': 'oc'}}},
    {'schema_version': 1, 'ocp_version': '4.19.0', 'platforms': {
        'amd64': {'machine': 'x86_64', 'elf_machine': 62, 'url': AMD64_URL,
                  'sha256': AMD64_SHA256, 'archive_member': 'oc'},
        'arm64': {'machine': 'aarch64', 'elf_machine': 183, 'url': AMD64_URL,
                  'sha256': ARM64_SHA256, 'archive_member': 'oc'}}},
])
def test_source_table_rejects_unsupported_tables(tmp_path, table):
    path = tmp_path / 'platforms.json'
    path.write_text(json.dumps(table))
    with pytest.raises(verify_arch.PlatformError):
        verify_arch.load_sources(path)


def test_source_table_rejects_unreadable_json(tmp_path):
    path = tmp_path / 'platforms.json'
    path.write_text('{not json')
    with pytest.raises(verify_arch.PlatformError):
        verify_arch.load_sources(path)


@pytest.mark.parametrize('machine,expected', [(62, 62), (183, 183)])
def test_elf_machine_reads_little_endian_headers(machine, expected):
    assert verify_arch.elf_machine(elf_binary(machine)) == expected


def test_elf_machine_honours_byte_order():
    import struct
    header = bytearray(elf_binary(183))
    header[5] = 2
    header[18:20] = struct.pack('>H', 183)
    assert verify_arch.elf_machine(bytes(header)) == 183


@pytest.mark.parametrize('header', [
    b'',
    b'not an elf image at all',
    elf_binary(62)[:12],
    elf_binary(62, elf_class=1),
    elf_binary(62, encoding=0),
])
def test_elf_machine_rejects_non_elf_or_32_bit_binaries(header):
    with pytest.raises(verify_arch.PlatformError):
        verify_arch.elf_machine(header)


def test_verify_machine_rejects_foreign_architecture():
    verify_arch.verify_machine(elf_binary(62), 'amd64')
    with pytest.raises(verify_arch.PlatformError):
        verify_arch.verify_machine(elf_binary(62), 'arm64')


def test_install_verifies_checksum_machine_and_runtime(tmp_path, monkeypatch):
    sources = fake_sources(tmp_path, 'arm64', elf_binary(183))
    probe = Probe()
    monkeypatch.setattr(verify_arch.subprocess, 'run', probe)
    destination = tmp_path / 'bin/oc'
    verify_arch.install(sources, 'arm64', 'arm64', destination)
    assert destination.read_bytes()[18:20] == (183).to_bytes(2, 'little')
    assert destination.stat().st_mode & 0o777 == 0o755
    assert probe.calls == [[str(destination), 'version', '--client']]


def test_install_accepts_dot_slash_archive_member(tmp_path, monkeypatch):
    sources = fake_sources(tmp_path, 'amd64', elf_binary(62), member_name='./oc')
    monkeypatch.setattr(verify_arch.subprocess, 'run', Probe())
    destination = tmp_path / 'oc'
    verify_arch.install(sources, 'amd64', 'amd64', destination)
    assert destination.is_file()


def test_install_rejects_checksum_mismatch_before_installing(tmp_path, monkeypatch):
    sources = fake_sources(tmp_path, 'amd64', elf_binary(62))
    sources['platforms']['amd64']['sha256'] = '0' * 64
    probe = Probe()
    monkeypatch.setattr(verify_arch.subprocess, 'run', probe)
    destination = tmp_path / 'oc'
    with pytest.raises(verify_arch.PlatformError, match='checksum'):
        verify_arch.install(sources, 'amd64', 'amd64', destination)
    assert not destination.exists() and probe.calls == []


def test_install_rejects_foreign_binary_in_matching_archive(tmp_path, monkeypatch):
    sources = fake_sources(tmp_path, 'arm64', elf_binary(62))
    probe = Probe()
    monkeypatch.setattr(verify_arch.subprocess, 'run', probe)
    destination = tmp_path / 'oc'
    with pytest.raises(verify_arch.PlatformError, match='ELF machine'):
        verify_arch.install(sources, 'arm64', 'arm64', destination)
    assert not destination.exists() and probe.calls == []


@pytest.mark.parametrize('member_name', ['missing', 'README.md'])
def test_install_rejects_missing_archive_member(tmp_path, monkeypatch, member_name):
    # The pinned archive member is always `oc`; anything else must fail closed.
    sources = fake_sources(tmp_path, 'amd64', elf_binary(62), member_name=member_name)
    monkeypatch.setattr(verify_arch.subprocess, 'run', Probe())
    destination = tmp_path / 'oc'
    with pytest.raises(verify_arch.PlatformError, match='missing or ambiguous'):
        verify_arch.install(sources, 'amd64', 'amd64', destination)
    assert not destination.exists()


def test_install_rejects_ambiguous_archive_member(tmp_path, monkeypatch):
    archive = tmp_path / 'duplicate.tar.gz'
    write_archive(archive, [('oc', elf_binary(62)), ('oc', elf_binary(62))])
    sources = {'ocp_version': '4.19.0', 'platforms': {'amd64': {
        'url': archive.as_uri(), 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'archive_member': 'oc', 'notes': ''}}}
    monkeypatch.setattr(verify_arch.subprocess, 'run', Probe())
    with pytest.raises(verify_arch.PlatformError, match='ambiguous'):
        verify_arch.install(sources, 'amd64', 'amd64', tmp_path / 'oc')


def test_install_rejects_build_target_that_disagrees_with_the_image(tmp_path, monkeypatch):
    sources = fake_sources(tmp_path, 'arm64', elf_binary(183), url='file:///nonexistent.tar.gz')
    probe = Probe()
    monkeypatch.setattr(verify_arch.subprocess, 'run', probe)
    with pytest.raises(verify_arch.PlatformError, match='does not match image architecture'):
        verify_arch.install(sources, 'arm64', 'amd64', tmp_path / 'oc')
    assert probe.calls == []


def test_install_rejects_platforms_without_a_pinned_client(tmp_path):
    with pytest.raises(verify_arch.PlatformError, match='no pinned'):
        verify_arch.install({'ocp_version': '4.19.0', 'platforms': {}}, 'arm64', 'arm64', tmp_path / 'oc')


@pytest.mark.parametrize('result', [
    SimpleNamespace(returncode=1, stdout='', stderr='private loader output'),
    SimpleNamespace(returncode=0, stdout='Client Version: 4.18.0\n', stderr=''),
])
def test_install_rejects_a_client_that_cannot_run_or_reports_another_version(tmp_path, monkeypatch, result):
    sources = fake_sources(tmp_path, 'amd64', elf_binary(62))
    monkeypatch.setattr(verify_arch.subprocess, 'run', lambda *args, **kwargs: result)
    with pytest.raises(verify_arch.PlatformError):
        verify_arch.install(sources, 'amd64', 'amd64', tmp_path / 'oc')


@pytest.mark.parametrize('stdout', [
    'Client Version: 4.19.01\n',
    'Client Version: 4.19.0-rc.1\n',
    'Client Version: v4.19.0\n',
    'Client Version: 4.19.0 (build fixture)\n',
])
def test_probe_requires_the_exact_client_version_line(tmp_path, monkeypatch, stdout):
    sources = fake_sources(tmp_path, 'amd64', elf_binary(62))
    monkeypatch.setattr(verify_arch.subprocess, 'run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout, stderr=''))
    with pytest.raises(verify_arch.PlatformError, match='declared OpenShift version'):
        verify_arch.install(sources, 'amd64', 'amd64', tmp_path / 'oc')


def test_cli_reports_sanitized_failure(tmp_path, capsys):
    table = tmp_path / 'platforms.json'
    table.write_text(json.dumps({'schema_version': 1, 'ocp_version': '4.19.0', 'platforms': {}}))
    assert verify_arch.main(['install', '--manifest', str(table), '--arch', 'arm64']) == 1
    captured = capsys.readouterr()
    assert 'Architecture verification failed' in captured.err
    assert 'Traceback' not in captured.err


def test_cli_requires_a_subcommand():
    with pytest.raises(SystemExit):
        verify_arch.main([])


def test_ci_proves_each_architecture_natively_without_publishing():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow['jobs']['contributor-bundles']
    matrix = job['strategy']['matrix']['include']
    assert {entry['arch']: entry['runs-on'] for entry in matrix} == {
        'amd64': 'ubuntu-latest', 'arm64': 'ubuntu-24.04-arm'}
    steps = yaml.safe_dump(job['steps'])
    assert 'docker build -f benchmarks/runner/Containerfile' in steps
    assert 'tools/scan_runner_image.py' in steps
    assert 'tests/contributor_import_smoke.py' in steps, 'container evidence must be imported on the host'
    assert 'guidellm-smoke' in steps and 'aiperf-smoke' in steps, 'both smoke harnesses must run per architecture'
    assert 'docker push' not in steps and 'docker login' not in steps and '--push' not in steps
    assert 'contributor-bundles-macos' in workflow['jobs'], 'native macOS support is not retired yet'
