# Engine versions and image identification

Contributors identify an image as **default** or **custom** in
`serving.image_usage`, or in a platform override's `image_usage`. Custom images
require a non-empty note explaining the custom image and an explicit engine
declaration. The declaration may reference a fully qualified **tag or digest**;
a digest is not mandatory for recipe engine resolution.

## Default RHOAI runtime

```yaml
serving:
  image: registry.example.org/rhoai/vllm:3.5
  image_usage:
    kind: default
    variant: cuda
```

With platform `{stack: rhoai, version: 3.5.0}`, the maintained release index
resolves the engine to vLLM `0.24.0`. The example image is illustrative, not a
verified default image reference. The contributor's `kind: default` assertion
identifies the runtime; reviewers must check that assertion. Image tags are not
parsed and image prefixes are not used to guess whether a runtime is default.
Digest inspection is not required to use the release mapping.

The index currently covers only CUDA/ROCm/CPU. Other default variants need an
explicit engine declaration until their release mappings are maintained.
Standalone vLLM stacks also need an explicit declaration: a RHOAI mapping does
not apply to a vLLM platform. An explicit default-runtime engine declaration
must agree with an applicable indexed release; a different engine should be
identified as custom instead.

## Custom image

```yaml
serving:
  image: quay.io/example/serving:model-support
  image_usage:
    kind: custom
    note: Custom build required for model-specific support unavailable in the default runtime.
  engine:
    name: vllm
    image: quay.io/example/serving:model-support
    version: 0.25.0+vendor.1
    source: https://example.org/immutable-build-record
```

These illustrative image/source references are not verified mappings. The
engine's `image` must equal the effective serving image, even when using a tag.
The HTTPS source should substantiate the custom engine version. Custom images
**never inherit** an engine version from the default-runtime release index.
The note identifies and explains the custom image; it is not benchmark evidence.

Replacing an image in a platform override discards both base `image_usage` and
base `engine`. Identify the replacement as default or custom in that override;
for custom replacements, provide a fresh note and engine declaration. Metadata
may be inherited only when the effective image reference is unchanged.

## Single maintained index

`engine-versions/index.yaml` contains 19 release entries extracted from the
operator-supplied 2026 RHAI component sheet for `vLLM [CUDA, ROCM, CPU]`.
TPU, Spyre, Neuron, Gaudi, and Omni have distinct rows and must not inherit these
versions. Each release can optionally list known default `images`, using tags
or digests; those references are informational and do not gate release lookup.
No unverified image references have been added.

Patch releases are independent entries. `3.5.0 GA` becomes `3.5.0`;
`3.5 EA1` becomes `3.5.0-ea1`, with the same convention for subsequent EA numbers.
The blank 3.3.2 cell is null; absent releases such as 3.3.4 are not invented.
Release lookup is exact: `3.5` does not silently select `3.5.0` or a latest patch.
The sheet records component associations, not proof that a release has shipped.

The source title, export filename, and SHA-256 retain extraction provenance
without committing the internal spreadsheet or its row/column coordinates. The
original shareable source URL has not been supplied; maintainers should retain
the matching export or add durable provenance before publishing this source as
publicly auditable.

Thibrahi or Saketh maintain the index through reviewed PRs. For updates, verify
the exact release/variant cell against the authoritative mapping, retain its
source and checksum, and run validation and tests. Do not carry values forward
through blank cells or infer engine versions from image tags.

## Resolver handoff

`tools/engine_versions.py` provides:

- `load_engine_index(repo)`: schema validation and duplicate-release checks.
- `release_vllm_version(index, release)`: exact release-component lookup.
- `resolve_engine(serving, overrides, platform, index)`: effective image and engine
  resolution after overrides, using the contributor's image classification.

Resolution returns `state` (`resolved`, `unknown`, `error`), effective `image`,
`version`, `source` (`declaration`, `release-index`, or null), `reason`, and,
when resolved, `evidence` (the declaration URL or index source record).
Unknown/error states cannot inherit validation badges. A resolved engine alone
does not establish validation: badges still require applicable benchmark
evidence. Engine metadata changes neither maturity nor measurement attribution.
Benchmark evidence keeps its existing image-digest/provenance requirements.

`compare_versions(left, right)` returns -1/0/1 or null (incomparable). Upstream
three-component releases compare numerically; a leading `v` is normalized.
Prerelease, development, and vendor builds permit exact equality only, never
ordered inheritance. Missing versions are incomparable.

## Compatibility policy

Git-diff validation requires identification and resolvable metadata for newly
added recipes/platforms and changed effective images. Explicitly declared custom
images require both a note and engine metadata. Existing unchanged, unclassified
images warn and resolve as unknown; they do not inherit defaults. `--current`
validation has no historical baseline and warns for unclassified images.
Malformed/conflicting declarations always fail. Blocked platforms are checked
when enabled. Contributors need not modify existing legacy recipes unrelated
to their submission.
