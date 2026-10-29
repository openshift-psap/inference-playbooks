"""Synthetic tooling proof only; none of these metrics are model evidence."""
import copy
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))

from contributor_bundle import (FORGE_REVISION, LEGACY_FORGE_REVISION, MAX_FILE, checksum, json_bytes,
                                json_load, read_archive, relative, replay, shareable, validate_request, write_archive)
from contributor_security import UIDAdmissionRejected, admit_manifest, check_runtime_uids, effective_uids
from import_contributor import import_bundle, upload_archive
from prepare_contributor import prepare
from run_contributor import execute, snapshot_transport, standalone_overrides
from validate import validate_benchmark_run


def commit(repo):
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                    'commit', '-m', 'synthetic fixture'], check=True, capture_output=True)


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    shutil.copytree(REPO / 'schema', repo / 'schema')
    root = repo / 'models/test/recipes/test-recipe'
    root.mkdir(parents=True)
    recipe = {'schema_version': 4, 'recipe_id': 'test-recipe', 'model_id': 'test',
              'platforms': [{'stack': 'vllm', 'version': 'v0.24.0', 'overrides': 'platforms/vllm.yaml'}],
              'hardware_profile': 'hardware-profiles/test.yaml', 'workload_profile': 'guidellm-8k1k',
              'deployment_mode': 'tp1', 'optimization_intent': 'latency', 'maturity': 'contributed',
              'deployment': {'scope': 'single-node'},
              'serving': {'model': 'example/model', 'image': 'docker.io/vllm/vllm-openai:v0.24.0',
                          'parallelism': {'mode': 'tp', 'tp': 1}, 'args': []}}
    (root / 'recipe.yaml').write_text('# retain authored comment\n' + yaml.safe_dump(recipe, sort_keys=False))
    (repo / 'models/test/model.yaml').write_text('model_id: test\nhuggingface_id: example/model\n')
    (root / 'platforms').mkdir()
    (root / 'platforms/vllm.yaml').write_text('{}\n')
    profile = repo / 'hardware-profiles/test.yaml'
    profile.parent.mkdir()
    profile.write_text('profile_revision: 1\naccelerator_key: nvidia-h200-x1\n')
    server = {'name': 'vllm', 'image': recipe['serving']['image'],
              'command': ['vllm', 'serve'], 'args': ['example/model'],
              'resources': {'limits': {'nvidia.com/gpu': '1'}}}
    deployment = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {'name': 'test'},
                  'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': 'test'}},
                           'template': {'metadata': {'labels': {'app': 'test'}}, 'spec': {
                               'securityContext': {'runAsUser': 65534, 'runAsNonRoot': True}, 'containers': [server]}}}}
    manifest = root / 'manifests/vllm-v0.24.0/deployment.yaml'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(yaml.safe_dump(deployment))
    subprocess.run(['git', '-C', str(repo), 'init'], check=True, capture_output=True)
    commit(repo)
    return repo, root, deployment


@pytest.fixture
def evidence(source, tmp_path):
    repo, root, deployment = source
    request = prepare(repo, str((root / 'recipe.yaml').relative_to(repo)), 'vllm', 'v0.24.0',
                      'guidellm-8k1k', 'quay.io/example/runner@sha256:' + 'a' * 64,
                      'ghcr.io/vllm-project/guidellm@sha256:' + 'b' * 64, tmp_path / 'package')
    files = {'inputs/' + name: (repo / name).read_bytes() for name in request['inputs']}
    labels = {'benchmark_tool': 'guidellm', 'benchmark_key': 'IP-8k/1k',
              'model_name': 'example/model', 'served_model': 'example/model'}
    kpis = {'tests': [{'labels': labels, 'kpis': [
        {'kpi_id': 'playbooks_output_tok_per_sec', 'is_curve': True,
         'values': [[c, c * 10] for c in [1, 4, 16, 32, 64, 128]]}]}]}
    files['kpis.json'] = json_bytes(kpis)
    launch = copy.deepcopy(deployment)
    decisions = admit_manifest(launch, 'benchmark', copy.deepcopy)
    pods = {'kind': 'List', 'items': [{'kind': 'Pod', 'spec': copy.deepcopy(launch['spec']['template']['spec']),
            'status': {'containerStatuses': [{'name': 'vllm', 'ready': True, 'imageID': 'sha256:' + 'c' * 64}]}}]}
    files['runtime.json'] = json_bytes({'launch': launch, 'pods_before': pods, 'pods_after': pods,
                                      'harness_invocation': {'image': request['harness_image']},
                                      'uid_admission': decisions})
    files['result.json'] = json_bytes(replay(request, files))
    files['raw/harness-0.json'] = json_bytes({'synthetic_fixture': True, 'metrics': {'requests': 100}})
    runtime = json_load(files['runtime.json'])
    runtime['native_exports'] = [{'path': 'raw/harness-0.json',
                                 'original_checksum': checksum(files['raw/harness-0.json']),
                                 'export_checksum': checksum(files['raw/harness-0.json']), 'redacted_fields': []}]
    files['runtime.json'] = json_bytes(runtime)
    bundle = tmp_path / 'evidence.tar.gz'
    digest = write_archive(bundle, request, 'succeeded', files)
    return repo, root, request, files, bundle, digest


