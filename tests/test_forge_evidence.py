"""Offline Forge contract and intake tests; no live credentials or network needed."""
import copy
import json
import subprocess
import sys
import shutil
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
from fetch_forge_evidence import Client, NoRedirect, append_reference, import_parent, safe_artifact
from ingest_forge import comment_runs
from normalize_kpis import normalize
from validate import validate_benchmark_run


@pytest.fixture
def kpis():
    return json.loads((REPO / 'tests/fixtures/forge-kpis-pr34-example.json').read_text())


def normalized(document, **kwargs):
    return normalize(document, run_id='forge-' + 'a' * 32, deployment_scope='single-node',
                     accelerator_key='nvidia-h200-x8', **kwargs)


def test_real_fixture_units_missing_percentiles_and_schema(kpis):
    results = normalized(kpis)
    assert set(results) == {'guidellm', 'aiperf'}
    guide = results['guidellm']
    assert [p['concurrency'] for p in guide['sweep']] == [1, 5]
    raw = next(k for k in kpis['tests'][0]['kpis'] if k['kpi_id'] == 'playbooks_ttft_median')
    assert guide['sweep'][0]['ttft_ms_p50'] == raw['values'][0][1] * 1000
    assert guide['sweep'][0]['ttft_ms_p90'] is None
    ai = results['aiperf']
    raw_ai = next(k for k in kpis['tests'][1]['kpis'] if k['kpi_id'] == 'aiperf_time_to_first_token_avg')
    assert ai['sweep'][0]['ttft_ms_avg'] == raw_ai['value']
    assert all(r['completeness'] == 'smoke' for r in results.values())
    for result in results.values():
        jsonschema.validate(result, json.loads((REPO / 'schema/benchmark-result.schema.json').read_text()))


def test_aiperf_multi_concurrency_and_explicit_full_sweep(kpis):
    ai = kpis['tests'][1]
    ai['labels']['benchmark_key'] = 'agentx'
    tests = []
    for c in (16, 1, 4):
        test = copy.deepcopy(ai)
        test['labels']['concurrency'] = str(c)
        tests.append(test)
    results = normalized({'tests': tests}, expected_concurrencies={'aiperf': [1, 4, 16]})
    assert [p['concurrency'] for p in results['aiperf']['sweep']] == [1, 4, 16]
    assert results['aiperf']['completeness'] == 'full-sweep'
    assert normalized({'tests': tests})['aiperf']['completeness'] == 'partial'


@pytest.mark.parametrize('expected_harness', ['guidellm', 'aiperf', None])
def test_missing_harness_expectation_stays_partial(expected_harness, caplog):
    levels = [1, 4, 16]
    tests = [{'labels': {'benchmark_tool': 'guidellm'}, 'kpis': [
        {'kpi_id': 'playbooks_output_tok_per_sec', 'is_curve': True,
         'values': [[c, c * 10] for c in levels]}]}]
    tests.extend({'labels': {'benchmark_tool': 'aiperf', 'concurrency': str(c)},
                  'kpis': [{'kpi_id': 'aiperf_output_token_throughput_avg',
                            'is_curve': False, 'value': c * 10}]} for c in levels)
    expected = {expected_harness: levels} if expected_harness else {}
    results = normalized({'tests': tests}, expected_concurrencies=expected)
    for harness, result in results.items():
        assert result['completeness'] == ('full-sweep' if harness == expected_harness else 'partial')
        assert [point['concurrency'] for point in result['sweep']] == levels
    warned = [record.message.split(':', 1)[0] for record in caplog.records
              if record.name == 'normalize_kpis' and record.levelname == 'WARNING']
    assert set(warned) == set(results) - {expected_harness}
    assert all('completeness remains partial' in record.message for record in caplog.records)
    caplog.clear()
    normalized({'tests': tests})
    assert not caplog.records
    normalized({'tests': tests}, expected_concurrencies={h: levels for h in results})
    assert not caplog.records


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), True])
def test_invalid_metrics_rejected(kpis, value):
    kpis['tests'][0]['kpis'][0]['values'][0][1] = value
    with pytest.raises(ValueError):
        normalized(kpis)


def test_duplicates_and_mixed_workloads_rejected(kpis):
    duplicate = copy.deepcopy(kpis['tests'][0])
    kpis['tests'].append(duplicate)
    with pytest.raises(ValueError, match='duplicate metric'):
        normalized(kpis)
    duplicate['labels']['benchmark_key'] = 'different'
    with pytest.raises(ValueError, match='mixed workload'):
        normalized(kpis)


