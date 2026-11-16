#!/usr/bin/env python3
"""Issue-event intake for model coverage tracking issues.

This helper deliberately does not require the Projects dashboard. It runs for
tracking-issue events with the workflow's scoped ``issues: write`` token and:

* assigns the default triage owner on the first ``opened`` event when the YAML
  definition omits ``owner``, the issue is open with no assignees, and its
  paginated history has no prior assignment event;
* creates or updates one issue-specific validation-feedback comment, including
  when ``MODEL_TRACKER_WRITE_ENABLED`` is not enabled and on assignment changes.

Existing assignees and explicit YAML owners are never changed, and any prior
assignment/unassignment history prevents replay. Dashboard field
and comment writes remain in ``model_coverage_sync.py``; the feedback comment
uses a distinct marker and therefore never edits the sync comment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from model_coverage import (
    DEFAULT_TRIAGE_OWNER,
    DEFAULT_TRIAGE_OWNER_LOGIN,
    build_catalog,
    evaluate,
    is_tracking_issue,
    issue_assignees,
    owner_is_omitted,
)
from model_coverage_sync import (
    GhClient,
    SyncError,
    comment_actions,
    fetch_comments,
    fetch_issue,
    fetch_issues,
    require_repository,
    resolve_comment_owner,
    safe_comment_text,
)

FEEDBACK_MARKER = "<!-- inference-playbooks:model-coverage-feedback -->"
FEEDBACK_ACTIONS = frozenset({"opened", "edited", "reopened", "assigned", "unassigned"})
ASSIGNMENT_ACTIONS = frozenset({"opened"})
ASSIGNMENT_EVENT_TYPES = frozenset({"assigned", "unassigned"})


def fetch_assignment_events(
    client: GhClient, repository: str, issue_number: int,
) -> list[dict]:
    """Return paginated issue assignment/unassignment events.

    GitHub's issue events endpoint is the authoritative history for deciding
    whether first-intake assignment has already happened. It is different from
    comments and survives a later manual unassignment.
    """
    events = client.rest_paginated(
        f"repos/{repository}/issues/{issue_number}/events?per_page=100"
    )
    return [
        event for event in events
        if isinstance(event, dict)
        and event.get("event") in ASSIGNMENT_EVENT_TYPES
    ]


def assignment_skip_reason(
    issue: dict, assignment_events: list[dict] | None,
) -> str | None:
    """Return why default assignment is unsafe, or ``None`` when eligible.

    Eligibility is deliberately conservative. Unknown history, a non-open
    issue, an existing assignee, an explicit YAML owner, or any prior
    assignment/unassignment event all prohibit a new automatic assignment.
    """
    state = str(issue.get("state") or "").lower()
    if state != "open":
        return f"issue state is {state or 'unknown'}, not open"
    assignees = issue_assignees(issue)
    if assignees:
        return "issue already has assignee(s): " + ", ".join(
            f"@{login}" for login in assignees
        )
    body = issue.get("body") or ""
    if not owner_is_omitted(body):
        return (
            "the current definition declares an owner or its fenced YAML is not "
            "parseable"
        )
    if assignment_events is None:
        return "assignment history could not be verified"
    if assignment_events:
        return (
            "issue assignment history already exists; first-intake assignment "
            "is not replayed"
        )
    return None



def assert_trusted_intake_context(environment: dict | None = None) -> None:
    """Allow intake writes only for trusted issue-event or explicit local runs."""
    environment = os.environ if environment is None else environment
    event = environment.get("GITHUB_EVENT_NAME") or ""
    if event:
        if event != "issues":
            raise SyncError(
                f"intake writes are forbidden for {event} events; only issue events "
                "may assign owners or post feedback"
            )
        if not environment.get("GITHUB_REPOSITORY"):
            raise SyncError("intake writes in Actions require GITHUB_REPOSITORY")
        default_branch = environment.get("MODEL_TRACKER_DEFAULT_BRANCH") or "main"
        expected = f"refs/heads/{default_branch}"
        ref = environment.get("GITHUB_REF")
        if ref and ref != expected:
            raise SyncError(
                f"intake writes require the trusted default branch ({expected}); "
                f"got {ref}"
            )
    elif environment.get("MODEL_TRACKER_ALLOW_LOCAL_WRITE") != "true":
        raise SyncError(
            "local intake writes require MODEL_TRACKER_ALLOW_LOCAL_WRITE=true"
        )


def load_event(path: str | Path) -> dict:
    """Load and minimally validate a GitHub issue event payload."""
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise SyncError("GitHub event payload must be a JSON object")
    action = value.get("action")
    if not isinstance(action, str) or not action:
        raise SyncError("GitHub event payload is missing an action")
    issue = value.get("issue")
    if not isinstance(issue, dict):
        raise SyncError("GitHub event payload is missing an issue object")
    number = issue.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise SyncError(f"GitHub event payload has an invalid issue number {number!r}")
    return value


def _owner_label(owner_source: str | None) -> str:
    return {
        "explicit": "explicit YAML owner",
        "assignees": "derived from current issue assignees",
        "default": "default triage owner",
    }.get(owner_source or "", "unknown source")


def feedback_comment_body(
    issue_number: int,
    *,
    model: dict | None = None,
    error: dict | None = None,
    definition_warnings: list[dict] | None = None,
    source_sha: str = "unknown",
    dashboard_sync_enabled: bool = False,
    action: str | None = None,
    assignment_planned: bool = False,
) -> str:
    """Render one issue-specific intake result without echoing issue prose."""
    definition_warnings = definition_warnings or []
    result = "invalid" if error else "valid"
    model_id = (model or {}).get("model_id") or (error or {}).get("model_id")
    heading = f"`{model_id}`" if model_id else f"issue #{issue_number}"
    lines = [
        FEEDBACK_MARKER,
        f"## Model coverage intake: {heading}",
        "",
        f"**Result:** {result}",
    ]
    if error:
        lines.extend([
            "",
            f"The tracker could not evaluate this issue definition at "
            f"`{source_sha or 'unknown'}`.",
            "",
            f"**Error:** {safe_comment_text(error.get('error'), 800)}",
            "",
            "### How to fix it",
            "",
            "- Keep exactly one fenced `yaml` block and keep the tracker marker.",
            "- Quote numeric-looking versions: `version: \"3.6\"`, not `version: 3.6`.",
            "- Use the recipe's `recipe_id` field, not its directory name.",
            "- Put public checkpoint links, hardware, required features, and stretch "
            "goals in the prose fields outside the YAML block.",
            "- `owner` is optional. Omit it (or leave it empty) to use the default "
            f"triage owner {DEFAULT_TRIAGE_OWNER}, or use a GitHub user/team handle.",
        ])
    else:
        owner_source = model.get("owner_source") if model else None
        lines.extend([
            "",
            f"**Status:** {model['status']} | "
            f"**Completed:** {'yes' if model['completed'] else 'no'} | "
            f"**Completion criterion:** {model['completion']} | "
            f"**Coverage:** {model['coverage']['completed_platforms']}/"
            f"{model['coverage']['target_platforms']} platforms",
            "",
            f"**Owner:** {model['owner']} ({_owner_label(owner_source)})",
        ])
        if owner_source == "explicit":
            lines.append(
                "The YAML owner is authoritative; the intake workflow does not "
                "reassign an explicitly owned issue."
            )
        elif owner_source == "assignees":
            lines.append(
                "No owner is declared in YAML, so the tracker follows the current "
                "issue assignees in GitHub's assignment order. Change assignees to "
                "delegate ownership; no YAML edit is required."
            )
        else:
            if action == "opened" and assignment_planned:
                lines.append(
                    "No owner is declared and no assignee was present before this "
                    f"intake. The workflow attempts a one-time assignment to the "
                    f"default triage owner {DEFAULT_TRIAGE_OWNER}; if assignment is "
                    "unavailable, the tracker still uses that default."
                )
            else:
                lines.append(
                    "No owner is declared and no assignee is currently present. The "
                    f"tracker uses the default triage owner {DEFAULT_TRIAGE_OWNER}. "
                    "This event does not reassign an ownerless issue; assign a GitHub "
                    "user to delegate, or state an explicit owner in YAML."
                )
        lines.extend([
            "",
            "Only the required `targets` in the fenced YAML block count toward "
            "automated completion. Stretch goals in the prose section are visible "
            "planning context and do not block completion.",
        ])
    if definition_warnings:
        lines.extend(["", "### Warnings", ""])
        lines.extend(
            f"- {safe_comment_text(warning.get('warning'), 500)}"
            for warning in definition_warnings
        )
        lines.append(
            "Unknown recipe IDs are warnings, not errors: a planned recipe is valid. "
            "Check the recipe's `recipe_id` field if the target was meant to be "
            "existing."
        )
    if dashboard_sync_enabled:
        lines.extend([
            "",
            "Project dashboard sync is enabled; a separate dashboard comment and "
            "project fields may also update.",
        ])
    else:
        lines.extend([
            "",
            "Project dashboard sync is disabled (`MODEL_TRACKER_WRITE_ENABLED` is not "
            "`true`). This intake comment is the issue-specific validation result; no "
            "Project fields and no dashboard sync comment are written.",
        ])
    lines.extend([
        "",
        "_Maintained by the model-coverage intake workflow. Edit the issue's fenced "
        "YAML definition; do not edit this comment. See `docs/model-tracker.md`._",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _ignored_plan(action: str, issue_number: int, *, tracking: bool) -> dict:
    return {
        "schema_version": 1,
        "action": action,
        "issue": issue_number,
        "tracking": tracking,
        "result": "ignored",
        "model_id": None,
        "owner": None,
        "owner_source": None,
        "error": None,
        "definition_warnings": [],
        "warnings": [],
        "actions": [],
        "skipped_actions": [],
        "summary": {
            "actions": 0,
            "assignments": 0,
            "comments": 0,
            "skipped": 0,
            "definition_warnings": 0,
            "warnings": 0,
        },
    }


def build_intake_plan(
    report: dict,
    issue: dict,
    *,
    action: str,
    comments: dict[int, list[dict]] | None = None,
    comment_owner_login: str | None = None,
    source_sha: str = "unknown",
    dashboard_sync_enabled: bool = False,
    assignment_events: list[dict] | None = None,
    include_assignment: bool = True,
    include_feedback: bool = True,
) -> dict:
    """Build an idempotent assignment and feedback plan without writing."""
    number = issue.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise SyncError(f"intake issue has an invalid number {number!r}")
    if not is_tracking_issue(issue):
        return _ignored_plan(action, number, tracking=False)

    model = next(
        (item for item in report.get("models") or []
         if (item.get("issue") or {}).get("number") == number),
        None,
    )
    error = next(
        (item for item in report.get("errors") or []
         if item.get("number") == number),
        None,
    )
    if model is None and error is None:
        return _ignored_plan(action, number, tracking=True)

    definition_warnings = [
        warning for warning in report.get("warnings") or []
        if warning.get("number") == number
    ]
    if model:
        owner = model["owner"]
        owner_source = model.get("owner_source")
        model_id = model["model_id"]
        error_message = None
    else:
        owner = error.get("owner")
        owner_source = error.get("owner_source")
        if not owner:
            assignees = issue_assignees(issue)
            owner = (
                ", ".join(f"@{login}" for login in assignees)
                if assignees else DEFAULT_TRIAGE_OWNER
            )
            owner_source = "assignees" if assignees else "default"
        model_id = error.get("model_id")
        error_message = error.get("error") or "definition could not be evaluated"

    actions: list[dict] = []
    skipped_actions: list[dict] = []
    planner_warnings: list[str] = []
    assignment_planned = False
    if include_assignment and action in ASSIGNMENT_ACTIONS:
        skip_reason = assignment_skip_reason(issue, assignment_events)
        if skip_reason:
            skipped_actions.append({
                "action": "assign_default_owner",
                "issue": number,
                "reason": skip_reason,
            })
        else:
            assignment_planned = True
            actions.append({
                "action": "assign_default_owner",
                "issue": number,
                "login": DEFAULT_TRIAGE_OWNER_LOGIN,
                "previous_assignees": [],
            })

    if include_feedback and action in FEEDBACK_ACTIONS:
        body = feedback_comment_body(
            number,
            model=model,
            error=error,
            definition_warnings=definition_warnings,
            source_sha=source_sha,
            dashboard_sync_enabled=dashboard_sync_enabled,
            action=action,
            assignment_planned=assignment_planned,
        )
        actions.extend(comment_actions(
            body,
            number,
            (comments or {}).get(number, []),
            comment_owner_login,
            planner_warnings,
            marker=FEEDBACK_MARKER,
            create_when_foreign=True,
        ))

    result = "invalid" if error else "valid"
    return {
        "schema_version": 1,
        "action": action,
        "issue": number,
        "tracking": True,
        "result": result,
        "model_id": model_id,
        "owner": owner,
        "owner_source": owner_source,
        "error": error_message,
        "definition_warnings": definition_warnings,
        "warnings": planner_warnings,
        "actions": actions,
        "skipped_actions": skipped_actions,
        "summary": {
            "actions": len(actions),
            "assignments": sum(
                item["action"] == "assign_default_owner" for item in actions
            ),
            "comments": sum(
                item["action"] in {"create_comment", "update_comment"}
                for item in actions
            ),
            "skipped": len(skipped_actions),
            "definition_warnings": len(definition_warnings),
            "warnings": len(planner_warnings),
        },
    }


def _empty_apply_result() -> dict:
    return _finalize_apply_result([], [], [], [], [])


def _finalize_apply_result(
    completed: list[dict], skipped: list[dict], failed: list[dict],
    uncertain: list[dict], warnings: list[str],
) -> dict:
    return {
        "applied_actions": len(completed),
        "completed_actions": completed,
        "skipped_actions": skipped,
        "failed_actions": failed,
        "uncertain_actions": uncertain,
        "warnings": warnings,
        "operational_failure": bool(failed or uncertain),
        "summary": {
            "applied": len(completed),
            "skipped": len(skipped),
            "failed": len(failed),
            "uncertain": len(uncertain),
        },
    }


def _failure_result(
    action: str, issue_number: int, reason: str, *, uncertain: bool = False,
) -> dict:
    entry = {"action": action, "issue": issue_number, "reason": reason}
    return _finalize_apply_result(
        [], [], [] if uncertain else [entry],
        [entry] if uncertain else [], [],
    )


def _merge_apply_results(results: list[dict], planned_actions: int) -> dict:
    completed: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    uncertain: list[dict] = []
    warnings: list[str] = []
    index = 0
    for result in results:
        for key, target in (
            ("completed_actions", completed),
            ("skipped_actions", skipped),
            ("failed_actions", failed),
            ("uncertain_actions", uncertain),
        ):
            for entry in result.get(key) or []:
                target.append({**entry, "index": index})
                index += 1
        warnings.extend(result.get("warnings") or [])
    merged = _finalize_apply_result(completed, skipped, failed, uncertain, warnings)
    merged["summary"]["planned"] = planned_actions
    return merged


def _merge_plans(*plans: dict | None) -> dict:
    active = [plan for plan in plans if plan]
    if not active:
        raise SyncError("cannot merge an empty intake plan set")
    base = dict(active[-1])
    actions: list[dict] = []
    skipped: list[dict] = []
    warnings: list[str] = []
    for plan in active:
        actions.extend(plan.get("actions") or [])
        skipped.extend(plan.get("skipped_actions") or [])
        warnings.extend(plan.get("warnings") or [])
    base["actions"] = actions
    base["skipped_actions"] = skipped
    base["warnings"] = warnings
    base["summary"] = {
        "actions": len(actions),
        "assignments": sum(
            item["action"] == "assign_default_owner" for item in actions
        ),
        "comments": sum(
            item["action"] in {"create_comment", "update_comment"}
            for item in actions
        ),
        "skipped": len(skipped),
        "definition_warnings": len(base.get("definition_warnings") or []),
        "warnings": len(warnings),
    }
    return base


def _skip_feedback_plan(action: str, issue_number: int, reason: str) -> dict:
    plan = _ignored_plan(action, issue_number, tracking=False)
    plan["skipped_actions"] = [{
        "action": "post_feedback", "issue": issue_number, "reason": reason,
    }]
    plan["summary"]["skipped"] = 1
    return plan


def _replace_issue(issues: list[dict], issue: dict) -> list[dict]:
    number = issue.get("number")
    replaced = [
        issue if item.get("number") == number else item for item in issues
    ]
    if all(item.get("number") != number for item in replaced):
        replaced.append(issue)
    return replaced


def _dry_run_result(plan: dict) -> dict:
    result = _empty_apply_result()
    result["dry_run"] = True
    result["planned_actions"] = plan["summary"]["actions"]
    result["planned_skips"] = plan["summary"]["skipped"]
    result["summary"]["planned"] = plan["summary"]["actions"]
    return result


def apply_intake_plan(client: GhClient, plan: dict, repository: str) -> dict:
    """Apply assignment and feedback actions; never touch project fields.

    Assignment is revalidated against the authoritative issue and paginated
    assignment history immediately before writing. API failures are recorded and
    do not stop later feedback actions; a read-back failure after a successful
    assignment POST is reported as uncertain rather than completed or skipped.
    """
    repository = require_repository(repository)
    completed: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    uncertain: list[dict] = []
    warnings: list[str] = []
    for index, action in enumerate(plan["actions"]):
        kind = action["action"]
        issue_number = action.get("issue")
        action_entry = {
            "index": index,
            "action": kind,
            "issue": issue_number,
        }
        if kind == "assign_default_owner":
            try:
                current_issue = fetch_issue(client, repository, issue_number)
                assignment_events = fetch_assignment_events(
                    client, repository, issue_number,
                )
            except (SyncError, OSError, ValueError, KeyError, TypeError) as error:
                reason = f"assignment revalidation failed: {error}"
                failed.append({**action_entry, "reason": reason})
                warnings.append(f"issue #{issue_number}: {reason}")
                continue
            skip_reason = assignment_skip_reason(current_issue, assignment_events)
            if skip_reason:
                skipped.append({**action_entry, "reason": skip_reason})
                continue
            try:
                client.rest(
                    "POST",
                    f"repos/{repository}/issues/{issue_number}/assignees",
                    {"assignees": [action["login"]]},
                )
            except (SyncError, OSError, ValueError, KeyError, TypeError) as error:
                reason = f"assignment request failed: {error}"
                failed.append({**action_entry, "reason": reason})
                warnings.append(f"issue #{issue_number}: {reason}")
                continue
            try:
                read_back = fetch_issue(client, repository, issue_number)
            except (SyncError, OSError, ValueError, KeyError, TypeError) as error:
                reason = (
                    "assignment request returned success but read-back failed: "
                    f"{error}; assignment outcome is uncertain"
                )
                uncertain.append({**action_entry, "reason": reason})
                warnings.append(f"issue #{issue_number}: {reason}")
                continue
            if action["login"] not in issue_assignees(read_back):
                reason = (
                    f"assignment was accepted but {action['login']!r} is not an "
                    "assignee on read-back; the login may not be assignable"
                )
                failed.append({**action_entry, "reason": reason})
                warnings.append(f"issue #{issue_number}: {reason}")
                continue
            completed.append({**action_entry, "login": action["login"]})
        elif kind == "create_comment":
            try:
                created = client.rest(
                    "POST",
                    f"repos/{repository}/issues/{issue_number}/comments",
                    {"body": action["body"]},
                )
            except (SyncError, OSError, ValueError, KeyError, TypeError) as error:
                reason = f"feedback comment creation failed: {error}"
                failed.append({**action_entry, "reason": reason})
                warnings.append(f"issue #{issue_number}: {reason}")
                continue
            comment_id = created.get("id") if isinstance(created, dict) else None
            if not comment_id:
                reason = "feedback comment creation returned no comment id"
                failed.append({**action_entry, "reason": reason})
                warnings.append(f"issue #{issue_number}: {reason}")
                continue
            completed.append({**action_entry, "comment_id": comment_id})
        elif kind == "update_comment":
            try:
                client.rest(
                    "PATCH",
                    f"repos/{repository}/issues/comments/{action['comment_id']}",
                    {"body": action["body"]},
                )
            except (SyncError, OSError, ValueError, KeyError, TypeError) as error:
                reason = f"feedback comment update failed: {error}"
                failed.append({**action_entry, "reason": reason})
                warnings.append(f"issue #{issue_number}: {reason}")
                continue
            completed.append({
                **action_entry, "comment_id": action["comment_id"],
            })
        else:
            raise SyncError(f"unknown intake action {kind!r}")
    return _finalize_apply_result(completed, skipped, failed, uncertain, warnings)


def run_intake(
    client: GhClient, repository: str, action: str, issue_number: int,
    *,
    repo: Path = Path.cwd(),
    apply: bool = False,
    dashboard_sync_enabled: bool = False,
    catalog: dict | None = None,
    comment_owner_login: str | None = None,
) -> tuple[dict, dict | None, dict]:
    """Load, plan, and optionally apply one issue event.

    Dry runs remain pure planning. Apply mode performs assignment first, then
    refreshes the authoritative issue, re-evaluates it against the already
    loaded catalog/issues snapshot, re-reads comments, and builds feedback from
    the resulting state so a concurrent owner or definition edit is reflected.
    """
    repository = require_repository(repository)
    authoritative = fetch_issue(client, repository, issue_number)
    if not is_tracking_issue(authoritative):
        plan = _ignored_plan(action, issue_number, tracking=False)
        return plan, None, (
            _dry_run_result(plan) if not apply
            else _merge_apply_results([], plan["summary"]["actions"])
        )

    if catalog is None:
        catalog = build_catalog(repo)
    issues = _replace_issue(fetch_issues(client, repository), authoritative)
    report = evaluate(catalog, issues)
    owner_login = comment_owner_login
    if owner_login is None and action in FEEDBACK_ACTIONS:
        owner_login = resolve_comment_owner(
            client, os.environ.get("MODEL_TRACKER_BOT_LOGIN") or None,
        )

    if not apply:
        comments = (
            fetch_comments(client, repository, [issue_number])
            if action in FEEDBACK_ACTIONS else {}
        )
        assignment_events = None
        if action in ASSIGNMENT_ACTIONS:
            try:
                assignment_events = fetch_assignment_events(
                    client, repository, issue_number,
                )
            except (SyncError, OSError, ValueError, KeyError, TypeError):
                assignment_events = None
        plan = build_intake_plan(
            report, authoritative, action=action,
            comments=comments, comment_owner_login=owner_login,
            source_sha=_source_sha(report),
            dashboard_sync_enabled=dashboard_sync_enabled,
            assignment_events=assignment_events,
        )
        return plan, report, _dry_run_result(plan)

    assignment_events = None
    if action in ASSIGNMENT_ACTIONS:
        try:
            assignment_events = fetch_assignment_events(
                client, repository, issue_number,
            )
        except (SyncError, OSError, ValueError, KeyError, TypeError):
            # Unknown history is deliberately treated as unsafe; still build
            # and post issue feedback instead of failing intake.
            assignment_events = None
    assignment_plan = build_intake_plan(
        report, authoritative, action=action,
        comment_owner_login=owner_login,
        source_sha=_source_sha(report),
        dashboard_sync_enabled=dashboard_sync_enabled,
        assignment_events=assignment_events,
        include_feedback=False,
    )
    results = [apply_intake_plan(client, assignment_plan, repository)]
    final_plan = assignment_plan
    refreshed_report: dict | None = report

    if action in FEEDBACK_ACTIONS:
        try:
            refreshed = fetch_issue(client, repository, issue_number)
        except (SyncError, OSError, ValueError, KeyError, TypeError) as error:
            results.append(_failure_result(
                "refresh_issue", issue_number,
                f"post-assignment issue refresh failed: {error}",
            ))
        else:
            if not is_tracking_issue(refreshed):
                final_plan = _merge_plans(
                    assignment_plan,
                    _skip_feedback_plan(
                        action, issue_number,
                        "the issue is no longer tracked after assignment handling",
                    ),
                )
                refreshed_report = None
            else:
                refreshed_report = evaluate(
                    catalog, _replace_issue(issues, refreshed),
                )
                try:
                    comments = fetch_comments(
                        client, repository, [issue_number],
                    )
                except (SyncError, OSError, ValueError, KeyError, TypeError) as error:
                    results.append(_failure_result(
                        "fetch_comments", issue_number,
                        f"feedback comment re-read failed: {error}",
                    ))
                else:
                    feedback_plan = build_intake_plan(
                        refreshed_report, refreshed, action=action,
                        comments=comments, comment_owner_login=owner_login,
                        source_sha=_source_sha(refreshed_report),
                        dashboard_sync_enabled=dashboard_sync_enabled,
                        include_assignment=False,
                    )
                    results.append(
                        apply_intake_plan(client, feedback_plan, repository)
                    )
                    final_plan = _merge_plans(assignment_plan, feedback_plan)
    return final_plan, refreshed_report, _merge_apply_results(
        results, final_plan["summary"]["actions"],
    )



def to_intake_markdown(plan: dict, result: dict | None = None) -> str:
    """Render a workflow-summary view of one intake plan and apply result."""
    summary = plan.get("summary") or {}
    lines = [
        "## Model coverage intake",
        "",
        f"- Issue: #{plan.get('issue')}",
        f"- Event action: {plan.get('action')}",
        f"- Tracking issue: {'yes' if plan.get('tracking') else 'no'}",
        f"- Result: **{plan.get('result')}**",
        f"- Owner: {plan.get('owner') or '—'} ({_owner_label(plan.get('owner_source'))})",
        f"- Planned actions: {summary.get('actions', 0)}",
        f"- Planned owner assignments: {summary.get('assignments', 0)}",
        f"- Planned feedback comment actions: {summary.get('comments', 0)}",
        f"- Planned skips: {summary.get('skipped', 0)}",
        f"- Definition warnings: {summary.get('definition_warnings', 0)}",
    ]
    if result is not None:
        actual = result.get("summary") or {}
        lines.extend([
            "",
            "### Apply result",
            "",
            f"- Applied: {actual.get('applied', result.get('applied_actions', 0))}",
            f"- Skipped: {actual.get('skipped', 0)}",
            f"- Failed: {actual.get('failed', 0)}",
            f"- Uncertain: {actual.get('uncertain', 0)}",
        ])
        if result.get("dry_run"):
            lines.append("- Mode: dry run (no writes)")
        for failed in result.get("failed_actions") or []:
            lines.append(
                f"- Failed {failed.get('action')}: "
                f"{safe_comment_text(failed.get('reason'), 500)}"
            )
        for uncertain in result.get("uncertain_actions") or []:
            lines.append(
                f"- Uncertain {uncertain.get('action')}: "
                f"{safe_comment_text(uncertain.get('reason'), 500)}"
            )
        for warning in result.get("warnings") or []:
            lines.append(f"- Apply warning: {safe_comment_text(warning, 500)}")
    for skipped in plan.get("skipped_actions") or []:
        lines.append(
            f"- Skipped {skipped.get('action')}: "
            f"{safe_comment_text(skipped.get('reason'), 500)}"
        )
    if plan.get("error"):
        lines.extend(["", f"**Error:** {safe_comment_text(plan['error'], 800)}"])
    for warning in plan.get("definition_warnings") or []:
        lines.append(f"- Warning: {safe_comment_text(warning.get('warning'), 500)}")
    for warning in plan.get("warnings") or []:
        lines.append(f"- Planner warning: {safe_comment_text(warning, 500)}")
    return "\n".join(lines).rstrip() + "\n"


def _write_json(path: Path | None, value: object) -> None:
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )


def _source_sha(report: dict | None) -> str:
    return ((report or {}).get("catalog") or {}).get("source_sha") or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--repository", help="OWNER/REPO; defaults to GITHUB_REPOSITORY")
    parser.add_argument("--event", help="GitHub issue event JSON; defaults to GITHUB_EVENT_PATH")
    parser.add_argument("--apply", action="store_true", help="perform writes; default is dry run")
    parser.add_argument("--plan-out", type=Path, help="write the intake plan JSON here")
    parser.add_argument("--report-out", type=Path, help="write the evaluator JSON here")
    parser.add_argument("--result-out", type=Path, help="write applied/audit results here")
    parser.add_argument("--summary-out", type=Path, help="write the workflow summary markdown here")
    args = parser.parse_args()

    repository = args.repository or os.environ.get("GITHUB_REPOSITORY")
    event_path = args.event or os.environ.get("GITHUB_EVENT_PATH")
    if not repository:
        print(
            "Model coverage intake failed: --repository or GITHUB_REPOSITORY is required",
            file=sys.stderr,
        )
        return 2
    if not event_path:
        print(
            "Model coverage intake failed: --event or GITHUB_EVENT_PATH is required",
            file=sys.stderr,
        )
        return 2
    try:
        repository = require_repository(repository)
        if args.apply:
            assert_trusted_intake_context()
        event = load_event(event_path)
        action = event["action"]
        event_repository = (event.get("repository") or {}).get("full_name")
        if event_repository and event_repository != repository:
            raise SyncError(
                f"event repository {event_repository!r} does not match {repository}"
            )
        number = event["issue"]["number"]
        client = GhClient()
        plan, report, result = run_intake(
            client, repository, action, number,
            repo=args.repo,
            apply=args.apply,
            dashboard_sync_enabled=(
                os.environ.get("MODEL_TRACKER_WRITE_ENABLED") == "true"
            ),
        )
        _write_json(args.plan_out, plan)
        _write_json(args.report_out, report)
        if args.summary_out:
            args.summary_out.parent.mkdir(parents=True, exist_ok=True)
            args.summary_out.write_text(to_intake_markdown(plan, result))
        _write_json(args.result_out, result)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, SyncError) as error:
        print(f"Model coverage intake failed: {error}", file=sys.stderr)
        return 2

    for warning in plan.get("definition_warnings") or []:
        print(f"Issue #{plan['issue']}: warning: {warning['warning']}", file=sys.stderr)
    for warning in plan.get("warnings") or []:
        print(f"Issue #{plan['issue']}: warning: {warning}", file=sys.stderr)
    for failed in result.get("failed_actions") or []:
        print(
            f"Issue #{plan['issue']}: {failed.get('action')} failed: "
            f"{failed.get('reason')}",
            file=sys.stderr,
        )
    for uncertain in result.get("uncertain_actions") or []:
        print(
            f"Issue #{plan['issue']}: {uncertain.get('action')} outcome uncertain: "
            f"{uncertain.get('reason')}",
            file=sys.stderr,
        )
    for warning in result.get("warnings") or []:
        print(f"Issue #{plan['issue']}: warning: {warning}", file=sys.stderr)
    if plan["result"] == "invalid":
        print(
            f"Issue #{plan['issue']}: invalid definition: {plan.get('error')}",
            file=sys.stderr,
        )
    print(json.dumps({"plan": plan, "result": result}, indent=2, sort_keys=True))
    if result.get("operational_failure"):
        return 2
    return 1 if plan["result"] == "invalid" else 0


if __name__ == "__main__":
    raise SystemExit(main())