def test_prepare_is_pinned_scoped_and_portable(evidence, tmp_path):
    repo, root, request, files, bundle, digest = evidence
    assert request['forge_revision'] == '93b85db135425516eb000de62d2483c62cdacacc'
    launcher = (tmp_path / 'package/run.sh').read_text()
    assert '--user "$(id -u):$(id -g)"' in launcher
    assert request['runner_image'] in launcher
    assert '--userns=keep-id' in launcher
    subprocess.run(['sh', '-n', str(tmp_path / 'package/run.sh')], check=True)
    subprocess.run(['sh', '-n', str(tmp_path / 'package/monitor.sh')], check=True)
    assert (tmp_path / 'package/monitor.py').read_bytes() == (REPO / 'tools/monitor_contributor.py').read_bytes()
    status = subprocess.run([str(tmp_path / 'package/monitor.sh'), str(tmp_path), '--watch'],
                            check=True, capture_output=True, text=True)
    assert 'Outcome: succeeded' in status.stdout
    assert 'AWS' not in launcher
    assert not (tmp_path / 'package/playbooks/.git').exists()
    assert read_archive(bundle, digest)[0]['request'] == request


def test_archive_is_reproducible(evidence, tmp_path):
    _, _, request, files, bundle, digest = evidence
    other = tmp_path / 'other.tar.gz'
    assert write_archive(other, request, 'succeeded', files) == digest
    assert other.read_bytes() == bundle.read_bytes()


def test_import_dry_run_apply_replay_idempotence(evidence):
    repo, root, request, files, bundle, digest = evidence
    before = (root / 'recipe.yaml').read_bytes()
    report = import_bundle(repo, bundle, digest)
    assert report['completeness'] == 'full-sweep'
    assert not (root / 'results').exists()
    assert (root / 'recipe.yaml').read_bytes() == before
    with pytest.raises(ValueError, match='--reviewed'):
        import_bundle(repo, bundle, digest, apply=True)
    report = import_bundle(repo, bundle, digest, apply=True, reviewed=True)
    assert report['applied'] and not report['maturity_changed']
    recipe = yaml.safe_load((root / 'recipe.yaml').read_text())
    assert recipe['maturity'] == 'contributed'
    assert (root / 'recipe.yaml').read_text().startswith('# retain authored comment')
    path = root / recipe['benchmark_runs'][0]
    run = yaml.safe_load(path.read_text())
    assert validate_benchmark_run(repo, path, run)[0] == []
    snapshot = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    import_bundle(repo, bundle, digest, apply=True, reviewed=True)
    assert snapshot == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    normalized = path.parent / 'result.json'
    result = json_load(normalized.read_bytes())
    result['metrics']['output_tok_s'] = 99999
    normalized.write_bytes(json_bytes(result))
    assert 'raw KPI replay' in str(validate_benchmark_run(repo, path, run)[0])


@pytest.mark.parametrize('change', ['model', 'manifest', 'profile', 'override', 'recipe'])
def test_changed_current_inputs_reject_before_writes(evidence, change):
    repo, root, _, _, bundle, digest = evidence
    targets = {'model': repo / 'models/test/model.yaml', 'manifest': root / 'manifests/vllm-v0.24.0/deployment.yaml',
               'profile': repo / 'hardware-profiles/test.yaml', 'override': root / 'platforms/vllm.yaml',
               'recipe': root / 'recipe.yaml'}
    target = targets[change]
    data = yaml.safe_load(target.read_text())
    data['unexpected_change'] = True
    target.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        import_bundle(repo, bundle, digest, apply=True, reviewed=True)
    assert not (root / 'results').exists()