def test_comment_trust_and_origin():
    url = 'https://mlflow.example/#/experiments/470/runs/' + 'a' * 32
    comment = {'id': 123, 'user': {'login': 'psap-forge-bot'}, 'body': 'Succeeded: ' + url}
    assert comment_runs([comment], 'https://mlflow.example') == {'a' * 32: 123}
    assert not comment_runs([comment], 'https://other.example')
    for body in ('failed: ' + url, 'not successful, error: ' + url, url):
        assert not comment_runs([{**comment, 'body': body}], 'https://mlflow.example')
    assert not comment_runs([{**comment, 'user': {'login': 'attacker'}}], 'https://mlflow.example')


@pytest.fixture
def intake(tmp_path, kpis):
    shutil.copytree(REPO / 'schema', tmp_path / 'schema')
    recipe_path = tmp_path / 'models/test/recipes/test-recipe/recipe.yaml'
    recipe_path.parent.mkdir(parents=True)
    profile_path = tmp_path / 'hardware-profiles/test.yaml'
    profile_path.parent.mkdir()
    profile_path.write_text(yaml.safe_dump({'profile_revision': 1, 'accelerator_key': 'nvidia-h200-x8'}))
    recipe = {'recipe_id': 'test-recipe', 'hardware_profile': 'hardware-profiles/test.yaml',
              'deployment': {'scope': 'single-node'}, 'platforms': [
                  {'stack': 'vllm', 'version': '0.24.0', 'overrides': 'platforms/vllm.yaml'}],
              'serving': {'model': 'google/gemma-4-26B-A4B-it'}, 'benchmark_runs': []}
    recipe_path.write_text('# keep this comment\n' + yaml.safe_dump(recipe, sort_keys=False))
    (recipe_path.parent / 'platforms').mkdir()
    (recipe_path.parent / 'platforms/vllm.yaml').write_text('{}\n')
    (recipe_path.parent / 'config').mkdir()
    (recipe_path.parent / 'config/patch.yaml').write_text('{}\n')
    (tmp_path / 'models/test/model.yaml').write_text('{}\n')
    for args in (['init'], ['add', '.'], ['-c', 'user.email=test@example.org', '-c', 'user.name=Test', 'commit', '-m', 'fixture']):
        subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)
    revision = subprocess.run(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'], check=True,
                              capture_output=True, text=True).stdout.strip()
    values = {'recipe_path': str(recipe_path.relative_to(tmp_path)), 'tested_revision': revision,
              'hardware_profile': 'hardware-profiles/test.yaml', 'hardware_profile_revision': '1',
              'model_revision': 'b' * 40, 'image_digest': 'sha256:' + 'c' * 64,
              'platform_stack': 'vllm', 'platform_version': '0.24.0', 'pr_number': '34',
              'command': 'forge benchmark --config committed-config.yaml'}
    parent = {'info': {'run_id': 'd' * 32, 'status': 'FINISHED', 'experiment_id': '470'},
              'data': {'tags': [{'key': 'playbooks.' + k, 'value': v} for k, v in values.items()]}}
    child = {'info': {'run_id': 'a' * 32, 'status': 'FINISHED', 'experiment_id': '470'}, 'data': {'tags': []}}

    class FakeClient:
        uri = 'https://mlflow.example'
        headers = {'X-MLflow-Workspace': 'forge-sandbox'}
        def run(self, run_id):
            return parent
        def children(self, run):
            return [child]
        def kpis(self, run_id):
            return ('kpis/kpis.json', json.dumps(kpis).encode()) if run_id == 'a' * 32 else None

    return tmp_path, recipe_path, revision, FakeClient(), parent, values


def test_import_replay_and_idempotence(intake):
    repo, path, revision, client, parent, values = intake
    import_parent(client, repo, 'd' * 32, revision, 34, 123)
    before = {p: p.read_bytes() for p in repo.glob('models/**/*') if p.is_file()}
    recipe = yaml.safe_load(path.read_text())
    assert len(recipe['benchmark_runs']) == 2
    assert path.read_text().startswith('# keep this comment')
    for reference in recipe['benchmark_runs']:
        run_path = path.parent / reference
        run = yaml.safe_load(run_path.read_text())
        jsonschema.validate(run, json.loads((REPO / 'schema/benchmark-run.schema.json').read_text()))
        assert validate_benchmark_run(repo, run_path, run)[0] == []
        result_path = run_path.parent / 'result.json'
        result = json.loads(result_path.read_text())
        result['metrics']['output_tok_s'] = -10
        result_path.write_text(json.dumps(result))
        assert 'differs from raw KPI replay' in str(validate_benchmark_run(repo, run_path, run)[0])
        result_path.write_bytes(before[result_path])
    import_parent(client, repo, 'd' * 32, revision, 34, 123)
    assert before == {p: p.read_bytes() for p in repo.glob('models/**/*') if p.is_file()}


