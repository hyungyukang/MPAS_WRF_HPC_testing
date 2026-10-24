#!/usr/bin/env python3
"""Probe this HPC system and write a PerfSonde site profile.

Run on a login node of the system (no internet needed):

  ./perfsonde discover [--name NAME] [--partition P] [--account A]
                    [--max-compilers 2] [--probe-job] [--force] [--dry-run]

What it does
  * module system : `module -t list`, `module -t avail`, `module -t -d avail`
                    (defaults) and `module display` - Lmod or Environment Modules
  * toolchains    : for each GNU compiler (Cray: PrgEnv-gnu + gcc/gcc-native),
                    load it in a clean subshell, look again at `module avail`
                    (hierarchies), pick MPI, HDF5, netCDF-C/Fortran, PnetCDF,
                    load everything, then record the resolved versions
                    (`module -t list`), compiler wrappers (ftn/cc vs mpif90/mpicc)
                    and how to find each library prefix (module variables seen
                    in `module display`, nc-config/nf-config/pnetcdf-config, or
                    the bin/ directory the module adds to PATH), checking that
                    the expected header files exist
  * helper tools  : metis (gpmetis), nccmp, darshan-runtime, osu-micro-benchmarks
  * Slurm         : sinfo, scontrol show node/config, sacctmgr (partitions,
                    usable cores incl. core specialisation, SMT, accounting,
                    accounts); --probe-job runs a 1-node job to measure nproc

Output: config/sites/<NAME>/{site,builds,matrix}.yaml, discovery.md, discovery.json
Values it is unsure about are marked "TODO" in the YAML files and listed in
discovery.md. Only the Python standard library is required.
"""

import argparse
import datetime
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from subprocess import PIPE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = '@@PS@@'

BOOT = r'''
if ! type module >/dev/null 2>&1; then
  for f in /etc/profile.d/lmod.sh /usr/share/lmod/lmod/init/bash \
           /etc/profile.d/modules.sh /usr/share/Modules/init/bash \
           ${LMOD_PKG:+$LMOD_PKG/init/bash} ${MODULESHOME:+$MODULESHOME/init/bash}; do
    [ -f "$f" ] && . "$f" >/dev/null 2>&1 && type module >/dev/null 2>&1 && break
  done
fi
'''

MPI_FAMS = ['cray-mpich', 'mpich', 'openmpi', 'mvapich2', 'mvapich', 'hpcx-mpi',
            'intel-mpi', 'impi']
HDF5_FAMS = ['cray-hdf5', 'hdf5', 'hdf5-mpi', 'phdf5', 'cray-hdf5-parallel', 'hdf5-parallel']
PNETCDF_FAMS = ['cray-parallel-netcdf', 'parallel-netcdf', 'pnetcdf', 'parallelnetcdf']
TOOL_FAMS = {
    'metis': ['metis'],
    'nccmp': ['nccmp'],
    'darshan': ['darshan-runtime', 'darshan'],
    'osu': ['osu-micro-benchmarks', 'osu-benchmarks', 'osu'],
}
AVOID = ('serial', 'debug', 'dbg')
HEADERS = {'netcdf_c': ['include/netcdf.h'],
           'netcdf_f': ['include/netcdf.mod', 'include/NETCDF.mod', 'include/netcdf.inc'],
           'pnetcdf': ['include/pnetcdf.h'],
           'hdf5': ['include/hdf5.h']}
CONFIG_TOOL = {'netcdf_c': 'nc-config', 'netcdf_f': 'nf-config', 'pnetcdf': 'pnetcdf-config'}


# --------------------------------------------------------------------------
# shell / module helpers
# --------------------------------------------------------------------------

