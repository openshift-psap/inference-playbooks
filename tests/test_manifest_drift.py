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
from check_manifests import check_recipe
from render import render_recipe


class ManifestDriftTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        for directory in ("schema", "templates", "models"):
            shutil.copytree(REPO / directory, self.repo / directory)
        self.recipe = next(self.repo.glob("models/gemma-4/recipes/*/recipe.yaml"))
        self.directory = self.recipe.parent
        _, errors = render_recipe(self.repo, self.recipe)
        self.assertFalse(errors)
        self.output = next((self.directory / "manifests").glob("*/*.yaml"))

    def test_clean_and_stale_preserve_contributor_files(self):
        self.assertFalse(check_recipe(self.repo, self.directory))
        self.output.write_text("stale\n")
        self.assertIn("Manifest drift", "\n".join(check_recipe(self.repo, self.directory)))
        self.assertEqual(self.output.read_text(), "stale\n")

    def test_missing_output(self):
        self.output.unlink()
        self.assertTrue(check_recipe(self.repo, self.directory))

    def test_pinned_source_and_destination(self):
        recipe = yaml.safe_load(self.recipe.read_text())
        source = self.directory / "manifests/source/custom.yaml"
        source.parent.mkdir(parents=True)
        source.write_text("kind: ConfigMap\nmetadata: {name: pinned}\n")
        recipe["platforms"][0].update(pinned_manifest="manifests/source/custom.yaml", pinned_reason="test")
        self.output.unlink()  # The previous generated deployment is now obsolete.
        self.recipe.write_text(yaml.safe_dump(recipe))
        self.assertTrue(check_recipe(self.repo, self.directory))
        render_recipe(self.repo, self.recipe)
        self.assertFalse(check_recipe(self.repo, self.directory))
        source.write_text("kind: ConfigMap\nmetadata: {name: changed}\n")
        self.assertTrue(check_recipe(self.repo, self.directory))

    def configure_overlay(self):
        recipe = yaml.safe_load(self.recipe.read_text())
        recipe["serving"]["config_overrides"] = True
        self.recipe.write_text(yaml.safe_dump(recipe))
        (self.directory / "config/kustomization.yaml").write_text("resources: []\n")

    def test_failed_and_missing_kustomize_fail(self):
        self.configure_overlay()
        with patch("render.subprocess.run", side_effect=FileNotFoundError("kustomize")):
            self.assertIn("kustomize", "\n".join(check_recipe(self.repo, self.directory)))
        with patch("render.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "bad overlay")):
            self.assertIn("bad overlay", "\n".join(check_recipe(self.repo, self.directory)))

    def test_final_overlay_bytes_are_compared(self):
        self.configure_overlay()
        final = "kind: Deployment\nmetadata: {name: overlay}\n"
        with patch("render.run_kustomize", return_value=final):
            render_recipe(self.repo, self.recipe)
            self.assertFalse(check_recipe(self.repo, self.directory))
            self.output.write_text("stale\n")
            self.assertTrue(check_recipe(self.repo, self.directory))

    def test_stale_cli_exits_nonzero(self):
        self.output.write_text("stale\n")
        result = subprocess.run([sys.executable, str(REPO / "tools/check_manifests.py"), "--repo", str(self.repo), "--all"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("regenerate", result.stderr)

    def test_obsolete_platform_output_is_detected(self):
        obsolete = self.directory / "manifests/vllm-old/deployment.yaml"
        obsolete.parent.mkdir(parents=True)
        obsolete.write_text(self.output.read_text())
        self.assertIn("Obsolete", "\n".join(check_recipe(self.repo, self.directory)))

    def test_cached_check_uses_staged_templates_and_manifests(self):
        def git(*arguments):
            return subprocess.run(["git", "-C", str(self.repo), *arguments], check=True, capture_output=True)
        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.org")
        git("add", ".")
        git("commit", "-qm", "initial")
        template = self.repo / "templates/vllm/deployment.yaml.j2"
        original = template.read_text()
        template.write_text(original + "# staged drift\n")
        git("add", "templates/vllm/deployment.yaml.j2")
        template.write_text(original)  # Working tree is clean; index is not.
        command = [sys.executable, str(REPO / "tools/check_manifests.py"), "--repo", str(self.repo), "--base", "HEAD", "--cached"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Manifest drift", result.stderr)
        # Stage the matching output, then leave an unstaged stale output.
        self.output.write_text(self.output.read_text() + "# staged drift\n")
        git("add", str(self.output.relative_to(self.repo)))
        self.output.write_text("unstaged stale\n")
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "unstaged stale\n")