@pytest.mark.parametrize('failure', ['stale', 'missing', 'hardware', 'changed', 'model', 'config', 'untracked'])
def test_attribution_failure_does_not_write(intake, failure):
    repo, path, revision, client, parent, values = intake
    if failure == 'stale':
        parent['data']['tags'][1]['value'] = 'f' * 40
    elif failure == 'missing':
        parent['data']['tags'] = []
    elif failure == 'hardware':
        parent['data']['tags'][3]['value'] = '2'
    elif failure == 'changed':
        (path.parent / 'platforms/vllm.yaml').write_text('args: [changed]\n')
    elif failure == 'model':
        (repo / 'models/test/model.yaml').write_text('huggingface_id: different/model\n')
    elif failure == 'config':
        (path.parent / 'config/patch.yaml').write_text('args: [untested]\n')
    else:
        (path.parent / 'config/untested.yaml').write_text('{}\n')
    with pytest.raises(ValueError):
        import_parent(client, repo, 'd' * 32, revision, 34)
    assert not (path.parent / 'results').exists()


def test_unavailable_and_redirect_rejected(intake):
    repo, path, revision, client, parent, values = intake
    def unavailable(_):
        raise TimeoutError('unavailable')
    client.run = unavailable
    with pytest.raises(TimeoutError):
        import_parent(client, repo, 'd' * 32, revision, 34)
    assert not (path.parent / 'results').exists()
    with pytest.raises(ValueError, match='redirect'):
        NoRedirect().redirect_request(None, None, 302, '', {}, 'https://attacker.example')


@pytest.mark.parametrize('artifact', [
    {'authorization': 'redacted'}, {'prompts': ['private']},
    {'cluster': 'private.example.org'}, {'runtime_args': '--api-key token'},
    {'endpoint': 'http://10.0.0.1:8000'},
])
def test_sensitive_raw_artifact_rejected(artifact):
    with pytest.raises(ValueError):
        safe_artifact(artifact)


def test_workspace_header_auth_timeout_and_raw_download():
    import io
    import base64
    client = Client('https://mlflow.example', 'forge-sandbox', 'user', 'password')
    observed = []
    class Opener:
        def open(self, request, timeout):
            observed.append((request, timeout))
            return io.BytesIO(b'{"test": 1}')
    client.opener = Opener()
    assert client.request('/get-artifact?run_id=abc', raw=True) == b'{"test": 1}'
    request, timeout = observed[0]
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers['x-mlflow-workspace'] == 'forge-sandbox'
    assert headers['authorization'] == 'Basic ' + base64.b64encode(b'user:password').decode()
    assert timeout == 60
    with pytest.raises(ValueError):
        Client('http://mlflow.example', 'forge-sandbox', 'user', 'password')


def test_rounding_and_collision(kpis):
    for kpi in kpis['tests'][0]['kpis']:
        kpi['values'][0][0] = 1.1
    assert normalized(kpis)['guidellm']['sweep'][0]['concurrency'] == 1
    kpis['tests'][0]['kpis'][0]['values'][1][0] = 1.4
    with pytest.raises(ValueError, match='rounded-colliding'):
        normalized(kpis)


@pytest.mark.parametrize('block', ['benchmark_runs: []\n', 'benchmark_runs:\n  - results/old/run.yaml\n',
                                 'benchmark_runs:\n- results/old/run.yaml\n', ''])
def test_reference_update_preserves_surrounding_yaml(block):
    text = '# author comment\nrecipe_id: example\n' + block + 'notes:\n  detail: preserved\n'
    result = append_reference(text, 'results/new/run.yaml')
    assert result.startswith('# author comment\nrecipe_id: example\n')
    assert 'notes:\n  detail: preserved\n' in result
    assert yaml.safe_load(result)['notes'] == {'detail': 'preserved'}
    assert append_reference(result, 'results/new/run.yaml') == result


def test_merged_pr_discovery_intake(monkeypatch, tmp_path):
    import ingest_forge
    calls = []
    monkeypatch.setenv('GITHUB_REPOSITORY', 'org/repo')
    for key, value in {'MLFLOW_TRACKING_URI': 'https://mlflow.example', 'MLFLOW_USERNAME': 'user',
                       'MLFLOW_PASSWORD': 'password'}.items():
        monkeypatch.setenv(key, value)
    parent_id = 'a' * 32
    comment = {'id': 123, 'user': {'login': 'psap-forge-bot'},
               'body': 'Succeeded https://mlflow.example/#/experiments/470/runs/' + parent_id}
    pr = {'number': 34, 'merged_at': '2026-10-14', 'base': {'ref': 'main'},
          'head': {'sha': 'b' * 40, 'ref': 'recipe-branch'}}
    def github(endpoint, paginated=False):
        return [comment] if endpoint.endswith('/comments') else pr
    monkeypatch.setattr(ingest_forge, 'github', github)
    monkeypatch.setattr(ingest_forge.subprocess, 'run', lambda *a, **kw: None)
    monkeypatch.setattr(ingest_forge, 'import_parent', lambda *args: calls.append(args))
    monkeypatch.setattr(sys, 'argv', ['ingest_forge.py', '--pr-number', '34', '--repo', str(tmp_path)])
    ingest_forge.main()
    assert calls[0][1:] == (tmp_path, parent_id, 'b' * 40, 34, 123)
    calls.clear()
    pr['head']['ref'] = 'automation/forge-evidence-existing'
    ingest_forge.main()
    assert calls == []


