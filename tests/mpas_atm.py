"""MPAS-Atmosphere run matrix: builds x nodes x (ranks/node, threads) x repeats.

All values come from the site matrix.yaml (config/sites/<site>/, section 'mpas') and from the builds
that finished successfully under build.root (see build/build_matrix.py).
"""

import os
import shlex
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'lib'))

import reframe as rfm                      # noqa: E402
import reframe.utility.sanity as sn        # noqa: E402
from reframe.core.builtins import parameter  # noqa: E402

from perfsonde import base, builds, parsers, site  # noqa: E402

_M = base.model_matrix('mpas')


@rfm.simple_test
class MPASAtm(base.SondeRunBase):
    model = 'mpas'
    build = parameter(builds.available('mpas', _M.get('builds')))
    nodes = parameter([int(n) for n in _M.get('nodes', [1])])
    layout = parameter(base.layouts_for(_M) if _M else [], fmt=base.fmt_layout)
    rep = parameter(base.repeats_for(_M) if _M else [0])
    keep_files = ['log.atmosphere.0000.out', 'namelist.atmosphere',
                  'streams.atmosphere']

    def ps_stage(self):
        self.ps_stage_inputs(
            'namelist.atmosphere',
            self.ps_cfg.get('link_from_build',
                            ['src/core_atmosphere/physics/physics_wrf/files/*']),
        )

    def ps_prerun(self):
        """Make sure the METIS partition file for this rank count exists."""
        graph = self.ps_cfg.get('graph_file')
        if not graph:
            return []
        ntasks = self.layout[0] * self.nodes
        if ntasks == 1:
            return []          # a single task needs no partition file
        case = site.path(self.ps_cfg['case_dir'])
        cache = site.path(self.ps_cfg.get('partition_cache',
                                          os.path.join(site.build_root(), 'mpas_partitions')))
        q = shlex.quote
        part = f'{cache}/{graph}.part.{ntasks}'
        gpmetis = self.ps_cfg.get('gpmetis_cmd', 'gpmetis -minconn -contig -niter=200')
        metis_mod = self.ps_cfg.get('metis_module') or site.tool_module('metis', 'metis')
        # an empty tools.metis means gpmetis is already on PATH
        metis_load = f'module load {metis_mod} && ' if metis_mod else ''
        return [
            f'mkdir -p {q(cache)}',
            f'[ -e {q(cache + "/" + graph)} ] || ln -sf {q(case + "/" + graph)} {q(cache + "/" + graph)}',
            f'if [ ! -s {q(part)} ] && [ ! -s {q(case + "/" + graph + ".part." + str(ntasks))} ]; then '
            f'( {metis_load}cd {q(cache)} && '
            f'flock {q(graph)}.lock -c "[ -s {graph}.part.{ntasks} ] || {gpmetis} {graph} {ntasks}" ); fi',
            f'[ -s {q(graph)}.part.{ntasks} ] || ln -sf {q(part)} .',
        ]

    def ps_sanity(self):
        log = os.path.join(self.stagedir, 'log.atmosphere.0000.out')
        return sn.all([
            sn.assert_found(r'timer_name', log),
            sn.assert_not_found(r'CRITICAL ERROR', log),
        ])

    def ps_perf_variables(self):
        log = os.path.join(self.stagedir, 'log.atmosphere.0000.out')
        timers = {'total_time': 'total time',
                  'time_integration': 'time integration'}
        for t in self.ps_cfg.get('extra_timers', []):
            timers[t.strip().replace(' ', '_')] = t
        get = sn.deferrable(parsers.mpas_timer_total)
        return {var: sn.make_performance_function(get(log, name), 's')
                for var, name in timers.items()}
