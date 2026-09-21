import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "recipe_evidence.py"


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
        git(directory, "init", "-q")
        git(directory, "config", "user.email", "test@example.com")
        git(directory, "config", "user.name", "Test User")
        profile = directory / "hardware-profiles" / "h200-r1.yaml"
        profile.parent.mkdir(parents=True)
        profile.write_text(self.profile_text.format(revision=1, correction=""))
        recipe = directory / "models" / "glm" / "rhoai" / "3.5" / "recipes" / "h200-r1" / "agentic" / "recipe.yaml"
        recipe.parent.mkdir(parents=True)
        recipe.write_text("recipe_id: agentic\nhardware_profile: hardware-profiles/h200-r1.yaml\n")
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

    def test_recipe_change_selects_only_that_recipe(self):
        directory, _, recipe = self.make_repo()
        recipe.write_text("recipe_id: agentic\nmaturity: validated\n")
        git(directory, "add", ".")
        result = self.run_tool(directory, "--cached", "affected-recipes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"all": False, "recipes": ["models/glm/rhoai/3.5/recipes/h200-r1/agentic"]},
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
            {"all": False, "recipes": ["models/glm/rhoai/3.5/recipes/h200-r1/agentic"]},
        )

    def test_recipe_schema_requires_canonical_deployment_scope(self):
        schema = json.loads((REPO / "schema" / "recipe.schema.json").read_text())
        recipe = {
            "schema_version": 3,
            "recipe_id": "glm-agentic",
            "model_id": "glm",
            "platform": {"stack": "rhoai", "version": "3.5"},
            "hardware_profile": "hardware-profiles/h200-r1.yaml",
            "workload_profile": "agentic",
            "maturity": "day-zero",
            "deployment": {"scope": "single-node"},
        }
        validator = Draft202012Validator(schema)
        self.assertFalse(list(validator.iter_errors(recipe)))

        missing_scope = {**recipe, "deployment": {}}
        self.assertTrue(list(validator.iter_errors(missing_scope)))

        legacy_nodes = {**recipe, "match": {"nodes": "single"}}
        self.assertTrue(list(validator.iter_errors(legacy_nodes)))


if __name__ == "__main__":
    unittest.main()