@pytest.mark.parametrize('change', ['image', 'not-ready', 'after-image', 'argv', 'launch', 'labels', 'result', 'platform', 'input', 'private'])
def test_tampered_evidence_rejects_before_writes(evidence, tmp_path, change):
    repo, root, request, files, _, _ = evidence
    runtime = json_load(files['runtime.json'])
    if change == 'image':
        runtime['pods_before']['items'][0]['spec']['containers'][0]['image'] = 'docker.io/example/wrong:v1'
    elif change == 'not-ready':
        runtime['pods_before']['items'][0]['status']['containerStatuses'][0]['ready'] = False
    elif change == 'after-image':
        runtime['pods_after']['items'][0]['status']['containerStatuses'][0]['imageID'] = 'sha256:' + 'd' * 64
    elif change == 'argv':
        runtime['pods_before']['items'][0]['spec']['containers'][0]['args'].append('--other')
    elif change == 'launch':
        runtime['launch']['spec']['replicas'] = 99
    elif change == 'labels':
        document = json_load(files['kpis.json'])
        document['tests'][0]['labels']['served_model'] = 'example/wrong'
        files['kpis.json'] = json_bytes(document)
    elif change == 'result':
        document = json_load(files['result.json'])
        document['metrics']['output_tok_s'] = 123
        files['result.json'] = json_bytes(document)
    elif change == 'platform':
        request['platform']['version'] = 'wrong'
    elif change == 'input':
        files['inputs/' + request['recipe_path']] = b'{}'
    elif change == 'private':
        files['raw/harness-0.json'] = b'{"authorization":"Bearer secret"}'
    files['runtime.json'] = json_bytes(runtime)
    bundle = tmp_path / 'tampered.tar.gz'
    digest = write_archive(bundle, request, 'succeeded', files)
    with pytest.raises((ValueError, KeyError)):
        import_bundle(repo, bundle, digest, apply=True, reviewed=True)
    assert not (root / 'results').exists()


@pytest.mark.parametrize('outcome', ['failed', 'interrupted', 'collection-failed'])
def test_diagnostic_outcomes_not_importable(evidence, tmp_path, outcome):
    repo, root, request, files, _, _ = evidence
    path = tmp_path / 'diagnostic.tar.gz'
    digest = write_archive(path, request, outcome, files)
    with pytest.raises(ValueError, match='diagnostic-only'):
        import_bundle(repo, path, digest, apply=True, reviewed=True)
    assert not (root / 'results').exists()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a/../b', 'a//b', './a', 'a\\b', 'a\nb'])
def test_noncanonical_paths(name):
    with pytest.raises(ValueError, match='bundle path'):
        relative(name)


@pytest.mark.parametrize('mode', ['symlink', 'hardlink', 'device', 'executable', 'duplicate', 'oversize'])
def test_unsafe_archive_members(evidence, tmp_path, mode):
    _, _, _, _, _, _ = evidence
    path = tmp_path / 'unsafe.tar.gz'
    with tarfile.open(path, 'w:gz') as archive:
        info = tarfile.TarInfo('evil.json')
        info.mode = 0o600
        if mode == 'symlink':
            info.type, info.linkname = tarfile.SYMTYPE, '/outside'
        elif mode == 'hardlink':
            info.type, info.linkname = tarfile.LNKTYPE, '/outside'
        elif mode == 'device':
            info.type = tarfile.CHRTYPE
        elif mode == 'executable':
            info.mode = 0o755
        elif mode == 'oversize':
            info.size = MAX_FILE + 1
        if mode != 'oversize':
            archive.addfile(info, io.BytesIO(b''))
            if mode == 'duplicate':
                archive.addfile(info, io.BytesIO(b''))
        else:
            # tar header alone is sufficient: rejection precedes reading payload.
            archive.fileobj.write(info.tobuf())
    with pytest.raises(ValueError, match='archive member'):
        read_archive(path, checksum(path.read_bytes()))


