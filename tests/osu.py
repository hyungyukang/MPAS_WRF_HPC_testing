"""Interconnect baseline with OSU Micro-Benchmarks (module osu-micro-benchmarks).

  OSUPt2Pt     : osu_latency / osu_bw between 2 nodes, 1 rank per node
  OSUAllreduce : osu_allreduce on N nodes, full nodes

Settings come from the site's matrix.yaml (section 'osu'); modules from
site.yaml tools.osu.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'lib'))

import reframe as rfm                      # noqa: E402
import reframe.utility.sanity as sn        # noqa: E402
from reframe.core.builtins import (parameter, run_after,  # noqa: E402
                                   run_before, sanity_function, variable)

from perfsonde import base, site  # noqa: E402

_M = base.model_matrix('osu')

# Locate an OSU binary: explicit bin_dir, PATH, or <prefix>/libexec/... found
# through any OSU*_ROOT / OSU*_DIR variable that the module sets.
_FIND = r'''ps_osu_find() {
  local name=$1 d
  if [ -n "${PS_OSU_BIN_DIR:-}" ] && [ -x "$PS_OSU_BIN_DIR/$name" ]; then echo "$PS_OSU_BIN_DIR/$name"; return; fi
  if command -v "$name" >/dev/null 2>&1; then command -v "$name"; return; fi
  for d in $(env | grep -iE '^OSU[A-Z_]*_(ROOT|DIR|HOME|PREFIX)=' | cut -d= -f2-); do
    find "$d" -type f -name "$name" -perm -u+x 2>/dev/null | head -1; return
  done
}'''


class _OSUBase(rfm.RunOnlyRegressionTest):
    valid_prog_environs = ['sonde']
    exclusive_access = True

    @run_after('init')
    def osu_init(self):
        S = site.site()
        self.valid_systems = [f"{S['system']['name']}:compute"]
        self.tags = {'osu'}

    @run_before('run')
    def osu_setup(self):
        S = site.site()
        mods = _M.get('modules') or site.tool_module('osu') or ['osu-micro-benchmarks']
        if isinstance(mods, str):
            mods = mods.split()
        self.prerun_cmds = [
            S['system'].get('module_init', 'module reset'),
            f'module load {" ".join(mods)}',
            _FIND,
            f'OSU_EXE=$(ps_osu_find {self.bench})',
            'echo "OSU_EXE=$OSU_EXE"',
        ]
        if _M.get('bin_dir'):
            self.env_vars['PS_OSU_BIN_DIR'] = site.path(_M['bin_dir'])
        self.executable = '"$OSU_EXE"'
        self.job.launcher.options = list(S['slurm'].get('srun_options', []))

    @sanity_function
    def osu_ok(self):
        return sn.assert_found(r'^# OSU', self.stdout)

    def osu_size(self, size):
        return sn.extractsingle(rf'^\s*{size}\s+([\d.]+)', self.stdout, 1, float)

    @run_after('performance')
    def osu_record(self):
        base.append_result({
            'time': datetime.datetime.now().isoformat(timespec='seconds'),
            'test': type(self).__name__, 'model': 'osu',
            'build': self.bench, 'labels': {}, 'nodes': self.num_tasks // max(1, self.num_tasks_per_node or 1),
            'ranks_per_node': self.num_tasks_per_node, 'threads': 1,
            'ntasks': self.num_tasks, 'rep': getattr(self, 'rep', 0),
            'jobid': getattr(self.job, 'jobid', None),
            'perf': {k.split(':')[-1]: v[0] for k, v in self.perfvalues.items()},
            'units': {k.split(':')[-1]: v[4] for k, v in self.perfvalues.items()},
        })


@rfm.simple_test
class OSUPt2Pt(_OSUBase):
    bench = parameter(['osu_latency', 'osu_bw'] if _M else [])
    rep = parameter(base.repeats_for(_M) if _M else [0])
    num_tasks = 2
    num_tasks_per_node = 1

    @run_before('performance')
    def set_perf(self):
        unit = 'us' if self.bench == 'osu_latency' else 'MB/s'
        sizes = _M.get('pt2pt_sizes', [8, 1048576])
        self.perf_variables = {
            f'size_{s}': sn.make_performance_function(self.osu_size(s), unit)
            for s in sizes}


@rfm.simple_test
class OSUAllreduce(_OSUBase):
    bench = variable(str, value='osu_allreduce')
    nodes = parameter([int(n) for n in _M.get('allreduce_nodes', [])] if _M else [])
    rep = parameter(base.repeats_for(_M) if _M else [0])

    @run_after('init')
    def set_tasks(self):
        rpn = int(_M.get('allreduce_ranks_per_node',
                         site.site()['node']['cores_per_node']))
        self.num_tasks_per_node = rpn
        self.num_tasks = rpn * self.nodes

    @run_before('performance')
    def set_perf(self):
        sizes = _M.get('allreduce_sizes', [8, 65536, 1048576])
        self.perf_variables = {
            f'size_{s}': sn.make_performance_function(self.osu_size(s), 'us')
            for s in sizes}
