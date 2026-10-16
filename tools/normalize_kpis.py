#!/usr/bin/env python3
"""Normalize Forge KPIs locally; never infer deployment identity from labels."""
from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path


LOGGER = logging.getLogger(__name__)


GUIDELLM = {
    'playbooks_output_tok_per_sec': ('output_tok_s', 1),
    'playbooks_total_tok_per_sec': ('total_tok_s', 1),
    'playbooks_completed_requests': ('total_requests', 1),
    'playbooks_failed_requests': ('failed_requests', 1),
    'playbooks_measured_rps': ('rps', 1),
}
AIPERF = {
    'aiperf_output_token_throughput_avg': ('output_tok_s', 1),
    'aiperf_request_count_avg': ('total_requests', 1),
    'aiperf_request_throughput_avg': ('rps', 1),
}
for prefix in ('ttft', 'itl', 'tpot'):
    for statistic in ('median', 'p90', 'p95', 'p99', 'mean'):
        suffix = {'median': 'p50', 'mean': 'avg'}.get(statistic, statistic)
        GUIDELLM[f'playbooks_{prefix}_{statistic}'] = (f'{prefix}_ms_{suffix}', 1000)
for source, target in (('time_to_first_token', 'ttft'), ('inter_token_latency', 'itl'),
                       ('time_per_output_token', 'tpot')):
    for statistic in ('avg', 'p50', 'p90', 'p95', 'p99'):
        AIPERF[f'aiperf_{source}_{statistic}'] = (f'{target}_ms_{statistic}', 1)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f'invalid nonnegative finite metric: {value!r}')
    return value


def concurrency(value):
    if isinstance(value, bool):
        raise ValueError('boolean concurrency is invalid')
    value = number(float(value))
    rounded = int(math.floor(value + 0.5))
    if rounded < 1:
        raise ValueError('concurrency must round to a positive integer')
    return rounded


def normalize(document, *, run_id, deployment_scope, accelerator_key, expected_concurrencies=None):
    """Return one result per harness. Conflicting points must not be averaged."""
    groups = {}
    workloads = {}
    for test in document['tests']:
        labels = test.get('labels', {})
        harness = labels.get('benchmark_tool')
        if harness not in ('guidellm', 'aiperf'):
            continue
        points = groups.setdefault(harness, {})
        # Distinct workloads must not silently become a single sweep.
        identity = {k: labels[k] for k in ('benchmark_key', 'model_name', 'served_model', 'quantization', 'prompt_toks',
                    'output_toks', 'turns', 'deployment_profile') if labels.get(k)}
        if harness in workloads and workloads[harness] != identity:
            raise ValueError(f'{harness}: mixed workload attribution')
        workloads[harness] = identity
        mapping = GUIDELLM if harness == 'guidellm' else AIPERF
        for kpi in test['kpis']:
            metric = mapping.get(kpi['kpi_id'])
            if metric is None:
                continue  # Unknown KPIs remain intact in committed raw evidence.
            field, scale = metric
            if harness == 'guidellm':
                if kpi.get('is_curve') is not True:
                    raise ValueError('GuideLLM KPI must be a curve')
                values = kpi['values']
            else:
                if kpi.get('is_curve') is not False:
                    raise ValueError('AIPerf KPI must be scalar')
                values = [(labels['concurrency'], kpi['value'])]
            seen = set()
            for x, y in values:
                c = concurrency(x)
                if c in seen:
                    raise ValueError('duplicate or rounded-colliding concurrency')
                seen.add(c)
                point = points.setdefault(c, {'concurrency': c})
                value = None if y is None else number(number(y) * scale)
                if field in ('total_requests', 'failed_requests') and value is not None:
                    if not float(value).is_integer():
                        raise ValueError('request count must be integral')
                    value = int(value)
                if field in point:
                    raise ValueError(f'duplicate metric {harness}/{c}/{field}')
                point[field] = value
    results = {}
    for harness, points in groups.items():
        if not points:
            raise ValueError(f'{harness}: no recognized KPI points')
        sweep = [points[c] for c in sorted(points)]
        for point in sweep:
            for prefix in ('ttft', 'itl', 'tpot'):
                for percentile in ('p50', 'p90', 'p95', 'p99'):
                    point.setdefault(f'{prefix}_ms_{percentile}', None)
        key = workloads[harness].get('benchmark_key', '')
        completeness = 'smoke' if len(sweep) <= 2 or 'smoke' in key or key == 'ci-quick' else 'partial'
        if completeness != 'smoke' and expected_concurrencies is not None and harness in expected_concurrencies:
            expected = sorted(concurrency(c) for c in expected_concurrencies[harness])
            if len(set(expected)) != len(expected):
                raise ValueError('duplicate expected concurrency')
            if expected == [point['concurrency'] for point in sweep]:
                completeness = 'full-sweep'
        elif completeness != 'smoke' and expected_concurrencies is not None:
            LOGGER.warning('%s: expected_concurrencies has no harness entry; completeness remains partial', harness)
        # The artifact alone cannot prove the intended full sweep completed.
        results[harness] = {'schema_version': 2, 'run_id': f'{run_id}-{harness}',
            'deployment_scope': deployment_scope, 'accelerator_key': accelerator_key,
            'harness': harness, 'completeness': completeness,
            'reference_concurrency': sweep[-1]['concurrency'],
             'metrics': {k: v for k, v in sweep[-1].items() if k != 'concurrency'}, 'sweep': sweep}
        # The served_model marker identifies the new contract. Historical exact-label
        # results retain their original shape; all original labels also live in run.yaml.
        if 'served_model' in workloads[harness]:
            results[harness]['model_identity'] = {key: workloads[harness][key]
                for key in ('model_name', 'served_model', 'quantization') if key in workloads[harness]}
    if not results:
        raise ValueError('no supported benchmark harness found')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--deployment-scope', choices=['single-node', 'multi-node'], required=True)
    parser.add_argument('--accelerator-key', required=True)
    args = parser.parse_args()
    results = normalize(json.loads(args.input.read_text()), run_id=args.run_id,
                        deployment_scope=args.deployment_scope, accelerator_key=args.accelerator_key)
    for harness, result in results.items():
        directory = args.output / harness
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'result.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')


if __name__ == '__main__':
    main()
