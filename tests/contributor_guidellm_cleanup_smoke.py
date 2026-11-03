"""Exercise the installed Forge failure finalizers; fake Kubernetes, not the DSL.

Run in the built runner with --network none and without cluster credentials.
This checks lifecycle behavior, not GPU execution or performance.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, '/opt/forge')
os.chdir('/opt/forge')

from projects.core.dsl.runtime import TaskExecutionError
from projects.core.library import env
from projects.guidellm.toolbox.run_guidellm_benchmark import main as benchmark


with tempfile.TemporaryDirectory() as temporary:
    os.environ['ARTIFACT_DIR'] = temporary
    os.environ['KUBECONFIG'] = str(Path(temporary) / 'nonexistent-kubeconfig')
    env.init()
    state = {'created': False, 'captured': False, 'deleted': False, 'sidecar_running': False}

    def oc(*args, **kwargs):
        output = ''
        if kwargs.get('stdout_dest'):
            path = Path(kwargs['stdout_dest'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic diagnostic\n')
            if args[0] == 'describe':
                state['captured'] = True
        elif args[0] == 'create':
            document = yaml.safe_load(Path(args[2]).read_text())
            assert document['kind'] == 'Job'
            document['metadata']['uid'] = 'fixture-job-uid'
            state.update(created=True, sidecar_running=True)
            output = json.dumps(document)
        elif args[0] == 'delete':
            if args[1].startswith('--raw='):
                assert state['created'] and state['captured'], 'cleanup preceded diagnostic capture'
                assert args[1] == '--raw=/apis/batch/v1/namespaces/fixture/jobs/failure-smoke'
                options = json.loads(kwargs['input_text'])
                assert options['preconditions'] == {'uid': 'fixture-job-uid'}
                assert options['propagationPolicy'] == 'Foreground'
                state.update(deleted=True, sidecar_running=False)
            else:
                assert not state['created'], 'new resources must not be deleted by name'
        elif args[:2] == ('get', 'pods'):
            output = '' if '--no-headers' in args else 'fixture-pod'
        elif args[:2] == ('get', 'pod'):
            output = json.dumps({'terminated': {'exitCode': 1}})
        else:
            raise AssertionError(f'unexpected synthetic Kubernetes call: {args}')
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    benchmark.oc = oc
    benchmark.oc_get_json = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('unexpected Kubernetes lookup'))
    try:
        benchmark.run(endpoint_url='https://model.example.test', name='failure-smoke', namespace='fixture')
    except TaskExecutionError as error:
        assert error.task_name == 'check_guidellm_benchmark_result'
        assert 'exit code 1' in str(error.original_exception)
    else:
        raise AssertionError('failed GuideLLM main container was reported as success')
    assert state['captured'] and state['deleted'] and not state['sidecar_running']
    log = next(Path(temporary).glob('*__run_guidellm_benchmark/task-always.log')).read_text()
    assert 'TASK: cleanup_guidellm_resources' in log
    assert 'SKIPPING TASK: extract_results' in log
    print('PASS: genuine Forge failed main -> diagnostics -> UID-owned Job cleanup; original error preserved')
