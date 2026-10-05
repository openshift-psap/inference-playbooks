"""Tests for Recipe v4 template rendering."""

import sys
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from render import (
    TEMPLATE_MAP,
    build_template_context,
    render_template,
    sanitize_name,
    select_template,
)


def make_recipe(
    stack="vllm",
    version="v0.24.0",
    mode="tp",
    tp=1,
    pp=1,
    dp=1,
    args=None,
    env=None,
    port=8000,
    resources=None,
    router=None,
    decode=None,
    prefill=None,
):
    serving = {
        "image": "vllm/vllm-openai:v0.24.0",
        "model": "RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic",
        "parallelism": {"mode": mode, "tp": tp, "pp": pp, "dp": dp},
        "args": args or [
            {"flag": "--enable-auto-tool-choice", "required": True, "why": "Tool calling."},
        ],
        "port": port,
    }
    if env:
        serving["env"] = env
    if resources:
        serving["resources"] = resources
    if router:
        serving["router"] = router
    if decode:
        serving["decode"] = decode
    if prefill:
        serving["prefill"] = prefill
    return {
        "schema_version": 4,
        "recipe_id": "gemma-4-tp1-tool-calling",
        "model_id": "gemma-4",
        "platforms": [{"stack": stack, "version": version, "overrides": f"platforms/{stack}-{version}.yaml"}],
        "hardware_profile": "hardware-profiles/h200.yaml",
        "workload_profile": "guidellm-8k1k",
        "deployment_mode": "tp1-tool-calling",
        "optimization_intent": "latency",
        "maturity": "day-zero",
        "deployment": {"scope": "single-node"},
        "serving": serving,
    }


class TemplateSelectionTests(unittest.TestCase):
    """Test template selection from (platform, mode) pairs."""

    def test_vllm_tp_selects_deployment(self):
        self.assertEqual(select_template("vllm", "tp"), "vllm/deployment.yaml.j2")

    def test_vllm_dp_selects_deployment(self):
        self.assertEqual(select_template("vllm", "dp"), "vllm/deployment.yaml.j2")

    def test_vllm_tp_dp_selects_deployment(self):
        self.assertEqual(select_template("vllm", "tp+dp"), "vllm/deployment.yaml.j2")

    def test_vllm_pp_selects_lws(self):
        self.assertEqual(select_template("vllm", "pp"), "vllm/lws.yaml.j2")

    def test_vllm_tp_pp_selects_lws(self):
        self.assertEqual(select_template("vllm", "tp+pp"), "vllm/lws.yaml.j2")

    def test_rhoai_tp_selects_llmisvc(self):
        self.assertEqual(select_template("rhoai", "tp"), "rhoai/llmisvc.yaml.j2")

    def test_rhoai_pp_selects_llmisvc_pp(self):
        self.assertEqual(select_template("rhoai", "pp"), "rhoai/llmisvc-pp.yaml.j2")

    def test_rhoai_tp_pp_selects_llmisvc_pp(self):
        self.assertEqual(select_template("rhoai", "tp+pp"), "rhoai/llmisvc-pp.yaml.j2")

    def test_unsupported_raises(self):
        with self.assertRaises(ValueError):
            select_template("llm-d", "tp")

    def test_all_map_entries_have_template_files(self):
        for key, path in TEMPLATE_MAP.items():
            template_file = REPO / "templates" / path
            self.assertTrue(
                template_file.is_file(),
                f"Template {path} for {key} does not exist",
            )


class SanitizeNameTests(unittest.TestCase):

    def test_simple_name(self):
        self.assertEqual(sanitize_name("gemma-4-tp1"), "gemma-4-tp1")

    def test_dots_and_underscores(self):
        result = sanitize_name("glm_5.2_pp2")
        self.assertTrue(all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in result))

    def test_max_length(self):
        result = sanitize_name("a" * 100)
        self.assertLessEqual(len(result), 63)


