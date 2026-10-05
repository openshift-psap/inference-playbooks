import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.ci_baseline import git, resolve_baseline
from tools.recipe_evidence import affected_recipes, changed_paths

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
from engine_versions import baseline_images, engine_errors


class CiBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        self.commit("initial", "initial")
        self.stale = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-qb", "pr")
        self.commit("models/demo/recipes/new/recipe.yaml", "hardware_profile: hardware-profiles/gpu.yaml\n")
        self.pr = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "main")
        self.commit("models/demo/recipes/inherited/recipe.yaml", "hardware_profile: hardware-profiles/gpu.yaml\n")
        self.main = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "merge", "--no-ff", "-qm", "synthetic PR merge", "pr")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.event = {"number": 17, "pull_request": {"head": {"sha": self.pr}, "base": {"sha": self.stale}}}

    def commit(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", path)

    def resolve(self, **kwargs):
        args = dict(event_name="pull_request", event=self.event, sha=self.head, ref="refs/pull/17/merge")
        args.update(kwargs)
        return resolve_baseline(self.repo, **args)

    def test_stale_event_base_reproduces_false_addition(self):
        paths = changed_paths(self.repo, self.stale, self.head, False)
        self.assertIn(("A", ["models/demo/recipes/inherited/recipe.yaml"]), paths)

    def test_actual_merge_parent_excludes_inherited_recipe(self):
        comparison = self.resolve()
        self.assertEqual(comparison, {"base": self.main, "head": self.head})
        self.assertEqual(changed_paths(self.repo, comparison["base"], comparison["head"], False),
                         [("A", ["models/demo/recipes/new/recipe.yaml"])])
        self.assertEqual(affected_recipes(self.repo, comparison["base"], comparison["head"], False)["recipes"],
                         ["models/demo/recipes/new"])

    def test_push_preserves_before(self):
        self.assertEqual(self.resolve(event_name="push", event={"before": self.stale}, ref="refs/heads/main"),
                         {"base": self.stale, "head": self.head})

    def test_rejects_branch_checkout(self):
        with self.assertRaisesRegex(ValueError, "synthetic merge"):
            self.resolve(ref="refs/heads/pr")

    def test_rejects_wrong_pr_parent(self):
        self.event["pull_request"]["head"]["sha"] = self.stale
        with self.assertRaisesRegex(ValueError, "expected two-parent"):
            self.resolve()

    def test_rejects_checkout_mismatch(self):
        with self.assertRaisesRegex(ValueError, "HEAD differs"):
            self.resolve(sha=self.pr)

    def test_rejects_non_merge(self):
        git(self.repo, "checkout", "-q", "pr")
        with self.assertRaisesRegex(ValueError, "expected two-parent"):
            self.resolve(sha=self.pr)

    def test_rejects_missing_push_baseline(self):
        for before in ["", "0" * 40, "HEAD", "f" * 40]:
            with self.subTest(before=before), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                self.resolve(event_name="push", event={"before": before})

    def test_rejects_other_event(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.resolve(event_name="workflow_dispatch")

    def test_cli_emits_shared_comparison_outputs(self):
        event_path = self.repo / "event.json"
        event_path.write_text(json.dumps(self.event))
        output = self.repo / "output"
        subprocess.run(
            [sys.executable, str(REPO / "tools/ci_baseline.py")], cwd=self.repo,
            env={**os.environ, "GITHUB_EVENT_NAME": "pull_request",
                 "GITHUB_EVENT_PATH": str(event_path), "GITHUB_SHA": self.head,
                 "GITHUB_REF": "refs/pull/17/merge", "GITHUB_OUTPUT": str(output)},
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(output.read_text(), f"base={self.main}\nhead={self.head}\n")

    def test_correct_baseline_keeps_new_and_replaced_image_enforcement(self):
        # A legacy image added by main must remain legacy, while a genuinely new
        # recipe or replaced image still requires metadata in the validator.
        git(self.repo, "checkout", "-qb", "metadata-main", self.main)
        recipe_path = "models/demo/recipes/inherited/recipe.yaml"
        image = "quay.io/example/vllm:1"
        self.commit(recipe_path, f"serving:\n  image: {image}\nplatforms:\n  - stack: vllm\n    version: '1'\n    overrides: platforms/vllm.yaml\n")
        self.commit("models/demo/recipes/inherited/platforms/vllm.yaml", "{}\n")
        self.main = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "merge", "--no-ff", "-qm", "synthetic PR merge", "pr")
        self.head = git(self.repo, "rev-parse", "HEAD")
        comparison = self.resolve()
        platform = {"stack": "vllm", "version": "1"}
        key = ("vllm", "1")
        index = {"releases": [], "variants": []}
        for path, candidate, expected_error in [
            (recipe_path, image, False),
            ("models/demo/recipes/new/recipe.yaml", image, True),
            (recipe_path, "quay.io/example/vllm:2", True),
        ]:
            with self.subTest(path=path, image=candidate):
                previous = baseline_images(self.repo, self.repo / path, comparison["base"])
                self.assertEqual(bool(engine_errors(
                    {"image": candidate}, {}, platform, index,
                    require_metadata=previous.get(key) != candidate,
                )), expected_error)

    def test_shared_profile_change_still_selects_all_referencing_recipes(self):
        self.commit("hardware-profiles/gpu.yaml", "profile_revision: 1\n")
        self.main = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "pr")
        git(self.repo, "merge", "--ff-only", "-q", "main")
        self.commit("hardware-profiles/gpu.yaml", "profile_revision: 2\n")
        self.pr = git(self.repo, "rev-parse", "HEAD")
        self.event["pull_request"]["head"]["sha"] = self.pr
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "--no-ff", "-qm", "profile update merge", "pr")
        self.head = git(self.repo, "rev-parse", "HEAD")
        comparison = self.resolve()
        recipes = affected_recipes(self.repo, comparison["base"], comparison["head"], False)["recipes"]
        self.assertEqual(recipes, ["models/demo/recipes/inherited", "models/demo/recipes/new"])