def test_checksum_and_inventory(evidence, tmp_path):
    _, _, request, files, bundle, digest = evidence
    with pytest.raises(ValueError, match='checksum mismatch'):
        read_archive(bundle, 'sha256:' + '0' * 64)
    with pytest.raises(ValueError, match='duplicate JSON'):
        json_load(b'{"a":1,"a":2}')
    shareable({'env': [{'name': 'HF_TOKEN', 'valueFrom': {'secretKeyRef': {'name': 'local-secret', 'key': 'token'}}}]})
    with pytest.raises(ValueError, match='literal credential'):
        shareable({'env': [{'name': 'HF_TOKEN', 'value': 'never-export-this'}]})


def test_standalone_disables_every_internal_integration(evidence):
    _, _, request, _, _, _ = evidence
    overrides = standalone_overrides(request, 'benchmark')
    for key, value in overrides.items():
        if key.endswith('.enabled'):
            assert value is False
    assert overrides['ci_job.cluster'] == 'portable-contributor'
    assert overrides['inference_playbooks.recipe'] == 'test/recipes/test-recipe'
    assert overrides['inference_playbooks.manifest_path'] == 'manifests/vllm-v0.24.0/deployment.yaml'


def test_pin_is_consistent_with_image_and_guide():
    containerfile = (REPO / 'benchmarks/runner/Containerfile').read_text()
    assert f'forge-revision="{FORGE_REVISION}"' in containerfile
    assert f"revision = '{FORGE_REVISION}'" in containerfile
    assert FORGE_REVISION in (REPO / 'docs/contributor-benchmarks.md').read_text()


def test_unknown_forge_pin_is_rejected(evidence):
    _, _, request, files, _, _ = evidence
    request['forge_revision'] = '0' * 40
    with pytest.raises(ValueError, match='unsupported Forge revision'):
        validate_request(request, files)


def test_current_runner_refuses_legacy_request_before_preflight(evidence, tmp_path):
    _, _, request, _, _, _ = evidence
    request['forge_revision'] = LEGACY_FORGE_REVISION
    forge = tmp_path / 'forge'
    forge.mkdir()
    (forge / 'REVISION').write_text(FORGE_REVISION)
    output = tmp_path / 'outputs'
    with pytest.raises(ValueError, match='different Forge revision'):
        execute(request, tmp_path / 'package/playbooks', output, 'benchmark', 'fixture', forge, 60)
    assert not output.exists()


def test_snapshot_transport_is_bounded_and_checksummed(evidence, tmp_path):
    _, _, request, files, _, _ = evidence
    inputs, forge = tmp_path / 'inputs', tmp_path / 'forge'
    inputs.mkdir()
    forge.mkdir()
    (forge / 'REVISION').write_text(FORGE_REVISION)
    bundle = SimpleNamespace()
    snapshot_transport(bundle, request, inputs, files, forge)
    assert bundle._revision(inputs) == request['tested_revision']
    assert bundle._revision(forge) == FORGE_REVISION
    assert bundle._git_bytes(inputs, request['tested_revision'], request['recipe_path']) == files['inputs/' + request['recipe_path']]
    assert bundle._git_artifact_paths(inputs, request['tested_revision'], inputs / Path(request['manifest_path']).parent) == [inputs / request['manifest_path']]
    with pytest.raises(ValueError, match='outside'):
        bundle._revision(tmp_path)
    for root, revision, path in [(tmp_path, request['tested_revision'], request['recipe_path']),
                                  (inputs, '0' * 40, request['recipe_path']),
                                  (inputs, request['tested_revision'], 'models/unrequested.yaml')]:
        with pytest.raises(ValueError, match='outside'):
            bundle._git_bytes(root, revision, path)
    with pytest.raises(ValueError, match='outside selected platform'):
        bundle._git_artifact_paths(inputs, request['tested_revision'], inputs)
    files['inputs/' + request['recipe_path']] += b'\n# changed\n'
    with pytest.raises(ValueError, match='checksum'):
        bundle._git_bytes(inputs, request['tested_revision'], request['recipe_path'])
    (forge / 'REVISION').write_text(LEGACY_FORGE_REVISION)
    with pytest.raises(ValueError, match='outside'):
        bundle._revision(forge)


