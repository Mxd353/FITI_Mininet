#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bmv2 control plane for the FITI topology.

The data plane is ``p4src/fiti_slice.p4``: a VLAN-aware L2 switch with
per-port access/trunk configuration.  This module:

* compiles the P4 program with ``p4c-bm2-ss``;
* derives a spanning tree from ``fiti.json`` (bmv2 has no STP);
* maps Mininet interfaces to bmv2 port numbers;
* installs ``port_cfg`` / ``l2_fwd`` entries and one multicast group per
  VLAN on every switch, using the ``simple_switch`` Thrift runtime.

No P4Runtime / gRPC controller is required.
"""

import json
import os
import re
import shutil
import socket
import subprocess
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from fiti_log import open_detail_log
from p4_mininet import P4Switch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_P4_SRC = os.path.join(SCRIPT_DIR, 'p4src', 'fiti_slice.p4')
DEFAULT_P4_JSON = os.path.join(SCRIPT_DIR, 'build', 'fiti_slice.json')
DEFAULT_THRIFT_BASE = 9090
P4C = 'p4c-bm2-ss'
# bmv2 的 --log-level 取值（注意是 warn，不是 warning）
P4_LOG_LEVELS = ('trace', 'debug', 'info', 'warn', 'error', 'off')


# --------------------------------------------------------------------------
# Naming helpers (kept independent of fiti_topo to avoid circular imports)
# --------------------------------------------------------------------------

def switch_name(node_id):
    return 's%s' % node_id


def _switch_id(name):
    return int(str(name)[1:])


# --------------------------------------------------------------------------
# P4 compilation and runtime name resolution
# --------------------------------------------------------------------------

def compile_p4(src=None, out=None, force=False):
    """Compile the P4 program to bmv2 JSON if missing or out of date."""
    src = os.path.abspath(src or DEFAULT_P4_SRC)
    out = os.path.abspath(out or DEFAULT_P4_JSON)
    if (not force and os.path.exists(out)
            and os.path.getmtime(out) >= os.path.getmtime(src)):
        return out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cmd = [P4C, '--arch', 'v1model', '-o', out, src]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError('找不到 %s，请先安装 p4c 并确认它在 PATH 中'
                           % P4C)
    if result.returncode != 0:
        raise RuntimeError('P4 编译失败:\n%s\n%s'
                           % (result.stdout, result.stderr))
    return out


def _pick_table(tables, short):
    for name in tables:
        if name == short or name.endswith('.' + short):
            return name
    raise RuntimeError('P4 编译结果中找不到表 %s，现有表: %s'
                       % (short, ', '.join(sorted(tables))))


def runtime_names(json_path=None):
    """Return p4c-prefixed runtime names (``IngressImpl.port_cfg`` ...)."""
    json_path = json_path or DEFAULT_P4_JSON
    with open(json_path, encoding='utf-8') as handle:
        program = json.load(handle)
    tables = [table['name'] for pipeline in program.get('pipelines', [])
              for table in pipeline.get('tables', [])]
    actions = {action['name'] for action in program.get('actions', [])}

    port_cfg = _pick_table(tables, 'port_cfg')
    l2_fwd = _pick_table(tables, 'l2_fwd')
    egress_port = _pick_table(tables, 'egress_port')
    prefix = port_cfg.rsplit('.', 1)[0] if '.' in port_cfg else ''
    egress_prefix = egress_port.rsplit('.', 1)[0] if '.' in egress_port else ''

    def action(control_prefix, short):
        name = ('%s.%s' % (control_prefix, short)) if control_prefix else short
        if name in actions:
            return name
        for candidate in actions:
            if candidate.endswith('.' + short):
                return candidate
        raise RuntimeError('P4 编译结果中找不到动作 %s，现有动作: %s'
                           % (short, ', '.join(sorted(actions))))

    return {
        'port_cfg': port_cfg,
        'l2_fwd': l2_fwd,
        'egress_port': egress_port,
        'set_access': action(prefix, 'set_access'),
        'set_trunk': action(prefix, 'set_trunk'),
        'drop': action(prefix, 'drop'),
        'forward': action(prefix, 'forward'),
        'egress_set_access': action(egress_prefix, 'set_access'),
        'egress_set_trunk': action(egress_prefix, 'set_trunk'),
    }


# --------------------------------------------------------------------------
# Thrift CLI helpers
# --------------------------------------------------------------------------

def _cli_path():
    return shutil.which('simple_switch_CLI') or 'simple_switch_CLI'


def switch_cli(thrift_port, commands):
    """Feed runtime commands to simple_switch_CLI and return its output."""
    commands = list(commands)
    if not commands:
        return ''
    try:
        proc = subprocess.run([_cli_path(), '--thrift-port', str(thrift_port)],
                              input='\n'.join(commands) + '\n',
                              capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError('找不到 simple_switch_CLI，请先安装 bmv2')
    output = proc.stdout + proc.stderr
    if proc.returncode != 0 or 'Error' in output or 'Invalid' in output:
        raise RuntimeError('Thrift 端口 %d 的运行时命令失败:\n%s'
                           % (thrift_port, output.strip()))
    return output


def _mc_handles(output):
    # Only match the multicast-node creation lines; table_add also prints
    # "Entry has been added with handle N".
    return [int(value) for value in
            re.findall(r'node was created with handle\s+(\d+)', output)]


# --------------------------------------------------------------------------
# Mininet port mapping
# --------------------------------------------------------------------------

def build_port_maps(net, data):
    """Map bmv2 ports to peers.

    Returns:
      port_peer:    {(switch_name, port): peer_node_name}
      switch_ports: {switch_name: {port: peer_switch_name}}
      host_port:    {host_name: (switch_name, port)}
    """
    switch_names = {switch_name(node['id']) for node in data['nodes']}
    port_peer = {}
    switch_ports = {name: {} for name in switch_names}
    host_port = {}

    for name in sorted(switch_names, key=_switch_id):
        switch = net.get(name)
        if switch is None:
            raise RuntimeError('Mininet 中找不到交换机 %s' % name)
        for port, intf in switch.intfs.items():
            port = int(port)
            if port == 0 or intf is None or intf.link is None:
                continue
            link = intf.link
            peer_intf = link.intf2 if link.intf1 is intf else link.intf1
            if peer_intf is None:
                continue
            peer = peer_intf.node
            port_peer[(name, port)] = peer.name
            if peer.name in switch_names:
                switch_ports[name][port] = peer.name
            else:
                host_port[peer.name] = (name, port)

    return port_peer, switch_ports, host_port


# --------------------------------------------------------------------------
# Spanning tree
# --------------------------------------------------------------------------

@dataclass
class SpanningTree:
    root: str
    parent: Dict[str, Optional[str]]
    children: Dict[str, List[str]]
    depth: Dict[str, int]
    edges: Set[Tuple[str, str]]
    order: List[str]


def compute_spanning_tree(data, root=None):
    """BFS spanning tree over switch nodes (bmv2 has no STP)."""
    names = [switch_name(node['id']) for node in data['nodes']]
    names.sort(key=_switch_id)
    if not names:
        raise ValueError('拓扑没有节点')

    adjacency = {name: [] for name in names}
    for link in data['links']:
        src = switch_name(link['src'])
        dst = switch_name(link['dst'])
        if src not in adjacency or dst not in adjacency:
            raise ValueError('链路引用了不存在的节点: %s - %s' % (src, dst))
        adjacency[src].append(dst)
        adjacency[dst].append(src)
    for name in adjacency:
        adjacency[name].sort(key=_switch_id)

    root = switch_name(root) if root is not None else names[0]
    if root not in adjacency:
        raise ValueError('生成树根节点不存在: %s' % root)

    parent = {root: None}
    depth = {root: 0}
    order = [root]
    queue = deque([root])
    while queue:
        current = queue.popleft()
        for neighbor in adjacency[current]:
            if neighbor in parent:
                continue
            parent[neighbor] = current
            depth[neighbor] = depth[current] + 1
            order.append(neighbor)
            queue.append(neighbor)

    if len(parent) != len(names):
        missing = sorted(set(names) - set(parent), key=_switch_id)
        raise ValueError('拓扑不连通，无法建立生成树: %s' % ', '.join(missing))

    children = {name: [] for name in names}
    for name, par in parent.items():
        if par is not None:
            children[par].append(name)
    edges = {tuple(sorted((name, par))) for name, par in parent.items()
             if par is not None}

    return SpanningTree(root=root, parent=parent, children=children,
                        depth=depth, edges=edges, order=order)


def next_hop_on_tree(tree, src, dst):
    """First switch after ``src`` on the tree path to ``dst``."""
    if src == dst:
        return None
    path = []
    current = dst
    while current is not None:
        path.append(current)
        current = tree.parent[current]
    path.reverse()
    if src in path:
        index = path.index(src)
        if index + 1 < len(path):
            return path[index + 1]
        return None
    return tree.parent[src]


# --------------------------------------------------------------------------
# Rule planning / installation
# --------------------------------------------------------------------------

@dataclass
class SwitchPlan:
    name: str
    access_ports: List[Tuple[int, int]] = field(default_factory=list)
    trunk_ports: List[int] = field(default_factory=list)
    l2_entries: List[Tuple[int, str, int]] = field(default_factory=list)
    groups: Dict[int, List[int]] = field(default_factory=dict)


@dataclass
class FabricInfo:
    json_path: str
    tree: SpanningTree
    host_plan: Dict[str, dict]
    port_peer: Dict[Tuple[str, int], str]
    switch_ports: Dict[str, Dict[int, str]]
    host_port: Dict[str, Tuple[str, int]]
    plans: Dict[str, SwitchPlan]
    thrift_ports: Dict[str, int]


def build_rule_plan(data, host_plan, switch_ports, host_port, tree, macs):
    """Compute per-switch port/VLAN/unicast/flood rules (pure Python)."""
    switch_names = sorted(host_plan_switches(data, host_plan),
                          key=_switch_id)
    vlans = sorted({int(meta['vlan']) for meta in host_plan.values()})
    if not vlans:
        raise ValueError('主机规划为空，无法生成 P4 规则')

    for host, meta in host_plan.items():
        if host not in host_port:
            raise RuntimeError('主机 %s 没有对应的交换机端口' % host)
        if host not in macs:
            raise RuntimeError('主机 %s 缺少 MAC 地址' % host)
        if int(meta['vlan']) not in vlans:
            raise RuntimeError('主机 %s 的 VLAN 非法' % host)

    plans = {}
    for name in switch_names:
        plan = SwitchPlan(name=name)
        peers = switch_ports.get(name, {})

        # Access ports: one per host in the host plan.
        local_hosts = [host for host, (sw, _) in host_port.items()
                       if sw == name and host in host_plan]
        for host in sorted(local_hosts):
            plan.access_ports.append((host_port[host][1],
                                      int(host_plan[host]['vlan'])))
        plan.access_ports.sort()

        # Trunk ports: only spanning-tree links are active.  Ports on
        # non-tree links are left unconfigured, so port_cfg's default drop
        # blocks them.
        for port, peer in sorted(peers.items()):
            edge = tuple(sorted((name, peer)))
            if edge in tree.edges:
                plan.trunk_ports.append(port)
        plan.trunk_ports.sort()

        # One multicast group per VLAN, containing every active trunk port
        # plus the local access ports of that VLAN.  The egress pipeline
        # removes the copy that would return to the ingress port.
        for vlan in vlans:
            ports = list(plan.trunk_ports)
            ports += [port for port, access_vlan in plan.access_ports
                      if access_vlan == vlan]
            ports = sorted(set(ports))
            if ports:
                plan.groups[vlan] = ports

        # Static unicast entries: (VLAN, dst MAC) -> egress port.
        for host, meta in host_plan.items():
            vlan = int(meta['vlan'])
            dst_switch = meta['switch']
            if name == dst_switch:
                egress = host_port[host][1]
            else:
                nxt = next_hop_on_tree(tree, name, dst_switch)
                if nxt is None:
                    raise RuntimeError('生成树中 %s -> %s 没有下一跳'
                                       % (name, dst_switch))
                egress = None
                for port, peer in peers.items():
                    if peer == nxt:
                        egress = port
                        break
                if egress is None:
                    raise RuntimeError('%s 没有通往 %s 的端口' % (name, nxt))
            plan.l2_entries.append((vlan, macs[host], egress))

        plans[name] = plan

    return plans


def host_plan_switches(data, host_plan):
    """Switch names that exist in the topology (used for rule planning)."""
    known = {switch_name(node['id']) for node in data['nodes']}
    for meta in host_plan.values():
        if meta['switch'] not in known:
            raise ValueError('主机规划引用了未知交换机: %s' % meta['switch'])
    return known


def install_fabric(net, data, host_plan, json_path=None, thrift_ports=None,
                   root=None, log=None, log_dir=None, progress=None):
    """Install port_cfg, l2_fwd and multicast rules on every bmv2 switch.

    明细写入 ``<log_dir>/fiti-install.log``（默认 build/logs/），
    控制台默认不逐台打印；需要时传入 ``log=print`` 可恢复逐台输出。
    """
    json_path = os.path.abspath(json_path or DEFAULT_P4_JSON)
    names = runtime_names(json_path)
    port_peer, switch_ports, host_port = build_port_maps(net, data)
    tree = compute_spanning_tree(data, root=root)
    macs = {}
    for host in host_plan:
        node = net.get(host)
        if node is None:
            raise RuntimeError('Mininet 中找不到主机 %s' % host)
        macs[host] = node.MAC()

    plans = build_rule_plan(data, host_plan, switch_ports, host_port,
                            tree, macs)

    if thrift_ports is None:
        thrift_ports = {}
        for name in plans:
            switch = net.get(name)
            thrift_ports[name] = switch.thrift_port

    detail = open_detail_log(log_dir)
    if detail:
        detail.write('*** 下发 bmv2 流表 (端口 / MAC / VLAN 泛洪组)...\n')
        detail.flush()

    success = False
    try:
        for name in sorted(plans, key=_switch_id):
            plan = plans[name]
            commands = []
            for port, vlan in plan.access_ports:
                commands.append('table_add %s %s %d => %d'
                                % (names['port_cfg'], names['set_access'],
                                   port, vlan))
                commands.append('table_add %s %s %d =>'
                                % (names['egress_port'],
                                   names['egress_set_access'], port))
            for port in plan.trunk_ports:
                # simple_switch_CLI requires the "=>" separator even when the
                # action has no runtime parameters.
                commands.append('table_add %s %s %d =>'
                                % (names['port_cfg'], names['set_trunk'],
                                   port))
                commands.append('table_add %s %s %d =>'
                                % (names['egress_port'],
                                   names['egress_set_trunk'], port))
            for vlan, mac, egress in plan.l2_entries:
                commands.append('table_add %s %s %d %s => %d'
                                % (names['l2_fwd'], names['forward'],
                                   vlan, mac, egress))

            group_vlans = []
            for vlan, ports in sorted(plan.groups.items()):
                commands.append('mc_mgrp_create %d' % vlan)
                commands.append('mc_node_create %d %s'
                                % (vlan,
                                   ' '.join(str(port) for port in ports)))
                group_vlans.append(vlan)

            output = switch_cli(thrift_ports[name], commands)
            handles = _mc_handles(output)
            if len(handles) != len(group_vlans):
                raise RuntimeError(
                    '%s: 创建了 %d 个组但解析到 %d 个 node handle\n%s'
                    % (name, len(group_vlans), len(handles), output.strip()))
            associations = ['mc_node_associate %d %d' % (vlan, handle)
                            for vlan, handle in zip(group_vlans, handles)]
            switch_cli(thrift_ports[name], associations)

            line = ('  %s: 接入端口 %d, trunk 端口 %d, 单播表项 %d, VLAN 组 %d'
                    % (name, len(plan.access_ports), len(plan.trunk_ports),
                       len(plan.l2_entries), len(group_vlans)))
            if detail:
                detail.write(line + '\n')
                detail.flush()
            if log:
                log(line)
            if progress is not None:
                progress.advance(1, name)
        success = True
    finally:
        if detail:
            detail.write('*** P4 流表下发%s\n'
                         % ('完成' if success else '失败'))
            detail.close()

    return FabricInfo(json_path=json_path, tree=tree, host_plan=host_plan,
                      port_peer=port_peer, switch_ports=switch_ports,
                      host_port=host_port, plans=plans,
                      thrift_ports=thrift_ports)


# --------------------------------------------------------------------------
# Runtime verification
# --------------------------------------------------------------------------

def access_vlan_errors(net, fabric, json_path=None):
    """Query port_cfg on every switch and compare it with the host plan.

    Returns a list of human-readable errors.  This is the bmv2 replacement
    for the old ``ovs-vsctl get port ... tag`` check.
    """
    json_path = os.path.abspath(json_path or fabric.json_path)
    names = runtime_names(json_path)
    errors = []
    for switch_name, plan in sorted(fabric.plans.items(), key=lambda kv: _switch_id(kv[0])):
        expected_access = dict(plan.access_ports)
        expected_trunk = set(plan.trunk_ports)
        try:
            output = switch_cli(fabric.thrift_ports[switch_name],
                                ['table_dump %s' % names['port_cfg']])
        except RuntimeError as exc:
            errors.append('%s: %s' % (switch_name, exc))
            continue

        seen_access = {}
        seen_trunk = set()
        for chunk in output.split('Dumping entry')[1:]:
            match = re.search(r'ingress_port:\s*EXACT\s+([0-9a-fA-F]+)', chunk)
            action = re.search(r'Action entry:\s*(\S+)', chunk)
            if not match or not action:
                continue
            port = int(match.group(1), 16)
            action_name = action.group(1)
            if action_name == names['set_access']:
                data_match = re.search(r'Action entry:\s*\S+\s*-\s*([0-9a-fA-F]+)',
                                       chunk)
                if not data_match:
                    errors.append('%s: 端口 %d 的 set_access 缺少 VLAN 参数'
                                  % (switch_name, port))
                    continue
                seen_access[port] = int(data_match.group(1), 16)
            elif action_name == names['set_trunk']:
                seen_trunk.add(port)
            elif action_name == names['drop']:
                errors.append('%s: 端口 %d 仍为 drop 动作'
                              % (switch_name, port))

        for port, vlan in sorted(expected_access.items()):
            if seen_access.get(port) != vlan:
                errors.append('%s: 端口 %d 的 access VLAN 为 %s，期望 %d'
                              % (switch_name, port,
                                 seen_access.get(port, '缺失'), vlan))
        for port in sorted(expected_trunk):
            if port not in seen_trunk:
                errors.append('%s: 端口 %d 不是 trunk' % (switch_name, port))
    return errors


# --------------------------------------------------------------------------
# Mininet switch factory
# --------------------------------------------------------------------------

class FitiP4Switch(P4Switch):
    """simple_switch whose Thrift port is derived from the node id."""

    def __init__(self, name, thrift_base=DEFAULT_THRIFT_BASE,
                 json_path=None, p4_log_level='info', pcap_dump=None,
                 log_dir=None, sw_path=None, **kwargs):
        node_id = _switch_id(name) if name.startswith('s') else 0
        super(FitiP4Switch, self).__init__(
            name,
            sw_path=sw_path or (shutil.which('simple_switch') or 'simple_switch'),
            json_path=json_path,
            thrift_port=thrift_base + node_id,
            device_id=node_id,
            log_level=p4_log_level,
            pcap_dump=pcap_dump,
            log_dir=log_dir,
            **kwargs)


def _port_free(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('0.0.0.0', port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def choose_thrift_base(data, preferred=DEFAULT_THRIFT_BASE, step=1000,
                       tries=20):
    """Find a base such that base+node_id is free for every node."""
    ids = [int(node['id']) for node in data['nodes']]
    for attempt in range(tries):
        base = preferred + attempt * step
        if all(_port_free(base + node_id) for node_id in ids):
            return base
    raise RuntimeError('从 %d 开始找不到可用的 Thrift 端口 (需要 %d 个)，'
                       '请先执行 sudo pkill -f simple_switch'
                       % (preferred, len(ids)))


def make_switch_factory(json_path, thrift_base, p4_log_level='info',
                        pcap_dump=None, log_dir=None, sw_path=None):
    """Return a callable for ``Mininet(switch=...)``."""
    from functools import partial
    return partial(FitiP4Switch, json_path=json_path,
                   thrift_base=thrift_base, p4_log_level=p4_log_level,
                   pcap_dump=pcap_dump, log_dir=log_dir, sw_path=sw_path)
