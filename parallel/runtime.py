#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build and start the parallel-network Mininet scenario."""

from __future__ import annotations

import os
import socket

from mininet.link import TCLink
from mininet.net import Mininet

from fiti.log import Progress, ensure_log_dir, quiet_mininet_console
from fiti.p4_mininet import P4Host
from parallel.cleanup import cleanup_stale_links, other_instances
from parallel.config import ParallelConfig
from parallel.p4 import compile_p4
from parallel.switch import make_switch_factory
from parallel.topology import (ParallelTopology, a_name, c_name,
                               switch_ids)


def _port_free(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def find_free_thrift_base(base, count, step=100, attempts=20):
    """Find a contiguous Thrift port range for all PE (E) switches."""
    base = int(base)
    for _ in range(attempts):
        if base + count > 65535:
            break
        if all(_port_free(base + index) for index in range(1, count + 1)):
            return base
        base += step
    raise RuntimeError('找不到可用的 Thrift 端口段（需要 %d 个连续端口）'
                       % count)


def configure_forwarding(net, cfg):
    """Enable IPv6 forwarding on traditional routers and the public border."""
    for site in cfg.sites:
        for name in (c_name(site), a_name(site)):
            host = net.get(name)
            if host is not None:
                host.cmd('sysctl -w net.ipv6.conf.all.forwarding=1')
    border = net.get('pub_border')
    if border is not None:
        border.cmd('sysctl -w net.ipv6.conf.all.forwarding=1')


def build_parallel_net(cfg: ParallelConfig, json_path=None,
                       thrift_base=None, log_dir=None, start=True,
                       install_rules=True):
    json_path = os.path.abspath(json_path or cfg.p4_json)
    if start:
        running = other_instances()
        if running:
            raise RuntimeError(
                '检测到另一份 parallel_net.py 正在运行（PID %s），'
                '请先 make parallel-stop'
                % ', '.join(str(pid) for pid in running))
        cleanup_stale_links()
        json_path = compile_p4(out=json_path)
    base = int(thrift_base if thrift_base is not None else cfg.thrift_base)
    requested_base = base
    ids = switch_ids(cfg)
    count = max(ids.values())
    base = find_free_thrift_base(base, count)
    if base != requested_base:
        print('*** Thrift 端口段被占用，自动改用 %d～%d'
              % (base + 1, base + count))
    log_dir = ensure_log_dir(log_dir)

    topo = ParallelTopology(cfg)
    factory = make_switch_factory(
        json_path, base,
        p4_log_level=cfg.p4_log_level,
        log_dir=log_dir,
        switch_ids=ids)
    net = Mininet(topo=topo, switch=factory, host=P4Host,
                  controller=None, link=TCLink,
                  autoSetMacs=True, build=False)
    net.parallel_config = cfg
    net.p4_json = json_path
    net.thrift_base = base

    build_names = set(topo.hosts()) | set(topo.switches())
    try:
        with quiet_mininet_console(log_dir, allow_build_lists=True,
                                   allow_names=build_names):
            net.build()
        if start:
            with quiet_mininet_console(log_dir):
                with Progress('*** 启动并行网络 BMv2 交换机',
                              done='*** BMv2 交换机已启动'):
                    net.start()
            configure_forwarding(net, cfg)
            from parallel.ovs import configure_ovs_switches
            with Progress('*** 配置 S1/S2 OVS 接入交换机',
                          done='*** OVS 接入交换机配置完成'):
                configure_ovs_switches(net, cfg)
        if start and install_rules:
            from parallel.control import install_parallel_rules
            with Progress('*** 下发并行网络 P4 表项',
                          done='*** P4 表项下发完成'):
                install_parallel_rules(net, cfg, json_path)
    except (Exception, KeyboardInterrupt):
        net.stop()
        raise
    return net
