import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[1]

# Ensure tools/ is importable.
import sys
sys.path.insert(0, str(REPO / "tools"))

from constraints import (
    evaluate_constraints,
    match_scope,
    validate_recipe_against_constraints,
)


class ScopeMatchTests(unittest.TestCase):
    """Tests for match_scope."""

    def test_scope_match_empty(self):
        """Empty scope matches everything."""
        context = {
            "model_type": "llama",
            "platform": {"stack": "rhoai", "version": "3.5"},
        }
        self.assertTrue(match_scope({}, context))

    def test_scope_match_platform(self):
        """Scope with platform.stack matches correct platform."""
        scope = {"platform": {"stack": "rhoai"}}
        context = {"platform": {"stack": "rhoai", "version": "3.5"}}
        self.assertTrue(match_scope(scope, context))

    def test_scope_match_platform_version(self):
        """Scope with platform+version only matches exact version."""
        scope = {"platform": {"stack": "rhoai", "version": "3.5"}}
        context_match = {"platform": {"stack": "rhoai", "version": "3.5"}}
        context_no = {"platform": {"stack": "rhoai", "version": "3.6"}}
        self.assertTrue(match_scope(scope, context_match))
        self.assertFalse(match_scope(scope, context_no))

    def test_scope_match_model_type(self):
        """Scope with model_type matches."""
        scope = {"model_type": "glm"}
        context = {"model_type": "glm", "platform": {"stack": "rhoai"}}
        self.assertTrue(match_scope(scope, context))

    def test_scope_match_intersection(self):
        """Scope with multiple fields requires all to match."""
        scope = {"model_type": "glm", "platform": {"stack": "rhoai"}}
        context_both = {
            "model_type": "glm",
            "platform": {"stack": "rhoai", "version": "3.5"},
        }
        context_model_only = {
            "model_type": "glm",
            "platform": {"stack": "vllm", "version": "0.23.0"},
        }
        self.assertTrue(match_scope(scope, context_both))
        self.assertFalse(match_scope(scope, context_model_only))

    def test_scope_no_match(self):
        """Scope doesn't match when any specified field differs."""
        scope = {"platform": {"stack": "llm-d"}}
        context = {"platform": {"stack": "rhoai", "version": "3.5"}}
        self.assertFalse(match_scope(scope, context))

    def test_scope_match_parallelism(self):
        """Scope with parallelism mode matches."""
        scope = {"parallelism": {"mode": "pp"}}
        context = {"parallelism": {"mode": "pp", "pp": 2, "tp": 8}}
        self.assertTrue(match_scope(scope, context))

    def test_scope_no_match_missing_context_key(self):
        """Scope field that is absent in context does not match."""
        scope = {"model_type": "glm"}
        context = {"platform": {"stack": "rhoai"}}
        self.assertFalse(match_scope(scope, context))


