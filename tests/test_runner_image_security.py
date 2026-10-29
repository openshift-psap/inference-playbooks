"""Release safety checks use synthetic findings, never real credentials."""
import json
from pathlib import Path
import re
from types import SimpleNamespace
import sys

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
import scan_runner_image as scanner

IMAGE_ID = 'sha256:' + 'a' * 64


def fake_engine(monkeypatch, scenario='clean'):
    calls = []
    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[1:3] == ['image', 'inspect']:
            return SimpleNamespace(returncode=int(scenario == 'inspect-error'), stdout=IMAGE_ID + '\n')
        if argv[1] == 'save':
            assert argv[-1] == IMAGE_ID
            return SimpleNamespace(returncode=int(scenario == 'save-error'), stdout='')
        assert argv[1] == 'run'
        if scenario == 'timeout':
            raise scanner.subprocess.TimeoutExpired(argv, 600, output='private scanner output')
        if scenario != 'missing-report':
            mount = next(arg for arg in argv if arg.endswith(':/reports:Z'))
            report = Path(mount.removesuffix(':/reports:Z')) / 'report.json'
            document = {'SchemaVersion': 2, 'ArtifactType': 'container_image',
                        'Metadata': {'ImageID': IMAGE_ID}, 'Results': []}
            if scenario in {'secret', 'secret-zero-exit'}:
                document['Results'] = [{'Secrets': [{'Match': 'private scanner output'}]}]
            elif scenario == 'wrong-image':
                document['Metadata']['ImageID'] = 'sha256:' + 'b' * 64
            elif scenario == 'wrong-report':
                document['ArtifactType'] = 'filesystem'
            report.write_text('not json' if scenario == 'invalid-json' else json.dumps(document))
        return SimpleNamespace(returncode=int(scenario in {'secret', 'scanner-error'}),
                               stdout='private scanner output', stderr='private scanner output')
    monkeypatch.setattr(scanner, 'run', run)
    return calls


@pytest.mark.parametrize('engine', ['docker', 'podman'])
def test_gate_scans_frozen_image_offline_without_credentials(monkeypatch, capsys, engine):
    calls = fake_engine(monkeypatch)
    monkeypatch.setenv('CONTAINER_ENGINE', engine)
    assert scanner.main(['localhost/example:reviewed']) == 0
    command = calls[-1]
    assert command[command.index('--network') + 1] == 'none'
    assert '--read-only' in command and '--cap-drop' in command
    assert '--disable-telemetry' in command and '--offline-scan' in command
    assert '--skip-db-update' in command and '--skip-java-db-update' in command
    assert command[command.index('--scanners') + 1] == 'secret'
    assert command[command.index('--image-config-scanners') + 1] == 'secret'
    assert command[command.index('--exit-code') + 1] == '1'
    assert command[command.index('--secret-config') + 1] == ''
    assert ('--userns=keep-id' in command) == (engine == 'podman')
    assert scanner.SCANNER_IMAGE in command
    assert re.fullmatch(r'docker.io/aquasec/trivy@sha256:[a-f0-9]{64}', scanner.SCANNER_IMAGE)
    assert not any('docker.sock' in arg or 'kubeconfig' in arg for arg in command)
    assert not any(arg in {'push', '--env-file', '--privileged'} for arg in command)
    assert 'private scanner output' not in capsys.readouterr().out
    report_mount = next(arg for arg in command if arg.endswith(':/reports:Z'))
    assert not Path(report_mount.removesuffix(':/reports:Z')).exists()


@pytest.mark.parametrize('scenario', ['inspect-error', 'save-error', 'missing-report', 'timeout',
                                    'secret', 'secret-zero-exit', 'scanner-error', 'wrong-image',
                                    'wrong-report', 'invalid-json'])
def test_gate_fails_closed_without_printing_matches(monkeypatch, capsys, scenario):
    fake_engine(monkeypatch, scenario)
    monkeypatch.setenv('CONTAINER_ENGINE', 'docker')
    assert scanner.main(['localhost/example:reviewed']) == 1
    output = capsys.readouterr().out
    assert 'FAILED' in output or 'NOT approved' in output
    assert 'private scanner output' not in output
    assert 'Secret scan passed' not in output


def test_build_does_not_copy_host_python_caches():
    containerfile = (REPO / 'benchmarks/runner/Containerfile').read_text()
    assert 'COPY tools/*.py /opt/playbooks/tools/' in containerfile
    assert 'COPY tools/ /opt/playbooks/tools/' not in containerfile
    ignored = (REPO / '.dockerignore').read_text().splitlines()
    assert '**/__pycache__' in ignored and '**/*.py[cod]' in ignored
    assert '.git' in ignored


def test_ci_secret_gate_runs_after_build_and_before_smoke_without_publication():
    workflow = yaml.safe_load((REPO / '.github/workflows/recipe-evidence.yml').read_text())
    assert workflow['permissions'] == {'contents': 'read'}
    steps = workflow['jobs']['contributor-bundles']['steps']
    build = next(index for index, step in enumerate(steps) if 'docker build' in step.get('run', ''))
    gate = next(index for index, step in enumerate(steps) if 'tools/scan_runner_image.py' in step.get('run', ''))
    smoke = next(index for index, step in enumerate(steps) if 'docker run' in step.get('run', ''))
    assert build < gate < smoke
    assert steps[gate]['env'] == {'CONTAINER_ENGINE': 'docker'}
    assert not steps[gate].get('continue-on-error')
    assert not any('push' in step.get('run', '') or 'secrets.' in str(step) for step in steps)
    assert 'tests/test_runner_image_security.py' in steps[1]['run']
    precommit = yaml.safe_load((REPO / '.pre-commit-config.yaml').read_text())
    hook = next(hook for repo in precommit['repos'] for hook in repo.get('hooks', [])
                if hook['id'] == 'contributor-bundles')
    assert hook['entry'] in steps[1]['run']