class BuildContextTests(unittest.TestCase):
    """Test template context building."""

    def test_basic_tp_context(self):
        recipe = make_recipe(tp=1)
        context = build_template_context(recipe, {}, None)
        self.assertEqual(context["tp"], 1)
        self.assertEqual(context["pp"], 1)
        self.assertEqual(context["dp"], 1)
        self.assertEqual(context["gpu_count"], 1)
        self.assertEqual(context["replicas"], 1)
        self.assertEqual(context["port"], 8000)
        self.assertEqual(context["kind"], "Deployment")

    def test_tp8_gpu_count(self):
        recipe = make_recipe(tp=8)
        context = build_template_context(recipe, {}, None)
        self.assertEqual(context["gpu_count"], 8)

    def test_dp_gpu_count_and_replicas(self):
        recipe = make_recipe(mode="dp", dp=4)
        context = build_template_context(recipe, {}, None)
        self.assertEqual(context["replicas"], 1)
        self.assertEqual(context["gpu_count"], 4)

    def test_pp_context_lws(self):
        recipe = make_recipe(mode="tp+pp", tp=8, pp=2)
        context = build_template_context(recipe, {}, None)
        self.assertEqual(context["kind"], "LeaderWorkerSet")
        self.assertEqual(context["pp"], 2)
        self.assertEqual(context["tp"], 8)

    def test_rhoai_context(self):
        recipe = make_recipe(stack="rhoai", version="3.5")
        context = build_template_context(recipe, {}, None)
        self.assertEqual(context["kind"], "LLMInferenceService")
        self.assertEqual(context["stack"], "rhoai")

    def test_env_passed_through(self):
        recipe = make_recipe(env=[{"name": "NCCL_DEBUG", "value": "INFO"}])
        context = build_template_context(recipe, {}, None)
        self.assertEqual(len(context["env"]), 1)
        self.assertEqual(context["env"][0]["name"], "NCCL_DEBUG")

    def test_resources_defaults(self):
        recipe = make_recipe()
        context = build_template_context(recipe, {}, None)
        self.assertIn("requests", context["resources"])
        self.assertIn("limits", context["resources"])

    def test_custom_resources(self):
        recipe = make_recipe(resources={
            "requests": {"cpu": "16", "memory": "128Gi"},
            "limits": {"cpu": "16", "memory": "256Gi"},
        })
        context = build_template_context(recipe, {}, None)
        self.assertEqual(context["resources"]["requests"]["cpu"], "16")

    def test_role_args_resolution(self):
        recipe = make_recipe(
            mode="tp+pp",
            tp=8,
            pp=2,
            decode={
                "leader_args": [
                    {"flag": "--leader-flag", "value": "1", "required": True, "why": "Leader."},
                ],
                "worker_args": [
                    {"flag": "--worker-flag", "value": "1", "required": True, "why": "Worker."},
                ],
            },
        )
        context = build_template_context(recipe, {}, None)
        leader_flags = [a["flag"] for a in context["leader_args"]]
        worker_flags = [a["flag"] for a in context["worker_args"]]
        self.assertIn("--leader-flag", leader_flags)
        self.assertNotIn("--worker-flag", leader_flags)
        self.assertIn("--worker-flag", worker_flags)
        self.assertNotIn("--leader-flag", worker_flags)


