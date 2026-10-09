# Single-node vLLM companions

Normal local rendering automatically adds an eligible missing vLLM platform to
the **same recipe**, with explicit overrides and manifests. No separate preparation is required.

```bash
python3 tools/render.py models/<model>/recipes/<recipe>/recipe.yaml
python3 tools/validate.py --current
python3 tools/check_manifests.py models/<model>/recipes/<recipe>
```

Rendering preflights both platforms before materializing inputs/outputs; review the
diff, including any YAML reserialization. Retries are byte-idempotent and never commit.
Existing authored vLLM targets and explicit verification/evidence are not overwritten.
Multi-node recipes and blocked sources do not receive companions.

**Read-only:** `render.py --dry-run`, validation, drift/CI, and
`prepare_companions.py --check` never write inputs. Missing materialization fails
with the local render command. Submit reviewed inputs **and** outputs together.

## Version and serving inputs

The default reuses the source's effective image and exact resolved engine: an
image-bound declaration or maintained release mapping. No tag guessing, latest,
patch carry-forward, or automatic upgrade. See [engine metadata](engine-versions.md).
Numeric vLLM platform versions use the `v` prefix for existing constraint rules.

For an explicitly newer comparable release, author overrides with fully qualified
`image`, `image_usage`, and image-bound `engine`/HTTPS provenance, then optionally run:

```bash
python3 tools/prepare_companions.py models/<model>/recipes/<recipe>/recipe.yaml \
  --version <newer-engine-version> --overrides platforms/vllm-newer.yaml
```

Keep checkpoint identity in `serving.model`; declare local weights separately:

```yaml
serving:
  model: example/exact-checkpoint
  served_model_name: example-alias
  shared_memory: {size: 16Gi}
  probes:
    startup: {path: /health, port: 8000, failure_threshold: 180, period_seconds: 10}
    readiness: {path: /v1/models, port: 8000, period_seconds: 10}
deployment:
  scope: single-node
  storage:
    type: pvc
    pvc: {name: example-weights, mount_path: /mnt/models, read_only: true}
```

Optional `pvc.model_path` defaults to the mount and must stay inside it; mounts must
not mask runtime/cache paths. Claim name/class/mode/capacity are deployment-selected;
no PVC/downloader is created. Use **raw JSON**, not shell-prequoted args. TP RHOAI
and Deployment honor alias/probes/shm/local paths; legacy defaults remain unchanged.
The template's `hf-token` Secret and hardware/runtime prerequisites still apply.

## Mapping and verification

- Scope overlays with `platforms[].config`; `null` disables them. Omission retains
  legacy shared `config_overrides`. Generated companions always use `config: null`.
- Automatic scoped overlays must preserve runtime/object identity; only
  `kubernetes.io/description` may differ. Unscoped, remote, extra-resource,
  generator, unknown admission/security/runtime patches are blocked, not translated.
- Move reviewed alias/image/probe/PVC/shm settings into declarations and remove
  superseded active patches. Keep source history; do not apply RHOAI patches to Deployment.
- Unresolved engines, multiple missing sources, pins, PP/router/P-D, unsupported
  GPU/runtime/topology/resources, capacity or env/parallelism conflicts block mapping.
  Explicitly author/review a counterpart (own pin if needed); renderer limits still apply.

Fresh `platforms[].verification` is **day-zero / needs-verification / empty benchmarks**.
Absent assessment means unknown, never inheritance from recipe/source maturity or evidence.
Explicit platform `validated`/`production` requires verified deployment (date/method)
and indexed benchmark refs with matching `run.platform` stack/version, recipe, scope,
and hardware. Generation or a checksum is not deployment/benchmark verification.

Source/version/override drift requires review, never automatic reset or promotion.
New `companion.source_config_sha256` records source inputs, excluding assessments.
Legacy records without it remain unchanged; review their shared-input edits explicitly.
