"""Offline Forge contract and intake tests; no live credentials or network needed."""
import copy
import hashlib
import json
import subprocess
import sys
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jsonschema
import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
from fetch_forge_evidence import Client, NoKpiArtifactsError, NoRedirect, append_reference, import_parent, safe_artifact, serving_arguments
from forge_model_identity import validate_model_identity
from ingest_forge import selected_run
from forge_admission import admitted_execution, load_admissions
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


def forge_report(run_id='d' * 32, comment_id=123, label='mlflow results', workspace='forge-sandbox'):
    url = 'https://mlflow.example/#/experiments/470/runs/' + run_id
    query = '' if workspace is None else '?workspace=' + workspace
    return {'id': comment_id, 'created_at': '2026-10-15T00:00:00Z',
            'user': {'login': 'psap-forge-bot[bot]'},
            'body': f'Succeeded\n**MLFlow links**\n* [{label}]({url}/artifacts{query})\n'
                    '* [nested run](https://mlflow.example/#/experiments/470/runs/' + 'a' * 32 + ')'}


@pytest.mark.parametrize('label', ['mlflow results', 'MLFlow Test Results'])
def test_comment_trust_and_origin(label):
    comment = forge_report(label=label)
    assert selected_run([comment], 'https://mlflow.example', 'forge-sandbox') == ('d' * 32, 123, 'forge-sandbox')
    with pytest.raises(ValueError, match='invalid primary'):
        selected_run([comment], 'https://other.example', 'forge-sandbox')
    for body in ('failed', 'not successful, error', 'unsuccessful', 'not passed', 'a link without status'):
        assert selected_run([{**comment, 'body': body}], 'https://mlflow.example', 'forge-sandbox') is None
    for login in ('attacker', 'psap-forge-bot', 'psap-forge-bot-impostor[bot]'):
        assert selected_run([{**comment, 'user': {'login': login}}],
                            'https://mlflow.example', 'forge-sandbox') is None


@pytest.mark.parametrize('body', [
    'Succeeded but missing results',
    'Succeeded [mlflow results](not-a-url)',
    forge_report()['body'].replace('/artifacts?', '/artifacts/private.log?'),
    forge_report()['body'].replace('workspace=forge-sandbox', 'workspace=other'),
    forge_report()['body'] + '\n' + forge_report('b' * 32)['body'],
])
def test_latest_invalid_report_never_falls_back(body):
    older = forge_report(comment_id=122)
    selected = {**forge_report(), 'body': body}
    with pytest.raises(ValueError):
        selected_run([selected, older], 'https://mlflow.example', 'forge-sandbox')


def test_report_order_and_duplicate_parent_reports():
    older = forge_report('a' * 32, comment_id=999)
    older['created_at'] = '2026-10-14T00:00:00Z'
    newer = forge_report(comment_id=1)
    duplicate = forge_report(comment_id=2, label='MLFlow Test Results')
    assert selected_run([duplicate, older, newer], 'https://mlflow.example',
                        'forge-sandbox') == ('d' * 32, 2, 'forge-sandbox')


@pytest.mark.parametrize('default', ['forge-sandbox', 'forge-inference-playbooks', 'custom-workspace'])
@pytest.mark.parametrize('workspace', ['forge-sandbox', 'forge-inference-playbooks', None])
def test_report_workspace_routing_and_legacy_default(default, workspace):
    assert selected_run([forge_report(workspace=workspace)], 'https://mlflow.example', default) == (
        'd' * 32, 123, workspace or default)
    assert selected_run([forge_report(workspace=default)], 'https://mlflow.example', default) == (
        'd' * 32, 123, default)


@pytest.mark.parametrize('workspace', [
    '', 'other', 'forge-sandbox&workspace=forge-sandbox',
    'forge-sandbox&workspace=forge-inference-playbooks',
    'forge-sandbox%0d%0aInjected%3Atrue', '%20forge-sandbox', 'forge-sandbox%00',
])
def test_invalid_report_workspace_never_falls_back(workspace):
    older = forge_report(comment_id=122)
    with pytest.raises(ValueError, match='workspace'):
        selected_run([older, forge_report(workspace=workspace)], 'https://mlflow.example', 'forge-sandbox')


def test_primary_links_must_agree_on_workspace_and_parent():
    first = forge_report()
    duplicate = {**first, 'body': first['body'] + '\n' + first['body']}
    assert selected_run([duplicate], 'https://mlflow.example', 'forge-sandbox') == (
        'd' * 32, 123, 'forge-sandbox')
    conflicting = {**first, 'body': first['body'] + '\n' +
                   forge_report(workspace='forge-inference-playbooks')['body']}
    with pytest.raises(ValueError, match='ambiguous primary'):
        selected_run([conflicting], 'https://mlflow.example', 'forge-sandbox')


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


@pytest.mark.parametrize('model_revision', ['main', 'v1.2.0', 'release/model-v2', 'b' * 40, 'c' * 64])
def test_import_replay_and_idempotence(intake, model_revision):
    repo, path, revision, client, parent, values = intake
    next(tag for tag in parent['data']['tags'] if tag['key'] == 'playbooks.model_revision')['value'] = model_revision
    import_parent(client, repo, 'd' * 32, revision, 34, 123)
    before = {p: p.read_bytes() for p in repo.glob('models/**/*') if p.is_file()}
    recipe = yaml.safe_load(path.read_text())
    assert len(recipe['benchmark_runs']) == 2
    assert path.read_text().startswith('# keep this comment')
    for reference in recipe['benchmark_runs']:
        run_path = path.parent / reference
        run = yaml.safe_load(run_path.read_text())
        assert run['environment']['model_revision'] == model_revision
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


@pytest.fixture
def variant_intake(intake):
    repo, path, _, client, parent, values = intake
    recipe = yaml.safe_load(path.read_text())
    recipe['serving'].update(model='example/base-FP8', served_model_name='request-alias')
    path.write_text(yaml.safe_dump(recipe, sort_keys=False))
    (repo / 'models/test/model.yaml').write_text(yaml.safe_dump({
        'huggingface_id': 'example/base', 'quantizations': [
            {'name': 'fp8', 'huggingface_id': 'example/base-FP8'},
            {'name': 'int4', 'huggingface_id': 'example/base-INT4'}]}))
    for args in (['add', '.'], ['-c', 'user.email=test@example.org', '-c', 'user.name=Test',
                              'commit', '-m', 'variant fixture']):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    revision = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True,
                              capture_output=True, text=True).stdout.strip()
    next(tag for tag in parent['data']['tags'] if tag['key'] == 'playbooks.tested_revision')['value'] = revision
    return repo, path, revision, client, parent, values


@pytest.fixture
def parent_intake(variant_intake, kpis):
    repo, path, revision, client, parent, _ = variant_intake
    parent['data']['tags'] = []
    for test in kpis['tests']:
        test['labels'].update(model_name='example/base', served_model='example/base-FP8', quantization='fp8')
    raw = json.dumps(kpis).encode()
    source = '01__test/001__inference_playbooks_test_dir/src/server-deployment.yaml'
    artifacts = {
        '01__test/000__ci_metadata/fournos_fjob.yaml': yaml.safe_dump({'spec': {'env': {
            'FORGE_FOREIGN_TESTING_PULL_PULL_SHA': revision, 'FORGE_FOREIGN_TESTING_PULL_NUMBER': '34',
            'PULL_PULL_SHA': '5' * 40}}}),
        '01__test/001__inference_playbooks_test_dir/config.yaml': yaml.safe_dump({'inference_playbooks': {'recipe': 'test-recipe',
            'manifest_path': 'models/test/recipes/test-recipe/manifests/vllm-0.24.0/deployment.yaml'}}),
        source: yaml.safe_dump({'kind': 'Deployment', 'spec': {'template': {'spec': {'containers': [
            {'name': 'vllm', 'image': 'docker.io/vllm/vllm-openai:v0.24.0', 'args': ['example/base-FP8']}]}}}}),
        '01__test/run.log': ('   Command: /usr/bin/python3 /app/forge/projects/inference_playbooks/orchestration/ci.py test\n'
            '|       containerStatuses:\n|       - allocatedResources: {}\n|         containerID: cri-o://example\n'
            '|         image: docker.io/vllm/vllm-openai:v0.24.0\n'
            '|         imageID: docker.io/vllm/vllm-openai@sha256:' + 'c' * 64 + '\n'
            '|         lastState: {}\n|         name: vllm\n|         ready: true\n')}

    def request(endpoint, body=None, raw=False):
        query = parse_qs(urlsplit(endpoint).query)
        assert query['run_id'] == ['d' * 32]
        if endpoint.startswith('/get-artifact?'):
            assert raw is True
            return artifacts[query['path'][0]].encode()
        assert query['path'] == ['01__test/001__inference_playbooks_test_dir/src']
        return {'files': [{'path': source, 'is_dir': False}]}

    client.request = request
    client.kpis = lambda run_id: ('01__test/002__postprocessing/kpis/kpis.json', raw)
    client.children = lambda parent: pytest.fail('current parent KPIs must not enumerate children')
    revision = record_launch(repo, artifacts, source)
    return repo, path, revision, client, parent, artifacts, raw


