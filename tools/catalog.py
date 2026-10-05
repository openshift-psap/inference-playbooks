#!/usr/bin/env python3
"""Build a deterministic v4 recipe catalog (generated JSON is a build artifact)."""

from __future__ import annotations

import argparse
import copy
import contextlib
import hashlib
import json
import io
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import yaml

from constraints import load_constraints, validate_recipe_against_constraints
from engine_versions import compare_versions, engine_errors, load_engine_index, resolve_engine
from recipe_evidence import load_unique_yaml_all
from render import COMPONENT_KIND, build_template_context, merge_overrides
from validate import (contained_path, load_schema, load_schema_registry, load_yaml,
                      validate_benchmark_run, validate_document, validate_recipe_notes,
                      validate_v4_recipe)

REPOSITORY_URL = "https://github.com/openshift-psap/inference-playbooks"
SCHEMA_VERSION = 2


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def build_identity(repo: Path) -> dict:
    try:
        sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        dirty = bool(subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip())
        return {"source_sha": sha, "dirty": dirty}
    except subprocess.CalledProcessError:
        return {"source_sha": None, "dirty": True}


def source_link(repo: Path, path: Path, build: dict) -> dict:
    relative = path.resolve().relative_to(repo.resolve()).as_posix()
    sha = build["source_sha"]
    url = None if build["dirty"] or not sha else f"{REPOSITORY_URL}/blob/{sha}/{quote(relative, safe='/')}"
    return {"path": relative, "url": url}


def required_path(root: Path, reference: str) -> Path:
    path = contained_path(root, reference)
    if not path or not path.is_file():
        raise ValueError(f"{root}: missing or escaping required reference: {reference}")
    return path


def require_valid(path: Path, value: dict, schema: dict, registry) -> None:
    errors = validate_document(path, value, schema, registry)
    if errors:
        raise ValueError("\n".join(errors))


