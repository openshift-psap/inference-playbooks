import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from tools.recipe_evidence import load_unique_yaml

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "recipe_evidence.py"
VALIDATOR = REPO / "tools" / "validate.py"


def git(directory, *arguments):
    return subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )


class RecipeEvidenceTests(unittest.TestCase):
    profile_text = """\
schema_version: 1
profile_id: h200-r1
kind: hardware-profile
profile_revision: {revision}
accelerator_key: nvidia-h200-x8
accelerators:
  vendor: nvidia
  model: H200
  count_per_node: 8
correction_log:
  - revision: 1
    summary: Initial profile.
{correction}"""

    def make_repo(self):
        directory = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "engine-versions", directory / "engine-versions")
        git(directory, "init", "-q")
        git(directory, "config", "user.email", "test@example.com")
        git(directory, "config", "user.name", "Test User")
        profile = directory / "hardware-profiles" / "h200-r1.yaml"
        profile.parent.mkdir(parents=True)
        profile.write_text(self.profile_text.format(revision=1, correction=""))
        model = directory / "models" / "glm" / "model.yaml"
        model.parent.mkdir(parents=True)
        model.write_text("schema_version: 1\nmodel_id: glm\nname: GLM\nfamily: GLM\nquantizations:\n  - name: FP8\n")
        recipe = directory / "models" / "glm" / "recipes" / "h200-tp8-aggregated" / "recipe.yaml"
        recipe.parent.mkdir(parents=True)
        recipe.write_text("recipe_id: guidellm-tp8\nhardware_profile: hardware-profiles/h200-r1.yaml\n")
        platforms_dir = recipe.parent / "platforms"
        platforms_dir.mkdir()
        (platforms_dir / "rhoai-3.5.yaml").write_text("{}\n")
        git(directory, "add", ".")
        git(directory, "commit", "-qm", "initial")
        return directory, profile, recipe

    def run_tool(self, directory, *arguments):
        return subprocess.run(
            ["python3", str(TOOL), "--repo", str(directory), *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def run_validator(self, directory, *arguments):
        return subprocess.run(
            ["python3", str(VALIDATOR), "--repo", str(directory), *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def recipe_validator(self):
        schemas = [json.loads(path.read_text()) for path in (REPO / "schema").glob("*.schema.json")]
        registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema)) for schema in schemas
        )
        recipe = next(schema for schema in schemas if schema["$id"].endswith("/recipe.schema.json"))
        return Draft202012Validator(recipe, registry=registry)

    def sample_recipe(self):
        return {
            "schema_version": 4,
            "recipe_id": "glm-guidellm-tp8",
            "model_id": "glm",
            "platforms": [{"stack": "rhoai", "version": "3.5", "overrides": "platforms/rhoai-3.5.yaml"}],
            "hardware_profile": "hardware-profiles/h200-r1.yaml",
            "workload_profile": "guidellm-8k1k",
            "deployment_mode": "tp8-aggregated",
            "optimization_intent": "latency",
            "maturity": "day-zero",
            "serving": {
                "image": "vllm/vllm-openai:v0.24.0",
                "model": "RedHatAI/GLM-FP8",
                "parallelism": {"mode": "tp", "tp": 8},
                "args": [{"flag": "--enable-auto-tool-choice", "required": True, "why": "Tool calling."}],
            },
            "deployment": {"scope": "single-node"},
        }

    def test_profile_correction_is_allowed_with_a_revision_and_log(self):
        directory, profile, _ = self.make_repo()
        profile.write_text(self.profile_text.format(
            revision=2,
            correction="  - revision: 2\n    summary: Corrected host memory documentation.\n",
        ))
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "check-hardware-profiles")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_profile_identity_change_requires_a_new_file(self):
        directory, profile, _ = self.make_repo()
        changed = self.profile_text.format(revision=2, correction="  - revision: 2\n    summary: Incorrect change.\n")
        profile.write_text(changed.replace("nvidia-h200-x8", "nvidia-h200-x4").replace("count_per_node: 8", "count_per_node: 4"))
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "check-hardware-profiles")
        self.assertEqual(result.returncode, 1)
        self.assertIn("accelerator_key", result.stderr)

    def test_profile_metadata_correction_is_allowed(self):
        directory, profile, _ = self.make_repo()
        corrected = self.profile_text.format(
            revision=2,
            correction="  - revision: 2\n    summary: Added the recorded RoCE bandwidth.\n",
        )
        profile.write_text(corrected + "network:\n  inter_node_bandwidth_gbe: 400\n")
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "check-hardware-profiles")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_profile_is_allowed(self):
        directory, _, _ = self.make_repo()
        profile = directory / "hardware-profiles" / "h200-r2.yaml"
        profile.write_text(self.profile_text.format(revision=1, correction="").replace("h200-r1", "h200-r2"))
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "check-hardware-profiles")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_profile_requires_a_log_for_its_current_revision(self):
        directory, _, _ = self.make_repo()
        profile = directory / "hardware-profiles" / "h200-r2.yaml"
        profile.write_text(self.profile_text.format(revision=2, correction="").replace("h200-r1", "h200-r2"))
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "check-hardware-profiles")
        self.assertEqual(result.returncode, 1)
        self.assertIn("current revision", result.stderr)

    def test_duplicate_profile_fields_are_rejected(self):
        directory, profile, _ = self.make_repo()
        profile.write_text(self.profile_text.format(revision=1, correction="") + "profile_revision: 1\n")
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "check-hardware-profiles")
        self.assertEqual(result.returncode, 1)
        self.assertIn("duplicate key", result.stderr)

    def test_recipe_change_selects_only_that_recipe(self):
        directory, _, recipe = self.make_repo()
        recipe.write_text("recipe_id: guidellm-tp8\nmaturity: validated\n")
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "affected-recipes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"all": False, "recipes": ["models/glm/recipes/h200-tp8-aggregated"]},
        )

    def test_profile_correction_selects_referencing_recipe(self):
        directory, profile, _ = self.make_repo()
        profile.write_text(self.profile_text.format(
            revision=2,
            correction="  - revision: 2\n    summary: Corrected host memory documentation.\n",
        ))
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "affected-recipes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"all": False, "recipes": ["models/glm/recipes/h200-tp8-aggregated"]},
        )

    def test_new_profile_does_not_fan_out(self):
        directory, _, recipe = self.make_repo()
        # Even a pre-existing forward reference must not turn an addition into
        # a correction fan-out.
        recipe.write_text("hardware_profile: hardware-profiles/new.yaml\n")
        git(directory, "add", ".")
        git(directory, "commit", "-qm", "forward reference")
        (directory / "hardware-profiles/new.yaml").write_text("profile_id: new\n")
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "affected-recipes")
        self.assertEqual(json.loads(result.stdout)["recipes"], [])

    def test_model_and_template_changes_select_recipes(self):
        for changed in ("models/glm/model.yaml", "templates/vllm/test.yaml.j2"):
            with self.subTest(changed=changed):
                directory, _, _ = self.make_repo()
                path = directory / changed
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("changed: true\n")
                git(directory, "add", ".")
                result = self.run_tool(directory, "--cached", "affected-recipes")
                self.assertEqual(json.loads(result.stdout)["recipes"], ["models/glm/recipes/h200-tp8-aggregated"])

    def test_raw_only_does_not_render_until_converted(self):
        directory, _, _ = self.make_repo()
        raw = directory / "models/glm/recipes/raw/raw-manifest/input.yaml"
        raw.parent.mkdir(parents=True)
        raw.write_text("kind: Deployment\n")
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "affected-recipes")
        self.assertEqual(json.loads(result.stdout)["recipes"], [])
        (raw.parent.parent / "recipe.yaml").write_text("hardware_profile: hardware-profiles/h200-r1.yaml\n")
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "affected-recipes")
        self.assertEqual(json.loads(result.stdout)["recipes"], ["models/glm/recipes/raw"])

    def test_new_recipe_engine_metadata_is_required_in_diff_validation(self):
        directory, profile, recipe = self.make_repo()
        shutil.copytree(REPO / "schema", directory / "schema")
        profile.write_text(self.profile_text.format(revision=1, correction=""))
        recipe.write_text(yaml.safe_dump(self.sample_recipe()))
        git(directory, "add", ".")
        git(directory, "commit", "-qm", "existing recipe")
        new = recipe.parent.parent / "new-recipe"
        shutil.copytree(recipe.parent, new)
        document = self.sample_recipe()
        document["recipe_id"] = "new-recipe"
        document["serving"]["image"] = "docker.io/vllm/vllm-openai:v0.24.0"
        (new / "recipe.yaml").write_text(yaml.safe_dump(document))
        git(directory, "add", ".")
        result = self.run_validator(directory, "--base", "HEAD", "--cached")
        self.assertEqual(result.returncode, 1)
        self.assertIn("image_usage must identify", result.stderr)
        document["serving"]["image_usage"] = dict(kind="custom", note="Model-specific serving build")
        document["serving"]["engine"] = dict(name="vllm", image=document["serving"]["image"],
                                               version="0.24.0", source="https://example.org/build")
        (new / "recipe.yaml").write_text(yaml.safe_dump(document))
        git(directory, "add", ".")
        result = self.run_validator(directory, "--base", "HEAD", "--cached")
        self.assertEqual(result.returncode, 0, result.stderr)

        # The explicitly identified default runtime needs neither an engine
        # declaration nor an image digest.
        document["serving"].pop("engine")
        document["serving"]["image_usage"] = dict(kind="default", variant="cuda")
        document["platforms"][0]["version"] = "3.5.0"
        (new / "recipe.yaml").write_text(yaml.safe_dump(document))
        git(directory, "add", ".")
        result = self.run_validator(directory, "--base", "HEAD", "--cached")
        self.assertEqual(result.returncode, 0, result.stderr)

        # Replacing the default image must not inherit its classification.
        override = new / "platforms/rhoai-3.5.yaml"
        override.write_text("image: quay.io/example/custom:tag\n")
        git(directory, "add", ".")
        result = self.run_validator(directory, "--base", "HEAD", "--cached")
        self.assertEqual(result.returncode, 1)
        self.assertIn("image_usage must identify", result.stderr)

        override.write_text(yaml.safe_dump(dict(
            image="quay.io/example/custom:tag",
            image_usage=dict(kind="custom", note="Model-specific engine build"),
            engine=dict(name="vllm", image="quay.io/example/custom:tag", version="0.26.0", source="https://example.org/build"),
        )))
        git(directory, "add", ".")
        result = self.run_validator(directory, "--base", "HEAD", "--cached")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_staged_profile_correction_ignores_unstaged_recipe_edits(self):
        directory, profile, recipe = self.make_repo()
        profile.write_text(self.profile_text.format(
            revision=2,
            correction="  - revision: 2\n    summary: Corrected host memory documentation.\n",
        ))
        git(directory, "add", "hardware-profiles/h200-r1.yaml")
        recipe.write_text("recipe_id: guidellm-tp8\n")
        result = self.run_tool(directory, "--cached", "affected-recipes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"all": False, "recipes": ["models/glm/recipes/h200-tp8-aggregated"]},
        )

    def test_validator_reports_invalid_document_shapes_without_a_traceback(self):
        directory, profile, _ = self.make_repo()
        shutil.copytree(REPO / "schema", directory / "schema")
        profile.write_text("profile_id: h200-r1\n")
        result_path = directory / "models" / "glm" / "results" / "run-1" / "result.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_text("[]\n")
        result = self.run_validator(directory, "--current")
        self.assertEqual(result.returncode, 1)
        self.assertIn("profile_revision", result.stderr)
        self.assertIn("is not of type 'object'", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_recipe_schema_requires_canonical_deployment_scope(self):
        recipe = self.sample_recipe()
        validator = self.recipe_validator()
        self.assertFalse(list(validator.iter_errors(recipe)))

        missing_scope = {**recipe, "deployment": {}}
        self.assertTrue(list(validator.iter_errors(missing_scope)))

        legacy_containers = {**recipe, "deployment": {**recipe["deployment"], "containers": {}}}
        self.assertTrue(list(validator.iter_errors(legacy_containers)))

        legacy_nodes = {**recipe, "match": {"nodes": "single"}}
        self.assertTrue(list(validator.iter_errors(legacy_nodes)))

        custom_intent = {**recipe, "optimization_intent": "lowest cost at 128K context"}
        self.assertFalse(list(validator.iter_errors(custom_intent)))

    def test_custom_image_identification_requires_nonempty_note(self):
        recipe = self.sample_recipe()
        validator = self.recipe_validator()
        for usage in ({"kind": "custom"}, {"kind": "custom", "note": "   "}, {"kind": "default"}):
            recipe["serving"]["image_usage"] = usage
            self.assertTrue(list(validator.iter_errors(recipe)))
        for usage in ({"kind": "custom", "note": "Model-specific build"}, {"kind": "default", "variant": "cuda"}):
            recipe["serving"]["image_usage"] = usage
            self.assertFalse(list(validator.iter_errors(recipe)))

    def test_reader_notes_example_matches_schema(self):
        schemas = [json.loads(path.read_text()) for path in (REPO / "schema").glob("*.schema.json")]
        registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema)) for schema in schemas
        )
        schema = next(schema for schema in schemas if schema["$id"].endswith("/recipe-notes.schema.json"))
        notes = yaml.safe_load((REPO / "schema" / "examples" / "recipe-notes.yaml").read_text())
        self.assertFalse(list(Draft202012Validator(schema, registry=registry).iter_errors(notes)))

    def test_unquoted_status_date_remains_a_schema_string(self):
        notes = load_unique_yaml("schema_version: 1\ndecisions:\n  - subjects: [image]\n    why: Verified in CI.\n    status: {state: verified, date: 2026-08-12, method: CI}\n")
        self.assertEqual(notes["decisions"][0]["status"]["date"], "2026-08-12")

    def test_raw_only_leaf_accepts_multidoc_yaml_and_rejects_duplicates(self):
        directory, profile, recipe_path = self.make_repo()
        shutil.copytree(REPO / "schema", directory / "schema")
        profile.write_text(self.profile_text.format(revision=1, correction=""))
        recipe_path.unlink()
        raw = recipe_path.parent / "raw-manifest" / "deployment.yaml"
        raw.parent.mkdir()
        raw.write_text("kind: Deployment\nmetadata: {name: example}\n---\nkind: Service\nmetadata: {name: example}\n")
        result = self.run_validator(directory, "--current")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_validator(directory, "--current", "--require-converted-raw")
        self.assertIn("maintainer conversion required before merge", result.stderr)

        raw.write_text("kind: Deployment\nkind: Service\n")
        result = self.run_validator(directory, "--current")
        self.assertIn("found duplicate key 'kind'", result.stderr)

        raw.write_text("kind: Deployment\n")
        recipe_path.write_text(yaml.safe_dump(self.sample_recipe()))
        config = recipe_path.parent / "config"
        config.mkdir()
        (config / "modelserver.yaml").write_text("kind: Deployment\n")
        result = self.run_validator(directory, "--current", "--require-converted-raw")
        self.assertEqual(result.returncode, 0, result.stderr)

        raw_json = raw.parent / "values.json"
        raw_json.write_text('{"kind": "ConfigMap", "metadata": {"name": "example"}}')
        recipe_path.unlink()
        result = self.run_validator(directory, "--current")
        self.assertEqual(result.returncode, 0, result.stderr)

        raw_json.write_text("kind: ConfigMap\n")
        result = self.run_validator(directory, "--current")
        self.assertIn("cannot parse raw manifest", result.stderr)

        wrong = recipe_path.parent.parent / "not-a-workload" / "tp8-aggregated" / "raw-manifest" / "deployment.yaml"
        wrong.parent.mkdir(parents=True)
        wrong.write_text("kind: Deployment\n")
        result = self.run_validator(directory, "--current")
        self.assertIn("raw-manifest must be under models/<model>/recipes/<recipe>/raw-manifest", result.stderr)

    def test_validator_checks_reader_note_references(self):
        directory, profile, recipe_path = self.make_repo()
        shutil.copytree(REPO / "schema", directory / "schema")
        profile.write_text(self.profile_text.format(revision=1, correction=""))
        recipe = self.sample_recipe()
        recipe["notes"] = "guides/notes.yaml"
        recipe_path.write_text(yaml.safe_dump(recipe))
        source = recipe_path.parent / "config" / "modelserver.yaml"
        source.parent.mkdir()
        source.write_text(yaml.safe_dump({"kind": "Deployment"}))
        notes_path = recipe_path.parent / "guides" / "notes.yaml"
        notes_path.parent.mkdir()
        notes = {
            "schema_version": 1,
            "profile": {"specs": [{"label": "Scope", "source": "recipe.yaml#/deployment/scope"}]},
            "decisions": [{"subjects": ["GPU count", "--tensor-parallel-size"], "why": "They must agree."}],
        }
        notes_path.write_text(yaml.safe_dump(notes))
        result = self.run_validator(directory, "--current")
        self.assertEqual(result.returncode, 0, result.stderr)

        notes["profile"]["specs"][0]["source"] = "recipe.yaml#/deployment/missing"
        notes_path.write_text(yaml.safe_dump(notes))
        result = self.run_validator(directory, "--current")
        self.assertIn("spec source does not resolve", result.stderr)

        notes["profile"]["specs"][0]["source"] = "recipe.yaml#/deployment/scope"
        notes["features"] = [{"name": "Tool calling", "status": "claimed"}]
        notes_path.write_text(yaml.safe_dump(notes))
        result = self.run_validator(directory, "--current")
        self.assertIn("features must be an object with known keys", result.stderr)
        del notes["features"]

        notes["image_choices"] = [{
            "component": "modelserver", "container": "vllm", "ref": "docker.io/vllm/vllm-openai:v0.24.0",
            "status": {"state": "needs-verification", "note": "Run pending"}, "why": "Model support",
        }]
        notes_path.write_text(yaml.safe_dump(notes))
        recipe["maturity"] = "validated"
        recipe["image"] = {"recommended": {"ref": "docker.io/vllm/vllm-openai:v0.24.0", "status": {"state": "needs-verification", "note": "Pending"}}}
        recipe["benchmark_runs"] = ["results/run-1/run.yaml"]
        run_path = recipe_path.parent / "results" / "run-1" / "run.yaml"
        run_path.parent.mkdir(parents=True)
        run_path.write_text(yaml.safe_dump({
            "schema_version": 1,
            "run_id": "run-1",
            "recipe_id": recipe["recipe_id"],
            "deployment_scope": "single-node",
            "hardware_profile": recipe["hardware_profile"],
            "hardware_profile_revision": 1,
            "harness": "guidellm",
            "result": "result.json",
            "artifacts": [{"type": "raw-output", "path": "output.log"}],
        }))
        (run_path.parent / "result.json").write_text(json.dumps({
            "schema_version": 1,
            "run_id": "run-1",
            "deployment_scope": "single-node",
            "accelerator_key": "nvidia-h200-x8",
            "metrics": {},
        }))
        (run_path.parent / "output.log").write_text("harness output")
        recipe_path.write_text(yaml.safe_dump(recipe))
        result = self.run_validator(directory, "--current")
        self.assertIn("cannot recommend an unverified image", result.stderr)

    def test_artifact_local_path_validated(self):
        directory, profile, recipe_path = self.make_repo()
        shutil.copytree(REPO / "schema", directory / "schema")
        recipe = self.sample_recipe()
        recipe_path.write_text(yaml.safe_dump(recipe))
        recipe["benchmark_runs"] = ["results/run-1/run.yaml"]
        run_dir = recipe_path.parent / "results" / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(json.dumps({
            "schema_version": 1, "run_id": "run-1",
            "deployment_scope": "single-node",
            "accelerator_key": "nvidia-h200-x8", "metrics": {},
        }))
        run = {
            "schema_version": 1, "run_id": "run-1",
            "recipe_id": recipe["recipe_id"],
            "deployment_scope": "single-node",
            "hardware_profile": recipe["hardware_profile"],
            "hardware_profile_revision": 1,
            "harness": "guidellm", "result": "result.json",
            "artifacts": [{"type": "raw-output", "path": "output.log"}],
        }
        (run_dir / "run.yaml").write_text(yaml.safe_dump(run))
        recipe_path.write_text(yaml.safe_dump(recipe))
        result = self.run_validator(directory, "--current")
        self.assertIn("local file does not exist", result.stderr)

        (run_dir / "output.log").write_text("data")
        result = self.run_validator(directory, "--current")
        self.assertNotIn("local file does not exist", result.stderr)

    def test_artifact_external_uri_requires_checksum(self):
        directory, profile, recipe_path = self.make_repo()
        shutil.copytree(REPO / "schema", directory / "schema")
        recipe = self.sample_recipe()
        recipe["benchmark_runs"] = ["results/run-1/run.yaml"]
        run_dir = recipe_path.parent / "results" / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(json.dumps({
            "schema_version": 1, "run_id": "run-1",
            "deployment_scope": "single-node",
            "accelerator_key": "nvidia-h200-x8", "metrics": {},
        }))
        run = {
            "schema_version": 1, "run_id": "run-1",
            "recipe_id": recipe["recipe_id"],
            "deployment_scope": "single-node",
            "hardware_profile": recipe["hardware_profile"],
            "hardware_profile_revision": 1,
            "harness": "guidellm", "result": "result.json",
            "artifacts": [{
                "type": "raw-output",
                "uri": "s3://benchmark-bucket/runs/run-1/output.tar.gz",
                "checksum": "sha256:" + "a" * 64,
            }],
        }
        (run_dir / "run.yaml").write_text(yaml.safe_dump(run))
        recipe_path.write_text(yaml.safe_dump(recipe))
        result = self.run_validator(directory, "--current")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_artifact_schema_rejects_both_path_and_uri(self):
        validator = self.recipe_validator()
        schemas = [json.loads(p.read_text()) for p in (REPO / "schema").glob("*.schema.json")]
        registry = Registry().with_resources(
            (s["$id"], Resource.from_contents(s)) for s in schemas
        )
        run_schema = next(s for s in schemas if s["$id"].endswith("/benchmark-run.schema.json"))
        run_validator = Draft202012Validator(run_schema, registry=registry)
        run = {
            "schema_version": 1, "run_id": "run-1", "recipe_id": "test",
            "deployment_scope": "single-node",
            "hardware_profile": "hardware-profiles/h200.yaml",
            "hardware_profile_revision": 1,
            "harness": "guidellm", "result": "result.json",
            "artifacts": [{
                "type": "raw-output",
                "path": "output.log",
                "uri": "s3://bucket/output.log",
                "checksum": "sha256:" + "b" * 64,
            }],
        }
        errors = list(run_validator.iter_errors(run))
        self.assertTrue(any("oneOf" in str(e.schema_path) for e in errors),
                        f"Expected oneOf validation error, got: {errors}")

    def test_artifact_mlflow_uri_accepted(self):
        schemas = [json.loads(p.read_text()) for p in (REPO / "schema").glob("*.schema.json")]
        registry = Registry().with_resources(
            (s["$id"], Resource.from_contents(s)) for s in schemas
        )
        run_schema = next(s for s in schemas if s["$id"].endswith("/benchmark-run.schema.json"))
        run_validator = Draft202012Validator(run_schema, registry=registry)
        run = {
            "schema_version": 1, "run_id": "run-1", "recipe_id": "test",
            "deployment_scope": "single-node",
            "hardware_profile": "hardware-profiles/h200.yaml",
            "hardware_profile_revision": 1,
            "harness": "guidellm", "result": "result.json",
            "artifacts": [{
                "type": "metrics-export",
                "uri": "mlflow://experiment/run-1/artifacts/metrics.json",
                "checksum": "sha256:" + "c" * 64,
            }],
        }
        errors = list(run_validator.iter_errors(run))
        self.assertEqual(errors, [], f"Unexpected validation errors: {errors}")

    def test_v4_recipes_have_serving_block_and_platforms(self):
        recipes = [
            "models/gemma-4/recipes/h200-x8-mtp-single-gpu-8k1k",
            "models/glm-5.2/recipes/h200-x8-pp2-tp8-agentx-128k-rhoai",
            "models/glm-5.2/recipes/h200-x8-pp2-tp8-agentx-128k-vllm",
            "models/qwen3-235b-a22b/recipes/h200-x8-pp2-tp8-agentx-128k",
        ]
        for directory in recipes:
            with self.subTest(recipe=directory):
                root = REPO / directory
                recipe = yaml.safe_load((root / "recipe.yaml").read_text())
                self.assertEqual(recipe["schema_version"], 4)
                self.assertEqual(recipe["maturity"], "day-zero")
                self.assertIn("serving", recipe)
                self.assertIn("platforms", recipe)
                serving = recipe["serving"]
                self.assertIn("image", serving)
                self.assertIn("model", serving)
                self.assertIn("parallelism", serving)
                self.assertIn("args", serving)
                for platform_entry in recipe["platforms"]:
                    self.assertIn("stack", platform_entry)
                    self.assertIn("version", platform_entry)
                    self.assertIn("overrides", platform_entry)
                    override_path = root / platform_entry["overrides"]
                    self.assertTrue(override_path.is_file(), f"Override file missing: {override_path}")
                self.assertTrue((REPO / "models" / recipe["model_id"] / "model.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