def record_launch(repo, artifacts, source):
    """Commit the selected effective manifest as a producer would consume it."""
    config = yaml.safe_load(artifacts['01__test/001__inference_playbooks_test_dir/config.yaml'])
    manifest = repo / config['inference_playbooks']['manifest_path']
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(artifacts[source])
    recipe_path = manifest.parents[2] / 'recipe.yaml'
    recipe = yaml.safe_load(recipe_path.read_text())
    recipe['deployment']['manifests'] = [{'path': str(manifest.relative_to(recipe_path.parent)),
                                         'kind': yaml.safe_load(artifacts[source])['kind'], 'generated': True}]
    recipe_path.write_text(yaml.safe_dump(recipe, sort_keys=False))
    commit_evidence(repo)
    revision = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True,
                              capture_output=True, text=True).stdout.strip()
    fjob_path = '01__test/000__ci_metadata/fournos_fjob.yaml'
    fjob = yaml.safe_load(artifacts[fjob_path])
    fjob['spec']['env']['FORGE_FOREIGN_TESTING_PULL_PULL_SHA'] = revision
    artifacts[fjob_path] = yaml.safe_dump(fjob)
    return revision


def test_tagless_parent_two_harnesses_replay_and_receipt(parent_intake, monkeypatch):
    repo, path, revision, client, _, artifacts, raw = parent_intake
    import_parent(client, repo, 'd' * 32, revision, 34, 123, require_parent=True)
    references = yaml.safe_load(path.read_text())['benchmark_runs']
    assert len(references) == 2
    for reference in references:
        run_path = path.parent / reference
        run = yaml.safe_load(run_path.read_text())
        assert run['run_id'] == f'forge-{"d" * 32}-{run["harness"]}'
        assert run['forge']['mlflow_benchmark_run_id'] == run['forge']['mlflow_parent_run_id'] == 'd' * 32
        assert f'/runs/{"d" * 32}?' in run['forge']['mlflow_run_url']
        assert run['environment']['model_revision'] == 'main'
        assert run['environment']['image_digest'] == 'sha256:' + 'c' * 64
        assert run['command'] == artifacts['01__test/run.log'].splitlines()[0].strip().removeprefix('Command: ')
        assert (run_path.parent / 'kpis.json').read_bytes() == raw
        assert validate_benchmark_run(repo, run_path, run)[0] == []
    for args in (['add', '.'], ['-c', 'user.email=test@example.org', '-c', 'user.name=Test', 'commit', '-m', 'parent evidence']):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    client.request = client.run = client.kpis = lambda *a, **kw: pytest.fail('receipt hit must not contact MLflow')
    monkeypatch.setattr('fetch_forge_evidence.normalize', lambda *a, **kw: pytest.fail('receipt hit must not normalize'))
    assert admitted_execution(repo, load_admissions(repo), client, 'd' * 32, revision, 34, {})


@pytest.mark.parametrize('failure', ['stale-recipe', 'forge-sha-only', 'pr', 'command', 'digest', 'not-ready',
                                      'tag-conflict', 'checkpoint', 'old-model-labels', 'yaml'])
def test_tagless_parent_failure_does_not_write(parent_intake, failure):
    repo, path, revision, client, parent, artifacts, raw = parent_intake
    fjob_path = '01__test/000__ci_metadata/fournos_fjob.yaml'
    fjob = yaml.safe_load(artifacts[fjob_path])
    if failure == 'stale-recipe':
        fjob['spec']['env']['FORGE_FOREIGN_TESTING_PULL_PULL_SHA'] = 'eecf4808' + '1' * 32
        fjob['spec']['env']['PULL_PULL_SHA'] = revision
    elif failure == 'forge-sha-only':
        del fjob['spec']['env']['FORGE_FOREIGN_TESTING_PULL_PULL_SHA']
        fjob['spec']['env']['PULL_PULL_SHA'] = revision
    elif failure == 'pr':
        fjob['spec']['env']['FORGE_FOREIGN_TESTING_PULL_NUMBER'] = '35'
    artifacts[fjob_path] = yaml.safe_dump(fjob)
    if failure == 'command':
        artifacts['01__test/run.log'] = '\n'.join(artifacts['01__test/run.log'].splitlines()[1:])
    elif failure == 'digest':
        artifacts['01__test/run.log'] = artifacts['01__test/run.log'].replace('sha256:', 'unknown:')
    elif failure == 'not-ready':
        artifacts['01__test/run.log'] = artifacts['01__test/run.log'].replace('ready: true', 'ready: false')
    elif failure == 'tag-conflict':
        parent['data']['tags'] = [{'key': 'playbooks.model_revision', 'value': 'different'}]
    elif failure == 'checkpoint':
        key = next(key for key in artifacts if key.endswith('-deployment.yaml'))
        artifacts[key] = artifacts[key].replace('example/base-FP8', 'example/other')
    elif failure == 'old-model-labels':
        document = json.loads(raw)
        for test in document['tests']:
            del test['labels']['served_model']
        client.kpis = lambda run_id: ('01__test/002__postprocessing/kpis/kpis.json', json.dumps(document).encode())
    elif failure == 'yaml':
        artifacts[fjob_path] = 'spec: {}\nspec: {private: do-not-echo}\n'
    before = path.read_bytes()
    with pytest.raises(ValueError) as error:
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert 'do-not-echo' not in str(error.value)
    assert path.read_bytes() == before
    assert not (path.parent / 'results').exists()


def test_tagless_parent_explicit_model_revision(parent_intake):
    repo, path, revision, client, _, artifacts, _ = parent_intake
    source = next(key for key in artifacts if key.endswith('-deployment.yaml'))
    deployment = yaml.safe_load(artifacts[source])
    deployment['spec']['template']['spec']['containers'][0]['args'].append('--revision=release/model-v2')
    artifacts[source] = yaml.safe_dump(deployment)
    revision = record_launch(repo, artifacts, source)
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    for reference in yaml.safe_load(path.read_text())['benchmark_runs']:
        run = yaml.safe_load((path.parent / reference).read_text())
        assert run['environment']['model_revision'] == 'release/model-v2'


@pytest.mark.parametrize('container', [
    {'args': ['/weights/checkpoint', '--revision=release/v2']},
    {'command': ['vllm', 'serve'], 'args': ['/weights/checkpoint', '--revision', 'release/v2']},
    {'command': ['bash', '-c', 'vllm serve "$@"', '--'],
     'args': ['/weights/checkpoint', '--revision=release/v2']},
    {'command': ['python', '-m', 'vllm.entrypoints.openai.api_server'],
     'args': ['--model=/weights/checkpoint', '--revision=release/v2']},
])
def test_equivalent_model_argument_forms(container):
    assert serving_arguments(container) == ('/weights/checkpoint', 'release/v2')


@pytest.fixture
def pvc_parent_intake(parent_intake):
    repo, path, _, client, parent, artifacts, raw = parent_intake
    source = next(key for key in artifacts if key.endswith('-deployment.yaml'))
    deployment = yaml.safe_load(artifacts[source])
    deployment['metadata'] = {'name': 'server'}
    deployment['spec']['selector'] = {'matchLabels': {'app': 'server'}}
    deployment['spec']['template']['metadata'] = {'labels': {'app': 'server'}}
    pod = deployment['spec']['template']['spec']
    pod['securityContext'] = {'runAsUser': 65534, 'runAsNonRoot': True}
    pod['volumes'] = [{'name': 'weights', 'persistentVolumeClaim': {'claimName': 'prepared-model'}}]
    server = pod['containers'][0]
    server['command'] = ['/bin/bash', '-c', 'vllm serve "$@"', '--']
    server['args'] = ['/weights/checkpoint', '--served-model-name=request-alias']
    server['volumeMounts'] = [{'name': 'weights', 'mountPath': '/weights/checkpoint', 'readOnly': True}]
    artifacts[source] = yaml.safe_dump(deployment)
    revision = record_launch(repo, artifacts, source)
    return repo, path, revision, client, parent, artifacts, raw, source


