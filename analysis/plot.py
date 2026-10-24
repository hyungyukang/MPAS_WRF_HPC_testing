#!/usr/bin/env python3
"""Draw figures from results/summary.csv (run collect.py first).

  <model>_scaling.png       median time vs nodes, one line per build
                            (best layout at each node count), ideal scaling dashed
  <model>_layouts_N<n>.png  heatmap build x (ranks/node x threads) at n nodes

Needs matplotlib (it can also be run on a laptop with a copy of summary.csv):
  plot.py [--results DIR] [--max-series 8]
"""

import argparse
import csv
import math
import os
import sys
from collections import defaultdict

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'lib'))

MAIN = {'mpas': ('time_integration', 'MPAS-A time integration'),
        'wrf': ('total_main', 'WRF main-loop time')}

# Reference palette (dataviz skill, light mode): fixed categorical order
SERIES = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100',
          '#e87ba4', '#008300', '#6250d6', '#e34948']
SEQ = ['#cde2fb', '#b7d3f6', '#9ec5f4', '#86b6ef', '#6da7ec', '#5598e7',
       '#3987e5', '#2a78d6', '#256abf', '#1c5cab', '#184f95', '#104281', '#0d366b']
SURFACE, INK, INK2, GRID = '#fcfcfb', '#0b0b0b', '#52514e', '#e1e0d9'

plt.rcParams.update({
    'figure.facecolor': SURFACE, 'axes.facecolor': SURFACE,
    'axes.edgecolor': GRID, 'axes.labelcolor': INK2, 'text.color': INK,
    'xtick.color': INK2, 'ytick.color': INK2, 'axes.grid': True,
    'grid.color': GRID, 'grid.linewidth': 0.6, 'axes.spines.top': False,
    'axes.spines.right': False, 'font.size': 9, 'legend.frameon': False,
})


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(path):
    with open(path) as fp:
        rows = list(csv.DictReader(fp))
    for r in rows:
        for k in ('nodes', 'ranks_per_node', 'threads'):
            r[k] = int(float(r[k]))
    return rows


