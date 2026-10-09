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
        self.assertEqual(select_template("vllm", "tp", "single-node"), "vllm/deployment.yaml.j2")

    def test_vllm_dp_selects_deployment(self):
        self.assertEqual(select_template("vllm", "dp", "single-node"), "vllm/deployment.yaml.j2")

    def test_vllm_tp_dp_selects_deployment(self):
        self.assertEqual(select_template("vllm", "tp+dp", "single-node"), "vllm/deployment.yaml.j2")

    def test_vllm_pp_selects_lws(self):
        self.assertEqual(select_template("vllm", "pp", "single-node"), "vllm/lws.yaml.j2")

    def test_vllm_tp_pp_selects_lws(self):
        self.assertEqual(select_template("vllm", "tp+pp", "single-node"), "vllm/lws.yaml.j2")

    def test_rhoai_tp_selects_llmisvc(self):
        self.assertEqual(select_template("rhoai", "tp", "single-node"), "rhoai/llmisvc.yaml.j2")

    def test_rhoai_pp_selects_llmisvc_pp(self):
        self.assertEqual(select_template("rhoai", "pp", "single-node"), "rhoai/llmisvc-pp.yaml.j2")

    def test_rhoai_tp_pp_selects_llmisvc_pp(self):
        self.assertEqual(select_template("rhoai", "tp+pp", "single-node"), "rhoai/llmisvc-pp.yaml.j2")

    def test_unsupported_raises(self):
        with self.assertRaises(ValueError):
            select_template("llm-d", "tp", "single-node")

    def test_multi_node_tp_dp_selects_its_own_lws_template(self):
        from render import component_kind
        self.assertEqual(select_template("vllm", "tp+dp", "multi-node"), "vllm/dp-lws.yaml.j2")
        self.assertEqual(component_kind("vllm", "tp+dp", "multi-node"), "LeaderWorkerSet")
        self.assertEqual(component_kind("vllm", "tp+dp", "single-node"), "Deployment")

    def test_unsupported_scopes_have_no_deployment_fallback(self):
        for stack, mode, scope in (("vllm", "tp", "multi-node"), ("vllm", "dp", "multi-node"),
                                   ("vllm", "tp+dp", None), ("vllm", "tp", "unknown")):
            with self.subTest(stack=stack, mode=mode, scope=scope), self.assertRaisesRegex(ValueError, "scope="):
                select_template(stack, mode, scope)

    def test_pp_and_rhoai_selection_preserved_in_both_scopes(self):
        for scope in ("single-node", "multi-node"):
            for mode in ("pp", "tp+pp"):
                self.assertEqual(select_template("vllm", mode, scope), "vllm/lws.yaml.j2")
                self.assertEqual(select_template("rhoai", mode, scope), "rhoai/llmisvc-pp.yaml.j2")
            self.assertEqual(select_template("rhoai", "tp", scope), "rhoai/llmisvc.yaml.j2")

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
        recipe["platforms"].append({"stack": "vllm", "version": "v0.24.0", "overrides": "platforms/vllm-v0.24.0.yaml"})
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
        recipe["deployment"]["scope"] = "multi-node"  # Test pin I/O, not automatic companion conversion.
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
        recipe["deployment"]["scope"] = "multi-node"
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


