"""Synthetic RC identities only; no GPU or registry access."""
import copy
import json
import subprocess
import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))

from private_images import (binding, public_image, resolve_private, replace_serving_images,
                            sanitize_runtime, assert_no_private_tokens, private_tokens, request_binding)
from render import merge_overrides
from engine_versions import resolve_engine, engine_errors
from contributor_bundle import json_bytes, json_load, runtime_identity, write_archive
from import_contributor import import_bundle, replay_run
from test_contributor import source as public_source, evidence as public_evidence, commit
from validate import load_schema, load_schema_registry, validate_document

PRIVATE = {'image_visibility': 'private', 'image_env': 'RHOAI_36_RC_IMAGE'}
SECRET = 'restricted.example.org/internal/runtime:secret-rc2'


@pytest.fixture
def evidence(tmp_path):
    repo, root, deployment = public_source.__wrapped__(tmp_path)
    recipe = yaml.safe_load((root / 'recipe.yaml').read_text())
    recipe['serving'].pop('image')
    recipe['serving'].update(PRIVATE)
    (root / 'recipe.yaml').write_text(yaml.safe_dump(recipe))
    deployment['spec']['template']['spec']['containers'][0]['image'] = public_image(PRIVATE)
    (root / 'manifests/vllm-v0.24.0/deployment.yaml').write_text(yaml.safe_dump(deployment))
    commit(repo)
    return public_evidence.__wrapped__((repo, root, deployment), tmp_path)


def sanitized(evidence):
    repo, root, request, files, _, _ = evidence
    runtime = json_load(files['runtime.json'])
    for key in ('launch', 'pods_before', 'pods_after'):
        runtime[key] = replace_serving_images(runtime[key], public_image(PRIVATE), SECRET)
    recipe = yaml.safe_load((root / 'recipe.yaml').read_text())
    assert runtime_identity({**runtime, '_recipe': recipe}, request)[1] == 'sha256:' + 'c' * 64
    runtime, tokens = sanitize_runtime(runtime, PRIVATE, SECRET)
    files = dict(files, **{'runtime.json': json_bytes(runtime)})
    assert_no_private_tokens(files, tokens)
    return files


def test_private_binding_does_not_read_environment(monkeypatch):
    monkeypatch.setenv('RHOAI_36_RC_IMAGE', SECRET)
    merged = merge_overrides({'image': 'public.example.org/runtime:old', 'engine': {'old': True}}, PRIVATE)
    assert merged['image'] == public_image(PRIVATE)
    assert SECRET not in repr(merged) and 'engine' not in merged
    switched = merge_overrides(PRIVATE, {'image': 'public.example.org/runtime:ga'})
    assert 'image_env' not in switched and 'image_visibility' not in switched


@pytest.mark.parametrize('value', [{**PRIVATE, 'image': SECRET}, {'image_env': 'RC_IMAGE'},
                                  {**PRIVATE, 'image_env': 'BAD;NAME'}, {'image_visibility': 'private'}])
def test_ambiguous_binding_rejected(value):
    with pytest.raises(ValueError):
        binding(value)


def test_missing_private_value_never_echoes_input():
    for value in ('', 'private', 'bad value SECRET', 'registry.example.org/runtime:$(SECRET)',
                  'user:SECRET@registry.example.org/runtime:rc'):
        with pytest.raises(ValueError, match='missing or invalid') as error:
            resolve_private(PRIVATE, {'RHOAI_36_RC_IMAGE': value})
        assert 'SECRET' not in str(error.value)
    assert resolve_private(PRIVATE, {'RHOAI_36_RC_IMAGE': SECRET}) == SECRET


def test_private_engine_is_bound_to_variable_not_ga_index():
    serving = {**PRIVATE, 'image_usage': {'kind': 'custom', 'note': 'Pre-release runtime'},
               'engine': {'name': 'vllm', 'version': '0.28.0', 'image_env': 'RHOAI_36_RC_IMAGE',
                          'source': 'https://example.org/public-build-notes'}}
    index = {'variants': ['cuda'], 'releases': [{'version': '3.6', 'vllm_version': '0.31.0'}]}
    assert resolve_engine(serving, {}, {'stack': 'rhoai', 'version': '3.6'}, index)['version'] == '0.28.0'
    assert not engine_errors(serving, {}, {'stack': 'rhoai', 'version': '3.6'}, index, True)
    assert engine_errors(serving, {**PRIVATE, 'image_env': 'OTHER_RC'}, {}, index, True)


def test_private_archive_import_and_offline_replay(evidence, tmp_path):
    repo, root, request, _, _, _ = evidence
    files = sanitized(evidence)
    bundle = tmp_path / 'private-evidence.tar.gz'
    digest = write_archive(bundle, request, 'succeeded', files)
    import_bundle(repo, bundle, digest, apply=True, reviewed=True)
    path = root / 'results' / json_load(files['result.json'])['run_id'] / 'run.yaml'
    run = yaml.safe_load(path.read_text())
    assert run['environment']['image_visibility'] == 'private'
    assert 'image_digest' not in run['environment']
    assert_no_private_tokens({p.name: p.read_bytes() for p in path.parent.iterdir()}, private_tokens(SECRET, ['sha256:' + 'c' * 64]))
    replay_run(repo, path, run)
    # A later public recipe image does not rewrite the historical run or its policy.
    recipe = yaml.safe_load((root / 'recipe.yaml').read_text())
    recipe['serving'].pop('image_env')
    recipe['serving'].pop('image_visibility')
    recipe['serving']['image'] = 'public.example.org/runtime:ga'
    (root / 'recipe.yaml').write_text(yaml.safe_dump(recipe))
    replay_run(repo, path, run)


