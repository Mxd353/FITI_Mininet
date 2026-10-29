#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Functional verification runner for the parallel-network scenario.

Usage:
  python3 verify/runner.py --config configs/parallel-min.json --dry-run
  sudo python3 verify/runner.py --config configs/parallel-min.json
  sudo python3 verify/runner.py --config configs/parallel.json --tag core
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parallel.bmv2 import switch_cli
from parallel.config import load_config
from parallel.ovs import intf_to, trunk_vlans
from parallel.topology import switch_id
from verify.report import (Result, StepDetail, print_summary, write_evidence,
                           write_json, write_junit)
from verify.spec import SpecError, load_cases, summarize

DEFAULT_CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'cases')
DEFAULT_RUNS = 'runs'
LOSS_RE = re.compile(r'([\d.]+)% packet loss')
RECEIVED_RE = re.compile(r'(\d+) received')


class StepError(AssertionError):
    pass


def _ping_command(iface, target, count, timeout=1):
    parts = ['ping6', '-c', str(count), '-W', str(timeout)]
    if iface:
        parts.extend(['-I', iface])
    parts.append(target)
    return ' '.join(parts)


def parse_ping(output):
    loss = LOSS_RE.search(output)
    received = RECEIVED_RE.search(output)
    return (float(loss.group(1)) if loss else None,
            int(received.group(1)) if received else 0)


