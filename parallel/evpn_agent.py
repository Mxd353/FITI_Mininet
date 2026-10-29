#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime EVPN route-update agent for the parallel-network scenario.

The agent owns a :class:`parallel.evpn.RouteTable` and translates route
advertise/withdraw events into bmv2 table operations.  It is deliberately
small: BGP/EVPN is not implemented here.  FRR/ExaBGP (or a test script) can
feed JSON updates over a Unix socket, and a later adapter can replace the
socket with a real BGP session without changing the P4 data plane.

Supported dynamic updates:

* EVPN IP Prefix routes for L3VPN (l3_fib/vrf_map; local_sid stays static);
* EVPN MAC/IP routes for known-unicast L2VPN.

Dynamic IMET/BUM group membership is not supported yet; static groups from
``parallel.control`` remain in place.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socketserver
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from parallel.bmv2 import switch_cli
from parallel.config import DEFAULT_CONFIG, ParallelConfig, load_config
from parallel.control import (_action_name, _core_next, _host_intf,
                              _host_mac, _port_maps, _site_macs)
from parallel.evpn import (EvpnIpPrefixRoute, EvpnMacIpRoute, RouteTable,
                           build_evpn_routes, validate_evpn_routes)
from parallel.p4 import runtime_names
from parallel.topology import c_name, cloud_name, e_name, s1_name

DEFAULT_EVPN_SOCKET = '/tmp/parallel_evpn.sock'

#: How many site PEs to update concurrently.  Each update is an independent
#: simple_switch_CLI process, so parallelism is safe; set
#: PARALLEL_EVPN_WORKERS=1 to fall back to sequential updates.
MAX_WORKERS = max(1, int(os.environ.get('PARALLEL_EVPN_WORKERS', '16')))


class AgentError(RuntimeError):
    pass


def _route_from_payload(payload):
    try:
        table = RouteTable.from_dict({'routes': [payload]})
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise AgentError('非法 EVPN 路由: %s' % exc)
    routes = table.values()
    if len(routes) != 1:
        raise AgentError('一次必须提交一条 EVPN 路由')
    return routes[0]