class SingleNodeCompanionTests(unittest.TestCase):
    """Synthetic policy/prepare/render/drift contract, never a production recipe."""

    def setUp(self):
        import shutil
        import tempfile
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        for directory in ("schema", "templates"):
            shutil.copytree(REPO / directory, self.repo / directory)
        (self.repo / "engine-versions").mkdir()
        self.write(self.repo / "engine-versions/index.yaml", {
            "schema_version": 1, "purpose": "default-runtime-engine-resolution",
            "variants": ["cuda", "rocm", "cpu"],
            "source": {"title": "Synthetic fixture", "export": "2026.html", "sha256": "0" * 64},
            "releases": [{"version": "9.0.0", "vllm_version": "1.2.3"}],
        })
        self.write(self.repo / "hardware-profiles/fixture.yaml", {
            "schema_version": 1, "profile_id": "fixture", "profile_revision": 1,
            "kind": "hardware-profile", "accelerator_key": "nvidia-fixture-x8",
            "accelerators": {"vendor": "nvidia", "model": "fixture", "count_per_node": 8},
            "correction_log": [{"revision": 1, "summary": "Synthetic fixture."}],
        })
        self.write(self.repo / "models/fixture/model.yaml", {
            "schema_version": 2, "model_id": "fixture", "name": "Fixture", "family": "Fixture",
            "huggingface_id": "example/checkpoint", "model_type": "fixture",
            "architectures": ["FixtureForCausalLM"], "source": "manual", "quantizations": [{"name": "fp16"}],
        })
        self.path = self.repo / "models/fixture/recipes/fixture-tp/recipe.yaml"
        self.recipe = {
            "schema_version": 4, "recipe_id": "fixture-tp", "model_id": "fixture",
            "platforms": [{"stack": "rhoai", "version": "9.0.0", "overrides": "platforms/rhoai-9.0.0.yaml"}],
            "hardware_profile": "hardware-profiles/fixture.yaml", "workload_profile": "guidellm-8k1k",
            "deployment_mode": "tp2", "optimization_intent": "latency", "maturity": "contributed",
            "deployment": {"scope": "single-node", "status": {
                "state": "verified", "date": "2026-10-07", "method": "Synthetic source only."}},
            "serving": {
                "image": "registry.example.org/runtime:fixture", "image_usage": {"kind": "default", "variant": "cuda"},
                "model": "example/checkpoint", "parallelism": {"mode": "tp", "tp": 2},
                "args": [{"flag": "--speculative-config", "value": '{"method":"mtp","num_speculative_tokens":3}',
                          "required": True, "why": "Synthetic MTP quoting proof."}],
                "env": [{"name": "BASE", "value": "one"}],
                "resources": {"requests": {"cpu": "4", "memory": "16Gi"}, "limits": {"cpu": "8", "memory": "32Gi"}},
            },
        }
        self.write(self.path, self.recipe)
        self.source = self.path.parent / "platforms/rhoai-9.0.0.yaml"
        self.write(self.source, {"env": [{"name": "SOURCE", "value": "two"}]})

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def load(self):
        return yaml.safe_load(self.path.read_text())

    def prepare(self, **kwargs):
        from prepare_companions import prepare
        return prepare(self.repo, self.path, **kwargs)

    def test_prepare_render_validate_and_drift_end_to_end(self):
        import subprocess
        from check_manifests import check_recipe
        from render import render_recipe
        self.assertFalse(self.prepare())
        prepared = self.load()
        companion = prepared["platforms"][-1]
        self.assertEqual(companion["version"], "v1.2.3")
        self.assertEqual(prepared["maturity"], "contributed")
        self.assertEqual(prepared["deployment"]["status"]["state"], "verified")
        self.assertEqual(companion["verification"]["maturity"], "day-zero")
        self.assertEqual(companion["verification"]["deployment_status"]["state"], "needs-verification")
        self.assertEqual(companion["verification"]["benchmark_runs"], [])
        before = self.path.read_bytes()
        rendered, errors = render_recipe(self.repo, self.path)
        self.assertFalse(errors, errors)
        self.assertEqual(self.path.read_bytes(), before)  # Renderer never writes inputs.
        documents = list(yaml.safe_load_all(rendered))
        self.assertEqual({d["kind"] for d in documents}, {"LLMInferenceService", "Deployment"})
        deployment = next(d for d in documents if d["kind"] == "Deployment")
        spec = deployment["spec"]["template"]["spec"]
        container = spec["containers"][0]
        self.assertEqual(container["image"], self.recipe["serving"]["image"])
        self.assertEqual(container["args"][0], "example/checkpoint")
        self.assertIn('--speculative-config={"method":"mtp","num_speculative_tokens":3}', container["args"])
        self.assertIn("--tensor-parallel-size=2", container["args"])
        self.assertEqual(container["resources"]["requests"]["memory"], "16Gi")
        self.assertEqual(container["resources"]["limits"]["nvidia.com/gpu"], "2")
        self.assertEqual([e["name"] for e in container["env"]], ["HF_TOKEN", "BASE", "SOURCE"])
        self.assertEqual(next(v for v in spec["volumes"] if v["name"] == "dshm")["emptyDir"], {"medium": "Memory", "sizeLimit": "4Gi"})
        self.assertFalse(check_recipe(self.repo, self.path.parent))
        command = [sys.executable, str(REPO / "tools/validate.py"), "--repo", str(self.repo), "--current"]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.path.parent / "manifests/vllm-v1.2.3/deployment.yaml"
        output.write_text("stale\n")
        self.assertIn("Manifest drift", "\n".join(check_recipe(self.repo, self.path.parent)))
        self.assertEqual(output.read_text(), "stale\n")

    def configure_local_weights(self, mount="/mnt/models", model_path=None):
        self.recipe["deployment"]["storage"] = {"type": "pvc", "pvc": {
            "name": "fixture-weights", "mount_path": mount, "model_path": model_path or mount,
            "read_only": True, "size": "500Gi", "storage_class": "deployment-selected",
            "access_modes": ["ReadWriteOnce"],
        }}
        self.recipe["serving"].update(served_model_name="fixture-served-alias", shared_memory={"size": "16Gi"},
            probes={"startup": {"path": "/health", "port": 8000, "failure_threshold": 180, "period_seconds": 10},
                    "readiness": {"path": "/v1/models", "port": 8000, "period_seconds": 10, "timeout_seconds": 7}})
        self.recipe["serving"]["resources"] = {"requests": {"cpu": "32", "memory": "512Gi", "nvidia.com/gpu": "2"},
                                               "limits": {"memory": "512Gi", "nvidia.com/gpu": "2"}}
        self.write(self.path, self.recipe)

    def test_declarative_local_weights_alias_shm_probes_roundtrip(self):
        import json
        import subprocess
        from check_manifests import check_recipe
        from render import render_recipe
        self.configure_local_weights()
        source_bytes = self.source.read_bytes()
        serving_before = self.load()["serving"]
        self.assertFalse(self.prepare())
        self.assertEqual(self.load()["serving"], serving_before)
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertEqual(self.load()["platforms"][-1]["verification"]["benchmark_runs"], [])
        self.assertIsNone(self.load()["platforms"][-1]["config"])
        before = {p: p.read_bytes() for p in self.path.parent.rglob("*.yaml")}
        self.assertFalse(self.prepare())
        self.assertEqual(before, {p: p.read_bytes() for p in self.path.parent.rglob("*.yaml")})
        rendered, errors = render_recipe(self.repo, self.path)
        self.assertFalse(errors, errors)
        for document in yaml.safe_load_all(rendered):
            pod = document["spec"]["template"] if document["kind"] == "LLMInferenceService" else document["spec"]["template"]["spec"]
            container = pod["containers"][0]
            self.assertEqual(container["args"][0], "/mnt/models")
            self.assertIn("--served-model-name=fixture-served-alias", container["args"])
            self.assertEqual(container["image"], serving_before["image"])
            self.assertEqual(container["startupProbe"], {"httpGet": {"path": "/health", "port": 8000}, "periodSeconds": 10, "failureThreshold": 180})
            self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/v1/models")
            self.assertEqual(container["readinessProbe"]["timeoutSeconds"], 7)
            self.assertNotIn("cpu", container["resources"]["limits"])  # No invented 8-CPU cap below request=32.
            volume = next(v for v in pod["volumes"] if v["name"] == "model-weights")
            self.assertEqual(volume["persistentVolumeClaim"], {"claimName": "fixture-weights", "readOnly": True})
            mount = next(v for v in container["volumeMounts"] if v["name"] == "model-weights")
            self.assertEqual(mount, {"name": "model-weights", "mountPath": "/mnt/models", "readOnly": True})
            self.assertEqual(next(v for v in pod["volumes"] if v["name"] == "dshm")["emptyDir"]["sizeLimit"], "16Gi")
            command = ["/bin/bash", "-c", 'vllm(){ printf "%s\\n" "$@"; }; vllm serve "$@"', "--", *container["args"]]
            roundtrip = subprocess.run(command, check=True, text=True, capture_output=True).stdout.splitlines()
            config = next(arg.split("=", 1)[1] for arg in roundtrip if arg.startswith("--speculative-config="))
            self.assertEqual(json.loads(config), {"method": "mtp", "num_speculative_tokens": 3})
        self.assertFalse(check_recipe(self.repo, self.path.parent))
        result = subprocess.run([sys.executable, str(REPO / "tools/validate.py"), "--repo", str(self.repo), "--current"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_nondefault_mount_subdirectory_and_probe_fields(self):
        from render import render_recipe
        self.configure_local_weights("/weights", "/weights/checkpoint")
        self.recipe["serving"]["parallelism"]["tp"] = 1
        self.recipe["serving"]["resources"]["requests"]["nvidia.com/gpu"] = "1"
        self.recipe["serving"]["resources"]["limits"]["nvidia.com/gpu"] = "1"
        self.recipe["serving"]["probes"]["startup"].update(path="/startup", port=9000, initial_delay_seconds=5, timeout_seconds=11)
        self.write(self.path, self.recipe)
        self.assertFalse(self.prepare())
        rendered, errors = render_recipe(self.repo, self.path, dry_run=True)
        self.assertFalse(errors, errors)
        for document in yaml.safe_load_all(rendered):
            pod = document["spec"]["template"] if document["kind"] == "LLMInferenceService" else document["spec"]["template"]["spec"]
            container = pod["containers"][0]
            self.assertEqual(container["args"][0], "/weights/checkpoint")
            self.assertEqual(container["startupProbe"]["httpGet"], {"path": "/startup", "port": 9000})
            self.assertEqual(container["startupProbe"]["initialDelaySeconds"], 5)
            self.assertEqual(container["startupProbe"]["timeoutSeconds"], 11)
            self.assertIn("dshm", [v["name"] for v in pod["volumes"]])  # Explicit shm also works with TP1.

    def scoped_overlay(self, runtime=False):
        import shutil
        if not shutil.which("kustomize"):
            self.skipTest("Kustomize required for scoped-overlay integration")
        self.recipe["platforms"][0]["config"] = "config/rhoai"
        self.recipe["serving"]["config_overrides"] = True  # Explicit scope wins over legacy shared fallback.
        self.write(self.path, self.recipe)
        patch = [{"op": "add", "path": "/spec/template/securityContext", "value": {"runAsUser": 0}}] if runtime else [
            {"op": "add", "path": "/metadata/annotations", "value": {"kubernetes.io/description": "source-only"}}]
        self.write(self.path.parent / "config/rhoai/kustomization.yaml", {"resources": [], "patches": [{
            "path": "patch.yaml", "target": {"kind": "LLMInferenceService"}}]})
        self.write(self.path.parent / "config/rhoai/patch.yaml", patch)

    def test_scoped_metadata_overlay_never_applies_to_companion(self):
        from check_manifests import check_recipe
        from render import render_recipe
        self.configure_local_weights()
        self.scoped_overlay()
        overlay_before = (self.path.parent / "config/rhoai/patch.yaml").read_bytes()
        self.assertFalse(self.prepare())
        self.assertEqual((self.path.parent / "config/rhoai/patch.yaml").read_bytes(), overlay_before)
        rendered, errors = render_recipe(self.repo, self.path)
        self.assertFalse(errors, errors)
        source, target = list(yaml.safe_load_all(rendered))
        self.assertEqual(source["metadata"]["annotations"]["kubernetes.io/description"], "source-only")
        self.assertNotIn("annotations", target["metadata"])
        self.assertIsNone(self.load()["platforms"][-1]["config"])
        self.assertFalse(check_recipe(self.repo, self.path.parent))

    def test_scoped_runtime_security_patch_cannot_claim_mapping(self):
        self.scoped_overlay(runtime=True)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "runtime/security"):
            self.prepare()
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_admission_annotations_are_not_assumed_safe(self):
        self.scoped_overlay()
        self.write(self.path.parent / "config/rhoai/patch.yaml", [{"op": "add", "path": "/metadata/annotations",
            "value": {"sidecar.istio.io/inject": "true"}}])
        with self.assertRaisesRegex(ValueError, "runtime/security"):
            self.prepare()

    def test_automatic_overlay_rejects_remote_or_generated_resources_before_build(self):
        from unittest.mock import patch
        self.scoped_overlay()
        path = self.path.parent / "config/rhoai/kustomization.yaml"
        for customization in ({"resources": ["https://example.org/remote.yaml"]},
                              {"resources": [], "configMapGenerator": [{"name": "extra"}]}):
            self.write(path, customization)
            with patch("render.run_kustomize") as build, self.assertRaisesRegex(ValueError, "blocked|require review"):
                self.prepare()
            build.assert_not_called()

    def test_bad_pvc_paths_and_gpu_counts_block_mapping(self):
        self.configure_local_weights("/weights", "/elsewhere/checkpoint")
        with self.assertRaisesRegex(ValueError, "inside mount_path"):
            self.prepare()
        for path in ("/weights/../escape", "/tmp", "/dev", "/"):
            self.configure_local_weights(path)
            with self.subTest(path=path):
                try:
                    errors = self.prepare()
                except ValueError as error:
                    errors = [str(error)]
                self.assertTrue(errors)
        self.configure_local_weights()
        self.recipe["serving"]["resources"]["limits"]["nvidia.com/gpu"] = "7"
        self.write(self.path, self.recipe)
        with self.assertRaisesRegex(ValueError, "GPU count"):
            self.prepare()

    def test_nonlocal_rhoai_additional_args_preserve_json_shell_tokens(self):
        import json
        import shlex
        from render import render_recipe
        self.assertFalse(self.prepare())
        rendered, errors = render_recipe(self.repo, self.path, dry_run=True)
        self.assertFalse(errors, errors)
        source = next(d for d in yaml.safe_load_all(rendered) if d["kind"] == "LLMInferenceService")
        value = next(e["value"] for e in source["spec"]["template"]["containers"][0]["env"] if e["name"] == "VLLM_ADDITIONAL_ARGS")
        tokens = shlex.split(value)
        self.assertEqual(json.loads(tokens[tokens.index("--speculative-config") + 1]), {"method": "mtp", "num_speculative_tokens": 3})

    def test_old_shell_prequoting_requires_canonical_json_migration(self):
        self.write(self.source, {"args": [{"flag": "--speculative-config", "value": "'{\"method\":\"mtp\"}'",
                    "required": True, "why": "Legacy controller shell serialization."}]})
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "raw JSON object"):
            self.prepare()
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_and_escaping_scoped_overlay_paths_are_blocked(self):
        self.recipe["platforms"][0]["config"] = "config/missing"
        self.write(self.path, self.recipe)
        with self.assertRaisesRegex(ValueError, "overlay missing"):
            self.prepare()
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            (self.path.parent / "config").mkdir(exist_ok=True)
            (self.path.parent / "config/escaped").symlink_to(directory)
            self.recipe["platforms"][0]["config"] = "config/escaped"
            self.write(self.path, self.recipe)
            with self.assertRaisesRegex(ValueError, "escapes config"):
                self.prepare()

    def test_same_version_alias_probe_shm_overrides_cannot_drift(self):
        from render import render_recipe
        self.write(self.source, {"served_model_name": "source-alias", "shared_memory": {"size": "16Gi"},
                    "probes": {"startup": {"failure_threshold": 180}}})
        self.assertFalse(self.prepare())
        source = yaml.safe_load(self.source.read_text())
        source["served_model_name"] = "changed-alias"
        source["shared_memory"]["size"] = "24Gi"
        source["probes"]["startup"]["failure_threshold"] = 200
        self.write(self.source, source)
        _, errors = render_recipe(self.repo, self.path, dry_run=True)
        text = "\n".join(errors)
        for field in ("served_model_name", "shared_memory", "probes"):
            self.assertIn("stale/different " + field, text)

    def test_check_and_renderer_do_not_materialize_missing_inputs(self):
        from render import render_recipe
        before = self.path.read_bytes()
        self.assertTrue(self.prepare(check=True))
        _, errors = render_recipe(self.repo, self.path)
        self.assertIn("missing explicit", "\n".join(errors))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.path.parent / "manifests").exists())

    def test_idempotent_no_duplicate_and_no_inherited_benchmarks(self):
        self.recipe["benchmark_runs"] = ["results/source/run.yaml"]
        self.write(self.path, self.recipe)
        self.assertFalse(self.prepare())
        before = {p: p.read_bytes() for p in self.path.parent.rglob("*.yaml")}
        self.assertFalse(self.prepare())
        self.assertEqual(before, {p: p.read_bytes() for p in self.path.parent.rglob("*.yaml")})
        self.assertEqual(self.load()["platforms"][-1]["verification"]["benchmark_runs"], [])

    def test_existing_explicit_vllm_entry_is_preserved(self):
        self.recipe["platforms"].append({"stack": "vllm", "version": "v1.2.3", "overrides": "platforms/existing.yaml"})
        self.write(self.path, self.recipe)
        before = self.path.read_bytes()
        self.assertFalse(self.prepare())
        self.assertEqual(self.path.read_bytes(), before)

    def test_multi_node_unchanged(self):
        self.recipe["deployment"]["scope"] = "multi-node"
        self.write(self.path, self.recipe)
        before = self.path.read_bytes()
        self.assertFalse(self.prepare())
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_engine_blocks_without_image_tag_guessing(self):
        self.recipe["serving"].pop("image_usage")
        self.write(self.path, self.recipe)
        with self.assertRaisesRegex(ValueError, "image_usage"):
            self.prepare()

    def test_custom_image_requires_bound_declaration(self):
        self.recipe["serving"]["image_usage"] = {"kind": "custom", "note": "Fixture build."}
        self.write(self.path, self.recipe)
        with self.assertRaisesRegex(ValueError, "custom image requires"):
            self.prepare()
        self.recipe["serving"]["engine"] = {"name": "vllm", "version": "2.0.0", "image": self.recipe["serving"]["image"], "source": "https://example.org/build"}
        self.write(self.path, self.recipe)
        self.assertFalse(self.prepare())
        self.assertEqual(self.load()["platforms"][-1]["version"], "v2.0.0")

    def test_explicit_newer_requires_source_backed_target(self):
        from companions import companion_errors
        with self.assertRaisesRegex(ValueError, "requires --overrides"):
            self.prepare(version="1.3.0")
        self.write(self.path.parent / "platforms/newer.yaml", {
            "image": "registry.example.org/runtime:newer", "image_usage": {"kind": "custom", "note": "Reviewed newer build."},
            "engine": {"name": "vllm", "version": "1.3.0", "image": "registry.example.org/runtime:newer", "source": "https://example.org/build/newer"},
            "env": [{"name": "TARGET", "value": "three"}],
        })
        self.assertFalse(self.prepare(version="1.3.0", overrides="platforms/newer.yaml"))
        self.assertEqual(self.load()["platforms"][-1]["companion"]["version_policy"], "newer")
        self.assertFalse(companion_errors(self.repo, self.path, self.load()))
        from render import merge_overrides
        target = yaml.safe_load((self.path.parent / "platforms/newer.yaml").read_text())
        self.assertEqual([e["name"] for e in merge_overrides(self.recipe["serving"], target)["env"]], ["BASE", "SOURCE", "TARGET"])

    def test_older_and_opaque_newer_are_rejected(self):
        for version in ("1.2.2", "1.3.0.dev1"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "comparable"):
                self.prepare(version=version)

    def test_unfaithful_mappings_block_without_writes(self):
        import copy
        mutations = [
            ("overlay", lambda r: r["serving"].update(config_overrides=True)),
            ("storage", lambda r: r["deployment"].update(storage={"type": "pvc", "pvc": {"name": "weights"}})),
            ("topology", lambda r: r["serving"]["parallelism"].update(mode="tp+pp", pp=2)),
            ("capacity", lambda r: r["serving"]["parallelism"].update(tp=16)),
            ("pin", lambda r: r["platforms"][0].update(pinned_manifest="manifests/source/custom.yaml", pinned_reason="Custom security.")),
            ("router", lambda r: r["serving"].update(router={"strategy": "custom"})),
        ]
        for label, mutate in mutations:
            recipe = copy.deepcopy(self.recipe)
            mutate(recipe)
            self.write(self.path, recipe)
            before = self.path.read_bytes()
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.prepare()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse((self.path.parent / "platforms/vllm-v1.2.3.yaml").exists())

    def test_stale_source_and_version_changes_block_rendering(self):
        from render import render_recipe
        self.assertFalse(self.prepare())
        self.write(self.source, {"env": [{"name": "CHANGED", "value": "changed"}]})
        _, errors = render_recipe(self.repo, self.path)
        self.assertIn("stale/different env", "\n".join(errors))
        self.assertFalse((self.path.parent / "manifests").exists())

    def test_source_release_index_drift_blocks_same_version_companion(self):
        from render import render_recipe
        self.assertFalse(self.prepare())
        index_path = self.repo / "engine-versions/index.yaml"
        index = yaml.safe_load(index_path.read_text())
        index["releases"][0]["vllm_version"] = "1.2.4"
        self.write(index_path, index)
        _, errors = render_recipe(self.repo, self.path)
        self.assertIn("same/newer policy", "\n".join(errors))

    def test_version_specific_required_flag_constraint_blocks_output(self):
        from render import render_recipe
        self.write(self.repo / "schema/flag-constraints.yaml", {"schema_version": 1, "layers": [{
            "scope": {"platform": {"stack": "vllm", "version": "v1.2.3"}},
            "remove": [{"flag": "--speculative-config", "reason": "Synthetic unsupported feature."}],
        }]})
        self.assertFalse(self.prepare())
        _, errors = render_recipe(self.repo, self.path, dry_run=True)
        self.assertTrue(errors)
        self.assertIn("--speculative-config", "\n".join(errors))
        self.assertFalse((self.path.parent / "manifests").exists())

    def test_duplicate_alias_and_blocked_target_are_not_overwritten(self):
        self.recipe["platforms"].append({"stack": "vllm", "version": "v1.2.3",
            "overrides": "platforms/blocked.yaml", "blocked": True, "reason": "User block."})
        self.write(self.path, self.recipe)
        before = self.path.read_bytes()
        self.assertIn("duplicate", "\n".join(self.prepare()))
        self.assertEqual(self.path.read_bytes(), before)

    def test_non_nvidia_and_extra_resources_are_explicit_blockers(self):
        profile_path = self.repo / "hardware-profiles/fixture.yaml"
        profile = yaml.safe_load(profile_path.read_text())
        profile["accelerators"]["vendor"] = "amd"
        self.write(profile_path, profile)
        with self.assertRaisesRegex(ValueError, "nvidia.com/gpu"):
            self.prepare()
        profile["accelerators"]["vendor"] = "nvidia"
        self.write(profile_path, profile)
        self.recipe["serving"]["resources"]["limits"]["example.org/device"] = "1"
        self.write(self.path, self.recipe)
        with self.assertRaisesRegex(ValueError, "extra limits resources"):
            self.prepare()

    def test_non_cuda_runtime_variant_is_not_assumed_nvidia_compatible(self):
        self.recipe["serving"]["image_usage"]["variant"] = "rocm"
        self.write(self.path, self.recipe)
        with self.assertRaisesRegex(ValueError, "runtime variant"):
            self.prepare()

    def test_colliding_override_does_not_get_overwritten(self):
        target = self.path.parent / "platforms/vllm-v1.2.3.yaml"
        self.write(target, {"env": [{"name": "USER", "value": "work"}]})
        before = target.read_bytes()
        self.assertIn("refusing to overwrite", "\n".join(self.prepare()))
        self.assertEqual(target.read_bytes(), before)

    def test_override_symlink_cannot_escape_recipe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            external = Path(directory) / "user.yaml"
            external.write_text("user: work\n")
            (self.path.parent / "platforms/vllm-v1.2.3.yaml").symlink_to(external)
            self.assertIn("escapes", "\n".join(self.prepare()))
            self.assertEqual(external.read_text(), "user: work\n")

    def test_template_owned_env_and_parallelism_args_are_not_silently_overridden(self):
        self.write(self.source, {"env": [{"name": "HF_TOKEN", "value": "synthetic"}]})
        with self.assertRaisesRegex(ValueError, "template-owned HF_TOKEN"):
            self.prepare()
        self.write(self.source, {"args": [{"flag": "--tensor-parallel-size", "value": "4",
                    "required": True, "why": "Conflicting synthetic parallelism."}]})
        with self.assertRaisesRegex(ValueError, "template-owned parallelism"):
            self.prepare()

    def test_platform_evidence_requires_platform_identity_and_scope(self):
        from validate import platform_verification_errors
        self.assertFalse(self.prepare())
        recipe = self.load()
        platform = recipe["platforms"][-1]
        reference = "results/fixture/run.yaml"
        platform["verification"]["benchmark_runs"] = [reference]
        path = self.path.parent / reference
        path.parent.mkdir(parents=True)
        path.write_text("{}\n")
        run = {"recipe_id": recipe["recipe_id"], "deployment_scope": "single-node", "hardware_profile": recipe["hardware_profile"]}
        self.assertIn("run.platform", "\n".join(platform_verification_errors(self.repo, self.path, recipe, {path.resolve(): run})))
        run["platform"] = {"stack": "rhoai", "version": "9.0.0"}
        self.assertTrue(platform_verification_errors(self.repo, self.path, recipe, {path.resolve(): run}))
        run["platform"] = {"stack": "vllm", "version": "v1.2.3"}
        self.assertFalse(platform_verification_errors(self.repo, self.path, recipe, {path.resolve(): run}))
        run["deployment_scope"] = "multi-node"
        self.assertIn("deployment_scope", "\n".join(platform_verification_errors(self.repo, self.path, recipe, {path.resolve(): run})))
        run["deployment_scope"] = "single-node"
        run["recipe_id"] = "other"
        run["hardware_profile"] = "hardware-profiles/other.yaml"
        errors = "\n".join(platform_verification_errors(self.repo, self.path, recipe, {path.resolve(): run}))
        self.assertIn("recipe_id", errors)
        self.assertIn("hardware_profile", errors)
        platform["verification"]["benchmark_runs"] = ["../../escaped/run.yaml"]
        self.assertIn("escaping", "\n".join(platform_verification_errors(self.repo, self.path, recipe, {})))


