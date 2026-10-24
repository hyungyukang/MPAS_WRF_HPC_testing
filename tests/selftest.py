"""perfsonde self-test: checks the ReFrame install, the config and the log parsers
on the 'local' partition (current node, no Slurm, a few seconds).

  ./perfsonde selftest
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'lib'))

import reframe as rfm                      # noqa: E402
import reframe.utility.sanity as sn        # noqa: E402
from reframe.core.builtins import (parameter, run_after,  # noqa: E402
                                   run_before, sanity_function)

from perfsonde import parsers, site  # noqa: E402

DATA = os.path.join(_ROOT, 'tests', 'data')


@rfm.simple_test
class SondeSelfTest(rfm.RunOnlyRegressionTest):
    kind = parameter(['mpas', 'wrf'])
    valid_prog_environs = ['sonde']
    executable = 'cp'
    tags = {'selftest'}

    @run_after('init')
    def set_system(self):
        self.valid_systems = [f"{site.site()['system']['name']}:local"]
        self.logfile = {'mpas': 'log.atmosphere.0000.out', 'wrf': 'rsl.error.0000'}[self.kind]
        self.executable_opts = [os.path.join(DATA, self.logfile), '.']

    @sanity_function
    def found(self):
        pat = {'mpas': r'timer_name', 'wrf': r'SUCCESS COMPLETE WRF'}[self.kind]
        return sn.assert_found(pat, self.logfile)

    @run_before('performance')
    def set_perf(self):
        if self.kind == 'mpas':
            get = sn.deferrable(parsers.mpas_timer_total)
            self.perf_variables = {
                'time_integration': sn.make_performance_function(
                    get(os.path.join(self.stagedir, self.logfile), 'time integration'), 's')}
            self.reference = {'*': {'time_integration': (370.9876, -0.001, 0.001, 's')}}
        else:
            get = sn.deferrable(parsers.wrf_metric)
            self.perf_variables = {
                'mean_step': sn.make_performance_function(
                    get(os.path.join(self.stagedir, self.logfile), 'mean_step', 1, 1), 's')}
            self.reference = {'*': {'mean_step': (0.72017, -0.001, 0.001, 's')}}
