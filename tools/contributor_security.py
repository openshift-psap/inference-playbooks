"""Data-only non-root UID decisions; admission probes never create Pods."""
from __future__ import annotations

import copy


class UIDAdmissionRejected(ValueError):
    """The API explicitly rejected an authored runAsUser (not an auth/network failure)."""


def positive_uid(value):
    return type(value) is int and value > 0


def effective_uids(spec):
    """Container security context overrides Pod security context, including UID 0."""
    pod_context = spec.get('securityContext', {})
    result = {}
    for container in spec['containers']:
        context = container.get('securityContext', {})
        uid = context.get('runAsUser', pod_context.get('runAsUser'))
        if (not positive_uid(uid)
                or context.get('runAsNonRoot', pod_context.get('runAsNonRoot')) is False):
            raise ValueError('admission did not establish a concrete non-root UID for every container')
        result[container['name']] = uid
    if not result:
        raise ValueError('Pod admission candidate has no containers')
    return result


def pod_templates(manifest):
    if manifest['kind'] == 'Deployment':
        return {'template': manifest['spec']['template']}
    if manifest['kind'] == 'LeaderWorkerSet':
        templates = manifest['spec']['leaderWorkerTemplate']
        return {name: templates[name] for name in ('leaderTemplate', 'workerTemplate')}
    # Controller-expanded LLMInferenceService templates are not complete Pods.
    # Do not guess their missing image/configuration or change their UID policy.
    return {}


def admit_manifest(manifest, namespace, probe, *, allow_uid_omission=True):
    """Preserve authored UID first; omit it only after an explicit UID rejection.

    Probes use the caller's identity and the workload's service account. They are
    not a controller-identity attestation: actual admitted UIDs must be rechecked
    before benchmarking. No impersonation or permission changes are performed.
    """
    decisions = []
    for role, template in pod_templates(manifest).items():
        spec = template['spec']
        uid = spec.get('securityContext', {}).get('runAsUser')
        if uid is not None and not positive_uid(uid):
            raise ValueError('authored runAsUser must be non-root; refusing to weaken security')
        candidate = {'apiVersion': 'v1', 'kind': 'Pod',
                     'metadata': {**copy.deepcopy(template.get('metadata', {})),
                                  'generateName': 'playbooks-uid-probe-', 'namespace': namespace},
                     'spec': copy.deepcopy(spec)}
        candidate['metadata'].pop('name', None)
        mode = 'preserve'
        try:
            admitted = probe(candidate)
        except UIDAdmissionRejected:
            if not allow_uid_omission:
                raise ValueError('authored UID rejected; update and commit the recipe instead of rewriting its launch') from None
            if not positive_uid(uid) or spec.get('securityContext', {}).get('runAsNonRoot') is not True:
                raise ValueError('UID rejected; no safe pod-level non-root omission candidate available') from None
            del candidate['spec']['securityContext']['runAsUser']
            admitted = probe(candidate)
            mode = 'platform-assigned'
        uids = effective_uids(admitted['spec'])
        declared_names = {c['name'] for c in spec['containers']}
        if not declared_names.issubset(uids):
            raise ValueError('admission response omitted a declared container')
        if mode == 'preserve':
            for container in spec['containers']:
                declared = container.get('securityContext', {}).get('runAsUser', uid)
                if declared is not None and uids[container['name']] != declared:
                    raise ValueError('admission changed the authored effective UID')
        account = spec.get('serviceAccountName', 'default')
        if admitted['spec'].get('serviceAccountName', 'default') != account:
            raise ValueError('admission changed the intended service account')
        if mode == 'platform-assigned':
            # Never copy unrelated admission mutations back into recipe inputs.
            del spec['securityContext']['runAsUser']
        decisions.append({'role': role, 'mode': mode, 'authored_uid': uid,
                          'service_account': account,
                          'admitted_uids': {name: uids[name] for name in sorted(declared_names)},
                          'probe_identity': 'caller; actual controller admission rechecked before benchmark'})
    return decisions