def test_pvc_launch_uses_tested_manifest_not_hf_id(pvc_parent_intake):
    repo, path, revision, client, _, artifacts, _, source = pvc_parent_intake
    deployment = yaml.safe_load(artifacts[source])
    deployment['metadata'].update(name='server-abc01234', namespace='test-namespace')
    run_label = {'forge.openshift.io/run': 'server-abc01234'}
    deployment['spec']['selector']['matchLabels'].update(run_label)
    deployment['spec']['template']['metadata']['labels'].update(run_label)
    pod = deployment['spec']['template']['spec']
    del pod['securityContext']['runAsUser']
    pod['containers'][0]['env'] = [{'name': 'HOME', 'value': '/tmp'}, {'name': 'USER', 'value': 'nobody'}]
    artifacts[source] = yaml.safe_dump(deployment)
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert len(yaml.safe_load(path.read_text())['benchmark_runs']) == 2


def test_receipt_rechecks_committed_manifest_after_initial_binding(pvc_parent_intake):
    repo, path, revision, client, _, _, _, _ = pvc_parent_intake
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    commit_evidence(repo)
    manifest = path.parent / 'manifests/vllm-0.24.0/deployment.yaml'
    manifest.write_text(manifest.read_text().replace('/weights/checkpoint', '/other/weights'))
    commit_evidence(repo)
    with pytest.raises(ValueError, match='merged deployment inputs differ'):
        admitted_execution(repo, load_admissions(repo), client, 'd' * 32, revision, 34, {})


@pytest.mark.parametrize('failure', ['path', 'claim', 'mount', 'alias', 'image', 'env', 'security',
                                    'manifest-drift', 'missing-identity'])
def test_pvc_launch_drift_never_authorized_by_alias(pvc_parent_intake, failure):
    repo, path, revision, client, _, artifacts, raw, source = pvc_parent_intake
    deployment = yaml.safe_load(artifacts[source])
    pod = deployment['spec']['template']['spec']
    server = pod['containers'][0]
    if failure == 'path':
        server['args'][0] = '/other/weights'
    elif failure == 'claim':
        pod['volumes'][0]['persistentVolumeClaim']['claimName'] = 'wrong-model'
    elif failure == 'mount':
        server['volumeMounts'][0]['readOnly'] = False
    elif failure == 'alias':
        server['args'][1] = '--served-model-name=other-alias'
    elif failure == 'image':
        server['image'] = 'docker.io/vllm/vllm-openai:other'
    elif failure == 'env':
        server['env'] = [{'name': 'HOME', 'value': '/unexpected'}]
    elif failure == 'security':
        pod['securityContext']['runAsNonRoot'] = False
    elif failure == 'manifest-drift':
        (path.parent / 'manifests/vllm-0.24.0/deployment.yaml').write_text('kind: Other\n')
    else:
        document = json.loads(raw)
        for test in document['tests']:
            test['labels'] = {'benchmark_tool': test['labels']['benchmark_tool']}
        client.kpis = lambda _: ('01__test/002__postprocessing/kpis/kpis.json', json.dumps(document).encode())
    artifacts[source] = yaml.safe_dump(deployment)
    with pytest.raises(ValueError):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert not (path.parent / 'results').exists()


def test_manifest_bound_product_version_is_not_engine_version(parent_intake):
    repo, path, revision, client, _, _, raw = parent_intake
    document = json.loads(raw)
    for test in document['tests']:
        test['labels']['product_version'] = '3.5'
    client.kpis = lambda _: ('01__test/002__postprocessing/kpis/kpis.json', json.dumps(document).encode())
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    for reference in yaml.safe_load(path.read_text())['benchmark_runs']:
        run = yaml.safe_load((path.parent / reference).read_text())
        assert run['platform'] == {'stack': 'vllm', 'version': '0.24.0'}
        assert all(labels['product_version'] == '3.5' for labels in run['workload']['tests'])


@pytest.fixture(params=['LeaderWorkerSet', 'LLMInferenceService'])
def distributed_intake(parent_intake, request):
    repo, path, _, client, parent, artifacts, raw = parent_intake
    kind = request.param
    recipe = yaml.safe_load(path.read_text())
    recipe['deployment']['scope'] = 'multi-node'
    stack, version = ('rhoai', '3.5') if kind == 'LLMInferenceService' else ('vllm', '0.24.0')
    recipe['platforms'][0].update(stack=stack, version=version)
    path.write_text(yaml.safe_dump(recipe, sort_keys=False))
    for args in (['add', '.'], ['-c', 'user.email=test@example.org', '-c', 'user.name=Test', 'commit', '-m', 'distributed fixture']):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    revision = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True,
                              capture_output=True, text=True).stdout.strip()
    fjob = '01__test/000__ci_metadata/fournos_fjob.yaml'
    data = yaml.safe_load(artifacts[fjob])
    data['spec']['env']['FORGE_FOREIGN_TESTING_PULL_PULL_SHA'] = revision
    artifacts[fjob] = yaml.safe_dump(data)
    test_dir = '01__test/001__inference_playbooks_test_dir'
    config_path = test_dir + '/config.yaml'
    config = yaml.safe_load(artifacts[config_path])
    config['inference_playbooks']['manifest_path'] = f'models/test/recipes/test-recipe/manifests/{stack}-{version}/serving.yaml'
    artifacts[config_path] = yaml.safe_dump(config)
    del artifacts[test_dir + '/src/server-deployment.yaml']
    # Distributed captures need no pod status in the parent log, and no PVC/HF
    # preparation record. The common executed Forge command remains recorded.
    artifacts['01__test/run.log'] = artifacts['01__test/run.log'].splitlines()[0] + '\n'
    if kind == 'LeaderWorkerSet':
        image = 'docker.io/vllm/vllm-openai:v0.24.0'
        leader = {'name': 'vllm-leader', 'image': image, 'command': ['sh', '-c'], 'args': [
            'vllm serve example/base-FP8 \\\n  --served-model-name request-alias --nnodes 2 --node-rank 0']}
        worker = {'name': 'vllm-worker', 'image': image, 'command': ['sh', '-c'], 'args': [
            'vllm serve example/base-FP8 \\\n  --served-model-name request-alias --nnodes 2 --node-rank $(LWS_WORKER_INDEX) --headless']}
        manifest = {'kind': kind, 'spec': {'leaderWorkerTemplate': {
            'leaderTemplate': {'spec': {'containers': [leader]}}, 'workerTemplate': {'spec': {'containers': [worker]}}}}}
        source = test_dir + '/src/server-12345678-lws.yaml'
        capture = test_dir + '/003__capture_lws_state'
        pod_path = capture + '/artifacts/leaderworkerset.pods.yaml'
    else:
        image = 'registry.redhat.io/rhaiis/vllm-cuda-rhel9:3.5'
        override = {'name': 'main', 'env': [{'name': 'VLLM_ADDITIONAL_ARGS', 'value': '--kv-cache-dtype fp8'}]}
        manifest = {'kind': kind, 'spec': {'model': {'uri': 'pvc://existing-weights', 'name': 'request-alias'},
            'template': {'containers': [copy.deepcopy(override)]}, 'worker': {'containers': [copy.deepcopy(override)]}}}
        leader = {**copy.deepcopy(override), 'image': image, 'command': ['/bin/bash', '-c',
            'eval "exec vllm serve /weights/checkpoint --served-model-name request-alias ${VLLM_ADDITIONAL_ARGS} $@"', '--'],
            'volumeMounts': [{'name': 'weights', 'mountPath': '/weights/checkpoint', 'readOnly': True}]}
        worker = copy.deepcopy(leader)
        source = test_dir + '/src/server-12345678.yaml'
        capture = test_dir + '/003__capture_llmisvc_state'
        pod_path = capture + '/artifacts/llminferenceservice.pods.yaml'
    pods = []
    for container in (leader, worker):
        sidecar = {'name': 'sidecar', 'image': 'registry.example/sidecar:1'}
        pods.append({'kind': 'Pod', 'spec': {'containers': [copy.deepcopy(container), sidecar]}, 'status': {
            'containerStatuses': [
                {'name': container['name'], 'ready': True, 'imageID': image.split(':')[0] + '@sha256:' + 'c' * 64},
                {'name': 'sidecar', 'ready': True, 'imageID': 'sha256:' + 'd' * 64}],
            'initContainerStatuses': [{'name': 'init', 'ready': True, 'imageID': 'sha256:' + 'e' * 64}]}})
        if kind == 'LLMInferenceService':
            pods[-1]['spec']['volumes'] = [{'name': 'weights', 'persistentVolumeClaim': {'claimName': 'existing-weights'}}]
    if kind == 'LLMInferenceService':
        pods.append({'kind': 'Pod', 'spec': {'containers': [{'name': 'main', 'image': 'registry.example/router:1',
            'command': ['router']}]}, 'status': {'containerStatuses': [{'name': 'main', 'ready': True,
            'imageID': 'sha256:' + 'f' * 64}]}})
    artifacts[source] = yaml.safe_dump(manifest)
    artifacts[pod_path] = yaml.safe_dump({'kind': 'List', 'items': pods})

    def call(endpoint, body=None, raw=False):
        query = parse_qs(urlsplit(endpoint).query)
        assert query['run_id'] == ['d' * 32]
        if endpoint.startswith('/get-artifact?'):
            assert raw
            return artifacts[query['path'][0]].encode()
        directory = query['path'][0]
        assert directory in (test_dir, test_dir + '/src')
        return {'files': [{'path': source, 'is_dir': False}]} if directory.endswith('/src') else {
            'files': [{'path': capture, 'is_dir': True}]}

    client.request = call
    revision = record_launch(repo, artifacts, source)
    return repo, path, revision, client, parent, artifacts, raw, kind, source, pod_path


