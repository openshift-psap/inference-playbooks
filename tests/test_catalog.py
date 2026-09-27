# tests/test_catalog.py
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from catalog import build_catalog

REPO = Path(__file__).resolve().parents[1]


def _glm(cat):
    return next(m for m in cat["models"] if m["id"] == "glm-5.2")


def test_catalog_has_glm52_with_presentation():
    cat = build_catalog(REPO)
    m = _glm(cat)
    assert m["name"] == "GLM-5.2-FP8"
    assert m["icon_letter"] == "G"
    assert m["validated"] is True
    assert "MoE" in m["tags"]


def test_h200_single_throughput_view_renders_flags():
    m = _glm(build_catalog(REPO))
    view = m["views"]["rhoai|nvidia-h200-x8|single|guidellm-8k1k"]
    assert view["blocked"] is False
    flags = [r["flag"] for r in view["configure"]["flags_rows"]]
    assert "--tensor-parallel-size" in flags
    assert view["configure"]["manifest"]["body"].startswith("#") or "LLMInferenceService" in view["configure"]["manifest"]["body"]


def test_guidellm_publishes_only_the_sourced_c16_row():
    # Only C=16 traces to a committed result run; C=1/C=4 must not be published.
    m = _glm(build_catalog(REPO))
    view = m["views"]["rhoai|nvidia-h200-x8|single|guidellm-8k1k"]
    loads = [r["load"] for r in view["benchmark"]["rows"]]
    assert loads == ["C=16 · 189 req in 300 s"]
    assert not any("C=1 " in l or "C=4 " in l for l in loads)


def test_mi355x_hardware_label_is_correct():
    # amd-mi355x-x8 is an AMD MI355X, not an H100.
    m = _glm(build_catalog(REPO))
    hw = {h["id"]: h["label"] for h in m["selectors"]["hardware"]}
    assert hw["amd-mi355x-x8"] == "MI355X"


def test_missing_combo_is_blocked_with_reason():
    m = _glm(build_catalog(REPO))
    # B200 multi-node has no seeded recipe
    key = "rhoai|nvidia-b200-x8|multi|guidellm-8k1k"
    view = m["views"].get(key)
    assert view is not None and view["blocked"] is True
    assert view["reason"]


def test_model_without_presentation_gets_defaults(tmp_path):
    # minimal repo with one bare model, no presentation
    (tmp_path / "schema").mkdir()
    (tmp_path / "hardware-profiles").mkdir()
    for s in (REPO / "schema").glob("*.json"):
        (tmp_path / "schema" / s.name).write_text(s.read_text())
    mdir = tmp_path / "models" / "bare"
    mdir.mkdir(parents=True)
    (mdir / "model.yaml").write_text(
        "schema_version: 1\nmodel_id: bare\nname: Bare Model\nfamily: X\n"
        "quantizations:\n  - {name: FP8}\n"
    )
    cat = build_catalog(tmp_path)
    m = next(x for x in cat["models"] if x["id"] == "bare")
    assert m["icon_letter"] == "B"  # first letter of name
    assert m["icon_bg"].startswith("#")  # deterministic default color
    assert m["tags"] == []


def test_check_detects_drift(tmp_path):
    stale = tmp_path / "catalog.json"
    stale.write_text("{}\n")
    r = subprocess.run(
        [sys.executable, "tools/catalog.py", "--check", "--out", str(stale)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0


def test_matches_golden():
    golden = json.loads((REPO / "tests/fixtures/catalog.golden.json").read_text())
    assert build_catalog(REPO) == golden