class RenderTemplateTests(unittest.TestCase):
    """Test actual Jinja2 template rendering."""

    def test_vllm_deployment_renders(self):
        recipe = make_recipe(tp=1)
        context = build_template_context(recipe, {}, None)
        rendered = render_template("vllm/deployment.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        self.assertEqual(parsed["kind"], "Deployment")
        self.assertEqual(parsed["apiVersion"], "apps/v1")
        container = parsed["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], "vllm/vllm-openai:v0.24.0")
        self.assertIn("--enable-auto-tool-choice", container["args"])

    def test_vllm_deployment_tp8_has_shm(self):
        recipe = make_recipe(tp=8)
        context = build_template_context(recipe, {}, None)
        rendered = render_template("vllm/deployment.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        volumes = parsed["spec"]["template"]["spec"].get("volumes", [])
        volume_names = [v["name"] for v in volumes]
        self.assertIn("dshm", volume_names)
        container = parsed["spec"]["template"]["spec"]["containers"][0]
        mount_names = [m["name"] for m in container.get("volumeMounts", [])]
        self.assertIn("dshm", mount_names)

    def test_vllm_deployment_tp1_no_shm(self):
        recipe = make_recipe(tp=1)
        context = build_template_context(recipe, {}, None)
        rendered = render_template("vllm/deployment.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        volumes = parsed["spec"]["template"]["spec"].get("volumes", [])
        volume_names = [v["name"] for v in volumes]
        self.assertNotIn("dshm", volume_names)
        self.assertIn("hf-cache", volume_names)
        self.assertIn("tmp", volume_names)

    def test_vllm_deployment_with_env(self):
        recipe = make_recipe(env=[{"name": "NCCL_DEBUG", "value": "INFO"}])
        context = build_template_context(recipe, {}, None)
        rendered = render_template("vllm/deployment.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        container = parsed["spec"]["template"]["spec"]["containers"][0]
        env_names = [e["name"] for e in container["env"]]
        self.assertIn("HF_TOKEN", env_names)
        self.assertIn("NCCL_DEBUG", env_names)

    def test_vllm_deployment_gpu_resources(self):
        recipe = make_recipe(tp=4)
        context = build_template_context(recipe, {}, None)
        rendered = render_template("vllm/deployment.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        container = parsed["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["resources"]["requests"]["nvidia.com/gpu"], "4")
        self.assertEqual(container["resources"]["limits"]["nvidia.com/gpu"], "4")

    def test_vllm_deployment_dp_gpu_count(self):
        recipe = make_recipe(mode="dp", dp=4)
        context = build_template_context(recipe, {}, None)
        rendered = render_template("vllm/deployment.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        self.assertEqual(parsed["spec"]["replicas"], 1)
        container = parsed["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["resources"]["requests"]["nvidia.com/gpu"], "4")
        self.assertIn("--data-parallel-size=4", container["args"])

    def test_vllm_deployment_security_context(self):
        recipe = make_recipe()
        context = build_template_context(recipe, {}, None)
        rendered = render_template("vllm/deployment.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        pod_sec = parsed["spec"]["template"]["spec"]["securityContext"]
        self.assertTrue(pod_sec["runAsNonRoot"])
        self.assertEqual(pod_sec["runAsUser"], 65534)

    def test_vllm_deployment_probes(self):
        recipe = make_recipe(port=8080)
        context = build_template_context(recipe, {}, None)
        rendered = render_template("vllm/deployment.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        container = parsed["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["startupProbe"]["httpGet"]["port"], 8080)
        self.assertEqual(container["livenessProbe"]["httpGet"]["port"], 8080)
        self.assertEqual(container["readinessProbe"]["httpGet"]["port"], 8080)

    def test_vllm_deployment_args_with_values(self):
        recipe = make_recipe(args=[
            {"flag": "--max-model-len", "value": "16384", "required": True, "why": "Memory."},
            {"flag": "--enable-prefix-caching", "required": True, "why": "Cache."},
        ])
        context = build_template_context(recipe, {}, None)
        rendered = render_template("vllm/deployment.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        container = parsed["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("--max-model-len=16384", container["args"])
        self.assertIn("--enable-prefix-caching", container["args"])

    def test_vllm_lws_renders(self):
        recipe = make_recipe(mode="tp+pp", tp=8, pp=2)
        context = build_template_context(recipe, {}, None)
        rendered = render_template("vllm/lws.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        self.assertEqual(parsed["kind"], "LeaderWorkerSet")
        self.assertEqual(parsed["spec"]["leaderWorkerTemplate"]["size"], 2)

    def test_vllm_lws_leader_worker_split(self):
        recipe = make_recipe(
            mode="tp+pp",
            tp=8,
            pp=2,
            decode={
                "leader_args": [
                    {"flag": "--leader-only", "required": True, "why": "Leader."},
                ],
                "worker_args": [
                    {"flag": "--worker-only", "required": True, "why": "Worker."},
                ],
            },
        )
        context = build_template_context(recipe, {}, None)
        rendered = render_template("vllm/lws.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        leader_args = parsed["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["spec"]["containers"][0]["args"][0]
        worker_args = parsed["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]["containers"][0]["args"][0]
        self.assertIn("--leader-only", leader_args)
        self.assertNotIn("--worker-only", leader_args)
        self.assertIn("--worker-only", worker_args)
        self.assertNotIn("--leader-only", worker_args)

    def test_rhoai_llmisvc_renders(self):
        recipe = make_recipe(stack="rhoai", version="3.5")
        context = build_template_context(recipe, {}, None)
        rendered = render_template("rhoai/llmisvc.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        self.assertEqual(parsed["kind"], "LLMInferenceService")
        self.assertEqual(parsed["apiVersion"], "serving.kserve.io/v1alpha2")

    def test_rhoai_llmisvc_pp_renders(self):
        recipe = make_recipe(stack="rhoai", version="3.5", mode="tp+pp", tp=8, pp=2)
        context = build_template_context(recipe, {}, None)
        rendered = render_template("rhoai/llmisvc-pp.yaml.j2", context)
        parsed = yaml.safe_load(rendered)
        self.assertEqual(parsed["kind"], "LLMInferenceService")
        self.assertEqual(parsed["spec"]["parallelism"]["pipeline"], 2)
        self.assertEqual(parsed["spec"]["parallelism"]["tensor"], 8)
        self.assertIn("worker", parsed["spec"])


class RenderRecipeIntegrationTests(unittest.TestCase):
    """Test render_recipe end-to-end with temp directories."""

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())
        self.recipe_dir = (
            self.tmpdir / "models" / "gemma-4"
            / "recipes" / "h200-tp1-tool-calling"
        )
        self.recipe_dir.mkdir(parents=True)

        (self.tmpdir / "schema").mkdir()
        import shutil
        shutil.copytree(REPO / "templates", self.tmpdir / "templates")
        for schema_file in (REPO / "schema").glob("*.schema.json"):
            shutil.copy2(schema_file, self.tmpdir / "schema")
        for schema_file in (REPO / "schema").glob("*.yaml"):
            shutil.copy2(schema_file, self.tmpdir / "schema")

        (self.tmpdir / "models" / "gemma-4" / "model.yaml").write_text(yaml.dump({
            "schema_version": 2,
            "model_id": "gemma-4",
            "name": "Gemma 4",
            "family": "Gemma 4",
            "huggingface_id": "google/gemma-4-26B-A4B-it",
            "source": "huggingface",
            "model_type": "gemma4",
            "architectures": ["Gemma4ForConditionalGeneration"],
        }))

        platforms_dir = self.recipe_dir / "platforms"
        platforms_dir.mkdir()
        (platforms_dir / "vllm-v0.24.0.yaml").write_text("{}\n")
        (platforms_dir / "rhoai-3.5.yaml").write_text("{}\n")
        (platforms_dir / "llm-d-0.8.yaml").write_text("{}\n")

    def _write_recipe(self, recipe):
        recipe_path = self.recipe_dir / "recipe.yaml"
        recipe_path.write_text(yaml.dump(recipe, default_flow_style=False))
        return recipe_path

    def test_render_basic_tp1(self):
        from render import render_recipe
        recipe = make_recipe(tp=1)
        recipe_path = self._write_recipe(recipe)
        rendered, errors = render_recipe(self.tmpdir, recipe_path, dry_run=True)
        self.assertFalse(errors, errors)
        self.assertIn("kind: Deployment", rendered)

    def test_render_writes_manifest(self):
        from render import render_recipe
        recipe = make_recipe(tp=1)
        recipe_path = self._write_recipe(recipe)
        rendered, errors = render_recipe(self.tmpdir, recipe_path, dry_run=False)
        self.assertFalse(errors, errors)
        manifest_path = self.recipe_dir / "manifests" / "vllm-v0.24.0" / "deployment.yaml"
        self.assertTrue(manifest_path.is_file())
        content = manifest_path.read_text()
        self.assertIn("kind: Deployment", content)

    def test_render_v3_skipped(self):
        from render import render_recipe
        recipe = make_recipe()
        recipe["schema_version"] = 3
        recipe_path = self._write_recipe(recipe)
        rendered, errors = render_recipe(self.tmpdir, recipe_path, dry_run=True)
        self.assertTrue(errors)
        self.assertTrue(any("not a v4 recipe" in e for e in errors))

    def test_render_lws(self):
        from render import render_recipe
        recipe = make_recipe(mode="tp+pp", tp=8, pp=2)
        recipe_path = self._write_recipe(recipe)
        rendered, errors = render_recipe(self.tmpdir, recipe_path, dry_run=True)
        self.assertFalse(errors, errors)
        self.assertIn("kind: LeaderWorkerSet", rendered)

    def test_render_rhoai(self):
        from render import render_recipe
        recipe = make_recipe(stack="rhoai", version="3.5")
        recipe_path = self._write_recipe(recipe)
        rendered, errors = render_recipe(self.tmpdir, recipe_path, dry_run=True)
        self.assertFalse(errors, errors)
        self.assertIn("kind: LLMInferenceService", rendered)

    def test_render_blocked_platform_skipped(self):
        from render import render_recipe
        recipe = make_recipe(tp=1)
        recipe["platforms"].append({
            "stack": "rhoai", "version": "3.5",
            "overrides": "platforms/rhoai-3.5.yaml",
            "blocked": True,
            "reason": "Flag not supported",
        })
        recipe_path = self._write_recipe(recipe)
        rendered, errors = render_recipe(self.tmpdir, recipe_path, dry_run=True)
        self.assertFalse(errors, errors)
        self.assertIn("kind: Deployment", rendered)
        self.assertNotIn("LLMInferenceService", rendered)

    def test_render_pinned_manifest_copies_verbatim(self):
        from render import render_recipe
        recipe = make_recipe(tp=1)
        pinned_content = "kind: CustomResource\napiVersion: v1\nmetadata:\n  name: pinned\n"
        manifest_dir = self.recipe_dir / "manifests" / "llm-d-0.8"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "deployment.yaml").write_text(pinned_content)
        recipe["platforms"] = [
            {"stack": "vllm", "version": "v0.24.0", "overrides": "platforms/vllm-v0.24.0.yaml"},
            {
                "stack": "llm-d", "version": "0.8",
                "overrides": "platforms/llm-d-0.8.yaml",
                "pinned_manifest": "manifests/llm-d-0.8/deployment.yaml",
                "pinned_reason": "llm-d template WIP",
            },
        ]
        recipe_path = self._write_recipe(recipe)
        rendered, errors = render_recipe(self.tmpdir, recipe_path, dry_run=True)
        self.assertFalse(errors, errors)
        self.assertIn("kind: Deployment", rendered)
        self.assertIn("kind: CustomResource", rendered)

    def test_render_pinned_manifest_missing_file_errors(self):
        from render import render_recipe
        recipe = make_recipe(tp=1)
        recipe["platforms"] = [{
            "stack": "llm-d", "version": "0.8",
            "overrides": "platforms/llm-d-0.8.yaml",
            "pinned_manifest": "manifests/llm-d-0.8/deployment.yaml",
            "pinned_reason": "llm-d template WIP",
        }]
        recipe_path = self._write_recipe(recipe)
        rendered, errors = render_recipe(self.tmpdir, recipe_path, dry_run=True)
        self.assertTrue(errors)
        self.assertTrue(any("pinned_manifest does not exist" in e for e in errors))

    def test_render_pinned_manifest_writes_file(self):
        from render import render_recipe
        recipe = make_recipe(tp=1)
        pinned_content = "kind: PinnedDeploy\napiVersion: v1\n"
        manifest_dir = self.recipe_dir / "manifests" / "llm-d-0.8"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "custom.yaml").write_text(pinned_content)
        recipe["platforms"] = [{
            "stack": "llm-d", "version": "0.8",
            "overrides": "platforms/llm-d-0.8.yaml",
            "pinned_manifest": "manifests/llm-d-0.8/custom.yaml",
            "pinned_reason": "llm-d template WIP",
        }]
        recipe_path = self._write_recipe(recipe)
        rendered, errors = render_recipe(self.tmpdir, recipe_path, dry_run=False)
        self.assertFalse(errors, errors)
        output_path = self.recipe_dir / "manifests" / "llm-d-0.8" / "custom.yaml"
        self.assertTrue(output_path.is_file())
        self.assertEqual(output_path.read_text(), pinned_content)

    def test_render_all_blocked_produces_no_output(self):
        from render import render_recipe
        recipe = make_recipe(tp=1)
        recipe["platforms"] = [{
            "stack": "vllm", "version": "v0.24.0",
            "overrides": "platforms/vllm-v0.24.0.yaml",
            "blocked": True,
            "reason": "Temporary block",
        }]
        recipe_path = self._write_recipe(recipe)
        rendered, errors = render_recipe(self.tmpdir, recipe_path, dry_run=True)
        self.assertFalse(errors, errors)
        self.assertEqual(rendered, "")


if __name__ == "__main__":
    unittest.main()
