"""v4 contract tests; measurement/compatibility fixtures are deliberately synthetic."""
import copy
import json
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
from catalog import build_catalog, build_identity, canonical, validation_badge
from render import render_recipe

BUILD = {'source_sha': 'a' * 40, 'dirty': False}


@pytest.fixture
def repo(tmp_path):
    for name in ('models', 'schema', 'templates', 'hardware-profiles', 'engine-versions'):
        shutil.copytree(REPO / name, tmp_path / name)
    return tmp_path


def catalog(repo=REPO):
    return build_catalog(repo, BUILD)


def gemma_path(repo):
    return next(repo.glob('models/gemma-4/recipes/*/recipe.yaml'))


def clone_recipe(repo, name):
    original = gemma_path(repo)
    destination = original.parent.parent / name
    shutil.copytree(original.parent, destination)
    recipe = yaml.safe_load(original.read_text())
    recipe['recipe_id'] = name
    path = destination / 'recipe.yaml'
    path.write_text(yaml.safe_dump(recipe))
    return path, recipe


def test_current_entries_follow_repository_inputs():
    result = catalog()
    assert result['schema_version'] == 2
    recipes = [yaml.safe_load(path.read_text()) for path in REPO.glob('models/*/recipes/*/recipe.yaml')]
    assert len(result['entries']) == sum(len(recipe['platforms']) for recipe in recipes)
    assert {model['id'] for model in result['models']} == {recipe['model_id'] for recipe in recipes}
    for entry in result['entries']:
        path = REPO / entry['source']['path']
        recipe = yaml.safe_load(path.read_text())
        expected = [str((path.parent / reference).relative_to(REPO))
                    for reference in recipe.get('benchmark_runs', [])]
        assert [evidence['source']['path'] for evidence in entry['evidence']] == expected
        assert entry['maturity'] == recipe['maturity']
        assert entry['quantization'] == recipe.get('quantization')
        if recipe['maturity'] not in ('validated', 'production'):
            assert entry['validation']['state'] == 'none'
    assert len({entry['id'] for entry in result['entries']}) == len(result['entries'])


def test_deterministic_exact_artifacts_and_allocation():
    assert canonical(catalog()) == canonical(catalog())
    for entry in catalog()['entries']:
        for artifact in entry['artifacts']:
            assert artifact['body'].encode() == (REPO / artifact['source']['path']).read_bytes()
        assert entry['commands'] == []
    gemma = next(entry for entry in catalog()['entries'] if entry['model_id'] == 'gemma-4')
    assert gemma['hardware']['data']['accelerators']['count_per_node'] == 8
    assert gemma['gpu_allocation']['declared_gpus_per_replica'] == 1


@pytest.mark.parametrize('quantization', [None, 'FP8'])
def test_quantization_is_explicit_not_inferred_from_checkpoint(repo, quantization):
    path = gemma_path(repo)
    recipe = yaml.safe_load(path.read_text())
    recipe['serving']['model'] = 'synthetic/checkpoint-FP8'
    if quantization:
        recipe['quantization'] = quantization
    else:
        recipe.pop('quantization', None)
    path.write_text(yaml.safe_dump(recipe))
    render_recipe(repo, path)
    entry = next(entry for entry in catalog(repo)['entries'] if entry['recipe_id'] == recipe['recipe_id'])
    assert entry['quantization'] == quantization


def test_quickstart_commands_and_expected_results_retain_source(repo):
    path = gemma_path(repo)
    recipe = yaml.safe_load(path.read_text())
    notes_path = path.parent / recipe['notes']
    notes = yaml.safe_load(notes_path.read_text())
    notes['quickstart'] = [
        {'step': 'Existing prose', 'detail': 'Review the source manifests.'},
        {'step': 'Inspect', 'commands': 'oc get pods\noc get services', 'expected': 'Inspect the status.'},
    ]
    notes_path.write_text(yaml.safe_dump(notes))
    entry = next(entry for entry in catalog(repo)['entries'] if entry['recipe_id'] == recipe['recipe_id'])
    assert entry['notes']['quickstart'] == notes['quickstart']


@pytest.mark.parametrize('field, value', [('commands', ''), ('expected', ''), ('commands', ['oc get pods']), ('expected', 42)])
def test_invalid_quickstart_fields_fail_validation(repo, field, value):
    path = gemma_path(repo)
    recipe = yaml.safe_load(path.read_text())
    notes_path = path.parent / recipe['notes']
    notes = yaml.safe_load(notes_path.read_text())
    notes['quickstart'] = [{'step': 'Inspect', field: value}]
    notes_path.write_text(yaml.safe_dump(notes))
    with pytest.raises(ValueError):
        catalog(repo)