@pytest.mark.parametrize('declared_revision', [None, 'release/model-v2'])
def test_distributed_parent_import_replay_and_receipt(distributed_intake, declared_revision, monkeypatch):
    repo, path, revision, client, _, artifacts, raw, kind, source, pod_path = distributed_intake
    if declared_revision:
        manifest = yaml.safe_load(artifacts[source])
        if kind == 'LeaderWorkerSet':
            templates = manifest['spec']['leaderWorkerTemplate']
            containers = [templates[key]['spec']['containers'][0] for key in ('leaderTemplate', 'workerTemplate')]
        else:
            containers = [manifest['spec'][key]['containers'][0] for key in ('template', 'worker')]
        pods = yaml.safe_load(artifacts[pod_path])
        containers += [pod['spec']['containers'][0] for pod in pods['items'][:2]]
        for container in containers:
            if kind == 'LeaderWorkerSet':
                container['args'][0] += ' --revision=' + declared_revision
            else:
                container['env'][0]['value'] += ' --revision ' + declared_revision
        artifacts[source] = yaml.safe_dump(manifest)
        artifacts[pod_path] = yaml.safe_dump(pods)
        revision = record_launch(repo, artifacts, source)
    import_parent(client, repo, 'd' * 32, revision, 34, 123, require_parent=True)
    references = yaml.safe_load(path.read_text())['benchmark_runs']
    assert len(references) == 2
    for reference in references:
        run_path = path.parent / reference
        run = yaml.safe_load(run_path.read_text())
        assert run['environment']['model_revision'] == (declared_revision or 'main')
        assert run['environment']['image_digest'] == 'sha256:' + 'c' * 64
        assert run['platform']['stack'] == ('rhoai' if kind == 'LLMInferenceService' else 'vllm')
        assert run['forge']['mlflow_parent_run_id'] == run['forge']['mlflow_benchmark_run_id'] == 'd' * 32
        assert (run_path.parent / 'kpis.json').read_bytes() == raw
        assert validate_benchmark_run(repo, run_path, run)[0] == []
    for args in (['add', '.'], ['-c', 'user.email=test@example.org', '-c', 'user.name=Test', 'commit', '-m', 'distributed evidence']):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    client.request = client.run = client.kpis = lambda *a, **kw: pytest.fail('receipt hit contacted MLflow')
    monkeypatch.setattr('fetch_forge_evidence.normalize', lambda *a, **kw: pytest.fail('receipt hit normalized KPIs'))
    assert admitted_execution(repo, load_admissions(repo), client, 'd' * 32, revision, 34, {})


@pytest.mark.parametrize('failure', ['heterogeneous', 'missing-runtime', 'not-ready', 'wrong-model', 'wrong-kind', 'model-label-conflict'])
def test_distributed_parent_failure_does_not_write(distributed_intake, failure):
    repo, path, revision, client, _, artifacts, raw, kind, source, pod_path = distributed_intake
    pods = yaml.safe_load(artifacts[pod_path])
    if failure == 'heterogeneous':
        pods['items'][1]['status']['containerStatuses'][0]['imageID'] = 'sha256:' + 'b' * 64
    elif failure == 'missing-runtime':
        pods['items'] = []
    elif failure == 'not-ready':
        pods['items'][1]['status']['containerStatuses'][0]['ready'] = False
    elif failure == 'wrong-model':
        container = pods['items'][0]['spec']['containers'][0]
        key = 'args' if kind == 'LeaderWorkerSet' else 'command'
        container[key] = [arg.replace('example/base-FP8', 'example/wrong').replace('/weights/checkpoint', 'example/wrong')
                          for arg in container[key]]
    elif failure == 'wrong-kind':
        manifest = yaml.safe_load(artifacts[source])
        manifest['kind'] = 'StatefulSet'
        artifacts[source] = yaml.safe_dump(manifest)
    elif failure == 'model-label-conflict':
        document = json.loads(raw)
        document['tests'][0]['labels']['served_model'] = 'request-alias'
        client.kpis = lambda run_id: ('01__test/002__postprocessing/kpis/kpis.json', json.dumps(document).encode())
    artifacts[pod_path] = yaml.safe_dump(pods)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert path.read_bytes() == before
    assert not (path.parent / 'results').exists()


def test_lws_worker_can_omit_model(distributed_intake):
    repo, path, revision, client, _, artifacts, _, kind, source, pod_path = distributed_intake
    if kind != 'LeaderWorkerSet':
        pytest.skip('LWS worker-only command shape')
    manifest = yaml.safe_load(artifacts[source])
    worker = manifest['spec']['leaderWorkerTemplate']['workerTemplate']['spec']['containers'][0]
    worker['args'] = ['ray start --address=leader:6379 --block']
    pods = yaml.safe_load(artifacts[pod_path])
    pods['items'][1]['spec']['containers'][0] = copy.deepcopy(worker)
    artifacts[source], artifacts[pod_path] = yaml.safe_dump(manifest), yaml.safe_dump(pods)
    revision = record_launch(repo, artifacts, source)
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert len(yaml.safe_load(path.read_text())['benchmark_runs']) == 2


def test_lws_local_path_is_bound_to_tested_manifest(distributed_intake):
    repo, path, _, client, _, artifacts, _, kind, source, pod_path = distributed_intake
    if kind != 'LeaderWorkerSet':
        pytest.skip('LWS launch contract')
    artifacts[source] = artifacts[source].replace('example/base-FP8', '/weights/checkpoint')
    artifacts[pod_path] = artifacts[pod_path].replace('example/base-FP8', '/weights/checkpoint')
    revision = record_launch(repo, artifacts, source)
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert len(yaml.safe_load(path.read_text())['benchmark_runs']) == 2


def test_llmisvc_local_path_must_use_declared_pvc(distributed_intake):
    repo, path, revision, client, _, artifacts, _, kind, _, pod_path = distributed_intake
    if kind != 'LLMInferenceService':
        pytest.skip('LLMInferenceService PVC binding')
    artifacts[pod_path] = artifacts[pod_path].replace('existing-weights', 'wrong-weights')
    with pytest.raises(ValueError, match='model path disagrees'):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert not (path.parent / 'results').exists()


def test_llmisvc_unsupported_model_uri(distributed_intake):
    repo, _, revision, client, _, artifacts, _, kind, source, _ = distributed_intake
    if kind != 'LLMInferenceService':
        pytest.skip('LLMInferenceService model URI shape')
    manifest = yaml.safe_load(artifacts[source])
    manifest['spec']['model']['uri'] = 's3://unknown/weights'
    artifacts[source] = yaml.safe_dump(manifest)
    with pytest.raises(ValueError, match='configuration differs from tested manifest'):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)


