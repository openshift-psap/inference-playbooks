import json
from pathlib import Path
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((REPO / "schema" / "model.schema.json").read_text())
V = Draft202012Validator(SCHEMA)

BASE = {"schema_version": 1, "model_id": "m1", "name": "M", "family": "f",
        "quantizations": [{"name": "FP8"}]}

def test_model_without_presentation_still_valid():
    assert list(V.iter_errors(BASE)) == []

def test_model_with_presentation_valid():
    doc = {**BASE, "presentation": {"icon_bg": "#4B2E83", "icon_letter": "G",
                                    "provider": "zai-org", "tags": ["MoE", "FP8"]}}
    assert list(V.iter_errors(doc)) == []

def test_presentation_tags_must_be_strings():
    doc = {**BASE, "presentation": {"tags": [1, 2]}}
    assert list(V.iter_errors(doc)) != []
