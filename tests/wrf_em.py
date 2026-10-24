"""WRF (em_real) run matrix: builds x nodes x (ranks/node, threads) x repeats.

All values come from the site matrix.yaml (config/sites/<site>/, section 'wrf') and from the builds
that finished successfully under build.root (see build/build_matrix.py).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'lib'))

import reframe as rfm                      # noqa: E402
import reframe.utility.sanity as sn        # noqa: E402
from reframe.core.builtins import parameter  # noqa: E402

from perfsonde import base, builds, parsers  # noqa: E402

_M = base.model_matrix('wrf')


@rfm.simple_test
class WRFEm(base.SondeRunBase):
    model = 'wrf'
    build = parameter(builds.available('wrf', _M.get('builds')))
    nodes = parameter([int(n) for n in _M.get('nodes', [1])])
    layout = parameter(base.layouts_for(_M) if _M else [], fmt=base.fmt_layout)
    rep = parameter(base.repeats_for(_M) if _M else [0])
    keep_files = ['rsl.error.0000', 'namelist.input']

    def ps_stage(self):
        self.ps_stage_inputs(
            'namelist.input',
            self.ps_cfg.get('link_from_build', ['run/*']),
            exclude=('namelist.input', 'wrf.exe', 'real.exe', 'ndown.exe',
                     'tc.exe', 'README.namelist'),
        )

    def ps_sanity(self):
        rsl = os.path.join(self.stagedir, 'rsl.error.0000')
        return sn.assert_found(r'SUCCESS COMPLETE WRF', rsl)

    def ps_perf_variables(self):
        rsl = os.path.join(self.stagedir, 'rsl.error.0000')
        skip = int(self.ps_cfg.get('skip_steps', 1))
        dom = int(self.ps_cfg.get('domain', 1))
        get = sn.deferrable(parsers.wrf_metric)
        return {m: sn.make_performance_function(get(rsl, m, dom, skip), 's')
                for m in ('mean_step', 'median_step', 'total_main',
                          'total_write', 'total_input')}
