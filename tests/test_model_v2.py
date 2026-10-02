"""Tests for model.schema.json v1 and v2 validation."""

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((REPO / "schema" / "model.schema.json").read_text())


def validation_errors(document: dict) -> list[str]:
    """Return a list of validation error messages for a model document."""
    validator = Draft202012Validator(SCHEMA)
    return [error.message for error in validator.iter_errors(document)]


class ModelV1Tests(unittest.TestCase):
    """Ensure v1 model.yaml documents still validate after the schema update."""

    def test_minimal_v1_model_validates(self):
        model = {
            "schema_version": 1,
            "model_id": "test-model",
            "name": "Test Model",
            "family": "Test",
            "quantizations": [{"name": "FP16"}],
        }
        errors = validation_errors(model)
        self.assertFalse(errors, f"Valid v1 model should pass: {errors}")

    def test_v1_model_with_optional_fields_validates(self):
        model = {
            "schema_version": 1,
            "model_id": "test-model",
            "name": "Test Model",
            "family": "Test",
            "huggingface_id": "org/test-model",
            "parameters": "7B",
            "license": "Apache-2.0",
            "quantizations": [{"name": "FP16"}],
        }
        errors = validation_errors(model)
        self.assertFalse(errors, f"V1 with optional fields should pass: {errors}")

    def test_v1_model_does_not_require_v2_fields(self):
        """A v1 model without model_type, architectures, and source must validate."""
        model = {
            "schema_version": 1,
            "model_id": "test-model",
            "name": "Test Model",
            "family": "Test",
            "quantizations": [{"name": "FP16"}],
        }
        errors = validation_errors(model)
        self.assertFalse(errors, f"V1 must not require v2 fields: {errors}")


class ModelV2Tests(unittest.TestCase):
    """Test v2 model.yaml validation."""

    def _make_v2(self, **overrides) -> dict:
        """Return a valid v2 model document, optionally overriding fields."""
        base = {
            "schema_version": 2,
            "model_id": "test-model",
            "name": "Test Model",
            "family": "Test",
            "huggingface_id": "org/test-model",
            "source": "huggingface",
            "model_type": "llama",
            "architectures": ["LlamaForCausalLM"],
            "quantizations": [{"name": "FP16"}],
        }
        base.update(overrides)
        return base

    def test_complete_v2_model_validates(self):
        errors = validation_errors(self._make_v2())
        self.assertFalse(errors, f"Complete v2 model should validate: {errors}")

    def test_v2_missing_model_type_fails(self):
        model = self._make_v2()
        del model["model_type"]
        errors = validation_errors(model)
        self.assertTrue(errors, "V2 missing model_type should fail")
        self.assertTrue(
            any("model_type" in e for e in errors),
            f"Error should mention model_type: {errors}",
        )

    def test_v2_missing_architectures_fails(self):
        model = self._make_v2()
        del model["architectures"]
        errors = validation_errors(model)
        self.assertTrue(errors, "V2 missing architectures should fail")
        self.assertTrue(
            any("architectures" in e for e in errors),
            f"Error should mention architectures: {errors}",
        )

    def test_v2_missing_source_fails(self):
        model = self._make_v2()
        del model["source"]
        errors = validation_errors(model)
        self.assertTrue(errors, "V2 missing source should fail")
        self.assertTrue(
            any("source" in e for e in errors),
            f"Error should mention source: {errors}",
        )

    def test_v2_missing_huggingface_id_fails(self):
        model = self._make_v2()
        del model["huggingface_id"]
        errors = validation_errors(model)
        self.assertTrue(errors, "V2 missing huggingface_id should fail")
        self.assertTrue(
            any("huggingface_id" in e for e in errors),
            f"Error should mention huggingface_id: {errors}",
        )

    def test_v2_source_huggingface_validates(self):
        errors = validation_errors(self._make_v2(source="huggingface"))
        self.assertFalse(errors, f"source: huggingface should validate: {errors}")

    def test_v2_source_manual_validates(self):
        errors = validation_errors(self._make_v2(source="manual"))
        self.assertFalse(errors, f"source: manual should validate: {errors}")

    def test_v2_source_invalid_value_fails(self):
        errors = validation_errors(self._make_v2(source="custom"))
        self.assertTrue(errors, "Invalid source value should fail")

    def test_v2_with_quantization_huggingface_id_validates(self):
        model = self._make_v2(
            quantizations=[
                {"name": "FP8 dynamic", "huggingface_id": "org/test-model-FP8"},
            ],
        )
        errors = validation_errors(model)
        self.assertFalse(
            errors,
            f"quantizations[].huggingface_id should validate: {errors}",
        )

    def test_v2_with_multiple_architectures_validates(self):
        model = self._make_v2(
            architectures=["LlamaForCausalLM", "LlamaForSequenceClassification"],
        )
        errors = validation_errors(model)
        self.assertFalse(errors, f"Multiple architectures should validate: {errors}")

    def test_v2_empty_architectures_fails(self):
        errors = validation_errors(self._make_v2(architectures=[]))
        self.assertTrue(errors, "Empty architectures array should fail")

    def test_v2_with_all_optional_fields_validates(self):
        model = self._make_v2(
            parameters="26B",
            license="gemma",
            access={"note": "Requires agreement to terms."},
        )
        errors = validation_errors(model)
        self.assertFalse(errors, f"V2 with all optional fields should validate: {errors}")


class SchemaVersionTests(unittest.TestCase):
    """Test schema_version enforcement across both versions."""

    def test_invalid_schema_version_fails(self):
        model = {
            "schema_version": 3,
            "model_id": "test-model",
            "name": "Test Model",
            "family": "Test",
            "quantizations": [{"name": "FP16"}],
        }
        errors = validation_errors(model)
        self.assertTrue(errors, "schema_version 3 should be rejected")

    def test_schema_version_0_fails(self):
        model = {
            "schema_version": 0,
            "model_id": "test-model",
            "name": "Test Model",
            "family": "Test",
            "quantizations": [{"name": "FP16"}],
        }
        errors = validation_errors(model)
        self.assertTrue(errors, "schema_version 0 should be rejected")


if __name__ == "__main__":
    unittest.main()
