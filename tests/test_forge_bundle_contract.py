"""Forge PR322 contract fixtures and pre-merge evidence applicability proofs."""
import copy
import hashlib
import json
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from test_forge_evidence import (commit_evidence, distributed_fixture, intake, kpis,
                                 parent_intake, variant_intake)
from fetch_forge_evidence import import_parent
from forge_admission import admitted_execution, load_admissions
from forge_bundle_contract import SOURCE, SUPPORTED
from normalize_kpis import normalize
from validate import validate_benchmark_run


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture(params=['Deployment', 'LeaderWorkerSet', 'LLMInferenceService'])
def bundle_intake(request, parent_intake, monkeypatch):
    fixture = parent_intake if request.param == 'Deployment' else distributed_fixture(parent_intake, request.param)
    repo, path, _, client, parent, artifacts, raw = fixture[:7]
    config_path = SOURCE.rsplit('/', 1)[0] + '/config.yaml'
    config = yaml.safe_load(artifacts[config_path])['inference_playbooks']
    selected = repo / config['manifest_path']
    source = yaml.safe_load(selected.read_text())
    source['apiVersion'] = SUPPORTED[source['kind']]
    source['metadata'] = {'name': 'test-server'}
    # PR19 serves a local model path while model_name retains the canonical HF ID.
    if source['kind'] == 'LeaderWorkerSet':
        local_model = '/models/Test-Model'
        source = json.loads(json.dumps(source).replace('example/base-FP8', local_model))
        for key in artifacts:
            if key.endswith('/leaderworkerset.pods.yaml'):
                artifacts[key] = artifacts[key].replace('example/base-FP8', local_model)
        recipe = yaml.safe_load(path.read_text())
        recipe['serving']['model'] = local_model
        path.write_text(yaml.safe_dump(recipe, sort_keys=False))
        document = json.loads(raw)
        for test in document['tests']:
            test['labels']['served_model'] = local_model
            test['labels'].pop('quantization', None)
        raw = json.dumps(document).encode()
        client.kpis = lambda run_id: ('01__test/002__postprocessing/kpis/kpis.json', raw)
    # Distributed fixture retains a previous Deployment in the same platform.
    for other in selected.parent.glob('*.yaml'):
        if other != selected:
            other.unlink()
    selected.write_text(yaml.safe_dump(source, sort_keys=False))
    recipe = yaml.safe_load(path.read_text())
    recipe['schema_version'] = 4
    path.write_text(yaml.safe_dump(recipe, sort_keys=False))
    commit_evidence(repo)
    revision = git(repo, 'rev-parse', 'HEAD')
    config.update(recipe='test/recipes/test-recipe', namespace='test-namespace',
                  manifest_path=str(selected.relative_to(path.parent)), workload='ci-quick,aiperf-smoke')
    artifacts[config_path] = yaml.safe_dump({'inference_playbooks': config})
    fjob_path = '01__test/000__ci_metadata/fournos_fjob.yaml'
    fjob = yaml.safe_load(artifacts[fjob_path])
    fjob['spec']['env']['FORGE_FOREIGN_TESTING_PULL_PULL_SHA'] = revision
    artifacts[fjob_path] = yaml.safe_dump(fjob)
    submitted = copy.deepcopy(source)
    submitted['metadata']['namespace'] = 'test-namespace'
    metadata = {'contract_version': 1, 'recipe_id': recipe['recipe_id'],
                'recipe_path': str(path.relative_to(repo)), 'recipe_revision': revision,
                'forge_revision': '5' * 40, 'target_namespace': 'test-namespace',
                'source_files': [{'path': str(selected.relative_to(repo)),
                                  'sha256': hashlib.sha256(selected.read_bytes()).hexdigest(),
                                  'document_count': 1}]}
    for name, document in [('source', source), ('submitted', submitted)]:
        text = yaml.safe_dump_all([document], sort_keys=False)
        artifacts[f'{SOURCE}/{name}-bundle.yaml'] = text
        metadata[f'{name}_bundle_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    artifacts[SOURCE + '/bundle-metadata.yaml'] = yaml.safe_dump(metadata)
    monkeypatch.setattr('forge_workload_contract.fetch_profiles', lambda revision: (
        f'https://raw.githubusercontent.com/openshift-psap/forge/{revision}/projects/rhaiis/orchestration/config.d/workloads.yaml',
        yaml.safe_dump({'ci-quick': {'rates': [1, 5]},
                        'aiperf-smoke': {'tool': 'aiperf', 'concurrency': 1}}).encode()))
    benchmark_steps = {}
    for test in json.loads(raw)['tests']:
        harness = test['labels']['benchmark_tool']
        directory = '01__test/' + test['run_id']
        key = 'ci-quick' if harness == 'guidellm' else 'aiperf-smoke'
        artifacts[directory + '/__caliper_test_metadata__.yaml'] = yaml.safe_dump({
            'labels': {'phase': 'benchmark', 'benchmark_tool': harness},
            'kpi_labels': {'benchmark_key': key}})
        step = directory + f'/001__run_{harness}_benchmark'
        benchmark_steps[directory] = step
        command = (['guidellm', 'run', '--profile={"kind":"concurrent","streams":[1,5]}']
                   if harness == 'guidellm' else ['aiperf', 'profile', '--concurrency', '1'])
        artifacts[step + f'/src/{harness}-job.yaml'] = yaml.safe_dump({
            'kind': 'Job', 'spec': {'template': {'spec': {'containers': [{'command': command}]}}}})
    original_request = client.request

    def call(endpoint, body=None, raw=False):
        query = parse_qs(urlsplit(endpoint).query)
        if endpoint.startswith('/get-artifact?'):
            return artifacts[query['path'][0]].encode()
        if query['path'][0] in benchmark_steps:
            return {'files': [{'path': benchmark_steps[query['path'][0]], 'is_dir': True}]}
        return original_request(endpoint, body, raw)

    client.request = call
    return repo, path, revision, client, parent, artifacts, raw, selected


def test_bundle_import_replay_and_receipt(bundle_intake):
    repo, path, revision, client, _, artifacts, raw, selected = bundle_intake
    import_parent(client, repo, 'd' * 32, revision, 34, 123, require_parent=True)
    for reference in yaml.safe_load(path.read_text())['benchmark_runs']:
        run_path = path.parent / reference
        run = yaml.safe_load(run_path.read_text())
        assert run['forge']['tested_revision'] == revision
        assert (run_path.parent / 'kpis.json').read_bytes() == raw
        assert validate_benchmark_run(repo, run_path, run)[0] == []
    commit_evidence(repo)
    client.request = client.run = client.kpis = lambda *a, **kw: pytest.fail('receipt contacted MLflow')
    assert admitted_execution(repo, load_admissions(repo), client, 'd' * 32, revision, 34, {})


@pytest.mark.parametrize('failure', ['version', 'recipe', 'revision', 'producer', 'inventory', 'checksum',
                                    'submitted', 'source', 'namespace', 'manifest', 'yaml', 'tag-conflict'])
def test_bundle_rejects_invalid_attribution_before_writing(bundle_intake, failure):
    repo, path, revision, client, parent, artifacts, _, selected = bundle_intake
    key = SOURCE + '/bundle-metadata.yaml'
    metadata = yaml.safe_load(artifacts[key])
    if failure == 'version':
        metadata['contract_version'] = 2
    elif failure == 'recipe':
        metadata['recipe_id'] = 'different'
    elif failure == 'revision':
        metadata['recipe_revision'] = 'a' * 40
    elif failure == 'producer':
        metadata['forge_revision'] = 'not-a-sha'
    elif failure == 'inventory':
        metadata['source_files'] = []
    elif failure == 'checksum':
        metadata['submitted_bundle_sha256'] = '0' * 64
    elif failure in {'submitted', 'source'}:
        artifact = f'{SOURCE}/{failure}-bundle.yaml'
        document = yaml.safe_load(artifacts[artifact])
        document['spec']['replicas'] = 99
        artifacts[artifact] = yaml.safe_dump(document)
        metadata[f'{failure}_bundle_sha256'] = hashlib.sha256(artifacts[artifact].encode()).hexdigest()
    elif failure == 'namespace':
        metadata['target_namespace'] = 'elsewhere'
    elif failure == 'manifest':
        selected.write_text(selected.read_text() + '\n# changed\n')
    elif failure == 'tag-conflict':
        parent['data']['tags'] = [{'key': 'playbooks.recipe_path', 'value': 'models/other/recipe.yaml'}]
    artifacts[key] = yaml.safe_dump(metadata)
    if failure == 'yaml':
        artifacts[key] = 'contract_version: 1\ncontract_version: secret-do-not-echo\n'
    before = path.read_bytes()
    with pytest.raises(ValueError) as error:
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert 'secret-do-not-echo' not in str(error.value)
    assert path.read_bytes() == before
    assert not (path.parent / 'results').exists()


def test_import_after_main_merge_preserves_original_tested_sha(bundle_intake):
    repo, path, revision, client, _, _, _, selected = bundle_intake
    # Model PR19: benchmark A; land importer changes on main; merge main into PR.
    git(repo, 'branch', 'tooling-main')
    git(repo, 'switch', '-c', 'recipe-pr')
    (repo / 'review-notes.txt').write_text('Contributor review notes\n')
    commit_evidence(repo)
    git(repo, 'switch', 'tooling-main')
    (repo / 'importer-update.txt').write_text('Importer-only update\n')
    commit_evidence(repo)
    git(repo, 'switch', 'recipe-pr')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.org',
        'merge', '--no-ff', 'tooling-main', '-m', 'Merge importer changes')
    assert git(repo, 'rev-parse', 'HEAD') != revision
    git(repo, 'merge-base', '--is-ancestor', revision, 'HEAD')
    import_parent(client, repo, 'd' * 32, revision, 34, 123, require_parent=True)
    for reference in yaml.safe_load(path.read_text())['benchmark_runs']:
        assert yaml.safe_load((path.parent / reference).read_text())['forge']['tested_revision'] == revision
    before = {p: p.read_bytes() for p in (path.parent / 'results').rglob('*') if p.is_file()}
    commit_evidence(repo)
    import_parent(client, repo, 'd' * 32, revision, 34, 123, require_parent=True)
    assert before == {p: p.read_bytes() for p in (path.parent / 'results').rglob('*') if p.is_file()}