class ParallelEvpnAgent:
    """Apply EVPN RouteTable updates to the parallel-network P4 switches."""

    def __init__(self, net, cfg: ParallelConfig):
        self.net = net
        self.cfg = cfg
        self.names = runtime_names(cfg.p4_json)
        self.switch_ports = _port_maps(net, cfg)
        self.macs = _site_macs(net, cfg)
        self.table = getattr(net, 'evpn_routes', None)
        if self.table is None:
            self.table = build_evpn_routes(cfg, net)
            net.evpn_routes = self.table
        self.lock = threading.RLock()

    # ------------------------------------------------------------------
    # bmv2 command helpers
    # ------------------------------------------------------------------
    def _cmd(self, switch, table, action, keys, params=None):
        words = ['table_add', self.names['tables'][table],
                 _action_name(self.names, table, action)]
        words.extend(str(item) for item in keys)
        words.append('=>')
        if params:
            words.extend(str(item) for item in params)
        return switch, ' '.join(words)

    def _del(self, switch, table, keys):
        # simple_switch_CLI's table_delete takes an entry handle, not a key.
        return (switch, ('delete', table, tuple(keys)))

    def _delete_by_key(self, switch, table, keys):
        table_name = self.names['tables'][table]
        port = self.net.get(switch).thrift_port
        try:
            output = switch_cli(
                port, ['table_dump_entry_from_key %s %s'
                       % (table_name,
                          ' '.join(str(item) for item in keys))])
        except RuntimeError:
            # Entry does not exist; withdrawal is idempotent.
            return
        match = re.search(r'Dumping entry\s+(0x[0-9a-fA-F]+)', output)
        if match is None:
            return
        handle = int(match.group(1), 16)
        switch_cli(port, ['table_delete %s %d' % (table_name, handle)])

    def _run_switch(self, switch, commands, deletes):
        port = self.net.get(switch).thrift_port
        if commands:
            switch_cli(port, commands)
        for table, keys in deletes:
            self._delete_by_key(switch, table, keys)

    def _run(self, operations):
        commands = defaultdict(list)
        deletes = defaultdict(list)
        for switch, command in operations:
            if isinstance(command, tuple) and command[0] == 'delete':
                deletes[switch].append((command[1], command[2]))
            else:
                commands[switch].append(command)
        switches = sorted(set(commands) | set(deletes))
        if not switches:
            return
        workers = min(MAX_WORKERS, len(switches))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(self._run_switch, switch,
                            commands.get(switch, []),
                            deletes.get(switch, []))
                for switch in switches
            ]
            for future in futures:
                future.result()

    # ------------------------------------------------------------------
    # L3VPN
    # ------------------------------------------------------------------
    def _l3_e_commands(self, route, delete=False):
        network = self.cfg.network(route.vni)
        target = self.cfg.site(route.origin)
        ops = []
        for site in self.cfg.network_sites(network):
            e = e_name(site)
            if delete:
                ops.append(self._del(e, 'vrf_map', [route.prefix]))
                ops.append(self._del(e, 'l3_fib',
                                     [network.id, route.prefix]))
                # local_sid is static per (network, site) and is shared by
                # all prefixes.  Do not add/remove it for individual routes.
                continue

            ops.append(self._cmd(e, 'vrf_map', 'map_vrf',
                                 [route.prefix], [network.id]))
            if site.abbr == target.abbr:
                s1 = self.switch_ports[e][s1_name(site)]
                cloud_mac = self.cfg.cloud_mac(network, site, 1)
                ops.append(self._cmd(
                    e, 'l3_fib', 'forward_l3',
                    [network.id, route.prefix],
                    [s1, self.macs[site.abbr]['e_s1'], cloud_mac,
                     network.vlan]))
            else:
                local_route = self.table.l3_route(network.id, site.abbr)
                if local_route is None:
                    raise AgentError('站点 %s 缺少网络 %d 的本地 L3 路由'
                                     % (site.abbr, network.id))
                c = c_name(site)
                ops.append(self._cmd(
                    e, 'l3_fib', 'srv6_encap',
                    [network.id, route.prefix],
                    [local_route.sid, route.sid,
                     self.macs[site.abbr]['e_c'],
                     _host_mac(self.net.get(c), e),
                     self.switch_ports[e][c]]))
        return ops

    def _l3_c_commands(self, route, delete=False):
        """Update Linux C routes for a dynamically advertised prefix."""
        network = self.cfg.network(route.vni)
        target = self.cfg.site(route.origin)
        for site in self.cfg.network_sites(network):
            c = self.net.get(c_name(site))
            if site.abbr == target.abbr:
                intf = _host_intf(c, e_name(site))
                if delete:
                    c.cmd('ip -6 route del %s' % route.prefix)
                else:
                    c.cmd('ip -6 route replace %s via fd00:0:%d:1::2 dev %s'
                          % (route.prefix, site.id, intf.name))
            else:
                next_hop, intf = _core_next(
                    self.net, self.cfg, site, target)
                if delete:
                    c.cmd('ip -6 route del %s' % route.prefix)
                else:
                    c.cmd('ip -6 route replace %s via %s dev %s'
                          % (route.prefix, next_hop, intf.name))

    def _apply_l3(self, route, delete=False):
        if not isinstance(route, EvpnIpPrefixRoute):
            raise AgentError('L3 更新需要 IP Prefix 路由')
        ops = self._l3_e_commands(route, delete=delete)
        self._run(ops)
        self._l3_c_commands(route, delete=delete)

    # ------------------------------------------------------------------
    # L2VPN known unicast
    # ------------------------------------------------------------------
    def _l2_commands(self, route, delete=False):
        network = self.cfg.network(route.vni)
        target = self.cfg.site(route.origin)
        ops = []
        for site in self.cfg.network_sites(network):
            e = e_name(site)
            e_ports = self.switch_ports[e]
            cloud_hosts = [self.net.get(cloud_name(site, host_id))
                           for host_id in self.cfg.host_ids()]

            if site.abbr == target.abbr:
                # S1/S2 are OVS bridges and learn the local MAC themselves.
                continue

            local_route = self.table.imet_route(network.id, site.abbr)
            if local_route is None:
                raise AgentError('站点 %s 缺少网络 %d 的 L2 IMET 路由'
                                 % (site.abbr, network.id))
            if delete:
                ops.append(self._del(e, 'l2_tunnel',
                                     [network.vlan, route.mac]))
                for cloud in cloud_hosts:
                    cloud.cmd('ip -6 neigh del %s dev %s'
                              % (route.ip, self.cfg.vlan_ifname(network)))
            else:
                c = c_name(site)
                ops.append(self._cmd(
                    e, 'l2_tunnel', 'srv6_l2_encap',
                    [network.vlan, route.mac],
                    [local_route.sid, route.sid,
                     self.macs[site.abbr]['e_c'],
                     _host_mac(self.net.get(c), e), e_ports[c]]))
                for cloud in cloud_hosts:
                    cloud.cmd(
                        'ip -6 neigh replace %s lladdr %s dev %s nud permanent'
                        % (route.ip, route.mac,
                           self.cfg.vlan_ifname(network)))
        return ops

    def _apply_l2(self, route, delete=False):
        if not isinstance(route, EvpnMacIpRoute):
            raise AgentError('L2 已知单播更新需要 MAC/IP 路由')
        ops = self._l2_commands(route, delete=delete)
        self._run(ops)

    # ------------------------------------------------------------------
    # public update API
    # ------------------------------------------------------------------
    def advertise(self, payload):
        route = _route_from_payload(payload)
        if not isinstance(route, (EvpnIpPrefixRoute, EvpnMacIpRoute)):
            raise AgentError('动态 IMET/BUM 路由更新暂不支持')
        with self.lock:
            old = self.table.routes.get(route.key())
            # Validate against the tentative table before touching P4.
            self.table.advertise(route)
            errors = validate_evpn_routes(self.table)
            if errors:
                if old is None:
                    self.table.routes.pop(route.key(), None)
                else:
                    self.table.routes[route.key()] = old
                raise AgentError('EVPN 路由校验失败: %s' % '; '.join(errors))
            try:
                if old is not None:
                    self._apply(old, delete=True)
                self._apply(route, delete=False)
            except Exception:
                if old is None:
                    self.table.routes.pop(route.key(), None)
                else:
                    self.table.routes[route.key()] = old
                raise
            return {'action': 'replace' if old is not None else 'add',
                    'route': route.to_dict()}

    def withdraw(self, payload):
        route = _route_from_payload(payload)
        if not isinstance(route, (EvpnIpPrefixRoute, EvpnMacIpRoute)):
            raise AgentError('动态 IMET/BUM 路由更新暂不支持')
        with self.lock:
            update = self.table.withdraw(route)
            if update is None:
                raise AgentError('路由不存在: %s' % (route.key(),))
            try:
                self._apply(update.route, delete=True)
            except Exception:
                self.table.advertise(update.route)
                raise
            return {'action': 'remove', 'route': update.route.to_dict()}

    def _apply(self, route, delete=False):
        if isinstance(route, EvpnIpPrefixRoute):
            self._apply_l3(route, delete=delete)
        elif isinstance(route, EvpnMacIpRoute):
            self._apply_l2(route, delete=delete)
        else:
            raise AgentError('不支持的 EVPN 路由类型')

    def dump(self):
        with self.lock:
            return self.table.to_dict()

    def status(self):
        with self.lock:
            return {
                'routes': len(self.table.routes),
                'l3_routes': len(self.table.l3_routes()),
                'l2_imet': len(self.table.imet_routes()),
                'l2_mac': len(self.table.mac_ip_routes()),
                'socket_sites': [site.abbr for site in self.cfg.sites],
            }

    def handle(self, request):
        if not isinstance(request, dict):
            raise AgentError('请求必须是 JSON 对象')
        cmd = str(request.get('cmd', ''))
        if cmd == 'advertise':
            return self.advertise(request.get('route'))
        if cmd == 'withdraw':
            return self.withdraw(request.get('route'))
        if cmd == 'dump':
            return self.dump()
        if cmd == 'status':
            return self.status()
        raise AgentError('未知命令: %s' % cmd)


