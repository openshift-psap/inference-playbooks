#!/usr/bin/env python3
"""Sync evaluated model coverage into a GitHub Projects v2 dashboard.

Dry run is the default. Live writes require --apply plus an explicit
MODEL_TRACKER_WRITE_ENABLED=true opt-in and, in Actions, the trusted default
branch. The tool uses the gh CLI and the Python standard library only.

Owned project fields: Model ID, Owner, Completion Criterion, Completed, Tracker
Status, Blocked, Coverage, Jira, Blockers, Tracker Issue, Last Evaluated. Every
other field is left untouched. The tool maintains one marker comment per
tracking issue, authored by the configured sync identity; it never edits or
deletes another identity's comment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from model_coverage import (
    build_catalog, coverage_text, evaluate, is_tracking_issue, load_issues,
)

COMMENT_MARKER = "<!-- inference-playbooks:model-coverage -->"
REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
STATUS_FIELD = "Tracker Status"
COMPLETION_FIELD = "Completion Criterion"
COMPLETED_FIELD = "Completed"
BLOCKED_FIELD = "Blocked"
LAST_EVALUATED_FIELD = "Last Evaluated"
TEXT_FIELDS = (
    "Model ID", "Owner", "Coverage", "Jira", "Blockers", "Tracker Issue",
    LAST_EVALUATED_FIELD,
)
SINGLE_SELECT_FIELDS = {
    STATUS_FIELD: (
        "Planned", "In progress", "Contributed", "Validated", "Blocked", "Error", "Stale",
    ),
    COMPLETION_FIELD: ("Contributed", "Validated"),
    COMPLETED_FIELD: ("Completed", "Incomplete", "Unknown"),
    BLOCKED_FIELD: ("Yes", "No", "Unknown"),
}
STATUS_LABELS = {
    "planned": "Planned",
    "in-progress": "In progress",
    "contributed": "Contributed",
    "validated": "Validated",
    "blocked": "Blocked",
    "error": "Error",
    "stale": "Stale",
}
OWNED_STATUS_VALUES = frozenset(STATUS_LABELS.values())

PROJECT_QUERY = """
query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner { projectV2(number: $number) { ...TrackerProject } }
  }
}
fragment TrackerProject on ProjectV2 {
  id
  title
  fields(first: 100) {
    pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on ProjectV2FieldCommon { id name dataType }
      ... on ProjectV2SingleSelectField { options { id name } }
    }
  }
}
"""

FIELDS_QUERY = """
query($project: ID!, $cursor: String!) {
  node(id: $project) {
    ... on ProjectV2 {
      fields(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          __typename
          ... on ProjectV2FieldCommon { id name dataType }
          ... on ProjectV2SingleSelectField { options { id name } }
        }
      }
    }
  }
}
"""

ITEMS_QUERY = """
query($project: ID!, $cursor: String) {
  node(id: $project) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          content {
            __typename
            ... on Issue { id number url repository { nameWithOwner } }
          }
          fieldValues(first: 100) {
            pageInfo { hasNextPage }
            nodes {
              __typename
              ... on ProjectV2ItemFieldTextValue {
                text
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                optionId
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""

ADD_ITEM = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
  }
}
"""

UPDATE_TEXT = """
mutation($project: ID!, $item: ID!, $field: ID!, $text: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field, value: {text: $text}
  }) { projectV2Item { id } }
}
"""

UPDATE_SINGLE = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: {singleSelectOptionId: $option}
  }) { projectV2Item { id } }
}
"""

CLEAR_FIELD = """
mutation($project: ID!, $item: ID!, $field: ID!) {
  clearProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field
  }) { projectV2Item { id } }
}
"""

VIEWER_QUERY = """
query {
  viewer { login }
}
"""


class SyncError(RuntimeError):
    """A GitHub API, project configuration, or write-safety failure."""


class SyncApplyError(SyncError):
    """A write failure with the actions that had already completed."""

    def __init__(self, message: str, failed_action: dict, completed_actions: list[dict]):
        super().__init__(message)
        self.failed_action = failed_action
        self.completed_actions = completed_actions


