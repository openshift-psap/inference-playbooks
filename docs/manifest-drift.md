# Manifest drift gate

Run the same checker locally and in CI:

```sh
# One recipe, current files:
python3 tools/check_manifests.py models/<model>/recipes/<recipe>
# Complete publication inputs, independent of the affected matrix:
python3 tools/check_manifests.py --all
# Staged pre-commit inputs (isolated index snapshot):
python3 tools/check_manifests.py --base HEAD --cached
# A committed diff:
python3 tools/check_manifests.py --base <base-sha> --head <head-sha>
```

Install `tools/requirements.txt`; install Kustomize v5.6.0 on PATH for recipes
with config overlays. Missing tools or failed rendering fail the gate. Rendering
and comparison happen in temporary directories: contributor files are never
rewritten, reset, or committed by the checker. Regenerate reported paths with
`python3 tools/render.py <recipe>/recipe.yaml`, inspect the diff, and stage the
outputs yourself. Pinned manifests are copied verbatim and compared when their
source differs from the destination; a pin already at its destination is
authoritative rather than independently reproducible.

Selection uses `tools/recipe_evidence.py`: recipe-local changes select that
recipe; model metadata selects its recipes; corrections select exact hardware
profile references; newly added profiles do not fan out. Tools, templates,
schemas, engine index, workflow, and pre-commit changes select all recipes.
Raw-only intake has no recipe to render; adding its recipe activates selection.
Raw syntax and the CI requirement to convert before merge remain validator gates.

Baseline at main `25b8820`: all four v4 recipe/platform outputs were absent.
The foundation adds them through the unchanged rendering templates, without
hand-editing outputs or changing recipe configurations. Catalog/UI work is not
part of this gate.
