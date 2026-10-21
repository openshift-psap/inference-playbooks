"""Bind captured launch configuration to the selected manifest at the tested SHA."""
import copy
import subprocess
from pathlib import PurePosixPath

from recipe_evidence import load_unique_yaml_all


def model_mount_matches(pod, container, uri, model):
    """Bind a controller-generated local path to its declared PVC, not an HF ID."""
    if uri.startswith('hf://'):
        return model == uri.removeprefix('hf://')
    if not uri.startswith('pvc://') or not isinstance(model, str):
        return False
    claim, _, directory = uri.removeprefix('pvc://').partition('/')
    if (not claim or PurePosixPath(directory).is_absolute() or '..' in PurePosixPath(directory).parts
            or not PurePosixPath(model).is_absolute()):
        return False
    source_volumes = pod['spec'].get('volumes', [])
    volumes = {volume['name']: volume for volume in source_volumes}
    if len(volumes) != len(source_volumes):
        return False
    for mount in container.get('volumeMounts', []):
        volume = volumes.get(mount.get('name'), {})
        if volume.get('persistentVolumeClaim', {}).get('claimName') != claim or 'subPathExpr' in mount:
            continue
        if mount.get('subPath', '') == directory:
            expected = mount.get('mountPath')
        elif not mount.get('subPath'):
            expected = str(PurePosixPath(mount.get('mountPath', '')) / directory)
        else:
            continue
        if model == expected:
            return True
    return False


def _labels(actual, expected, additions):
    """Remove only Forge's known additive run labels, never authored labels."""
    metadata = actual.get('metadata', {})
    baseline = expected.get('metadata', {})
    labels = metadata.get('labels', {})
    for key, value in additions.items():
        if key not in baseline.get('labels', {}) and key in labels:
            if labels[key] != value:
                raise ValueError('captured launch has an invalid Forge label')
            del labels[key]
    if not labels and 'labels' not in baseline:
        metadata.pop('labels', None)
    if not metadata and 'metadata' not in expected:
        actual.pop('metadata', None)


def _pod(actual, expected):
    # Forge's _strip_hardcoded_uid removes only the pod-level UID and appends
    # HOME=/tmp, USER=nobody when absent. No other security/env drift is ignored.
    if ('runAsUser' not in actual.get('securityContext', {}) and
            'runAsUser' in expected.get('securityContext', {})):
        del expected['securityContext']['runAsUser']
    baseline = {container['name']: container for container in expected.get('containers', [])}
    for container in actual.get('containers', []):
        original = baseline.get(container.get('name'), {})
        declared = {entry['name'] for entry in original.get('env', [])}
        container['env'] = [entry for entry in container.get('env', [])
                            if not (entry.get('name') not in declared and entry in (
                                {'name': 'HOME', 'value': '/tmp'}, {'name': 'USER', 'value': 'nobody'}))]
        if not container['env'] and 'env' not in original:
            container.pop('env')


def validate_launch(repo, manifest_path, revision, captured):
    """Compare data only; never run historical renderers or arbitrary PR code."""
    source = subprocess.run(['git', '-C', str(repo), 'show', f'{revision}:{manifest_path}'],
                            check=True, capture_output=True, text=True).stdout
    documents = [doc for doc in load_unique_yaml_all(source)
                 if isinstance(doc, dict) and doc.get('kind') in
                 {'Deployment', 'LeaderWorkerSet', 'LLMInferenceService'}]
    if len(documents) != 1 or documents[0].get('kind') != captured.get('kind'):
        raise ValueError('tested manifest serving kind is missing or ambiguous')
    expected = documents[0]
    drift = subprocess.run(['git', '-C', str(repo), 'diff', '--name-only', revision, '--', manifest_path],
                           check=True, capture_output=True, text=True).stdout
    if drift:
        raise ValueError('selected manifest differs from tested revision')
    if expected.get('apiVersion') != captured.get('apiVersion'):
        raise ValueError('captured launch API version differs from tested manifest')
    actual_spec, expected_spec = copy.deepcopy(captured['spec']), copy.deepcopy(expected['spec'])
    run = captured.get('metadata', {}).get('name')
    additions = {'forge.openshift.io/run': run}
    if captured['kind'] == 'Deployment':
        actual_template, expected_template = actual_spec['template'], expected_spec['template']
        _labels(actual_template, expected_template, additions)
        _pod(actual_template['spec'], expected_template['spec'])
        selector = actual_spec.get('selector', {}).get('matchLabels', {})
        if 'forge.openshift.io/run' not in expected_spec.get('selector', {}).get('matchLabels', {}):
            if 'forge.openshift.io/run' in selector:
                if selector.pop('forge.openshift.io/run') != run:
                    raise ValueError('captured launch has an invalid Forge selector')
    elif captured['kind'] == 'LeaderWorkerSet':
        for role in ('leaderTemplate', 'workerTemplate'):
            actual = actual_spec['leaderWorkerTemplate'][role]
            baseline = expected_spec['leaderWorkerTemplate'][role]
            labels = {**additions, **({'forge.openshift.io/endpoint': 'leader'}
                                     if role == 'leaderTemplate' else {})}
            _labels(actual, baseline, labels)
            _pod(actual['spec'], baseline['spec'])
    if actual_spec != expected_spec:
        raise ValueError('captured launch configuration differs from tested manifest')
    return expected