def test_platform_versions_and_colliding_filter_dimensions(repo):
    path, recipe = clone_recipe(repo, 'synthetic-ambiguous')
    recipe['platforms'].append({**recipe['platforms'][0], 'version': 'v0.25.0'})
    path.write_text(yaml.safe_dump(recipe))
    render_recipe(repo, path)
    entries = [entry for entry in catalog(repo)['entries'] if entry['recipe_id'] == 'synthetic-ambiguous']
    assert len(entries) == 2
    assert len({entry['id'] for entry in entries}) == 2
    assert len({entry['hardware']['accelerator_key'] for entry in entries}) == 1


def test_profile_identity_is_not_erased_by_accelerator_group(repo):
    path, recipe = clone_recipe(repo, 'synthetic-other-profile')
    profile = yaml.safe_load((repo / recipe['hardware_profile']).read_text())
    profile['profile_id'] = 'synthetic-profile'
    profile['profile_revision'] += 1
    (repo / 'hardware-profiles/synthetic-profile.yaml').write_text(yaml.safe_dump(profile))
    recipe['hardware_profile'] = 'hardware-profiles/synthetic-profile.yaml'
    path.write_text(yaml.safe_dump(recipe))
    render_recipe(repo, path)
    entries = [entry for entry in catalog(repo)['entries'] if entry['model_id'] == 'gemma-4']
    assert len({entry['hardware']['accelerator_key'] for entry in entries}) == 1
    assert len({entry['hardware']['path'] for entry in entries}) == 2


def test_blocked_platform_and_optional_notes(repo):
    path, recipe = clone_recipe(repo, 'synthetic-blocked')
    recipe.pop('notes', None)
    recipe['platforms'][0].update(blocked=True, reason='Explicit synthetic blocker')
    path.write_text(yaml.safe_dump(recipe))
    entry = next(entry for entry in catalog(repo)['entries'] if entry['recipe_id'] == recipe['recipe_id'])
    assert entry['blocked'] and entry['reason'] == 'Explicit synthetic blocker'
    assert entry['artifacts'] == [] and entry['notes'] is None


def test_model_without_recipes_and_empty_catalog(tmp_path):
    for directory in ('schema', 'engine-versions'):
        shutil.copytree(REPO / directory, tmp_path / directory)
    assert catalog(tmp_path)['models'] == []
    model_dir = tmp_path / 'models/bare'
    model_dir.mkdir(parents=True)
    (model_dir / 'model.yaml').write_text('schema_version: 1\nmodel_id: bare\nname: Bare\nfamily: X\nquantizations: [{name: FP8}]\n')
    result = catalog(tmp_path)
    assert result['models'][0]['entry_ids'] == [] and result['entries'] == []


@pytest.mark.parametrize('kind', ['duplicate-key', 'duplicate-entry', 'missing-ref', 'malformed'])
def test_required_input_failures_are_visible(repo, kind):
    path = gemma_path(repo)
    recipe = yaml.safe_load(path.read_text())
    if kind == 'duplicate-key':
        path.write_text(path.read_text() + '\nmodel_id: gemma-4\n')
    elif kind == 'duplicate-entry':
        recipe['platforms'].append(recipe['platforms'][0])
        path.write_text(yaml.safe_dump(recipe))
    elif kind == 'missing-ref':
        recipe['hardware_profile'] = 'hardware-profiles/missing.yaml'
        path.write_text(yaml.safe_dump(recipe))
    else:
        path.write_text('invalid: [\n')
    with pytest.raises((ValueError, yaml.YAMLError)):
        catalog(repo)


def synthetic_evidence():
    entry = copy.deepcopy(catalog()['entries'][0])
    entry['maturity'] = 'validated'
    entry['engine'] = {'state': 'resolved', 'version': '0.24.0'}
    entry['compatibility_signature'].update(model_revision='synthetic-model-revision', workload_sha256='b' * 64)
    environment = {'platform': {'stack': 'rhoai', 'version': 'synthetic'}, 'vllm_version': '0.24.0',
                   'image': 'registry.example.org/synthetic@sha256:' + 'c' * 64,
                   'catalog_compatibility': copy.deepcopy(entry['compatibility_signature'])}
    return entry, [{'run': {'run_id': 'synthetic-run', 'environment': environment}}]


def test_badges_preserve_cross_stack_attribution_and_earlier_engine():
    entry, evidence = synthetic_evidence()
    badge = validation_badge(entry, evidence)
    assert badge['state'] == 'same-engine' and badge['tested_platform']['stack'] == 'rhoai'
    assert badge['tested_platform'] != entry['platform']
    entry['engine']['version'] = '0.25.0'
    badge = validation_badge(entry, evidence)
    assert badge['state'] == 'earlier-engine' and badge['tested_engine'] == '0.24.0'


