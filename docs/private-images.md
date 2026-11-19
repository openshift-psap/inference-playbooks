# Private RC images for a GA recipe

The recipe's platform is the intended release (for example RHOAI `"3.6"`).
The runtime image can be private without publishing its identity. No exact
RC-to-GA digest comparison or final-image rerun is needed to display measurements
under that release. The catalog keeps a pre-release qualification beside results.

## Declare the binding

In `recipe.yaml`:

```yaml
serving:
  image_visibility: private
  image_env: RHOAI_36_RC_IMAGE
  image_usage:
    kind: custom
    note: Pre-release runtime used to prepare release-day results.
  engine:
    name: vllm
    image_env: RHOAI_36_RC_IMAGE
    version: 0.28.0
    source: https://example.org/public-build-notes
```

The engine version and URL above are illustrative, not verified release metadata.
Supply a verified version and public-safe source. Never infer the version from
the private tag or a future GA mapping. Keep the other required serving fields.
The same fields can appear directly in a platform override, without `serving:`.
Changing the binding invalidates inherited image usage and engine metadata.

Private bindings require an uppercase environment-variable name and prohibit
`image`. Public bindings require `image` and prohibit `image_env`. There is no
shared registry, arbitrary shell expansion or general-purpose secret injection.
Use a dedicated variable, not a credential variable.

## Prepare and execute

Render and commit the public placeholder manifests normally. Rendering never
reads the private variable, even if it happens to be set on the build host.
`private.invalid/runtime:RHOAI_36_RC_IMAGE` is deliberately non-deployable.
Private images currently require template-rendered, single serving resources;
pinned manifests are rejected. Overlays must preserve the image placeholders.

Use the [contributor workflow](contributor-benchmarks.md), with a runner built
from this change (or the native macOS runner packaged from this revision).
An older released runner does not implement private substitution. Publish a new
runner release separately before making it the default; no release digest is
invented by this change.

Set the variable through your execution host's private environment mechanism,
then invoke the package's `run.sh` normally. The container launcher forwards only
the declared variable by name, not its value in command arguments. Missing or
invalid values fail before cluster access. No credentials are stored in recipes.

The runner validates Forge's committed source/submission first, then substitutes
only the serving image in the objects submitted to the API. Every supported
serving role receives the binding. It verifies ready runtime identity before and
after the workload and retains the original capture under `private-diagnostics/`.
Python and child-process output are captured there during private execution.

Only the sanitized evidence archive/checksum may be shared after review. The
export replaces serving images with their original placeholder and marks runtime
image IDs `private`; this is a withholding marker, not a digest. Public run records
omit `image` and `image_digest`, retaining `image_visibility: private` and a
qualification. Public artifact checksums cover the bytes actually exported.
Other deployment configuration, workload checks and measurement normalization
remain in force. The leak guard rejects exports containing known private image
references, registry/repository/tag fragments or observed digests, including in
free-text outputs. Resolve collection failures privately; do not bypass the guard.

Public offline replay cannot independently verify the withheld image identity.
This does not upgrade contributor-controlled evidence to independent validation.
Hosted jobs must not receive private values in their recorded environment. Use
the hub-mounted vault and matching producer/importer described below.

### Hosted Forge compatibility target

Target [Forge PR #322](https://github.com/openshift-psap/forge/pull/322), not Forge
`main`. The inspected PR head is
`6f0512ee4bc12096181cbd5c6d3c00edef26779e`; recheck the head when validating updates.
The companion changes build on that head; the unmodified head lacks this support.

Provision the `psap-forge-private-images` vault on the Forge/Fournos hub. Its
`images.json` file is a JSON object mapping the recipe's `image_env` name to the
private reference. `PSAP_FORGE_PRIVATE_IMAGES_SECRET_PATH` identifies the mounted
vault directory, never the image value. Keep it outside the artifact directory.
Use the normal Fournos vault mounting mechanism. Image-pull credentials belong
on the target GPU cluster; provisioning those credentials is separate.

The updated producer emits bundle contract **v3**, with `private_image` containing
only the public binding and `submission_mode: private-image-override`. Source and
submitted bundle artifacts retain placeholders. Optional Deployment CI placement
is still checked. The final submission boundary validates those bytes before
injecting the vault value into the API request in memory. Resource reads and
readiness operations suppress raw API output. Runtime captures discover observed
digests in memory, then sanitize before writing. Private runs omit raw Pod logs
and Deployment image-status logging. Captures reject mismatched serving images
and heterogeneous observed serving digests.

Private execution artifacts are staged outside the published tree. Before copying
them into the hosted artifact tree, the producer checks every file and relative
path for known private identifiers, including observed digests. A failed check
blocks publication of that staged tree. This also covers workload failures; it
supplements collection-time sanitization rather than replacing it.

The importer independently checks the tested recipe/override binding, placeholder
bundles, and withheld runtime IDs. Private Deployment evidence comes from the
structured Pod capture rather than image-status blocks in `run.log`. Public v1/v2
imports retain their existing contract. Public replay cannot establish the withheld
image identity and must retain the pre-release qualification.

For local producer/importer verification, set `FORGE_PRIVATE_COMPAT_REPO` to the
Forge PR #322 worktree and run `tests/test_forge_bundle_contract.py`. This uses
synthetic data and the real bundle producer; it does not deploy a workload or
establish that hub secrets or target pull credentials are provisioned.

## Move to GA

Replace the binding with the public GA image, remove `image_env`, and provide
fresh image-bound engine metadata:

```yaml
image_visibility: public
image: registry.example.org/runtime:ga
```

The example is illustrative. Use the actual public image when available.
Render the deployment manifests again. Historical runs retain their tested
revision, measurements and private-image attribution; never backfill them with
the GA reference or digest. Exact platform/version matching and all existing
non-image evidence requirements remain unchanged. No automatic tracker sync is
enabled by this feature.
