"""Resolve comparison commits from the tree actually checked out by CI."""

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse


def git(repo, *args):
    """Run Git without shell interpolation and return stripped stdout."""
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE,
    ).strip()


def github_pr_head(repo, server, repository, number):
    """Read the base repository's live PR head ref from the GitHub server.

    This fallback is only for missing event metadata. If the PR has advanced,
    the caller rejects the old merge rather than accepting a different head.
    """
    parsed = urlparse(server)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("GitHub server must be an HTTPS origin")
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or any(part in (".", "..") for part in repository.split("/"))):
        raise ValueError("invalid GitHub repository identity")
    ref = f"refs/pull/{number}/head"
    rows = git(repo, "ls-remote", "--exit-code", f"{server.rstrip('/')}/{repository}.git", ref).splitlines()
    if len(rows) != 1:
        raise ValueError("GitHub did not return one PR head ref")
    fields = rows[0].split()
    if len(fields) != 2 or fields[1] != ref or not re.fullmatch(r"[0-9a-f]{40}", fields[0]):
        raise ValueError("GitHub returned an invalid PR head identity")
    return fields[0]


def resolve_baseline(repo, event_name, event, sha, ref, head_lookup=None):
    """Resolve the tested comparison, rejecting mismatched checkout identities.

    Prefer event-bound PR head metadata. When absent, require an independent
    head lookup; never treat the merge's second parent as its own proof.
    """
    head = git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if git(repo, "rev-parse", "HEAD") != head:
        raise ValueError("checkout HEAD differs from the event commit")
    if event_name == "pull_request":
        match = re.fullmatch(r"refs/pull/([1-9][0-9]*)/merge", ref)
        if not match:
            raise ValueError("pull_request validation requires the synthetic merge checkout")
        number = int(match[1])
        if event.get("number", number) != number:
            raise ValueError("event PR number differs from the merge ref")
        parents = git(repo, "rev-list", "--parents", "-n", "1", head).split()[1:]
        if len(parents) != 2:
            raise ValueError("checkout is not the expected two-parent PR merge")
        expected_head = event.get("pull_request", {}).get("head", {}).get("sha")
        if expected_head:
            if parents[1] != expected_head:
                raise ValueError("checkout is not the expected two-parent PR merge")
        else:
            if head_lookup is None:
                raise ValueError("missing event PR head requires a GitHub metadata lookup")
            if parents[1] != head_lookup(number):
                raise ValueError("GitHub PR head differs from the tested merge; the PR may have advanced, start a new run")
        base = parents[0]
    elif event_name == "push":
        before = event.get("before", "")
        if not re.fullmatch(r"[0-9a-f]{40}", before) or before == "0" * 40:
            raise ValueError("push comparison requires an existing before commit")
        base = git(repo, "rev-parse", "--verify", f"{before}^{{commit}}")
    else:
        raise ValueError(f"unsupported CI event: {event_name}")
    return {"base": base, "head": head}


def main():
    """Publish comparison outputs only after event and checkout checks pass."""
    try:
        comparison = resolve_baseline(
            Path.cwd(), os.environ["GITHUB_EVENT_NAME"],
            json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()),
            os.environ["GITHUB_SHA"], os.environ["GITHUB_REF"],
            head_lookup=lambda number: github_pr_head(
                Path.cwd(), os.environ["GITHUB_SERVER_URL"],
                os.environ["GITHUB_REPOSITORY"], number,
            ),
        )
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Cannot resolve CI comparison: {error}") from error
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for key, value in comparison.items():
            output.write(f"{key}={value}\n")
    print(f"CI comparison: {comparison['base']} -> {comparison['head']}")


if __name__ == "__main__":
    main()