def test_replay_rejects_raw_checksum_and_completeness_drift(intake):
    repo, path, revision, client, parent, values = intake
    import_parent(client, repo, 'd' * 32, revision, 34)
    reference = yaml.safe_load(path.read_text())['benchmark_runs'][0]
    run_path = path.parent / reference
    run = yaml.safe_load(run_path.read_text())
    raw_path = run_path.parent / 'kpis.json'
    raw = raw_path.read_bytes()
    raw_path.write_bytes(raw + b'\n')
    assert 'Forge KPI checksum mismatch' in str(validate_benchmark_run(repo, run_path, run)[0])
    raw_path.write_bytes(raw)
    run['forge']['completeness'] = 'full-sweep'
    assert 'Forge completeness differs' in str(validate_benchmark_run(repo, run_path, run)[0])


def test_real_fixture_without_attribution_is_not_publishable(intake):
    repo, path, revision, client, parent, values = intake
    parent['data']['tags'] = []
    with pytest.raises(ValueError, match='missing Forge attribution tags'):
        import_parent(client, repo, 'd' * 32, revision, 34)
    assert not (path.parent / 'results').exists()


def test_import_does_not_promote_recipe(intake):
    repo, path, revision, client, parent, values = intake
    before = yaml.safe_load(path.read_text())
    import_parent(client, repo, 'd' * 32, revision, 34)
    after = yaml.safe_load(path.read_text())
    before.pop('benchmark_runs')
    after.pop('benchmark_runs')
    assert after == before


def test_existing_run_cannot_be_overwritten(intake):
    repo, path, revision, client, parent, values = intake
    import_parent(client, repo, 'd' * 32, revision, 34)
    reference = yaml.safe_load(path.read_text())['benchmark_runs'][0]
    raw_path = (path.parent / reference).parent / 'kpis.json'
    raw_path.write_text('{}\n')
    before = {p: p.read_bytes() for p in repo.glob('models/**/*') if p.is_file()}
    with pytest.raises(ValueError, match='immutable run already exists'):
        import_parent(client, repo, 'd' * 32, revision, 34)
    assert before == {p: p.read_bytes() for p in repo.glob('models/**/*') if p.is_file()}


def test_v1_run_and_result_remain_compatible():
    run = {'schema_version': 1, 'run_id': 'old-run', 'recipe_id': 'example',
           'deployment_scope': 'single-node', 'hardware_profile': 'hardware-profiles/test.yaml',
           'hardware_profile_revision': 1, 'harness': 'guidellm', 'result': 'result.json',
           'artifacts': [{'type': 'raw-output', 'path': 'output.json'}]}
    result = {'schema_version': 1, 'run_id': 'old-run', 'deployment_scope': 'single-node',
              'accelerator_key': 'nvidia-h200-x8', 'metrics': {}}
    for name, document in [('benchmark-run', run), ('benchmark-result', result)]:
        jsonschema.validate(document, json.loads((REPO / f'schema/{name}.schema.json').read_text()))
    run['schema_version'] = 2
    with pytest.raises(jsonschema.ValidationError, match='evidence_source'):
        jsonschema.validate(run, json.loads((REPO / 'schema/benchmark-run.schema.json').read_text()))


def test_ingestion_workflow_is_standalone():
    text = (REPO / '.github/workflows/benchmark-ingest.yml').read_text()
    assert 'catalog.py' not in text
    assert 'catalog/**' not in text
    assert 'site/' not in text
    assert 'python3 tools/validate.py --current' in text
    assert 'ref: main' in text
    assert "github.ref == 'refs/heads/main'" in text


def test_attribution_cannot_remove_tested_evidence_references(intake, monkeypatch):
    repo, path, revision, client, parent, values = intake
    original_run = subprocess.run
    def run(command, **kwargs):
        result = original_run(command, **kwargs)
        if 'show' in command:
            tested = yaml.safe_load(result.stdout)
            tested['benchmark_runs'] = ['results/tested-run/run.yaml']
            result.stdout = yaml.safe_dump(tested)
        return result
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(ValueError, match='removed tested benchmark references'):
        import_parent(client, repo, 'd' * 32, revision, 34)
    assert not (path.parent / 'results').exists()