@pytest.mark.parametrize('change', ['service', 'extra-file', 'llmisvc-hostpath'])
def test_prepare_rejects_incompatible_forge_bundles(source, tmp_path, change):
    repo, root, manifest = source
    path = root / 'manifests/vllm-v0.24.0/deployment.yaml'
    if change == 'service':
        path.write_text(path.read_text() + '---\napiVersion: v1\nkind: Service\nmetadata:\n  name: test\n')
    elif change == 'extra-file':
        (path.parent / 'extra.yml').write_text('kind: Service\n')
    else:
        manifest = {'apiVersion': 'serving.kserve.io/v1alpha2', 'kind': 'LLMInferenceService',
                    'metadata': {'name': 'test'}, 'spec': {'model': {'uri': 'pvc://models/model'},
                    'template': {'volumes': [{'name': 'model', 'hostPath': {'path': '/models'}}]}}}
        path.write_text(yaml.safe_dump(manifest))
    commit(repo)
    with pytest.raises(ValueError, match='serving resource|self-contained|hostPath'):
        prepare(repo, str((root / 'recipe.yaml').relative_to(repo)), 'vllm', 'v0.24.0',
                'guidellm-8k1k', 'quay.io/example/runner@sha256:' + 'a' * 64,
                'ghcr.io/vllm-project/guidellm@sha256:' + 'b' * 64, tmp_path / 'incompatible')
    assert not (tmp_path / 'incompatible').exists()


@pytest.mark.parametrize('change', ['name', 'uid', 'env', 'label'])
def test_new_pin_rejects_old_launch_rewrites(evidence, tmp_path, change):
    repo, _, request, files, _, _ = evidence
    runtime = json_load(files['runtime.json'])
    launch = runtime['launch']
    if change == 'name':
        launch['metadata']['name'] = 'test-run'
    elif change == 'uid':
        del launch['spec']['template']['spec']['securityContext']['runAsUser']
    elif change == 'env':
        launch['spec']['template']['spec']['containers'][0]['env'] = [{'name': 'HOME', 'value': '/tmp'}]
    else:
        launch['spec']['template']['metadata']['labels']['forge.openshift.io/run'] = 'test'
    files['runtime.json'] = json_bytes(runtime)
    path = tmp_path / 'rewrite.tar.gz'
    digest = write_archive(path, request, 'succeeded', files)
    with pytest.raises(ValueError, match='namespace-only'):
        import_bundle(repo, path, digest)


def test_legacy_pin_bundle_remains_importable_and_replayable(evidence, tmp_path):
    repo, _, request, files, _, _ = evidence
    request['forge_revision'] = LEGACY_FORGE_REVISION
    runtime = json_load(files['runtime.json'])
    runtime['launch']['metadata']['name'] = 'legacy-test-run'
    def probe(pod):
        if 'runAsUser' in pod['spec']['securityContext']:
            raise UIDAdmissionRejected('runAsUser rejected')
        pod['spec']['securityContext']['runAsUser'] = 1000123000
        return pod
    runtime['uid_admission'] = admit_manifest(runtime['launch'], 'benchmark', probe)
    for key in ('pods_before', 'pods_after'):
        runtime[key]['items'][0]['spec']['securityContext']['runAsUser'] = 1000123000
    files['runtime.json'] = json_bytes(runtime)
    path = tmp_path / 'legacy.tar.gz'
    digest = write_archive(path, request, 'succeeded', files)
    assert import_bundle(repo, path, digest, apply=True, reviewed=True)['applied']
    run_path = next(repo.glob('models/*/recipes/*/results/*/run.yaml'))
    assert validate_benchmark_run(repo, run_path, yaml.safe_load(run_path.read_text()))[0] == []


def test_maintainer_upload_is_conditional_checksums_and_retry(tmp_path, monkeypatch):
    path = tmp_path / 'bundle.tar.gz'
    path.write_bytes(b'example')
    digest = checksum(path.read_bytes())
    import base64
    encoded = base64.b64encode(bytes.fromhex(digest[7:])).decode()
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json_bytes({'ChecksumSHA256': encoded}))
    monkeypatch.setattr(subprocess, 'run', run)
    uri = upload_archive(path, 's3://evidence/accepted', digest)
    assert uri == f's3://evidence/accepted/{digest[7:]}.tar.gz'
    assert calls[0][calls[0].index('--if-none-match') + 1] == '*'
    def existing(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1 if 'put-object' in argv else 0,
                               stdout=json_bytes({'ChecksumSHA256': encoded}))
    monkeypatch.setattr(subprocess, 'run', existing)
    assert upload_archive(path, 's3://evidence/accepted', digest) == uri


