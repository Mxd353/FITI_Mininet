#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verification case loading and validation (no Mininet/root required)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

#: Required fields per step type.
STEP_FIELDS = {
    'ping': ('node', 'target'),
    'exec': ('node', 'command'),
    'ovs_trunks': ('switch', 'peer'),
    'p4_entry': ('switch', 'table', 'keys'),
    'linux_route': ('node', 'match'),
    'evpn_advertise': ('route',),
    'evpn_withdraw': ('route',),
    'link': ('node', 'peer', 'action'),
    'sleep': ('seconds',),
}

TAG_FAULTS = 'faults'


class SpecError(ValueError):
    pass


@dataclass
class Case:
    name: str
    steps: List[dict]
    tags: Tuple[str, ...] = ()
    requires_sites: Tuple[str, ...] = ()
    requires_hosts: int = 1
    teardown: List[dict] = field(default_factory=list)
    description: str = ''
    source: str = ''

    def wants(self, tag: str) -> bool:
        return tag in self.tags


def _as_tuple(value) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _validate_step(step, where: str) -> dict:
    if not isinstance(step, dict):
        raise SpecError('%s: 步骤必须是对象' % where)
    kind = step.get('type')
    if kind not in STEP_FIELDS:
        raise SpecError('%s: 未知步骤类型 %r' % (where, kind))
    for key in STEP_FIELDS[kind]:
        if key not in step:
            raise SpecError('%s: %s 缺少字段 %s' % (where, kind, key))
    if kind == 'ping':
        expect = step.get('expect', 'pass')
        if expect not in ('pass', 'fail'):
            raise SpecError('%s: ping.expect 必须是 pass/fail' % where)
    if kind == 'link':
        if step['action'] not in ('up', 'down'):
            raise SpecError('%s: link.action 必须是 up/down' % where)
    return dict(step)


def load_cases(path: str, tags: Optional[Sequence[str]] = None,
               include_faults: bool = False) -> List[Case]:
    """Load every ``*.yaml`` file under ``path`` (or a single file)."""
    files = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.endswith(('.yaml', '.yml')):
                files.append(os.path.join(path, name))
    else:
        files = [path]
    if not files:
        raise SpecError('没有找到用例文件: %s' % path)

    cases = []
    for filename in files:
        with open(filename, encoding='utf-8') as handle:
            data = yaml.safe_load(handle) or {}
        entries = data.get('cases') if isinstance(data, dict) else data
        if not entries:
            raise SpecError('%s: 没有 cases' % filename)
        for index, entry in enumerate(entries, 1):
            where = '%s#%d' % (os.path.basename(filename), index)
            if not isinstance(entry, dict) or 'name' not in entry:
                raise SpecError('%s: 用例必须有 name' % where)
            steps = [_validate_step(step, '%s/%s' % (where, entry['name']))
                     for step in entry.get('steps', [])]
            teardown = [
                _validate_step(step, '%s/%s/teardown'
                               % (where, entry['name']))
                for step in entry.get('teardown', [])]
            if not steps:
                raise SpecError('%s: 用例没有 steps' % where)
            cases.append(Case(
                name=str(entry['name']),
                steps=steps,
                tags=_as_tuple(entry.get('tags')),
                requires_sites=_as_tuple(entry.get('requires_sites')),
                requires_hosts=int(entry.get('requires_hosts', 1)),
                teardown=teardown,
                description=str(entry.get('description', '')),
                source=filename,
            ))

    names = [case.name for case in cases]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise SpecError('用例重名: %s' % ', '.join(sorted(duplicates)))

    return filter_cases(cases, tags, include_faults)


def filter_cases(cases: List[Case], tags: Optional[Sequence[str]] = None,
                 include_faults: bool = False) -> List[Case]:
    if tags:
        wanted = set(tags)
        return [case for case in cases
                if wanted.intersection(case.tags)]
    if include_faults:
        return list(cases)
    return [case for case in cases if not case.wants(TAG_FAULTS)]


def summarize(cases: List[Case], config_sites,
              hosts_per_site: int = 1) -> List[dict]:
    """Return a dry-run table (case -> run/skip + reason)."""
    sites = set(config_sites)
    rows = []
    for case in cases:
        missing = [site for site in case.requires_sites if site not in sites]
        reason = ''
        if missing:
            reason = '缺少站点: %s' % ','.join(missing)
        elif case.requires_hosts > hosts_per_site:
            reason = '需要每站点 %d 台主机' % case.requires_hosts
        rows.append({
            'name': case.name,
            'tags': ','.join(case.tags),
            'steps': len(case.steps),
            'status': 'SKIP' if reason else 'RUN',
            'reason': reason,
            'source': os.path.basename(case.source),
        })
    return rows