@pytest.mark.parametrize('change', ['add', 'remove', 'model', 'profile', 'recipe'])
def test_changed_inputs_after_test_are_rejected(bundle_intake, change):
    repo, path, revision, client, _, _, _, selected = bundle_intake
    if change == 'add':
        (selected.parent / 'extra.yaml').write_text('{}\n')
    elif change == 'remove':
        selected.unlink()
    else:
        target = {'model': repo / 'models/test/model.yaml',
                  'profile': repo / 'hardware-profiles/test.yaml', 'recipe': path}[change]
        data = yaml.safe_load(target.read_text())
        data['changed'] = True
        target.write_text(yaml.safe_dump(data))
    commit_evidence(repo)
    with pytest.raises(ValueError):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert not (path.parent / 'results').exists()


@pytest.mark.parametrize('second', ['different', None])
def test_disjoint_sweeps_cannot_be_combined(kpis, second):
    first = copy.deepcopy(kpis['tests'][1])
    first['labels'].update(sweep_id='first', concurrency='4')
    other = copy.deepcopy(first)
    other['labels']['concurrency'] = '8'
    if second:
        other['labels']['sweep_id'] = second
    else:
        del other['labels']['sweep_id']
    with pytest.raises(ValueError, match='multiple sweep identities'):
        normalize({'tests': [first, other]}, run_id='fixture', deployment_scope='single-node',
                  accelerator_key='nvidia-h200-x8')


