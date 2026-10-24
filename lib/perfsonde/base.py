"""Common ReFrame machinery for the perfsonde run tests.

Subclasses must define the parameters ``build``, ``nodes``, ``layout`` and
``rep`` and the class attribute ``model`` ('mpas' or 'wrf'), and implement
``ps_perf_variables()`` / ``ps_sanity()``.
"""

import datetime
import fcntl
import glob
import json
import os
import re
import shlex

import reframe as rfm
import reframe.utility.sanity as sn
from reframe.core.builtins import run_after, run_before, sanity_function

from . import builds, namelist, site


# --------------------------------------------------------------------------
# Matrix helpers (evaluated at import time to build the parameter spaces)
# --------------------------------------------------------------------------

def model_matrix(model):
    try:
        return site.matrix().get(model) or {}
    except FileNotFoundError:
        return {}


def layouts_for(mcfg):
    """[(ranks_per_node, threads)] from either 'layouts' or 'threads_per_rank'."""
    if mcfg.get('layouts'):
        return [tuple(int(v) for v in lay) for lay in mcfg['layouts']]
    cores = int(site.site()['node']['cores_per_node'])
    return [(cores // int(t), int(t)) for t in mcfg.get('threads_per_rank', [1])]


def fmt_layout(lay):
    return f'{lay[0]}x{lay[1]}'


def repeats_for(mcfg):
    return list(range(int(mcfg.get('repeats', site.matrix().get('repeats', 1)))))


# --------------------------------------------------------------------------
# Base test
# --------------------------------------------------------------------------

class SondeRunBase(rfm.RunOnlyRegressionTest):
    """Run one build of one model on one (nodes, layout) point."""

    model = None
    ps_partition = 'compute'
    valid_prog_environs = ['sonde']
    exclusive_access = True
    maintainers = ['perfsonde']

    # ---------------- init: validity and descriptive fields ---------------
    @run_after('init')
    def ps_init(self):
        self.ps_site = site.site()
        self.ps_cfg = model_matrix(self.model)
        self.valid_systems = [f"{self.ps_site['system']['name']}:{self.ps_partition}"]
        self.manifest = builds.load_manifest(self.build)
        rpn, omp = self.layout
        cores = int(self.ps_site['node']['cores_per_node'])
        self.skip_if(rpn * omp > cores,
                     f'layout {rpn}x{omp} exceeds {cores} cores per node')
        self.skip_if(omp > 1 and not self.manifest.get('openmp'),
                     f'build {self.build} has no OpenMP; threads={omp}')
        self.tags = {self.model, self.build}
        self.descr = (f'{self.model} {self.build} nodes={self.nodes} '
                      f'ranks/node={rpn} threads={omp} rep={self.rep}')
        tl = self.ps_cfg.get('time_limit')
        if tl:
            self.time_limit = site.rfm_duration(tl)

    # ---------------- staging -------------------------------------------
    def ps_build_dir(self):
        return builds.build_dir(self.build)

    def ps_overrides(self):
        """Merge namelist/file overrides: global, then per build label."""
        nl = dict(self.ps_cfg.get('namelist', {}) or {})
        fp = {k: list(v) for k, v in (self.ps_cfg.get('file_patches') or {}).items()}
        for axis, by_label in (self.ps_cfg.get('by_build_label') or {}).items():
            extra = (by_label or {}).get(self.manifest['labels'].get(axis), {}) or {}
            nl.update(extra.get('namelist', {}) or {})
            for f, pats in (extra.get('file_patches') or {}).items():
                fp.setdefault(f, []).extend(pats)
        return nl, fp

    def ps_link(self, src):
        dst = os.path.join(self.stagedir, os.path.basename(src))
        if os.path.lexists(dst):
            os.remove(dst)
        os.symlink(src, dst)

    def ps_materialize(self, name):
        """Replace a staged symlink by a real copy so it can be edited."""
        p = os.path.join(self.stagedir, name)
        with open(p) as fp:
            text = fp.read()
        os.remove(p)
        return p, text

    def ps_stage_inputs(self, namelist_file, build_links, exclude=()):
        src = os.path.join(self.ps_build_dir(), 'src')
        for pat in build_links:
            for f in sorted(glob.glob(os.path.join(src, pat))):
                if os.path.basename(f) not in exclude:
                    self.ps_link(f)
        case = site.path(self.ps_cfg['case_dir'])
        if not os.path.isdir(case):
            raise FileNotFoundError(f'case_dir not found: {case}')
        for f in sorted(glob.glob(os.path.join(case, '*'))):
            self.ps_link(f)
        nl, patches = self.ps_overrides()
        if nl:
            p, text = self.ps_materialize(namelist_file)
            with open(p, 'w') as fp:
                fp.write(namelist.patch(text, nl))
        for fname, pats in patches.items():
            p, text = self.ps_materialize(fname)
            for pat, rep in pats:
                text = re.sub(pat, rep, text, flags=re.MULTILINE)
            with open(p, 'w') as fp:
                fp.write(text)

    # ---------------- job setup -----------------------------------------
    @run_before('run')
    def ps_setup_job(self):
        rpn, omp = self.layout
        smt = int(self.ps_site['node'].get('smt', 1))
        self.num_tasks_per_node = rpn
        self.num_tasks = rpn * self.nodes
        # with SMT on, --hint=nomultithread makes each of these CPUs a full core
        self.num_cpus_per_task = omp
        self.executable = os.path.join(self.ps_build_dir(), 'bin',
                                       builds.EXE[self.model])
        env = {'OMP_NUM_THREADS': str(omp), 'OMP_PLACES': 'cores',
               'OMP_PROC_BIND': 'close'}
        env.update({k: str(v) for k, v in (self.ps_cfg.get('env') or {}).items()})
        self.env_vars.update(env)

        launcher_opts = []
        if self.ps_partition == 'compute':
            launcher_opts += list(self.ps_site['slurm'].get('srun_options', []))
            launcher_opts += list(self.ps_cfg.get('srun_options', []))
            if smt > 1:
                self.job.options += ['--hint=nomultithread']

        pre = [f'source {shlex.quote(os.path.join(self.ps_build_dir(), "env.sh"))} || exit 1',
               'ulimit -s unlimited 2>/dev/null || true',
               'echo "PERFSONDE_NODES=$(scontrol show hostnames "${SLURM_JOB_NODELIST:-}" '
               '2>/dev/null | paste -sd, -)"']
        dsh = self.ps_cfg.get('darshan') or site.matrix().get('darshan')
        if dsh:
            dsh = dsh if isinstance(dsh, dict) else {}
            pre += [f'module load {dsh.get("module") or site.tool_module("darshan", "darshan-runtime")}',
                    'PS_DARSHAN_LIB=${PS_DARSHAN_LIB:-' +
                    dsh.get('lib', '$(dirname $(dirname $(command -v darshan-config)))'
                            '/lib/libdarshan.so') + '}',
                    'export DARSHAN_LOGFILE=$PWD/darshan.log',
                    'echo "PERFSONDE_DARSHAN_LIB=$PS_DARSHAN_LIB"']
            launcher_opts += ['--export=ALL,LD_PRELOAD=$PS_DARSHAN_LIB']
            self.keep_files = list(self.keep_files) + ['darshan.log']
        pre += self.ps_prerun()
        pre += list(self.ps_cfg.get('prerun_cmds', []))
        pre.append('PERFSONDE_T0=$(date +%s.%N)')     # timing starts right before srun
        self.prerun_cmds = pre

        post = ['awk -v a="$PERFSONDE_T0" -v b="$(date +%s.%N)" '
                "'BEGIN{printf \"PERFSONDE_WALL=%.3f\\n\", b-a}'"]
        ver = self.ps_cfg.get('verify') or {}
        if ver.get('reference') and ver.get('output'):
            post += [f'module load {ver.get("module") or site.tool_module("nccmp", "nccmp")}',
                     f'nccmp {ver.get("nccmp_opts", "-d -f")} '
                     f'{shlex.quote(site.path(ver["reference"]))} '
                     f'{ver["output"]} > nccmp.log 2>&1; '
                     'echo "PERFSONDE_NCCMP=$?"']
            self.keep_files = list(self.keep_files) + ['nccmp.log']
        self.postrun_cmds = post + list(self.ps_cfg.get('postrun_cmds', []))
        if launcher_opts:
            self.job.launcher.options = launcher_opts
        self.ps_stage()

    # hooks for subclasses
    def ps_prerun(self):
        return []

    def ps_stage(self):
        pass

    # ---------------- sanity / performance -------------------------------
    @sanity_function
    def ps_sanity_all(self):
        return sn.all([sn.assert_found(r'PERFSONDE_WALL=', self.stdout),
                       self.ps_sanity()])

    def ps_sanity(self):
        return True

    @run_before('performance')
    def ps_set_perf_vars(self):
        pv = {'wall': sn.make_performance_function(
            sn.extractsingle(r'PERFSONDE_WALL=([\d.]+)', self.stdout, 1, float), 's')}
        pv.update(self.ps_perf_variables())
        self.perf_variables = pv

    def ps_perf_variables(self):
        return {}

    # ---------------- results.jsonl -------------------------------------
    @run_after('performance')
    def ps_record(self):
        out = os.path.join(self.stagedir, self.stdout.evaluate()
                           if hasattr(self.stdout, 'evaluate') else self.stdout)
        try:
            with open(out, errors='replace') as fp:
                stdout = fp.read()
        except OSError:
            stdout = ''
        m = re.search(r'PERFSONDE_NCCMP=(\d+)', stdout)
        nodes = re.search(r'PERFSONDE_NODES=(\S*)', stdout)
        rpn, omp = self.layout
        rec = {
            'time': datetime.datetime.now().isoformat(timespec='seconds'),
            'test': type(self).__name__,
            'model': self.model,
            'build': self.build,
            'labels': self.manifest.get('labels', {}),
            'openmp_build': bool(self.manifest.get('openmp')),
            'modules': self.manifest.get('modules', []),
            'nodes': self.nodes,
            'ranks_per_node': rpn,
            'threads': omp,
            'ntasks': rpn * self.nodes,
            'cores_used': rpn * omp * self.nodes,
            'rep': self.rep,
            'system': self.current_system.name,
            'partition': self.current_partition.name,
            'jobid': getattr(self.job, 'jobid', None),
            'nodelist': (nodes.group(1) if nodes and nodes.group(1)
                         else ','.join(getattr(self.job, 'nodelist', None) or [])),
            'perf': {k.split(':')[-1]: v[0] for k, v in self.perfvalues.items()},
            'units': {k.split(':')[-1]: v[4] for k, v in self.perfvalues.items()},
            'identical': (m.group(1) == '0') if m else None,
            'outputdir': self.outputdir,
        }
        append_result(rec)


def append_result(rec):
    """Append one record to results/results.jsonl (safe for concurrent runs)."""
    rdir = site.results_root()
    os.makedirs(rdir, exist_ok=True)
    with open(os.path.join(rdir, 'results.jsonl'), 'a') as fp:
        fcntl.flock(fp, fcntl.LOCK_EX)
        fp.write(json.dumps(rec) + '\n')
        fcntl.flock(fp, fcntl.LOCK_UN)
