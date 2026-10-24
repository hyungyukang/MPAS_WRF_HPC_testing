"""Locate the perfsonde tree, pick the current site profile and load its YAML files.

Site profiles live in config/sites/<name>/{site,builds,matrix}.yaml.
The site is chosen, in order, by
  1. PERFSONDE_SITE_NAME (perfsonde --site NAME sets it)
  2. the hostname matching one profile's system.hostnames regexes
  3. the only profile, if there is exactly one
Single files can still be overridden with PERFSONDE_SITE / PERFSONDE_BUILDS /
PERFSONDE_MATRIX (full paths), which is handy for tests.

Paths in the files may use ~, $VARS and {site}; relative paths are under ROOT.
"""

import functools
import os
import re
import socket

import yaml

ROOT = os.environ.get(
    'PERFSONDE_ROOT',
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
SITES_DIR = os.path.join(ROOT, 'config', 'sites')


class SiteError(RuntimeError):
    pass


def list_sites():
    if not os.path.isdir(SITES_DIR):
        return []
    return sorted(d for d in os.listdir(SITES_DIR)
                  if os.path.isfile(os.path.join(SITES_DIR, d, 'site.yaml')))


def _hostnames():
    names = {socket.gethostname(), socket.getfqdn()}
    return {n for n in names if n}


@functools.lru_cache(maxsize=None)
def site_name():
    name = os.environ.get('PERFSONDE_SITE_NAME')
    if name:
        if name not in list_sites() and not os.environ.get('PERFSONDE_SITE'):
            raise SiteError(f'unknown site {name!r}; available: {", ".join(list_sites()) or "none"}'
                            f' (create one with: ./perfsonde discover --name {name})')
        return name
    if os.environ.get('PERFSONDE_SITE'):
        with open(os.environ['PERFSONDE_SITE']) as fp:
            return (yaml.safe_load(fp) or {}).get('system', {}).get('name', 'site')
    sites = list_sites()
    hosts = _hostnames()
    matches = []
    for s in sites:
        with open(os.path.join(SITES_DIR, s, 'site.yaml')) as fp:
            pats = ((yaml.safe_load(fp) or {}).get('system', {}) or {}).get('hostnames') or []
        if any(re.search(p, h) for p in pats for h in hosts):
            matches.append(s)
    if len(matches) == 1:
        return matches[0]
    if len(sites) == 1:
        return sites[0]
    why = 'several profiles match this host' if matches else 'no profile matches this host'
    raise SiteError(f'{why} ({", ".join(sorted(hosts))}); use --site NAME '
                    f'(available: {", ".join(sites) or "none"}) or ./perfsonde discover')


def site_dir(name=None):
    return os.path.join(SITES_DIR, name or site_name())


def path(p):
    """Resolve a path from a config file: ~, $VAR, {site}; relative -> under ROOT."""
    p = str(p)
    if '{site}' in p:
        p = p.replace('{site}', site_name())
    p = os.path.expandvars(os.path.expanduser(p))
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(ROOT, p))


def _load(envvar, default_name):
    fname = os.environ.get(envvar) or os.path.join(site_dir(), default_name)
    with open(fname) as fp:
        return yaml.safe_load(fp) or {}


def site():
    return _load('PERFSONDE_SITE', 'site.yaml')


def builds_config():
    return _load('PERFSONDE_BUILDS', 'builds.yaml')


def matrix():
    return _load('PERFSONDE_MATRIX', 'matrix.yaml')


def tool_module(key, default=None):
    """Module name(s) for helper tools (metis, nccmp, darshan, osu) from site.yaml."""
    return (site().get('tools') or {}).get(key, default)


def build_root():
    return path(site()['build'].get('root', 'work/{site}/builds'))


def results_root():
    return path(site().get('results', {}).get('root', 'work/{site}/results'))


def work_path(key, default):
    return path((site().get('reframe') or {}).get(key, default))


def rfm_duration(val):
    """Slurm-style '[D-]HH:MM:SS' (or MM:SS) -> ReFrame '1h30m0s'; other values pass through."""
    if val is None:
        return None
    s = str(val).strip()
    days = 0
    if '-' in s and ':' in s:
        d, s = s.split('-', 1)
        days = int(d)
    if ':' not in s:
        return str(val)
    parts = [int(p) for p in s.split(':')]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts
    return f'{days * 24 + h}h{m}m{sec}s'