def test_complete_tags_cannot_bypass_bundle_validation(bundle_intake):
    from fetch_forge_evidence import execution_values
    repo, path, revision, client, parent, artifacts, _, _ = bundle_intake
    values = execution_values(client, 'd' * 32, repo, revision, 34)
    parent['data']['tags'] = [{'key': 'playbooks.' + key, 'value': value} for key, value in values.items()]
    metadata_path = SOURCE + '/bundle-metadata.yaml'
    metadata = yaml.safe_load(artifacts[metadata_path])
    metadata['contract_version'] = 999
    artifacts[metadata_path] = yaml.safe_dump(metadata)
    with pytest.raises(ValueError, match='unsupported Forge bundle contract'):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert not (path.parent / 'results').exists()


@pytest.mark.parametrize('kind', ['InferenceService', 'ServingRuntime', 'Service'])
def test_unsupported_resources_rejected(bundle_intake, kind):
    repo, path, _, client, _, artifacts, _, selected = bundle_intake
    document = yaml.safe_load(selected.read_text())
    document['kind'] = kind
    selected.write_text(yaml.safe_dump(document))
    commit_evidence(repo)
    revision = git(repo, 'rev-parse', 'HEAD')
    fjob_path = '01__test/000__ci_metadata/fournos_fjob.yaml'
    fjob = yaml.safe_load(artifacts[fjob_path])
    fjob['spec']['env']['FORGE_FOREIGN_TESTING_PULL_PULL_SHA'] = revision
    artifacts[fjob_path] = yaml.safe_dump(fjob)
    metadata_path = SOURCE + '/bundle-metadata.yaml'
    metadata = yaml.safe_load(artifacts[metadata_path])
    metadata['recipe_revision'] = revision
    metadata['source_files'][0]['sha256'] = hashlib.sha256(selected.read_bytes()).hexdigest()
    artifacts[metadata_path] = yaml.safe_dump(metadata)
    with pytest.raises(ValueError, match='unsupported Forge bundle resource set'):
        import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    assert not (path.parent / 'results').exists()


def test_receipt_rejects_new_platform_artifact(bundle_intake):
    repo, path, revision, client, _, _, _, selected = bundle_intake
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    commit_evidence(repo)
    (selected.parent / 'new.yaml').write_text('{}\n')
    commit_evidence(repo)
    client.request = client.run = client.kpis = lambda *a, **kw: pytest.fail('receipt contacted MLflow')
    with pytest.raises(ValueError, match='deployment inputs differ'):
        admitted_execution(repo, load_admissions(repo), client, 'd' * 32, revision, 34, {})


def test_offline_validation_rederives_expected_levels(bundle_intake):
    repo, path, revision, client, _, _, _, _ = bundle_intake
    import_parent(client, repo, 'd' * 32, revision, 34, require_parent=True)
    reference = yaml.safe_load(path.read_text())['benchmark_runs'][0]
    run_path = path.parent / reference
    run = yaml.safe_load(run_path.read_text())
    assert run['forge']['expected_concurrencies'] == {'guidellm': [1, 5], 'aiperf': [1]}
    run['forge']['expected_concurrencies']['aiperf'] = [999]
    assert 'differ from workload replay' in str(validate_benchmark_run(repo, run_path, run)[0])
