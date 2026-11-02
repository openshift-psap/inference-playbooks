# Validation promotion reviewer checklist

The [recipe's](../recipe.yaml) `maturity` and referenced benchmark evidence own
validation status. Reader views should derive status from those fields, not
from flag rationale, headlines, comments, or deployment guides.

When reviewing a PR that promotes this recipe:

- [ ] Commit complete benchmark provenance, authoritative raw output, and
  parser-generated normalized results; link the runs from the recipe.
- [ ] Ensure evidence matches the deployed image digest, model revision,
  hardware-profile revision, serving settings, workload, and storage backend.
- [ ] **Human-reconcile every entry in [`known_issues`](notes.yaml): close
  addressed questions alongside adding evidence and updating `maturity`.**
  Resolve promotion blockers, remove obsolete entries, and explicitly justify
  any remaining deployment risks. In particular, reconcile the context budget,
  driver/backend compatibility, RDMA settings, image identity, storage, and
  security permissions.
- [ ] Keep flag rationale focused on configuration intent; do not add generic
  validation-status prose elsewhere.
- [ ] Run schema/provenance validation, rendering, and manifest-drift checks.
  CI checks structured evidence requirements; reviewers assess whether the
  concrete deployment questions and risks have been resolved.
