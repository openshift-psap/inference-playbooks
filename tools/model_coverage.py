#!/usr/bin/env python3
"""Evaluate planned model coverage from tracking issues and the repository catalog.

The repository catalog is the only source of implementation, maturity, and
benchmark-evidence truth. Tracking issues own only the plan: model identity,
owner, completion criterion, target recipe/platform coverage, blockers, and an
optional internal Jira key. A merged recipe, a green CI run, or a validation
badge for another platform never completes a target by itself.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from catalog import REPOSITORY_URL, build_catalog, compare_versions, validation_badge
from recipe_evidence import load_unique_yaml

TRACKER_MARKER = "<!-- inference-playbooks:model-coverage-tracker -->"
JIRA_NOTE = "Refer to internal Jira for more details on this task."
LABEL = "model-coverage"
JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-[0-9]+$")
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
OWNER = re.compile(r"^@?[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:/[A-Za-z0-9_.-]{1,100})?$")
RECIPE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STACK = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
MAX_TARGETS = 100
MAX_PLATFORMS = 100
MAX_BLOCKERS = 20
PLACEHOLDERS = {
    "model_id": "replace-with-model-id",
    "owner": "@your-github-handle",
    "recipe_id": "replace-with-recipe-id",
}

TOP_LEVEL_KEYS = frozenset({
    "schema_version", "model_id", "owner", "completion", "targets", "jira", "blockers",
})
TARGET_KEYS = frozenset({"recipe_id", "platforms"})
PLATFORM_KEYS = frozenset({"stack", "version", "completion"})
COMPLETION_LEVELS = {"contributed": 2, "validated": 3}
STATUS_RANK = {"planned": 0, "in-progress": 1, "contributed": 2, "validated": 3}
VALIDATED_MATURITY = {"validated", "production"}
CONTRIBUTED_MATURITY = {"contributed", "validated", "production"}
FENCED_YAML = re.compile(r"^```ya?ml[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)


class DefinitionError(ValueError):
    """A tracking issue definition that cannot be evaluated safely."""


def _labels(issue: dict) -> set[str]:
    names = set()
    for label in issue.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.add(name)
    return names


def is_tracking_issue(issue: dict) -> bool:
    """Return whether an issue claims to be, or is labeled as, a tracker entry."""
    if "pull_request" in issue:
        return False
    body = issue.get("body") or ""
    return TRACKER_MARKER in body or LABEL in _labels(issue)


_NO_NON_STRING_KEY = object()


def _unknown_keys(
    value: dict, allowed: frozenset[str], location: str,
    optional: frozenset[str] = frozenset(),
) -> None:
    non_string = next(
        (key for key in value if not isinstance(key, str)), _NO_NON_STRING_KEY,
    )
    if non_string is not _NO_NON_STRING_KEY:
        raise DefinitionError(f"{location}: mapping keys must be strings, got {non_string!r}")
    unknown = sorted(set(value) - allowed)
    missing = sorted(allowed - set(value) - optional)
    if unknown:
        raise DefinitionError(f"{location}: unknown key(s): {', '.join(unknown)}")
    if missing:
        raise DefinitionError(f"{location}: missing required key(s): {', '.join(missing)}")


def _text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DefinitionError(f"{location}: expected a non-empty string")
    return value.strip()


def parse_definition(body: str) -> dict:
    """Extract and validate the single fenced YAML target definition."""
    blocks = FENCED_YAML.findall(body or "")
    if len(blocks) != 1:
        raise DefinitionError(
            "expected exactly one fenced yaml model target definition; "
            f"found {len(blocks)}. Keep the template's ```yaml block intact."
        )
    try:
        value = load_unique_yaml(blocks[0])
    except yaml.YAMLError as error:
        raise DefinitionError(f"target definition is not valid YAML: {error}") from error
    if not isinstance(value, dict):
        raise DefinitionError("target definition must be a YAML mapping")
    _unknown_keys(value, TOP_LEVEL_KEYS, "definition", frozenset({"jira", "blockers"}))

    schema_version = value["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        raise DefinitionError("definition.schema_version: expected integer 1")
    model_id = _text(value["model_id"], "definition.model_id")
    if not MODEL_ID.fullmatch(model_id):
        raise DefinitionError("definition.model_id: use lowercase letters, digits, '.', '_' or '-'")
    owner = _text(value["owner"], "definition.owner")
    if not OWNER.fullmatch(owner):
        raise DefinitionError(
            "definition.owner: expected a GitHub user or team handle such as @octocat or @org/team"
        )
    if model_id == PLACEHOLDERS["model_id"] or owner == PLACEHOLDERS["owner"]:
        raise DefinitionError(
            "definition: replace the issue form placeholders with the model ID and owner"
        )
    completion = _text(value["completion"], "definition.completion")
    if completion not in COMPLETION_LEVELS:
        raise DefinitionError(
            "definition.completion: expected 'contributed' or 'validated'"
        )

    jira = value.get("jira")
    if jira is not None:
        jira = _text(jira, "definition.jira")
        if not JIRA_KEY.fullmatch(jira):
            raise DefinitionError(
                "definition.jira: expected an internal Jira issue key such as RHOAIENG-123; "
                "do not include descriptions or links"
            )

    blockers = value.get("blockers", [])
    if not isinstance(blockers, list) or any(not isinstance(item, str) or not item.strip() for item in blockers):
        raise DefinitionError("definition.blockers: expected a list of non-empty strings")
    blockers = [item.strip() for item in blockers]
    if len(blockers) > MAX_BLOCKERS:
        raise DefinitionError(f"definition.blockers: at most {MAX_BLOCKERS} entries per model")

    targets = value["targets"]
    if not isinstance(targets, list) or not targets:
        raise DefinitionError("definition.targets: expected at least one target")
    if len(targets) > MAX_TARGETS:
        raise DefinitionError(f"definition.targets: at most {MAX_TARGETS} targets per model")
    parsed_targets = []
    seen_recipes = set()
    seen_platforms = set()
    for index, target in enumerate(targets):
        location = f"definition.targets[{index}]"
        if not isinstance(target, dict):
            raise DefinitionError(f"{location}: expected a mapping")
        _unknown_keys(target, TARGET_KEYS, location)
        recipe_id = _text(target["recipe_id"], f"{location}.recipe_id")
        if recipe_id == PLACEHOLDERS["recipe_id"]:
            raise DefinitionError(
                f"{location}.recipe_id: replace the issue form recipe placeholder"
            )
        if not RECIPE_ID.fullmatch(recipe_id):
            raise DefinitionError(
                f"{location}.recipe_id: use the v4 recipe ID with letters, digits, '.', '_' or '-'"
            )
        if recipe_id in seen_recipes:
            raise DefinitionError(f"{location}.recipe_id: duplicate target recipe {recipe_id!r}")
        seen_recipes.add(recipe_id)
        platforms = target["platforms"]
        if not isinstance(platforms, list) or not platforms:
            raise DefinitionError(f"{location}.platforms: expected at least one platform")
        if len(platforms) > MAX_PLATFORMS:
            raise DefinitionError(f"{location}.platforms: at most {MAX_PLATFORMS} platforms")
        parsed_platforms = []
        for platform_index, platform in enumerate(platforms):
            platform_location = f"{location}.platforms[{platform_index}]"
            if not isinstance(platform, dict):
                raise DefinitionError(f"{platform_location}: expected a mapping")
            _unknown_keys(platform, PLATFORM_KEYS, platform_location, frozenset({"completion"}))
            stack = _text(platform["stack"], f"{platform_location}.stack")
            version = _text(platform["version"], f"{platform_location}.version")
            if not STACK.fullmatch(stack):
                raise DefinitionError(f"{platform_location}.stack: expected a stack identifier")
            if not VERSION.fullmatch(version):
                raise DefinitionError(f"{platform_location}.version: expected a version identifier")
            platform_completion = platform.get("completion")
            if platform_completion is not None:
                platform_completion = _text(platform_completion, f"{platform_location}.completion")
                if platform_completion not in COMPLETION_LEVELS:
                    raise DefinitionError(
                        f"{platform_location}.completion: expected 'contributed' or 'validated'"
                    )
            identity = (recipe_id, stack, version)
            if identity in seen_platforms:
                raise DefinitionError(
                    f"{platform_location}: duplicate target platform {stack}/{version} for {recipe_id}"
                )
            seen_platforms.add(identity)
            parsed_platforms.append({
                "stack": stack, "version": version, "completion": platform_completion,
            })
        parsed_targets.append({"recipe_id": recipe_id, "platforms": parsed_platforms})

    return {
        "schema_version": 1,
        "model_id": model_id,
        "owner": owner,
        "completion": completion,
        "targets": parsed_targets,
        "jira": jira,
        "blockers": blockers,
    }


def _issue_reference(issue: dict) -> dict:
    return {
        "number": issue.get("number"),
        "title": issue.get("title") or "",
        "url": issue.get("html_url") or issue.get("url") or "",
        "state": issue.get("state") or "unknown",
        "node_id": issue.get("node_id"),
    }


def _validation_applies(entry: dict, platform: dict) -> tuple[dict, bool, str | None]:
    """Select validation evidence for the exact target platform from that platform's runs.

    The catalog's ``validation`` field is the first applicable badge across all
    evidence, which may belong to a different platform. Re-run the existing
    catalog helper over only the evidence whose tested platform matches this
    target so a later exact run is not hidden by an earlier cross-platform run.
    """
    def tested_platform(item: dict) -> dict:
        return (((item.get("run") or {}).get("environment") or {}).get("platform") or {})

    filtered = [
        item for item in entry.get("evidence") or []
        if tested_platform(item).get("stack") == platform["stack"]
        and tested_platform(item).get("version") == platform["version"]
    ]
    engine_version = (entry.get("engine") or {}).get("version")
    same_engine = [
        item for item in filtered
        if engine_version
        and compare_versions(
            engine_version,
            ((item.get("run") or {}).get("environment") or {}).get("vllm_version"),
        ) == 0
    ]
    # Prefer an exact-current-engine run even when an earlier run is ordered
    # first; require same-engine for independent validation. If the same-engine
    # candidates are unusable, use the full exact-platform set only to explain
    # earlier-engine evidence; that explanation never validates the target.
    badge = validation_badge(entry, same_engine) if same_engine else validation_badge(entry, filtered)
    if same_engine and badge.get("state") != "same-engine":
        exact_badge = validation_badge(entry, filtered)
        if exact_badge.get("state") == "earlier-engine":
            badge = exact_badge
    state = badge.get("state")
    if state == "same-engine" and badge.get("run_id"):
        return badge, True, None
    if state == "earlier-engine":
        return badge, False, (
            f"benchmark evidence is for earlier engine {badge.get('tested_engine')}; "
            "that is compatible evidence, not independent validation of the current engine"
        )
    return badge, False, badge.get("reason") or "no applicable validated benchmark evidence"


def _platform_status(entry: dict | None, platform: dict) -> dict:
    if entry is None:
        return {
            "stack": platform["stack"],
            "version": platform["version"],
            "status": "planned",
            "completed": False,
            "maturity": None,
            "reason": "recipe or target platform is not present in the repository",
            "validation_state": "none",
            "validation_applies": False,
            "validation_label": None,
            "evidence_run_id": None,
            "recipe_source": None,
            "evidence_source": None,
            "evidence_pr_url": None,
        }
    if entry.get("blocked"):
        return {
            "stack": platform["stack"],
            "version": platform["version"],
            "status": "blocked",
            "completed": False,
            "maturity": entry.get("maturity"),
            "reason": entry.get("reason") or "blocked in the recipe definition",
            "validation_state": "none",
            "validation_applies": False,
            "validation_label": None,
            "evidence_run_id": None,
            "recipe_source": (entry.get("source") or {}).get("url"),
            "evidence_source": None,
            "evidence_pr_url": None,
        }

    maturity = entry.get("maturity")
    badge, applies, validation_note = _validation_applies(entry, platform)
    independently_validated = applies and maturity in VALIDATED_MATURITY
    if independently_validated:
        status = "validated"
        reason = badge.get("label") or "independently validated benchmark evidence"
    elif maturity in CONTRIBUTED_MATURITY:
        status = "contributed"
        reason = (
            "declared recipe maturity is validated, but this target platform has no "
            "same-engine committed benchmark evidence"
            if maturity in VALIDATED_MATURITY else "recipe is contributed"
        )
        if validation_note and maturity in VALIDATED_MATURITY:
            reason = f"{reason}: {validation_note}"
    else:
        status = "in-progress"
        reason = f"recipe maturity is {maturity or 'unknown'}"
        if validation_note and badge.get("state") not in {None, "none"}:
            reason = f"{reason}: {validation_note}"
    evidence_source = None
    evidence_pr_url = None
    if independently_validated:
        for item in entry.get("evidence") or []:
            if (item.get("run") or {}).get("run_id") == badge.get("run_id"):
                evidence_source = (item.get("result_source") or item.get("source") or {}).get("url")
                pr_number = ((item.get("run") or {}).get("forge") or {}).get("pr_number")
                if isinstance(pr_number, int) and pr_number > 0:
                    evidence_pr_url = f"{REPOSITORY_URL}/pull/{pr_number}"
                break
    return {
        "stack": platform["stack"],
        "version": platform["version"],
        "status": status,
        "completed": False,
        "maturity": maturity,
        "reason": reason,
        "validation_state": badge.get("state") or "none",
        "validation_applies": independently_validated,
        "validation_label": badge.get("label") if independently_validated else None,
        "evidence_run_id": badge.get("run_id") if independently_validated else None,
        "recipe_source": (entry.get("source") or {}).get("url"),
        "evidence_source": evidence_source,
        "evidence_pr_url": evidence_pr_url,
    }


def _target_status(platforms: list[dict]) -> str:
    statuses = [platform["status"] for platform in platforms]
    if all(status == "planned" for status in statuses):
        return "planned"
    if "blocked" in statuses:
        return "blocked"
    if all(status == "validated" for status in statuses):
        return "validated"
    if all(status in {"contributed", "validated"} for status in statuses):
        return "contributed"
    return "in-progress"


def _error_entry(reference: dict, message: str, definition: dict | None = None) -> dict:
    entry = {**reference, "error": message}
    if definition:
        entry.update({
            "model_id": definition["model_id"],
            "owner": definition["owner"],
            "blockers": definition["blockers"],
            "jira": definition["jira"],
        })
    else:
        entry["model_id"] = None
    return entry


def evaluate(catalog: dict, issues: list[dict]) -> dict:
    """Evaluate all tracking issues against an already-built catalog."""
    entries = catalog.get("entries") or []
    entry_index = {
        (
            entry["model_id"], entry["recipe_id"],
            entry["platform"]["stack"], entry["platform"]["version"],
        ): entry
        for entry in entries
    }

    errors = []
    models = {}
    definitions = {}
    duplicates = set()
    for issue in issues:
        if not is_tracking_issue(issue):
            continue
        reference = _issue_reference(issue)
        try:
            definition = parse_definition(issue.get("body") or "")
        except DefinitionError as error:
            errors.append(_error_entry(reference, str(error)))
            continue
        model_id = definition["model_id"]
        if model_id in models or model_id in duplicates:
            duplicates.add(model_id)
            previous = models.pop(model_id, None)
            if previous:
                errors.append(_error_entry(
                    previous["issue"],
                    f"model_id {model_id!r} is declared by more than one tracking issue "
                    f"(also issue #{issue.get('number')}); both declarations are invalid "
                    "until only one remains",
                    definitions.get(model_id),
                ))
            errors.append(_error_entry(
                reference,
                f"model_id {model_id!r} is declared by more than one tracking issue; "
                "keep exactly one tracking issue per model",
                definition,
            ))
            continue

        targets = []
        status_counts = {
            status: 0
            for status in ("planned", "in-progress", "contributed", "validated", "blocked")
        }
        completed_platforms = 0
        total_platforms = 0
        for target in definition["targets"]:
            platforms = []
            for platform in target["platforms"]:
                entry = entry_index.get(
                    (model_id, target["recipe_id"], platform["stack"], platform["version"])
                )
                state = _platform_status(entry, platform)
                required = platform.get("completion") or definition["completion"]
                state["completion_required"] = required
                state["completed"] = (
                    state["status"] in STATUS_RANK
                    and STATUS_RANK[state["status"]] >= COMPLETION_LEVELS[required]
                )
                status_counts[state["status"]] += 1
                total_platforms += 1
                if state["completed"]:
                    completed_platforms += 1
                platforms.append(state)
            target.update({
                "status": _target_status(platforms),
                "completed": all(platform["completed"] for platform in platforms),
                "platforms": platforms,
            })
            targets.append(target)

        platform_states = [
            platform["status"] for target in targets for platform in target["platforms"]
        ]
        if status_counts["blocked"]:
            status = "blocked"
        elif all(state == "validated" for state in platform_states):
            status = "validated"
        elif all(state in {"contributed", "validated"} for state in platform_states):
            status = "contributed"
        elif all(state == "planned" for state in platform_states):
            status = "planned"
        else:
            status = "in-progress"

        models[model_id] = {
            "model_id": model_id,
            "owner": definition["owner"],
            "completion": definition["completion"],
            "status": status,
            "completed": completed_platforms == total_platforms,
            "in_repository": model_id in {
                model["id"] for model in catalog.get("models") or []
            },
            "issue": reference,
            "jira": (
                {"key": definition["jira"], "note": JIRA_NOTE}
                if definition["jira"] else None
            ),
            "blockers": definition["blockers"],
            "coverage": {
                "target_recipes": len(targets),
                "target_platforms": total_platforms,
                "completed_platforms": completed_platforms,
                "remaining_platforms": total_platforms - completed_platforms,
                "completion_required": definition["completion"],
                "status_counts": status_counts,
                "contributed_or_validated_platforms": (
                    status_counts["contributed"] + status_counts["validated"]
                ),
                "independently_validated_platforms": status_counts["validated"],
            },
            "targets": targets,
        }
        definitions[model_id] = definition

    catalog_models = sorted(model["id"] for model in catalog.get("models") or [])
    untracked = [
        model for model in catalog_models
        if model not in models
    ]
    planned_models = sorted(
        model_id for model_id, model in models.items()
        if not model["in_repository"]
    )
    errors.sort(key=lambda item: (item.get("number") or 0, item.get("error") or ""))
    return {
        "schema_version": 1,
        "catalog": {
            "schema_version": catalog.get("schema_version"),
            "source_sha": (catalog.get("build") or {}).get("source_sha"),
            "dirty": (catalog.get("build") or {}).get("dirty"),
        },
        "tracking_issue_count": sum(1 for issue in issues if is_tracking_issue(issue)),
        "models": sorted(models.values(), key=lambda model: model["model_id"]),
        "untracked_models": untracked,
        "planned_models": planned_models,
        "errors": errors,
    }


def coverage_text(model: dict) -> str:
    coverage = model["coverage"]
    complete = coverage["completed_platforms"] == coverage["target_platforms"]
    return (
        f"{'complete' if complete else 'partial'}; "
        f"{coverage['completed_platforms']}/{coverage['target_platforms']} platforms meet "
        f"their criterion (contributed+ {coverage['contributed_or_validated_platforms']}, "
        f"validated {coverage['independently_validated_platforms']}; "
        f"default {coverage['completion_required']})"
    )


def to_markdown(report: dict) -> str:
    """Render a deterministic reader view from the evaluator JSON."""
    lines = [
        "# Model coverage tracker",
        "",
        f"Catalog source: `{(report.get('catalog') or {}).get('source_sha') or 'unknown'}`"
        f"{' (dirty checkout)' if (report.get('catalog') or {}).get('dirty') else ''}. "
        "Status is derived from committed recipes and benchmark evidence; CI success alone is not validation.",
        "",
        "## Summary",
        "",
        "| Model | Owner | Status | Completed | Criterion | Coverage | Tracker issue |",
        "|-------|-------|--------|-----------|-----------|----------|---------------|",
    ]
    for model in report["models"]:
        issue = model["issue"]
        lines.append(
            f"| `{model['model_id']}` | {model['owner']} | {model['status']} | "
            f"{'yes' if model['completed'] else 'no'} | "
            f"{model['completion']} | {coverage_text(model)} | "
            f"[#{issue['number']}]({issue['url']}) |"
        )
    if not report["models"]:
        lines.append("| _No tracking issues yet_ | — | — | — | — | — | — |")

    for model in report["models"]:
        issue = model["issue"]
        lines.extend([
            "",
            f"## `{model['model_id']}`",
            "",
            f"- Owner: {model['owner']}",
            f"- Tracker issue: [#{issue['number']}]({issue['url']}) ({issue['state']})",
            f"- Delivery status: **{model['status']}**"
            f"{' (all targets complete)' if model['completed'] else ' (partial coverage)'}",
            f"- Completion criterion: {model['completion']}",
            f"- Coverage: {coverage_text(model)}",
            f"- Recipe model present: {'yes' if model['in_repository'] else 'no (planned)'}",
        ])
        if model["jira"]:
            lines.append(f"- Jira: {model['jira']['key']} — {model['jira']['note']}")
        if model["blockers"]:
            lines.append("- Blockers:")
            lines.extend(f"  - {blocker}" for blocker in model["blockers"])
        else:
            lines.append("- Blockers: none recorded")
        lines.extend([
            "",
            "| Recipe | Platform | Requirement | Repository maturity | Tracker status | Evidence |",
            "|--------|----------|-------------|---------------------|----------------|----------|",
        ])
        for target in model["targets"]:
            for platform in target["platforms"]:
                evidence = (
                    f"[`{platform['evidence_run_id']}`]({platform['evidence_source']})"
                    if platform["evidence_run_id"] and platform["evidence_source"]
                    else f"`{platform['evidence_run_id']}`"
                    if platform["evidence_run_id"] else platform["reason"]
                )
                if platform.get("evidence_pr_url"):
                    evidence += f" · [PR]({platform['evidence_pr_url']})"
                recipe = (
                    f"[`{target['recipe_id']}`]({platform['recipe_source']})"
                    if platform.get("recipe_source") else f"`{target['recipe_id']}`"
                )
                lines.append(
                    f"| {recipe} | {platform['stack']} {platform['version']} | "
                    f"{platform['completion_required']} | "
                    f"{platform['maturity'] or '—'} | {platform['status']} | {evidence} |"
                )
        lines.append("")
        lines.append("Partial targets never mark the model complete. A validation badge for another "
                     "platform, an `earlier-engine` run, or a same-configuration compatibility claim "
                     "is not independent validation of the target platform on the current engine.")

    if report["untracked_models"]:
        lines.extend([
            "",
            "## Existing models without a tracking issue",
            "",
            "These repository models are not represented by a tracking issue and are not counted "
            "as planned or complete:",
            "",
        ])
        lines.extend(f"- `{model}`" for model in report["untracked_models"])
    if report["planned_models"]:
        lines.extend([
            "",
            "## Planned models not yet in the repository",
            "",
        ])
        lines.extend(f"- `{model}`" for model in report["planned_models"])
    if report["errors"]:
        lines.extend(["", "## Definition errors", ""])
        for error in report["errors"]:
            number = error.get("number")
            model = f" (`{error['model_id']}`)" if error.get("model_id") else ""
            lines.append(f"- Issue #{number}{model}: {error['error']}")
    return "\n".join(lines).rstrip() + "\n"


def load_issues(path: str) -> list[dict]:
    """Load issues from a gh JSON list, an object with an issues key, or stdin."""
    text = sys.stdin.read() if path == "-" else Path(path).read_text()
    value = json.loads(text)
    if isinstance(value, dict):
        value = value.get("issues")
    if (isinstance(value, list) and value
            and all(isinstance(page, list) for page in value)):
        value = [item for page in value for item in page]
    if not isinstance(value, list) or any(not isinstance(issue, dict) for issue in value):
        raise ValueError(
            "issues input must be a JSON issue list, paginated page list, "
            "or an object with an 'issues' list"
        )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--issues", required=True, help="gh issue JSON file, or - for stdin")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--out", type=Path, help="write output here instead of stdout")
    parser.add_argument(
        "--allow-errors", action="store_true",
        help="write the report and exit 0 when issue definitions are invalid "
             "(catalog/input failures still exit 2)",
    )
    args = parser.parse_args()
    try:
        catalog = build_catalog(args.repo)
        report = evaluate(catalog, load_issues(args.issues))
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        print(f"Model coverage evaluation failed: {error}", file=sys.stderr)
        return 2
    output = (
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if args.format == "json" else to_markdown(report)
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output)
    else:
        print(output, end="")
    if report["errors"]:
        for error in report["errors"]:
            print(f"Issue #{error.get('number')}: {error['error']}", file=sys.stderr)
        if not args.allow_errors:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
