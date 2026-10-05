"""Resolve comparison commits from the tree actually checked out by CI."""

import json
import os
import re
import subprocess
from pathlib import Path


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE,
    ).strip()


def resolve_baseline(repo, event_name, event, sha, ref):
    head = git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
    if git(repo, "rev-parse", "HEAD") != head:
        raise ValueError("checkout HEAD differs from the event commit")
    if event_name == "pull_request":
        number = event["number"]
        if ref != f"refs/pull/{number}/merge":
            raise ValueError("pull_request validation requires the synthetic merge checkout")
        parents = git(repo, "rev-list", "--parents", "-n", "1", head).split()[1:]
        if len(parents) != 2 or parents[1] != event["pull_request"]["head"]["sha"]:
            raise ValueError("checkout is not the expected two-parent PR merge")
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
    try:
        comparison = resolve_baseline(
            Path.cwd(), os.environ["GITHUB_EVENT_NAME"],
            json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text()),
            os.environ["GITHUB_SHA"], os.environ["GITHUB_REF"],
        )
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Cannot resolve CI comparison: {error}") from error
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for key, value in comparison.items():
            output.write(f"{key}={value}\n")
    print(f"CI comparison: {comparison['base']} -> {comparison['head']}")


if __name__ == "__main__":
    main()