class Shell:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.login = False
        rc, out, _ = self._run(BOOT + 'type module >/dev/null 2>&1 && echo HAVE_MODULE')
        if 'HAVE_MODULE' not in out:
            self.login = True
            rc, out, _ = self._run(BOOT + 'type module >/dev/null 2>&1 && echo HAVE_MODULE')
            if 'HAVE_MODULE' not in out:
                raise SystemExit('discover: the `module` command is not available in bash '
                                 '(tried the usual Lmod / Environment Modules init files)')

    def _run(self, script, timeout=600):
        cmd = ['bash', '-lc' if self.login else '-c', script]
        if self.verbose:
            print('--- running:\n' + script, file=sys.stderr)
        try:
            p = subprocess.run(cmd, stdout=PIPE, stderr=PIPE, universal_newlines=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, '', 'timeout'
        return p.returncode, p.stdout, p.stderr

    def sections(self, body, timeout=600):
        """Run BOOT+body; split stdout on '@@PS@@ name' markers."""
        rc, out, err = self._run(BOOT + body, timeout)
        secs, cur = {'_pre': []}, '_pre'
        for line in out.splitlines():
            if line.startswith(MARK + ' '):
                cur = line[len(MARK) + 1:].strip()
                secs.setdefault(cur, [])
            else:
                secs.setdefault(cur, []).append(line)
        return {k: '\n'.join(v) for k, v in secs.items()}, rc, err


def sec(name):
    return f'echo "{MARK} {name}"'


def parse_terse(text):
    """Module names from `module -t avail|list` output (Lmod or Tcl)."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(MARK) or s.endswith(':') or s.endswith('/'):
            continue
        if ' ' in s and not re.match(r'^\S+\s*\(', s):
            continue      # messages, banners
        m = re.match(r'^([^\s()<>]+)', s)
        if m and not m.group(1).startswith('.'):
            name = m.group(1)
            if name not in out:
                out.append(name)
    return out


def family(name):
    return name.rsplit('/', 1)[0] if '/' in name else name


def version(name):
    return name.rsplit('/', 1)[1] if '/' in name else ''


def vkey(name):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', version(name))]


def pick(avail, defaults, fams, avoid=AVOID):
    """Best module among families *fams* (in preference order)."""
    for fam in fams:
        cands = [m for m in avail if family(m) == fam]
        if not cands:
            continue
        good = [m for m in cands if not any(a in version(m).lower() for a in avoid)] or cands
        dflt = [m for m in good if m in defaults]
        return dflt[0] if dflt else max(good, key=vkey)
    return None


def versions_of(avail, fam):
    return sorted([m for m in avail if family(m) == fam], key=vkey)


def parse_display(text):
    """setenv / prepend_path(PATH) entries from `module display`."""
    env, paths = {}, []
    for line in text.splitlines():
        m = re.match(r'\s*setenv\(\s*"([^"]+)"\s*,\s*"([^"]*)"', line) or \
            re.match(r'\s*setenv\s+(\S+)\s+(\S+)', line)
        if m:
            env[m.group(1)] = m.group(2)
            continue
        m = re.match(r'\s*(?:prepend_path|append_path)\(\s*"PATH"\s*,\s*"([^"]+)"', line) or \
            re.match(r'\s*(?:prepend-path|append-path)\s+PATH\s+(\S+)', line)
        if m:
            paths += m.group(1).split(':')
    return env, paths


def parse_env(text):
    env = {}
    for line in text.splitlines():
        if '=' in line and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', line):
            k, v = line.split('=', 1)
            env[k] = v
    return env


def has_headers(prefix, kind):
    return bool(prefix) and any(os.path.exists(os.path.join(prefix, h)) for h in HEADERS[kind])


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

class Discover:
    def __init__(self, args):
        self.args = args
        self.sh = Shell(args.verbose)
        self.warn = []
        self.todo = []
        self.info = {'generated': datetime.datetime.now().isoformat(timespec='seconds'),
                     'host': socket.getfqdn(), 'user': getpass.getuser()}

    # ---------------- module system ------------------------------------
    def probe(self, loads, final=False, display=()):
        body = [f'{self.init} >/dev/null 2>&1']
        for m in loads:
            body.append(f'module load {m} >/dev/null 2>&1 || echo "{MARK}FAIL {m}"')
        body += [sec('list'), 'module -t list 2>&1']
        if not final:
            body += [sec('avail'), 'module -t avail 2>&1',
                     sec('defaults'), 'module -t -d avail 2>&1 || module avail -t -d 2>&1']
        else:
            body += [sec('which')]
            for c in ('ftn', 'cc', 'CC', 'mpif90', 'mpifort', 'mpicc', 'mpicxx', 'mpic++',
                      'mpiCC', 'gfortran', 'gcc', 'g++', 'gpmetis', 'nccmp', 'srun'):
                body.append(f'printf "%s|%s\\n" {c} "$(command -v {c} 2>/dev/null)"')
            body += [sec('versions'),
                     'echo "gfortran|$(gfortran -dumpfullversion 2>/dev/null || gfortran -dumpversion 2>/dev/null)"',
                     'echo "gfortran_line|$(gfortran --version 2>/dev/null | head -1)"',
                     sec('config')]
            for t in ('nc-config', 'nf-config', 'pnetcdf-config'):
                body.append(f'command -v {t} >/dev/null 2>&1 && echo "{t}|$({t} --prefix 2>/dev/null)"')
            body += [sec('env'), 'env']
            for i, m in enumerate(display):
                body += [sec(f'display {i}'), f'module display {m} 2>&1']
        secs, rc, err = self.sh.sections('\n'.join(body) + '\n')
        fails = re.findall(MARK + r'FAIL (\S+)', '\n'.join(secs.values()))
        res = {'list': parse_terse(secs.get('list', '')), 'fail': fails}
        if not final:
            res['avail'] = parse_terse(secs.get('avail', ''))
            res['defaults'] = parse_terse(secs.get('defaults', ''))
        else:
            res['which'] = dict(l.split('|', 1) for l in secs.get('which', '').splitlines() if '|' in l)
            res['versions'] = dict(l.split('|', 1) for l in secs.get('versions', '').splitlines() if '|' in l)
            res['config'] = dict(l.split('|', 1) for l in secs.get('config', '').splitlines() if '|' in l)
            res['env'] = parse_env(secs.get('env', ''))
            res['display'] = [parse_display(secs.get(f'display {i}', '')) for i in range(len(display))]
        return res

    def module_system(self):
        secs, _, _ = self.sh.sections(
            sec('lmod') + '\necho "${LMOD_VERSION:-}"\n' + sec('modver') +
            '\nmodule --version 2>&1 | head -3\n' + sec('reset') +
            '\n(module reset >/dev/null 2>&1 && echo RESET_OK) || echo RESET_FAIL\n' +
            sec('list') + '\nmodule -t list 2>&1\n')
        lmod = secs.get('lmod', '').strip()
        kind = f'Lmod {lmod}' if lmod else (secs.get('modver', '').strip().splitlines() or ['unknown'])[0]
        self.init = 'module reset' if 'RESET_OK' in secs.get('reset', '') else 'module purge'
        if self.init == 'module purge':
            self.warn.append('`module reset` failed; using `module purge` as the clean start. '
                             'If purge removes modules the system needs (e.g. Slurm), set '
                             'system.module_init in site.yaml.')
        self.info['module_system'] = kind
        self.info['module_init'] = self.init
        self.info['initial_list'] = parse_terse(secs.get('list', ''))
        base = self.probe([])
        self.base = base
        self.info['clean_list'] = base['list']
        dflt_dar = [m for m in base['list'] if family(m) in ('darshan-runtime', 'darshan')]
        if dflt_dar:
            self.warn.append(f'{dflt_dar[0]} is loaded by default, so every build/run is traced by '
                             'Darshan. For clean timings consider system.module_init: '
                             f'"{self.init} && module unload {family(dflt_dar[0])}" in site.yaml.')
        self.info['avail_count'] = len(base['avail'])
        print(f'[modules] {kind}; clean start = "{self.init}"; {len(base["avail"])} modules visible')

    # ---------------- toolchains -----------------------------------------
    def compilers(self):
        av, df = self.base['avail'], self.base['defaults']
        fams = {family(m) for m in av}
        self.cray = 'PrgEnv-gnu' in fams
        n = self.args.max_compilers
        if self.cray:
            prg = pick(av, df, ['PrgEnv-gnu'])
            s = self.probe([prg])
            gccfams = ['gcc-native', 'gcc']
            loaded = next((m for m in s['list'] if family(m) in gccfams), None)
            alts = [m for f in gccfams for m in versions_of(s['avail'], f)]
            order = ([loaded] if loaded else []) + sorted(
                [m for m in alts if m != loaded], key=vkey, reverse=True)
            out = [[prg, c] for c in order[:n]] or [[prg]]
            print(f'[compiler] HPE Cray PE: {prg}; GNU versions {", ".join(alts) or "?"}')
            return out
        gcc = [m for f in ('gcc', 'gnu', 'gcc-native') for m in versions_of(av, f)]
        if not gcc:
            self.warn.append('no gcc module found: using the system compiler (no compiler module)')
            return [[]]
        dflt = pick(av, df, ['gcc', 'gnu', 'gcc-native'], avoid=())
        order = [dflt] + sorted([m for m in gcc if m != dflt], key=vkey, reverse=True)
        print(f'[compiler] GNU modules: {", ".join(gcc)}')
        return [[c] for c in order[:n]]

    def toolchain(self, base):
        tc = {'base': base, 'warn': []}
        s1 = self.probe(base)
        if s1['fail']:
            tc['error'] = f'cannot load {s1["fail"]}'
            return tc
        mpi = next((m for m in s1['list'] if family(m) in MPI_FAMS), None) or \
            pick(s1['avail'], s1['defaults'], MPI_FAMS)
        if not mpi:
            tc['warn'].append('no MPI module found')
        loads = base + ([mpi] if mpi and mpi not in base else [])
        s2 = self.probe(loads)
        av, df = s2['avail'], s2['defaults']
        fams = {family(m) for m in av}
        libs = {}
        libs['hdf5'] = pick(av, df, HDF5_FAMS)
        if 'cray-netcdf' in fams:
            libs['netcdf_c'] = libs['netcdf_f'] = pick(av, df, ['cray-netcdf'])
        elif 'netcdf-fortran' in fams:
            libs['netcdf_c'] = pick(av, df, ['netcdf-c', 'netcdf'])
            libs['netcdf_f'] = pick(av, df, ['netcdf-fortran'])
        else:
            libs['netcdf_c'] = libs['netcdf_f'] = pick(av, df, ['netcdf', 'netcdf-mpi', 'netcdf-c'])
        libs['pnetcdf'] = pick(av, df, PNETCDF_FAMS)
        for k, v in libs.items():
            if not v:
                tc['warn'].append(f'no module for {k}')
        lib_mods = []
        for k in ('hdf5', 'netcdf_c', 'netcdf_f', 'pnetcdf'):
            if libs.get(k) and libs[k] not in lib_mods:
                lib_mods.append(libs[k])
        tools = {k: pick(av, df, f) or pick(self.base['avail'], self.base['defaults'], f)
                 for k, f in TOOL_FAMS.items()}
        fin = self.probe(loads + lib_mods, final=True, display=lib_mods)
        for f in fin['fail']:
            tc['warn'].append(f'module load {f} failed; dropped')
        lib_mods = [m for m in lib_mods if m not in fin['fail']]
        # pin to the versions actually loaded
        loaded = fin['list']
        by_fam = {family(m): m for m in loaded}
        modules = [by_fam.get(family(m), m) for m in base]
        for m in [mpi] + lib_mods:
            if m and by_fam.get(family(m), m) not in modules:
                modules.append(by_fam.get(family(m), m))
        tc.update(mpi=mpi, libs=libs, tools=tools, modules=modules, loaded=loaded,
                  avail_after_mpi=av, defaults_after_mpi=df,
                  which={k: v for k, v in fin['which'].items() if v},
                  gfortran=fin['versions'].get('gfortran', ''),
                  gfortran_line=fin['versions'].get('gfortran_line', ''))
        # library prefixes
        disp = dict(zip(lib_mods, fin['display']))
        tc['prefix'] = {}
        for kind in ('netcdf_c', 'netcdf_f', 'pnetcdf', 'hdf5'):
            mod = libs.get(kind)
            tc['prefix'][kind] = self.find_prefix(kind, mod, disp.get(mod, ({}, [])), fin)
            if not tc['prefix'][kind]['expr'] and kind != 'hdf5':
                tc['warn'].append(f'could not find the {kind} install prefix')
        # WRF's PnetCDF I/O needs the Fortran interface, MPAS (SMIOL) only the C one
        pv = tc['prefix']['pnetcdf']['value']
        tc['pnetcdf_fortran'] = bool(pv) and any(
            os.path.exists(os.path.join(pv, 'include', f)) for f in ('pnetcdf.inc', 'pnetcdf.mod'))
        if pv and not tc['pnetcdf_fortran']:
            tc['warn'].append('PnetCDF has no Fortran interface (include/pnetcdf.inc): '
                              'WRF io_form=11 builds are left out')
        # compiler wrappers
        w = tc['which']
        cray_wrap = self.cray and 'ftn' in w and any(family(m) == 'craype' for m in loaded)
        if cray_wrap:
            tc['wrappers'] = {'fc': 'ftn', 'cc': 'cc', 'cxx': 'CC'}
        else:
            tc['wrappers'] = {'fc': 'mpif90' if 'mpif90' in w else ('mpifort' if 'mpifort' in w else 'mpif90'),
                              'cc': 'mpicc',
                              'cxx': next((c for c in ('mpicxx', 'mpic++', 'mpiCC') if c in w), 'mpicxx')}
            if 'mpif90' not in w and 'mpifort' not in w:
                tc['warn'].append('no MPI Fortran wrapper (mpif90/mpifort/ftn) found on PATH')
        # label from the compiler module version (gcc-native/14.2 -> gnu14), else gfortran
        cmod = next((m for m in base if family(m) in ('gcc', 'gnu', 'gcc-native')), None)
        maj = (version(cmod) if cmod else tc['gfortran'] or '').split('.')[0]
        tc['label'] = ('gnu' if self.cray else 'gcc') + (re.sub(r'\D', '', maj) or 'sys')
        return tc

    def find_prefix(self, kind, mod, display, fin):
        """Return {'expr', 'value', 'how'}: a bash expression for the prefix."""
        env = fin['env']
        denv, dpaths = display
        cands = []
        # 1. variables the module sets (e.g. NETCDF_DIR, NETCDF_FORTRAN_ROOT)
        names = sorted(denv, key=lambda v: (not re.search(r'(DIR|ROOT|PREFIX|HOME|PATH|BASE)$', v), v))
        for var in names:
            val = env.get(var, denv[var])
            if os.path.isdir(val):
                cands.append((f'${var}', val, f'module display: setenv {var}'))
        # 2. nc-config / nf-config / pnetcdf-config --prefix
        tool = CONFIG_TOOL.get(kind)
        if tool and fin['config'].get(tool):
            cands.append((f'$({tool} --prefix)', fin['config'][tool].strip(), f'{tool} --prefix'))
        # 3. bin/ directory the module adds to PATH
        for p in dpaths:
            if p.rstrip('/').endswith('/bin'):
                pre = os.path.dirname(p.rstrip('/'))
                cands.append((pre, pre, 'module display: PATH entry'))
        for expr, val, how in cands:
            if has_headers(val, kind):
                return {'expr': expr, 'value': val, 'how': how, 'module': mod}
        return {'expr': '', 'value': '', 'how': 'not found', 'module': mod}

    # ---------------- Slurm ---------------------------------------------
    def slurm(self):
        a = self.args
        sl = {'scheduler': 'slurm', 'partition': a.partition or '', 'account': a.account or ''}
        if not shutil.which('sinfo'):
            self.warn.append('sinfo not found: Slurm values left as TODO')
            self.todo.append('slurm.partition / account / node.cores_per_node (sinfo not found)')
            sl.update(cores=None, smt=1)
            return sl
        out = subprocess.run(['sinfo', '-h', '-o', '%P|%a|%l|%D|%c|%z'],
                             stdout=PIPE, stderr=PIPE, universal_newlines=True).stdout
        parts = []
        for line in out.splitlines():
            f = line.split('|')
            if len(f) < 6:
                continue
            name = f[0].rstrip('*')
            if any(p['name'] == name for p in parts):
                continue
            parts.append({'name': name, 'default': f[0].endswith('*'), 'avail': f[1],
                          'timelimit': f[2], 'nodes': f[3], 'cpus': f[4], 'sct': f[5]})
        sl['partitions'] = parts
        if not sl['partition']:
            d = [p for p in parts if p['default']] or parts
            sl['partition'] = d[0]['name'] if d else ''
            if not any(p['default'] for p in parts):
                self.todo.append('slurm.partition: no default partition; picked the first one')
        # one node of that partition
        node = subprocess.run(['sinfo', '-h', '-p', sl['partition'], '-N', '-o', '%N'],
                              stdout=PIPE, stderr=PIPE, universal_newlines=True).stdout.split()
        nd = {}
        if node:
            txt = subprocess.run(['scontrol', 'show', 'node', node[0]],
                                 stdout=PIPE, stderr=PIPE, universal_newlines=True).stdout
            nd = dict(re.findall(r'(\w+)=(\S*)', txt))
            sl['node_example'] = node[0]
        sockets = int(nd.get('Sockets', 0) or 0)
        cps = int(nd.get('CoresPerSocket', 0) or 0)
        tpc = int(nd.get('ThreadsPerCore', 1) or 1)
        spec = int(nd.get('CoreSpecCount', 0) or 0)
        cores, how = None, ''
        if nd.get('CPUEfctv') and tpc:
            cores, how = int(nd['CPUEfctv']) // tpc, f'CPUEfctv={nd["CPUEfctv"]} / ThreadsPerCore={tpc}'
        elif sockets and cps:
            cores = sockets * cps - spec
            how = f'Sockets*CoresPerSocket={sockets * cps}' + (f' - CoreSpecCount={spec}' if spec else '')
        sl.update(cores=cores, cores_how=how, smt=tpc, total_cores=sockets * cps or None,
                  core_spec=spec, memory_mb=nd.get('RealMemory'), features=nd.get('AvailableFeatures'))
        if cores is None:
            self.todo.append('node.cores_per_node: could not read it from scontrol')
        # accounting -> scheduler backend used by ReFrame
        cfg = subprocess.run(['scontrol', 'show', 'config'], stdout=PIPE, stderr=PIPE, universal_newlines=True).stdout
        acc = re.search(r'AccountingStorageType\s*=\s*(\S+)', cfg)
        sl['accounting'] = acc.group(1) if acc else 'unknown'
        if acc and acc.group(1).endswith('none'):
            sl['scheduler'] = 'squeue'
        # accounts
        if not sl['account']:
            env_acc = os.environ.get('SBATCH_ACCOUNT') or os.environ.get('SLURM_ACCOUNT')
            accts = []
            if shutil.which('sacctmgr'):
                o = subprocess.run(['sacctmgr', '-n', '-P', 'show', 'assoc',
                                    f'where user={getpass.getuser()}', 'format=account'],
                                   stdout=PIPE, stderr=PIPE, universal_newlines=True).stdout
                accts = sorted({l.strip() for l in o.splitlines() if l.strip()})
            sl['accounts'] = accts
            if env_acc:
                sl['account'] = env_acc
            elif len(accts) == 1:
                sl['account'] = accts[0]
            elif accts:
                sl['account'] = accts[0]
                self.todo.append(f'slurm.account: several accounts ({", ".join(accts)}); picked {accts[0]}')
        if self.args.probe_job:
            self.probe_job(sl)
        print(f'[slurm] partition={sl["partition"]} account={sl["account"] or "-"} '
              f'cores/node={sl["cores"]} smt={sl["smt"]} scheduler={sl["scheduler"]}')
        return sl

    def probe_job(self, sl):
        cmd = ['sbatch', '--wait', '--exclusive', '-N', '1', '-t', '00:05:00',
               '-J', 'perfsonde-probe', '-p', sl['partition']]
        if sl.get('account'):
            cmd += ['-A', sl['account']]
        outf = os.path.join(ROOT, f'.perfsonde-probe-{os.getpid()}.out')
        cmd += ['-o', outf, '--wrap',
                'echo NPROC=$(nproc); echo TPC=$(lscpu | awk -F: \'/Thread\\(s\\) per core/{gsub(/ /,"",$2);print $2}\'); hostname']
        print('[slurm] submitting a 1-node probe job and waiting (sbatch --wait) ...')
        r = subprocess.run(cmd, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        txt = open(outf).read() if os.path.exists(outf) else ''
        if os.path.exists(outf):
            os.remove(outf)
        m = re.search(r'NPROC=(\d+)', txt)
        if r.returncode == 0 and m:
            nproc = int(m.group(1))
            tpc = sl.get('smt') or 1
            sl['cores'], sl['cores_how'] = nproc // tpc, f'probe job: nproc={nproc} / ThreadsPerCore={tpc}'
        else:
            self.warn.append(f'probe job failed: {r.stderr.strip() or txt[:200]}')

    # ---------------- run ------------------------------------------------
    def run(self):
        self.module_system()
        tcs = []
        for base in self.compilers():
            print(f'[toolchain] probing {" ".join(base) or "(system compiler)"} ...')
            tc = self.toolchain(base)
            if tc.get('error'):
                self.warn.append(f'toolchain {base}: {tc["error"]}')
                continue
            print(f'  -> {" ".join(tc["modules"])}')
            print(f'     wrappers {tc["wrappers"]["fc"]}/{tc["wrappers"]["cc"]}; '
                  + ', '.join(f'{k}={v["expr"] or "?"}' for k, v in tc['prefix'].items()))
            for w in tc['warn']:
                print(f'     warning: {w}')
            tcs.append(tc)
        labels = set()
        for tc in tcs:
            lab, i = tc['label'], 2
            while lab in labels:
                lab, i = f'{tc["label"]}_{i}', i + 1
            tc['label'] = lab
            labels.add(lab)
        if not tcs:
            raise SystemExit('discover: no usable GNU toolchain found; see warnings above')
        self.tcs = tcs
        self.sl = self.slurm()
        # version inventory for "library version" comparisons
        av = set(self.base['avail'])
        for tc in tcs:
            av |= set(tc['loaded'])
        s2 = self.probe(tcs[0]['modules'])
        av |= set(s2['avail'])
        inv = {}
        for fam in ['gcc', 'gcc-native', 'PrgEnv-gnu', 'cpe', 'llvm', 'aocc', 'cce', 'intel',
                    'nvhpc'] + MPI_FAMS + HDF5_FAMS + ['cray-netcdf', 'netcdf', 'netcdf-c',
                                                       'netcdf-fortran'] + PNETCDF_FAMS:
            vs = versions_of(sorted(av), fam)
            if vs:
                inv[fam] = vs
        self.inventory = inv


# --------------------------------------------------------------------------
# writing the profile
# --------------------------------------------------------------------------

def q(s):
    """YAML double-quoted scalar."""
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


def flow_list(items):
    return '[' + ', '.join(items) + ']'


def site_name_guess():
    fq = socket.getfqdn()
    labels = fq.split('.')
    first = labels[0]
    if re.match(r'^(login|ln|uan|head|front|fe)[-_]?\d*$', first) and len(labels) > 1:
        return labels[1]
    return re.sub(r'[-_]?\d+$', '', first) or first


def host_patterns():
    pats = []
    for h in {socket.getfqdn(), socket.gethostname()}:
        if h:
            p = '^' + re.sub(r'\d+', r'\\d+', re.escape(h).replace('\\-', '-')) + '$'
            if p not in pats:
                pats.append(p)
    return pats


def render_site(name, d):
    sl, tc0 = d.sl, d.tcs[0]
    cores = sl.get('cores')
    smt = sl.get('smt') or 1
    tools = tc0['tools']
    osu = tc0['modules'][:len(tc0['base'])] + ([tc0['mpi']] if tc0['mpi'] else []) + \
        ([tools['osu']] if tools.get('osu') else [])
    metis = tools.get('metis') or ('' if 'gpmetis' in tc0['which'] else 'TODO-metis-module')
    nccmp = tools.get('nccmp') or ('' if 'nccmp' in tc0['which'] else 'TODO-nccmp-module')
    acct = sl.get('account') or ''
    lines = [
        '# =============================================================================',
        f'# PerfSonde site profile "{name}" — generated by tools/discover.py on {d.info["generated"]}',
        f'# host {d.info["host"]}; see discovery.md for what was found and why.',
        '# Check every line marked TODO. Paths may use ~, $VAR and {site}.',
        '# =============================================================================',
        '',
        'system:',
        f'  name: {name}',
        f'  descr: {q("generated by PerfSonde discover on " + d.info["host"])}',
        '  hostnames:                 # this profile is picked automatically on matching hosts',
    ]
    lines += [f'    - {q(p)}' for p in host_patterns()]
    lines += [
        '  modules_system: nomod      # each build\'s env.sh loads its own modules',
        f'  module_init: {q(d.init)}   # clean start before loading a toolchain ({d.info["module_system"]})',
        '',
        'slurm:',
        f'  scheduler: {sl["scheduler"]}            # accounting: {sl.get("accounting", "?")}',
        f'  partition: {sl.get("partition") or "TODO"}' +
        (f'                # all: {", ".join(p["name"] for p in sl.get("partitions", []))}'
         if sl.get('partitions') else ''),
        f'  account: {q(acct)}' + ('' if acct else '                 # TODO if your site needs -A')
        + (f'   # yours: {", ".join(sl["accounts"])}' if len(sl.get('accounts') or []) > 1 else ''),
        '  qos: ""',
        '  extra_options: []           # e.g. ["--constraint=cpu"]',
        '  time_limit: "00:30:00"',
        '  max_jobs: 8',
        '  srun_options: ["--cpu-bind=cores", "--distribution=block:block"]',
        '',
        '# helper tools (MPAS partitioning, output comparison, I/O tracing, network baseline)',
        '# an empty string means "already on PATH, no module needed"',
        'tools:',
        f'  metis: {q(metis)}',
        f'  nccmp: {q(nccmp)}',
        f'  darshan: {q(tools.get("darshan") or "")}',
        f'  osu: {flow_list(osu)}' + ('' if tools.get('osu') else
                                       '   # TODO: no osu-micro-benchmarks module; set osu.bin_dir in matrix.yaml'),
        '',
        'node:',
        f'  cores_per_node: {cores if cores else "TODO"}' +
        (f'          # {sl.get("cores_how")}' if sl.get('cores_how') else ''),
        f'  smt: {smt}                     # hardware threads per core seen by Slurm',
        '',
        'build:',
        '  root: work/{site}/builds    # TODO: put on a large shared file system (scratch/project)',
        '  mode: sbatch',
        '  make_jobs: 16',
        '  sbatch_options:',
        '    - "--nodes=1"',
        '    - "--ntasks=1"',
        '    - "--cpus-per-task=16"',
        '    - "--time=02:00:00"',
        '',
        'results:',
        '  root: work/{site}/results',
        '',
        'reframe:',
        '  dir: work/{site}/rfm',
        '',
    ]
    return '\n'.join(lines)


def render_toolchain(tc):
    p = tc['prefix']
    out = [f'  {tc["label"]}:',
           f'    # probed: gfortran {tc["gfortran"] or "?"}; MPI {tc["mpi"] or "none"}; '
           f'wrappers {tc["wrappers"]["fc"]}/{tc["wrappers"]["cc"]}/{tc["wrappers"]["cxx"]}',
           f'    modules: {flow_list(tc["modules"])}',
           '    env:']
    names = {'netcdf_c': 'NETCDF_C_PREFIX', 'netcdf_f': 'NETCDF_F_PREFIX',
             'pnetcdf': 'PNETCDF_PREFIX', 'hdf5': 'HDF5_PREFIX'}
    for kind, var in names.items():
        e = p[kind]['expr']
        if e:
            out.append(f'      {var}: {q(e)}          # {p[kind]["how"]} -> {p[kind]["value"]}')
        elif kind != 'hdf5':
            out.append(f'      {var}: "TODO"          # not found; set the install prefix')
    w = tc['wrappers']
    out += ['    mpas:',
            '      make_overrides: {' + f'FC_PARALLEL: {w["fc"]}, CC_PARALLEL: {w["cc"]}, '
            f'CXX_PARALLEL: {w["cxx"]}' + '}']
    wrf = {}
    if w['fc'] != 'mpif90':
        wrf['DM_FC'] = w['fc']
    if w['cc'] != 'mpicc':
        wrf['DM_CC'] = w['cc']
    if wrf:
        out += ['    wrf:',
                '      configure_overrides: {' + ', '.join(f'{k}: {v}' for k, v in wrf.items()) + '}']
    return '\n'.join(out)


def render_builds(name, d):
    tcs = d.tcs
    has_pnc = any(tc['prefix']['pnetcdf']['expr'] for tc in tcs)
    has_pnc_f = any(tc['prefix']['pnetcdf']['expr'] and tc.get('pnetcdf_fortran') for tc in tcs)
    labels = [tc['label'] for tc in tcs]
    lines = [
        '# =============================================================================',
        f'# build matrix for site "{name}" — generated by tools/discover.py',
        '# Toolchains below were loaded and checked on this system (see discovery.md).',
        '# Variants = cartesian product of the axes of each model.  ./perfsonde build list -v',
        '# =============================================================================',
        '',
        'toolchains:',
    ]
    lines += [render_toolchain(tc) for tc in tcs]
    alt = alternatives(d)
    if alt:
        lines += ['', '  # --- library-version comparison: copy a toolchain and change one module, e.g.']
        lines += [f'  # {l}' for l in alt]
    lines += ['', f'''mpas:
  source: TODO/path/to/MPAS-Model     # a tree you have already built once
  core: atmosphere
  name: "mpas-{{toolchain}}-{{opt}}-{{par}}"
  common:
    env:
      NETCDF: "$NETCDF_C_PREFIX"
      NETCDFF: "$NETCDF_F_PREFIX"
      PNETCDF: "$PNETCDF_PREFIX"      # SMIOL needs PnetCDF{'' if has_pnc else '  (TODO: none found!)'}
    mpas:
      target: gnu
      # make_args: ["PRECISION=double"]   # recent MPAS builds single precision by default
  axes:
    toolchain: {flow_list(labels)}
    opt:
      O3: {{}}                          # gnu target default (-O3)
      O2:
        mpas:
          make_overrides:
            FFLAGS_OPT: "-std=f2008 -fimplicit-none -O2 -ffree-line-length-none -fconvert=big-endian -ffree-form"
            CFLAGS_OPT: "-O2"
            CXXFLAGS_OPT: "-O2"
            LDFLAGS_OPT: "-O2"
      O3native:
        mpas:
          make_overrides:
            FFLAGS_OPT: "-std=f2008 -fimplicit-none -O3 -march=native -ffree-line-length-none -fconvert=big-endian -ffree-form"
            CFLAGS_OPT: "-O3 -march=native"
            CXXFLAGS_OPT: "-O3 -march=native"
            LDFLAGS_OPT: "-O3 -march=native"
    par:
      mpi:    {{openmp: false}}
      hybrid: {{openmp: true}}
  exclude: []

wrf:
  source: TODO/path/to/WRF            # a tree you have already built once (submodules populated)
  name: "wrf-{{toolchain}}-{{opt}}-{{par}}-{{io}}"
  common:
    env:
      NETCDF: "$NETCDF_F_PREFIX"
      NETCDF_C: "$NETCDF_C_PREFIX"
      WRFIO_NCD_LARGE_FILE_SUPPORT: "1"
    wrf:
      configure_match: '^GNU \\(gfortran/gcc\\)$'
      compile_case: em_real
  axes:
    toolchain: {flow_list(labels)}
    opt:
      O2: {{}}                          # WRF GNU default
      O3native:
        wrf:
          configure_overrides:
            FCOPTIM: "-O3 -march=native -ftree-vectorize -funroll-loops"
            CFLAGS_LOCAL: "-w -O3 -march=native -c"
    par:
      dmpar: {{openmp: false, wrf: {{parallel: dmpar}}}}
      dmsm:  {{openmp: true,  wrf: {{parallel: dm+sm}}}}
    io:
      nc:  {{env: {{PNETCDF: null}}}}''' + ('''
      pnc: {env: {PNETCDF: "$PNETCDF_PREFIX"}}''' if has_pnc_f else '''
      # pnc: {env: {PNETCDF: "$PNETCDF_PREFIX"}}   # no PnetCDF with a Fortran interface found''') + '''
  exclude: []
''']
    return '\n'.join(lines)


def alternatives(d):
    """Commented toolchain variants using other installed library versions."""
    tc = d.tcs[0]
    out = []
    skip = {family(m) for m in tc['base']}       # compilers are already separate toolchains
    for fam in [family(m) for m in tc['modules']]:
        cur = next(m for m in tc['modules'] if family(m) == fam)
        others = [m for m in d.inventory.get(fam, []) if m != cur
                  and not any(a in version(m).lower() for a in AVOID)]
        if others and fam not in skip:
            alt_mods = [others[-1] if m == cur else m for m in tc['modules']]
            out.append(f'{tc["label"]}_{re.sub(r"[^A-Za-z0-9]", "", fam)}{re.sub(r"[^0-9]", "", version(others[-1]))}: '
                       f'{{modules: {flow_list(alt_mods)}, env: ...same as {tc["label"]}...}}')
    if 'cpe' in d.inventory:
        out.append(f'Cray PE versions: {", ".join(d.inventory["cpe"])} - add "cpe/<ver>" as the '
                   'LAST module of a toolchain copy and check build_info.txt for the result')
    return out


def render_matrix(name, d):
    cores = d.sl.get('cores') or 0
    threads = [t for t in (1, 2, 4, 7, 8, 14, 16) if cores and cores % t == 0][:5] or [1, 2, 4]
    return f'''# =============================================================================
# run matrix for site "{name}" — generated by tools/discover.py
# Each model: successful builds x nodes x layouts x repeats Slurm jobs.
#   threads_per_rank: full-node layouts ({cores or "?"} cores/node):
#     {", ".join(f"{cores // t}x{t}" for t in threads) if cores else "set node.cores_per_node first"}
# Count the jobs first:  ./perfsonde list mpas
# =============================================================================

repeats: 3
darshan: false

mpas:
  case_dir: TODO/path/to/mpas_case    # init file, namelist.atmosphere, streams.atmosphere, graph.info
  graph_file: TODO.graph.info         # e.g. x1.40962.graph.info (partitions made with gpmetis)
  builds: all
  nodes: [1, 2, 4]
  threads_per_rank: {flow_list(str(t) for t in threads)}
  time_limit: "00:30:00"
  link_from_build: ["src/core_atmosphere/physics/physics_wrf/files/*"]
  namelist:
    nhyd_model.config_run_duration: "0_06:00:00"
  file_patches: {{}}
  by_build_label: {{}}
  extra_timers: []
  env: {{}}

wrf:
  case_dir: TODO/path/to/wrf_case     # namelist.input, wrfinput_d01, wrfbdy_d01
  builds: all
  nodes: [1, 2, 4]
  threads_per_rank: {flow_list(str(t) for t in threads)}
  time_limit: "00:30:00"
  skip_steps: 1
  domain: 1
  link_from_build: ["run/*"]
  namelist: {{}}
  by_build_label:
    io:
      nc:  {{namelist: {{time_control.io_form_history: 2}}}}
      pnc: {{namelist: {{time_control.io_form_history: 11}}}}
  env:
    OMP_STACKSIZE: 1G

osu:
  pt2pt_sizes: [8, 65536, 1048576]
  allreduce_nodes: [2, 4]
  allreduce_sizes: [8, 65536, 1048576]
'''


def render_report(name, d):
    sl = d.sl
    L = [f'# perfsonde discovery: {name}', '',
         f'- generated: {d.info["generated"]} on `{d.info["host"]}` by `{d.info["user"]}`',
         f'- module system: {d.info["module_system"]}; clean start: `{d.init}`',
         f'- modules loaded at login: {", ".join(d.info["initial_list"]) or "-"}',
         f'- after clean start: {", ".join(d.info["clean_list"]) or "-"}',
         f'- HPE Cray PE (PrgEnv-gnu): {"yes" if d.cray else "no"}', '',
         '## Slurm', '',
         f'- partition: `{sl.get("partition")}` (default marked by sinfo), account: `{sl.get("account") or "-"}`',
         f'- usable cores per node: {sl.get("cores")} ({sl.get("cores_how", "")}), '
         f'SMT {sl.get("smt")}, core specialisation {sl.get("core_spec", 0)}',
         f'- accounting: {sl.get("accounting")} -> ReFrame scheduler `{sl.get("scheduler")}`', '']
    if sl.get('partitions'):
        L += ['| partition | default | state | time limit | nodes | CPUs | S:C:T |', '|---|---|---|---|---|---|---|']
        L += [f'| {p["name"]} | {"*" if p["default"] else ""} | {p["avail"]} | {p["timelimit"]} | '
              f'{p["nodes"]} | {p["cpus"]} | {p["sct"]} |' for p in sl['partitions']]
        L.append('')
    L += ['## Toolchains', '']
    for tc in d.tcs:
        L += [f'### {tc["label"]}', '',
              f'- requested: `{" ".join(tc["base"])}` + MPI `{tc["mpi"]}`',
              f'- pinned module list: `{" ".join(tc["modules"])}`',
              f'- gfortran: {tc["gfortran_line"] or tc["gfortran"]}',
              f'- wrappers: FC=`{tc["wrappers"]["fc"]}` CC=`{tc["wrappers"]["cc"]}` CXX=`{tc["wrappers"]["cxx"]}`',
              '- library prefixes:']
        for k, v in tc['prefix'].items():
            L.append(f'  - {k} ({v["module"] or "no module"}): `{v["expr"] or "NOT FOUND"}` '
                     f'via {v["how"]} -> `{v["value"]}`')
        L.append("- helper tools: " + ', '.join(f'{k}=`{v or "-"}`' for k, v in tc['tools'].items()))
        L.append(f'- loaded after probe: `{" ".join(tc["loaded"])}`')
        for w in tc['warn']:
            L.append(f'- WARNING: {w}')
        L.append('')
    L += ['## Installed versions (for library-version comparisons)', '']
    for fam, vs in d.inventory.items():
        L.append(f'- {fam}: {", ".join(version(v) or v for v in vs)}')
    L += ['', '## Warnings', ''] + ([f'- {w}' for w in d.warn] or ['- none'])
    L += ['', '## TODO', '',
          '- builds.yaml: `mpas.source`, `wrf.source` (trees you have already built once)',
          '- matrix.yaml: `case_dir`, `graph_file`',
          '- site.yaml: `build.root` on a large shared file system']
    L += [f'- {t}' for t in d.todo]
    return '\n'.join(L) + '\n'


def validate(outdir):
    """Load the generated files and expand the build matrix (needs PyYAML)."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        return 'PyYAML not available: skipped validation (run ./perfsonde build list later)'
    env = dict(os.environ, PERFSONDE_SITE=os.path.join(outdir, 'site.yaml'),
               PERFSONDE_BUILDS=os.path.join(outdir, 'builds.yaml'),
               PERFSONDE_MATRIX=os.path.join(outdir, 'matrix.yaml'))
    code = ('import sys; sys.path.insert(0, %r)\n'
            'from perfsonde import builds, site\n'
            'site.site(); site.matrix()\n'
            'n = [len(builds.expand(m)) for m in builds.MODELS]\n'
            'print("ok: %%d MPAS + %%d WRF build variants" %% tuple(n))\n') % os.path.join(ROOT, 'lib')
    r = subprocess.run([sys.executable, '-c', code], stdout=PIPE, stderr=PIPE, universal_newlines=True, env=env)
    return (r.stdout.strip() or r.stderr.strip().splitlines()[-1]) if r.returncode == 0 \
        else 'VALIDATION FAILED: ' + r.stderr.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--name', help='site name (default: from the host name)')
    ap.add_argument('--partition', help='Slurm partition (default: sinfo default)')
    ap.add_argument('--account', help='Slurm account (default: SBATCH_ACCOUNT or sacctmgr)')
    ap.add_argument('--max-compilers', type=int, default=2,
                    help='GNU compiler versions to probe (default 2: default + newest)')
    ap.add_argument('--probe-job', action='store_true',
                    help='submit a 1-node job to measure usable cores (waits in the queue)')
    ap.add_argument('--force', action='store_true', help='overwrite an existing profile (*.bak kept)')
    ap.add_argument('--dry-run', action='store_true', help='print the files instead of writing')
    ap.add_argument('-v', '--verbose', action='store_true', help='show the shell scripts run')
    args = ap.parse_args()
    name = args.name or site_name_guess()
    if not re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$', name):
        sys.exit(f'bad site name {name!r}')
    outdir = os.path.join(ROOT, 'config', 'sites', name)
    if os.path.exists(os.path.join(outdir, 'site.yaml')) and not (args.force or args.dry_run):
        sys.exit(f'{outdir} already exists: use --force to regenerate (old files kept as *.bak) '
                 f'or --name to pick another name')
    d = Discover(args)
    d.run()
    files = {'site.yaml': render_site(name, d), 'builds.yaml': render_builds(name, d),
             'matrix.yaml': render_matrix(name, d), 'discovery.md': render_report(name, d)}
    raw = {'info': d.info, 'slurm': d.sl, 'toolchains': d.tcs, 'inventory': d.inventory,
           'avail_clean': d.base['avail'], 'defaults_clean': d.base['defaults'],
           'warnings': d.warn, 'todo': d.todo}
    if args.dry_run:
        for fn, txt in files.items():
            print(f'\n===== {fn} =====\n{txt}')
        return
    os.makedirs(outdir, exist_ok=True)
    for fn, txt in files.items():
        p = os.path.join(outdir, fn)
        if os.path.exists(p) and fn.endswith('.yaml'):
            shutil.copy2(p, p + '.bak')
        with open(p, 'w') as fp:
            fp.write(txt)
    with open(os.path.join(outdir, 'discovery.json'), 'w') as fp:
        json.dump(raw, fp, indent=1, default=str)
    print(f'\nwrote {outdir}/: site.yaml builds.yaml matrix.yaml discovery.md discovery.json')
    print('check:', validate(outdir))
    n_todo = sum(t.count('TODO') for k, t in files.items() if k.endswith('.yaml'))
    print(f'{n_todo} TODO marks to fill in (grep -n TODO {os.path.relpath(outdir)}/*.yaml); '
          f'then: ./perfsonde --site {name} build list -v')


if __name__ == '__main__':
    main()
