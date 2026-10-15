import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def test_workflow_separates_unprivileged_pr_and_main_deployment():
    workflow = yaml.safe_load((REPO / '.github/workflows/pages.yml').read_text())
    events = workflow.get('on', workflow.get(True))  # YAML 1.1 treats on as boolean.
    assert 'pull_request' in events and 'pull_request_target' not in events
    assert events['push']['branches'] == ['main']
    assert workflow['permissions'] == {'contents': 'read'}
    build = workflow['jobs']['build']; deploy = workflow['jobs']['deploy']
    assert 'pull_request' in build['if'] and "refs/heads/main" in build['if']
    assert 'refs/heads/main' in deploy['if'] and "!= 'pull_request'" in deploy['if']
    assert deploy['needs'] == 'build'
    assert deploy['permissions'] == {'contents': 'read', 'pages': 'write', 'id-token': 'write'}
    assert deploy['environment']['name'] == 'github-pages'
    checkout = build['steps'][0]
    assert checkout['with']['persist-credentials'] is False
    assert workflow['concurrency']['group'] != deploy['concurrency']['group']
    assert 'pull_request' in str(workflow['concurrency']['cancel-in-progress'])
    assert deploy['concurrency']['cancel-in-progress'] is False
    for job in workflow['jobs'].values():
        for step in job['steps']:
            if 'uses' in step: assert re.search(r'@[a-f0-9]{40}$', step['uses'])
    assert 'commits/main' in deploy['steps'][0]['run'] and '$GITHUB_SHA' in deploy['steps'][0]['run']
    assert 'secrets.' not in (REPO / '.github/workflows/pages.yml').read_text()
    uploads = [step for step in build['steps'] if 'upload-' in step.get('uses', '')]
    assert len(uploads) == 2 and all(step['with']['path'] == '.build/site' for step in uploads)
    assert 'pull_request' in uploads[0]['if']
    assert 'refs/heads/main' in uploads[1]['if']


def test_ingest_installs_same_pinned_kustomize_before_site_build():
    ingest = yaml.safe_load((REPO / '.github/workflows/benchmark-ingest.yml').read_text())
    pages = yaml.safe_load((REPO / '.github/workflows/pages.yml').read_text())
    steps = ingest['jobs']['ingest']['steps']
    install = next(step for step in steps if step.get('name') == 'Install Kustomize')
    pages_install = next(step for step in pages['jobs']['build']['steps'] if step.get('name') == 'Install Kustomize')
    assert install['run'] == pages_install['run']
    build = next(step for step in steps if step.get('name') == 'Check catalog integration')
    assert steps.index(install) < steps.index(build)
    assert 'sha256sum --check' in install['run'] and '$GITHUB_PATH' in install['run']


def test_chart_cdn_script_requires_integrity_and_crossorigin():
    from html.parser import HTMLParser
    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts = []
        def handle_starttag(self, tag, attrs):
            if tag == 'script':
                self.scripts.append(dict(attrs))
    parser = Scripts()
    parser.feed((REPO / 'site/index.html').read_text())
    chart = next(script for script in parser.scripts if 'cdn.jsdelivr.net' in script.get('src', ''))
    assert chart['src'] == 'https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js'
    assert re.fullmatch(r'sha384-[A-Za-z0-9+/]{64}', chart['integrity'])
    assert chart['crossorigin'] == 'anonymous'


def test_site_build_is_deterministic_and_does_not_modify_tracked_sources():
    before = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=REPO)
    outputs = [REPO / '.build/test-bundle-one', REPO / '.build/test-bundle-two']
    for output in outputs:
        result = subprocess.run([sys.executable, 'tools/build-site.py', '--out', str(output)], cwd=REPO, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert (outputs[0] / 'catalog.json').read_bytes() == (outputs[1] / 'catalog.json').read_bytes()
    assert (outputs[0] / 'build-provenance.json').read_bytes() == (outputs[1] / 'build-provenance.json').read_bytes()
    provenance = json.loads((outputs[0] / 'build-provenance.json').read_text())
    assert {'index.html', 'workloads.html', 'app.js', 'styles.css', 'catalog.json'} <= set(provenance['sha256'])
    after = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=REPO)
    assert before == after


def test_mismatched_build_sha_fails_before_publication():
    result = subprocess.run([sys.executable, 'tools/build-site.py', '--source-sha', '0' * 40], cwd=REPO, capture_output=True, text=True)
    assert result.returncode != 0 and 'source SHA' in result.stderr
