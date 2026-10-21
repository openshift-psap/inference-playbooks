# Production and staging Pages

- Production: `https://openshift-psap.github.io/inference-playbooks/`
- Staging: `https://openshift-psap.github.io/inference-playbooks/staging/`
- Long-lived staging branch: `staging/all-pr-manifests`.

The main-branch `Catalog site` workflow publishes one combined Pages artifact.
The root bundle is built and tested from `main`; the preview is built from an
exact captured staging SHA and placed under `staging/`. Relative assets and
navigation stay within their respective sites. Both build-provenance files and
the combined `pages-provenance.json` record the published source revisions.

Main pushes and manual main dispatches rebuild both sites. Staging pushes run
the separate `Staging catalog` workflow's validation and test suite without
publishing privileges. Once that build succeeds, a main-branch `Catalog site`
`workflow_run` rebuilds both sites and
publishes them. Failed staging builds do not replace the live preview. PR runs
only produce unprivileged preview artifacts; fork PRs cannot trigger publication.

The `github-pages` environment remains restricted to `main`. No branch-policy
exception, repository secret or custom DNS is required. The deploy job has no
checkout or build command and publishes only the same run's combined artifact.
It rejects stale production or staging revisions immediately before deployment;
the shared deployment concurrency group serializes publication.

Do not delete or repurpose the staging branch while this workflow is enabled.
To roll back a preview, revert the relevant staging commits and push normally;
the next successful build publishes the reverted revision. Do not force-reset
the long-lived branch. A failure to build either bundle fails publication rather
than dropping the other site or mixing unverified output.