class CaseRunner(object):
    def __init__(self, net, cfg, agent=None, evidence=True):
        self.net = net
        self.cfg = cfg
        self.agent = agent
        self.evidence = evidence

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _node(self, name):
        node = self.net.get(name)
        if node is None:
            raise StepError('找不到节点: %s' % name)
        return node

    def _switch(self, name):
        return self._node(name)

    def _thrift(self, switch_name):
        abbr = switch_name.split('_', 1)[1]
        site = self.cfg.site(abbr)
        return int(self.net.thrift_base) + switch_id(site, 'E')

    def _intf_to(self, node, peer):
        return intf_to(node, peer)

    # ------------------------------------------------------------------
    # steps
    # ------------------------------------------------------------------
    def run_step(self, step):
        kind = step['type']
        handler = getattr(self, '_step_%s' % kind, None)
        if handler is None:
            raise StepError('未实现的步骤类型: %s' % kind)
        return handler(step)

    def _step_ping(self, step):
        node = self._node(step['node'])
        iface = step.get('iface')
        target = step['target']
        count = int(step.get('count', 3))
        # Warm up ND/FDB so the measured run is not biased by first-packet loss.
        node.cmd(_ping_command(iface, target, 1))
        output = node.cmd(_ping_command(iface, target, count))
        loss, received = parse_ping(output)
        if loss is None:
            raise StepError('无法解析 ping 输出')
        expect = step.get('expect', 'pass')
        if expect == 'fail':
            if loss < 100:
                raise StepError('期望 100%% 丢包，实际 %.0f%%（收到 %d）'
                                % (loss, received))
        else:
            max_loss = float(step.get('max_loss', 0))
            min_received = int(step.get('min_received', 1))
            if loss > max_loss or received < min_received:
                raise StepError('期望可达，实际丢包 %.0f%%，收到 %d/%d'
                                % (loss, received, count))
        return output.strip()

    def _step_exec(self, step):
        node = self._node(step['node'])
        output = node.cmd(step['command'])
        if 'expect_contains' in step and step['expect_contains'] not in output:
            raise StepError('输出不包含 %r' % step['expect_contains'])
        if ('expect_not_contains' in step
                and step['expect_not_contains'] in output):
            raise StepError('输出不应包含 %r' % step['expect_not_contains'])
        return output.strip()

    def _step_ovs_trunks(self, step):
        node = self._switch(step['switch'])
        intf = self._intf_to(node, step['peer'])
        vlans = trunk_vlans(node, intf)
        for vlan in step.get('contains', []):
            if int(vlan) not in vlans:
                raise StepError('%s %s 缺少 VLAN %s（trunks=%d 个）'
                                % (node.name, intf.name, vlan, len(vlans)))
        for vlan in step.get('not_contains', []):
            if int(vlan) in vlans:
                raise StepError('%s %s 仍然放行 VLAN %s'
                                % (node.name, intf.name, vlan))
        return '%s %s: %d VLANs' % (node.name, intf.name, len(vlans))

    def _step_p4_entry(self, step):
        switch = step['switch']
        table = step['table']
        keys = ' '.join(str(key) for key in step['keys'])
        command = 'table_dump_entry_from_key %s %s' % (table, keys)
        try:
            output = switch_cli(self._thrift(switch), [command])
            found = True
        except RuntimeError as exc:
            output = str(exc)
            found = False
        present = bool(step.get('present', True))
        if found != present:
            raise StepError('%s %s 表项 %s %s'
                            % (switch, table, keys,
                               '不存在' if present else '仍然存在'))
        return output.strip()

    def _step_linux_route(self, step):
        node = self._node(step['node'])
        table = step.get('table')
        command = 'ip -6 route show'
        if table is not None:
            command += ' table %s' % table
        output = node.cmd(command)
        present = step['match'] in output
        if present != bool(step.get('present', True)):
            raise StepError('%s 路由表 %s 中%s %r'
                            % (node.name, table if table is not None else 'main',
                               '存在' if present else '缺少', step['match']))
        return output.strip()

    def _step_evpn_advertise(self, step):
        if self.agent is None:
            raise StepError('EVPN agent 未启动')
        return json.dumps(self.agent.advertise(step['route']),
                          ensure_ascii=False)

    def _step_evpn_withdraw(self, step):
        if self.agent is None:
            raise StepError('EVPN agent 未启动')
        return json.dumps(self.agent.withdraw(step['route']),
                          ensure_ascii=False)

    def _step_link(self, step):
        node = self._node(step['node'])
        intf = self._intf_to(node, step['peer'])
        node.cmd('ip link set dev %s %s' % (intf.name, step['action']))
        return '%s %s' % (intf.name, step['action'])

    def _step_sleep(self, step):
        seconds = float(step['seconds'])
        time.sleep(seconds)
        return 'sleep %.1fs' % seconds

    # ------------------------------------------------------------------
    # case execution
    # ------------------------------------------------------------------
    def run_case(self, case):
        result = Result(name=case.name)
        start = time.time()
        try:
            for index, step in enumerate(case.steps, 1):
                detail_start = time.time()
                try:
                    output = self.run_step(step)
                    detail = StepDetail(index, step['type'], 'pass',
                                        time.time() - detail_start,
                                        output=output or '')
                except Exception as exc:  # noqa: BLE001 - report every failure
                    detail = StepDetail(index, step['type'], 'fail',
                                        time.time() - detail_start,
                                        message=str(exc),
                                        output=getattr(exc, 'output', '')
                                        or str(exc))
                result.add(detail)
                if detail.status == 'fail':
                    break
        finally:
            for index, step in enumerate(case.teardown, 1):
                detail_start = time.time()
                try:
                    output = self.run_step(step)
                    result.details.append(StepDetail(
                        index, 'teardown:%s' % step['type'], 'pass',
                        time.time() - detail_start, output=output or ''))
                except Exception as exc:  # noqa: BLE001
                    result.status = 'fail'
                    result.details.append(StepDetail(
                        index, 'teardown:%s' % step['type'], 'fail',
                        time.time() - detail_start, message=str(exc)))
                    if not result.reason:
                        result.reason = 'teardown 失败: %s' % exc
        result.seconds = time.time() - start
        if result.status != 'fail':
            result.status = 'pass'
        return result

    def collect_evidence(self, case):
        lines = []
        switches = sorted({step.get('switch') for step in case.steps
                           if step.get('switch')})
        for name in switches:
            port = None
            try:
                port = self._thrift(name)
            except (KeyError, ValueError, IndexError):
                continue
            for table in ('l3_fib', 'local_sid', 'l2_tunnel', 'vlan_map'):
                try:
                    output = switch_cli(port, ['table_dump %s' % table])
                    lines.append('=== %s %s ===' % (name, table))
                    lines.append(output.strip())
                except RuntimeError as exc:
                    lines.append('=== %s %s 失败: %s' % (name, table, exc))
        nodes = sorted({step.get('node') for step in case.steps
                        if step.get('node')})
        for name in nodes:
            node = self.net.get(name)
            if node is None:
                continue
            for command in ('ip -6 route show', 'ip -6 rule show',
                            'ip -6 addr show'):
                lines.append('=== %s: %s ===' % (name, command))
                lines.append(node.cmd(command).strip())
        return '\n'.join(lines)


