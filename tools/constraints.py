#!/usr/bin/env python3
"""Flag constraint evaluation engine for inference playbook recipes.

Prevents invalid vLLM flags from reaching rendered manifests by evaluating
layered constraint rules against recipe context.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from recipe_evidence import load_unique_yaml


def load_constraints(repo: Path) -> dict:
    """Load and validate flag-constraints.yaml against its JSON schema."""
    constraints_path = repo / "schema" / "flag-constraints.yaml"
    schema_path = repo / "schema" / "flag-constraints.schema.json"
    if not constraints_path.is_file():
        return {"schema_version": 1, "layers": []}
    text = constraints_path.read_text()
    data = load_unique_yaml(text)
    if not isinstance(data, dict):
        raise ValueError("flag-constraints.yaml must be a YAML mapping")
    schema = json.loads(schema_path.read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(data), key=lambda e: str(e.json_path))
    if errors:
        messages = [f"{e.json_path or '$'}: {e.message}" for e in errors]
        raise ValueError(
            "flag-constraints.yaml schema errors:\n" + "\n".join(messages)
        )
    return data


def _match_sub_scope(scope_value: object, context_value: object) -> bool:
    """Match a single scope field against its context counterpart.

    For dict values (platform, parallelism), each specified key must match.
    For scalar values, exact equality is required.
    """
    if isinstance(scope_value, dict):
        if not isinstance(context_value, dict):
            return False
        for key, value in scope_value.items():
            ctx_val = context_value.get(key)
            if key == "mode" and isinstance(value, str) and isinstance(ctx_val, str):
                if value not in ctx_val.split("+"):
                    return False
            elif ctx_val != value:
                return False
        return True
    return scope_value == context_value


def match_scope(scope: dict, recipe_context: dict) -> bool:
    """Check if a scope matches the recipe context.

    recipe_context has keys: model_type, model_id, platform (dict with
    stack, version), parallelism (dict with mode, tp, pp, dp),
    workload_profile.

    Each scope field is optional.  All specified fields must match.
    Omitted fields match anything.  An empty scope matches everything.
    """
    if not scope:
        return True
    for key, value in scope.items():
        if not _match_sub_scope(value, recipe_context.get(key)):
            return False
    return True


def evaluate_constraints(
    constraints: dict, recipe_context: dict
) -> tuple[list[dict], list[dict], list[str]]:
    """Evaluate all constraint layers against a recipe context.

    Returns (removes, forces, errors):
    - removes: list of {flag, reasons: [str]} for flags to remove
    - forces: list of {flag, value, reasons: [str]} for flags to force
    - errors: list of str for conflicts (remove+force same flag,
      force same flag with different values)
    """
    remove_map: dict[str, list[str]] = defaultdict(list)
    force_map: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for layer in constraints.get("layers", []):
        scope = layer.get("scope", {})
        if not match_scope(scope, recipe_context):
            continue
        for entry in layer.get("remove", []):
            when = entry.get("when")
            if when and not match_scope(when, recipe_context):
                continue
            remove_map[entry["flag"]].append(entry["reason"])
        for entry in layer.get("force", []):
            when = entry.get("when")
            if when and not match_scope(when, recipe_context):
                continue
            force_map[entry["flag"]].append((entry["value"], entry["reason"]))

    removes = [
        {"flag": flag, "reasons": reasons}
        for flag, reasons in sorted(remove_map.items())
    ]
    # Collapse force entries per flag.
    forces = []
    errors = []
    for flag in sorted(force_map):
        entries = force_map[flag]
        values = {v for v, _ in entries}
        reasons = [r for _, r in entries]
        if len(values) > 1:
            errors.append(
                f"conflicting force values for {flag}: "
                + ", ".join(sorted(values))
            )
        else:
            forces.append(
                {"flag": flag, "value": entries[0][0], "reasons": reasons}
            )

    # Detect remove+force on same flag.
    for flag in sorted(set(remove_map) & set(force_map)):
        errors.append(
            f"flag {flag} appears in both remove and force constraints"
        )

    return removes, forces, errors


def _build_recipe_context(recipe: dict, model: dict) -> dict:
    """Build a recipe_context dict from recipe and model data."""
    platform = recipe.get("platform", {})

    serving = recipe.get("serving", {})
    if isinstance(serving, dict) and "parallelism" in serving:
        parallelism_raw = serving.get("parallelism", {})
    else:
        deployment = recipe.get("deployment", {})
        parallelism_raw = deployment.get("parallelism", {}) if isinstance(deployment, dict) else {}
    if not isinstance(parallelism_raw, dict):
        parallelism_raw = {}

    parallelism: dict = {}
    for v4_key, v3_key in [("tp", "tensor"), ("pp", "pipeline"), ("dp", "data")]:
        val = parallelism_raw.get(v4_key) or parallelism_raw.get(v3_key)
        if val is not None:
            parallelism[v4_key] = val

    mode = parallelism_raw.get("mode")
    if mode:
        parallelism["mode"] = mode
    else:
        pp = parallelism.get("pp")
        tp = parallelism.get("tp")
        dp = parallelism.get("dp")
        if pp and pp > 1 and tp and tp > 1:
            parallelism["mode"] = "tp+pp"
        elif pp and pp > 1:
            parallelism["mode"] = "pp"
        elif tp and tp > 1 and dp and dp > 1:
            parallelism["mode"] = "tp+dp"
        elif tp and tp > 1:
            parallelism["mode"] = "tp"
        elif dp and dp > 1:
            parallelism["mode"] = "dp"

    context: dict = {}
    model_type = model.get("model_type") or model.get("family")
    if model_type:
        context["model_type"] = model_type
    model_id = recipe.get("model_id")
    if model_id:
        context["model_id"] = model_id
    if isinstance(platform, dict) and platform:
        context["platform"] = platform
    if parallelism:
        context["parallelism"] = parallelism
    workload = recipe.get("workload_profile")
    if workload:
        context["workload_profile"] = workload

    return context


def _extract_contributor_flags(recipe: dict) -> dict[str, str | None]:
    """Extract contributor-specified flags from a recipe.

    v4: serving.args is a list of {flag, value?, ...} objects.
    v3: deployment.components has raw CLI args in containers.
    Returns {flag: value_or_None} pairs.
    """
    result: dict[str, str | None] = {}

    serving = recipe.get("serving", {})
    if isinstance(serving, dict):
        for arg_list_key in ("args",):
            args = serving.get(arg_list_key, [])
            if isinstance(args, list):
                for arg in args:
                    if isinstance(arg, dict) and "flag" in arg:
                        result[arg["flag"]] = arg.get("value")
        for role_name in ("decode", "prefill"):
            role_block = serving.get(role_name, {})
            if isinstance(role_block, dict):
                for arg_key in ("args", "leader_args", "worker_args"):
                    role_args = role_block.get(arg_key, [])
                    if isinstance(role_args, list):
                        for arg in role_args:
                            if isinstance(arg, dict) and "flag" in arg:
                                result[arg["flag"]] = arg.get("value")

    return result


def validate_recipe_against_constraints(
    constraints: dict, recipe: dict, model: dict
) -> list[str]:
    """Validate a recipe's serving args against flag constraints.

    Build recipe_context from recipe + model.  Evaluate constraints.
    Check:
    - Contributor arg matches a 'remove' constraint -> error
      (unless constraint_overrides allows it)
    - Contributor arg value conflicts with a 'force' value -> error
    - Internal constraint conflicts (remove+force, force+force with
      different values) -> error

    Returns list of validation error strings.
    """
    context = _build_recipe_context(recipe, model)
    removes, forces, errors = evaluate_constraints(constraints, context)

    contributor_flags = _extract_contributor_flags(recipe)

    # v4: constraint_overrides in serving block; v3: at recipe root
    serving = recipe.get("serving", {})
    overrides_list = []
    if isinstance(serving, dict):
        overrides_list = serving.get("constraint_overrides", [])
    if not overrides_list:
        overrides_list = recipe.get("constraint_overrides", [])
    if not isinstance(overrides_list, list):
        overrides_list = []
    overrides: dict[str, dict] = {}
    for entry in overrides_list:
        if isinstance(entry, dict) and "flag" in entry:
            overrides[entry["flag"]] = entry

    # Check removes: contributor should not use flags marked for removal.
    for entry in removes:
        flag = entry["flag"]
        if flag in contributor_flags:
            override = overrides.get(flag, {})
            if isinstance(override, dict) and override.get("allow") is True:
                reason = override.get("reason", "")
                if reason and isinstance(reason, str) and reason.strip():
                    continue
                errors.append(
                    f"{flag}: constraint_overrides must include a reason"
                )
                continue
            reasons_text = "; ".join(entry["reasons"])
            errors.append(
                f"{flag} is not allowed: {reasons_text}"
            )

    # Check forces: contributor value must not conflict with forced value.
    for entry in forces:
        flag = entry["flag"]
        forced_value = entry["value"]
        if flag in contributor_flags:
            contributor_value = contributor_flags[flag]
            if contributor_value is not None and contributor_value != forced_value:
                errors.append(
                    f"{flag} must be {forced_value!r}, "
                    f"but recipe specifies {contributor_value!r}"
                )

    return errors
