"""Tests for Recipe v4 schema and validator support."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from validate import resolve_role_args, validate_v4_recipe  # noqa: E402


def load_schema():
    return json.loads((REPO / "schema" / "recipe.schema.json").read_text())


def make_validator():
    schemas = [json.loads(p.read_text()) for p in sorted((REPO / "schema").glob("*.schema.json"))]
    registry = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in schemas
    )
    recipe_schema = next(s for s in schemas if s["$id"].endswith("/recipe.schema.json"))
    return Draft202012Validator(recipe_schema, registry=registry)


class RecipeV4SchemaTests(unittest.TestCase):
    """Test JSON Schema validation for v4 serving block."""

    def setUp(self):
        self.schema = load_schema()
        self.validator = make_validator()

        self.v4_serving = {
            "image": "vllm/vllm-openai:v0.24.0",
            "model": "RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic",
            "parallelism": {"mode": "tp", "tp": 1},
            "args": [
                {
                    "flag": "--enable-auto-tool-choice",
                    "required": True,
                    "why": "Enables tool calling.",
                }
            ],
        }

        self.v4_recipe = {
            "schema_version": 4,
            "recipe_id": "gemma-4-tp1-tool-calling",
            "model_id": "gemma-4",
            "platforms": [{"stack": "vllm", "version": "v0.24.0", "overrides": "platforms/vllm-v0.24.0.yaml"}],
            "hardware_profile": "hardware-profiles/nvidia-h200-sxm-8x-nvlink-r1.yaml",
            "workload_profile": "guidellm-8k1k",
            "deployment_mode": "tp1-tool-calling",
            "optimization_intent": "single-GPU agentic serving",
            "maturity": "day-zero",
            "deployment": {"scope": "single-node"},
            "serving": self.v4_serving,
        }

    def errors_for(self, recipe):
        return list(self.validator.iter_errors(recipe))

    # --- v4 basic validation ---

    def test_v4_recipe_with_serving_validates(self):
        errors = self.errors_for(self.v4_recipe)
        self.assertFalse(errors, [e.message for e in errors])

    def test_v4_recipe_missing_serving_fails(self):
        recipe = {k: v for k, v in self.v4_recipe.items() if k != "serving"}
        errors = self.errors_for(recipe)
        self.assertTrue(errors)
        self.assertTrue(
            any("serving" in e.message for e in errors),
            f"Expected 'serving' in error messages: {[e.message for e in errors]}",
        )

    def test_v4_recipe_missing_platforms_fails(self):
        recipe = {k: v for k, v in self.v4_recipe.items() if k != "platforms"}
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    def test_v4_recipe_with_empty_serving_fails(self):
        recipe = {**self.v4_recipe, "serving": {}}
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    # --- v4 platforms ---

    def test_v4_multi_platform_validates(self):
        recipe = {**self.v4_recipe, "platforms": [
            {"stack": "vllm", "version": "v0.24.0", "overrides": "platforms/vllm-v0.24.0.yaml"},
            {"stack": "rhoai", "version": "3.5", "overrides": "platforms/rhoai-3.5.yaml"},
        ]}
        errors = self.errors_for(recipe)
        self.assertFalse(errors, [e.message for e in errors])

    def test_v4_platform_missing_overrides_fails(self):
        recipe = {**self.v4_recipe, "platforms": [
            {"stack": "vllm", "version": "v0.24.0"},
        ]}
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    def test_v4_empty_platforms_fails(self):
        recipe = {**self.v4_recipe, "platforms": []}
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    def test_v4_blocked_platform_with_reason_validates(self):
        recipe = {**self.v4_recipe, "platforms": [
            {"stack": "vllm", "version": "v0.24.0", "overrides": "platforms/vllm-v0.24.0.yaml"},
            {"stack": "rhoai", "version": "3.5", "overrides": "platforms/rhoai-3.5.yaml",
             "blocked": True, "reason": "RHOAI 3.5 lacks --enable-auto-tool-choice"},
        ]}
        errors = self.errors_for(recipe)
        self.assertFalse(errors, [e.message for e in errors])

    def test_v4_blocked_platform_without_reason_fails(self):
        recipe = {**self.v4_recipe, "platforms": [
            {"stack": "rhoai", "version": "3.5", "overrides": "platforms/rhoai-3.5.yaml",
             "blocked": True},
        ]}
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    # --- v4 serving.env ---

    def test_v4_recipe_with_env_value_validates(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {
            **self.v4_serving,
            "env": [{"name": "VLLM_ENGINE_ITERATION_TIMEOUT_S", "value": "120"}],
        }
        errors = self.errors_for(recipe)
        self.assertFalse(errors, [e.message for e in errors])

    def test_v4_recipe_with_env_value_from_validates(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {
            **self.v4_serving,
            "env": [
                {
                    "name": "HF_TOKEN",
                    "value_from": {
                        "secretKeyRef": {"name": "hf-secret", "key": "token"}
                    },
                }
            ],
        }
        errors = self.errors_for(recipe)
        self.assertFalse(errors, [e.message for e in errors])

    def test_v4_recipe_env_without_value_or_value_from_fails(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {
            **self.v4_serving,
            "env": [{"name": "MISSING_VALUE"}],
        }
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    # --- v4 serving.parallelism schema ---

    def test_v4_parallelism_unknown_mode_fails(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {
            **self.v4_serving,
            "parallelism": {"mode": "unknown", "tp": 1},
        }
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    def test_v4_parallelism_missing_tp_fails(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {
            **self.v4_serving,
            "parallelism": {"mode": "tp"},
        }
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    # --- v4 serving.args schema ---

    def test_v4_args_invalid_flag_pattern_fails(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {
            **self.v4_serving,
            "args": [{"flag": "no-dashes", "required": True, "why": "bad flag"}],
        }
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    def test_v4_args_missing_why_fails(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {
            **self.v4_serving,
            "args": [{"flag": "--some-flag", "required": True}],
        }
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    # --- v4 serving.port ---

    def test_v4_port_zero_fails(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {**self.v4_serving, "port": 0}
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    def test_v4_port_valid_validates(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {**self.v4_serving, "port": 8080}
        errors = self.errors_for(recipe)
        self.assertFalse(errors, [e.message for e in errors])

    # --- v4 serving.constraint_overrides ---

    def test_v4_constraint_override_validates(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {
            **self.v4_serving,
            "constraint_overrides": [
                {
                    "flag": "--some-flag",
                    "allow": True,
                    "reason": "Needed for this workload.",
                }
            ],
        }
        errors = self.errors_for(recipe)
        self.assertFalse(errors, [e.message for e in errors])

    # --- v4 serving.config_overrides ---

    def test_v4_config_overrides_validates(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {**self.v4_serving, "config_overrides": True}
        errors = self.errors_for(recipe)
        self.assertFalse(errors, [e.message for e in errors])

    # --- Pinned manifest ---

    def test_v4_pinned_manifest_with_reason_validates(self):
        recipe = {**self.v4_recipe}
        recipe["platforms"] = [{
            "stack": "llm-d", "version": "0.8",
            "overrides": "platforms/llm-d-0.8.yaml",
            "pinned_manifest": "manifests/llm-d-0.8/deployment.yaml",
            "pinned_reason": "llm-d template WIP",
        }]
        errors = self.errors_for(recipe)
        self.assertFalse(errors, [e.message for e in errors])

    def test_v4_pinned_manifest_without_reason_fails(self):
        recipe = {**self.v4_recipe}
        recipe["platforms"] = [{
            "stack": "llm-d", "version": "0.8",
            "overrides": "platforms/llm-d-0.8.yaml",
            "pinned_manifest": "manifests/llm-d-0.8/deployment.yaml",
        }]
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    def test_v4_pinned_manifest_bad_path_fails(self):
        recipe = {**self.v4_recipe}
        recipe["platforms"] = [{
            "stack": "llm-d", "version": "0.8",
            "overrides": "platforms/llm-d-0.8.yaml",
            "pinned_manifest": "../../../etc/passwd",
            "pinned_reason": "testing",
        }]
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    # --- v4 additional properties disallowed ---

    def test_v4_serving_extra_property_fails(self):
        recipe = {**self.v4_recipe}
        recipe["serving"] = {**self.v4_serving, "unknown_field": "value"}
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    # --- schema_version 5 fails ---

    def test_unsupported_schema_version_fails(self):
        recipe = {**self.v4_recipe, "schema_version": 5}
        errors = self.errors_for(recipe)
        self.assertTrue(errors)

    # --- Example file validates ---

    def test_example_v4_file_validates(self):
        import yaml

        example = REPO / "schema" / "examples" / "recipe-v4-example.yaml"
        with open(example) as fh:
            recipe = yaml.safe_load(fh)
        errors = self.errors_for(recipe)
        self.assertFalse(errors, [e.message for e in errors])


class RecipeV4ValidatorTests(unittest.TestCase):
    """Test validate_v4_recipe() semantic checks that go beyond JSON Schema."""

    def setUp(self):
        self.base_serving = {
            "image": "vllm/vllm-openai:v0.24.0",
            "model": "RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic",
            "parallelism": {"mode": "tp", "tp": 1},
            "args": [
                {
                    "flag": "--enable-auto-tool-choice",
                    "required": True,
                    "why": "Enables tool calling.",
                }
            ],
        }
        self.tmpdir = Path(tempfile.mkdtemp())
        self.recipe_dir = (
            self.tmpdir
            / "models"
            / "gemma-4"
            / "recipes"
            / "h200-tp1-tool-calling"
        )
        self.recipe_dir.mkdir(parents=True)
        self.recipe_path = self.recipe_dir / "recipe.yaml"
        platforms_dir = self.recipe_dir / "platforms"
        platforms_dir.mkdir()
        (platforms_dir / "vllm-v0.24.0.yaml").write_text("{}\n")

    def make_recipe(self, serving_overrides=None):
        serving = {**self.base_serving}
        if serving_overrides:
            serving.update(serving_overrides)
        return {
            "schema_version": 4,
            "recipe_id": "gemma-4-tp1-tool-calling",
            "model_id": "gemma-4",
            "platforms": [{"stack": "vllm", "version": "v0.24.0", "overrides": "platforms/vllm-v0.24.0.yaml"}],
            "hardware_profile": "hardware-profiles/h200.yaml",
            "workload_profile": "guidellm-8k1k",
            "deployment_mode": "tp1-tool-calling",
            "optimization_intent": "latency",
            "maturity": "day-zero",
            "deployment": {"scope": "single-node"},
            "serving": serving,
        }

    # --- Parallelism mode consistency ---

    def test_tp_mode_with_pp_greater_than_1_fails(self):
        recipe = self.make_recipe(
            {"parallelism": {"mode": "tp", "tp": 4, "pp": 2}}
        )
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("pp=2" in e for e in errors))

    def test_tp_mode_with_dp_greater_than_1_fails(self):
        recipe = self.make_recipe(
            {"parallelism": {"mode": "tp", "tp": 4, "dp": 2}}
        )
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("dp=2" in e for e in errors))

    def test_pp_mode_with_tp_greater_than_1_fails(self):
        recipe = self.make_recipe(
            {"parallelism": {"mode": "pp", "tp": 2, "pp": 4}}
        )
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("tp=2" in e for e in errors))

    def test_dp_mode_with_pp_greater_than_1_fails(self):
        recipe = self.make_recipe(
            {"parallelism": {"mode": "dp", "tp": 1, "dp": 4, "pp": 2}}
        )
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("pp=2" in e for e in errors))

    def test_tp_pp_mode_with_dp_greater_than_1_fails(self):
        recipe = self.make_recipe(
            {"parallelism": {"mode": "tp+pp", "tp": 4, "pp": 2, "dp": 2}}
        )
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("dp=2" in e for e in errors))

    def test_tp_dp_mode_with_pp_greater_than_1_fails(self):
        recipe = self.make_recipe(
            {"parallelism": {"mode": "tp+dp", "tp": 4, "pp": 2, "dp": 2}}
        )
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("pp=2" in e for e in errors))

    def test_valid_tp_mode_passes(self):
        recipe = self.make_recipe(
            {"parallelism": {"mode": "tp", "tp": 8}}
        )
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertFalse(errors, errors)

    def test_valid_tp_pp_mode_passes(self):
        recipe = self.make_recipe(
            {"parallelism": {"mode": "tp+pp", "tp": 4, "pp": 2}}
        )
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertFalse(errors, errors)

    # --- Parallelism flags in serving.args ---

    def test_tensor_parallel_size_in_args_fails(self):
        recipe = self.make_recipe({
            "args": [
                {"flag": "--tensor-parallel-size", "value": "8", "required": True, "why": "TP size."},
            ],
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("--tensor-parallel-size" in e for e in errors))

    def test_pipeline_parallel_size_in_args_fails(self):
        recipe = self.make_recipe({
            "args": [
                {"flag": "--pipeline-parallel-size", "value": "2", "required": True, "why": "PP."},
            ],
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("--pipeline-parallel-size" in e for e in errors))

    def test_data_parallel_size_in_args_fails(self):
        recipe = self.make_recipe({
            "args": [
                {"flag": "--data-parallel-size", "value": "2", "required": True, "why": "DP."},
            ],
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("--data-parallel-size" in e for e in errors))

    def test_num_scheduler_steps_in_args_fails(self):
        recipe = self.make_recipe({
            "args": [
                {"flag": "--num-scheduler-steps", "value": "4", "required": True, "why": "Chunked."},
            ],
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("--num-scheduler-steps" in e for e in errors))

    # --- Duplicate flags ---

    def test_duplicate_flags_in_args_fails(self):
        recipe = self.make_recipe({
            "args": [
                {"flag": "--max-model-len", "value": "16384", "required": True, "why": "Memory."},
                {"flag": "--max-model-len", "value": "8192", "required": True, "why": "Different."},
            ],
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("duplicate flag" in e for e in errors))

    def test_no_duplicate_flags_passes(self):
        recipe = self.make_recipe({
            "args": [
                {"flag": "--max-model-len", "value": "16384", "required": True, "why": "Memory."},
                {"flag": "--enable-auto-tool-choice", "required": True, "why": "Tool calling."},
            ],
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertFalse(errors, errors)

    # --- config_overrides ---

    def test_config_overrides_without_kustomization_fails(self):
        recipe = self.make_recipe({"config_overrides": True})
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("kustomization.yaml" in e for e in errors))

    def test_config_overrides_with_kustomization_passes(self):
        config_dir = self.recipe_dir / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "kustomization.yaml").write_text("resources: []\n")
        recipe = self.make_recipe({"config_overrides": True})
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertFalse(errors, errors)

    # --- router values ---

    def test_router_values_missing_file_fails(self):
        recipe = self.make_recipe({
            "router": {"strategy": "prefix", "values": "config/router-values.yaml"},
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("router.values" in e for e in errors))

    def test_router_values_existing_file_passes(self):
        config_dir = self.recipe_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "router-values.yaml").write_text("key: val\n")
        recipe = self.make_recipe({
            "router": {"strategy": "prefix", "values": "config/router-values.yaml"},
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertFalse(errors, errors)

    # --- Platform override validation ---

    def test_override_file_missing_fails(self):
        recipe = self.make_recipe()
        recipe["platforms"] = [{"stack": "rhoai", "version": "3.5", "overrides": "platforms/rhoai-3.5.yaml"}]
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(any("override file does not exist" in e for e in errors))

    def test_override_file_valid_passes(self):
        platforms_dir = self.recipe_dir / "platforms"
        platforms_dir.mkdir(parents=True, exist_ok=True)
        (platforms_dir / "vllm-v0.24.0.yaml").write_text("{}\n")

        schema_dir = self.tmpdir / "schema"
        schema_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        for f in (REPO / "schema").glob("*.schema.json"):
            shutil.copy2(f, schema_dir)

        recipe = self.make_recipe()
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertFalse(errors, errors)


class RecipeV4RoleBlockTests(unittest.TestCase):
    """Test role block (decode/prefill) validation and arg resolution."""

    def setUp(self):
        self.schema = load_schema()
        self.validator = make_validator()

        self.base_serving = {
            "image": "vllm/vllm-openai:v0.24.0",
            "model": "RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic",
            "parallelism": {"mode": "tp", "tp": 8},
            "args": [
                {"flag": "--max-model-len", "value": "16384", "required": True, "why": "Memory limit."},
                {"flag": "--enable-prefix-caching", "required": True, "why": "Prefix caching."},
                {"flag": "--gpu-memory-utilization", "value": "0.95", "required": True, "why": "Max GPU use."},
            ],
        }
        self.tmpdir = Path(tempfile.mkdtemp())
        self.recipe_dir = (
            self.tmpdir
            / "models"
            / "gemma-4"
            / "recipes"
            / "h200-tp8-aggregated"
        )
        self.recipe_dir.mkdir(parents=True)
        self.recipe_path = self.recipe_dir / "recipe.yaml"
        platforms_dir = self.recipe_dir / "platforms"
        platforms_dir.mkdir()
        (platforms_dir / "vllm-v0.24.0.yaml").write_text("{}\n")

    def errors_for(self, recipe):
        return list(self.validator.iter_errors(recipe))

    def make_recipe(self, serving_overrides=None):
        serving = {**self.base_serving}
        if serving_overrides:
            serving.update(serving_overrides)
        return {
            "schema_version": 4,
            "recipe_id": "gemma-4-tp8-pd",
            "model_id": "gemma-4",
            "platforms": [{"stack": "vllm", "version": "v0.24.0", "overrides": "platforms/vllm-v0.24.0.yaml"}],
            "hardware_profile": "hardware-profiles/h200.yaml",
            "workload_profile": "guidellm-8k1k",
            "deployment_mode": "tp8-aggregated",
            "optimization_intent": "throughput",
            "maturity": "day-zero",
            "deployment": {"scope": "single-node"},
            "serving": serving,
        }

    # --- Schema validation of role blocks ---

    def test_decode_args_validates(self):
        recipe = self.make_recipe({
            "decode": {
                "args": [
                    {"flag": "--decode-only-flag", "required": True, "why": "Decode specific."},
                ],
            },
        })
        errors = self.errors_for(recipe)
        self.assertFalse(errors, [e.message for e in errors])

    def test_prefill_args_validates(self):
        recipe = self.make_recipe({
            "router": {"strategy": "prefix"},
            "prefill": {
                "args": [
                    {"flag": "--prefill-only-flag", "required": True, "why": "Prefill specific."},
                ],
            },
        })
        errors = self.errors_for(recipe)
        self.assertFalse(errors, [e.message for e in errors])

    # --- Exclude validation ---

    def test_exclude_valid_flag_validates(self):
        recipe = self.make_recipe({
            "decode": {
                "exclude": [
                    {"flag": "--enable-prefix-caching", "reason": "Not needed for decode role."},
                ],
            },
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertFalse(errors, errors)

    def test_exclude_dead_flag_fails(self):
        recipe = self.make_recipe({
            "decode": {
                "exclude": [
                    {"flag": "--nonexistent-flag", "reason": "Does not exist."},
                ],
            },
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("--nonexistent-flag" in e and "does not exist" in e for e in errors))

    # --- Flag overlap ---

    def test_duplicate_flag_in_serving_and_role_args_fails(self):
        recipe = self.make_recipe({
            "decode": {
                "args": [
                    {"flag": "--max-model-len", "value": "8192", "required": True, "why": "Override."},
                ],
            },
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("--max-model-len" in e and "without an exclude" in e for e in errors))

    def test_exclude_then_readd_in_role_args_validates(self):
        recipe = self.make_recipe({
            "decode": {
                "exclude": [
                    {"flag": "--max-model-len", "reason": "Override with decode-specific value."},
                ],
                "args": [
                    {"flag": "--max-model-len", "value": "8192", "required": True, "why": "Smaller for decode."},
                ],
            },
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertFalse(errors, errors)

    # --- Leader/worker conflicts ---

    def test_leader_worker_same_flag_fails(self):
        recipe = self.make_recipe({
            "decode": {
                "leader_args": [
                    {"flag": "--some-flag", "value": "leader-val", "required": True, "why": "Leader."},
                ],
                "worker_args": [
                    {"flag": "--some-flag", "value": "worker-val", "required": True, "why": "Worker."},
                ],
            },
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("--some-flag" in e and "leader_args" in e and "worker_args" in e for e in errors))

    def test_duplicate_flag_in_role_args_fails(self):
        recipe = self.make_recipe({
            "decode": {
                "args": [
                    {"flag": "--decode-flag", "required": True, "why": "First."},
                    {"flag": "--decode-flag", "required": True, "why": "Duplicate."},
                ],
            },
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("duplicate flag" in e and "--decode-flag" in e for e in errors))

    # --- Prefill requires P/D mode ---

    def test_prefill_without_pd_strategy_fails(self):
        recipe = self.make_recipe({
            "prefill": {
                "args": [
                    {"flag": "--prefill-flag", "required": True, "why": "Prefill."},
                ],
            },
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertTrue(errors)
        self.assertTrue(any("P/D router strategy" in e for e in errors))

    def test_prefill_with_pd_strategy_validates(self):
        recipe = self.make_recipe({
            "router": {"strategy": "prefix"},
            "prefill": {
                "args": [
                    {"flag": "--prefill-flag", "required": True, "why": "Prefill."},
                ],
            },
        })
        errors = validate_v4_recipe(self.tmpdir, self.recipe_path, recipe)
        self.assertFalse(errors, errors)

    # --- resolve_role_args ---

    def test_resolve_role_args_basic(self):
        serving = {
            "args": [
                {"flag": "--max-model-len", "value": "16384", "required": True, "why": "Memory."},
                {"flag": "--enable-prefix-caching", "required": True, "why": "Caching."},
            ],
        }
        result = resolve_role_args(serving, "decode")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["flag"], "--max-model-len")
        self.assertEqual(result[1]["flag"], "--enable-prefix-caching")

    def test_resolve_role_args_with_exclude(self):
        serving = {
            "args": [
                {"flag": "--max-model-len", "value": "16384", "required": True, "why": "Memory."},
                {"flag": "--enable-prefix-caching", "required": True, "why": "Caching."},
            ],
            "decode": {
                "exclude": [
                    {"flag": "--enable-prefix-caching", "reason": "Not for decode."},
                ],
            },
        }
        result = resolve_role_args(serving, "decode")
        flags = [a["flag"] for a in result]
        self.assertIn("--max-model-len", flags)
        self.assertNotIn("--enable-prefix-caching", flags)
        self.assertEqual(len(result), 1)

    def test_resolve_role_args_with_role_args(self):
        serving = {
            "args": [
                {"flag": "--max-model-len", "value": "16384", "required": True, "why": "Memory."},
            ],
            "decode": {
                "args": [
                    {"flag": "--decode-flag", "required": True, "why": "Decode only."},
                ],
            },
        }
        result = resolve_role_args(serving, "decode")
        flags = [a["flag"] for a in result]
        self.assertEqual(flags, ["--max-model-len", "--decode-flag"])

    def test_resolve_role_args_leader_vs_worker(self):
        serving = {
            "args": [
                {"flag": "--max-model-len", "value": "16384", "required": True, "why": "Memory."},
            ],
            "decode": {
                "leader_args": [
                    {"flag": "--leader-flag", "value": "true", "required": True, "why": "Leader only."},
                ],
                "worker_args": [
                    {"flag": "--worker-flag", "value": "true", "required": True, "why": "Worker only."},
                ],
            },
        }
        leader_result = resolve_role_args(serving, "decode", "leader")
        worker_result = resolve_role_args(serving, "decode", "worker")

        leader_flags = [a["flag"] for a in leader_result]
        worker_flags = [a["flag"] for a in worker_result]

        self.assertIn("--max-model-len", leader_flags)
        self.assertIn("--leader-flag", leader_flags)
        self.assertNotIn("--worker-flag", leader_flags)

        self.assertIn("--max-model-len", worker_flags)
        self.assertIn("--worker-flag", worker_flags)
        self.assertNotIn("--leader-flag", worker_flags)


if __name__ == "__main__":
    unittest.main()