class EvaluateConstraintsTests(unittest.TestCase):
    """Tests for evaluate_constraints."""

    def _constraints(self, layers):
        return {"schema_version": 1, "layers": layers}

    def test_remove_constraint_applied(self):
        """Flag in remove list appears in the removes output."""
        constraints = self._constraints([
            {
                "scope": {"platform": {"stack": "rhoai", "version": "3.5"}},
                "remove": [
                    {"flag": "--num-scheduler-steps", "reason": "Not supported."},
                ],
            }
        ])
        context = {"platform": {"stack": "rhoai", "version": "3.5"}}
        removes, forces, errors = evaluate_constraints(constraints, context)
        self.assertEqual(len(removes), 1)
        self.assertEqual(removes[0]["flag"], "--num-scheduler-steps")
        self.assertEqual(errors, [])

    def test_force_constraint_applied(self):
        """Force entry appears in forces output."""
        constraints = self._constraints([
            {
                "scope": {"platform": {"stack": "rhoai"}},
                "force": [
                    {
                        "flag": "--distributed-executor-backend",
                        "value": "mp",
                        "reason": "Required.",
                    },
                ],
            }
        ])
        context = {"platform": {"stack": "rhoai", "version": "3.5"}}
        removes, forces, errors = evaluate_constraints(constraints, context)
        self.assertEqual(len(forces), 1)
        self.assertEqual(forces[0]["flag"], "--distributed-executor-backend")
        self.assertEqual(forces[0]["value"], "mp")
        self.assertEqual(errors, [])

    def test_force_force_conflict(self):
        """Two force on same flag with different values produces error."""
        constraints = self._constraints([
            {
                "scope": {},
                "force": [
                    {"flag": "--dtype", "value": "float16", "reason": "A"},
                ],
            },
            {
                "scope": {},
                "force": [
                    {"flag": "--dtype", "value": "bfloat16", "reason": "B"},
                ],
            },
        ])
        _, _, errors = evaluate_constraints(constraints, {})
        self.assertTrue(any("conflicting force" in e for e in errors))

    def test_remove_force_conflict(self):
        """Remove and force on same flag produces error."""
        constraints = self._constraints([
            {
                "scope": {},
                "remove": [
                    {"flag": "--some-flag", "reason": "Remove it."},
                ],
                "force": [
                    {"flag": "--some-flag", "value": "x", "reason": "Force it."},
                ],
            },
        ])
        _, _, errors = evaluate_constraints(constraints, {})
        self.assertTrue(any("both remove and force" in e for e in errors))

    def test_layers_stack(self):
        """Multiple matching layers accumulate their constraints."""
        constraints = self._constraints([
            {
                "scope": {"platform": {"stack": "rhoai"}},
                "remove": [
                    {"flag": "--flag-a", "reason": "reason A"},
                ],
            },
            {
                "scope": {"platform": {"stack": "rhoai"}},
                "remove": [
                    {"flag": "--flag-b", "reason": "reason B"},
                ],
            },
        ])
        context = {"platform": {"stack": "rhoai", "version": "3.5"}}
        removes, _, errors = evaluate_constraints(constraints, context)
        flags = {r["flag"] for r in removes}
        self.assertEqual(flags, {"--flag-a", "--flag-b"})
        self.assertEqual(errors, [])

    def test_when_narrows_scope(self):
        """'when' further restricts which recipes a constraint applies to."""
        constraints = self._constraints([
            {
                "scope": {"model_type": "glm"},
                "remove": [
                    {
                        "flag": "--speculative-config",
                        "when": {"platform": {"stack": "rhoai"}},
                        "reason": "Not supported on RHOAI for GLM.",
                    },
                ],
            }
        ])
        context_rhoai = {
            "model_type": "glm",
            "platform": {"stack": "rhoai", "version": "3.5"},
        }
        context_vllm = {
            "model_type": "glm",
            "platform": {"stack": "vllm", "version": "v0.24.0"},
        }
        removes_rhoai, _, _ = evaluate_constraints(constraints, context_rhoai)
        removes_vllm, _, _ = evaluate_constraints(constraints, context_vllm)
        self.assertEqual(len(removes_rhoai), 1)
        self.assertEqual(len(removes_vllm), 0)

    def test_no_constraints_match(self):
        """Recipe with no matching constraints passes clean."""
        constraints = self._constraints([
            {
                "scope": {"platform": {"stack": "llm-d"}},
                "remove": [
                    {"flag": "--flag-x", "reason": "llm-d only"},
                ],
            }
        ])
        context = {"platform": {"stack": "rhoai", "version": "3.5"}}
        removes, forces, errors = evaluate_constraints(constraints, context)
        self.assertEqual(removes, [])
        self.assertEqual(forces, [])
        self.assertEqual(errors, [])


