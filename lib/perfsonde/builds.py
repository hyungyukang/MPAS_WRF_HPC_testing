"""Expand the build matrix in config/sites/<site>/builds.yaml and read finished builds.

A build variant is the cartesian product of the *axes* of one model. Every
axis value is a small spec; specs are merged in axis order:

  modules  list  -> concatenated (duplicates dropped, order kept)
  env      dict  -> later keys win
  mpas/wrf dict  -> merged recursively
  other    any   -> later value wins
"""

import copy
import itertools
import json
import os
import re

from . import site

MODELS = ('mpas', 'wrf')
EXE = {'mpas': 'atmosphere_model', 'wrf': 'wrf.exe'}


def merge(base, extra):
    out = copy.deepcopy(base)
    for key, val in (extra or {}).items():
        if key == 'modules':
            mods = list(out.get('modules', []))
            for m in val or []:
                if m not in mods:
                    mods.append(m)
            out['modules'] = mods
        elif isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _axis_values(axis_name, axis, toolchains):
    """Return [(label, spec)] for one axis.

    An axis is either a mapping {label: spec} or a list of labels that refer to
    entries of the top-level ``toolchains`` section.
    """
    if isinstance(axis, dict):
        return [(str(k), v or {}) for k, v in axis.items()]
    if isinstance(axis, list):
        out = []
        for label in axis:
            if label not in toolchains:
                raise KeyError(f'axis {axis_name!r}: unknown toolchain {label!r}')
            out.append((str(label), toolchains[label] or {}))
        return out
    raise TypeError(f'axis {axis_name!r} must be a mapping or a list')


def _excluded(labels, rules):
    for rule in rules or []:
        if all(labels.get(k) == str(v) for k, v in rule.items()):
            return True
    return False


def expand(model, cfg=None):
    """Return the list of resolved build specs for *model*."""
    cfg = cfg or site.builds_config()
    mcfg = cfg.get(model)
    if not mcfg:
        return []
    toolchains = cfg.get('toolchains', {})
    axes = mcfg.get('axes', {})
    names = list(axes)
    choices = [_axis_values(n, axes[n], toolchains) for n in names]
    template = mcfg.get('name', model + '-' + '-'.join('{%s}' % n for n in names))
    specs = []
    for combo in itertools.product(*choices):
        labels = {n: lab for n, (lab, _) in zip(names, combo)}
        if _excluded(labels, mcfg.get('exclude')):
            continue
        spec = merge({}, cfg.get('defaults', {}))
        spec = merge(spec, mcfg.get('common', {}))
        for _, part in combo:
            spec = merge(spec, part)
        spec['name'] = template.format(**labels)
        spec['model'] = model
        spec['labels'] = labels
        spec['source'] = site.path(mcfg['source'])
        spec.setdefault('openmp', False)
        specs.append(spec)
    names_seen = [s['name'] for s in specs]
    dups = {n for n in names_seen if names_seen.count(n) > 1}
    if dups:
        raise ValueError(f'duplicate build names {sorted(dups)}: '
                         f'include every axis in the "name" template')
    return specs


# --------------------------------------------------------------------------
# Finished builds (used by the ReFrame run tests)
# --------------------------------------------------------------------------

def build_dir(name):
    return os.path.join(site.build_root(), name)


def load_manifest(name):
    with open(os.path.join(build_dir(name), 'build.json')) as fp:
        return json.load(fp)


def is_ok(name):
    d = build_dir(name)
    try:
        model = load_manifest(name)['model']
    except (OSError, ValueError, KeyError):
        return False
    return (os.path.exists(os.path.join(d, 'BUILD_OK')) and
            os.access(os.path.join(d, 'bin', EXE[model]), os.X_OK))


def available(model, patterns=None):
    """Names of successful builds for *model*, filtered by regex *patterns*.

    *patterns* may be None/"all" (everything) or a list of regular
    expressions matched against the build name.
    """
    root = site.build_root()
    if not os.path.isdir(root):
        return []
    names = []
    for name in sorted(os.listdir(root)):
        if not is_ok(name):
            continue
        try:
            if load_manifest(name)['model'] != model:
                continue
        except (OSError, ValueError, KeyError):
            continue
        names.append(name)
    if patterns in (None, 'all', ['all']):
        return names
    if isinstance(patterns, str):
        patterns = [patterns]
    return [n for n in names if any(re.fullmatch(p, n) for p in patterns)]
