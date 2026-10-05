import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_repository_validates():
    r = subprocess.run(
        [sys.executable, "tools/validate.py", "--current"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


def test_glm52_model_present():
    assert (REPO / "models/glm-5.2/model.yaml").is_file()