def test_upload_failure_never_creates_references(evidence, monkeypatch):
    repo, root, _, _, bundle, digest = evidence
    before = (root / 'recipe.yaml').read_bytes()
    import import_contributor
    monkeypatch.setattr(import_contributor, 'upload_archive', lambda *args: (_ for _ in ()).throw(ValueError('storage failure')))
    with pytest.raises(ValueError, match='storage failure'):
        import_bundle(repo, bundle, digest, apply=True, reviewed=True, s3_prefix='s3://evidence/accepted')
    assert (root / 'recipe.yaml').read_bytes() == before
    assert not (root / 'results').exists()


def test_source_side_redaction_preserves_metrics_and_records_paths():
    from run_contributor import export_native
    paths = []
    value = {'endpoint': 'http://private.benchmark.svc.cluster.local:8000',
             'hostname': 'private-host', 'metrics': {'output_tokens': 1234, 'ttft_ms': 1.25}}
    exported = export_native(value, [value['endpoint']], paths)
    assert exported == {'endpoint': '${ENDPOINT}', 'metrics': value['metrics']}
    assert sorted(paths) == ['/endpoint', '/hostname']
    shareable(exported)


@pytest.mark.parametrize('api_key', [None, '', 'synthetic-secret-never-share'])
def test_native_guidellm_api_key_is_removed_not_allowlisted(api_key):
    from run_contributor import export_native
    value = {'config': {'backend': {'api_key': api_key}},
             'metrics': {'output_tokens': 1234, 'ttft_ms': 1.25}}
    before = copy.deepcopy(value)
    paths = []
    exported = export_native(value, [], paths)
    assert exported == {'config': {'backend': {}}, 'metrics': value['metrics']}
    assert paths == ['/config/backend/api_key']
    assert value == before
    shareable(exported)
    with pytest.raises(ValueError, match='sensitive evidence field'):
        shareable(value)


def test_input_closure_cannot_drop_an_override(evidence, tmp_path):
    repo, root, request, files, _, _ = evidence
    key = next(name for name in request['inputs'] if '/platforms/' in name)
    del request['inputs'][key]
    del files['inputs/' + key]
    bundle = tmp_path / 'omitted.tar.gz'
    digest = write_archive(bundle, request, 'succeeded', files)
    with pytest.raises(ValueError, match='input closure'):
        import_bundle(repo, bundle, digest, apply=True, reviewed=True)
    assert not (root / 'results').exists()


def test_decompression_is_bounded_before_tar_headers(tmp_path, monkeypatch):
    import gzip
    import contributor_bundle
    path = tmp_path / 'bomb.tar.gz'
    path.write_bytes(gzip.compress(b'x' * 100000))
    monkeypatch.setattr(contributor_bundle, 'MAX_TOTAL', 1024)
    with pytest.raises(ValueError, match='expanded bundle exceeds'):
        read_archive(path, checksum(path.read_bytes()))


def test_cyclic_yaml_privacy_walk_is_bounded():
    value = {}
    value['self'] = value
    with pytest.raises(ValueError, match='complexity limit'):
        shareable(value)
    shareable('vllm serve example/model --host 0.0.0.0 --port 8000')
    with pytest.raises(ValueError, match='IP'):
        shareable('vllm serve example/model --host 10.20.30.40')


def test_partial_import_can_be_resumed_without_rewriting_evidence(evidence):
    repo, root, _, _, bundle, digest = evidence
    report = import_bundle(repo, bundle, digest, apply=True, reviewed=True)
    directory = Path(report['destination'])
    (directory / 'result.json').unlink()
    import_bundle(repo, bundle, digest, apply=True, reviewed=True)
    assert (directory / 'result.json').is_file()


def test_historical_replay_does_not_block_later_assessment_or_relabel_evidence(evidence):
    repo, root, _, _, bundle, digest = evidence
    report = import_bundle(repo, bundle, digest, apply=True, reviewed=True)
    path = Path(report['destination']) / 'run.yaml'
    run = yaml.safe_load(path.read_text())
    recipe = yaml.safe_load((root / 'recipe.yaml').read_text())
    recipe['maturity'] = 'validated'
    (root / 'recipe.yaml').write_text(yaml.safe_dump(recipe))
    assert validate_benchmark_run(repo, path, run)[0] == []
    manifest = root / 'manifests/vllm-v0.24.0/deployment.yaml'
    manifest.write_text(manifest.read_text().replace('v0.24.0', 'v0.25.0'))
    assert validate_benchmark_run(repo, path, run)[0] == []
    assert run['environment']['image_digest'] == 'sha256:' + 'c' * 64
    with pytest.raises(ValueError):
        import_bundle(repo, bundle, digest)


