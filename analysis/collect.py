#!/usr/bin/env python3
"""Summarise results/results.jsonl (written by the ReFrame tests).

Outputs (in results/ by default):
  runs.csv      one row per run, every metric as a column
  summary.csv   one row per (model, build, nodes, ranks/node, threads):
                n, median/min/max/CV of each metric, speed-up and parallel
                efficiency vs the smallest node count of the same build/layout
and prints, per model and node count, the fastest configurations.

Only the Python standard library is used, so it runs on the HPC as is.
  collect.py [--results DIR] [--sacct] [--top N]
"""

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'lib'))

# The metric used to rank and to compute speed-up, per model.
MAIN = {'mpas': 'time_integration', 'wrf': 'total_main', 'osu': None}
KEY = ('model', 'build', 'nodes', 'ranks_per_node', 'threads')


def load(path):
    rows = []
    with open(path) as fp:
        for n, line in enumerate(fp, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                print(f'warning: {path}:{n}: bad JSON line skipped', file=sys.stderr)
    return rows


def sacct(jobids):
    """{jobid: {Elapsed, MaxRSS, ConsumedEnergyRaw}} for the batch steps."""
    ids = sorted({str(j) for j in jobids if j})
    if not ids:
        return {}
    cmd = ['sacct', '-n', '-P', '-j', ','.join(ids),
           '--format=JobID,ElapsedRaw,MaxRSS,ConsumedEnergyRaw,State']
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'warning: sacct failed: {exc}', file=sys.stderr)
        return {}
    info = defaultdict(dict)
    for line in out.splitlines():
        f = line.split('|')
        if len(f) < 5:
            continue
        jid, step = f[0].split('.')[0], f[0]
        rec = info[jid]
        if step == jid:
            rec['sacct_elapsed_s'] = f[1]
            rec['sacct_state'] = f[4]
            rec['sacct_energy_j'] = f[3]
        if f[2]:      # MaxRSS is reported on steps; keep the largest
            rec['sacct_maxrss'] = max(rec.get('sacct_maxrss', ''), f[2], key=_rss_bytes)
    return info


def _rss_bytes(s):
    if not s:
        return 0
    mult = {'K': 1 << 10, 'M': 1 << 20, 'G': 1 << 30, 'T': 1 << 40}
    try:
        return float(s[:-1]) * mult[s[-1]] if s[-1] in mult else float(s)
    except ValueError:
        return 0


def flatten(rows, extra):
    out = []
    for r in rows:
        f = {k: r.get(k) for k in ('time', 'model', 'build', 'nodes', 'ranks_per_node',
                                   'threads', 'ntasks', 'cores_used', 'rep', 'jobid',
                                   'nodelist', 'identical', 'test')}
        for k, v in (r.get('labels') or {}).items():
            f[f'label_{k}'] = v
        for k, v in (r.get('perf') or {}).items():
            f[k] = v
        f.update(extra.get(str(r.get('jobid')), {}))
        out.append(f)
    return out