class DistributedDPTests(unittest.TestCase):
    """Synthetic TP-local/DP-node cases, no production recipe ownership."""

    write = SingleNodeCompanionTests.write
    load = SingleNodeCompanionTests.load
    prepare = SingleNodeCompanionTests.prepare
    configure_local_weights = SingleNodeCompanionTests.configure_local_weights

    def setUp(self):
        SingleNodeCompanionTests.setUp(self)
        self.recipe["deployment"]["scope"] = "multi-node"
        self.recipe["platforms"] = [{"stack": "vllm", "version": "v0.24.0", "overrides": "platforms/vllm-v0.24.0.yaml", "config": None}]
        self.write(self.path.parent / "platforms/vllm-v0.24.0.yaml", {})
        self.recipe["serving"]["image_usage"] = {"kind": "custom", "note": "Synthetic audited engine fixture."}
        self.recipe["serving"]["engine"] = {"name": "vllm", "version": "0.24.0",
            "image": self.recipe["serving"]["image"], "source": "https://example.org/build/v0.24.0"}
        self.recipe["serving"]["parallelism"] = {"mode": "tp+dp", "tp": 8, "dp": 2}
        self.write(self.path, self.recipe)

    def rendered(self, dry_run=True):
        from render import render_recipe
        rendered, errors = render_recipe(self.repo, self.path, dry_run=dry_run)
        self.assertFalse(errors, errors)
        return list(yaml.safe_load_all(rendered))

    def test_tp8_dp2_lws_gpu_roles_group_and_api_routing(self):
        from render import build_template_context
        context = build_template_context(self.recipe, {}, None)
        self.assertEqual(context["kind"], "LeaderWorkerSet")
        self.assertEqual(context["gpu_count"], 8)
        lws, service = self.rendered()
        self.assertEqual(lws["spec"]["leaderWorkerTemplate"]["size"], 2)
        self.assertEqual(lws["spec"]["replicas"], 1)
        self.assertEqual(lws["spec"]["startupPolicy"], "LeaderCreated")
        templates = lws["spec"]["leaderWorkerTemplate"]
        for key in ("leaderTemplate", "workerTemplate"):
            spec = templates[key]["spec"]
            container = spec["containers"][0]
            self.assertEqual(container["resources"]["requests"]["nvidia.com/gpu"], "8")
            self.assertEqual(container["resources"]["limits"]["nvidia.com/gpu"], "8")
            self.assertEqual(spec["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]["topologyKey"], "kubernetes.io/hostname")
            script = container["args"][0]
            for token in ("--tensor-parallel-size 8", "--data-parallel-size 2", "--data-parallel-size-local 1",
                          '--data-parallel-address "$DP_ADDRESS"', "--data-parallel-rpc-port 13345"):
                self.assertIn(token, script)
            for token in ("--pipeline-parallel-size", "--nnodes", "--node-rank", "--data-parallel-rank "):
                self.assertNotIn(token, script)
            env = {entry["name"]: entry for entry in container["env"]}
            self.assertEqual(env["POD_IP"]["valueFrom"]["fieldRef"]["fieldPath"], "status.podIP")
            self.assertEqual(env["VLLM_HOST_IP"]["valueFrom"]["fieldRef"]["fieldPath"], "status.podIP")
        leader = templates["leaderTemplate"]["spec"]["containers"][0]
        worker = templates["workerTemplate"]["spec"]["containers"][0]
        self.assertNotIn("--headless", leader["args"][0])
        self.assertNotIn("--data-parallel-start-rank", leader["args"][0])  # Rank zero default; no hybrid-LB inference.
        self.assertIn("--api-server-count 1", leader["args"][0])
        self.assertIn('--headless --data-parallel-start-rank "$LWS_WORKER_INDEX"', worker["args"][0])
        self.assertIn("LWS_LEADER_ADDRESS", worker["args"][0])
        self.assertIn("getaddrinfo", worker["args"][0])
        self.assertNotIn("readinessProbe", worker)  # No invented HTTP endpoint on headless workers.
        self.assertNotIn("ports", worker)  # No invented fixed worker listener; internal ports are runtime-owned.
        self.assertIn("readinessProbe", leader)
        self.assertEqual(service["kind"], "Service")
        self.assertEqual(service["spec"]["selector"], {"app.kubernetes.io/name": context["name"], "role": "leader"})
        self.assertEqual(service["spec"]["ports"][0]["targetPort"], "http")

    def test_declared_inputs_role_args_and_shell_rank_roundtrip(self):
        import json
        import os
        import subprocess
        self.configure_local_weights()
        self.recipe["deployment"]["storage"]["pvc"]["access_modes"] = ["ReadOnlyMany"]
        for side in ("requests", "limits"):
            self.recipe["serving"]["resources"][side]["nvidia.com/gpu"] = "8"
        self.recipe["serving"]["decode"] = {
            "leader_args": [{"flag": "--disable-log-stats", "required": False, "why": "Synthetic leader logging."}],
            "worker_args": [{"flag": "--aggregate-engine-logging", "required": False, "why": "Synthetic worker logging."}],
        }
        self.write(self.path, self.recipe)
        lws, _ = self.rendered()
        for role, index in (("leader", "0"), ("worker", "1")):
            spec = lws["spec"]["leaderWorkerTemplate"][role + "Template"]["spec"]
            container = spec["containers"][0]
            script = container["args"][0]
            subprocess.run(["sh", "-n", "-c", script], check=True)
            # Substitute a harmless shell function for the executable only.
            # Keep real DNS resolution, rank bounds, argv and JSON serialization.
            script = 'vllm(){ printf "%s\\n" "$@"; };\n' + script.replace("exec vllm", "vllm")
            env = {**os.environ, "POD_IP": "127.0.0.1", "LWS_LEADER_ADDRESS": "localhost", "LWS_WORKER_INDEX": index, "LWS_GROUP_SIZE": "2"}
            result = subprocess.run(["sh", "-c", script], env=env, check=True, capture_output=True, text=True, timeout=10)
            argv = result.stdout.splitlines()
            self.assertEqual(argv[0:2], ["serve", "/mnt/models"])
            self.assertIn("--served-model-name=fixture-served-alias", argv)
            self.assertEqual(json.loads(next(a.split("=", 1)[1] for a in argv if a.startswith("--speculative-config="))), {"method": "mtp", "num_speculative_tokens": 3})
            self.assertEqual(argv[argv.index("--data-parallel-size-local") + 1], "1")
            self.assertEqual(argv[argv.index("--data-parallel-address") + 1], "127.0.0.1")
            if role == "worker":
                self.assertEqual(argv[argv.index("--data-parallel-start-rank") + 1], "1")
                self.assertIn("--aggregate-engine-logging", argv)
                self.assertNotIn("--disable-log-stats", argv)
            else:
                self.assertIn("--disable-log-stats", argv)
                self.assertNotIn("--aggregate-engine-logging", argv)
                self.assertEqual(container["startupProbe"]["failureThreshold"], 180)
                self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/v1/models")
            self.assertEqual(next(v for v in spec["volumes"] if v["name"] == "dshm")["emptyDir"]["sizeLimit"], "16Gi")
            self.assertEqual(next(v for v in spec["volumes"] if v["name"] == "model-weights")["persistentVolumeClaim"]["readOnly"], True)
            self.assertEqual(container["resources"]["requests"]["cpu"], "32")
            self.assertNotIn("cpu", container["resources"]["limits"])

    def test_multi_node_preparation_validation_and_drift(self):
        import subprocess
        from check_manifests import check_recipe
        before = self.path.read_bytes()
        self.assertFalse(self.prepare())  # No companion is created for multi-node.
        self.assertEqual(self.path.read_bytes(), before)
        self.rendered(dry_run=False)
        result = subprocess.run([sys.executable, str(REPO / "tools/validate.py"), "--repo", str(self.repo), "--current"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(check_recipe(self.repo, self.path.parent))
        output = self.path.parent / "manifests/vllm-v0.24.0/leaderworkerset.yaml"
        self.assertEqual(len(list(yaml.safe_load_all(output.read_text()))), 2)
        output.write_text("stale\n")
        self.assertIn("Manifest drift", "\n".join(check_recipe(self.repo, self.path.parent)))
        self.assertEqual(output.read_text(), "stale\n")

    def test_worker_rank_and_group_bounds_fail_before_launch(self):
        import os
        import subprocess
        lws, _ = self.rendered()
        script = lws["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]["containers"][0]["args"][0]
        script = 'vllm(){ echo CALLED; };\n' + script.replace("exec vllm", "vllm")
        for rank, group_size in (("0", "2"), ("2", "2"), ("gpu7", "2"), ("1", "3")):
            env = {**os.environ, "POD_IP": "127.0.0.1", "LWS_LEADER_ADDRESS": "localhost", "LWS_WORKER_INDEX": rank, "LWS_GROUP_SIZE": group_size}
            result = subprocess.run(["sh", "-c", script], env=env, text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("CALLED", result.stdout)

    def test_leader_bind_address_uses_runtime_pod_ip_family(self):
        import os
        import subprocess
        lws, _ = self.rendered()
        script = lws["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["spec"]["containers"][0]["args"][0]
        script = 'vllm(){ printf "%s\\n" "$@"; };\n' + script.replace("exec vllm", "vllm")
        for ip, host in (("127.0.0.1", "0.0.0.0"), ("::1", "::")):
            env = {**os.environ, "POD_IP": ip, "LWS_WORKER_INDEX": "0", "LWS_GROUP_SIZE": "2"}
            argv = subprocess.run(["sh", "-c", script], env=env, check=True, text=True, capture_output=True).stdout.splitlines()
            self.assertEqual(argv[argv.index("--host") + 1], host)
            self.assertEqual(argv[argv.index("--data-parallel-address") + 1], ip)

    def test_single_node_tp_dp_keeps_tp_times_dp_gpu_allocation(self):
        from render import build_template_context
        self.recipe["deployment"]["scope"] = "single-node"
        self.write(self.path, self.recipe)
        self.assertEqual(build_template_context(self.recipe, {}, None)["gpu_count"], 16)
        document, = self.rendered()
        self.assertEqual(document["kind"], "Deployment")
        self.assertEqual(document["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["nvidia.com/gpu"], "16")

    def test_unresolved_unaudited_versions_and_image_replacement_block(self):
        from render import render_recipe
        import copy
        original = copy.deepcopy(self.recipe)
        for version in (None, "0.23.0", "0.25.0", "0.24.0+vendor.1"):
            self.recipe = copy.deepcopy(original)
            if version is None:
                self.recipe["serving"].pop("engine")
            else:
                self.recipe["serving"]["engine"]["version"] = version
            self.write(self.path, self.recipe)
            _, errors = render_recipe(self.repo, self.path)
            self.assertIn("audit", "\n".join(errors))
            self.assertFalse((self.path.parent / "manifests").exists())
        self.recipe = original
        self.write(self.path, self.recipe)
        self.write(self.path.parent / "platforms/vllm-v0.24.0.yaml", {"image": "registry.example.org/replacement:unknown"})
        _, errors = render_recipe(self.repo, self.path)
        self.assertIn("image-bound resolved engine", "\n".join(errors))

    def test_invalid_topology_resources_env_overlay_and_pvc_are_diagnostics(self):
        import copy
        from render import render_recipe
        original = copy.deepcopy(self.recipe)
        cases = [
            (lambda r: r["serving"]["parallelism"].update(dp=1), "DP>=2"),
            (lambda r: r["serving"]["parallelism"].update(tp=16), "each declared node"),
            (lambda r: r["serving"].update(resources={"limits": {"nvidia.com/gpu": "16"}}), "per-pod TP"),
            (lambda r: r["serving"].update(env=[{"name": "RANK", "value": "7"}]), "vLLM ranks"),
            (lambda r: r["platforms"][0].update(config="config/custom"), "overlays"),
            (lambda r: r["deployment"].update(storage={"type": "nfs"}), "read-only PVC"),
        ]
        for mutation, message in cases:
            self.recipe = copy.deepcopy(original)
            mutation(self.recipe)
            self.write(self.path, self.recipe)
            _, errors = render_recipe(self.repo, self.path)
            self.assertIn(message, "\n".join(errors))
            self.assertFalse((self.path.parent / "manifests").exists())
        self.recipe = copy.deepcopy(original)
        self.configure_local_weights()
        self.write(self.path, self.recipe)
        _, errors = render_recipe(self.repo, self.path)
        self.assertIn("ReadOnlyMany or ReadWriteMany", "\n".join(errors))

    def test_owned_startup_flags_cannot_enable_other_rank_or_lb_modes(self):
        from render import render_recipe
        original = self.recipe["serving"]["args"][:]
        for flag in ("--data-parallel-rank", "--data-parallel-hybrid-lb", "--nnodes", "--enable-expert-parallel", "--headless", "--config", "--grpc"):
            self.recipe["serving"]["args"] = original + [{"flag": flag, "required": True, "why": "Synthetic incompatible startup."}]
            self.write(self.path, self.recipe)
            _, errors = render_recipe(self.repo, self.path)
            self.assertIn("template-owned", "\n".join(errors))

    def test_platform_label_must_match_the_audited_engine(self):
        from render import render_recipe
        self.recipe["platforms"][0]["version"] = "v0.25.0"
        self.write(self.path, self.recipe)
        _, errors = render_recipe(self.repo, self.path)
        self.assertIn("platform version must match", "\n".join(errors))

    def test_missing_scope_and_unsupported_multi_node_modes_block(self):
        from render import build_template_context, render_recipe
        for mode in ("tp", "dp"):
            self.recipe["serving"]["parallelism"]["mode"] = mode
            self.write(self.path, self.recipe)
            _, errors = render_recipe(self.repo, self.path)
            self.assertIn("No template", "\n".join(errors))
        self.recipe["deployment"].pop("scope")
        self.write(self.path, self.recipe)
        _, errors = render_recipe(self.repo, self.path)
        self.assertIn("scope=None", "\n".join(errors))
        with self.assertRaisesRegex(ValueError, "scope=None"):
            build_template_context(self.recipe, {}, None)


if __name__ == "__main__":
    unittest.main()
