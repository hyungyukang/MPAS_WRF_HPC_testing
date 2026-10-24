"""ReFrame configuration for perfsonde, built from the current site profile
(config/sites/<site>/site.yaml; see lib/perfsonde/site.py for how the site is chosen).

Partitions
  compute : Slurm batch jobs launched with srun (the real benchmarks)
  local   : runs on the current node, no MPI launcher (self-test only)
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), 'lib'))

from perfsonde import site as _site  # noqa: E402

_S = _site.site()
_sys = _S['system']
_slurm = _S['slurm']
_node = _S['node']
_rfm = _S.get('reframe', {})

_RFM = _site.path(_rfm.get('dir', 'work/{site}/rfm'))
os.makedirs(_RFM, exist_ok=True)

_access = []
if _slurm.get('partition'):
    _access.append(f"--partition={_slurm['partition']}")
if _slurm.get('account'):
    _access.append(f"--account={_slurm['account']}")
if _slurm.get('qos'):
    _access.append(f"--qos={_slurm['qos']}")
_access += list(_slurm.get('extra_options', []))

site_configuration = {
    'systems': [
        {
            'name': _sys.get('name', _site.site_name()),
            'descr': _sys.get('descr', ''),
            'hostnames': ['.*'],          # perfsonde picks the site itself
            'modules_system': _sys.get('modules_system', 'nomod'),
            'stagedir': _site.path(_rfm.get('stage_dir', _RFM + '/stage')),
            'outputdir': _site.path(_rfm.get('output_dir', _RFM + '/output')),
            'partitions': [
                {
                    'name': 'compute',
                    'descr': 'Slurm compute nodes',
                    'scheduler': _slurm.get('scheduler', 'slurm'),
                    'launcher': 'srun',
                    'access': _access,
                    'environs': ['sonde'],
                    'max_jobs': int(_slurm.get('max_jobs', 8)),
                    'time_limit': _site.rfm_duration(_slurm.get('time_limit', '00:30:00')),
                    'processor': {
                        'num_cpus': _node['cores_per_node'] * _node.get('smt', 1),
                        'num_cpus_per_core': _node.get('smt', 1),
                    },
                },
                {
                    'name': 'local',
                    'descr': 'Current node, no launcher (perfsonde self-test)',
                    'scheduler': 'local',
                    'launcher': 'local',
                    'environs': ['sonde'],
                    'max_jobs': 4,
                },
            ],
        },
    ],
    'environments': [
        # Modules are loaded by each build's env.sh, so the ReFrame
        # environment itself is empty.
        {'name': 'sonde'},
    ],
    'logging': [
        {
            'level': 'debug',
            'handlers': [
                {'type': 'stream', 'name': 'stdout', 'level': 'info',
                 'format': '%(message)s'},
                {'type': 'file', 'name': os.path.join(_RFM, 'reframe.log'),
                 'level': 'debug2',
                 'format': '[%(asctime)s] %(levelname)s: %(check_info)s: %(message)s',
                 'append': False},
            ],
            'handlers_perflog': [
                {
                    'type': 'filelog',
                    'basedir': _site.path(_rfm.get('perflog_dir', _RFM + '/perflogs')),
                    'prefix': '%(check_system)s/%(check_partition)s',
                    'level': 'info',
                    'format': ('%(check_result)s|%(check_job_completion_time)s|'
                               '%(check_#ALL)s'),
                    'ignore_keys': [
                        'check_build_locally', 'check_build_time_limit',
                        'check_executable_opts', 'check_hashcode',
                        'check_keep_files', 'check_local', 'check_maintainers',
                        'check_max_pending_time', 'check_outputdir',
                        'check_prebuild_cmds', 'check_prefix',
                        'check_prerun_cmds', 'check_postbuild_cmds',
                        'check_postrun_cmds', 'check_readonly_files',
                        'check_sourcepath', 'check_sourcesdir',
                        'check_stagedir', 'check_strict_check', 'check_tags',
                        'check_time_limit', 'check_valid_prog_environs',
                        'check_valid_systems', 'check_variables',
                    ],
                    'format_perfvars': ('%(check_perf_value)s|'
                                        '%(check_perf_unit)s|'),
                    'append': True,
                },
            ],
        },
    ],
    'general': [
        {
            'check_search_path': [os.path.join(os.path.dirname(_HERE), 'tests')],
            'report_file': os.path.join(_RFM, 'reports', 'run-report-{sessionid}.json'),
        },
    ],
}
