# Recipe evidence layout

Recipes describe a deployable model profile. They link to immutable hardware
profiles, executable manifests, and benchmark runs. Hardware profiles include
the applicable network/interconnect facts. A generated recipe README presents
the important parts of those inputs; the source files remain the authority.

## Immutable profiles

Hardware profiles are single YAML files. A profile records capabilities that
give a benchmark result its physical meaning, such as GPU model, count, memory,
NVLink topology, and applicable network configuration. It must not include
mutable runtime facts such as image digest, driver version, storage backend, or
workload.

Profiles are corrigible: a factual correction increments `profile_revision`
and adds a correction-log entry. A different accelerator type or count receives
a new profile file. Results record the profile revision known when they ran;
Git history preserves earlier profile contents when missing facts are added.

```text
hardware-profiles/
  nvidia-h200-sxm-8x-nvlink-r1.yaml
```

Recipe and benchmark-run files should use a repository-relative profile path:

```yaml
hardware_profile: hardware-profiles/nvidia-h200-sxm-8x-nvlink-r1.yaml
hardware_profile_revision: 1
```

Each benchmark run additionally records its exact image digest, model revision,
software versions, workload, storage, and sanitized environment details. A
hardware profile alone is not a performance claim.

## CI behavior

`tools/recipe_evidence.py check-hardware-profiles` validates new profiles and
permits only documented corrections to existing ones. `affected-recipes` emits
the recipe directories that require README rendering: changes inside a recipe
select that recipe; a profile correction selects only recipes that explicitly
reference it; renderer/schema changes select all recipes.

The renderer and benchmark normalizer will consume the same references in a
follow-up change. Their checks belong in the same targeted matrix, so an
unrelated recipe is not rendered or parsed.

## Advisory warnings for newly introduced files

`tools/validate.py --base <base> --head <head>` also reviews new file placement.
CI runs this same command; pre-commit uses `--base HEAD --cached` and reads the
staged index, not unstaged recipe edits. The check reads Git data only and never
executes historical code, renders manifests, or invokes Kustomize.

Warnings identify additions (including rename destinations) that lack a normal
source/output owner, for example:

- Extra or nested `manifests/` files that the renderer does not emit and that
  are not explicitly pinned or declared `generated: false`.
- Unreferenced platform overrides, structured guide metadata, or result files
  without a recipe/run reference; `config/` files without active config overrides
  or an explicit input reference.
- Execution/data files in legacy model layouts or shared helper directories
  outside the recipe flow, and operational YAML/JSON under `tests/forge/`.
- Files outside the documented repository roots or nested hardware profiles.

Normal generated outputs, pins, explicit hand-authored exceptions, raw-only
intake, workload inputs under `benchmarks/`, documentation, and ordinary tooling
or test fixtures are recognized. A shared helper can be legitimate: a warning
asks reviewers to establish its ownership and validation coverage, not to move
it automatically. This is a placement/ownership review aid, not a complete
Kustomize dependency-graph audit; an active `config/` directory is treated as
recipe input.

On GitHub Actions these are file-linked `::warning` annotations; locally they
are readable warnings on stderr. They **do not fail validation** or downgrade
existing errors. Unchanged/modified legacy files are not reflagged. `--current`
has no comparison baseline, so it skips this additions-only advisory.

### Visible PR conversation feedback

After a PR's `Recipe evidence` run completes, `file-flow-comment.yml` posts one
bot comment headed **⚠️ Playbook file-flow warnings**, listing the annotated
files and reasons. Subsequent runs update that comment rather than adding noise.
When a successful check reports no warnings, an existing comment is updated to
**✅ Playbook file-flow warnings cleared**. Clean PRs without a previous warning
comment receive no comment. An early CI failure does not clear previous warnings.

The publisher runs on `workflow_run` from the default branch, with only Actions/
Checks read and PR write permissions. It reads GitHub API annotations, never
checks out PR code or downloads artifacts, escapes untrusted annotation text,
and verifies the source workflow, current run attempt, and exact open PR head
before writing. This also supports fork PRs without granting their checks write
access. Closed, stale, or ambiguously associated PRs are skipped. The workflow
must first reach the default branch before it can publish comments.

The comment mirrors GitHub's retained file-flow annotations (platform annotation
limits still apply). It displays at most 30 entries with bounded text/size and
links the source run for any overflow. The comment remains advisory, not a merge
gate; no PR labels, reviews, or branch-protection settings are changed.
