#!/usr/bin/env python3
"""Read-only local progress view; standard-library only, no cluster credentials."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import time

STAGES = {
    'preflight': 'Checking prerequisites',
    'deploying': 'Starting serving workload',
    'benchmarking': 'Running benchmark',
    'finalizing': 'Cleaning up and postprocessing',
    'collecting': 'Collecting evidence',
    'packaging': 'Writing diagnostic bundle',
    'finished': 'Finished',
}
OUTCOMES = {'succeeded', 'failed', 'interrupted', 'collection-failed'}
MAX_TOTAL = 256 * 1024 * 1024


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class Progress:
    """Execution-owned observations, never copied into the shareable archive."""
    def __init__(self, output: Path, request: dict):
        self.output = output
        self.state = {'schema_version': 1, 'run_id': request['run_id'],
                      'workload': request['workload'], 'started_at': timestamp(), 'outcome': 'running'}
        self.update('preflight')

    def update(self, stage, **observations):
        if stage not in STAGES:
            raise ValueError('unknown progress stage')
        now = timestamp()
        if stage != self.state.get('stage'):
            self.state['stage_started_at'] = now
        self.state.update(stage=stage, updated_at=now, **observations)
        temporary = self.output / '.progress.json.tmp'
        temporary.write_text(json.dumps(self.state, sort_keys=True, indent=2) + '\n')
        temporary.replace(self.output / 'progress.json')


def completed_bundle(output):
    """Read a reported outcome, not an importer verdict; also supports old runs."""
    path, sidecar = output / 'evidence.tar.gz', output / 'evidence.tar.gz.sha256'
    if not path.exists() or not sidecar.exists():
        return None
    digest = sidecar.read_text().strip()
    if path.stat().st_size > MAX_TOTAL or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise ValueError('invalid bundle checksum or size')
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    if 'sha256:' + hasher.hexdigest() != digest:
        raise ValueError('bundle checksum mismatch')
    with gzip.open(path, 'rb') as stream:
        expanded = stream.read(MAX_TOTAL + 1)
    if len(expanded) > MAX_TOTAL:
        raise ValueError('expanded bundle exceeds progress reader limit')
    with tarfile.open(fileobj=io.BytesIO(expanded), mode='r:') as archive:
        members = [member for member in archive if member.name == 'bundle.json']
        if len(members) != 1 or not members[0].isfile() or members[0].size > 1024 * 1024:
            raise ValueError('missing or unsafe bundle envelope')
        envelope = json.load(archive.extractfile(members[0]))
    if envelope.get('schema_version') != 1 or envelope.get('outcome') not in OUTCOMES:
        raise ValueError('unsupported bundle outcome')
    return {'schema_version': 1, 'run_id': envelope['request']['run_id'], 'stage': 'finished',
            'workload': envelope['request']['workload'], 'outcome': envelope['outcome']}


def latest_task(output):
    """Only display task identifiers, never arbitrary private log lines."""
    patterns = ('*__inference_playbooks_test_dir/*__wait_recipe/task.log',
                '*__inference_playbooks_test_dir/*__benchmark_*/*__run_*_benchmark/task.log')
    root = output / 'private-diagnostics'
    paths = [path for pattern in patterns for path in root.glob(pattern)
             if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(output.resolve())]
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                text = stream.read(65536).decode(errors='replace')
        except FileNotFoundError:
            continue
        tasks = re.findall(r'^~~ TASK: ([a-zA-Z0-9_]{1,100})\b', text, re.M)
        if tasks:
            return tasks[-1]
    return None


def snapshot(output: Path):
    path = output / 'progress.json'
    try:
        if path.stat().st_size > 65536:
            raise ValueError('progress file exceeds limit')
        state = json.loads(path.read_text())
    except FileNotFoundError:
        state = completed_bundle(output)
        if state is None:
            return {'stage': 'unknown', 'outcome': 'unknown',
                    'note': 'No progress snapshot yet (not started, or an older runner).'}
    except json.JSONDecodeError:
        return {'stage': 'unknown', 'outcome': 'unknown', 'note': 'Progress snapshot is not readable yet.'}
    if not isinstance(state, dict) or state.get('schema_version') != 1 or state.get('stage') not in STAGES:
        raise ValueError('unsupported progress snapshot')
    if state.get('outcome') not in OUTCOMES | {'running'}:
        raise ValueError('unsupported progress outcome')
    if state['stage'] == 'finished' and state['outcome'] not in OUTCOMES:
        raise ValueError('finished progress requires a final outcome')
    if not re.fullmatch(r'contributor-[a-f0-9]{32}', state.get('run_id', '')):
        raise ValueError('invalid progress run identity')
    if state['stage'] != 'finished':
        completed = completed_bundle(output)
        if completed:
            if completed['run_id'] != state['run_id']:
                raise ValueError('progress and bundle belong to different runs')
            state.update(completed)
            return state
        state['task'] = latest_task(output)
    return state


def age(value):
    if not value:
        return 'unknown'
    try:
        seconds = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()))
    except (ValueError, TypeError):
        return 'unknown'
    return f'{seconds // 60}m {seconds % 60:02d}s'


def render(state):
    # JSON escaping prevents terminal control characters from local metadata/logs.
    def safe(value):
        return json.dumps(str(value), ensure_ascii=True)[1:-1]
    lines = [f'Run: {safe(state.get("run_id", "unknown"))}',
             f'Stage: {STAGES.get(state["stage"], "Unknown")}',
             f'Outcome: {safe(state["outcome"])}']
    if state.get('workload'):
        lines.append(f'Workload: {safe(state["workload"])}')
    if state.get('started_at'):
        end = state.get('updated_at') if state['stage'] == 'finished' else None
        if end:
            try:
                seconds = int((datetime.fromisoformat(end) - datetime.fromisoformat(state['started_at'])).total_seconds())
                lines.append(f'Elapsed: {seconds // 60}m {seconds % 60:02d}s')
            except (TypeError, ValueError):
                pass
        else:
            lines.append(f'Elapsed: {age(state["started_at"])}; stage age: {age(state.get("stage_started_at"))}')
    if state.get('task'):
        lines.append(f'Latest Forge task: {safe(state["task"])}')
    if 'serving_ready_containers' in state:
        lines.append(f'Last observed ready serving containers: {safe(state["serving_ready_containers"])}')
    if state.get('detail'):
        lines.append(safe(state['detail']))
    if state.get('note'):
        lines.append(safe(state['note']))
    if state['stage'] == 'finished':
        lines.append('Execution outcome only; maintainer import validation/review is separate.')
    else:
        lines.append('Stage age is not a heartbeat. No overall percentage is inferred.')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='local run output directory')
    parser.add_argument('--watch', action='store_true', help='refresh every five seconds until a final outcome; Ctrl-C exits only the monitor')
    args = parser.parse_args(argv)
    if not args.output.is_dir():
        parser.error('output directory does not exist; start the run first')
    try:
        while True:
            state = snapshot(args.output)
            print(render(state), flush=True)
            if not args.watch or state['stage'] == 'finished':
                return 0
            print('\n--- refresh in 5s; Ctrl-C stops this monitor only ---', flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, KeyError, EOFError, tarfile.TarError):
        print('Cannot read progress safely; inspect local diagnostics. No cluster action was taken.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