@pytest.mark.parametrize('current_count', [0, 1, 2])
def test_kpi_discovery_ignores_archives_without_fallback(current_count):
    client = Client('https://mlflow.example', 'forge-sandbox', 'test', 'test')
    files = [{'path': '01__test/002__postprocessing/historical_data', 'is_dir': True}]
    files.extend({'path': f'01__test/{i:03}__postprocessing/kpis/kpis.json', 'is_dir': False}
                 for i in range(current_count))

    def request(endpoint, body=None, raw=False):
        query = parse_qs(urlsplit(endpoint).query)
        assert query.get('path', [''])[0] != '01__test/002__postprocessing/historical_data'
        return b'current' if raw else {'files': files}

    client.request = request
    if current_count == 2:
        with pytest.raises(ValueError, match='ambiguous'):
            client.kpis('d' * 32)
    else:
        assert client.kpis('d' * 32) == (('01__test/000__postprocessing/kpis/kpis.json', b'current')
                                           if current_count else None)


@pytest.mark.parametrize('labels', [
    {'model_name': 'example/base', 'served_model': 'example/base-FP8'},
    {'model_name': 'example/base', 'served_model': 'example/base-FP8', 'quantization': 'fp8'},
    {'model_name': 'example/base-FP8'},
    {'model_name': 'example/base-FP8', 'quantization': 'fp8'},
])
def test_exact_checkpoint_identity_accepted_and_preserved(variant_intake, kpis, labels):
    repo, path, revision, client, _, _ = variant_intake
    for test in kpis['tests']:
        test['labels'].update(labels)
    raw = client.kpis('a' * 32)[1]
    import_parent(client, repo, 'd' * 32, revision, 34)
    for run_path in path.parent.glob('results/*/run.yaml'):
        run = yaml.safe_load(run_path.read_text())
        for stored in run['workload']['tests']:
            assert {key: stored[key] for key in labels} == labels
        assert (run_path.parent / 'kpis.json').read_bytes() == raw
        result = json.loads((run_path.parent / 'result.json').read_text())
        if 'served_model' in labels:
            assert result['model_identity'] == labels
        else:
            assert 'model_identity' not in result  # Historical replay stays byte-compatible.
        assert validate_benchmark_run(repo, run_path, run)[0] == []


@pytest.mark.parametrize('model', [
    {'huggingface_id': 'example/base'},
    {'huggingface_id': 'example/base', 'quantizations': [{'name': 'fp8'}]},
    {'huggingface_id': 'example/base', 'quantizations': [
        {'name': 'fp8', 'huggingface_id': 'example/base-FP8-other'}]},
])
def test_quantization_requires_explicit_exact_checkpoint_declaration(model):
    document = {'tests': [{'labels': {'model_name': 'example/base',
        'served_model': 'example/base-FP8', 'quantization': 'fp8'}}]}
    with pytest.raises(ValueError, match='KPI quantization attribution'):
        validate_model_identity(document, {'serving': {'model': 'example/base-FP8'}}, model)


@pytest.mark.parametrize('model', [{}, {'huggingface_id': ''}])
def test_new_contract_requires_declared_base_identity(model):
    document = {'tests': [{'labels': {'model_name': 'example/base', 'served_model': 'example/base'}}]}
    with pytest.raises(ValueError, match='KPI base model attribution'):
        validate_model_identity(document, {'serving': {'model': 'example/base'}}, model)


@pytest.mark.parametrize('labels', [
    {'model_name': 'example/base', 'served_model': 'example/base-INT4'},
    {'model_name': 'wrong/base', 'served_model': 'example/base-FP8'},
    {'model_name': 'example/base', 'served_model': 'example/base-FP8', 'quantization': 'int4'},
    {'model_name': 'example/base'},
])
def test_offline_replay_rejects_consistently_wrong_model_identity(variant_intake, kpis, labels):
    repo, path, revision, client, _, _ = variant_intake
    for test in kpis['tests']:
        test['labels'].update(model_name='example/base', served_model='example/base-FP8')
    import_parent(client, repo, 'd' * 32, revision, 34)
    for test in kpis['tests']:
        test['labels'].pop('served_model', None)
        test['labels'].update(labels)
    # Even internally consistent raw evidence, checksums, labels and normalization
    # must not pass replay when they contradict the owning recipe/model.
    raw = json.dumps(kpis).encode()
    checksum = 'sha256:' + hashlib.sha256(raw).hexdigest()
    results = normalized(kpis)
    for run_path in path.parent.glob('results/*/run.yaml'):
        run = yaml.safe_load(run_path.read_text())
        run['forge']['kpis_checksum'] = checksum
        run['artifacts'][0]['checksum'] = checksum
        run['workload']['tests'] = [test['labels'] for test in kpis['tests']
                                  if test['labels']['benchmark_tool'] == run['harness']]
        (run_path.parent / 'kpis.json').write_bytes(raw)
        (run_path.parent / 'result.json').write_text(json.dumps(results[run['harness']]))
        assert 'KPI ' in str(validate_benchmark_run(repo, run_path, run)[0])


@pytest.mark.parametrize('field', ['model_name', 'served_model', 'quantization'])
def test_result_model_identity_schema_requires_nonempty_strings(kpis, field):
    result = normalized(kpis)['guidellm']
    result['model_identity'] = {field: ''}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(result, json.loads((REPO / 'schema/benchmark-result.schema.json').read_text()))


@pytest.mark.parametrize('labels', [
    {'model_name': 'example/base', 'served_model': 'request-alias'},
    {'model_name': 'example/base', 'served_model': 'example/base-INT4'},
    {'model_name': 'different/base', 'served_model': 'example/base-FP8'},
    {'model_name': 'example/base-FP8', 'served_model': 'example/base-FP8'},
    {'served_model': 'example/base-FP8'},
    {'model_name': 'example/base', 'served_model': ''},
    {'model_name': 'example/base'},
    {'model_name': 'example/base', 'quantization': 'fp8'},
    {'model_name': 'example/base', 'served_model': 'example/base-FP8', 'quantization': 'int4'},
    {'model_name': 'example/base-FP8', 'quantization': 'int4'},
    {'model_name': 'example/base-FP8', 'quantization': ''},
])
def test_ambiguous_or_mismatched_model_identity_rejected(variant_intake, kpis, labels):
    repo, path, revision, client, _, _ = variant_intake
    for test in kpis['tests']:
        test['labels'].pop('model_name', None)
        test['labels'].update(labels)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='KPI .*attribution'):
        import_parent(client, repo, 'd' * 32, revision, 34)
    assert path.read_bytes() == before
    assert not (path.parent / 'results').exists()


@pytest.mark.parametrize('field', ['served_model', 'quantization'])
def test_normalization_does_not_merge_distinct_model_labels(kpis, field):
    ai = copy.deepcopy(kpis['tests'][1])
    duplicate = copy.deepcopy(ai)
    duplicate['labels'].update(concurrency='32', **{field: 'different'})
    with pytest.raises(ValueError, match='mixed workload attribution'):
        normalized({'tests': [ai, duplicate]})


@pytest.mark.parametrize('model_revision', [None, '', ' ', ' main', 'main ', 'release candidate',
                                           'main\n', 'main\t', 'main\x00', 'main\x1b', 'main\x7f',
                                           'Bearer credential', 'password=credential', 'secret=credential',
                                           '--api-key=credential', 'https://user:credential@example.org/model'])
def test_invalid_model_revision_does_not_write(intake, model_revision):
    repo, path, revision, client, parent, values = intake
    tag = next(tag for tag in parent['data']['tags'] if tag['key'] == 'playbooks.model_revision')
    if model_revision is None:
        parent['data']['tags'].remove(tag)
    else:
        tag['value'] = model_revision
    before = path.read_bytes()
    with pytest.raises(ValueError):
        import_parent(client, repo, 'd' * 32, revision, 34)
    assert not (path.parent / 'results').exists()
    assert path.read_bytes() == before


@pytest.mark.parametrize('model_revision', ['', ' ', ' main', 'main ', 'release candidate',
                                           'main\n', 'main\t', 'main\x00', 'main\x1b', 'main\x7f'])
def test_schema_rejects_empty_or_non_identifier_model_revision(intake, model_revision):
    repo, path, revision, client, parent, values = intake
    import_parent(client, repo, 'd' * 32, revision, 34)
    run = yaml.safe_load(next(path.parent.glob('results/*/run.yaml')).read_text())
    run['environment']['model_revision'] = model_revision
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(run, json.loads((REPO / 'schema/benchmark-run.schema.json').read_text()))