class GhClient:
    """Small gh CLI wrapper with injectable execution for tests."""

    def __init__(self, runner=subprocess.run, token: str | None = None):
        self.runner = runner
        self.token = token

    def _run(self, arguments: list[str], payload: dict | None = None) -> object:
        environment = os.environ.copy()
        if self.token:
            environment["GH_TOKEN"] = self.token
        result = self.runner(
            ["gh", *arguments],
            input=json.dumps(payload) if payload is not None else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise SyncError(f"gh {' '.join(arguments[:3])} failed: {detail or 'no error detail'}")
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as error:
            raise SyncError(f"gh returned invalid JSON: {error}") from error

    def graphql(self, query: str, variables: dict) -> dict:
        # GitHub's GraphQL endpoint takes a JSON body with top-level "query" and
        # "variables"; spreading variables beside "query" is rejected.
        value = self._run(
            ["api", "graphql", "--input", "-"],
            {"query": query, "variables": variables},
        )
        if not isinstance(value, dict):
            raise SyncError("GitHub GraphQL returned a non-object response")
        if value.get("errors"):
            messages = "; ".join(
                str(error.get("message", error)) for error in value["errors"]
            )
            raise SyncError(f"GitHub GraphQL error: {messages}")
        data = value.get("data")
        if not isinstance(data, dict):
            raise SyncError("GitHub GraphQL response is missing data")
        return data

    def rest(self, method: str, path: str, payload: dict | None = None) -> object:
        arguments = ["api", "-X", method, path]
        if payload is not None:
            arguments.extend(["--input", "-"])
        return self._run(arguments, payload)

    def rest_paginated(self, path: str) -> list[dict]:
        value = self._run(["api", "--paginate", "--slurp", path])
        if not isinstance(value, list):
            raise SyncError(f"GitHub REST pagination returned a non-list for {path}")
        if value and all(isinstance(page, list) for page in value):
            flattened = [item for page in value for item in page]
        else:
            flattened = value
        if any(not isinstance(item, dict) for item in flattened):
            raise SyncError(f"GitHub REST pagination returned non-object items for {path}")
        return flattened


def fetch_issues(client: GhClient, repository: str) -> list[dict]:
    """Read every issue, excluding pull requests, with REST pagination."""
    issues = client.rest_paginated(f"repos/{repository}/issues?state=all&per_page=100")
    return [issue for issue in issues if "pull_request" not in issue]


def require_repository(repository: str) -> str:
    """Reject malformed repository names before they reach API paths."""
    if not isinstance(repository, str) or not REPOSITORY_NAME.fullmatch(repository):
        raise SyncError(f"invalid repository {repository!r}; expected OWNER/REPO")
    return repository


def _issue_urls(repository: str, number: int) -> tuple[str, str]:
    return (
        f"https://github.com/{repository}/issues/{number}",
        f"https://api.github.com/repos/{repository}/issues/{number}",
    )


def validate_issue_identity(issue: dict, repository: str) -> int:
    """Require an --issues entry to identify one issue in ``repository``.

    A label or marker comment alone must not authorize a write: the issue
    number and any supplied browser/API URL have to agree with the selected
    repository before any project item or comment is touched.
    """
    number = issue.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise SyncError(f"--issues entry has an invalid issue number {number!r}")
    expected_html, expected_api = _issue_urls(repository, number)
    supplied_html = issue.get("html_url")
    supplied_url = issue.get("url")
    if supplied_html is not None and supplied_html != expected_html:
        raise SyncError(
            f"issue #{number} html_url {supplied_html!r} does not belong to "
            f"{repository}; expected {expected_html}"
        )
    if supplied_url is not None and supplied_url not in {expected_html, expected_api}:
        raise SyncError(
            f"issue #{number} url {supplied_url!r} does not belong to "
            f"{repository}; expected {expected_html} or {expected_api}"
        )
    if supplied_html is None and supplied_url is None:
        raise SyncError(
            f"issue #{number} has no html_url or url; cannot bind it to {repository}"
        )
    node_id = issue.get("node_id")
    if node_id is not None and (not isinstance(node_id, str) or not node_id):
        raise SyncError(f"issue #{number} has an invalid node_id {node_id!r}")
    return number


def fetch_issue(client: GhClient, repository: str, number: int) -> dict:
    data = client.rest("GET", f"repos/{repository}/issues/{number}")
    if not isinstance(data, dict):
        raise SyncError(f"GitHub returned a non-object issue for #{number}")
    if "pull_request" in data:
        raise SyncError(f"issue #{number} is a pull request, not a tracking issue")
    return data


def bind_repository_issues(
    client: GhClient, repository: str, issues: list[dict],
) -> list[dict]:
    """Bind --issues tracking entries to authoritative repository issues.

    The evaluator may consume a supplied snapshot, but a write plan must use
    the real issue number, URL, body, and node_id from GitHub. Verification
    happens before planning so a foreign or stale entry cannot reach a
    mutation.
    """
    repository = require_repository(repository)
    bound = []
    seen = set()
    for issue in issues:
        if not is_tracking_issue(issue):
            bound.append(issue)
            continue
        number = validate_issue_identity(issue, repository)
        if number in seen:
            raise SyncError(f"--issues contains duplicate tracking issue #{number}")
        seen.add(number)
        authoritative = fetch_issue(client, repository, number)
        if authoritative.get("number") != number:
            raise SyncError(
                f"--issues issue #{number} resolved to a different issue number "
                f"{authoritative.get('number')!r} in {repository}"
            )
        expected_html, expected_api = _issue_urls(repository, number)
        if authoritative.get("html_url") != expected_html:
            raise SyncError(
                f"authoritative issue #{number} html_url "
                f"{authoritative.get('html_url')!r} does not belong to {repository}"
            )
        if authoritative.get("url") != expected_api:
            raise SyncError(
                f"authoritative issue #{number} API url "
                f"{authoritative.get('url')!r} does not belong to {repository}"
            )
        node_id = authoritative.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise SyncError(f"authoritative issue #{number} is missing node_id")
        supplied_node_id = issue.get("node_id")
        if supplied_node_id is not None and supplied_node_id != node_id:
            raise SyncError(
                f"issue #{number} node_id {supplied_node_id!r} is not bound to the "
                f"repository issue node_id {node_id!r}"
            )
        bound.append(authoritative)
    return bound


def _field_nodes(nodes: list[dict]) -> dict[str, dict]:
    fields = {}
    for node in nodes:
        name = node.get("name")
        if not isinstance(name, str):
            continue
        fields[name] = {
            "id": node.get("id"),
            "name": name,
            "dataType": node.get("dataType"),
            "options": {
                option.get("name"): option.get("id")
                for option in node.get("options") or []
                if isinstance(option, dict)
            },
        }
    return fields


def fetch_project(client: GhClient, owner: str, number: int) -> dict:
    data = client.graphql(PROJECT_QUERY, {"owner": owner, "number": number})
    project = ((data.get("repositoryOwner") or {}).get("projectV2"))
    if not isinstance(project, dict):
        raise SyncError(
            f"Project v2 {owner}#{number} was not found or is not readable; "
            "verify the owner, number, and token project scope"
        )
    fields = _field_nodes((project.get("fields") or {}).get("nodes") or [])
    page_info = (project.get("fields") or {}).get("pageInfo") or {}
    while page_info.get("hasNextPage"):
        cursor = page_info.get("endCursor")
        if not cursor:
            raise SyncError("project field pagination reported another page without an endCursor")
        page = client.graphql(FIELDS_QUERY, {"project": project["id"], "cursor": cursor})
        connection = (((page.get("node") or {}).get("fields")) or {})
        fields.update(_field_nodes(connection.get("nodes") or []))
        page_info = connection.get("pageInfo") or {}
    return {"id": project.get("id"), "title": project.get("title"), "fields": fields}


def fetch_items(client: GhClient, project_id: str) -> list[dict]:
    items = []
    cursor = None
    while True:
        data = client.graphql(ITEMS_QUERY, {"project": project_id, "cursor": cursor})
        connection = (((data.get("node") or {}).get("items")) or {})
        for item in connection.get("nodes") or []:
            values = (item.get("fieldValues") or {}).get("pageInfo") or {}
            if values.get("hasNextPage"):
                raise SyncError(
                    "project item has more than 100 field values; reduce project fields "
                    "or extend pagination before syncing"
                )
            items.append(item)
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return items
        cursor = page_info.get("endCursor")
        if not cursor:
            raise SyncError("project item pagination reported another page without an endCursor")


def fetch_comments(client: GhClient, repository: str, issue_numbers: list[int]) -> dict[int, list[dict]]:
    return {
        number: client.rest_paginated(f"repos/{repository}/issues/{number}/comments?per_page=100")
        for number in sorted(set(issue_numbers))
    }


def fetch_viewer_login(client: GhClient) -> str | None:
    data = client.graphql(VIEWER_QUERY, {})
    login = ((data.get("viewer") or {}).get("login"))
    return login if isinstance(login, str) and login else None


def resolve_comment_owner(client: GhClient, configured: str | None = None) -> str | None:
    """Return the exact login that owns tracker comments.

    Installation tokens can make the GraphQL ``viewer`` query unavailable, so a
    configured login (``MODEL_TRACKER_BOT_LOGIN``) wins, then the viewer, then
    the REST ``/user`` identity.
    """
    if configured:
        return configured
    try:
        login = fetch_viewer_login(client)
    except SyncError:
        login = None
    if login:
        return login
    try:
        data = client.rest("GET", "user")
    except SyncError:
        return None
    login = data.get("login") if isinstance(data, dict) else None
    return login if isinstance(login, str) and login else None


def require_owned_fields(project: dict) -> None:
    """Fail with the exact field setup needed, before planning mutations."""
    fields = project.get("fields") or {}
    problems = []
    for name in TEXT_FIELDS:
        field = fields.get(name)
        if not field:
            problems.append(f"missing text field {name!r}")
        elif field.get("dataType") != "TEXT":
            problems.append(f"field {name!r} must be text, got {field.get('dataType')!r}")
    for name, required_options in SINGLE_SELECT_FIELDS.items():
        field = fields.get(name)
        if not field:
            problems.append(f"missing single-select field {name!r}")
            continue
        if field.get("dataType") != "SINGLE_SELECT":
            problems.append(f"field {name!r} must be single select, got {field.get('dataType')!r}")
            continue
        missing = sorted(set(required_options) - set(field.get("options") or {}))
        if missing:
            problems.append(f"field {name!r} is missing options: {', '.join(missing)}")
    if problems:
        raise SyncError(
            "GitHub Project schema is not ready: " + "; ".join(problems) +
            ". Follow docs/model-tracker.md#project-setup; this tool never creates or deletes fields."
        )


def _resolve_desired(values: dict[str, str], project: dict, names: tuple[str, ...]) -> list[dict]:
    desired = []
    for name in names:
        field = project["fields"][name]
        value = values.get(name, "")
        option_id = None
        if name in SINGLE_SELECT_FIELDS:
            option_id = field["options"].get(value)
            if not option_id:
                raise SyncError(f"Project field {name!r} has no option {value!r}")
        desired.append({
            "field": name,
            "field_id": field["id"],
            "data_type": field["dataType"],
            "value": value,
            "option_id": option_id,
        })
    return desired


def desired_fields(model: dict, project: dict, *, source_sha: str = "unknown") -> list[dict]:
    values = {
        "Model ID": model["model_id"],
        "Owner": model["owner"],
        "Completion Criterion": model["completion"].capitalize(),
        COMPLETED_FIELD: "Completed" if model["completed"] else "Incomplete",
        STATUS_FIELD: STATUS_LABELS[model["status"]],
        BLOCKED_FIELD: "Yes" if (model.get("blockers") or model["status"] == "blocked") else "No",
        "Coverage": coverage_text(model),
        "Jira": (model.get("jira") or {}).get("key") or "",
        "Blockers": "\n".join(model.get("blockers") or []),
        "Tracker Issue": model["issue"]["url"],
        LAST_EVALUATED_FIELD: source_sha or "unknown",
    }
    return _resolve_desired(values, project, (*TEXT_FIELDS, *SINGLE_SELECT_FIELDS))


def stale_fields(error: dict, project: dict, source_sha: str) -> list[dict]:
    """Project values for an issue the evaluator could not use.

    Old coverage and completion values must not survive an invalid edit, so they
    are explicitly replaced with unknown/error values instead of left stale.
    """
    values = {
        STATUS_FIELD: "Error",
        COMPLETED_FIELD: "Unknown",
        BLOCKED_FIELD: "Unknown",
        "Coverage": "unknown — issue definition could not be evaluated",
        "Blockers": "Issue definition is invalid; blocker data could not be evaluated.",
        "Tracker Issue": error.get("url") or "",
        LAST_EVALUATED_FIELD: source_sha or "unknown",
    }
    names = [
        STATUS_FIELD, COMPLETED_FIELD, BLOCKED_FIELD,
        "Coverage", "Blockers", "Tracker Issue", LAST_EVALUATED_FIELD,
    ]
    if error.get("model_id"):
        values["Model ID"] = error["model_id"]
        names.append("Model ID")
    if error.get("owner"):
        values["Owner"] = error["owner"]
        names.append("Owner")
    if "jira" in error:
        values["Jira"] = error.get("jira") or ""
        names.append("Jira")
    if "blockers" in error:
        values["Blockers"] = "\n".join(error.get("blockers") or [])
    return _resolve_desired(values, project, tuple(names))


def item_values(item: dict) -> dict[str, dict]:
    values = {}
    for node in (item.get("fieldValues") or {}).get("nodes") or []:
        field_name = (node.get("field") or {}).get("name")
        if not field_name:
            continue
        if node.get("__typename") == "ProjectV2ItemFieldTextValue":
            values[field_name] = {"type": "text", "value": node.get("text") or ""}
        elif node.get("__typename") == "ProjectV2ItemFieldSingleSelectValue":
            values[field_name] = {
                "type": "single",
                "value": node.get("name") or "",
                "option_id": node.get("optionId"),
            }
    return values


def _tracker_status(existing: dict[str, dict]) -> str:
    return ((existing.get(STATUS_FIELD) or {}).get("value") or "")


def _strict_owned_item_url(item: dict, repository: str) -> str | None:
    """Return the content URL only for a tracker-owned item in ``repository``.

    Used by catalog-wide stale reconciliation: require a same-repository Issue
    content, an owned Tracker Status, and a Tracker Issue value equal to that
    content URL. This prevents touching foreign items in a shared org project.
    """
    content = item.get("content") or {}
    url = content.get("url")
    if not url or content.get("__typename") != "Issue":
        return None
    if (content.get("repository") or {}).get("nameWithOwner") != repository:
        return None
    existing = item_values(item)
    if _tracker_status(existing) not in OWNED_STATUS_VALUES:
        return None
    if ((existing.get("Tracker Issue") or {}).get("value") or "") != url:
        return None
    return url


def _previously_owned_item_url(item: dict, repository: str) -> str | None:
    """Return the tracked issue URL for an item this repository previously owned.

    Deleted and transferred issues can lose their same-repository content, so
    the owned Tracker Issue URL is the fallback gate. A same-repository content
    item must still match that URL exactly.
    """
    existing = item_values(item)
    if _tracker_status(existing) not in OWNED_STATUS_VALUES:
        return None
    tracker_issue = ((existing.get("Tracker Issue") or {}).get("value") or "")
    if not tracker_issue.startswith(f"https://github.com/{repository}/issues/"):
        return None
    content = item.get("content") or {}
    same_repository = (
        content.get("__typename") == "Issue"
        and (content.get("repository") or {}).get("nameWithOwner") == repository
    )
    if same_repository and content.get("url") and content.get("url") != tracker_issue:
        return None
    return tracker_issue


def _stale_state_fields(project: dict, source_sha: str, reason: str) -> list[dict]:
    values = {
        STATUS_FIELD: "Stale",
        COMPLETED_FIELD: "Unknown",
        BLOCKED_FIELD: "Unknown",
        "Coverage": f"unknown — reconciliation failed: {_safe_comment_text(reason, 200)}",
        LAST_EVALUATED_FIELD: source_sha or "unknown",
    }
    return _resolve_desired(values, project, tuple(values))


def _safe_comment_text(value: object, limit: int = 500) -> str:
    """Keep issue-authored text inert and bounded in a bot comment."""
    return (
        str(value).replace("@", "&#64;").replace("\r", " ").replace("\n", " ").strip()[:limit]
    )


def _comment_body(model: dict, source_sha: str = "unknown") -> str:
    coverage = model["coverage"]
    lines = [
        COMMENT_MARKER,
        f"## Model coverage: `{model['model_id']}`",
        "",
        f"**Status:** {model['status']} | **Completed:** "
        f"{'yes' if model['completed'] else 'no'} | "
        f"**Completion criterion:** {model['completion']} | "
        f"**Coverage:** {coverage['completed_platforms']}/{coverage['target_platforms']} platforms",
        "",
        f"Contributed-or-better: {coverage['contributed_or_validated_platforms']} | "
        f"Independently validated: {coverage['independently_validated_platforms']} | "
        f"Remaining: {coverage['remaining_platforms']}",
        "",
        f"Evaluated at `{source_sha or 'unknown'}`.",
        "",
        "| Recipe | Platform | Requirement | Repository maturity | Tracker status | Evidence |",
        "|--------|----------|-------------|---------------------|----------------|----------|",
    ]
    rows = []
    for target in model["targets"]:
        for platform in target["platforms"]:
            if platform.get("evidence_run_id"):
                evidence = f"[{platform['evidence_run_id']}]({platform.get('evidence_source')})" \
                    if platform.get("evidence_source") else platform["evidence_run_id"]
                if platform.get("evidence_pr_url"):
                    evidence += f" · [PR]({platform['evidence_pr_url']})"
            else:
                evidence = platform["reason"].replace("|", "/")
            recipe = (
                f"[{target['recipe_id']}]({platform['recipe_source']})"
                if platform.get("recipe_source") else f"`{target['recipe_id']}`"
            )
            rows.append(
                f"| {recipe} | {platform['stack']} {platform['version']} | "
                f"{platform['completion_required']} | "
                f"{platform['maturity'] or '—'} | {platform['status']} | {evidence} |"
            )
    for row in rows[:200]:
        lines.append(row)
    if len(rows) > 200:
        lines.append(f"| _… {len(rows) - 200} more target platforms_ | — | — | — | — | See evaluator JSON |")
    if model.get("jira"):
        lines.extend(["", f"Jira: {model['jira']['key']} — {model['jira']['note']}"])
    if model.get("blockers"):
        lines.extend(["", "Blockers:"])
        lines.extend(f"- {_safe_comment_text(blocker)}" for blocker in model["blockers"])
    lines.extend([
        "",
        "_Maintained by the model-coverage workflow. Edit the issue's fenced YAML definition; "
        "do not edit this comment. Partial targets never mark a model complete._",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _error_comment_body(error: dict, source_sha: str = "unknown") -> str:
    model = f" for `{error['model_id']}`" if error.get("model_id") else ""
    return "\n".join([
        COMMENT_MARKER,
        f"## Model coverage: definition error{model}",
        "",
        f"The tracker could not evaluate issue #{error.get('number')} at "
        f"`{source_sha or 'unknown'}`. The previous status is marked `Error` and "
        "coverage is unknown until the definition is fixed.",
        "",
        f"**Error:** {_safe_comment_text(error.get('error'))}",
        "",
        "Fix the fenced YAML definition in the issue body. Unknown keys, duplicate "
        "model declarations, placeholders, and invalid completion criteria are hard errors.",
        "",
        "_Maintained by the model-coverage workflow. Do not edit this comment._",
    ]) + "\n"


def _field_actions(
    existing: dict[str, dict], desired: list[dict], *,
    item_id: str, issue: int | None, model_id: str | None,
) -> list[dict]:
    actions = []
    for field in desired:
        name = field["field"]
        previous = (existing.get(name) or {}).get("value") or ""
        if previous == field["value"]:
            continue
        common = {
            "model_id": model_id,
            "issue": issue,
            "item_id": item_id,
            "field": name,
            "field_id": field["field_id"],
            "previous": previous,
        }
        if field["data_type"] == "TEXT" and field["value"] == "":
            actions.append({"action": "clear_field", **common})
        else:
            actions.append({
                "action": "update_field", **common,
                "data_type": field["data_type"],
                "value": field["value"],
                "option_id": field["option_id"],
            })
    return actions


def _comment_actions(
    body: str, issue_number: int, comments: list[dict],
    owner_login: str | None, warnings: list[str],
) -> list[dict]:
    """Update only a marker comment authored by the exact sync identity.

    Any ``Bot`` user can write a marker, so authorship is matched on the
    authenticated identity. Duplicate owned comments are reported, never
    deleted, because deletion is not reversible.
    """
    marker = [c for c in comments if (c.get("body") or "").startswith(COMMENT_MARKER)]
    if not owner_login:
        warnings.append(
            f"issue #{issue_number}: comment ownership identity is unknown; set "
            "MODEL_TRACKER_BOT_LOGIN or authenticate with a readable viewer login; "
            "comment left untouched"
        )
        return []
    owned = sorted(
        [c for c in marker if (c.get("user") or {}).get("login") == owner_login],
        key=lambda c: c.get("id") or 0,
    )
    if len(owned) > 1:
        warnings.append(
            f"issue #{issue_number} has {len(owned)} tracker comments owned by "
            f"{owner_login}; updated the first and left duplicates for manual review"
        )
    if owned:
        if (owned[0].get("body") or "") == body:
            return []
        return [{
            "action": "update_comment",
            "issue": issue_number,
            "comment_id": owned[0]["id"],
            "previous_body": owned[0].get("body") or "",
            "body": body,
        }]
    if marker:
        warnings.append(
            f"issue #{issue_number} has a tracker marker comment not authored by "
            f"{owner_login}; left it untouched and did not create a duplicate"
        )
        return []
    return [{"action": "create_comment", "issue": issue_number, "body": body}]


def build_sync_plan(
    report: dict,
    project: dict,
    items: list[dict],
    comments: dict[int, list[dict]] | None = None,
    *,
    include_comments: bool = True,
    comment_owner_login: str | None = None,
    source_sha: str = "unknown",
    repository: str | None = None,
) -> dict:
    """Build an idempotent, deterministic mutation plan without anything live."""
    require_owned_fields(project)
    items_by_url: dict[str, list[dict]] = {}
    for item in items:
        content = item.get("content") or {}
        url = content.get("url")
        if isinstance(url, str) and url:
            items_by_url.setdefault(url, []).append(item)

    actions = []
    warnings = []

    def matching_item(reference: dict) -> dict | None:
        matched = sorted(
            items_by_url.get(reference.get("url") or "", []),
            key=lambda item: item.get("id") or "",
        )
        if len(matched) > 1:
            warnings.append(
                f"issue #{reference.get('number')} has {len(matched)} project items; "
                "updated the first and left duplicates for maintainer review"
            )
        return matched[0] if matched else None

    def add_comments(reference: dict, body: str) -> None:
        number = reference.get("number")
        if include_comments and number:
            actions.extend(_comment_actions(
                body, number, (comments or {}).get(number, []),
                comment_owner_login, warnings,
            ))

    for model in report["models"]:
        issue = model["issue"]
        desired = desired_fields(model, project, source_sha=source_sha)
        item = matching_item(issue)
        if item:
            actions.extend(_field_actions(
                item_values(item), desired,
                item_id=item["id"], issue=issue.get("number"),
                model_id=model["model_id"],
            ))
        else:
            if not issue.get("node_id"):
                raise SyncError(
                    f"issue #{issue.get('number')} is missing node_id; fetch issues with "
                    "gh api so project items can be created"
                )
            actions.append({
                "action": "add_item",
                "model_id": model["model_id"],
                "issue": issue.get("number"),
                "content_id": issue["node_id"],
                "fields": [
                    field for field in desired
                    if field["data_type"] != "TEXT" or field["value"]
                ],
            })
        add_comments(issue, _comment_body(model, source_sha))

    for error in report.get("errors") or []:
        item = matching_item(error)
        if item is None:
            if not error.get("node_id"):
                raise SyncError(
                    f"issue #{error.get('number')} is missing node_id; fetch issues with "
                    "gh api so project items can be created"
                )
            actions.append({
                "action": "add_item",
                "model_id": error.get("model_id"),
                "issue": error.get("number"),
                "content_id": error["node_id"],
                "fields": [
                    field for field in stale_fields(error, project, source_sha)
                    if field["data_type"] != "TEXT" or field["value"]
                ],
            })
        else:
            actions.extend(_field_actions(
                item_values(item), stale_fields(error, project, source_sha),
                item_id=item["id"], issue=error.get("number"),
                model_id=error.get("model_id"),
            ))
        add_comments(error, _error_comment_body(error, source_sha))

    if repository:
        tracked_urls = {
            model["issue"].get("url")
            for model in report["models"]
            if model["issue"].get("url")
        }
        tracked_urls.update(
            error.get("url")
            for error in report.get("errors") or []
            if error.get("url")
        )
        for item in items:
            tracked_url = _previously_owned_item_url(item, repository)
            if not tracked_url or tracked_url in tracked_urls:
                continue
            existing = item_values(item)
            actions.extend(_field_actions(
                existing,
                _stale_state_fields(
                    project, source_sha, "issue is no longer tracked by this repository",
                ),
                item_id=item["id"], issue=None,
                model_id=(existing.get("Model ID") or {}).get("value"),
            ))
            warnings.append(
                f"project item {item['id']} ({tracked_url}) is no longer tracked; "
                "marked Stale/Unknown and left in the project for manual review"
            )

    return {
        "schema_version": 1,
        "source_sha": source_sha,
        "project": {"id": project.get("id"), "title": project.get("title")},
        "repository": repository or "",
        "models": len(report["models"]),
        "errors": len(report.get("errors") or []),
        "actions": actions,
        "warnings": warnings,
        "summary": {
            "actions": len(actions),
            "item_adds": sum(action["action"] == "add_item" for action in actions),
            "field_updates": sum(
                action["action"] in {"update_field", "clear_field"} for action in actions
            ),
            "comments": sum(action["action"].endswith("_comment") for action in actions),
        },
    }


def build_stale_plan(
    project: dict, items: list[dict], source_sha: str, reason: str, repository: str,
) -> dict:
    """Mark only tracker-owned project items stale after a catalog-wide failure.

    This is the narrow failure path: it never adds items, writes comments, or
    touches items without a same-repository Issue, an owned Tracker Status, and
    a Tracker Issue value equal to that Issue URL.
    """
    require_owned_fields(project)
    desired = _stale_state_fields(project, source_sha, reason)
    actions = []
    touched = 0
    for item in items:
        url = _strict_owned_item_url(item, repository)
        if not url:
            continue
        existing = item_values(item)
        touched += 1
        actions.extend(_field_actions(
            existing, desired,
            item_id=item["id"], issue=None,
            model_id=(existing.get("Model ID") or {}).get("value"),
        ))
    return {
        "schema_version": 1,
        "source_sha": source_sha or "unknown",
        "reason": reason,
        "mode": "stale-reconciliation",
        "repository": repository,
        "project": {"id": project.get("id"), "title": project.get("title")},
        "models": 0,
        "errors": 0,
        "touched_items": touched,
        "actions": actions,
        "warnings": [],
        "summary": {
            "actions": len(actions),
            "item_adds": 0,
            "field_updates": len(actions),
            "comments": 0,
        },
    }


def assert_trusted_write_context(environment: dict | None = None) -> None:
    """Allow live writes only from an explicit trusted default-branch run."""
    environment = os.environ if environment is None else environment
    if environment.get("MODEL_TRACKER_WRITE_ENABLED") != "true":
        raise SyncError("live writes require MODEL_TRACKER_WRITE_ENABLED=true")
    event = environment.get("GITHUB_EVENT_NAME") or ""
    default_branch = environment.get("MODEL_TRACKER_DEFAULT_BRANCH") or "main"
    if event in {"pull_request", "pull_request_target", "workflow_run"}:
        raise SyncError(f"live writes are forbidden for {event} events")
    if event:
        if event != "issues":
            expected = f"refs/heads/{default_branch}"
            if environment.get("GITHUB_REF") != expected:
                raise SyncError(
                    f"live writes require the trusted default branch ({expected}); "
                    f"got {environment.get('GITHUB_REF') or 'no ref'}"
                )
        if not environment.get("GITHUB_REPOSITORY"):
            raise SyncError("live writes in Actions require GITHUB_REPOSITORY")
    elif environment.get("MODEL_TRACKER_ALLOW_LOCAL_WRITE") != "true":
        raise SyncError(
            "local live writes also require MODEL_TRACKER_ALLOW_LOCAL_WRITE=true"
        )


def _apply_field(client: GhClient, project_id: str, action: dict, item_id: str) -> None:
    if action["action"] == "clear_field":
        client.graphql(CLEAR_FIELD, {
            "project": project_id, "item": item_id, "field": action["field_id"],
        })
    elif action["data_type"] == "TEXT":
        client.graphql(UPDATE_TEXT, {
            "project": project_id, "item": item_id, "field": action["field_id"],
            "text": action["value"],
        })
    else:
        client.graphql(UPDATE_SINGLE, {
            "project": project_id, "item": item_id, "field": action["field_id"],
            "option": action["option_id"],
        })


def apply_plan(client: GhClient, plan: dict, repository: str) -> dict:
    """Execute a previously built plan and return a deterministic result summary."""
    project_id = plan["project"]["id"]
    completed: list[dict] = []
    item_ids: dict[str, str] = {}
    for index, action in enumerate(plan["actions"]):
        kind = action["action"]
        created_comment_id = None
        try:
            if kind == "add_item":
                data = client.graphql(ADD_ITEM, {
                    "project": project_id, "content": action["content_id"],
                })
                item_id = (((data.get("addProjectV2ItemById") or {}).get("item") or {}).get("id"))
                if not item_id:
                    raise SyncError(
                        f"addProjectV2ItemById returned no item for issue #{action['issue']}"
                    )
                item_ids[str(action["issue"])] = item_id
                completed.append({
                    "index": index, "action": "add_item",
                    "issue": action.get("issue"), "item_id": item_id,
                })
                for field in action["fields"]:
                    _apply_field(client, project_id, {
                        **field, "action": "update_field",
                    }, item_id)
                    completed.append({
                        "index": index, "action": "update_field",
                        "issue": action.get("issue"), "item_id": item_id,
                        "field": field["field"],
                    })
                continue
            elif kind in {"update_field", "clear_field"}:
                _apply_field(client, project_id, action, action["item_id"])
            elif kind == "create_comment":
                created = client.rest(
                    "POST", f"repos/{repository}/issues/{action['issue']}/comments",
                    {"body": action["body"]},
                )
                created_comment_id = created.get("id") if isinstance(created, dict) else None
                if not created_comment_id:
                    raise SyncError("comment creation returned no comment id")
            elif kind == "update_comment":
                client.rest(
                    "PATCH", f"repos/{repository}/issues/comments/{action['comment_id']}",
                    {"body": action["body"]},
                )
            else:
                raise SyncError(f"unknown sync action {kind!r}")
        except (SyncError, OSError, ValueError, KeyError, TypeError) as error:
            raise SyncApplyError(
                f"sync action {index} ({kind}) failed: {error}",
                action, completed,
            ) from error
        completed.append({
            "index": index,
            "action": kind,
            "issue": action.get("issue"),
            "comment_id": created_comment_id or action.get("comment_id"),
            "previous_body": action.get("previous_body"),
        })
    return {
        "applied_actions": len(completed),
        "added_items": len(item_ids),
        "project": plan["project"],
        "completed_actions": completed,
    }


def _write_json(path: Path | None, value: object) -> None:
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )


def _source_sha(report: dict | None = None) -> str:
    sha = ((report or {}).get("catalog") or {}).get("source_sha")
    return sha or os.environ.get("GITHUB_SHA") or "unknown"


def _execute_plan(client: GhClient, plan: dict, repository: str, args) -> tuple[dict | None, int]:
    if args.apply:
        try:
            return apply_plan(client, plan, repository), 0
        except SyncApplyError as error:
            _write_json(args.result_out, {
                "error": str(error),
                "failed_action": error.failed_action,
                "completed_actions": error.completed_actions,
                "source_sha": plan.get("source_sha"),
            })
            print(f"Model coverage sync failed: {error}", file=sys.stderr)
            return None, 2
    return {
        "dry_run": True,
        "applied_actions": 0,
        "planned_actions": plan["summary"]["actions"],
        "source_sha": plan.get("source_sha"),
    }, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--repository", help="OWNER/REPO; defaults to GITHUB_REPOSITORY")
    parser.add_argument("--project-owner", help="Project owner login; defaults to a repository variable")
    parser.add_argument("--project-number", type=int, help="Project v2 number")
    parser.add_argument("--issues", help="gh issue JSON file, or - for stdin; default: fetch all issues")
    parser.add_argument("--apply", action="store_true", help="perform writes; default is dry run")
    parser.add_argument("--no-comment", action="store_true", help="do not plan tracker comments")
    parser.add_argument("--plan-out", type=Path, help="write the mutation plan JSON here")
    parser.add_argument("--report-out", type=Path, help="write the evaluator JSON here")
    parser.add_argument("--result-out", type=Path, help="write applied/audit results here")
    parser.add_argument(
        "--mark-stale", action="store_true",
        help="mark existing tracker-owned project items Stale instead of evaluating "
             "(narrow catalog-failure reconciliation path)",
    )
    parser.add_argument("--stale-reason", help="failure reason stored in stale Coverage")
    args = parser.parse_args()

    repository = args.repository or os.environ.get("GITHUB_REPOSITORY")
    owner = args.project_owner or os.environ.get("MODEL_TRACKER_PROJECT_OWNER")
    number = args.project_number or os.environ.get("MODEL_TRACKER_PROJECT_NUMBER")
    try:
        number = int(number) if number is not None else None
    except (TypeError, ValueError):
        print("Model coverage sync failed: project number must be an integer", file=sys.stderr)
        return 2
    if not repository or not owner or not number:
        print(
            "Model coverage sync failed: --repository, --project-owner, and --project-number "
            "(or their Actions variables) are required",
            file=sys.stderr,
        )
        return 2
    try:
        repository = require_repository(repository)
    except SyncError as error:
        print(f"Model coverage sync failed: {error}", file=sys.stderr)
        return 2
    if args.apply:
        try:
            assert_trusted_write_context()
        except SyncError as error:
            print(f"Model coverage sync refused: {error}", file=sys.stderr)
            return 2

    client = GhClient(token=os.environ.get("MODEL_TRACKER_PROJECT_TOKEN") or None)
    report = None
    try:
        project = fetch_project(client, owner, number)
        items = fetch_items(client, project["id"])
        if args.mark_stale:
            plan = build_stale_plan(
                project, items, _source_sha(),
                args.stale_reason or "catalog evaluation failed",
                repository,
            )
        else:
            try:
                catalog = build_catalog(args.repo)
            except Exception as error:
                # A catalog-wide failure has no per-model report. Leave an
                # explicit Stale/Unknown audit instead of retaining old status.
                failure = f"catalog evaluation failed: {error}"
                plan = build_stale_plan(
                    project, items, _source_sha(), failure, repository,
                )
                _write_json(args.plan_out, plan)
                _write_json(args.report_out, None)
                result, status = _execute_plan(client, plan, repository, args)
                if status:
                    return status
                result["error"] = failure
                _write_json(args.result_out, result)
                print(f"Model coverage sync failed: {failure}", file=sys.stderr)
                return 2
            issues = (
                bind_repository_issues(client, repository, load_issues(args.issues))
                if args.issues else fetch_issues(client, repository)
            )
            report = evaluate(catalog, issues)
            for error in report["errors"]:
                print(f"Issue #{error.get('number')}: {error['error']}", file=sys.stderr)
            comments: dict[int, list[dict]] = {}
            comment_owner_login = None
            if not args.no_comment:
                numbers = [
                    item["issue"].get("number") for item in report["models"]
                ] + [error.get("number") for error in report["errors"]]
                comments = fetch_comments(
                    client, repository, [number for number in numbers if number],
                )
                comment_owner_login = resolve_comment_owner(
                    client, os.environ.get("MODEL_TRACKER_BOT_LOGIN") or None,
                )
            plan = build_sync_plan(
                report, project, items, comments,
                include_comments=not args.no_comment,
                comment_owner_login=comment_owner_login,
                source_sha=_source_sha(report),
                repository=repository,
            )

        # Persist the exact plan before any mutation so a failure after the
        # first write still leaves a reviewable audit trail.
        _write_json(args.plan_out, plan)
        _write_json(args.report_out, report)
        result, status = _execute_plan(client, plan, repository, args)
        if status:
            return status
        _write_json(args.result_out, result)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, SyncError) as error:
        print(f"Model coverage sync failed: {error}", file=sys.stderr)
        return 2

    output = {"plan": plan, "result": result}
    if report is not None:
        output["report"] = report
    print(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