def test_admission_preserves_authored_uid_and_does_not_persist_probe(source):
    _, _, manifest = source
    before = copy.deepcopy(manifest)
    calls = []
    def probe(pod):
        calls.append(copy.deepcopy(pod))
        assert pod['kind'] == 'Pod'
        assert pod['metadata']['namespace'] == 'benchmark'
        return copy.deepcopy(pod)
    decisions = admit_manifest(manifest, 'benchmark', probe)
    assert manifest == before
    assert len(calls) == 1
    assert decisions[0]['mode'] == 'preserve'
    assert decisions[0]['admitted_uids'] == {'vllm': 65534}


def test_admission_omits_uid_only_when_api_demonstrates_nonroot_assignment(source):
    _, _, manifest = source
    calls = []
    def probe(pod):
        calls.append(copy.deepcopy(pod))
        if 'runAsUser' in pod['spec']['securityContext']:
            raise UIDAdmissionRejected('runAsUser outside namespace range')
        admitted = copy.deepcopy(pod)
        admitted['spec']['securityContext']['runAsUser'] = 1000720000
        return admitted
    decisions = admit_manifest(manifest, 'benchmark', probe)
    assert len(calls) == 2
    assert 'runAsUser' not in manifest['spec']['template']['spec']['securityContext']
    assert manifest['spec']['template']['spec']['securityContext']['runAsNonRoot'] is True
    assert decisions[0]['mode'] == 'platform-assigned'
    assert decisions[0]['admitted_uids'] == {'vllm': 1000720000}
    actual = {'items': [{'spec': {**manifest['spec']['template']['spec'],
                                 'securityContext': {'runAsUser': 1000720000, 'runAsNonRoot': True}}}]}
    check_runtime_uids(actual, decisions)


@pytest.mark.parametrize('injected_uid', [None, 0, -1, True, '65534'])
def test_admission_refuses_unknown_or_root_fallback(source, injected_uid):
    _, _, manifest = source
    before = copy.deepcopy(manifest)
    def probe(pod):
        if 'runAsUser' in pod['spec']['securityContext']:
            raise UIDAdmissionRejected('runAsUser rejected')
        admitted = copy.deepcopy(pod)
        if injected_uid is not None:
            admitted['spec']['securityContext']['runAsUser'] = injected_uid
        return admitted
    with pytest.raises(ValueError, match='concrete non-root UID'):
        admit_manifest(manifest, 'benchmark', probe)
    assert manifest == before


def test_admission_does_not_fallback_on_auth_or_network_error(source):
    _, _, manifest = source
    calls = []
    def probe(pod):
        calls.append(pod)
        raise ValueError('permission denied or network unavailable')
    with pytest.raises(ValueError, match='permission denied'):
        admit_manifest(manifest, 'benchmark', probe)
    assert len(calls) == 1


def test_new_forge_admission_never_rewrites_rejected_uid(source):
    _, _, manifest = source
    before, calls = copy.deepcopy(manifest), []
    def probe(pod):
        calls.append(pod)
        raise UIDAdmissionRejected('runAsUser rejected')
    with pytest.raises(ValueError, match='update and commit the recipe'):
        admit_manifest(manifest, 'benchmark', probe, allow_uid_omission=False)
    assert len(calls) == 1 and manifest == before


def test_container_uid_overrides_pod_uid_and_root_is_rejected(source):
    _, _, manifest = source
    spec = manifest['spec']['template']['spec']
    spec['containers'][0]['securityContext'] = {'runAsUser': 12345, 'runAsNonRoot': True}
    assert effective_uids(spec) == {'vllm': 12345}
    spec['containers'][0]['securityContext']['runAsUser'] = 0
    with pytest.raises(ValueError, match='concrete non-root UID'):
        admit_manifest(manifest, 'benchmark', copy.deepcopy)