class ValidateRecipeConstraintsTests(unittest.TestCase):
    """Tests for validate_recipe_against_constraints."""

    def _constraints(self, layers):
        return {"schema_version": 1, "layers": layers}

    def _v4_args(self, *flag_value_pairs):
        """Convert ('--flag', 'val') or ('--flag',) pairs to v4 arg objects."""
        result = []
        for item in flag_value_pairs:
            if isinstance(item, tuple):
                flag = item[0]
                entry = {"flag": flag, "required": True, "why": "test"}
                if len(item) > 1:
                    entry["value"] = item[1]
                result.append(entry)
            elif isinstance(item, str):
                result.append({"flag": item, "required": True, "why": "test"})
        return result

    def _recipe(self, args=None, overrides=None, parallelism=None):
        recipe = {
            "schema_version": 4,
            "recipe_id": "test-recipe",
            "model_id": "glm-4",
            "platform": {"stack": "rhoai", "version": "3.5"},
            "deployment": {"scope": "single-node"},
        }
        if args is not None:
            recipe["serving"] = {
                "image": "vllm/vllm-openai:v0.24.0",
                "model": "test/model",
                "parallelism": {"mode": "tp", "tp": 1},
                "args": args,
            }
        if overrides is not None:
            recipe["serving"]["constraint_overrides"] = overrides
        if parallelism is not None:
            recipe["serving"]["parallelism"] = parallelism
        return recipe

    def _model(self, family="glm"):
        return {"model_id": "glm-4", "family": family}

    def test_remove_constraint_blocks_flag(self):
        """Contributor arg that matches a remove constraint produces error."""
        constraints = self._constraints([
            {
                "scope": {"platform": {"stack": "rhoai", "version": "3.5"}},
                "remove": [
                    {"flag": "--num-scheduler-steps", "reason": "Not supported."},
                ],
            }
        ])
        recipe = self._recipe(args=self._v4_args(("--num-scheduler-steps", "4")))
        errors = validate_recipe_against_constraints(
            constraints, recipe, self._model()
        )
        self.assertTrue(any("--num-scheduler-steps" in e for e in errors))

    def test_remove_constraint_with_override(self):
        """constraint_overrides exempts flag from remove constraint."""
        constraints = self._constraints([
            {
                "scope": {"platform": {"stack": "rhoai", "version": "3.5"}},
                "remove": [
                    {"flag": "--num-scheduler-steps", "reason": "Not supported."},
                ],
            }
        ])
        recipe = self._recipe(
            args=self._v4_args(("--num-scheduler-steps", "4")),
            overrides=[
                {
                    "flag": "--num-scheduler-steps",
                    "allow": True,
                    "reason": "Confirmed working with custom runtime image.",
                }
            ],
        )
        errors = validate_recipe_against_constraints(
            constraints, recipe, self._model()
        )
        self.assertFalse(any("--num-scheduler-steps" in e for e in errors))

    def test_force_constraint_missing_flag(self):
        """Force adds flag automatically, no error when not present."""
        constraints = self._constraints([
            {
                "scope": {"platform": {"stack": "rhoai"}},
                "force": [
                    {
                        "flag": "--distributed-executor-backend",
                        "value": "mp",
                        "reason": "Required.",
                    },
                ],
            }
        ])
        recipe = self._recipe(args=self._v4_args())
        errors = validate_recipe_against_constraints(
            constraints, recipe, self._model()
        )
        # No error: force with missing flag is informational, not a
        # validation failure.
        self.assertFalse(
            any("--distributed-executor-backend" in e for e in errors)
        )

    def test_force_constraint_wrong_value(self):
        """Contributor value that differs from forced value produces error."""
        constraints = self._constraints([
            {
                "scope": {"platform": {"stack": "rhoai"}},
                "force": [
                    {
                        "flag": "--distributed-executor-backend",
                        "value": "mp",
                        "reason": "Required.",
                    },
                ],
            }
        ])
        recipe = self._recipe(
            args=self._v4_args(("--distributed-executor-backend", "ray"))
        )
        errors = validate_recipe_against_constraints(
            constraints, recipe, self._model()
        )
        self.assertTrue(
            any("--distributed-executor-backend" in e and "mp" in e for e in errors)
        )

    def test_override_requires_reason(self):
        """constraint_overrides without a reason produces error."""
        constraints = self._constraints([
            {
                "scope": {"platform": {"stack": "rhoai", "version": "3.5"}},
                "remove": [
                    {"flag": "--num-scheduler-steps", "reason": "Not supported."},
                ],
            }
        ])
        recipe = self._recipe(
            args=self._v4_args(("--num-scheduler-steps", "4")),
            overrides=[
                {"flag": "--num-scheduler-steps", "allow": True, "reason": ""}
            ],
        )
        errors = validate_recipe_against_constraints(
            constraints, recipe, self._model()
        )
        self.assertTrue(any("reason" in e for e in errors))

    def test_no_serving_args_passes(self):
        """Recipe with no serving args passes against any constraints."""
        constraints = self._constraints([
            {
                "scope": {},
                "remove": [
                    {"flag": "--flag-x", "reason": "Nope."},
                ],
            }
        ])
        recipe = self._recipe()
        errors = validate_recipe_against_constraints(
            constraints, recipe, self._model()
        )
        self.assertEqual(errors, [])


class SchemaValidationTests(unittest.TestCase):
    """Tests for the flag-constraints schema itself."""

    def test_constraint_schema_validates(self):
        """flag-constraints.yaml validates against its JSON schema."""
        import yaml as pyyaml

        schema_path = REPO / "schema" / "flag-constraints.schema.json"
        data_path = REPO / "schema" / "flag-constraints.yaml"
        schema = json.loads(schema_path.read_text())
        data = pyyaml.safe_load(data_path.read_text())
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(data))
        self.assertEqual(errors, [], [str(e) for e in errors])

    def test_schema_rejects_bad_flag_pattern(self):
        """Schema rejects flags not starting with --."""
        schema = json.loads(
            (REPO / "schema" / "flag-constraints.schema.json").read_text()
        )
        bad_data = {
            "schema_version": 1,
            "layers": [
                {
                    "scope": {},
                    "remove": [
                        {"flag": "no-dashes", "reason": "bad"},
                    ],
                }
            ],
        }
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(bad_data))
        self.assertTrue(len(errors) > 0)

    def test_schema_rejects_missing_scope(self):
        """Schema requires scope in each layer."""
        schema = json.loads(
            (REPO / "schema" / "flag-constraints.schema.json").read_text()
        )
        bad_data = {
            "schema_version": 1,
            "layers": [
                {
                    "remove": [
                        {"flag": "--some-flag", "reason": "bad"},
                    ],
                }
            ],
        }
        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(bad_data))
        self.assertTrue(len(errors) > 0)


if __name__ == "__main__":
    unittest.main()