class _EvpnUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, agent):
        self.agent = agent
        super().__init__(path, _EvpnHandler)


class _EvpnHandler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline()
        try:
            request = json.loads(line.decode('utf-8'))
            result = self.server.agent.handle(request)
            response = {'ok': True, 'result': result}
        except Exception as exc:
            response = {'ok': False, 'error': str(exc)}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + '\n')
                         .encode('utf-8'))


def start_evpn_agent(net, cfg, socket_path=DEFAULT_EVPN_SOCKET):
    agent = ParallelEvpnAgent(net, cfg)
    path = os.path.abspath(socket_path)
    if os.path.exists(path):
        os.unlink(path)
    server = _EvpnUnixServer(path, agent)
    os.chmod(path, 0o666)
    thread = threading.Thread(target=server.serve_forever,
                              name='parallel-evpn-agent', daemon=True)
    thread.start()
    agent.server = server
    agent.thread = thread
    agent.socket_path = path
    return agent


def stop_evpn_agent(agent):
    if agent is None:
        return
    server = getattr(agent, 'server', None)
    if server is not None:
        server.shutdown()
        server.server_close()
    path = getattr(agent, 'socket_path', None)
    if path and os.path.exists(path):
        os.unlink(path)


def main(argv=None):
    # A standalone server cannot attach to a running Mininet netns.  This
    # entry point is intentionally only a reminder/help message.
    parser = argparse.ArgumentParser(
        description='并行网络 EVPN 动态路由 agent（需在 parallel_net.py 内启动）')
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    parser.add_argument('--socket', default=DEFAULT_EVPN_SOCKET)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    raise SystemExit(
        'agent 必须在已启动的 Mininet 进程内运行；请使用 '
        'parallel_net.py --evpn-socket %s（配置 %s）'
        % (args.socket, cfg.path))


if __name__ == '__main__':
    raise SystemExit(main())