def runtime_details(value: object) -> list[dict]:
    """Expose exact container settings from final artifacts, not rebuilt commands."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "containers" and isinstance(child, list):
                found.extend(copy.deepcopy(container) for container in child if isinstance(container, dict))
            else:
                found.extend(runtime_details(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(runtime_details(child))
    return found


def field_values(value: object, field: str) -> list:
    if isinstance(value, dict):
        return ([copy.deepcopy(value[field])] if field in value else []) + [item for child in value.values() for item in field_values(child, field)]
    if isinstance(value, list):
        return [item for child in value for item in field_values(child, field)]
    return []


def artifacts_for(repo: Path, directory: Path, platform: dict, build: dict) -> list[dict]:
    if platform.get("blocked"):
        return []
    folder = directory / "manifests" / f"{platform['stack']}-{platform['version']}"
    paths = sorted(path for path in folder.glob("*") if path.suffix in {".yaml", ".yml"})
    if not paths:
        raise ValueError(f"{folder}: final deployment artifacts missing; render the recipe first")
    artifacts = []
    for path in paths:
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError(f"{path}: artifact escapes recipe")
        raw = path.read_bytes()
        body = raw.decode("utf-8")
        documents = [document for document in load_unique_yaml_all(body) if document is not None]
        if not documents or any(not isinstance(doc, dict) or not doc.get("kind") for doc in documents):
            raise ValueError(f"{path}: deployment artifact requires Kubernetes object documents")
        artifacts.append({"name": path.name, "source": source_link(repo, path, build),
                          "body": body, "sha256": digest(raw),
                          "kinds": [doc["kind"] for doc in documents],
                          "containers": runtime_details(documents),
                          "volume_bindings": field_values(documents, "volumes"),
                          "model_storage": field_values(documents, "uri")})
    return artifacts


def resolve_specs(repo: Path, recipe_path: Path, recipe: dict, serving: dict, model: dict, profile: dict, notes: dict | None) -> list[dict]:
    specs = []
    for spec in (notes or {}).get("profile", {}).get("specs", []):
        reference, _, pointer = spec["source"].partition("#")
        roots = {"recipe.yaml": {**recipe, "serving": serving}, "model.yaml": model, "hardware_profile": profile}
        value = roots.get(reference)
        state = "resolved"
        if value is None:
            # A legacy config pointer is not proven equivalent to the final artifact.
            state = "unverified"
        else:
            try:
                for part in pointer.lstrip("/").split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    value = value[int(part)] if isinstance(value, list) else value[part]
            except (KeyError, IndexError, TypeError, ValueError):
                state, value = "unresolved", None
        specs.append({**spec, "state": state, "value": value if state == "resolved" else None})
    return specs


def compatibility_signature(repo: Path, directory: Path, recipe: dict, serving: dict, roles: dict, profile: dict, artifacts: list[dict]) -> dict:
    """Strict configuration snapshot; missing checkpoint/workload proof blocks badges."""
    revisions = {arg.get("value") for args in roles.values() for arg in args if arg.get("flag") == "--revision"}
    revision = next(iter(revisions)) if len(revisions) == 1 else None
    workload_files = sorted(path for path in (directory / "benchmarks").rglob("*") if path.is_file())
    if any(not path.resolve().is_relative_to(directory.resolve()) for path in workload_files):
        raise ValueError(f"{directory}: workload inputs escape recipe")
    workload = {path.relative_to(directory / "benchmarks").as_posix(): digest(path.read_bytes()) for path in workload_files}
    final_runtime = [{key: value for key, value in container.items() if key != "image"}
                     for artifact in artifacts for container in artifact["containers"]]
    return {"model_id": recipe["model_id"], "checkpoint": serving["model"], "model_revision": revision,
            "hardware_profile": recipe["hardware_profile"], "hardware_profile_revision": profile["profile_revision"],
            "hardware_sha256": digest(canonical(profile)), "scope": recipe["deployment"]["scope"],
            "parallelism": serving["parallelism"], "workload_profile": recipe["workload_profile"],
            "workload_sha256": digest(canonical(workload)) if workload else None,
            "serving": {key: value for key, value in serving.items() if key not in {"image", "engine", "image_usage", "args", "decode", "prefill"}},
            "roles": roles, "storage": recipe["deployment"].get("storage"), "final_runtime": final_runtime,
            "volume_bindings": [binding for artifact in artifacts for binding in artifact["volume_bindings"]],
            "model_storage": [binding for artifact in artifacts for binding in artifact["model_storage"]]}


def validation_badge(entry: dict, evidence: list[dict]) -> dict:
    empty = {"state": "none", "label": None, "run_id": None, "reason": "No applicable validated benchmark evidence"}
    if entry["blocked"] or entry["maturity"] not in {"validated", "production"} or entry["engine"]["state"] != "resolved":
        return empty
    signature = entry["compatibility_signature"]
    if not signature.get("model_revision") or not signature.get("workload_sha256") or not signature.get("final_runtime"):
        return {**empty, "reason": "Checkpoint, workload or runtime equivalence proof is missing"}
    for item in evidence:
        environment = item["run"].get("environment", {})
        tested_platform = environment.get("platform", {})
        image = environment.get("image", "")
        if environment.get("catalog_compatibility") != signature or not tested_platform.get("stack") or not tested_platform.get("version"):
            continue
        if not isinstance(image, str) or not re.search(r"@sha256:[a-f0-9]{64}$", image):
            continue
        tested_version = environment.get("vllm_version")
        if not isinstance(tested_version, str):
            continue
        comparison = compare_versions(entry["engine"]["version"], tested_version)
        if comparison is None or comparison < 0:
            continue
        label = "Validated" if comparison == 0 else f"Validated on earlier vLLM — v{tested_version.removeprefix('v')}"
        return {"state": "same-engine" if comparison == 0 else "earlier-engine", "label": label,
                "run_id": item["run"]["run_id"], "tested_platform": tested_platform,
                "tested_engine": tested_version, "tested_image": image,
                "qualification": "Evidence belongs to the original run; the selected platform was not necessarily directly benchmarked."}
    return empty


def build_catalog(repo: Path, build: dict | None = None) -> dict:
    repo = repo.resolve()
    build = build_identity(repo) if build is None else build
    registry = load_schema_registry(repo)
    schemas = {name: load_schema(repo, name) for name in ("model", "recipe", "hardware-profile", "recipe-notes", "platform-overrides", "benchmark-run", "benchmark-result")}
    index, constraints = load_engine_index(repo), load_constraints(repo)
    models = {}
    for path in sorted(repo.glob("models/*/model.yaml")):
        model = load_yaml(path)
        require_valid(path, model, schemas["model"], registry)
        if model["model_id"] != path.parent.name or model["model_id"] in models:
            raise ValueError(f"{path}: duplicate or misplaced model identity")
        models[model["model_id"]] = {"id": model["model_id"], "name": model["name"], "metadata": model,
                                   "source": source_link(repo, path, build), "entry_ids": []}
    entries = []
    identities, run_ids = set(), set()
    for path in sorted(repo.glob("models/*/recipes/*/recipe.yaml")):
        recipe = load_yaml(path)
        require_valid(path, recipe, schemas["recipe"], registry)
        with contextlib.redirect_stdout(io.StringIO()):
            errors = validate_v4_recipe(repo, path, recipe, registry)
        errors += validate_recipe_notes(repo, path, recipe, schemas["recipe-notes"], registry)
        if errors:
            raise ValueError("\n".join(errors))
        if recipe["model_id"] not in models or recipe["model_id"] != path.parents[2].name:
            raise ValueError(f"{path}: missing or misplaced recipe/model identity")
        model = models[recipe["model_id"]]["metadata"]
        profile_path = required_path(repo, recipe["hardware_profile"])
        profile = load_yaml(profile_path)
        require_valid(profile_path, profile, schemas["hardware-profile"], registry)
        notes_path = required_path(path.parent, recipe["notes"]) if recipe.get("notes") else None
        notes = load_yaml(notes_path) if notes_path else None
        evidence = []
        for reference in recipe.get("benchmark_runs", []):
            run_path = required_path(path.parent, reference)
            if not run_path.is_relative_to((path.parent / 'results').resolve()):
                raise ValueError(f'{run_path}: benchmark reference escapes results/')
            run = load_yaml(run_path)
            require_valid(run_path, run, schemas["benchmark-run"], registry)
            errors, result_path = validate_benchmark_run(repo, run_path, run)
            if errors or result_path is None:
                raise ValueError("\n".join(errors) or f"{run_path}: result missing")
            raw_outputs = [artifact for artifact in run['artifacts'] if artifact['type'] == 'raw-output']
            if not raw_outputs:
                raise ValueError(f'{run_path}: published metrics require authoritative raw-output provenance')
            for artifact in raw_outputs:
                checksum = artifact.get('checksum', '')
                if not re.fullmatch(r'sha256:[a-f0-9]{64}', checksum):
                    raise ValueError(f'{run_path}: raw-output checksum is required')
                if artifact.get('path'):
                    raw_path = required_path(run_path.parent, artifact['path'])
                    if 'sha256:' + digest(raw_path.read_bytes()) != checksum:
                        raise ValueError(f'{run_path}: raw-output checksum mismatch')
            if run["run_id"] in run_ids:
                raise ValueError(f"{run_path}: duplicate run identity")
            run_ids.add(run["run_id"])
            result = json.loads(result_path.read_text(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"nonfinite JSON: {value}")))
            require_valid(result_path, result, schemas["benchmark-result"], registry)
            if (run["recipe_id"] != recipe["recipe_id"] or run["deployment_scope"] != recipe["deployment"]["scope"]
                or run["hardware_profile"] != recipe["hardware_profile"]
                or result["run_id"] != run["run_id"] or result["accelerator_key"] != profile["accelerator_key"]
                or result["deployment_scope"] != run["deployment_scope"]):
                raise ValueError(f"{run_path}: evidence identity/scope/profile mismatch")
            evidence.append({"run": run, "result": result, "source": source_link(repo, run_path, build),
                             "result_source": source_link(repo, result_path, build),
                             "run_body": run_path.read_text(), "result_body": result_path.read_text()})
        for platform in recipe["platforms"]:
            identity = [recipe["model_id"], recipe["recipe_id"], platform["stack"], platform["version"]]
            entry_id = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
            if entry_id in identities:
                raise ValueError(f"{path}: duplicate recipe/platform identity: {entry_id}")
            identities.add(entry_id)
            override_path = required_path(path.parent, platform["overrides"])
            overrides = load_yaml(override_path)
            require_valid(override_path, overrides, schemas["platform-overrides"], registry)
            serving = merge_overrides(copy.deepcopy(recipe["serving"]), overrides)
            effective = {**recipe, "serving": serving, "platform": platform}
            blocked = platform.get("blocked", False)
            if not blocked:
                errors = validate_recipe_against_constraints(constraints, effective, model)
                errors += engine_errors(recipe["serving"], overrides, platform, index)
                if errors:
                    raise ValueError(f"{path}: {'; '.join(errors)}")
            context = build_template_context(effective, model, constraints, platform)
            roles = ({"leader": context["leader_args"], "worker": context["worker_args"]}
                     if serving['parallelism'].get('pp', 1) > 1 else {"serving": context['args']})
            artifacts = artifacts_for(repo, path.parent, platform, build)
            engine = resolve_engine(recipe["serving"], overrides, platform, index)
            if artifacts and serving["image"] not in {container.get("image") for artifact in artifacts for container in artifact["containers"]}:
                engine = {**engine, "state": "unknown", "version": None, "reason": "Final artifact image does not match serving metadata"}
            entry = {"id": entry_id, "identity": identity, "model_id": recipe["model_id"], "recipe_id": recipe["recipe_id"],
                     "platform": {"stack": platform["stack"], "version": platform["version"]},
                     "blocked": blocked, "reason": platform.get("reason"), "maturity": recipe["maturity"],
                     "scope": recipe["deployment"]["scope"], "workload_profile": recipe["workload_profile"],
                     "optimization_intent": recipe["optimization_intent"], "deployment_mode": recipe["deployment_mode"],
                     "hardware": {"path": recipe["hardware_profile"], "revision": profile["profile_revision"],
                                  "accelerator_key": profile["accelerator_key"], "data": profile,
                                  "source": source_link(repo, profile_path, build)},
                     "gpu_allocation": {"parallelism": serving["parallelism"], "declared_gpus_per_replica":
                                        serving["parallelism"].get("tp", 1) * serving["parallelism"].get("pp", 1) * serving["parallelism"].get("dp", 1)},
                     "serving": serving, "roles": roles, "artifacts": artifacts, "engine": engine,
                     "source": source_link(repo, path, build), "override_source": source_link(repo, override_path, build),
                     "notes": notes, "notes_source": source_link(repo, notes_path, build) if notes_path else None,
                     "specs": resolve_specs(repo, path, recipe, serving, model, profile, notes),
                     "evidence": evidence, "commands": [],
                     "command_note": "Use the exact deployment artifacts; standalone equivalence has not been established."}
            entry["compatibility_signature"] = compatibility_signature(repo, path.parent, recipe, serving, roles, profile, artifacts)
            entry["validation"] = validation_badge(entry, evidence)
            entries.append(entry)
            models[recipe["model_id"]]["entry_ids"].append(entry_id)
    return {"schema_version": SCHEMA_VERSION, "build": build, "models": list(models.values()), "entries": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, default=Path(".build/site/catalog.json"))
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--source-sha", help="require this exact checked-out SHA")
    args = parser.parse_args()
    try:
        identity = build_identity(args.repo)
        if args.require_clean and identity["dirty"]:
            raise ValueError("catalog publication requires a clean checkout")
        if args.source_sha and identity["source_sha"] != args.source_sha:
            raise ValueError("catalog source SHA differs from checked-out build identity")
        catalog = build_catalog(args.repo, identity)
        output = json.dumps(catalog, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        if args.stdout:
            print(output, end="")
        else:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(output, encoding="utf-8")
        return 0
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        print(f"Catalog build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