@pytest.mark.parametrize('with_children', [False, True])
def test_successful_run_without_kpi_artifacts_does_not_write(intake, with_children):
    repo, path, revision, client, parent, values = intake
    if not with_children:
        parent['info']['run_id'] = 'd3512ac9760743f49667f9c4113328bc'
        client.children = lambda _: []
    client.kpis = lambda _: None
    with pytest.raises(NoKpiArtifactsError, match='no KPI artifacts'):
        import_parent(client, repo, parent['info']['run_id'], revision, 34)
    assert not (path.parent / 'results').exists()
    assert yaml.safe_load(path.read_text())['benchmark_runs'] == []


@pytest.mark.parametrize('raw', [b'not json', b'{}', b'{"tests": []}',
                                 b'{"tests": [{"labels": {"benchmark_tool": "unsupported"}}]}'])
def test_present_invalid_artifact_is_not_empty_evidence(intake, raw):
    repo, path, revision, client, parent, values = intake
    original_kpis = client.kpis
    # Prepare valid parent evidence before encountering the invalid child.
    client.kpis = lambda run_id: (original_kpis('a' * 32) if run_id == 'd' * 32
                                 else ('kpis.json', raw))
    with pytest.raises((ValueError, KeyError)) as error:
        import_parent(client, repo, 'd' * 32, revision, 34)
    assert not isinstance(error.value, NoKpiArtifactsError)
    assert not (path.parent / 'results').exists()
    assert yaml.safe_load(path.read_text())['benchmark_runs'] == []


@pytest.mark.parametrize('failure', [TimeoutError('unavailable'), PermissionError('forbidden'),
                                    ValueError('ambiguous kpis.json artifacts')])
def test_artifact_discovery_failure_is_not_empty_evidence(intake, failure):
    repo, path, revision, client, parent, values = intake
    def kpis(run_id):
        if run_id == 'a' * 32:
            raise failure
        return None
    client.kpis = kpis
    with pytest.raises(type(failure)) as error:
        import_parent(client, repo, 'd' * 32, revision, 34)
    assert error.value is failure
    assert not (path.parent / 'results').exists()


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


@pytest.fixture
def legacy_presentation_intake(intake):
    repo, path, _, client, parent, values = intake
    model_path = repo / 'models/test/model.yaml'
    model_path.write_text('presentation:\n  provider: Example\n')
    commit_evidence(repo)
    revision = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True,
                              capture_output=True, text=True).stdout.strip()
    next(tag for tag in parent['data']['tags'] if tag['key'] == 'playbooks.tested_revision')['value'] = revision
    model_path.write_text('{}\n')
    commit_evidence(repo)
    return repo, path, revision, client, parent, values


def test_removed_legacy_model_presentation_allows_import_and_local_admission(legacy_presentation_intake):
    repo, path, revision, client, parent, values = legacy_presentation_intake
    import_parent(client, repo, 'd' * 32, revision, 34, 123, require_parent=True)
    commit_evidence(repo)
    for method in ('run', 'children', 'kpis', 'request'):
        setattr(client, method, lambda *args, **kwargs: pytest.fail('admitted execution must stay local'))
    assert admitted_execution(repo, load_admissions(repo), client, 'd' * 32, revision, 34, {})
    (repo / 'models/test/model.yaml').write_text('presentation:\n  provider: Dirty\n')
    with pytest.raises(ValueError, match='dirty Forge deployment input'):
        admitted_execution(repo, load_admissions(repo), client, 'd' * 32, revision, 34, {})


@pytest.mark.parametrize('change', [
    {'huggingface_id': 'different/model'}, {'quantizations': [{'name': 'INT4'}]},
    {'model_type': 'different'}, {'architectures': ['DifferentArchitecture']},
    {'source': 'huggingface'}, {'future_field': 'must still match'},
])
def test_legacy_presentation_does_not_hide_other_model_changes(legacy_presentation_intake, change):
    repo, path, revision, client, parent, values = legacy_presentation_intake
    (repo / 'models/test/model.yaml').write_text(yaml.safe_dump(change))
    with pytest.raises(ValueError, match='merged deployment inputs differ'):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert not (path.parent / 'results').exists()


@pytest.mark.parametrize('failure', ['override', 'config', 'recipe', 'duplicate-model-key',
                                    'model-symlink', 'model-mode'])
def test_legacy_model_exception_preserves_other_input_checks(legacy_presentation_intake, failure):
    repo, path, revision, client, parent, values = legacy_presentation_intake
    if failure == 'duplicate-model-key':
        (repo / 'models/test/model.yaml').write_text('presentation: {}\npresentation: {}\n')
    elif failure == 'model-symlink':
        target = repo / 'models/test/model.yaml'
        target.unlink()
        target.symlink_to('recipes/test-recipe/platforms/vllm.yaml')
    elif failure == 'model-mode':
        (repo / 'models/test/model.yaml').chmod(0o755)
    elif failure == 'recipe':
        path.write_text(path.read_text() + 'presentation: {}\n')
    else:
        target = path.parent / ('platforms/vllm.yaml' if failure == 'override' else 'config/patch.yaml')
        target.write_text('presentation: {}\n')
    with pytest.raises((ValueError, yaml.YAMLError)):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
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


@pytest.mark.parametrize('workspace', ['forge-sandbox', 'forge-inference-playbooks'])
def test_workspace_header_auth_timeout_and_raw_download(workspace):
    import io
    import base64
    client = Client('https://mlflow.example', workspace, 'user', 'password')
    observed = []
    class Opener:
        def open(self, request, timeout):
            observed.append((request, timeout))
            return io.BytesIO(b'{"test": 1}')
    client.opener = Opener()
    assert client.request('/get-artifact?run_id=abc', raw=True) == b'{"test": 1}'
    request, timeout = observed[0]
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers['x-mlflow-workspace'] == workspace
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


@pytest.mark.parametrize('failure', [None, NoKpiArtifactsError('empty'),
                                    ValueError('stale or mismatched tested revision'), TimeoutError('unavailable')])
@pytest.mark.parametrize('workspace', ['forge-sandbox', 'forge-inference-playbooks'])
def test_merged_pr_discovery_intake(monkeypatch, tmp_path, capsys, failure, workspace):
    import ingest_forge
    calls = []
    monkeypatch.setenv('GITHUB_REPOSITORY', 'org/repo')
    for key, value in {'MLFLOW_TRACKING_URI': 'https://mlflow.example', 'MLFLOW_WORKSPACE': 'forge-sandbox',
                       'MLFLOW_USERNAME': 'user',
                       'MLFLOW_PASSWORD': 'password'}.items():
        monkeypatch.setenv(key, value)
    comment = forge_report(workspace=workspace)
    comments = [forge_report('a' * 32, comment_id=122), comment]
    pr = {'number': 34, 'merged_at': '2026-10-14', 'base': {'ref': 'main'},
          'head': {'sha': 'b' * 40, 'ref': 'recipe-branch'}}
    def github(endpoint, paginated=False):
        return comments if endpoint.endswith('/comments') else pr
    monkeypatch.setattr(ingest_forge, 'github', github)
    monkeypatch.setattr(ingest_forge.subprocess, 'run', lambda *a, **kw: None)
    monkeypatch.setattr(ingest_forge, 'load_admissions', lambda repo: {})
    def import_run(*args, **kwargs):
        calls.append((args, kwargs))
        if failure is not None:
            raise failure
    monkeypatch.setattr(ingest_forge, 'import_parent', import_run)
    monkeypatch.setattr(sys, 'argv', ['ingest_forge.py', '--pr-number', '34', '--repo', str(tmp_path)])
    if failure is not None:
        with pytest.raises(type(failure)) as error:
            ingest_forge.main()
        assert error.value is failure
    else:
        ingest_forge.main()
    assert len(calls) == 1
    assert calls[0][0][0].headers['X-MLflow-Workspace'] == workspace
    assert calls[0][0][1:] == (tmp_path, 'd' * 32, 'b' * 40, 34, 123)
    assert calls[0][1] == {'require_parent': True, 'admissions': {}}
    assert 'skipping' not in capsys.readouterr().out
    calls.clear()
    comments.clear()
    ingest_forge.main()
    assert 'no trusted successful Forge comment; no import' in capsys.readouterr().out
    assert calls == []
    comments.append(comment)
    pr['head']['ref'] = 'automation/forge-evidence-existing'
    ingest_forge.main()
    assert calls == []