@pytest.mark.parametrize('change', ['root', 'different-uid', 'different-account', 'missing-uid'])
def test_actual_admitted_uid_must_match_probe_before_benchmark(source, change):
    _, _, manifest = source
    decisions = admit_manifest(manifest, 'benchmark', copy.deepcopy)
    pod = {'spec': copy.deepcopy(manifest['spec']['template']['spec'])}
    if change == 'root':
        pod['spec']['securityContext']['runAsUser'] = 0
    elif change == 'different-uid':
        pod['spec']['securityContext']['runAsUser'] = 12345
    elif change == 'different-account':
        pod['spec']['serviceAccountName'] = 'different'
    else:
        del pod['spec']['securityContext']['runAsUser']
    with pytest.raises(ValueError):
        check_runtime_uids({'items': [pod]}, decisions)


def test_import_rejects_missing_or_tampered_uid_evidence(evidence, tmp_path):
    repo, _, request, files, _, _ = evidence
    runtime = json_load(files['runtime.json'])
    runtime['uid_admission'][0]['authored_uid'] = 12345
    files['runtime.json'] = json_bytes(runtime)
    path = tmp_path / 'bad-uid.tar.gz'
    digest = write_archive(path, request, 'succeeded', files)
    with pytest.raises(ValueError, match='UID admission decision'):
        import_bundle(repo, path, digest)


@pytest.mark.parametrize('decisions', [None, {}, []])
def test_import_requires_uid_decisions(evidence, tmp_path, decisions):
    repo, _, request, files, _, _ = evidence
    runtime = json_load(files['runtime.json'])
    runtime['uid_admission'] = decisions
    files['runtime.json'] = json_bytes(runtime)
    path = tmp_path / 'missing-uid.tar.gz'
    digest = write_archive(path, request, 'succeeded', files)
    with pytest.raises(ValueError, match='UID admission decisions'):
        import_bundle(repo, path, digest)


@pytest.mark.parametrize('change', [None, 'openshift-ca', 'bad-openshift-ca', 'secret', 'root-ca', 'writable', 'mount-path', 'extra-source'])
def test_import_allows_only_standard_injected_service_account_mount(evidence, tmp_path, change):
    repo, _, request, files, _, _ = evidence
    runtime = json_load(files['runtime.json'])
    volume = {'name': 'kube-api-access-test', 'projected': {'defaultMode': 420, 'sources': [
        {'serviceAccountToken': {'expirationSeconds': 3607, 'path': 'token'}},
        {'configMap': {'name': 'kube-root-ca.crt', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}},
        {'downwardAPI': {'items': [{'fieldRef': {'apiVersion': 'v1', 'fieldPath': 'metadata.namespace'},
                                   'path': 'namespace'}]}}]}}
    mount = {'name': volume['name'], 'mountPath': '/var/run/secrets/kubernetes.io/serviceaccount', 'readOnly': True}
    if change in {'openshift-ca', 'bad-openshift-ca'}:
        volume['projected']['sources'].append({'configMap': {'name': 'openshift-service-ca.crt',
            'items': [{'key': 'service-ca.crt', 'path': 'service-ca.crt'}]}})
        if change == 'bad-openshift-ca':
            volume['projected']['sources'][-1]['configMap']['items'][0]['key'] = 'unexpected'
    elif change == 'secret':
        volume['projected']['sources'][0] = {'secret': {'name': 'unexpected'}}
    elif change == 'root-ca':
        volume['projected']['sources'][1]['configMap']['name'] = 'unexpected'
    elif change == 'writable':
        mount['readOnly'] = False
    elif change == 'mount-path':
        mount['mountPath'] = '/model'
    elif change == 'extra-source':
        volume['projected']['sources'].append({'secret': {'name': 'unexpected'}})
    for key in ('pods_before', 'pods_after'):
        for pod in runtime[key]['items']:
            pod['spec']['volumes'] = [volume]
            pod['spec']['containers'][0]['volumeMounts'] = [mount]
    files['runtime.json'] = json_bytes(runtime)
    path = tmp_path / 'admitted-mount.tar.gz'
    digest = write_archive(path, request, 'succeeded', files)
    if change in {None, 'openshift-ca'}:
        assert import_bundle(repo, path, digest)['applied'] is False
    else:
        with pytest.raises(ValueError, match='runtime serving configuration differs'):
            import_bundle(repo, path, digest)
