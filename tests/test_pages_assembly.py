"""Combined publication never replaces the production root with staging code."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from assemble_pages import assemble


def bundle(root, label, sha):
    files = {'index.html': f'<h1>{label}</h1>', 'catalog.json': json.dumps({'label': label}),
             'app.js': f'// {label}', 'fonts/font.woff2': f'{label} font'}
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    provenance = {'build': {'source_sha': sha * 40, 'dirty': False},
                  'sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}}
    (root / 'build-provenance.json').write_text(json.dumps(provenance))
    return root


def test_root_and_staging_preserve_exact_source_bytes(tmp_path):
    production = bundle(tmp_path / 'production', 'PRODUCTION', 'a')
    staging = bundle(tmp_path / 'preview', 'STAGING', 'b')
    out = tmp_path / 'pages'
    assemble(production, staging, out)
    for source, destination in ((production, out), (staging, out / 'staging')):
        for path in source.rglob('*'):
            if path.is_file():
                assert path.read_bytes() == (destination / path.relative_to(source)).read_bytes()
    manifest = json.loads((out / 'pages-provenance.json').read_text())
    assert manifest['production']['build']['source_sha'] == 'a' * 40
    assert manifest['staging']['build']['source_sha'] == 'b' * 40


@pytest.mark.parametrize('invalid', ['dirty', 'checksum', 'extra-file', 'symlink', 'traversal', 'missing-index', 'missing-source-sha'])
def test_invalid_staging_never_changes_production_or_existing_publication(tmp_path, invalid):
    production = bundle(tmp_path / 'production', 'PRODUCTION', 'a')
    staging = bundle(tmp_path / 'preview', 'STAGING', 'b')
    record = staging / 'build-provenance.json'
    provenance = json.loads(record.read_text())
    if invalid == 'dirty':
        provenance['build']['dirty'] = True
    elif invalid == 'checksum':
        (staging / 'app.js').write_text('tampered')
    elif invalid == 'extra-file':
        (staging / 'unexpected.txt').write_text('stale')
    elif invalid == 'symlink':
        (staging / 'outside-link').symlink_to(production / 'index.html')
    elif invalid == 'traversal':
        provenance['sha256']['../escape'] = 'a' * 64
    elif invalid == 'missing-index':
        provenance['sha256'].pop('index.html')
    elif invalid == 'missing-source-sha':
        provenance['build']['source_sha'] = None
    record.write_text(json.dumps(provenance))
    before = (production / 'index.html').read_bytes()
    output = tmp_path / 'pages'
    with pytest.raises(ValueError):
        assemble(production, staging, output)
    assert not output.exists()
    assert (production / 'index.html').read_bytes() == before


def test_stale_output_is_not_silently_reused(tmp_path):
    production = bundle(tmp_path / 'production', 'PRODUCTION', 'a')
    staging = bundle(tmp_path / 'preview', 'STAGING', 'b')
    output = tmp_path / 'pages'
    output.mkdir()
    (output / 'previous-file').write_text('keep')
    with pytest.raises(ValueError, match='fresh or empty'):
        assemble(production, staging, output)
    assert (output / 'previous-file').read_text() == 'keep'


def test_output_cannot_overlap_input(tmp_path):
    production = bundle(tmp_path / 'production', 'PRODUCTION', 'a')
    staging = bundle(tmp_path / 'preview', 'STAGING', 'b')
    with pytest.raises(ValueError, match='overlap'):
        assemble(production, staging, production / 'staging')