def write_csv(path, rows):
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(path, 'w', newline='') as fp:
        w = csv.DictWriter(fp, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def summarise(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r.get(k) for k in KEY)].append(r)
    summary = []
    for key, rs in groups.items():
        s = dict(zip(KEY, key))
        s['n'] = len(rs)
        for k, v in rs[0].items():
            if k.startswith('label_'):
                s[k] = v
        metrics = sorted({m for r in rs for m in (r.get('_perf') or {})})
        for m in metrics:
            vals = [r['_perf'][m] for r in rs if m in (r.get('_perf') or {})
                    and isinstance(r['_perf'][m], (int, float))]
            if not vals:
                continue
            med = statistics.median(vals)
            s[f'{m}_median'] = round(med, 6)
            s[f'{m}_min'] = round(min(vals), 6)
            s[f'{m}_max'] = round(max(vals), 6)
            if len(vals) > 1 and med:
                s[f'{m}_cv_pct'] = round(100 * statistics.stdev(vals) / statistics.fmean(vals), 2)
        ident = [r.get('identical') for r in rs if r.get('identical') is not None]
        if ident:
            s['identical'] = all(ident)
        summary.append(s)

    # speed-up / efficiency vs the smallest node count of the same build+layout
    for model, metric in MAIN.items():
        if not metric:
            continue
        col = f'{metric}_median'
        base = {}
        for s in summary:
            if s['model'] != model or col not in s:
                continue
            k = (s['build'], s['ranks_per_node'], s['threads'])
            if k not in base or s['nodes'] < base[k]['nodes']:
                base[k] = s
        for s in summary:
            k = (s['build'], s['ranks_per_node'], s['threads'])
            if s['model'] == model and col in s and k in base:
                b = base[k]
                sp = b[col] / s[col] if s[col] else float('nan')
                s['speedup'] = round(sp, 3)
                s['efficiency'] = round(sp * b['nodes'] / s['nodes'], 3)
                s['speedup_base_nodes'] = b['nodes']
    summary.sort(key=lambda s: tuple(str(s.get(k)) if k in ('model', 'build')
                                     else (s.get(k) or 0) for k in KEY))
    return summary


def report(summary, top):
    osu = [s for s in summary if s['model'] == 'osu']
    if osu:
        print('\n=== OSU (median) ===')
        for s in osu:
            vals = ', '.join(f'{k[:-7]}={v:g}' for k, v in s.items() if k.endswith('_median'))
            print(f'  {s["build"]:14s} nodes={s["nodes"]} ranks/node={s["ranks_per_node"]}: {vals}')
    for model, metric in MAIN.items():
        col = f'{metric}_median' if metric else None
        rows = [s for s in summary if s['model'] == model and col and col in s]
        if not rows:
            continue
        print(f'\n=== {model}: fastest by median {metric} (s) ===')
        for nodes in sorted({s['nodes'] for s in rows}):
            print(f'-- nodes={nodes}')
            best = sorted((s for s in rows if s['nodes'] == nodes), key=lambda s: s[col])[:top]
            for i, s in enumerate(best, 1):
                cv = s.get(f'{metric}_cv_pct', '')
                ident = {True: 'same', False: 'DIFF'}.get(s.get('identical'), '')
                print(f'  {i:2d}. {s[col]:10.3f}  {s["build"]:36s} '
                      f'{s["ranks_per_node"]:>4}x{s["threads"]:<2} n={s["n"]}'
                      + (f' cv={cv}%' if cv != '' else '')
                      + (f' eff={s["efficiency"]:.2f}' if 'efficiency' in s else '')
                      + (f' out={ident}' if ident else ''))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--site', help='site profile (config/sites/<SITE>); default: auto')
    ap.add_argument('--results', help='results directory (default: site.yaml results.root)')
    ap.add_argument('--sacct', action='store_true', help='add sacct elapsed/MaxRSS/energy')
    ap.add_argument('--top', type=int, default=5)
    args = ap.parse_args()
    if args.site:
        os.environ['PERFSONDE_SITE_NAME'] = args.site
    if args.results:
        rdir = args.results
    else:
        from perfsonde import site
        rdir = site.results_root()
    src = os.path.join(rdir, 'results.jsonl')
    if not os.path.exists(src):
        sys.exit(f'no results yet: {src}')
    rows = load(src)
    extra = sacct([r.get('jobid') for r in rows]) if args.sacct else {}
    flat = flatten(rows, extra)
    write_csv(os.path.join(rdir, 'runs.csv'), flat)
    for r, f in zip(rows, flat):
        f['_perf'] = r.get('perf') or {}
    summary = summarise(flat)
    for f in flat:
        f.pop('_perf', None)
    write_csv(os.path.join(rdir, 'summary.csv'), summary)
    print(f'{len(rows)} runs -> {rdir}/runs.csv, {len(summary)} groups -> {rdir}/summary.csv')
    report(summary, args.top)


if __name__ == '__main__':
    main()