@pytest.mark.parametrize('failure', ['unknown-engine', 'opaque-engine', 'config-mismatch', 'no-model-revision', 'no-digest', 'unvalidated'])
def test_badges_do_not_inherit_without_proof(failure):
    entry, evidence = synthetic_evidence()
    if failure == 'unknown-engine': entry['engine']['state'] = 'unknown'
    elif failure == 'opaque-engine': entry['engine']['version'] = '0.25.0+vendor.1'
    elif failure == 'config-mismatch': evidence[0]['run']['environment']['catalog_compatibility']['parallelism'] = {'tp': 99}
    elif failure == 'no-model-revision': entry['compatibility_signature']['model_revision'] = None
    elif failure == 'no-digest': evidence[0]['run']['environment']['image'] = 'registry.example.org/synthetic:tag'
    else: entry['maturity'] = 'contributed'
    assert validation_badge(entry, evidence)['state'] == 'none'


def test_stdout_is_json_and_does_not_modify_tracked_catalog(tmp_path):
    result = subprocess.run([sys.executable, 'tools/catalog.py', '--stdout'], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    dirty = build_identity(REPO)['dirty']
    assert json.loads(result.stdout)['build']['dirty'] == dirty
    assert all((entry['source']['url'] is None) == dirty for entry in json.loads(result.stdout)['entries'])


def test_old_snapshot_claims_do_not_become_current_evidence(repo):
    original = json.loads((REPO / 'tests/fixtures/pr17-original-catalog.json').read_text())
    assert original['models'][0]['validated'] is True
    for path in repo.glob('models/*/recipes/*/recipe.yaml'):
        recipe = yaml.safe_load(path.read_text())
        recipe.pop('benchmark_runs', None)
        path.write_text(yaml.safe_dump(recipe))
    assert all(entry['validation']['state'] == 'none' and not entry['evidence'] for entry in catalog(repo)['entries'])


def attach_synthetic_run(repo):
    path = gemma_path(repo)
    recipe = yaml.safe_load(path.read_text())
    directory = path.parent / 'results/synthetic-run'
    directory.mkdir(parents=True)
    raw = b'{"zero":0,"synthetic":true}\n'
    (directory / 'raw.json').write_bytes(raw)
    profile = yaml.safe_load((repo / recipe['hardware_profile']).read_text())
    run = {'schema_version': 1, 'run_id': 'synthetic-run', 'recipe_id': recipe['recipe_id'],
           'deployment_scope': recipe['deployment']['scope'], 'hardware_profile': recipe['hardware_profile'],
           'hardware_profile_revision': profile['profile_revision'], 'harness': 'synthetic-fixture', 'result': 'result.json',
           'artifacts': [{'type': 'raw-output', 'path': 'raw.json', 'checksum': 'sha256:' + hashlib.sha256(raw).hexdigest()}]}
    result = {'schema_version': 1, 'run_id': 'synthetic-run', 'deployment_scope': run['deployment_scope'],
              'accelerator_key': profile['accelerator_key'], 'metrics': {
                  'zero': {'value': 0, 'unit': None, 'statistic': 'unknown-statistic', 'alien': 'kept'}, 'missing': None},
              'raw_metrics': {'unfamiliar': 'preserved'}}
    (directory / 'run.yaml').write_text(yaml.safe_dump(run))
    (directory / 'result.json').write_text(json.dumps(result))
    recipe['benchmark_runs'] = ['results/synthetic-run/run.yaml']
    path.write_text(yaml.safe_dump(recipe))
    return directory, run, result


def test_only_referenced_metrics_preserve_zero_unknown_and_raw_content(repo):
    directory, run, result = attach_synthetic_run(repo)
    (directory / 'unreferenced.json').write_text('{"metrics":{"fabricated":9999}}')
    entry = next(entry for entry in catalog(repo)['entries'] if entry['recipe_id'] == run['recipe_id'])
    evidence = entry['evidence'][0]
    assert evidence['result'] == result
    assert evidence['run'] == run
    assert evidence['run_body'] == (directory / 'run.yaml').read_text()
    assert evidence['result_body'] == (directory / 'result.json').read_text()
    assert 'fabricated' not in evidence['result']['metrics']
    assert entry['validation']['state'] == 'none'


@pytest.mark.parametrize('failure', ['run-id', 'recipe-id', 'scope', 'profile', 'checksum'])
def test_mismatched_or_tampered_evidence_fails(repo, failure):
    directory, run, result = attach_synthetic_run(repo)
    if failure == 'run-id': result['run_id'] = 'different-run'
    elif failure == 'recipe-id': run['recipe_id'] = 'different-recipe'
    elif failure == 'scope': result['deployment_scope'] = 'multi-node'
    elif failure == 'profile': result['accelerator_key'] = 'different-accelerators'
    else: (directory / 'raw.json').write_text('tampered')
    (directory / 'run.yaml').write_text(yaml.safe_dump(run))
    (directory / 'result.json').write_text(json.dumps(result))
    with pytest.raises(ValueError): catalog(repo)