def allow_uid_removal(decision, actual, expected):
    """Validate the exact bounded launch transformation during offline replay."""
    uid = expected.get('securityContext', {}).get('runAsUser')
    if (decision.get('authored_uid') != uid
            or decision.get('service_account') != expected.get('serviceAccountName', 'default')
            or set(decision.get('admitted_uids', {})) != {c['name'] for c in expected['containers']}
            or not all(positive_uid(v) for v in decision['admitted_uids'].values())
            or decision.get('probe_identity') != 'caller; actual controller admission rechecked before benchmark'):
        raise ValueError('UID admission decision does not match the tested template')
    mode = decision.get('mode')
    if mode == 'preserve':
        for container in expected['containers']:
            declared = container.get('securityContext', {}).get('runAsUser', uid)
            if declared is not None and decision['admitted_uids'][container['name']] != declared:
                raise ValueError('UID admission decision did not preserve the authored effective UID')
        return False
    if (mode != 'platform-assigned' or not positive_uid(uid)
            or actual.get('securityContext', {}).get('runAsNonRoot') is not True
            or 'runAsUser' in actual.get('securityContext', {})):
        raise ValueError('unsupported UID admission transformation')
    return True


def check_runtime_uids(pods, decisions):
    """Fail before workload execution if actual admission differs from the probe."""
    if not pods.get('items'):
        raise ValueError('no admitted serving Pods for UID verification')
    for pod in pods['items']:
        spec = pod['spec']
        observed = effective_uids(spec)
        account = spec.get('serviceAccountName', 'default')
        if decisions:
            matches = [d for d in decisions if d['service_account'] == account
                       and all(d['admitted_uids'].get(name) == uid for name, uid in observed.items())]
            if not matches:
                raise ValueError('actual admitted non-root UID/service account differs from dry-run evidence')


def runtime_mounts(spec, container, declared):
    """Ignore only Kubernetes' standard injected, read-only API credential mount.

    Validate its projected sources, not just a familiar name/path. Authored
    volumes/mounts and any other admission additions still compare exactly.
    """
    authored_names = {v['name'] for v in declared.get('volumes', [])}
    volumes = spec.get('volumes', [])
    if len({v['name'] for v in volumes}) != len(volumes):
        raise ValueError('runtime serving configuration differs from launch: duplicate volumes')
    ignored = set()
    if declared.get('automountServiceAccountToken') is not False:
        for volume in volumes:
            name = volume['name']
            if name in authored_names or not name.startswith('kube-api-access-'):
                continue
            sources = volume.get('projected', {}).get('sources', [])
            if len(sources) not in {3, 4}:
                continue
            expiry = sources[0].get('serviceAccountToken', {}).get('expirationSeconds')
            if type(expiry) is not int or not 600 <= expiry <= 86400:
                continue
            expected = {'name': name, 'projected': {'defaultMode': 420, 'sources': [
                {'serviceAccountToken': {'expirationSeconds': expiry, 'path': 'token'}},
                {'configMap': {'name': 'kube-root-ca.crt', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}},
                {'downwardAPI': {'items': [{'fieldRef': {'apiVersion': 'v1', 'fieldPath': 'metadata.namespace'},
                                            'path': 'namespace'}]}}]}}
            if len(sources) == 4:
                expected['projected']['sources'].append({'configMap': {
                    'name': 'openshift-service-ca.crt',
                    'items': [{'key': 'service-ca.crt', 'path': 'service-ca.crt'}]}})
            if volume == expected:
                ignored.add(name)
    return [mount for mount in container.get('volumeMounts', []) if not (
        mount.get('name') in ignored and mount == {
            'name': mount['name'], 'mountPath': '/var/run/secrets/kubernetes.io/serviceaccount', 'readOnly': True})]
