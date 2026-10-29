#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verification result containers and report writers."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import List
from xml.sax.saxutils import escape


@dataclass
class StepDetail:
    index: int
    type: str
    status: str
    seconds: float = 0.0
    message: str = ''
    output: str = ''


@dataclass
class Result:
    name: str
    status: str = 'pass'
    seconds: float = 0.0
    reason: str = ''
    details: List[StepDetail] = field(default_factory=list)

    def add(self, detail: StepDetail) -> None:
        self.details.append(detail)
        if detail.status == 'fail':
            self.status = 'fail'
            if not self.reason:
                self.reason = detail.message or ('步骤 %d (%s) 失败'
                                                 % (detail.index, detail.type))

    @property
    def is_failure(self) -> bool:
        return self.status == 'fail'


def print_summary(results, stream=print) -> None:
    for result in results:
        if result.status == 'skip':
            stream('[SKIP] %-34s %s' % (result.name, result.reason))
        elif result.status == 'pass':
            stream('[PASS] %-34s %.2fs' % (result.name, result.seconds))
        else:
            stream('[FAIL] %-34s %s' % (result.name, result.reason))
            for detail in result.details:
                if detail.status == 'fail':
                    for line in (detail.output or '').strip().splitlines()[-5:]:
                        stream('       %s' % line)
    passed = sum(1 for item in results if item.status == 'pass')
    failed = sum(1 for item in results if item.status == 'fail')
    skipped = sum(1 for item in results if item.status == 'skip')
    stream('')
    stream('通过 %d，失败 %d，跳过 %d，共 %d'
           % (passed, failed, skipped, len(results)))


def write_json(results, path) -> None:
    payload = {
        'created': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'summary': {
            'total': len(results),
            'pass': sum(1 for item in results if item.status == 'pass'),
            'fail': sum(1 for item in results if item.status == 'fail'),
            'skip': sum(1 for item in results if item.status == 'skip'),
        },
        'cases': [asdict(item) for item in results],
    }
    _write(path, json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


def write_junit(results, path, suite='parallel-verify') -> None:
    failures = sum(1 for item in results if item.status == 'fail')
    skipped = sum(1 for item in results if item.status == 'skip')
    total = sum(item.seconds for item in results)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<testsuite name="%s" tests="%d" failures="%d" skipped="%d" '
             'time="%.3f">' % (escape(suite), len(results), failures,
                               skipped, total)]
    for result in results:
        lines.append('  <testcase classname="%s" name="%s" time="%.3f">'
                     % (escape(suite), escape(result.name), result.seconds))
        if result.status == 'fail':
            lines.append('    <failure message="%s">%s</failure>'
                         % (escape(result.reason),
                            escape(_details_text(result))))
        elif result.status == 'skip':
            lines.append('    <skipped message="%s"/>'
                         % escape(result.reason))
        lines.append('  </testcase>')
    lines.append('</testsuite>')
    _write(path, '\n'.join(lines) + '\n')


def write_evidence(results, directory) -> None:
    os.makedirs(directory, exist_ok=True)
    for result in results:
        if result.status != 'fail':
            continue
        _write(os.path.join(directory, '%s.txt' % _safe(result.name)),
               _details_text(result))


def _details_text(result) -> str:
    lines = ['case: %s' % result.name,
             'status: %s' % result.status,
             'reason: %s' % result.reason,
             '']
    for detail in result.details:
        lines.append('step %d (%s): %s (%.2fs)'
                     % (detail.index, detail.type, detail.status,
                        detail.seconds))
        if detail.message:
            lines.append('  message: %s' % detail.message)
        if detail.output:
            for line in detail.output.strip().splitlines():
                lines.append('  | %s' % line)
        lines.append('')
    return '\n'.join(lines) + '\n'


def _safe(name) -> str:
    return ''.join(char if char.isalnum() or char in '-_.' else '_'
                   for char in name)


def _write(path, text) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)
