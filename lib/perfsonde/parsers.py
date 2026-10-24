"""Plain-Python parsers for MPAS-A and WRF timing output.

They are deliberately free of ReFrame so they can be unit-tested and reused
by the analysis scripts.
"""

import re
import statistics

# MPAS mpas_timer_write() format:
#   (i2, 1x, a45, f15.5, i10, 3f15.5, 1x, f8.2, 3x, f8.2, 3x, f8.2)
#   level, indent//name, total, calls, min, max, avg, pct_tot, pct_par, par_eff
_NUM = r'[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?|\*+|NaN'
_MPAS_TIMER = re.compile(
    rf'^\s*(\d+)\s(.{{1,45}}?)\s+({_NUM})\s+(\d+)\s+({_NUM})\s+({_NUM})\s+({_NUM})'
    rf'\s+({_NUM})\s+({_NUM})\s+({_NUM})\s*$'
)


def _f(x):
    try:
        return float(x)
    except ValueError:
        return float('nan')


def mpas_timers(text):
    """Return {timer_name: {...}} from an MPAS log. First occurrence wins.

    Only lines after the 'timer_name' header are considered.
    """
    out = {}
    started = False
    for line in text.splitlines():
        if not started:
            if 'timer_name' in line and 'total' in line:
                started = True
            continue
        m = _MPAS_TIMER.match(line)
        if not m:
            continue
        name = m.group(2).strip()
        if name in out:
            continue
        out[name] = {
            'level': int(m.group(1)),
            'total': _f(m.group(3)),
            'calls': int(m.group(4)),
            'min': _f(m.group(5)),
            'max': _f(m.group(6)),
            'avg': _f(m.group(7)),
            'pct_tot': _f(m.group(8)),
            'pct_par': _f(m.group(9)),
            'par_eff': _f(m.group(10)),
        }
    return out


def mpas_timer_total(path, name):
    with open(path, errors='replace') as fp:
        timers = mpas_timers(fp.read())
    if name not in timers:
        raise ValueError(f'MPAS timer {name!r} not found in {path}; '
                         f'available: {", ".join(timers)}')
    return timers[name]['total']


# WRF: "Timing for main: time 2019-09-04_12:00:30 on domain   1:    0.70393 elapsed seconds"
_WRF_MAIN = re.compile(
    r'Timing for main:\s+time\s+(\S+)\s+on domain\s+(\d+):\s+([\d.Ee+-]+)\s+elapsed seconds')
_WRF_WRITE = re.compile(
    r'Timing for Writing\s+(.*?)\s+for domain\s+(\d+):\s+([\d.Ee+-]+)\s+elapsed seconds')
_WRF_INPUT = re.compile(
    r'Timing for processing (?:wrfinput|lateral boundary)[^:]*:\s+([\d.Ee+-]+)\s+elapsed seconds')


def wrf_timing(text, domain=1, skip_steps=1):
    """Summarise WRF rsl.error.0000 timing for one domain.

    skip_steps: number of leading main-loop steps to drop (first step includes
    start-up and radiation initialisation).
    """
    steps = [float(m.group(3)) for m in _WRF_MAIN.finditer(text)
             if int(m.group(2)) == domain]
    writes = [float(m.group(3)) for m in _WRF_WRITE.finditer(text)
              if int(m.group(2)) == domain]
    inputs = [float(m.group(1)) for m in _WRF_INPUT.finditer(text)]
    used = steps[skip_steps:] if len(steps) > skip_steps else steps
    if not used:
        raise ValueError('no "Timing for main" lines found')
    return {
        'nsteps': len(steps),
        'mean_step': statistics.fmean(used),
        'median_step': statistics.median(used),
        'max_step': max(used),
        'total_main': sum(steps),
        'total_write': sum(writes),
        'total_input': sum(inputs),
    }


def wrf_metric(path, metric, domain=1, skip_steps=1):
    with open(path, errors='replace') as fp:
        return wrf_timing(fp.read(), domain, skip_steps)[metric]
