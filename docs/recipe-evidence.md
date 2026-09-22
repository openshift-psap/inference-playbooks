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
