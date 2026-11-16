"""Run pinned Forge and Caliper flows natively on Apple Silicon without a cluster."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from bootstrap_macos import install_runtime
from prepare_contributor import MACOS_RUNNER_MODULES


with tempfile.TemporaryDirectory(prefix='contributor-macos-smoke-') as temporary:
    root = Path(temporary).resolve()
    runner = root / 'package/runner'
    tools = runner / 'tools'
    tools.mkdir(parents=True)
    shutil.copyfile(ROOT / 'benchmarks/runner/requirements.lock', runner / 'requirements.lock')
    for module in MACOS_RUNNER_MODULES:
        shutil.copyfile(ROOT / 'tools' / module, tools / module)

    work = root / 'runtime'
    python, forge, oc = install_runtime(work, runner)
    assert Path(python).is_symlink(), 'native macOS venv must link the base interpreter'
    home = root / 'home'
    home.mkdir(mode=0o700)
    environment = {
        'FORGE_ROOT': str(forge),
        'HOME': str(home),
        'PATH': os.pathsep.join((str(python.parent), str(forge / 'bin'), str(oc), os.environ.get('PATH', ''))),
        'PYTHONNOUSERSITE': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': os.pathsep.join((str(tools), str(forge))),
        'TMPDIR': str(root),
        'TOOLS_ROOT': str(tools),
    }
    for harness in ('guidellm', 'aiperf'):
        subprocess.run([str(python), str(ROOT / 'tests/contributor_container_smoke.py')],
                       check=True, env={**environment, 'SMOKE_HARNESS': harness})
    subprocess.run([str(python), str(ROOT / 'tests/contributor_guidellm_cleanup_smoke.py')],
                   check=True, env=environment)

print('PASS: native macOS bootstrap, Forge/Caliper smokes, and cleanup')
