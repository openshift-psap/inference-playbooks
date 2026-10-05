# Schema changelog

## Add `contributed` maturity level

- `maturity` enum now accepts four values:
  `day-zero`, `contributed`, `validated`, `production`.
- `contributed` marks recipes provided by other Red Hat engineers that have
  been reviewed but not independently tested on our infrastructure.
- Same schema enforcement as `day-zero`: `benchmark_runs` and `image` are
  not required. `validated` and `production` still require both.

## Benchmark artifact external storage

### Structured artifacts

- `benchmark-run.schema.json` `artifacts` items now have a defined schema
  with required `type` field and `oneOf` location: local `path` or
  external `uri` with mandatory `checksum`.
- Supported artifact types: `raw-output`, `log`, `trace`, `model-config`,
  `harness-config`, `metrics-export`, `profile-snapshot`, `other`.
- External URIs support `s3://`, `gs://`, `mlflow://`, and `https://`
  protocols.
- `checksum` uses `sha256:<64-hex-chars>` format, required for external
  artifacts.
- Optional `description` and `size_bytes` fields.
- Validator checks local artifact paths exist and don't escape the run
  directory; external URIs require a checksum.

## Recipe schema v4 layout restructure

### Flat recipe layout

- Recipe path simplified from 9 parts to 4:
  `models/<model-id>/recipes/<recipe-id>/recipe.yaml`
- Stack, version, hardware, and workload no longer encoded in path.
- Recipe ID encodes hardware and deployment context as prefix
  (e.g. `h200-x8-pp2-tp8-agentx-128k`).

### Multi-platform support

- `platform` (singular object) replaced by `platforms` (array, minItems: 1).
- Each platform entry has `stack`, `version`, and `overrides` (path to
  per-platform override file under `platforms/`).
- Override files validated against new `platform-overrides.schema.json`.
- Override merge semantics: `image`/`served_model_name`/`resources`/`router`
  replace; `env` appends; `args` merge by flag; `probes` merge by type.
- Renderer generates manifests per platform into `manifests/<stack>-<version>/`.

### v3 schema removal

- `schema_version` changed from `enum: [3, 4]` to `const: 4`.
- Removed: `platform` (singular), `match`, `deployment.components`,
  `deployment.auxiliary_sources`, and all v3 if/then/else conditionals.
- Top-level `additionalProperties: false` rejects unknown properties.
- `serving` and `platforms` both required at top level.

### Notes schema

- Added `features` property (object with boolean values) to
  `recipe-notes.schema.json`.

## Recipe schema v4 additions

### Image lifecycle

- Adds `image` block with `recommended` (ref, status, why),
  `pin_for_disconnected` (SHA256 digest, status), and `target`
  (convergence image ref, status, why). Status uses the same
  verified/derived/needs-verification state machine as v3.
- `serving.image` remains the operational image string used by
  templates. Validator cross-checks it matches `image.recommended.ref`.
- `image` is required for `validated` and `production` maturity.

### Serving enhancements

- Adds `serving.served_model_name` for the vLLM `--served-model-name`
  endpoint routing value.
- Adds `serving.probes` with optional `startup`, `readiness`, and
  `liveness` overrides. Each can set `failure_threshold`,
  `period_seconds`, `timeout_seconds`, `initial_delay_seconds`, `path`,
  `port`, and `why`. Templates provide defaults; recipe overrides
  specific fields only.

### Features block

- Adds top-level `features` for structured catalog filtering:
  `tool_calling`, `speculative_decoding`, `structured_output`,
  `reasoning_parser`, `prefix_caching`, `kv_cache_dtype`.
- Validator cross-checks feature claims against `serving.args`.
- Moved from `recipe-notes.schema.json` to the recipe itself because
  features are structural claims, not documentation.

### Quantization reference

- Adds optional `quantization` string. Must match an entry in
  `models/<model_id>/model.yaml` quantizations. Gives catalog
  structured quantization filtering.

### Deployment storage

- Adds `deployment.storage` with `type` (pvc, nfs, s3, hostpath),
  typed `pvc` config (name, size, access_modes, storage_class), and
  `why`. Templates can emit storage manifests from this configuration.

### Hardware profile networking

- Structures `network` in `hardware-profile.schema.json` with typed
  fields: `inter_node_transport` (roce, infiniband, tcp, none),
  `inter_node_bandwidth_gbe`, `rdma` (boolean),
  `accelerator_nic_allocation`, `notes`.
- Templates auto-inject networking env vars based on hardware profile
  network type and platform stack (e.g. `KSERVE_INFER_ROCE=True` when
  `rdma: true` + `platform.stack == rhoai`). Recipe `serving.env`
  overrides auto-injected vars.

## Recipe schema v3

### Deployment scope and identity

- `deployment.scope` is the required, canonical node-scope field with values
  `single-node` or `multi-node`.
- `match.nodes` is removed as a supported catalog selector; catalog tooling
  derives node scope from `deployment.scope`.
- Requires explicit platform, hardware-profile, and workload-profile identity.
- Limits workload profiles to the reusable PR #7 benchmark workloads and adds
  deployment-mode plus free-form optimization-intent metadata. `latency` and
  `throughput` remain the standard catalog values.
- Restricts platform-version identifiers to safe path components for CI matrix
  generation.

### Components and container overrides

- Adds manifest-specific component schemas for Deployment,
  LLMInferenceService, LeaderWorkerSet, and the llm-d router. Components
  reference editable `config/` inputs; container settings reuse ordered
  `command`/`args` tokens, `env` values, and optional resource overrides.
- Adds optional per-flag `arg_choices` notes with a required essentiality
  boolean and explanation; an imported manifest may leave the list empty.
- Adds `deployment.auxiliary_sources` for validated references to supporting
  recipe-local manifests such as Services and operator configurations.
- Moves role-specific configuration out of the common deployment object.
- Requires at least one typed deployment component; benchmark runs remain
  optional for day-zero recipes.

### Benchmark evidence and reader notes

- Adds references to benchmark run records for validated and production
  recipes.
- Adds optional, separately validated reader notes for decision rationale,
  image choices, feature claims, omissions, quickstart, known issues, sizing,
  and generated catalog spec references.

## Supporting schemas v1

- Adds model metadata, corrigible hardware-profile, benchmark-run, and
  normalized benchmark-result schemas.
- Benchmark runs record the hardware-profile revision known when they ran.
- Benchmark runs must reference their normalized result; result run IDs
  resolve to exactly one run record.

## Engine metadata foundation

- Added optional image-bound `serving.engine` and platform override `engine`.
- Added `image_usage` classification: custom images require an explanatory note.
- Added one provenance-tracked CUDA/ROCm/CPU release index for contributor-identified
  default RHOAI runtimes, covering patch and EA releases. Image references are optional;
  a recipe image digest is not required for engine resolution.
- New recipes require image identification and resolved metadata during Git-diff validation;
  existing recipes remain compatible with unknown engine versions.
## 2026-09-26

- model.schema.json: add optional `presentation` object (icon_bg, icon_letter, provider, tags) for catalog UI display. Additive; existing instances remain valid.
