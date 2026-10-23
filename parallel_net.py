#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中关村主干网并行网络仿真 CLI。

用法:
  python3 parallel_net.py --dump
  sudo python3 parallel_net.py
  sudo python3 parallel_net.py --no-cli
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from mininet.cli import CLI
from mininet.log import setLogLevel

from fiti.log import ensure_log_dir
from parallel.config import DEFAULT_CONFIG, dump_config, load_config
from parallel.topology import ParallelTopology


def _dump(cfg):
    dump_config(cfg)
    topo = ParallelTopology(cfg)
    print()
    print('交换机: %s' % ' '.join(sorted(topo.switches())))
    print('主机: %s' % ' '.join(sorted(topo.hosts())))
    print('链路:')
    for link in topo.links():
        print('  %s - %s' % (link[0], link[1]))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='中关村主干网并行网络 Mininet/BMv2 仿真')
    parser.add_argument('--config', default=DEFAULT_CONFIG,
                        help='并行网络配置 (默认: %(default)s)')
    parser.add_argument('--dump', action='store_true',
                        help='只打印拓扑和地址规划，不启动 Mininet')
    parser.add_argument('--no-cli', action='store_true',
                        help='启动后不进入 Mininet CLI')
    parser.add_argument('--no-install', action='store_true',
                        help='只启动拓扑，不下发 P4 表项')
    parser.add_argument('--evpn-socket', default='/tmp/parallel_evpn.sock',
                        help='EVPN 动态路由 agent 的 Unix socket')
    parser.add_argument('--no-evpn-agent', action='store_true',
                        help='不启动 EVPN 动态路由 agent')
    parser.add_argument('--p4-json', default=None,
                        help='P4 编译产物路径')
    parser.add_argument('--thrift-base', type=int, default=None,
                        help='Thrift 端口基址')
    parser.add_argument('--log-dir', default=None,
                        help='bmv2 日志目录')
    parser.add_argument('--log', default='info',
                        choices=('debug', 'info', 'warning', 'error',
                                 'critical'))
    args = parser.parse_args(argv)
    setLogLevel(args.log)

    cfg = load_config(args.config)
    if args.dump:
        _dump(cfg)
        return 0

    if os.geteuid() != 0:
        raise SystemExit('Mininet/BMv2 需要 root 权限，请使用 sudo 运行')

    from parallel.runtime import build_parallel_net

    log_dir = ensure_log_dir(args.log_dir)
    net = None
    evpn_agent = None
    try:
        net = build_parallel_net(
            cfg,
            json_path=args.p4_json,
            thrift_base=args.thrift_base,
            log_dir=log_dir,
            install_rules=not args.no_install)
        print('*** 并行网络已启动')
        print('*** 交换机: %s' % ' '.join(sorted(sw.name for sw in net.switches)))
        if not args.no_install and not args.no_evpn_agent:
            from parallel.evpn_agent import start_evpn_agent
            evpn_agent = start_evpn_agent(net, cfg, args.evpn_socket)
            print('*** EVPN agent: %s' % evpn_agent.socket_path)
        if args.no_cli:
            while True:
                time.sleep(1)
        CLI(net)
    finally:
        if evpn_agent is not None:
            from parallel.evpn_agent import stop_evpn_agent
            stop_evpn_agent(evpn_agent)
        if net is not None:
            net.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