def test_private_launch_and_runtime_must_still_match(evidence, tmp_path):
    repo, _, request, _, _, _ = evidence
    files = sanitized(evidence)
    runtime = json_load(files['runtime.json'])
    runtime['pods_after']['items'][0]['spec']['containers'][0]['args'].append('--changed')
    files['runtime.json'] = json_bytes(runtime)
    bundle = tmp_path / 'bad.tar.gz'
    digest = write_archive(bundle, request, 'succeeded', files)
    with pytest.raises(ValueError, match='configuration differs'):
        import_bundle(repo, bundle, digest, apply=False, reviewed=False)


def test_missing_or_public_policy_cannot_accept_withheld_digest(evidence, tmp_path):
    repo, _, request, _, _, _ = evidence
    files = sanitized(evidence)
    runtime = json_load(files['runtime.json'])
    runtime.pop('image_visibility')
    files['runtime.json'] = json_bytes(runtime)
    bundle = tmp_path / 'bad.tar.gz'
    digest = write_archive(bundle, request, 'succeeded', files)
    with pytest.raises(ValueError, match='visibility differs'):
        import_bundle(repo, bundle, digest, apply=False, reviewed=False)


@pytest.mark.parametrize('leak', [SECRET, 'restricted.example.org', 'secret-rc2', 'sha256:' + 'c' * 64])
def test_leak_guard_covers_free_text_and_nested_artifacts(leak):
    with pytest.raises(ValueError, match='private image identifier'):
        assert_no_private_tokens({'kpis.json': json_bytes({'nested': [{'log': leak}]})},
                                 private_tokens(SECRET, ['sha256:' + 'c' * 64]))


def test_launcher_forwards_only_variable_name(evidence, tmp_path):
    launcher = (tmp_path / 'package/run.sh').read_text()
    assert '-e RHOAI_36_RC_IMAGE' in launcher
    assert SECRET not in launcher
    subprocess.run(['sh', '-n', str(tmp_path / 'package/run.sh')], check=True)


def test_schema_enforces_private_public_exclusion(evidence):
    repo, root, _, _, _, _ = evidence
    recipe = yaml.safe_load((root / 'recipe.yaml').read_text())
    schema, registry = load_schema(repo, 'recipe'), load_schema_registry(repo)
    assert not validate_document(root / 'recipe.yaml', recipe, schema, registry)
    recipe['serving']['image'] = SECRET
    assert validate_document(root / 'recipe.yaml', recipe, schema, registry)
    for overrides in ({**PRIVATE, 'image': SECRET}, {'image_env': 'RC'},
                      {'image_visibility': 'public', 'image_env': 'RC', 'image': 'public.example.org/runtime:ga'}):
        assert validate_document(root / 'platforms/vllm.yaml', overrides,
                                 load_schema(repo, 'platform-overrides'), registry)


def test_badge_and_tracker_preserve_private_attribution_without_digest():
    from catalog import validation_badge
    from model_coverage import _validation_applies
    signature = {'model_revision': 'test-revision', 'workload_sha256': 'test-workload',
                 'final_runtime': [{'args': ['unchanged']}]}
    platform = {'stack': 'rhoai', 'version': '3.6'}
    entry = {'blocked': False, 'maturity': 'validated', 'engine': {'state': 'resolved', 'version': '0.28.0'},
             'compatibility_signature': signature}
    evidence = [{'run': {'run_id': 'private-run', 'environment': {
        'image_visibility': 'private', 'platform': platform, 'vllm_version': '0.28.0',
        'catalog_compatibility': copy.deepcopy(signature)}}}]
    entry['evidence'] = evidence
    badge = validation_badge(entry, evidence)
    assert badge['state'] == 'same-engine' and badge['tested_image'] is None
    assert 'pre-release' in badge['label']
    assert _validation_applies(entry, platform)[1]
    evidence[0]['run']['environment']['catalog_compatibility']['final_runtime'] = []
    assert not _validation_applies(entry, platform)[1]


def test_private_binding_replaces_all_distributed_roles():
    placeholder = public_image(PRIVATE)
    document = {'spec': {'leaderWorkerTemplate': {
        key: {'spec': {'containers': [{'name': name, 'image': placeholder}]}}
        for key, name in [('leaderTemplate', 'vllm-leader'), ('workerTemplate', 'vllm-worker')]}}}
    replaced = replace_serving_images(document, placeholder, SECRET)
    assert repr(replaced).count(SECRET) == 2
    assert SECRET not in repr(document)
    document['spec']['leaderWorkerTemplate']['workerTemplate']['spec']['containers'][0]['image'] = 'other'
    with pytest.raises(ValueError, match='differs from declared'):
        replace_serving_images(document, placeholder, SECRET)


def test_private_recipe_renders_placeholder_even_with_secret_environment(evidence, monkeypatch):
    from render import render_recipe
    repo, root, _, _, _, _ = evidence
    shutil.copytree(REPO / 'templates', repo / 'templates')
    monkeypatch.setenv('RHOAI_36_RC_IMAGE', SECRET)
    rendered, errors = render_recipe(repo, root / 'recipe.yaml', dry_run=True)
    assert not errors
    assert public_image(PRIVATE) in rendered and SECRET not in rendered
