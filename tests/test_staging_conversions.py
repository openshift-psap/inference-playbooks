"""Preservation and reproducibility checks for converted staging submissions."""

import json
from pathlib import Path
import shlex
import shutil
import subprocess
import unittest

import yaml


REPO = Path(__file__).resolve().parents[1]
H100 = REPO / "models/glm-5.3/recipes/h100-x64-pd-pcp8dcp8-ep32-dp16tp2-agentx-275k"
NEMOTRON = REPO / "models/nemotron-3-ultra-550b/recipes/h200-x32-pd-3p1d-tp8-agentx-128k"
B200 = REPO / "models/glm-5.2/recipes/b200-x8-tp8-ep-mtp3-guidellm-8k1k"


def load(path):
    return yaml.safe_load(path.read_text())


def documents(path):
    return {(doc["kind"], doc["metadata"]["name"]): doc
            for doc in yaml.safe_load_all(path.read_text()) if doc is not None}


class StagingConversionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("kustomize"), "Kustomize required")
    def test_pinned_bundles_rebuild_byte_for_byte(self):
        for directory in (H100, NEMOTRON):
            with self.subTest(recipe=directory.name):
                recipe = load(directory / "recipe.yaml")
                output = subprocess.check_output([
                    "kustomize", "build", "--load-restrictor", "LoadRestrictionsNone",
                    str(directory / "config"),
                ])
                pin = directory / recipe["platforms"][0]["pinned_manifest"]
                self.assertEqual(output, pin.read_bytes())
                self.assertEqual(recipe["maturity"], "day-zero")
                self.assertEqual(recipe["deployment"]["status"]["state"], "needs-verification")
                self.assertFalse(recipe.get("benchmark_runs"))

    def test_h100_preserves_all_runtime_fields_except_documented_changes(self):
        recipe = load(H100 / "recipe.yaml")
        actual = documents(H100 / recipe["platforms"][0]["pinned_manifest"])
        expected = {}
        for name in ("disaggregatedset.yaml", "podmonitor.yaml", "vllm-startup-patches.configmap.yaml"):
            doc = load(H100 / "raw-manifest" / name)
            doc["metadata"]["namespace"] = "glm52-canary"
            expected[(doc["kind"], doc["metadata"]["name"])] = doc
        deployment = expected[("DisaggregatedSet", "glm53-e2e-pcp")]
        for role in deployment["spec"]["roles"]:
            pod = role["spec"]["leaderWorkerTemplate"]["workerTemplate"]["spec"]
            affinity = pod["affinity"]["nodeAffinity"]
            terms = affinity["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
            for term in terms:
                term["matchExpressions"] = [item for item in term["matchExpressions"]
                                            if item["key"] != "kubernetes.io/hostname"]
            affinity.pop("preferredDuringSchedulingIgnoredDuringExecution", None)
            for container in pod["initContainers"]:
                if container["image"] == "busybox:1.36":
                    container["image"] = "docker.io/library/busybox:1.36"
        self.assertEqual(actual, expected)
        self.assertNotIn("kubernetes.io/hostname", yaml.safe_dump(actual))
        self.assertNotIn("engine", recipe)  # Do not invent the bundled vLLM version.
        self.assertNotIn("engine", recipe["serving"])

    def test_nemotron_only_qualifies_the_submitted_serving_image(self):
        recipe = load(NEMOTRON / "recipe.yaml")
        actual = documents(NEMOTRON / recipe["platforms"][0]["pinned_manifest"])
        original = REPO / "models/nemotron-3-ultra-550b/vllm/v0.26.0/multi-node/manifests"
        expected = {}
        for role in ("prefill", "decode"):
            doc = load(original / f"nemotron-3-ultra-550b-h200-pd-{role}.yaml")
            pod = doc["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["spec"]
            pod["containers"][0]["image"] = recipe["serving"]["image"]
            expected[(doc["kind"], doc["metadata"]["name"])] = doc
        self.assertEqual(actual, expected)

    def test_b200_authored_target_preserves_dependencies_without_duplicate_volumes(self):
        recipe = load(B200 / "recipe.yaml")
        platform = next(p for p in recipe["platforms"] if p["stack"] == "vllm")
        self.assertNotIn("companion", platform)
        verification = platform["verification"]
        self.assertEqual(verification["maturity"], "day-zero")
        self.assertEqual(verification["deployment_status"]["state"], "needs-verification")
        self.assertEqual(verification["benchmark_runs"], [])
        deployment = load(B200 / "manifests/vllm-v0.27.0/deployment.yaml")
        self.assertEqual(deployment["spec"]["replicas"], 0)
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        self.assertEqual(len(volumes), len(pod["volumes"]))
        self.assertEqual(volumes["hf-cache"]["persistentVolumeClaim"]["claimName"], "glm-5-2-hf-cache")
        self.assertEqual(volumes["jit-cache"]["persistentVolumeClaim"]["claimName"], "glm-5-2-jit-cache")
        self.assertEqual(volumes["dshm"]["emptyDir"]["sizeLimit"], "2Gi")
        self.assertNotIn("cpu", container["resources"]["limits"])
        self.assertEqual(container["resources"]["requests"]["ephemeral-storage"], "1Ti")
        self.assertIn("instanttensor==0.1.9", pod["initContainers"][0]["command"][-1])
        for mount in container["volumeMounts"]:
            self.assertIn(mount["name"], volumes)
        value = next(a.split("=", 1)[1] for a in container["args"] if a.startswith("--speculative-config="))
        self.assertEqual(json.loads(value), {"method": "mtp", "num_speculative_tokens": 3})

    def test_b200_rhoai_adapter_round_trips_template_quoted_json(self):
        manifest = load(B200 / "manifests/rhoai-3.5/llminferenceservice.yaml")
        container = manifest["spec"]["template"]["containers"][0]
        args_env = next(e["value"] for e in container["env"] if e["name"] == "VLLM_ADDITIONAL_ARGS")
        parsed = shlex.split(args_env)
        value = parsed[parsed.index("--speculative-config") + 1]
        self.assertEqual(json.loads(value), {"method": "mtp", "num_speculative_tokens": 3})
        self.assertIn("shlex.split", container["command"][-1])


if __name__ == "__main__":
    unittest.main()
