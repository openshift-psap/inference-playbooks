"""Engine resolution fixtures are synthetic, not published image mappings."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema.exceptions import ValidationError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from engine_versions import compare_versions, engine_errors, load_engine_index, release_vllm_version, resolve_engine
from render import merge_overrides


class EngineVersionTests(unittest.TestCase):
    def setUp(self):
        self.image = "registry.example.org/rhoai/vllm:3.5"
        self.engine = dict(name="vllm", image=self.image, version="0.24.0", source="https://example.org/build/1")
        self.serving = dict(image=self.image, image_usage=dict(kind="default", variant="cuda"))
        self.platform = dict(stack="rhoai", version="3.5.0")
        self.index = load_engine_index(REPO)

    def resolve(self, overrides=None, index=None):
        return resolve_engine(self.serving, overrides or {}, self.platform, self.index if index is None else index)

    def test_identified_default_runtime_needs_no_digest(self):
        result = self.resolve()
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(result["source"], "release-index")
        self.assertEqual(result["evidence"], self.index["source"])

    def test_custom_and_missing_mapping_are_unknown(self):
        self.assertEqual(self.resolve({"image": "quay.io/example/custom:latest"})["state"], "unknown")
        self.assertEqual(self.resolve(index={"releases": [], "variants": []})["state"], "unknown")
        self.assertEqual(self.resolve({"image_usage": {"kind": "custom", "note": "Model support"}})["state"], "unknown")
        self.platform["stack"] = "vllm"
        self.assertEqual(self.resolve()["state"], "unknown")

    def test_explicit_declaration_without_index(self):
        self.serving["engine"] = self.engine
        self.serving["image_usage"] = dict(kind="custom", note="Model-specific engine build")
        result = self.resolve(index={"releases": [], "variants": []})
        self.assertEqual(result["source"], "declaration")

    def test_image_replacement_discards_base_declaration(self):
        self.serving["engine"] = self.engine
        overrides = {"image": "quay.io/example/custom:latest"}
        self.assertEqual(self.resolve(overrides)["state"], "unknown")
        self.assertNotIn("engine", merge_overrides(self.serving, overrides))
        self.assertNotIn("image_usage", merge_overrides(self.serving, overrides))
        overrides["image_usage"] = dict(kind="custom", note="New model support")
        overrides["engine"] = {**self.engine, "image": overrides["image"], "version": "0.25.0.dev1"}
        self.assertEqual(self.resolve(overrides)["version"], "0.25.0.dev1")
        self.assertEqual(merge_overrides(self.serving, overrides)["engine"], overrides["engine"])

    def test_image_binding_and_conflict(self):
        self.assertEqual(self.resolve({"engine": {**self.engine, "image": "quay.io/example/other:1"}})["state"], "error")
        self.assertEqual(self.resolve({"engine": {**self.engine, "version": "0.23.0"}})["state"], "error")
        self.assertEqual(self.resolve({"engine": {**self.engine, "version": "v0.24.0"}})["state"], "resolved")

    def test_new_custom_image_policy_and_legacy(self):
        custom = {"image": "quay.io/example/custom:1"}
        self.assertFalse(engine_errors(self.serving, custom, self.platform, self.index))
        self.assertTrue(engine_errors(self.serving, custom, self.platform, self.index, require_metadata=True))
        self.assertFalse(engine_errors(self.serving, {}, self.platform, self.index, require_metadata=True))
        self.serving["engine"] = {**self.engine, "image": "quay.io/example/wrong:1"}
        self.assertTrue(engine_errors(self.serving, custom, self.platform, self.index))

    def test_custom_note_and_identification_are_required(self):
        custom = {"image_usage": {"kind": "custom"}, "engine": self.engine}
        self.assertIn("image_usage.note", engine_errors(self.serving, custom, self.platform, self.index)[0])
        custom["image_usage"]["note"] = "   "
        self.assertTrue(engine_errors(self.serving, custom, self.platform, self.index))
        custom["image_usage"]["note"] = "Required model support"
        self.assertFalse(engine_errors(self.serving, custom, self.platform, self.index, True))
        self.serving.pop("image_usage")
        self.serving["engine"] = self.engine
        self.assertEqual(self.resolve()["state"], "unknown")
        self.assertTrue(engine_errors(self.serving, {}, self.platform, self.index, True))

    def test_custom_engine_can_differ_from_release_without_inheritance(self):
        custom = {"image_usage": dict(kind="custom", note="Backported model support"),
                  "engine": {**self.engine, "version": "0.25.0+vendor.1"}}
        result = self.resolve(custom)
        self.assertEqual(result["version"], "0.25.0+vendor.1")
        self.assertEqual(result["source"], "declaration")
        self.assertFalse(engine_errors(self.serving, custom, self.platform, self.index, True))

    def test_nonindexed_variant_cannot_inherit_cuda_version(self):
        for variant in ("tpu", "gaudi", "spyre", "neuron", "omni"):
            result = self.resolve({"image_usage": dict(kind="default", variant=variant)})
            self.assertEqual(result["state"], "unknown")

    def test_patch_and_ea_resolution(self):
        for release, version in (("3.4.2", "0.18.0"), ("3.5.0-ea1", "0.19.1"), ("3.5.0-ea2", "0.21.0")):
            self.platform["version"] = release
            self.assertEqual(self.resolve()["version"], version)
        for release in ("3.3.2", "3.3.4", "3.5", "3.5.0-ea3"):
            self.platform["version"] = release
            self.assertEqual(self.resolve()["state"], "unknown")

    def test_numeric_comparison_and_opaque_builds(self):
        self.assertEqual(compare_versions("v0.9.0", "0.10.0"), -1)
        self.assertEqual(compare_versions("0.24.0", "v0.24.0"), 0)
        self.assertEqual(compare_versions("1.0.0", "0.24.0"), 1)
        for opaque in ("0.24.0rc1", "0.24.0.dev1", "0.24.0+vendor.1", "nightly", "0.24"):
            self.assertIsNone(compare_versions(opaque, "0.24.0"))
            self.assertEqual(compare_versions(opaque, opaque), 0)
        self.assertIsNone(compare_versions(None, "0.24.0"))

    def test_index_validation_and_duplicate_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            shutil.copytree(REPO / "schema", repo / "schema")
            (repo / "engine-versions").mkdir()
            path = repo / "engine-versions/index.yaml"
            path.write_text(yaml.safe_dump(self.index))
            self.assertEqual(load_engine_index(repo), self.index)
            self.index["releases"].append(self.index["releases"][0])
            path.write_text(yaml.safe_dump(self.index))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_engine_index(repo)
            path.write_text("schema_version: 1\nreleases: [{version: '3.5.0'}]\n")
            with self.assertRaises(ValidationError):
                load_engine_index(repo)
            path.write_text("schema_version: 1\nschema_version: 1\nreleases: []\n")
            with self.assertRaises(yaml.YAMLError):
                load_engine_index(repo)

    def test_release_index_includes_patch_and_ea_versions(self):
        index = load_engine_index(REPO)
        self.assertEqual(set(index["source"]), {"title", "export", "sha256"})
        self.assertTrue(all(set(entry) == {"version", "vllm_version"} for entry in index["releases"]))
        expected = {
            "3.3.0": "0.13.0", "3.3.1": "0.13.0", "3.3.2": None,
            "3.3.3": "0.13.0", "3.3.5": "0.13.0", "3.3.6": "0.13.0",
            "3.4.0-ea1": "0.14.1", "3.4.0-ea2": "0.16.0",
            "3.4.0": "0.18.0", "3.4.1": "0.18.0", "3.4.2": "0.18.0",
            "3.4.3": "0.18.0", "3.4.5": "0.18.0",
            "3.5.0-ea1": "0.19.1", "3.5.0-ea2": "0.21.0",
            "3.5.0": "0.24.0", "3.5.1": "0.24.0",
            "3.6.0-ea1": "0.26.0", "3.6.0-ea2": "0.28.0",
        }
        self.assertEqual({entry["version"]: entry["vllm_version"] for entry in index["releases"]}, expected)
        self.assertEqual(index["variants"], ["cuda", "rocm", "cpu"])
        for release, version in expected.items():
            self.assertEqual(release_vllm_version(index, release), version)
        for absent in ("3.5", "3.3.4", "3.4.4", "3.5.0 GA", "3.5 EA1", "3.5.0-ea3"):
            self.assertIsNone(release_vllm_version(index, absent))

    def test_unclassified_image_cannot_inherit_release_version(self):
        self.platform["version"] = "3.5.0"
        self.assertEqual(release_vllm_version(load_engine_index(REPO), "3.5.0"), "0.24.0")
        self.serving.pop("image_usage")
        self.assertEqual(self.resolve()["state"], "unknown")

    def test_release_index_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            shutil.copytree(REPO / "schema", repo / "schema")
            shutil.copytree(REPO / "engine-versions", repo / "engine-versions")
            path = repo / "engine-versions/index.yaml"
            index = load_engine_index(repo)
            index["releases"].append(dict(index["releases"][0]))
            path.write_text(yaml.safe_dump(index))
            with self.assertRaisesRegex(ValueError, "duplicate release component"):
                load_engine_index(repo)
            index["releases"].pop()
            index["releases"][0]["version"] = "3.3.0 GA"
            path.write_text(yaml.safe_dump(index))
            with self.assertRaises(ValidationError):
                load_engine_index(repo)

    def test_optional_index_image_references_do_not_require_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            shutil.copytree(REPO / "schema", repo / "schema")
            shutil.copytree(REPO / "engine-versions", repo / "engine-versions")
            index = load_engine_index(repo)
            index["releases"][0]["images"] = ["registry.example.org/rhoai/vllm:3.3"]
            (repo / "engine-versions/index.yaml").write_text(yaml.safe_dump(index))
            self.assertEqual(load_engine_index(repo), index)