def test_push_routes_each_pr_to_its_report_workspace(monkeypatch, tmp_path):
    import ingest_forge
    for key, value in {'GITHUB_REPOSITORY': 'org/repo', 'MLFLOW_TRACKING_URI': 'https://mlflow.example',
                       'MLFLOW_WORKSPACE': 'forge-sandbox', 'MLFLOW_USERNAME': 'user',
                       'MLFLOW_PASSWORD': 'password'}.items():
        monkeypatch.setenv(key, value)
    workspaces = {34: 'forge-inference-playbooks', 35: 'forge-sandbox'}
    prs = {number: {'number': number, 'merged_at': '2026-10-14', 'base': {'ref': 'main'},
                    'head': {'sha': 'b' * 40, 'ref': f'recipe-{number}'}} for number in workspaces}
    def github(endpoint, paginated=False):
        if '/commits/' in endpoint:
            return list(prs.values())
        number = int(endpoint.split('/')[-2 if endpoint.endswith('/comments') else -1])
        return [forge_report(workspace=workspaces[number])] if endpoint.endswith('/comments') else prs[number]
    monkeypatch.setattr(ingest_forge, 'github', github)
    monkeypatch.setattr(ingest_forge.subprocess, 'run', lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, stdout='commit\n'))
    scans = []
    admissions = {}
    def load(repo):
        scans.append(repo)
        return admissions
    monkeypatch.setattr(ingest_forge, 'load_admissions', load)
    calls = []
    def import_run(client, repo, parent_id, revision, number, comment_id, **kwargs):
        assert kwargs == {'require_parent': True, 'admissions': admissions}
        calls.append((number, client.headers['X-MLflow-Workspace'], parent_id))
    monkeypatch.setattr(ingest_forge, 'import_parent', import_run)
    monkeypatch.setattr(sys, 'argv', ['ingest_forge.py', '--before', 'a' * 40,
                                    '--head', 'b' * 40, '--repo', str(tmp_path)])
    ingest_forge.main()
    assert calls == [(number, workspace, 'd' * 32) for number, workspace in workspaces.items()]
    assert scans == [tmp_path]


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


def commit_evidence(repo):
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.email=test@example.org', '-c', 'user.name=Test',
                    'commit', '-m', 'admitted evidence'], check=True, capture_output=True)


@pytest.mark.parametrize('status', ['RUNNING', 'SCHEDULED', 'UNKNOWN'])
def test_ingestion_requires_terminal_children_before_writes(intake, status):
    repo, path, revision, client, parent, values = intake
    child = client.children(parent)[0]
    child['info']['status'] = status
    original_kpis = client.kpis
    client.kpis = lambda _: original_kpis('a' * 32)
    with pytest.raises(ValueError, match='nonterminal direct children'):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert not (path.parent / 'results').exists()
    assert yaml.safe_load(path.read_text())['benchmark_runs'] == []


@pytest.mark.parametrize('status', ['FAILED', 'KILLED'])
def test_terminal_failed_children_remain_ineligible(intake, status):
    repo, path, revision, client, parent, values = intake
    client.children(parent)[0]['info']['status'] = status
    raw = json.dumps(json.loads((REPO / 'tests/fixtures/forge-kpis-pr34-example.json').read_text())).encode()
    client.kpis = lambda run_id: ('kpis.json', raw) if run_id == 'd' * 32 else pytest.fail('ineligible child')
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert len(list(path.parent.glob('results/*/forge-import.json'))) == 1


def test_primary_child_rejected_but_direct_fetch_still_accepts_child(intake):
    repo, path, revision, client, parent, values = intake
    child = client.children(parent)[0]
    child['data']['tags'] = [{'key': 'mlflow.parentRunId', 'value': 'd' * 32}]
    client.run = lambda run_id: child if run_id == 'a' * 32 else parent
    with pytest.raises(ValueError, match='not a parent'):
        import_parent(client, repo, 'a' * 32, revision, 34, require_parent=True)
    assert not (path.parent / 'results').exists()
    import_parent(client, repo, 'a' * 32, revision, 34)
    assert not list(path.parent.glob('results/*/forge-import.json'))


def test_selected_parent_revision_cannot_be_overridden_by_child(intake):
    repo, path, revision, client, parent, values = intake
    parent['data']['tags'][1]['value'] = 'f' * 40
    client.children(parent)[0]['data']['tags'] = [{'key': 'playbooks.tested_revision', 'value': revision}]
    with pytest.raises(ValueError, match='selected execution tested revision mismatch'):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert not (path.parent / 'results').exists()


def test_legacy_replay_admits_once_without_changing_comment_provenance(intake):
    repo, path, revision, client, parent, values = intake
    import_parent(client, repo, 'd' * 32, revision, 34, 123)
    commit_evidence(repo)
    assert load_admissions(repo) == {}
    before = {p: p.read_bytes() for p in path.parent.glob('results/*/*')}
    admissions = {}
    import_parent(client, repo, 'd' * 32, revision, 34, 999, require_parent=True, admissions=admissions)
    assert len(admissions) == 1
    assert all(p.read_bytes() == raw for p, raw in before.items())
    receipt = next(path.parent.glob('results/*/forge-import.json'))
    receipt_before = receipt.read_bytes()
    import_parent(client, repo, 'd' * 32, revision, 34, 1000, require_parent=True)
    assert receipt.read_bytes() == receipt_before
    commit_evidence(repo)
    assert admitted_execution(repo, load_admissions(repo), client, 'd' * 32, revision, 34, {})


@pytest.mark.parametrize('failure', ['dirty-run', 'dirty-raw', 'staged-result', 'blob-mismatch',
                                    'missing-output', 'subset-receipt', 'wrong-owner', 'duplicate-key'])
def test_invalid_admission_fails_closed(intake, failure):
    repo, path, revision, client, parent, values = intake
    import_parent(client, repo, 'd' * 32, revision, 34, 123, require_parent=True)
    commit_evidence(repo)
    receipt_path = next(path.parent.glob('results/*/forge-import.json'))
    receipt = json.loads(receipt_path.read_text())
    anchor = repo / receipt['outputs'][0]['run_path']
    if failure in {'dirty-run', 'dirty-raw', 'staged-result', 'blob-mismatch'}:
        name = {'dirty-run': 'run.yaml', 'dirty-raw': 'kpis.json',
                'staged-result': 'result.json', 'blob-mismatch': 'kpis.json'}[failure]
        target = anchor.parent / name
        target.write_text(target.read_text() + '\n')
        if failure == 'staged-result':
            subprocess.run(['git', '-C', str(repo), 'add', str(target)], check=True)
        elif failure == 'blob-mismatch':
            commit_evidence(repo)
    elif failure == 'missing-output':
        recipe = yaml.safe_load(path.read_text())
        recipe['benchmark_runs'] = [str(anchor.relative_to(path.parent))]
        path.write_text(yaml.safe_dump(recipe))
        commit_evidence(repo)
    elif failure == 'wrong-owner':
        receipt['outputs'][0]['recipe_path'] = 'models/other/recipes/other/recipe.yaml'
        receipt_path.write_text(json.dumps(receipt))
        commit_evidence(repo)
    elif failure == 'subset-receipt':
        receipt['outputs'] = receipt['outputs'][:1]
        receipt_path.write_text(json.dumps(receipt))
        commit_evidence(repo)
    else:
        receipt_path.write_text(receipt_path.read_text().replace('"version": 1', '"version": 1, "version": 1'))
        commit_evidence(repo)
    with pytest.raises(ValueError):
        load_admissions(repo)


def test_unreferenced_receipt_cannot_authorize_skip(intake):
    repo, path, revision, client, parent, values = intake
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    anchor = next(path.parent.glob('results/*/forge-import.json')).parent / 'run.yaml'
    recipe = yaml.safe_load(path.read_text())
    recipe['benchmark_runs'].remove(str(anchor.relative_to(path.parent)))
    path.write_text(yaml.safe_dump(recipe))
    commit_evidence(repo)
    assert load_admissions(repo) == {}


@pytest.mark.parametrize('reference_kind', ['untracked-alias', 'committed-alias', 'worktree-component',
                                          'dot', 'parent', 'double-slash'])
def test_admission_rejects_noncanonical_or_symlink_references(intake, reference_kind):
    repo, path, revision, client, parent, values = intake
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    commit_evidence(repo)
    if reference_kind == 'worktree-component':
        (path.parent / 'results').rename(path.parent / 'saved-results')
        (path.parent / 'results').symlink_to('saved-results', target_is_directory=True)
        with pytest.raises(ValueError, match='dirty|symlink'):
            load_admissions(repo)
        return
    recipe = yaml.safe_load(path.read_text())
    if reference_kind.endswith('alias'):
        recipe['benchmark_runs'] = [reference.replace('results/', 'alias/', 1)
                                    for reference in recipe['benchmark_runs']]
    else:
        replacement = {'dot': './results/', 'parent': 'results/../results/',
                       'double-slash': 'results//'}[reference_kind]
        recipe['benchmark_runs'] = [reference.replace('results/', replacement, 1)
                                    for reference in recipe['benchmark_runs']]
    path.write_text(yaml.safe_dump(recipe))
    if reference_kind == 'committed-alias':
        (path.parent / 'alias').symlink_to('results', target_is_directory=True)
    commit_evidence(repo)
    if reference_kind == 'untracked-alias':
        (path.parent / 'alias').symlink_to('results', target_is_directory=True)
    with pytest.raises(ValueError, match='canonical|committed|symlink'):
        load_admissions(repo)


