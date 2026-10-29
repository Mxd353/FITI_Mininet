#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the verification harness and the config renderer."""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from parallel.config import load_config
from verify.report import Result, StepDetail, write_json, write_junit
from verify.runner import CaseRunner, parse_ping
from verify.spec import SpecError, load_cases
from tools.render_config import (render_huawei, render_ovs, render_sim,
                                 render_summary)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _path(*parts):
    return os.path.join(ROOT, *parts)


class CaseSpecTest(unittest.TestCase):
    def test_default_cases_exclude_faults(self):
        cases = load_cases(_path('verify', 'cases'))
        names = [case.name for case in cases]
        self.assertIn('l3vpn-net1-bj-sh', names)
        self.assertNotIn('link-down-bj-tjn', names)
        self.assertEqual(len(names), len(set(names)))

    def test_faults_included_with_flag(self):
        cases = load_cases(_path('verify', 'cases'), include_faults=True)
        self.assertIn('link-down-bj-tjn', [case.name for case in cases])

    def test_tag_filter(self):
        cases = load_cases(_path('verify', 'cases'), tags=['isolation'])
        self.assertTrue(cases)
        self.assertTrue(all('isolation' in case.tags for case in cases))

    def test_invalid_step_is_rejected(self):
        with tempfile.NamedTemporaryFile('w', suffix='.yaml',
                                         delete=False) as handle:
            handle.write('cases:\n'
                         '  - name: bad\n'
                         '    steps:\n'
                         '      - type: nope\n')
            name = handle.name
        try:
            with self.assertRaises(SpecError):
                load_cases(name)
        finally:
            os.unlink(name)


class PingParseTest(unittest.TestCase):
    def test_unicast_ok(self):
        output = ('3 packets transmitted, 3 received, 0% packet loss, '
                  'time 2001ms')
        self.assertEqual(parse_ping(output), (0.0, 3))

    def test_unicast_lost(self):
        output = ('3 packets transmitted, 0 received, 100% packet loss, '
                  'time 2004ms')
        self.assertEqual(parse_ping(output), (100.0, 0))

    def test_multicast_duplicates(self):
        output = ('3 packets transmitted, 3 received, +2 duplicates, '
                  '0% packet loss')
        self.assertEqual(parse_ping(output), (0.0, 3))


class _FakeNode(object):
    def __init__(self, name, loss):
        self.name = name
        self.loss = loss

    def cmd(self, command):
        if command.startswith('ping6'):
            if command.split()[2] == '1':
                return '1 packets transmitted, 1 received, 0% packet loss'
            if self.loss == 0:
                return '3 packets transmitted, 3 received, 0% packet loss'
            return '3 packets transmitted, 0 received, 100% packet loss'
        return ''


class _FakeNet(object):
    def __init__(self, loss):
        self.thrift_base = 11000
        self.node = _FakeNode('cl_bj', loss)

    def get(self, name):
        return self.node if name == 'cl_bj' else None


class RunnerLogicTest(unittest.TestCase):
    def _run(self, loss, expect):
        from verify.spec import Case
        cfg = load_config(_path('configs', 'parallel-min.json'))
        runner = CaseRunner(_FakeNet(loss), cfg)
        case = Case(name='t', steps=[{
            'type': 'ping', 'node': 'cl_bj', 'iface': 'v3501',
            'target': '240a:a806:1:2::10', 'expect': expect,
        }], tags=('t',))
        return runner.run_case(case)

    def test_reachable_passes(self):
        self.assertEqual(self._run(0, 'pass').status, 'pass')

    def test_unreachable_expected_passes(self):
        self.assertEqual(self._run(100, 'fail').status, 'pass')

    def test_reachable_expected_but_lost_fails(self):
        result = self._run(100, 'pass')
        self.assertEqual(result.status, 'fail')
        self.assertIn('丢包', result.reason)


class ReportTest(unittest.TestCase):
    def test_json_and_junit(self):
        results = [
            Result(name='ok', status='pass', seconds=0.1),
            Result(name='bad', status='fail', seconds=0.2,
                   reason='期望可达',
                   details=[StepDetail(1, 'ping', 'fail', 0.2,
                                       output='100% packet loss')]),
            Result(name='skipme', status='skip', reason='缺少站点: gz'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            json_path = os.path.join(directory, 'report.json')
            xml_path = os.path.join(directory, 'report.xml')
            write_json(results, json_path)
            write_junit(results, xml_path)
            with open(json_path, encoding='utf-8') as handle:
                data = handle.read()
            self.assertIn('"fail": 1', data)
            tree = ET.parse(xml_path)
            suite = tree.getroot()
            self.assertEqual(suite.get('tests'), '3')
            self.assertEqual(suite.get('failures'), '1')
            self.assertEqual(suite.get('skipped'), '1')


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(_path('configs', 'parallel-min.json'))

    def test_huawei_matches_golden(self):
        with open(_path('configs', 'expected', 'huawei-min.txt'),
                  encoding='utf-8') as handle:
            expected = handle.read()
        self.assertEqual(render_huawei(self.cfg), expected)

    def test_sim_rows(self):
        rows = render_sim(self.cfg).strip().splitlines()
        self.assertEqual(len(rows), 1 + len(self.cfg.sites) * 4)
        self.assertIn('3501', rows[1])
        self.assertIn('fd00:0:1:1::101', rows[1])

    def test_ovs_lines(self):
        lines = render_ovs(self.cfg).strip().splitlines()
        self.assertEqual(len(lines), len(self.cfg.sites) * 4)
        self.assertTrue(any('1-3500,3901-4094' in line for line in lines))

    def test_summary_networks(self):
        import json
        payload = json.loads(render_summary(self.cfg))
        self.assertEqual(len(payload['networks']), 4)
        self.assertEqual(payload['networks'][0]['vlan'], 3501)


if __name__ == '__main__':
    unittest.main()
