"""Local progress observations are not benchmark evidence or cluster controls."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import monitor_contributor as monitor


REQUEST = {'run_id': 'contributor-' + 'a' * 32, 'workload': 'guidellm-smoke'}


def test_progress_is_atomic_local_and_reports_observations(tmp_path):
    progress = monitor.Progress(tmp_path, REQUEST)
    first = monitor.snapshot(tmp_path)
    assert first['stage'] == 'preflight'
    progress.update('benchmarking', serving_ready_containers=1, detail='Concurrency windows: 1, 5.')
    state = monitor.snapshot(tmp_path)
    assert state['outcome'] == 'running'
    assert state['started_at'] == first['started_at']
    text = monitor.render(state)
    assert 'Running benchmark' in text and 'ready serving containers: 1' in text
    assert 'Concurrency windows: 1, 5.' in text
    assert not (tmp_path / '.progress.json.tmp').exists()
    progress.update('finished', outcome='succeeded')
    assert 'maintainer import validation/review is separate' in monitor.render(monitor.snapshot(tmp_path))


def test_monitor_only_displays_native_task_identifiers(tmp_path):
    monitor.Progress(tmp_path, REQUEST)
    log = tmp_path / 'private-diagnostics/001__inference_playbooks_test_dir/001__wait_recipe/task.log'
    log.parent.mkdir(parents=True)
    log.write_text('private credentials must not be displayed\n~~ TASK: wait_for_primary : description\n')
    state = monitor.snapshot(tmp_path)
    assert state['task'] == 'wait_for_primary'
    assert 'private credentials' not in monitor.render(state)
    assert monitor.main([str(tmp_path)]) == 0


def test_read_only_monitor_does_not_rewrite_progress(tmp_path):
    monitor.Progress(tmp_path, REQUEST)
    path = tmp_path / 'progress.json'
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert monitor.main([str(tmp_path)]) == 0
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_watch_stops_on_reported_final_outcome(tmp_path, monkeypatch, capsys):
    progress = monitor.Progress(tmp_path, REQUEST)
    sleeps = []
    def refresh(seconds):
        sleeps.append(seconds)
        progress.update('finished', outcome='failed')
    monkeypatch.setattr(monitor.time, 'sleep', refresh)
    assert monitor.main([str(tmp_path), '--watch']) == 0
    text = capsys.readouterr().out
    assert 'Checking prerequisites' in text and 'Outcome: failed' in text
    assert sleeps == [5]


def test_ctrl_c_only_exits_monitor(tmp_path, monkeypatch):
    progress = monitor.Progress(tmp_path, REQUEST)
    def interrupt(_seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(monitor.time, 'sleep', interrupt)
    assert monitor.main([str(tmp_path), '--watch']) == 0
    assert monitor.snapshot(tmp_path)['outcome'] == 'running'
    assert progress.state['stage'] == 'preflight'


def test_missing_and_partial_snapshots_do_not_claim_running_or_success(tmp_path):
    assert monitor.snapshot(tmp_path)['outcome'] == 'unknown'
    (tmp_path / 'progress.json').write_text('{')
    assert monitor.snapshot(tmp_path)['outcome'] == 'unknown'


def test_finished_bundle_wins_if_writer_stopped_before_final_snapshot(tmp_path):
    monitor.Progress(tmp_path, REQUEST)
    legacy_bundle(tmp_path)
    assert monitor.snapshot(tmp_path)['outcome'] == 'succeeded'


@pytest.mark.parametrize('value', [[], {'schema_version': 99}, {'schema_version': 1, 'stage': 'finished', 'outcome': 'running'}])
def test_invalid_progress_is_not_a_success(tmp_path, value):
    (tmp_path / 'progress.json').write_text(json.dumps(value))
    assert monitor.main([str(tmp_path)]) == 1


def legacy_bundle(output, *, outcome='succeeded', symlink=False):
    envelope = json.dumps({'schema_version': 1, 'request': REQUEST, 'outcome': outcome}).encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w') as archive:
        member = tarfile.TarInfo('bundle.json')
        member.size = len(envelope)
        if symlink:
            member.type, member.linkname, member.size = tarfile.SYMTYPE, '/private-file', 0
        archive.addfile(member, None if symlink else io.BytesIO(envelope))
    data = gzip.compress(raw.getvalue())
    (output / 'evidence.tar.gz').write_bytes(data)
    (output / 'evidence.tar.gz.sha256').write_text('sha256:' + hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize('outcome', sorted(monitor.OUTCOMES))
def test_old_completed_runs_need_no_credentials_or_progress_file(tmp_path, outcome):
    legacy_bundle(tmp_path, outcome=outcome)
    state = monitor.snapshot(tmp_path)
    assert state['outcome'] == outcome and state['stage'] == 'finished'


@pytest.mark.parametrize('change', ['checksum', 'link'])
def test_unsafe_terminal_bundle_fails_closed(tmp_path, monkeypatch, change):
    legacy_bundle(tmp_path, symlink=change == 'link')
    if change == 'checksum':
        (tmp_path / 'evidence.tar.gz.sha256').write_text('sha256:' + '0' * 64)
    assert monitor.main([str(tmp_path)]) == 1


def test_local_text_cannot_inject_terminal_control_characters(tmp_path):
    progress = monitor.Progress(tmp_path, REQUEST)
    progress.update('deploying', detail='not a clear-screen command: \x1b[2J')
    assert '\x1b' not in monitor.render(monitor.snapshot(tmp_path))


@pytest.mark.parametrize('run_id', [123, {}, [], None])
def test_invalid_run_identity_is_reported_without_traceback(tmp_path, capsys, run_id):
    monitor.Progress(tmp_path, REQUEST)
    path = tmp_path / 'progress.json'
    state = json.loads(path.read_text())
    state['run_id'] = run_id
    path.write_text(json.dumps(state))
    assert monitor.main([str(tmp_path)]) == 1
    assert 'Cannot read progress safely' in capsys.readouterr().out


def test_malformed_progress_uses_verified_bundle_and_watch_finishes(tmp_path):
    (tmp_path / 'progress.json').write_text('{')
    legacy_bundle(tmp_path)
    assert monitor.snapshot(tmp_path)['outcome'] == 'succeeded'
    assert monitor.main([str(tmp_path), '--watch']) == 0


def test_malformed_progress_does_not_bypass_bundle_checksum(tmp_path):
    (tmp_path / 'progress.json').write_text('{')
    legacy_bundle(tmp_path)
    (tmp_path / 'evidence.tar.gz.sha256').write_text('sha256:' + '0' * 64)
    assert monitor.main([str(tmp_path), '--watch']) == 1


def test_monitor_streams_bundle_larger_than_old_expanded_limit(tmp_path):
    class Zeroes:
        def read(self, size):
            return b'\0' * size
    path = tmp_path / 'evidence.tar.gz'
    with tarfile.open(str(path), 'w|gz') as archive:
        member = tarfile.TarInfo('raw/harness-0.json')
        member.size = 257 * 1024 * 1024
        archive.addfile(member, Zeroes())
        envelope = json.dumps({'schema_version': 1, 'request': REQUEST, 'outcome': 'succeeded'}).encode()
        member = tarfile.TarInfo('bundle.json')
        member.size = len(envelope)
        archive.addfile(member, io.BytesIO(envelope))
    (tmp_path / 'evidence.tar.gz.sha256').write_text('sha256:' + hashlib.sha256(path.read_bytes()).hexdigest())
    assert monitor.snapshot(tmp_path)['outcome'] == 'succeeded'