def test_admission_identity_is_namespaced_and_legacy_conflicts_are_local(intake):
    repo, path, revision, client, parent, values = intake
    import_parent(client, repo, 'd' * 32, revision, 34)
    commit_evidence(repo)
    legacy = load_admissions(repo)
    with pytest.raises(ValueError, match='PR/revision'):
        admitted_execution(repo, legacy, client, 'd' * 32, 'f' * 40, 34, {})
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    commit_evidence(repo)
    admissions = load_admissions(repo)
    client.headers = {'X-MLflow-Workspace': 'forge-inference-playbooks'}
    assert not admitted_execution(repo, admissions, client, 'd' * 32, revision, 34, {})
    client.headers = {'X-MLflow-Workspace': 'forge-sandbox'}
    client.uri = 'https://other.example'
    assert not admitted_execution(repo, admissions, client, 'd' * 32, revision, 34, {})


def test_uncommitted_admission_sources_cannot_authorize_skip(intake):
    repo, path, revision, client, parent, values = intake
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    with pytest.raises(ValueError, match='dirty Forge admission source'):
        load_admissions(repo)


def test_legacy_subset_discovery_cannot_admit_parent(intake):
    repo, path, revision, client, parent, values = intake
    child = client.children(parent)[0]
    sibling = copy.deepcopy(child)
    sibling['info']['run_id'] = 'b' * 32
    original_kpis = client.kpis
    client.children = lambda _: [child, sibling]
    client.kpis = lambda run_id: original_kpis('a' * 32) if run_id != 'd' * 32 else None
    import_parent(client, repo, 'd' * 32, revision, 34)
    commit_evidence(repo)
    admissions = load_admissions(repo)
    client.children = lambda _: [child]
    before = {p: p.read_bytes() for p in path.parent.glob('results/*/*')}
    with pytest.raises(ValueError, match='omitted previously committed positive outputs'):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True, admissions=admissions)
    assert before == {p: p.read_bytes() for p in path.parent.glob('results/*/*')}
    assert not list(path.parent.glob('results/*/forge-import.json'))


@pytest.mark.parametrize('failure', ['pr', 'revision', 'platform', 'profile', 'model', 'config', 'untracked'])
def test_admission_hit_preserves_current_attribution(intake, failure):
    repo, path, revision, client, parent, values = intake
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    commit_evidence(repo)
    if failure == 'platform':
        (path.parent / 'platforms/vllm.yaml').write_text('args: [changed]\n')
    elif failure == 'profile':
        (repo / 'hardware-profiles/test.yaml').write_text('profile_revision: 2\n')
    elif failure == 'model':
        (repo / 'models/test/model.yaml').write_text('huggingface_id: changed/model\n')
    elif failure == 'config':
        (path.parent / 'config/patch.yaml').write_text('args: [changed]\n')
    elif failure == 'untracked':
        (path.parent / 'config/new.yaml').write_text('{}\n')
    with pytest.raises(ValueError):
        admitted_execution(repo, load_admissions(repo), client, 'd' * 32,
                           'f' * 40 if failure == 'revision' else revision,
                           35 if failure == 'pr' else 34, {})


@pytest.mark.parametrize('indexed', [False, True])
@pytest.mark.parametrize('workspace', ['forge-sandbox', 'forge-inference-playbooks'])
def test_latest_only_thousand_reports_scale_and_privacy(intake, monkeypatch, capsys, indexed, workspace):
    import fetch_forge_evidence
    import forge_admission
    import ingest_forge
    import normalize_kpis
    repo, path, revision, client, parent, values = intake
    client.headers = {'X-MLflow-Workspace': workspace}
    if indexed:
        import_parent(client, repo, 'd' * 32, revision, 34, 123, require_parent=True)
        commit_evidence(repo)
    before = {p: p.read_bytes() for p in repo.glob('models/**/*') if p.is_file()}
    reports = [forge_report(f'{i:032x}', comment_id=i) for i in range(1, 1001)]
    reports.append(forge_report(comment_id=1001, label='MLFlow Test Results', workspace=workspace))
    pr = {'merged_at': '2026-10-15', 'base': {'ref': 'main'},
          'head': {'sha': revision, 'ref': 'recipe-branch'}}
    monkeypatch.setenv('GITHUB_REPOSITORY', 'org/repo')
    for key, value in {'MLFLOW_TRACKING_URI': client.uri, 'MLFLOW_WORKSPACE': 'forge-sandbox',
                       'MLFLOW_USERNAME': 'private-user', 'MLFLOW_PASSWORD': 'private-password'}.items():
        monkeypatch.setenv(key, value)
    def make_client(uri, selected_workspace, *args):
        assert uri == client.uri
        assert selected_workspace == workspace
        return client
    monkeypatch.setattr(ingest_forge, 'Client', make_client)
    monkeypatch.setattr(ingest_forge, 'github', lambda endpoint, *args: reports if endpoint.endswith('/comments') else pr)
    original_git = subprocess.run
    git_calls = []
    def run(command, **kwargs):
        git_calls.append(command)
        if command[:2] == ['git', 'fetch']:
            return subprocess.CompletedProcess(command, 0)
        return original_git(command, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    requests = []
    for method in ('run', 'children', 'kpis'):
        original = getattr(client, method)
        def observe(*args, method=method, original=original):
            requests.append((method, args))
            if indexed:
                pytest.fail('indexed selected execution must not request MLflow')
            return original(*args)
        monkeypatch.setattr(client, method, observe)
    if indexed:
        def no_normalize(*args, **kwargs):
            pytest.fail('indexed execution must not normalize')
        monkeypatch.setattr(fetch_forge_evidence, 'normalize', no_normalize)
        monkeypatch.setattr(normalize_kpis, 'normalize', no_normalize)
        original_read_text, original_read_bytes = Path.read_text, Path.read_bytes
        def metadata_only_text(file, *args, **kwargs):
            assert file.name not in {'kpis.json', 'result.json'}
            return original_read_text(file, *args, **kwargs)
        def metadata_only_bytes(file, *args, **kwargs):
            assert file.name not in {'kpis.json', 'result.json'}
            return original_read_bytes(file, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', metadata_only_text)
        monkeypatch.setattr(Path, 'read_bytes', metadata_only_bytes)
    monkeypatch.setattr(sys, 'argv', ['ingest_forge.py', '--pr-number', '34', '--repo', str(repo)])
    ingest_forge.main()
    captured = capsys.readouterr()
    output = captured.out
    assert captured.err == ''
    assert len(output.splitlines()) == 1
    assert 'private-user' not in output and 'private-password' not in output
    for receipt_path in path.parent.glob('results/*/forge-import.json'):
        receipt_text = receipt_path.read_text()
        assert 'private-user' not in receipt_text and 'private-password' not in receipt_text
    assert sum('ls-tree' in command for command in git_calls) == 1
    assert sum('status' in command for command in git_calls) == 1
    if indexed:
        assert requests == []
        assert 'imported=0 indexed=1' in output
        assert sum('show' in command for command in git_calls) == 1
        monkeypatch.setattr(Path, 'read_bytes', original_read_bytes)
        assert before == {p: p.read_bytes() for p in repo.glob('models/**/*') if p.is_file()}
    else:
        assert [args[0] for method, args in requests if method == 'run'] == ['d' * 32]
        assert [args[0] for method, args in requests if method == 'kpis'] == ['d' * 32, 'a' * 32]
        assert 'imported=1 indexed=0' in output
        for run_path in path.parent.glob('results/*/run.yaml'):
            forge = yaml.safe_load(run_path.read_text())['forge']
            assert forge['mlflow_workspace'] == workspace
            assert f'workspace={workspace}' in forge['mlflow_run_url']
        for receipt_path in path.parent.glob('results/*/forge-import.json'):
            assert json.loads(receipt_path.read_text())['workspace'] == workspace


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
