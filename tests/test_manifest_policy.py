"""Repository-input Service policy, including raw intake and rendered output."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from manifest_policy import validate_manifest_text, validate_recipe_manifest_files
from check_manifests import check_recipe
from render import render_recipe


SERVICE = "apiVersion: v1\nkind: Service\nmetadata:\n  name: forbidden-http\n"
LWS = "apiVersion: leaderworkerset.x-k8s.io/v1\nkind: LeaderWorkerSet\nmetadata:\n  name: model\n"
LLMISVC = "apiVersion: serving.kserve.io/v1alpha1\nkind: LLMInferenceService\nmetadata:\n  name: model\n"


class ManifestPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="service-policy-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        for directory in ("schema", "engine-versions", "templates"):
            shutil.copytree(REPO / directory, self.repo / directory)
        self.leaf = self.repo / "models/example/recipes/h200-tp1"
        self.leaf.mkdir(parents=True)

    def write(self, relative, text):
        path = self.leaf / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def validate(self, *arguments, env=None):
        return subprocess.run(
            [sys.executable, str(REPO / "tools/validate.py"), "--repo", str(self.repo),
             "--current", *arguments],
            capture_output=True, text=True, env=env,
        )

    def recipe(self, **platform_extra):
        # Only renderer inputs are needed; validator tests below deliberately
        # prove the policy is independent of successful recipe-schema checks.
        recipe = {
            "schema_version": 4, "model_id": "example", "recipe_id": "h200-tp1",
            "serving": {"image": "docker.io/vllm/vllm-openai:v0.24.0",
                        "model": "example/model", "parallelism": {"mode": "tp", "tp": 1}},
            "platforms": [{"stack": "vllm", "version": "v0.24.0", **platform_extra}],
        }
        return self.write("recipe.yaml", yaml.safe_dump(recipe))

    def test_raw_only_formats_and_document_locations_fail(self):
        cases = [
            ("nested/not-a-service.yaml", SERVICE, "document 1"),
            ("input.yml", LWS + "---\n" + SERVICE, "document 2"),
            ("input.json", json.dumps(yaml.safe_load(SERVICE)), "document 1"),
            ("list.json", json.dumps({"apiVersion": "v1", "kind": "List", "items": [
                yaml.safe_load(LWS), {"apiVersion": "v1", "kind": "List", "items": [
                    yaml.safe_load(SERVICE)]}]}), "document 1.items[1].items[0]"),
            ("empty-doc.yaml", "---\n---\n" + SERVICE, "document 2"),
        ]
        for filename, text, location in cases:
            with self.subTest(filename=filename):
                path = self.write("raw-manifest/" + filename, text)
                try:
                    for flags in ((), ("--require-converted-raw",)):
                        result = self.validate(*flags)
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertIn(str(path), result.stderr)
                        self.assertIn(location, result.stderr)
                        self.assertIn("Service/forbidden-http", result.stderr)
                        self.assertIn("Author-supplied Service objects are prohibited", result.stderr)
                finally:
                    path.unlink()

    def test_service_free_raw_only_control_passes(self):
        self.write("raw-manifest/deployment.yaml", LWS + "---\n" + LLMISVC)
        self.write("raw-manifest/nested/list.json", json.dumps({
            "kind": "List", "apiVersion": "v1", "items": [yaml.safe_load(LLMISVC)]}))
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_generated_and_explicit_references_fail_before_schema(self):
        recipe = {"platforms": [{"pinned_manifest": "pins/input.json"}],
                  "deployment": {"manifests": [{"path": "legacy/bundle.yml"}]}}
        self.write("recipe.yaml", yaml.safe_dump(recipe))
        paths = [self.write(name, SERVICE) for name in (
            "config/nested/resource.yml", "manifests/vllm-v0.24.0/bundle.yaml",
            "pins/input.json", "legacy/bundle.yml")]
        # JSON must be parsed as JSON, not a YAML-only shortcut.
        paths[2].write_text(json.dumps(yaml.safe_load(SERVICE)))
        result = self.validate()
        self.assertEqual(result.returncode, 1)
        for path in paths:
            self.assertIn(f"{path}: document 1: Service/forbidden-http", result.stderr)
        self.assertEqual(len(validate_recipe_manifest_files(self.leaf, recipe)), 4)

    def test_only_documents_and_list_items_are_objects_not_embedded_data(self):
        text = LWS + "spec:\n  arbitrary: {kind: Service}\n---\n" + LLMISVC
        text += '---\nkind: ConfigMap\ndata:\n  example: |\n    kind: Service\n'
        self.assertEqual(validate_manifest_text(Path("control.yaml"), text), [])
        self.write("config/values.yaml", text)
        self.write("results/log.yaml", SERVICE)
        self.write("guides/notes.yaml", SERVICE)
        self.write("benchmarks/prompt.json", json.dumps(yaml.safe_load(SERVICE)))
        self.assertEqual(validate_recipe_manifest_files(self.leaf, {}), [])

    def test_recursive_list_alias_does_not_hide_other_items_or_recurse_forever(self):
        errors = validate_manifest_text(Path("list.yaml"),
            "&self\nkind: List\nitems:\n  - *self\n  - kind: Service\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("document 1.items[1]: Service/<unnamed>", errors[0])

    def test_typed_service_lists_are_rejected_including_empty_and_nested_json(self):
        typed_list = {"apiVersion": "v1", "kind": "ServiceList", "items": [
            {"metadata": {"name": "blocked"}, "spec": {"ports": [{"port": 8000}]}}]}
        for filename, value, location in (
            ("typed-list.yaml", typed_list, "document 1"),
            ("empty.yml", {**typed_list, "items": []}, "document 1"),
            ("nested.json", {"kind": "List", "items": [
                {"kind": "List", "items": [typed_list]}]}, "document 1.items[0].items[0]"),
        ):
            with self.subTest(filename=filename):
                text = json.dumps(value) if filename.endswith(".json") else yaml.safe_dump(value)
                errors = validate_manifest_text(Path(filename), text)
                self.assertEqual(len(errors), 1)
                self.assertIn(f"{location}: ServiceList/<unnamed>", errors[0])
                path = self.write("raw-manifest/" + filename, text)
                result = self.validate()
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"{path}: {location}: ServiceList/", result.stderr)
                path.unlink()

    def test_raw_only_config_and_output_are_not_a_policy_bypass(self):
        self.write("raw-manifest/input.yaml", LWS)
        for filename in ("config/nested/input.json", "manifests/rhoai-3.5/output.yml"):
            path = self.write(filename, json.dumps(yaml.safe_load(SERVICE)))
            result = self.validate()
            self.assertEqual(result.returncode, 1)
            self.assertIn(str(path), result.stderr)
            self.assertIn("Service/forbidden-http", result.stderr)

    def test_syntax_errors_remain_failures(self):
        for text in ("kind: [", "kind: Deployment\nkind: Service\n"):
            errors = validate_manifest_text(Path("input.yaml"), text)
            self.assertTrue(errors)
            self.assertIn("cannot parse manifest", errors[0])

    def test_renderer_rejects_config_and_pin_without_writing(self):
        for filename in ("config/nested/input.yaml", "pins/input.json"):
            with self.subTest(filename=filename):
                recipe_path = self.recipe(pinned_manifest=filename) if filename.startswith("pins") else self.recipe()
                resource = self.write(filename, json.dumps(yaml.safe_load(SERVICE)))
                try:
                    for dry_run in (False, True):
                        _, errors = render_recipe(self.repo, recipe_path, dry_run=dry_run)
                        self.assertTrue(any("Service/forbidden-http" in error for error in errors))
                        self.assertFalse((self.leaf / "manifests").exists())
                finally:
                    resource.unlink()

    def test_final_kustomize_output_rejected_before_write_or_dry_run(self):
        recipe_path = self.recipe()
        recipe = yaml.safe_load(recipe_path.read_text())
        recipe["serving"]["config_overrides"] = True
        recipe_path.write_text(yaml.safe_dump(recipe))
        self.write("config/kustomization.yaml", "resources: []\n")
        with patch("render.run_kustomize", return_value=LWS + "---\n" + SERVICE):
            for dry_run in (False, True):
                rendered, errors = render_recipe(self.repo, recipe_path, dry_run=dry_run)
                self.assertEqual(rendered, "")
                self.assertIn("manifests/vllm-v0.24.0/deployment.yaml: document 2", errors[0])
                self.assertIn("Service/forbidden-http", errors[0])
                self.assertFalse((self.leaf / "manifests").exists())

    def test_manifest_drift_gate_maps_isolated_render_errors_to_repository_file(self):
        recipe_path = self.recipe()
        recipe = yaml.safe_load(recipe_path.read_text())
        recipe["serving"]["config_overrides"] = True
        recipe_path.write_text(yaml.safe_dump(recipe))
        self.write("config/kustomization.yaml", "resources: []\n")
        with patch("render.run_kustomize", return_value=LWS + "---\n" + SERVICE):
            errors = check_recipe(self.repo, self.leaf)
        output = self.leaf / "manifests/vllm-v0.24.0/deployment.yaml"
        self.assertTrue(errors[0].startswith(f"{output}: document 2: Service/"))
        self.assertFalse(output.exists())

    def test_service_free_renderer_and_pin_controls(self):
        recipe_path = self.recipe()
        rendered, errors = render_recipe(self.repo, recipe_path)
        self.assertEqual(errors, [])
        self.assertEqual(yaml.safe_load(rendered)["kind"], "Deployment")
        self.write("pins/model.yml", LWS + "---\n" + LLMISVC)
        recipe_path = self.recipe(pinned_manifest="pins/model.yml")
        rendered, errors = render_recipe(self.repo, recipe_path)
        self.assertEqual(errors, [])
        self.assertEqual(rendered, LWS + "---\n" + LLMISVC)

    def test_renderer_repairs_stale_generated_service_but_validation_still_rejects_it(self):
        recipe_path = self.recipe()
        recipe = yaml.safe_load(recipe_path.read_text())
        recipe["deployment"] = {"manifests": [{
            "path": "manifests/vllm-v0.24.0/deployment.yaml", "kind": "Deployment", "generated": True}]}
        recipe_path.write_text(yaml.safe_dump(recipe))
        clean, errors = render_recipe(self.repo, recipe_path)
        self.assertEqual(errors, [])
        output = self.write("manifests/vllm-v0.24.0/deployment.yaml", clean + "---\n" + SERVICE)
        stale = output.read_bytes()
        result = self.validate()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{output}: document 2: Service/", result.stderr)
        errors = check_recipe(self.repo, self.leaf)
        self.assertTrue(any(f"{output}: document 2: Service/" in error for error in errors))
        self.assertEqual(output.read_bytes(), stale)
        rendered, errors = render_recipe(self.repo, recipe_path, dry_run=True)
        self.assertEqual(errors, [])
        self.assertEqual(rendered, clean)
        self.assertEqual(output.read_bytes(), stale)
        rendered, errors = render_recipe(self.repo, recipe_path)
        self.assertEqual(errors, [])
        self.assertEqual(rendered, clean)
        self.assertEqual(output.read_text(), clean)
        self.assertEqual(check_recipe(self.repo, self.leaf), [])

    def test_newly_rendered_typed_service_list_is_rejected_before_write(self):
        recipe_path = self.recipe()
        with patch("render.render_template", return_value="apiVersion: v1\nkind: ServiceList\nitems: []\n"):
            for dry_run in (False, True):
                rendered, errors = render_recipe(self.repo, recipe_path, dry_run=dry_run)
                self.assertEqual(rendered, "")
                self.assertIn("deployment.yaml: document 1: ServiceList/<unnamed>", errors[0])
                self.assertFalse((self.leaf / "manifests").exists())

    def test_renderer_does_not_exempt_pins_hand_authored_or_unregenerated_outputs(self):
        output_name = "manifests/vllm-v0.24.0/deployment.yaml"
        for ownership in ("pin", "hand-authored", "blocked", "unregenerated"):
            with self.subTest(ownership=ownership):
                extra = {"pinned_manifest": output_name} if ownership == "pin" else {}
                if ownership == "blocked":
                    extra["blocked"] = True
                recipe_path = self.recipe(**extra)
                if ownership == "hand-authored":
                    recipe = yaml.safe_load(recipe_path.read_text())
                    recipe["deployment"] = {"manifests": [
                        {"path": output_name, "kind": "Deployment", "generated": False}]}
                    recipe_path.write_text(yaml.safe_dump(recipe))
                name = "manifests/other-version/deployment.yaml" if ownership == "unregenerated" else output_name
                resource = self.write(name, SERVICE)
                for dry_run in (False, True):
                    _, errors = render_recipe(self.repo, recipe_path, dry_run=dry_run)
                    self.assertTrue(any("Service/forbidden-http" in error for error in errors))
                    self.assertEqual(resource.read_text(), SERVICE)
                resource.unlink()

    def test_ci_annotation_summary_and_failure(self):
        path = self.write("raw-manifest/input.yaml", SERVICE)
        summary = self.repo / "summary.md"
        result = self.validate(env={**os.environ, "GITHUB_ACTIONS": "true",
                                    "GITHUB_STEP_SUMMARY": str(summary)})
        self.assertEqual(result.returncode, 1)
        relative = path.relative_to(self.repo).as_posix()
        self.assertIn(f"::error file={relative},title=Prohibited Service::", result.stdout)
        self.assertIn("document 1: Service/forbidden-http", result.stdout)
        self.assertIn(relative, summary.read_text())
        self.assertIn("Author-supplied Service objects are prohibited", summary.read_text())


if __name__ == "__main__":
    unittest.main()
