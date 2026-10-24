"""Minimal Fortran-namelist patcher (WRF namelist.input, MPAS namelist.atmosphere).

Overrides are given as {"group.key": value} or {"key": value}:
  * an existing ``key = ...`` line is replaced (in the named group, or the
    first group that has it when no group is given);
  * a missing key is inserted at the top of its group (group required).
Values: bool -> .true./.false., str -> 'quoted' (unless it already starts
with a quote or a dot), list -> comma separated.
"""

import re


def fortran_value(val):
    if isinstance(val, bool):
        return '.true.' if val else '.false.'
    if isinstance(val, (list, tuple)):
        return ', '.join(fortran_value(v) for v in val)
    if isinstance(val, str):
        s = val.strip()
        if s[:1] in ("'", '"', '.') or re.fullmatch(r'[-+\d.eE]+', s):
            return s
        return f"'{s}'"
    return str(val)


def patch(text, overrides):
    lines = text.splitlines()
    for full_key, val in (overrides or {}).items():
        group, _, key = full_key.rpartition('.')
        group = group.lower()
        key_re = re.compile(rf'^(\s*){re.escape(key)}\s*=.*$', re.IGNORECASE)
        cur, done, gstart = None, False, None
        for i, line in enumerate(lines):
            s = line.strip().lower()
            if s.startswith('&'):
                cur = s[1:].split()[0] if s[1:] else ''
                if group and cur == group:
                    gstart = i
                continue
            if s == '/':
                cur = None
                continue
            if cur is None or (group and cur != group):
                continue
            m = key_re.match(line)
            if m:
                lines[i] = f'{m.group(1) or " "}{key} = {fortran_value(val)}'
                done = True
                break
        if not done:
            if gstart is None:
                raise KeyError(f'namelist key {full_key!r} not found; '
                               f'give it as "group.key" to insert it')
            lines.insert(gstart + 1, f' {key} = {fortran_value(val)}')
    return '\n'.join(lines) + '\n'
