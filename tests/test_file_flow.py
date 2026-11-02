"""Advisory ownership checks use the same committed/staged snapshot as CI."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))

from file_flow import introduced_file_warnings, report_file_warning

LEAF = 'models/example/recipes/h200-x8-tp8'


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def write(repo, path, text='{}\n'):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def commit(repo):
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'fixture')
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def flow_repo(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.email', 'test@example.org')
    git(tmp_path, 'config', 'user.name', 'Test')
    write(tmp_path, 'README.md', '# Fixture\n')
    write(tmp_path, 'models/legacy/old/runtime.yaml')
    base = commit(tmp_path)
    return tmp_path, base


def recipe(repo, **updates):
    value = {'schema_version': 4, 'recipe_id': 'h200-x8-tp8',
             'platforms': [{'stack': 'vllm', 'version': '1', 'overrides': 'platforms/vllm-1.yaml'}],
             'serving': {'parallelism': {'mode': 'tp'}}, 'deployment': {}}
    value.update(updates)
    write(repo, LEAF + '/recipe.yaml', yaml.safe_dump(value))
    return value


def warnings(repo, base, head=None, cached=False):
    return dict(introduced_file_warnings(repo, base, head or 'HEAD', cached))


def test_out_of_flow_additions_include_the_pr4_failure_pattern(flow_repo):
    repo, base = flow_repo
    recipe(repo)
    unexpected = [LEAF + '/manifests/vllm-1/forge/deployment.yaml',
                  LEAF + '/platforms/unused.yaml', LEAF + '/config/unused.yaml',
                  LEAF + '/deploy.yaml', 'models/example/model-ops/download.yaml',
                  'models/example/vllm/1/manifests/deployment.yaml',
                  'tests/forge/recipes.yaml', 'hardware-profiles/nested/profile.yaml',
                  'unmanaged/deployment.yaml']
    for name in unexpected:
        write(repo, name)
    head = commit(repo)
    result = warnings(repo, base, head)
    assert set(result) == set(unexpected)
    assert 'render' in result[unexpected[0]]
    assert 'v4' in result['tests/forge/recipes.yaml']


def test_standard_sources_outputs_and_raw_only_intake_are_quiet(flow_repo):
    repo, base = flow_repo
    recipe(repo, serving={'parallelism': {'mode': 'tp'}, 'config_overrides': True})
    for name in [LEAF + '/platforms/vllm-1.yaml', LEAF + '/config/kustomization.yaml',
                 LEAF + '/config/runtime.yaml', LEAF + '/manifests/vllm-1/deployment.yaml',
                 LEAF + '/guides/README.md', LEAF + '/benchmarks/workload.yaml',
                 'models/example/model.yaml', 'hardware-profiles/example.yaml',
                 'models/raw/recipes/intake/raw-manifest/input.yaml',
                 'models/raw/recipes/intake/raw-manifest/README.md',
                 'models/legacy/old/README.md', 'docs/flow.md', 'tests/fixtures/input.yaml',
                 'tools/helper.py', 'schema/new.schema.json', '.github/workflows/new.yml']:
        write(repo, name)
    assert warnings(repo, base, commit(repo)) == {}


def test_pins_and_explicit_hand_authored_exceptions_are_quiet(flow_repo):
    repo, base = flow_repo
    recipe(repo, platforms=[{'stack': 'rhoai', 'version': '3.5', 'overrides': 'platforms/rhoai.yaml',
                             'pinned_manifest': 'config/serving.yaml'}],
           deployment={'manifests': [{'path': 'manifests/extra/auxiliary.yaml', 'generated': False}]})
    for name in ['platforms/rhoai.yaml', 'config/serving.yaml',
                 'manifests/rhoai-3.5/serving.yaml', 'manifests/extra/auxiliary.yaml']:
        write(repo, LEAF + '/' + name)
    assert warnings(repo, base, commit(repo)) == {}


def test_notes_and_result_artifacts_need_explicit_references(flow_repo):
    repo, base = flow_repo
    recipe(repo, notes='guides/notes.yaml', benchmark_runs=['results/run/run.yaml'])
    write(repo, LEAF + '/results/run/run.yaml', yaml.safe_dump({
        'result': 'result.json', 'artifacts': [{'path': 'raw/output.txt'}]}))
    for name in ['guides/notes.yaml', 'results/run/result.json', 'results/run/raw/output.txt',
                 'results/run/forge-import.json', 'guides/unused.yaml', 'results/loose/output.txt']:
        write(repo, LEAF + '/' + name)
    assert set(warnings(repo, base, commit(repo))) == {
        LEAF + '/guides/unused.yaml', LEAF + '/results/loose/output.txt'}


def test_old_files_modifications_and_deletions_do_not_warn_but_renames_do(flow_repo):
    repo, base = flow_repo
    write(repo, 'models/legacy/old/runtime.yaml', 'kind: Deployment\n')
    assert warnings(repo, base, commit(repo)) == {}
    git(repo, 'mv', 'models/legacy/old/runtime.yaml', 'models/legacy/old/renamed.yaml')
    head = commit(repo)
    assert set(warnings(repo, base, head)) == {'models/legacy/old/renamed.yaml'}
    git(repo, 'rm', 'models/legacy/old/renamed.yaml')
    assert warnings(repo, head, commit(repo)) == {}


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('declared', [False, True])
def test_ownership_uses_head_or_index_not_unstaged_recipe(flow_repo, cached, declared):
    repo, base = flow_repo
    data = recipe(repo)
    name = LEAF + '/manifests/vllm-1/forge/deployment.yaml'
    write(repo, name)
    exception = [{'path': 'manifests/vllm-1/forge/deployment.yaml', 'generated': False}]
    data['deployment']['manifests'] = exception if declared else []
    write(repo, LEAF + '/recipe.yaml', yaml.safe_dump(data))
    git(repo, 'add', '.')
    head = 'HEAD' if cached else commit(repo)
    data['deployment']['manifests'] = [] if declared else exception
    write(repo, LEAF + '/recipe.yaml', yaml.safe_dump(data))
    assert set(warnings(repo, base, head, cached)) == (set() if declared else {name})


def test_github_warning_properties_are_escaped(monkeypatch, capsys):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    report_file_warning('models/test,part%\n::error::injected.yaml', 'Review\r\nplacement%')
    output = capsys.readouterr().err
    assert output.startswith('::warning file=')
    assert '%2C' in output and '%25' in output and '%0A' in output and '%0D' in output
    assert len(output.splitlines()) == 1
    assert '\n::error' not in output


def test_local_warning_is_readable_without_actions_commands(monkeypatch, capsys):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    report_file_warning('models/example/extra.yaml', 'Review placement')
    output = capsys.readouterr().err
    assert output.startswith('Warning:') and 'models/example/extra.yaml' in output
    assert '::warning' not in output


@pytest.mark.parametrize('actions', [False, True])
def test_validator_warns_without_failing_and_current_mode_skips_diff(flow_repo, actions):
    repo, _ = flow_repo
    for directory in ['schema', 'engine-versions']:
        shutil.copytree(REPO / directory, repo / directory)
    base = commit(repo)
    name = 'models/example/model-ops/download.yaml'
    write(repo, name)
    head = commit(repo)
    env = {**os.environ, 'GITHUB_ACTIONS': 'true' if actions else 'false'}
    command = [sys.executable, str(REPO / 'tools/validate.py'), '--repo', str(repo)]
    result = subprocess.run([*command, '--base', base, '--head', head], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert name in result.stderr
    assert ('::warning file=' in result.stderr) == actions
    current = subprocess.run([*command, '--current'], capture_output=True, text=True, env=env)
    assert current.returncode == 0, current.stderr
    assert name not in current.stderr


def test_advisory_does_not_downgrade_existing_validation_errors(flow_repo):
    repo, _ = flow_repo
    for directory in ['schema', 'engine-versions']:
        shutil.copytree(REPO / directory, repo / directory)
    base = commit(repo)
    write(repo, 'models/example/model.yaml', '{}\n')
    head = commit(repo)
    result = subprocess.run([sys.executable, str(REPO / 'tools/validate.py'), '--repo', str(repo),
                             '--base', base, '--head', head], capture_output=True, text=True)
    assert result.returncode == 1
    assert 'required property' in result.stderr
