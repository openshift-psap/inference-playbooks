"""Run pinned Forge and Caliper flows natively on Apple Silicon without a cluster."""
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

from bootstrap_macos import install_runtime
from contributor_fixtures import HARNESS_IMAGES, SMOKE_WORKLOADS, build_source_repo, commit
from prepare_contributor import MACOS_RUNNER_MODULES, prepare

RECIPE = 'models/test/recipes/test-recipe/recipe.yaml'
SMOKE = ROOT / 'tests/contributor_container_smoke.py'
# Native packaging verifies these files against the tested revision before use.
NATIVE_RUNNER_SOURCES = ('tools/prepare_contributor.py', 'tools/bootstrap_macos.py',
                         'benchmarks/runner/requirements.lock',
                         *(f'tools/{name}' for name in MACOS_RUNNER_MODULES))


def commit_native_runner_sources(repo: Path):
    for name in NATIVE_RUNNER_SOURCES:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    commit(repo)


def smoke_command(python, script, package: Path, output: Path):
    """The shared smoke expects a prepared package; native mode only changes the runtime."""
    return [str(python), str(script),
            '--request', str(package / 'request.json'),
            '--inputs', str(package / 'playbooks'),
            '--output', str(output)]


def main():
    if platform.machine() != 'arm64':
        raise SystemExit('native macOS smoke requires Apple Silicon')
    with tempfile.TemporaryDirectory(prefix='contributor-macos-smoke-') as temporary:
        root = Path(temporary).resolve()
        repo, _, _ = build_source_repo(root / 'fixture')
        commit_native_runner_sources(repo)

        packages = {}
        for workload in SMOKE_WORKLOADS:
            package = root / 'packages' / workload
            prepare(repo, RECIPE, 'vllm', 'v0.24.0', workload, None,
                    HARNESS_IMAGES[workload], package, runner_mode='macos-arm64')
            packages[workload] = package

        work = root / 'runtime'
        python, forge, oc = install_runtime(work, packages[SMOKE_WORKLOADS[0]] / 'runner')
        assert Path(python).is_symlink(), 'native macOS venv must link the base interpreter'
        tools = packages[SMOKE_WORKLOADS[0]] / 'runner/tools'
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
        for workload in SMOKE_WORKLOADS:
            output = root / 'outputs' / workload
            subprocess.run(smoke_command(python, SMOKE, packages[workload], output),
                           check=True, env=environment)
            archive = output / 'evidence.tar.gz'
            assert archive.is_file(), 'native smoke did not produce a portable bundle'
            assert (output / 'evidence.sha256').read_text().strip().startswith('sha256:')
        subprocess.run([str(python), str(ROOT / 'tests/contributor_guidellm_cleanup_smoke.py')],
                       check=True, env=environment)

    print('PASS: native macOS bootstrap, Forge/Caliper smokes, and cleanup')


if __name__ == '__main__':
    main()