def scaling(rows, model, metric, title, outdir, max_series):
    col = f'{metric}_median'
    best = defaultdict(dict)        # build -> nodes -> (time, layout)
    for r in rows:
        t = num(r.get(col))
        if r['model'] != model or t is None:
            continue
        cur = best[r['build']].get(r['nodes'])
        if cur is None or t < cur[0]:
            best[r['build']][r['nodes']] = (t, f"{r['ranks_per_node']}x{r['threads']}")
    if not best:
        return None
    # keep the max_series builds that are fastest at their largest node count
    rank = sorted(best, key=lambda b: best[b][max(best[b])][0])
    shown = rank[:max_series]
    fig, ax = plt.subplots(figsize=(7.5, 4.6))
    allnodes = sorted({n for b in shown for n in best[b]})
    for i, b in enumerate(shown):
        pts = sorted(best[b].items())
        xs, ys = [p[0] for p in pts], [p[1][0] for p in pts]
        ax.plot(xs, ys, color=SERIES[i], lw=2, marker='o', ms=5,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=b)
        if len(shown) <= 4:
            ax.annotate(b, (xs[-1], ys[-1]), xytext=(6, 0), textcoords='offset points',
                        va='center', fontsize=8, color=INK2)
    # ideal scaling from the overall fastest build's first point
    ref = best[shown[0]]
    n0 = min(ref)
    t0 = ref[n0][0]
    ax.plot(allnodes, [t0 * n0 / n for n in allnodes], ls='--', lw=1, color=INK2,
            label='ideal (from fastest build)')
    ax.set_xscale('log', base=2)
    ax.set_yscale('log')
    ax.set_xticks(allnodes)
    ax.set_xticklabels([str(n) for n in allnodes])
    ax.set_xlabel('nodes')
    ax.set_ylabel(f'{metric} median (s, log)')
    hidden = len(rank) - len(shown)
    ax.set_title(f'{title}: strong scaling (best layout per point)'
                 + (f' - {hidden} slower builds hidden' if hidden else ''),
                 loc='left', fontsize=10, color=INK)
    ax.legend(loc='upper left', bbox_to_anchor=(1.02, 1), fontsize=8)
    fig.tight_layout()
    path = os.path.join(outdir, f'{model}_scaling.png')
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def heatmaps(rows, model, metric, title, outdir):
    col = f'{metric}_median'
    paths = []
    for nodes in sorted({r['nodes'] for r in rows if r['model'] == model}):
        cells = {}
        for r in rows:
            t = num(r.get(col))
            if r['model'] == model and r['nodes'] == nodes and t is not None:
                cells[(r['build'], (r['ranks_per_node'], r['threads']))] = t
        if not cells:
            continue
        builds = sorted({b for b, _ in cells},
                        key=lambda b: min(v for (bb, _), v in cells.items() if bb == b))
        lays = sorted({l for _, l in cells}, key=lambda l: (l[1], -l[0]))
        lo, hi = min(cells.values()), max(cells.values())
        fig, ax = plt.subplots(figsize=(max(6.0, 2.6 + 1.05 * len(lays)), 1.2 + 0.38 * len(builds)))
        ax.grid(False)
        for i, b in enumerate(builds):
            for j, l in enumerate(lays):
                v = cells.get((b, l))
                if v is None:
                    ax.add_patch(plt.Rectangle((j, i), 1, 1, fc=SURFACE, ec=GRID, lw=1))
                    ax.text(j + .5, i + .5, '-', ha='center', va='center', color=INK2, fontsize=8)
                    continue
                frac = 0 if hi == lo else (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo))
                k = round(frac * (len(SEQ) - 1))
                ax.add_patch(plt.Rectangle((j, i), 1, 1, fc=SEQ[k], ec=SURFACE, lw=2))
                ax.text(j + .5, i + .5, f'{v:.4g}', ha='center', va='center', fontsize=8,
                        color='white' if k >= 6 else INK,
                        fontweight='bold' if v == lo else 'normal')
        ax.set_xlim(0, len(lays))
        ax.set_ylim(len(builds), 0)
        ax.set_xticks([j + .5 for j in range(len(lays))])
        ax.set_xticklabels([f'{a}x{b}' for a, b in lays])
        ax.set_yticks([i + .5 for i in range(len(builds))])
        ax.set_yticklabels(builds, fontsize=8)
        ax.set_xlabel('MPI ranks per node x OpenMP threads')
        ax.tick_params(length=0)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_title(f'{title}, {nodes} node(s): median seconds\n'
                     f'darker = slower, bold = fastest, - = not run',
                     loc='left', fontsize=9, color=INK)
        fig.tight_layout()
        path = os.path.join(outdir, f'{model}_layouts_N{nodes}.png')
        fig.savefig(path, dpi=150)
        plt.close(fig)
        paths.append(path)
    return paths


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--site', help='site profile (config/sites/<SITE>); default: auto')
    ap.add_argument('--results', help='results directory (default: site.yaml results.root)')
    ap.add_argument('--max-series', type=int, default=8)
    args = ap.parse_args()
    if args.site:
        os.environ['PERFSONDE_SITE_NAME'] = args.site
    if args.results:
        rdir = args.results
    else:
        from perfsonde import site
        rdir = site.results_root()
    path = os.path.join(rdir, 'summary.csv')
    if not os.path.exists(path):
        sys.exit(f'{path} not found: run analysis/collect.py first')
    rows = load(path)
    outdir = os.path.join(rdir, 'figures')
    os.makedirs(outdir, exist_ok=True)
    made = []
    for model, (metric, title) in MAIN.items():
        p = scaling(rows, model, metric, title, outdir, min(args.max_series, len(SERIES)))
        if p:
            made.append(p)
        made += heatmaps(rows, model, metric, title, outdir)
    for p in made:
        print(p)
    if not made:
        print('nothing to plot yet')


if __name__ == '__main__':
    main()
