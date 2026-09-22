# Schema changelog

## Recipe schema v3

- Makes `deployment.scope` the required, canonical node-scope field with
  values `single-node` or `multi-node`.
- Removes `match.nodes` as a supported catalog selector; catalog tooling derives
  node scope from `deployment.scope`.
- Requires explicit platform, hardware-profile, and workload-profile identity.
- Limits workload profiles to the reusable PR #7 benchmark workloads and adds
  deployment-mode plus free-form optimization-intent metadata. `latency` and
  `throughput` remain the standard catalog values.
- Restricts platform-version identifiers to safe path components for CI matrix
  generation.
- Adds references to benchmark run records for validated and production recipes.

## Supporting schemas v1

- Adds model metadata, corrigible hardware-profile, benchmark-run, and
  normalized benchmark-result schemas.
- Benchmark runs record the hardware-profile revision known when they ran.
- Benchmark runs must reference their normalized result; result run IDs resolve
  to exactly one run record.
