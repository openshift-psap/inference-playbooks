"""Tests for the issue-driven model coverage tracker (offline, no GitHub calls)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

from model_coverage import (  # noqa: E402
    DefinitionError, JIRA_NOTE, TRACKER_MARKER, build_catalog, evaluate,
    is_tracking_issue, parse_definition, to_markdown,
)
from model_coverage_sync import (  # noqa: E402
    BLOCKED_FIELD, COMMENT_MARKER, COMPLETED_FIELD, COMPLETION_FIELD,
    LAST_EVALUATED_FIELD, SINGLE_SELECT_FIELDS, STATUS_FIELD, TEXT_FIELDS,
    GhClient, SyncApplyError, SyncError, apply_plan, assert_trusted_write_context,
    bind_repository_issues, build_stale_plan, build_sync_plan, desired_fields,
    fetch_items, main, resolve_comment_owner,
)
import model_coverage_sync as sync_module  # noqa: E402


SIGNATURE = {
    "model_revision": "checkpoint@sha256:" + "a" * 64,
    "workload_sha256": "b" * 64,
    "final_runtime": "c" * 64,
}
IMAGE = "quay.io/example/runtime@sha256:" + "d" * 64


def platform(stack="vllm", version="v0.24.0"):
    return {"stack": stack, "version": version}


def evidence_item(run_id, stack="vllm", version="v0.24.0",
                  engine_version="v0.24.0", pr_number=None,
                  image=IMAGE, compatibility=SIGNATURE):
    run = {
        "run_id": run_id,
        "environment": {
            "platform": platform(stack, version),
            "image": image,
            "vllm_version": engine_version,
            "catalog_compatibility": compatibility,
        },
    }
    if pr_number:
        run["forge"] = {"pr_number": pr_number}
    return {
        "run": run,
        "source": {"url": f"https://example.test/{run_id}/run.yaml"},
        "result_source": {"url": f"https://example.test/{run_id}/result.json"},
    }


def entry(model_id, recipe_id, stack="vllm", version="v0.24.0", *,
          maturity="day-zero", blocked=False, validation=None, evidence=(),
          engine_version="v0.24.0"):
    validation = validation or {
        "state": "none", "label": None, "run_id": None,
        "reason": "No applicable validated benchmark evidence",
    }
    return {
        "model_id": model_id,
        "recipe_id": recipe_id,
        "platform": platform(stack, version),
        "maturity": maturity,
        "blocked": blocked,
        "reason": "blocked for a fixture reason" if blocked else None,
        "validation": validation,
        "engine": {"state": "resolved", "version": engine_version},
        "compatibility_signature": SIGNATURE,
        "source": {"path": f"models/{model_id}/recipes/{recipe_id}/recipe.yaml",
                   "url": f"https://example.test/{recipe_id}/recipe.yaml"},
        "evidence": [evidence_item(**item) for item in evidence],
    }


def catalog(*entries, model_ids=None):
    ids = sorted(model_ids or {item["model_id"] for item in entries})
    return {
        "schema_version": 2,
        "build": {"source_sha": "a" * 40, "dirty": False},
        "models": [{"id": model_id} for model_id in ids],
        "entries": list(entries),
    }


def definition(model_id="model-a", owner="@owner", completion="contributed", targets=None, **extra):
    value = {
        "schema_version": 1,
        "model_id": model_id,
        "owner": owner,
        "completion": completion,
        "blockers": [],
        "targets": targets or [
            {"recipe_id": "recipe-a", "platforms": [platform()]},
        ],
    }
    value.update(extra)
    return value


def issue(number, value=None, *, body=None, node_id=None, labels=("model-coverage",)):
    if body is None:
        body = f"{TRACKER_MARKER}\n```yaml\n{yaml.safe_dump(value, sort_keys=False)}```\n"
    return {
        "number": number,
        "title": f"model {number}",
        "html_url": f"https://github.test/o/r/issues/{number}",
        "url": f"https://api.github.test/repos/o/r/issues/{number}",
        "state": "open",
        "node_id": node_id or f"I_{number}",
        "labels": [{"name": name} for name in labels],
        "body": body,
    }


def test_partial_targets_never_complete_the_model():
    repository = catalog(
        entry("model-a", "recipe-a"),
        entry("model-a", "recipe-a", "rhoai", "3.5", maturity="contributed"),
        entry("model-a", "recipe-a", "vllm", "v0.25.0", maturity="contributed"),
    )
    report = evaluate(repository, [issue(101, definition(targets=[
        {"recipe_id": "recipe-a", "platforms": [
            platform(), platform("rhoai", "3.5"), platform("vllm", "v0.25.0"),
        ]},
        {"recipe_id": "recipe-planned", "platforms": [platform()]},
    ]))])
    model = report["models"][0]
    assert model["completed"] is False
    assert model["status"] == "in-progress"
    assert model["coverage"]["target_platforms"] == 4
    assert model["coverage"]["completed_platforms"] == 2
    assert model["coverage"]["remaining_platforms"] == 2
    assert model["coverage"]["status_counts"] == {
        "planned": 1, "in-progress": 1, "contributed": 2, "validated": 0, "blocked": 0,
    }
    assert model["targets"][1]["status"] == "planned"
    assert report["errors"] == []


def test_ci_or_cross_platform_badge_does_not_satisfy_validated():
    cross_validation = {
        "state": "same-engine", "label": "Validated", "run_id": "run-cross",
        "tested_platform": platform("rhoai", "3.5"),
    }
    repository = catalog(
        entry("model-a", "recipe-a", maturity="contributed"),
        entry("model-a", "recipe-cross", maturity="validated",
              validation=cross_validation,
              evidence=[{"run_id": "run-cross", "stack": "rhoai", "version": "3.5"}]),
        entry("model-a", "recipe-missing-badge", maturity="validated"),
    )
    report = evaluate(repository, [issue(102, definition(completion="validated", targets=[
        {"recipe_id": "recipe-a", "platforms": [platform()]},
        {"recipe_id": "recipe-cross", "platforms": [platform()]},
        {"recipe_id": "recipe-missing-badge", "platforms": [platform()]},
    ]))])
    model = report["models"][0]
    assert model["completed"] is False
    assert model["status"] == "contributed"
    assert model["coverage"]["independently_validated_platforms"] == 0
    assert [item["status"] for item in model["targets"][1]["platforms"]] == ["contributed"]
    assert "no same-engine committed benchmark evidence" in model["targets"][1]["platforms"][0]["reason"]
    assert "No applicable validated benchmark evidence" in model["targets"][2]["platforms"][0]["reason"]


def test_competing_ordered_evidence_uses_the_exact_target_run():
    # The catalog's first badge belongs to another platform; a later evidence
    # item is the exact target and must win after filtering.
    cross = {
        "state": "same-engine", "label": "Validated", "run_id": "run-cross",
        "tested_platform": platform("rhoai", "3.5"),
    }
    repository = catalog(entry(
        "model-a", "recipe-a", maturity="validated", validation=cross,
        evidence=[
            {"run_id": "run-cross", "stack": "rhoai", "version": "3.5"},
            {"run_id": "run-target", "pr_number": 7},
        ],
    ))
    model = evaluate(
        repository, [issue(115, definition(completion="validated"))]
    )["models"][0]
    platform_state = model["targets"][0]["platforms"][0]
    assert model["completed"] is True
    assert platform_state["status"] == "validated"
    assert platform_state["evidence_run_id"] == "run-target"
    assert platform_state["evidence_source"].endswith("run-target/result.json")
    assert platform_state["evidence_pr_url"].endswith("/pull/7")
    assert platform_state["validation_label"] == "Validated"


def test_same_engine_exact_run_wins_over_earlier_ordered_run():
    repository = catalog(entry(
        "model-a", "recipe-a", maturity="validated",
        evidence=[
            {"run_id": "run-older", "engine_version": "v0.23.0"},
            {"run_id": "run-current"},
        ],
    ))
    model = evaluate(
        repository, [issue(117, definition(completion="validated"))]
    )["models"][0]
    platform_state = model["targets"][0]["platforms"][0]
    assert model["completed"] is True
    assert platform_state["validation_state"] == "same-engine"
    assert platform_state["evidence_run_id"] == "run-current"


def test_exact_platform_committed_evidence_completes_validated():
    validation = {
        "state": "same-engine", "label": "Validated", "run_id": "run-1",
        "tested_platform": platform(),
    }
    repository = catalog(entry(
        "model-a", "recipe-a", maturity="validated",
        validation=validation, evidence=[{"run_id": "run-1", "pr_number": 42}],
    ))
    report = evaluate(repository, [issue(103, definition(completion="validated"))])
    model = report["models"][0]
    assert model["completed"] is True
    assert model["status"] == "validated"
    assert model["targets"][0]["platforms"][0]["status"] == "validated"
    assert model["targets"][0]["platforms"][0]["evidence_source"].endswith("result.json")
    assert model["targets"][0]["platforms"][0]["evidence_pr_url"].endswith("/pull/42")
    markdown = to_markdown(report)
    assert "https://example.test/run-1/result.json" in markdown
    assert "/pull/42" in markdown
    assert "all targets complete" in markdown


def test_earlier_engine_exact_platform_is_not_independent_validation():
    validation = {
        "state": "earlier-engine", "label": "Validated on earlier vLLM — v0.23.0",
        "run_id": "run-older", "tested_platform": platform(),
    }
    repository = catalog(entry(
        "model-a", "recipe-a", maturity="validated",
        validation=validation,
        evidence=[{"run_id": "run-older", "engine_version": "v0.23.0"}],
    ))
    model = evaluate(repository, [issue(113, definition(completion="validated"))])["models"][0]
    platform_state = model["targets"][0]["platforms"][0]
    assert model["completed"] is False
    assert model["status"] == "contributed"
    assert platform_state["status"] == "contributed"
    assert platform_state["validation_state"] == "earlier-engine"
    assert platform_state["evidence_run_id"] is None
    assert "earlier engine v0.23.0" in platform_state["reason"]
    assert "compatible evidence" in platform_state["reason"]


def test_unusable_same_engine_falls_back_to_earlier_engine_explanation():
    repository = catalog(entry(
        "model-a", "recipe-a", maturity="validated",
        evidence=[
            # Same engine version, but the image is not digest-pinned, so the
            # same-engine candidate cannot produce a badge.
            {"run_id": "run-broken-same-engine",
             "image": "quay.io/example/runtime:latest"},
            {"run_id": "run-older", "engine_version": "v0.23.0"},
        ],
    ))
    model = evaluate(
        repository, [issue(118, definition(completion="validated"))]
    )["models"][0]
    platform_state = model["targets"][0]["platforms"][0]
    assert model["completed"] is False
    assert model["status"] == "contributed"
    assert platform_state["status"] == "contributed"
    assert platform_state["validation_state"] == "earlier-engine"
    assert platform_state["evidence_run_id"] is None
    assert "earlier engine v0.23.0" in platform_state["reason"]
    assert "not independent validation" in platform_state["reason"]


def test_blocked_recipe_never_completes():
    repository = catalog(entry("a", "recipe-a", maturity="contributed", blocked=True))
    model = evaluate(repository, [issue(104, definition(model_id="a"))])["models"][0]
    assert model["completed"] is False
    assert model["status"] == "blocked"
    assert model["targets"][0]["platforms"][0]["status"] == "blocked"


def test_existing_untracked_and_planned_models_are_reported():
    repository = catalog(entry("tracked", "recipe-a"), model_ids={"tracked", "untracked"})
    report = evaluate(repository, [issue(105, definition(model_id="tracked"))])
    assert report["untracked_models"] == ["untracked"]
    assert report["planned_models"] == []

    planned = evaluate(repository, [issue(106, definition(model_id="future-model", targets=[
        {"recipe_id": "future-recipe", "platforms": [platform()]},
    ]))])
    assert planned["planned_models"] == ["future-model"]
    assert planned["models"][0]["in_repository"] is False
    assert planned["models"][0]["status"] == "planned"

    assert evaluate(repository, [])["untracked_models"] == ["tracked", "untracked"]


def test_closing_or_removing_one_identifier_keeps_the_issue_tracked():
    repository = catalog(entry("a", "recipe-a", maturity="contributed"), model_ids={"a"})
    tracked = issue(120, definition(model_id="a"))
    assert evaluate(repository, [tracked])["models"][0]["model_id"] == "a"

    closed = {**tracked, "state": "closed"}
    assert evaluate(repository, [closed])["models"][0]["model_id"] == "a"

    without_label = {**tracked, "labels": []}
    assert evaluate(repository, [without_label])["models"][0]["model_id"] == "a"

    no_marker_body = f"```yaml\n{yaml.safe_dump(definition(model_id='a'), sort_keys=False)}```\n"
    without_marker = {**tracked, "body": no_marker_body}
    assert evaluate(repository, [without_marker])["models"][0]["model_id"] == "a"

    untracked = {**tracked, "labels": [], "body": no_marker_body}
    report = evaluate(repository, [untracked])
    assert report["models"] == []
    assert report["untracked_models"] == ["a"]

    deleted_yaml = {**tracked, "body": f"{TRACKER_MARKER}\nDefinition removed.\n"}
    report = evaluate(repository, [deleted_yaml])
    assert report["models"] == []
    assert [error["number"] for error in report["errors"]] == [120]
    assert "exactly one fenced yaml" in report["errors"][0]["error"]


def test_current_repository_models_are_reported_when_untracked():
    repository = build_catalog(REPO)
    report = evaluate(repository, [])
    assert report["errors"] == []
    assert report["untracked_models"] == sorted(
        model["id"] for model in repository["models"]
    )


def test_current_repository_kimi_k3_reaches_contributed_without_ci_validation():
    report = evaluate(build_catalog(REPO), [issue(114, definition(
        model_id="kimi-k3",
        targets=[{
            "recipe_id": "kimi-k3-vllm-h200-pp2-tp8",
            "platforms": [platform("vllm", "v0.29.0")],
        }],
    ))])
    model = report["models"][0]
    assert model["status"] == "contributed"
    assert model["completed"] is True
    assert model["coverage"]["contributed_or_validated_platforms"] == 1
    assert model["coverage"]["independently_validated_platforms"] == 0
    assert "kimi-k3" not in report["untracked_models"]


@pytest.mark.parametrize(("body", "message"), [
    (f"{TRACKER_MARKER}\n```yaml\nschema_version: 1\nmodel_id: a\nmodel_id: b\n```\n", "duplicate key"),
    (f"{TRACKER_MARKER}\n```yaml\nschema_version: 1\n```\n```yaml\nschema_version: 1\n```\n", "exactly one"),
    (f"{TRACKER_MARKER}\n```yaml\nschema_version: 1\nmodel_id: a\nowner: o\ncompletion: contributed\ntargets: []\ntypo: true\n```\n", "unknown key"),
    (f"{TRACKER_MARKER}\n```yaml\nschema_version: 1\nmodel_id: a\nowner: o\ncompletion: maybe\ntargets:\n  - recipe_id: r\n    platforms: [{{stack: vllm, version: v1}}]\n```\n", "contributed"),
    (f"{TRACKER_MARKER}\n```yaml\nschema_version: 1\nmodel_id: a\nowner: o\ncompletion: contributed\njira: 'a description, not a key'\ntargets:\n  - recipe_id: r\n    platforms: [{{stack: vllm, version: v1}}]\n```\n", "Jira issue key"),
    (f"{TRACKER_MARKER}\n```yaml\nschema_version: 1\nmodel_id: a\nowner: o\ncompletion: contributed\ntargets:\n  - recipe_id: r\n    platforms: [{{stack: vllm, version: v1}}, {{stack: vllm, version: v1}}]\n```\n", "duplicate target platform"),
])
def test_malformed_definitions_are_actionable(body, message):
    report = evaluate(catalog(entry("a", "r")), [issue(107, body=body)])
    assert len(report["errors"]) == 1
    assert report["errors"][0]["number"] == 107
    assert message in report["errors"][0]["error"]


def test_duplicate_model_issues_invalidate_both_declarations():
    repository = catalog(entry("a", "recipe-a", maturity="contributed"))
    duplicate = evaluate(repository, [
        issue(108, definition(model_id="a")),
        issue(109, definition(model_id="a")),
    ])
    assert duplicate["models"] == []
    assert {error["number"] for error in duplicate["errors"]} == {108, 109}
    assert all(error["model_id"] == "a" for error in duplicate["errors"])
    assert all("more than one tracking issue" in error["error"] for error in duplicate["errors"])
    # Nothing is published as complete, and the still-real repository model is
    # reported as untracked rather than silently claimed.
    assert duplicate["untracked_models"] == ["a"]


def test_recipe_ids_are_scoped_to_their_own_model():
    # The same recipe_id legitimately exists under another model. A missing
    # target under this model is planned, not a global-uniqueness error.
    repository = catalog(
        entry("a", "shared-recipe", maturity="contributed"),
        entry("b", "shared-recipe", maturity="contributed"),
    )
    report = evaluate(repository, [issue(110, definition(model_id="a", targets=[
        {"recipe_id": "shared-recipe", "platforms": [platform()]},
        {"recipe_id": "only-under-b", "platforms": [platform()]},
    ]))])
    assert report["errors"] == []
    model = report["models"][0]
    assert model["targets"][0]["status"] == "contributed"
    assert model["targets"][1]["status"] == "planned"
    assert model["targets"][1]["platforms"][0]["status"] == "planned"


def test_per_platform_completion_overrides_the_model_default():
    repository = catalog(
        entry("a", "recipe-a", maturity="contributed"),
        entry("a", "recipe-b", maturity="contributed", stack="rhoai", version="3.5"),
    )
    report = evaluate(repository, [issue(116, definition(model_id="a", targets=[
        {"recipe_id": "recipe-a", "platforms": [platform()]},
        {"recipe_id": "recipe-b", "platforms": [
            {"stack": "rhoai", "version": "3.5", "completion": "validated"},
        ]},
    ]))])
    model = report["models"][0]
    first, second = (target["platforms"][0] for target in model["targets"])
    assert first["completion_required"] == "contributed"
    assert first["completed"] is True
    assert second["completion_required"] == "validated"
    assert second["completed"] is False
    assert model["coverage"]["completed_platforms"] == 1
    assert model["completed"] is False
    assert "| contributed |" in to_markdown(report)
    assert "| validated |" in to_markdown(report)


def test_definition_keys_must_be_strings():
    body = (
        f"{TRACKER_MARKER}\n```yaml\n"
        "schema_version: 1\nmodel_id: a\nowner: o\ncompletion: contributed\n"
        "targets:\n  - recipe_id: r\n    platforms: [{stack: vllm, version: v1}]\n"
        "1: true\n```\n"
    )
    with pytest.raises(DefinitionError, match="mapping keys must be strings"):
        parse_definition(body)


def test_null_mapping_key_is_a_definition_error_not_a_type_error():
    body = (
        f"{TRACKER_MARKER}\n```yaml\n"
        "schema_version: 1\nmodel_id: a\nowner: o\ncompletion: contributed\n"
        "targets:\n  - recipe_id: r\n    platforms: [{stack: vllm, version: v1}]\n"
        "~: true\n```\n"
    )
    with pytest.raises(DefinitionError, match="mapping keys must be strings, got None"):
        parse_definition(body)


def test_schema_version_must_be_an_integer_not_float():
    body = (
        f"{TRACKER_MARKER}\n```yaml\n"
        "schema_version: 1.0\nmodel_id: a\nowner: o\ncompletion: contributed\n"
        "targets:\n  - recipe_id: r\n    platforms: [{stack: vllm, version: v1}]\n```\n"
    )
    with pytest.raises(DefinitionError, match="expected integer 1"):
        parse_definition(body)


def test_jira_key_is_rendered_as_fixed_referral_only():
    report = evaluate(catalog(entry("a", "recipe-a")), [
        issue(111, definition(model_id="a", jira="RHOAIENG-123")),
    ])
    assert report["models"][0]["jira"] == {"key": "RHOAIENG-123", "note": JIRA_NOTE}
    markdown = to_markdown(report)
    assert JIRA_NOTE in markdown
    assert "RHOAIENG-123" in markdown


def test_output_is_deterministic():
    repository = catalog(entry("a", "recipe-a", maturity="contributed"))
    issues = [issue(112, definition(model_id="a", blockers=["waiting for hardware"]))]
    first = evaluate(repository, issues)
    second = evaluate(repository, issues)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first == second
    assert "waiting for hardware" in to_markdown(first)


def project():
    fields = {}
    for index, name in enumerate(TEXT_FIELDS):
        fields[name] = {"id": f"F{index}", "name": name, "dataType": "TEXT", "options": {}}
    for index, (name, options) in enumerate(
        SINGLE_SELECT_FIELDS.items(), start=len(fields)
    ):
        fields[name] = {
            "id": f"F{index}", "name": name, "dataType": "SINGLE_SELECT",
            "options": {option: f"O{index}-{offset}" for offset, option in enumerate(options)},
        }
    return {"id": "PROJECT", "title": "Model coverage", "fields": fields}


def report_with_model(completion="contributed", target_status="contributed"):
    validation = {
        "state": "same-engine" if target_status == "validated" else "none",
        "label": "Validated" if target_status == "validated" else None,
        "run_id": "run-1" if target_status == "validated" else None,
        "tested_platform": platform(),
    }
    repository = catalog(entry(
        "model-a", "recipe-a",
        maturity="validated" if target_status == "validated" else "contributed",
        validation=validation,
        evidence=[{"run_id": "run-1"}] if target_status == "validated" else [],
    ))
    return evaluate(repository, [issue(200, definition(completion=completion))])


def item_for(model, project_document, *, human_fields=True, source_sha="unknown",
             repository="o/r", url=None):
    nodes = []
    for field in desired_fields(model, project_document, source_sha=source_sha):
        if field["data_type"] == "TEXT":
            nodes.append({
                "__typename": "ProjectV2ItemFieldTextValue",
                "field": {"name": field["field"]},
                "text": field["value"],
            })
        else:
            nodes.append({
                "__typename": "ProjectV2ItemFieldSingleSelectValue",
                "field": {"name": field["field"]},
                "name": field["value"],
                "optionId": field["option_id"],
            })
    if human_fields:
        nodes.append({
            "__typename": "ProjectV2ItemFieldTextValue",
            "field": {"name": "Human Notes"},
            "text": "keep me",
        })
    return {
        "id": "ITEM1",
        "content": {
            "__typename": "Issue",
            "url": url or model["issue"]["url"],
            "repository": {"nameWithOwner": repository},
        },
        "fieldValues": {"pageInfo": {"hasNextPage": False}, "nodes": nodes},
    }


def repository_issue(number, value=None, *, repository="o/r", node_id=None, body=None):
    document = issue(number, value, node_id=node_id, body=body)
    document["html_url"] = f"https://github.com/{repository}/issues/{number}"
    document["url"] = f"https://api.github.com/repos/{repository}/issues/{number}"
    return document


class IssueReadClient:
    def __init__(self, issues):
        self.issues = {item["number"]: item for item in issues}
        self.calls = []

    def rest(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        assert method == "GET"
        return self.issues[int(path.rsplit("/", 1)[-1])]


def test_sync_binds_supplied_issues_to_authoritative_repository_issue():
    authoritative = repository_issue(200, definition(model_id="model-a"))
    supplied = {**authoritative, "node_id": None, "body": "stale snapshot body"}
    client = IssueReadClient([authoritative])

    bound = bind_repository_issues(client, "o/r", [supplied])

    assert bound == [authoritative]
    assert bound[0]["node_id"] == "I_200"
    assert bound[0]["body"] == authoritative["body"]
    assert client.calls == [("GET", "repos/o/r/issues/200", None)]


@pytest.mark.parametrize(("change", "message"), [
    ({"html_url": "https://github.com/other/repo/issues/200"}, "does not belong to o/r"),
    ({"html_url": "https://github.com/o/r/issues/not-a-number"}, "expected https://github.com/o/r/issues/200"),
    ({"number": 201}, "expected https://github.com/o/r/issues/201"),
    ({"url": "https://api.github.com/repos/other/repo/issues/200"}, "url .* does not belong"),
    ({"html_url": "http://github.com/o/r/issues/200"}, "does not belong to o/r"),
    ({"html_url": None, "url": None}, "has no html_url or url"),
])
def test_sync_rejects_foreign_or_malformed_issue_identity(change, message):
    supplied = {**repository_issue(200, definition(model_id="model-a")), **change}
    client = IssueReadClient([])
    with pytest.raises(SyncError, match=message):
        bind_repository_issues(client, "o/r", [supplied])
    assert client.calls == []


def test_sync_accepts_browser_url_when_html_url_is_absent():
    authoritative = repository_issue(200, definition(model_id="model-a"))
    supplied = {
        **authoritative,
        "html_url": None,
        "url": authoritative["html_url"],
    }
    client = IssueReadClient([authoritative])

    assert bind_repository_issues(client, "o/r", [supplied]) == [authoritative]


def test_sync_rejects_node_id_not_bound_to_the_authoritative_issue():
    authoritative = repository_issue(200, definition(model_id="model-a"))
    supplied = {**authoritative, "node_id": "I_FOREIGN"}
    client = IssueReadClient([authoritative])
    with pytest.raises(SyncError, match="node_id.*not bound"):
        bind_repository_issues(client, "o/r", [supplied])


def test_sync_rejects_duplicate_supplied_tracking_issue():
    issue_document = repository_issue(200, definition(model_id="model-a"))
    client = IssueReadClient([issue_document])
    with pytest.raises(SyncError, match="duplicate tracking issue #200"):
        bind_repository_issues(client, "o/r", [issue_document, issue_document])


def test_main_refuses_foreign_issue_snapshot_before_any_mutation(tmp_path, monkeypatch):
    issues_path = tmp_path / "issues.json"
    foreign = {
        **repository_issue(200, definition(model_id="model-a")),
        "html_url": "https://github.com/other/repo/issues/200",
    }
    issues_path.write_text(json.dumps([foreign]))

    class RecordingClient:
        def __init__(self, *args, **kwargs):
            self.calls = []

        def graphql(self, *args, **kwargs):
            self.calls.append(("graphql", args, kwargs))
            return {}

        def rest(self, *args, **kwargs):
            self.calls.append(("rest", args, kwargs))
            return {}

    client = RecordingClient()
    monkeypatch.setattr(sync_module, "GhClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(sync_module, "fetch_project", lambda *args: project())
    monkeypatch.setattr(sync_module, "fetch_items", lambda *args: [])
    monkeypatch.setattr(
        sync_module, "build_catalog",
        lambda _repo: catalog(entry("model-a", "recipe-a", maturity="contributed")),
    )
    monkeypatch.setenv("MODEL_TRACKER_PROJECT_OWNER", "org")
    monkeypatch.setenv("MODEL_TRACKER_PROJECT_NUMBER", "1")
    monkeypatch.setenv("MODEL_TRACKER_WRITE_ENABLED", "true")
    monkeypatch.setenv("MODEL_TRACKER_ALLOW_LOCAL_WRITE", "true")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setattr(sys, "argv", [
        "model_coverage_sync.py",
        "--repo", str(REPO), "--repository", "o/r", "--apply",
        "--issues", str(issues_path),
    ])

    assert main() == 2
    assert client.calls == []


def test_sync_plan_adds_one_item_and_one_marker_comment():
    report = report_with_model()
    plan = build_sync_plan(report, project(), [], {}, comment_owner_login="tracker-bot")
    assert plan["summary"]["item_adds"] == 1
    assert plan["summary"]["comments"] == 1
    add = next(action for action in plan["actions"] if action["action"] == "add_item")
    assert add["content_id"] == "I_200"
    fields = {field["field"]: field["value"] for field in add["fields"]}
    assert fields[STATUS_FIELD] == "Contributed"
    assert fields[COMPLETION_FIELD] == "Contributed"
    assert fields[COMPLETED_FIELD] == "Completed"
    assert fields[BLOCKED_FIELD] == "No"
    assert fields[LAST_EVALUATED_FIELD] == "unknown"
    assert not any("Human Notes" in json.dumps(action) for action in plan["actions"])


def test_sync_plan_is_idempotent_and_preserves_human_fields():
    report = report_with_model()
    project_document = project()
    model = report["models"][0]
    existing = item_for(model, project_document)
    body_plan = build_sync_plan(
        report, project_document, [existing], {}, comment_owner_login="tracker-bot",
    )
    comment = next(action for action in body_plan["actions"] if action["action"] == "create_comment")
    assert comment["body"].startswith(COMMENT_MARKER)
    comments = {200: [{
        "id": 9, "body": comment["body"],
        "user": {"type": "Bot", "login": "tracker-bot"},
    }]}
    plan = build_sync_plan(
        report, project_document, [existing], comments, comment_owner_login="tracker-bot",
    )
    assert plan["actions"] == []
    assert plan["warnings"] == []


def test_sync_plan_warns_but_never_deletes_duplicate_owned_comments():
    report = report_with_model()
    project_document = project()
    model = report["models"][0]
    existing = item_for(model, project_document)
    draft = build_sync_plan(
        report, project_document, [existing], {}, comment_owner_login="tracker-bot",
    )
    body = next(action["body"] for action in draft["actions"] if action["action"] == "create_comment")
    comments = {200: [
        {"id": 1, "body": body, "user": {"type": "Bot", "login": "tracker-bot"}},
        {"id": 2, "body": body, "user": {"type": "Bot", "login": "tracker-bot"}},
        {"id": 3, "body": "human note", "user": {"type": "User", "login": "human"}},
    ]}
    plan = build_sync_plan(
        report, project_document, [existing], comments, comment_owner_login="tracker-bot",
    )
    assert plan["actions"] == []
    assert "left duplicates for manual review" in plan["warnings"][0]


def test_sync_plan_updates_marker_owned_by_authenticated_service_identity():
    report = report_with_model()
    project_document = project()
    model = report["models"][0]
    existing = item_for(model, project_document)
    comments = {200: [
        {"id": 5, "body": f"{COMMENT_MARKER}\nstale service comment",
         "user": {"type": "User", "login": "tracker-service"}},
    ]}
    plan = build_sync_plan(
        report, project_document, [existing], comments,
        comment_owner_login="tracker-service",
    )
    assert [action["action"] for action in plan["actions"]] == ["update_comment"]
    assert plan["actions"][0]["previous_body"].endswith("stale service comment")


def test_sync_plan_leaves_another_bots_marker_comment_untouched():
    report = report_with_model()
    project_document = project()
    model = report["models"][0]
    existing = item_for(model, project_document)
    comments = {200: [
        {"id": 1, "body": f"{COMMENT_MARKER}\nother bot",
         "user": {"type": "Bot", "login": "other-bot"}},
    ]}
    plan = build_sync_plan(
        report, project_document, [existing], comments, comment_owner_login="tracker-bot",
    )
    assert plan["actions"] == []
    assert "not authored by tracker-bot" in plan["warnings"][0]


def test_sync_plan_preserves_human_marker_comment():
    report = report_with_model()
    project_document = project()
    model = report["models"][0]
    existing = item_for(model, project_document)
    comments = {200: [
        {"id": 1, "body": f"{COMMENT_MARKER}\nmanual",
         "user": {"type": "User", "login": "human"}},
    ]}
    plan = build_sync_plan(
        report, project_document, [existing], comments, comment_owner_login="tracker-bot",
    )
    assert plan["actions"] == []
    assert "not authored by tracker-bot" in plan["warnings"][0]


def test_sync_plan_skips_comments_when_owner_identity_is_unknown():
    report = report_with_model()
    plan = build_sync_plan(report, project(), [], {})
    assert plan["summary"]["comments"] == 0
    assert "ownership identity is unknown" in plan["warnings"][0]


def test_resolve_comment_owner_prefers_config_and_falls_back_to_rest():
    class ViewerFailureClient:
        def graphql(self, query, variables):
            raise SyncError("viewer unavailable")

        def rest(self, method, path, payload=None):
            assert (method, path) == ("GET", "user")
            return {"login": "service-user"}

    assert resolve_comment_owner(ViewerFailureClient(), "configured") == "configured"
    assert resolve_comment_owner(ViewerFailureClient()) == "service-user"


def test_invalid_definition_marks_error_and_keeps_other_model_updates():
    repository = catalog(entry("model-a", "recipe-a", maturity="contributed"))
    issues = [
        issue(200, definition(model_id="model-a")),
        issue(201, body=f"{TRACKER_MARKER}\n```yaml\nbad: yaml\n```\n"),
    ]
    report = evaluate(repository, issues)
    assert [error["number"] for error in report["errors"]] == [201]
    sha = report["catalog"]["source_sha"]

    project_document = project()
    valid_item = item_for(report["models"][0], project_document, source_sha=sha)
    for node in valid_item["fieldValues"]["nodes"]:
        if node["field"]["name"] == "Coverage":
            node["text"] = "old coverage"
    invalid_item = item_for(report["models"][0], project_document, source_sha=sha)
    invalid_item["id"] = "ITEM2"
    invalid_item["content"]["url"] = issues[1]["html_url"]
    for node in invalid_item["fieldValues"]["nodes"]:
        if node["field"]["name"] == STATUS_FIELD:
            node["name"] = "Validated"
        elif node["field"]["name"] == "Coverage":
            node["text"] = "complete; old validated coverage"
        elif node["field"]["name"] == LAST_EVALUATED_FIELD:
            node["text"] = "old-sha"

    comments = {201: [{
        "id": 88,
        "body": f"{COMMENT_MARKER}\n## Model coverage: `model-a`\nold validated",
        "user": {"type": "Bot", "login": "tracker-bot"},
    }]}
    plan = build_sync_plan(
        report, project_document, [valid_item, invalid_item], comments,
        comment_owner_login="tracker-bot", source_sha=sha, repository="o/r",
    )
    by_field = {
        action["field"]: action["value"]
        for action in plan["actions"]
        if action["action"] == "update_field" and action["item_id"] == "ITEM2"
    }
    assert by_field[STATUS_FIELD] == "Error"
    assert by_field[COMPLETED_FIELD] == "Unknown"
    assert by_field[BLOCKED_FIELD] == "Unknown"
    assert by_field["Coverage"].startswith("unknown")
    assert by_field[LAST_EVALUATED_FIELD] == sha
    # The valid model's stale coverage is still corrected in the same plan.
    assert any(
        action["action"] == "update_field"
        and action["item_id"] == "ITEM1"
        and action["field"] == "Coverage"
        for action in plan["actions"]
    )
    error_comment = next(
        action for action in plan["actions"]
        if action["action"] == "update_comment" and action["issue"] == 201
    )
    assert "definition error" in error_comment["body"]
    assert sha in error_comment["body"]
    assert "old validated" not in error_comment["body"]


def test_malformed_issue_without_project_item_is_added_as_error():
    report = evaluate(
        catalog(entry("model-a", "recipe-a", maturity="contributed")),
        [issue(210, body=f"{TRACKER_MARKER}\n```yaml\nbad: yaml\n```\n")],
    )
    assert [error["number"] for error in report["errors"]] == [210]
    plan = build_sync_plan(
        report, project(), [], {}, comment_owner_login="tracker-bot", source_sha="sha",
    )
    adds = [action for action in plan["actions"] if action["action"] == "add_item"]
    assert len(adds) == 1
    assert adds[0]["issue"] == 210
    assert adds[0]["content_id"] == "I_210"
    values = {field["field"]: field["value"] for field in adds[0]["fields"]}
    assert values[STATUS_FIELD] == "Error"
    assert values[COMPLETED_FIELD] == "Unknown"
    assert values[BLOCKED_FIELD] == "Unknown"
    assert values["Coverage"].startswith("unknown")
    assert values[LAST_EVALUATED_FIELD] == "sha"
    assert any(
        action["action"] == "create_comment" and action["issue"] == 210
        for action in plan["actions"]
    )


def test_apply_plan_adds_missing_error_item_with_error_fields():
    report = evaluate(
        catalog(entry("model-a", "recipe-a", maturity="contributed")),
        [issue(212, body=f"{TRACKER_MARKER}\n```yaml\nbad: yaml\n```\n")],
    )
    plan = build_sync_plan(
        report, project(), [], {}, include_comments=False, source_sha="sha",
    )

    class FakeClient:
        def __init__(self):
            self.calls = []

        def graphql(self, query, variables):
            self.calls.append((query, variables))
            if "addProjectV2ItemById" in query:
                return {"addProjectV2ItemById": {"item": {"id": "NEW_ITEM"}}}
            return {}

    client = FakeClient()
    result = apply_plan(client, plan, "o/r")
    assert result["added_items"] == 1
    add_call = next(
        variables for query, variables in client.calls
        if "addProjectV2ItemById" in query
    )
    assert add_call["content"] == "I_212"
    error_status = next(
        variables for query, variables in client.calls
        if "updateProjectV2ItemFieldValue" in query
        and variables["option"] == project()["fields"][STATUS_FIELD]["options"]["Error"]
    )
    assert error_status["item"] == "NEW_ITEM"


def test_malformed_issue_without_node_id_cannot_be_added():
    report = evaluate(
        catalog(),
        [issue(211, body=f"{TRACKER_MARKER}\n```yaml\nbad: yaml\n```\n", node_id=None)],
    )
    # issue() substitutes a default node_id; remove it to model a partial snapshot.
    report["errors"][0]["node_id"] = None
    with pytest.raises(SyncError, match="missing node_id"):
        build_sync_plan(report, project(), [], {}, comment_owner_login="tracker-bot")


def test_stale_plan_marks_only_tracker_owned_items():
    report = report_with_model()
    project_document = project()
    owned = item_for(report["models"][0], project_document, source_sha="old")
    foreign = item_for(report["models"][0], project_document, source_sha="old")
    foreign["id"] = "FOREIGN"
    foreign["content"]["repository"]["nameWithOwner"] = "other/repo"
    unowned = item_for(report["models"][0], project_document, source_sha="old")
    unowned["id"] = "UNOWNED"
    unowned["fieldValues"]["nodes"] = [
        node for node in unowned["fieldValues"]["nodes"]
        if node["field"]["name"] != STATUS_FIELD
    ]
    mismatched = item_for(report["models"][0], project_document, source_sha="old")
    mismatched["id"] = "MISMATCHED"
    for node in mismatched["fieldValues"]["nodes"]:
        if node["field"]["name"] == "Tracker Issue":
            node["text"] = "https://github.com/o/r/issues/998"
    human = {
        "id": "HUMAN",
        "content": {
            "__typename": "Issue",
            "url": "https://github.test/o/r/issues/999",
            "repository": {"nameWithOwner": "o/r"},
        },
        "fieldValues": {"pageInfo": {"hasNextPage": False}, "nodes": [{
            "__typename": "ProjectV2ItemFieldTextValue",
            "field": {"name": "Human Notes"},
            "text": "do not touch",
        }]},
    }
    plan = build_stale_plan(
        project_document, [owned, foreign, unowned, mismatched, human],
        "deadbeef", "catalog broke", "o/r",
    )
    assert plan["touched_items"] == 1
    assert all(action["item_id"] == "ITEM1" for action in plan["actions"])
    values = {
        action["field"]: action["value"]
        for action in plan["actions"] if action["action"] == "update_field"
    }
    assert values[STATUS_FIELD] == "Stale"
    assert values[COMPLETED_FIELD] == "Unknown"
    assert "catalog broke" in values["Coverage"]
    assert values[LAST_EVALUATED_FIELD] == "deadbeef"


def test_sync_plan_marks_previously_owned_untracked_items_stale_without_deleting():
    report = report_with_model()
    project_document = project()
    tracked = item_for(report["models"][0], project_document)
    deleted = item_for(
        report["models"][0], project_document,
        url="https://github.com/o/r/issues/996",
    )
    deleted["id"] = "DELETED"
    deleted["content"] = None
    for node in deleted["fieldValues"]["nodes"]:
        if node["field"]["name"] == "Tracker Issue":
            node["text"] = "https://github.com/o/r/issues/996"
        if node["field"]["name"] == STATUS_FIELD:
            node["name"] = "Validated"
        if node["field"]["name"] == "Coverage":
            node["text"] = "validated (old)"
    foreign = item_for(
        report["models"][0], project_document,
        repository="other/repo", url="https://github.com/other/repo/issues/1",
    )
    foreign["id"] = "FOREIGN"
    for node in foreign["fieldValues"]["nodes"]:
        if node["field"]["name"] == "Tracker Issue":
            node["text"] = "https://github.com/other/repo/issues/1"
        if node["field"]["name"] == STATUS_FIELD:
            node["name"] = "Validated"
    unowned = item_for(
        report["models"][0], project_document,
        url="https://github.com/o/r/issues/995",
    )
    unowned["id"] = "UNOWNED"
    for node in unowned["fieldValues"]["nodes"]:
        if node["field"]["name"] == "Tracker Issue":
            node["text"] = "https://github.com/o/r/issues/995"
    unowned["fieldValues"]["nodes"] = [
        node for node in unowned["fieldValues"]["nodes"]
        if node["field"]["name"] != STATUS_FIELD
    ]
    transferred = item_for(
        report["models"][0], project_document,
        repository="other/repo", url="https://github.com/other/repo/issues/994",
    )
    transferred["id"] = "TRANSFERRED"
    for node in transferred["fieldValues"]["nodes"]:
        if node["field"]["name"] == "Tracker Issue":
            node["text"] = "https://github.com/o/r/issues/994"
        if node["field"]["name"] == STATUS_FIELD:
            node["name"] = "Validated"
    plan = build_sync_plan(
        report, project_document, [tracked, deleted, foreign, unowned, transferred], {},
        include_comments=False, repository="o/r",
    )
    stale = [action for action in plan["actions"] if action["item_id"] == "DELETED"]
    assert {action["field"]: action["value"] for action in stale}[STATUS_FIELD] == "Stale"
    assert {action["field"]: action["value"] for action in stale}[COMPLETED_FIELD] == "Unknown"
    moved = [action for action in plan["actions"] if action["item_id"] == "TRANSFERRED"]
    assert {action["field"]: action["value"] for action in moved}[STATUS_FIELD] == "Stale"
    assert not [action for action in plan["actions"] if action["item_id"] == "FOREIGN"]
    assert not [action for action in plan["actions"] if action["item_id"] == "UNOWNED"]
    assert not [action for action in plan["actions"] if action["action"] == "add_item"]
    assert any("996" in warning and "no longer tracked" in warning for warning in plan["warnings"])
    assert any("994" in warning and "no longer tracked" in warning for warning in plan["warnings"])


def test_main_catalog_failure_applies_explicit_stale_plan_and_returns_nonzero(
    tmp_path, monkeypatch,
):
    report = report_with_model()
    project_document = project()
    owned = item_for(report["models"][0], project_document, source_sha="old")
    plan_path = tmp_path / "plan.json"
    report_path = tmp_path / "report.json"
    result_path = tmp_path / "result.json"

    def broken_catalog(_repo):
        raise ValueError("bad catalog")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def graphql(self, *args, **kwargs):
            return {}

        def rest(self, *args, **kwargs):
            return {}

    monkeypatch.setattr(sync_module, "build_catalog", broken_catalog)
    monkeypatch.setattr(sync_module, "fetch_project", lambda *args: project_document)
    monkeypatch.setattr(sync_module, "fetch_items", lambda *args: [owned])
    monkeypatch.setattr(sync_module, "GhClient", FakeClient)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("MODEL_TRACKER_PROJECT_OWNER", "org")
    monkeypatch.setenv("MODEL_TRACKER_PROJECT_NUMBER", "1")
    monkeypatch.setenv("GITHUB_SHA", "deadbeef")
    monkeypatch.setenv("MODEL_TRACKER_WRITE_ENABLED", "true")
    monkeypatch.setenv("MODEL_TRACKER_ALLOW_LOCAL_WRITE", "true")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setattr(sys, "argv", [
        "model_coverage_sync.py",
        "--repo", str(REPO),
        "--apply",
        "--plan-out", str(plan_path),
        "--report-out", str(report_path),
        "--result-out", str(result_path),
    ])

    assert main() == 2
    plan = json.loads(plan_path.read_text())
    assert plan["mode"] == "stale-reconciliation"
    assert plan["touched_items"] == 1
    assert {action["field"]: action["value"] for action in plan["actions"]}[STATUS_FIELD] == "Stale"
    result = json.loads(result_path.read_text())
    assert result["applied_actions"] == len(plan["actions"])
    assert all(item["action"] == "update_field" for item in result["completed_actions"])
    assert "catalog evaluation failed: bad catalog" in result["error"]
    assert json.loads(report_path.read_text()) is None


def test_missing_project_fields_fail_with_setup_guidance():
    report = report_with_model()
    incomplete = project()
    del incomplete["fields"]["Coverage"]
    with pytest.raises(SyncError, match="missing text field 'Coverage'"):
        build_sync_plan(report, incomplete, [], {})


def test_apply_plan_updates_fields_and_comments_with_injected_client():
    report = report_with_model()
    project_document = project()
    model = report["models"][0]
    existing = item_for(model, project_document)
    # Change Coverage to force exactly one update, and leave the comment absent.
    for node in existing["fieldValues"]["nodes"]:
        if node["field"]["name"] == "Coverage":
            node["text"] = "stale"
    plan = build_sync_plan(
        report, project_document, [existing], {}, comment_owner_login="tracker-bot",
    )
    assert [action["action"] for action in plan["actions"]] == [
        "update_field", "create_comment",
    ]

    class FakeClient:
        def __init__(self):
            self.calls = []

        def graphql(self, query, variables):
            self.calls.append(("graphql", query, variables))
            return {}

        def rest(self, method, path, payload=None):
            self.calls.append(("rest", method, path, payload))
            return {"id": 303}

    fake = FakeClient()
    result = apply_plan(fake, plan, "o/r")
    assert result["applied_actions"] == 2
    assert len(result["completed_actions"]) == 2
    created = next(
        action for action in result["completed_actions"]
        if action["action"] == "create_comment"
    )
    assert created["comment_id"] == 303
    assert any(call[0] == "graphql" for call in fake.calls)
    assert any(call[0] == "rest" and call[1] == "POST" for call in fake.calls)


def test_apply_plan_audit_reports_completed_actions_on_failure():
    report = report_with_model()
    project_document = project()
    model = report["models"][0]
    existing = item_for(model, project_document)
    for node in existing["fieldValues"]["nodes"]:
        if node["field"]["name"] == "Coverage":
            node["text"] = "stale"
    plan = build_sync_plan(
        report, project_document, [existing], {}, comment_owner_login="tracker-bot",
    )

    class FailingClient:
        def __init__(self):
            self.calls = 0

        def graphql(self, query, variables):
            self.calls += 1
            return {}

        def rest(self, method, path, payload=None):
            raise SyncError("comment API unavailable")

    with pytest.raises(SyncApplyError) as raised:
        apply_plan(FailingClient(), plan, "o/r")
    assert len(raised.value.completed_actions) == 1
    assert raised.value.failed_action["action"] == "create_comment"


def test_api_failures_include_gh_stderr():
    class Result:
        returncode = 1
        stdout = ""
        stderr = "HTTP 403: Resource not accessible by integration"

    client = GhClient(runner=lambda *args, **kwargs: Result())
    with pytest.raises(SyncError, match="403: Resource not accessible"):
        client.graphql("query { viewer { login } }", {})


def test_graphql_transport_payload_wraps_variables():
    seen = {}

    def runner(arguments, **kwargs):
        seen["arguments"] = arguments
        seen["payload"] = json.loads(kwargs["input"])

        class Result:
            returncode = 0
            stdout = json.dumps({"data": {"viewer": {"login": "tracker-bot"}}})
            stderr = ""

        return Result()

    query = "query($owner: String!) { repositoryOwner(login: $owner) { id } }"
    data = GhClient(runner=runner).graphql(query, {"owner": "org"})
    assert data == {"viewer": {"login": "tracker-bot"}}
    assert seen["arguments"] == ["gh", "api", "graphql", "--input", "-"]
    assert seen["payload"] == {"query": query, "variables": {"owner": "org"}}
    assert "owner" not in seen["payload"]


def test_project_items_pagination_uses_every_cursor():
    payloads = []

    def runner(arguments, **kwargs):
        payloads.append(json.loads(kwargs["input"]))
        if payloads[-1]["variables"]["cursor"] is None:
            body = {"data": {"node": {"items": {
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                "nodes": [{"id": "ONE"}],
            }}}}
        else:
            body = {"data": {"node": {"items": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": "TWO"}],
            }}}}

        class Result:
            returncode = 0
            stdout = json.dumps(body)
            stderr = ""

        return Result()

    assert fetch_items(GhClient(runner=runner), "PROJECT") == [{"id": "ONE"}, {"id": "TWO"}]
    assert [payload["variables"]["cursor"] for payload in payloads] == [None, "cursor-1"]


def test_write_guard_requires_opt_in_and_trusted_branch():
    with pytest.raises(SyncError, match="MODEL_TRACKER_WRITE_ENABLED"):
        assert_trusted_write_context({})
    with pytest.raises(SyncError, match="forbidden for pull_request"):
        assert_trusted_write_context({
            "MODEL_TRACKER_WRITE_ENABLED": "true",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_REF": "refs/pull/1/merge",
            "GITHUB_REPOSITORY": "o/r",
        })
    with pytest.raises(SyncError, match="trusted default branch"):
        assert_trusted_write_context({
            "MODEL_TRACKER_WRITE_ENABLED": "true",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": "refs/heads/feature",
            "GITHUB_REPOSITORY": "o/r",
        })
    assert_trusted_write_context({
        "MODEL_TRACKER_WRITE_ENABLED": "true",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "o/r",
    })
    assert_trusted_write_context({
        "MODEL_TRACKER_WRITE_ENABLED": "true",
        "GITHUB_EVENT_NAME": "issues",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "o/r",
    })
    assert_trusted_write_context({
        "MODEL_TRACKER_WRITE_ENABLED": "true",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/trunk",
        "GITHUB_REPOSITORY": "o/r",
        "MODEL_TRACKER_DEFAULT_BRANCH": "trunk",
    })
    with pytest.raises(SyncError, match="trusted default branch"):
        assert_trusted_write_context({
            "MODEL_TRACKER_WRITE_ENABLED": "true",
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "o/r",
            "MODEL_TRACKER_DEFAULT_BRANCH": "trunk",
        })
    with pytest.raises(SyncError, match="MODEL_TRACKER_ALLOW_LOCAL_WRITE"):
        assert_trusted_write_context({"MODEL_TRACKER_WRITE_ENABLED": "true"})


def test_issue_form_default_definition_parses():
    template = yaml.safe_load(
        (REPO / ".github" / "ISSUE_TEMPLATE" / "model-coverage.yml").read_text()
    )
    markdown = "\n".join(
        element["attributes"].get("value", "")
        for element in template["body"] if element["type"] == "markdown"
    )
    # GitHub issue forms do not submit markdown block content, so the marker
    # must live in the editable textarea to reach the issue body.
    assert TRACKER_MARKER not in markdown
    textarea = next(
        element for element in template["body"]
        if element["type"] == "textarea" and element["id"] == "target-definition"
    )
    assert textarea["attributes"]["value"].startswith(TRACKER_MARKER)
    submitted = {
        "number": 400, "body": textarea["attributes"]["value"], "labels": [],
    }
    assert is_tracking_issue(submitted) is True
    with pytest.raises(DefinitionError, match="placeholders"):
        parse_definition(textarea["attributes"]["value"])
    filled = textarea["attributes"]["value"].replace(
        "replace-with-model-id", "model-a"
    ).replace("@your-github-handle", "@owner").replace(
        "replace-with-recipe-id", "recipe-a"
    )
    parsed = parse_definition(filled)
    assert parsed["model_id"] == "model-a"
    assert parsed["completion"] == "contributed"
    target_platform = parsed["targets"][0]["platforms"][0]
    assert target_platform["stack"] == "vllm"
    assert target_platform["version"] == "v0.24.0"
    assert target_platform["completion"] is None
    assert is_tracking_issue({**submitted, "body": filled}) is True


def test_workflow_serializes_writers_and_uploads_audits():
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "model-coverage.yml").read_text()
    )
    triggers = workflow.get("on") or workflow.get(True)
    assert {"push", "pull_request", "issues", "workflow_dispatch", "schedule"} <= set(triggers)
    assert triggers["push"]["branches"] == ["main"]
    assert workflow["concurrency"] == {
        "group": "model-coverage", "cancel-in-progress": False,
    }
    check = workflow["jobs"]["check"]
    assert check["permissions"] == {"contents": "read", "issues": "read"}
    assert "MODEL_TRACKER_PROJECT_TOKEN" not in json.dumps(check)
    check_runs = "\n".join(step.get("run", "") for step in check["steps"])
    assert "--allow-errors" in check_runs
    check_uploads = [
        step for step in check["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert check_uploads and all(step["if"] == "always()" for step in check_uploads)

    sync = workflow["jobs"]["sync"]
    assert sync["needs"] == "check"
    assert sync["permissions"] == {"contents": "read"}
    assert "vars.MODEL_TRACKER_WRITE_ENABLED == 'true'" in sync["if"]
    assert "github.event_name == 'issues'" in sync["if"]
    assert "refs/heads/main" not in sync["if"]
    assert "format('refs/heads/{0}', github.event.repository.default_branch)" in sync["if"]
    sync_text = json.dumps(sync)
    assert "--result-out" in sync_text
    assert "MODEL_TRACKER_BOT_LOGIN" in sync_text
    sync_apply = next(
        step for step in sync["steps"] if "--apply" in step.get("run", "")
    )
    assert sync_apply["env"]["MODEL_TRACKER_DEFAULT_BRANCH"] == (
        "${{ github.event.repository.default_branch }}"
    )
    sync_uploads = [
        step for step in sync["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert sync_uploads and all(step["if"] == "always()" for step in sync_uploads)

    stale = workflow["jobs"]["stale"]
    assert stale["needs"] == "check"
    assert "always()" in stale["if"]
    assert "needs.check.outputs.reconciliation_failure == 'true'" in stale["if"]
    assert "refs/heads/main" not in stale["if"]
    assert "format('refs/heads/{0}', github.event.repository.default_branch)" in stale["if"]
    stale_apply = next(
        step for step in stale["steps"] if "--apply" in step.get("run", "")
    )
    assert stale_apply["env"]["MODEL_TRACKER_DEFAULT_BRANCH"] == (
        "${{ github.event.repository.default_branch }}"
    )
    assert "--mark-stale" in json.dumps(stale)
    assert "--apply" in json.dumps(stale)
    assert any(
        step.get("id") == "evaluate" for step in check["steps"]
    )
    assert any(
        step.get("id") == "validate" for step in check["steps"]
    )
    record = next(
        step for step in check["steps"] if step.get("id") == "record_failure"
    )
    assert "steps.validate.outcome == 'failure'" in record["if"]
    assert "steps.evaluate.outcome == 'failure'" in record["if"]
    assert (
        "steps.record_failure.outputs.reconciliation_failure"
        in check["outputs"]["reconciliation_failure"]
    )

    dry_run_steps = [
        step for step in check["steps"] if "--apply" in step.get("run", "")
    ]
    assert dry_run_steps == []


def test_workflow_mints_environment_scoped_tracker_app_token():
    path = REPO / ".github" / "workflows" / "model-coverage.yml"
    text = path.read_text()
    workflow = yaml.safe_load(text)

    # The static project secret is gone; App credentials are environment-scoped.
    assert "MODEL_TRACKER_PROJECT_TOKEN" not in text
    assert "FORGE_INGEST_APP_CLIENT_ID" in text
    assert "FORGE_INGEST_APP_PRIVATE_KEY" in text
    assert "FORGE_INGEST_APP_PRIVATE_KEY" not in json.dumps(
        workflow["jobs"]["check"]
    )

    for job_name in ("sync", "stale"):
        job = workflow["jobs"][job_name]
        assert job["environment"] == "forge-evidence"
        assert job["permissions"] == {"contents": "read"}
        assert "vars.MODEL_TRACKER_WRITE_ENABLED == 'true'" in job["if"]

        mint = next(
            step for step in job["steps"] if step.get("id") == "app-token"
        )
        assert mint["uses"] == (
            "actions/create-github-app-token@"
            "64ab729a6294000f5aa4a4bc0b73d1f32bc45510"
        )
        assert mint["with"] == {
            "client-id": "${{ vars.FORGE_INGEST_APP_CLIENT_ID }}",
            "private-key": "${{ secrets.FORGE_INGEST_APP_PRIVATE_KEY }}",
            "owner": "${{ github.repository_owner }}",
            "repositories": "${{ github.event.repository.name }}",
            "permission-organization-projects": "write",
            "permission-issues": "write",
            "permission-contents": "read",
        }

        apply = next(
            step for step in job["steps"] if "--apply" in step.get("run", "")
        )
        assert apply["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
        assert "MODEL_TRACKER_PROJECT_TOKEN" not in json.dumps(apply)
        assert job["steps"].index(mint) < job["steps"].index(apply)

    identity = next(
        step for step in workflow["jobs"]["sync"]["steps"]
        if step.get("name") == "Resolve tracker comment identity"
    )
    assert identity["env"]["APP_SLUG"] == "${{ steps.app-token.outputs.app-slug }}"
    assert identity["env"]["CONFIGURED_LOGIN"] == "${{ vars.MODEL_TRACKER_BOT_LOGIN }}"
    assert '"MODEL_TRACKER_BOT_LOGIN=${APP_SLUG}[bot]"' in identity["run"]
    assert '"MODEL_TRACKER_BOT_LOGIN=$CONFIGURED_LOGIN"' in identity["run"]


def test_cli_evaluates_issues_file(tmp_path):
    issues = tmp_path / "issues.json"
    issues.write_text("[]")
    result = subprocess.run(
        [sys.executable, str(REPO / "tools" / "model_coverage.py"),
         "--repo", str(REPO), "--issues", str(issues), "--format", "json"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["tracking_issue_count"] == 0
    assert report["untracked_models"] == sorted(
        model["id"] for model in build_catalog(REPO)["models"]
    )


def test_cli_allow_errors_still_writes_reports(tmp_path):
    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([
        issue(301, body=f"{TRACKER_MARKER}\n```yaml\nbad: yaml\n```\n"),
    ]))
    out = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(REPO / "tools" / "model_coverage.py"),
         "--repo", str(REPO), "--issues", str(issues),
         "--allow-errors", "--out", str(out)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(out.read_text())
    assert report["errors"][0]["number"] == 301


def test_cli_returns_actionable_failure_for_malformed_issue(tmp_path):
    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([issue(300, body=f"{TRACKER_MARKER}\n```yaml\nbad: yaml\n```\n")]))
    result = subprocess.run(
        [sys.executable, str(REPO / "tools" / "model_coverage.py"),
         "--repo", str(REPO), "--issues", str(issues)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert result.returncode == 1
    assert "Issue #300" in result.stderr
    assert "unknown key" in result.stderr