def _run(args, cfg, cases):
    if os.geteuid() != 0:
        raise SystemExit('功能验证会启动 Mininet，需要用 sudo 运行')

    from fiti.log import ensure_log_dir
    from parallel.evpn_agent import start_evpn_agent, stop_evpn_agent
    from parallel.runtime import build_parallel_net

    log_dir = ensure_log_dir(args.log_dir)
    net = None
    agent = None
    results = []
    try:
        net = build_parallel_net(cfg, thrift_base=args.thrift_base,
                                 log_dir=log_dir, install_rules=True)
        if not args.no_agent:
            agent = start_evpn_agent(net, cfg, args.evpn_socket)
        runner = CaseRunner(net, cfg, agent)
        available = {site.abbr for site in cfg.sites}
        for case in cases:
            missing = [site for site in case.requires_sites
                       if site not in available]
            if missing:
                results.append(Result(
                    name=case.name, status='skip',
                    reason='缺少站点: %s' % ','.join(missing)))
                continue
            result = runner.run_case(case)
            if result.status == 'fail' and args.evidence:
                evidence = runner.collect_evidence(case)
                if evidence:
                    result.details.append(StepDetail(
                        0, 'evidence', 'pass', 0.0, output=evidence))
            results.append(result)
    finally:
        if agent is not None:
            stop_evpn_agent(agent)
        if net is not None:
            net.stop()
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='并行网络功能验证')
    parser.add_argument('--config', default='configs/parallel-min.json')
    parser.add_argument('--cases', default=DEFAULT_CASES,
                        help='用例文件或目录（默认 verify/cases）')
    parser.add_argument('--tag', action='append', default=None,
                        help='只跑带该标签的用例，可重复')
    parser.add_argument('--all', action='store_true',
                        help='包含默认跳过的故障用例')
    parser.add_argument('--list', action='store_true', help='列出用例')
    parser.add_argument('--dry-run', action='store_true',
                        help='只校验用例和配置，不启动 Mininet')
    parser.add_argument('--runs-dir', default=DEFAULT_RUNS)
    parser.add_argument('--no-evidence', dest='evidence', action='store_false',
                        help='失败时不 dump 表项和路由')
    parser.add_argument('--no-agent', action='store_true',
                        help='不启动 EVPN agent')
    parser.add_argument('--thrift-base', type=int, default=None)
    parser.add_argument('--log-dir', default=None)
    parser.add_argument('--evpn-socket', default='/tmp/parallel_evpn.sock')
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    try:
        cases = load_cases(args.cases, args.tag, include_faults=args.all)
    except SpecError as exc:
        raise SystemExit('用例错误: %s' % exc)

    if args.list or args.dry_run:
        rows = summarize(cases, [site.abbr for site in cfg.sites])
        width = max(len(row['name']) for row in rows) if rows else 10
        for row in rows:
            print('%-6s %-*s %-12s %s'
                  % (row['status'], width, row['name'], row['tags'],
                     row['reason'] or row['source']))
        print('\n共 %d 条用例，配置 %s' % (len(cases), cfg.path))
        return 0

    results = _run(args, cfg, cases)
    print('')
    print_summary(results)

    stamp = time.strftime('%Y%m%d-%H%M%S')
    run_dir = os.path.join(args.runs_dir, stamp)
    write_json(results, os.path.join(run_dir, 'report.json'))
    write_junit(results, os.path.join(run_dir, 'report.xml'))
    if args.evidence:
        write_evidence(results, os.path.join(run_dir, 'evidence'))
    print('\n报告: %s' % run_dir)
    return 1 if any(result.is_failure for result in results) else 0


if __name__ == '__main__':
    sys.exit(main())
