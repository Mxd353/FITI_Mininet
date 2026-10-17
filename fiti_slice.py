#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FITI 拓扑网络切片（VLAN 多租户 + SRv6 主机 overlay）。

功能:
  - 在同一份 FITI 物理拓扑上运行多个 VLAN 隔离的切片
  - 每个切片独立主机、独立 IPv4 子网、独立 VLAN
  - 可选 SRv6 主机 overlay：Linux 命名空间内封装/解封装，OVS 只做 L2 承载
  - 切片内全量 ping、跨切片隔离检查、SRv6 ping 检查

命令行:
  python3 fiti_slice.py --slices slices.json --dump     # 免 root 查看切片规划
  sudo python3 fiti_slice.py --slices slices.json       # 启动并进入 CLI
  sudo python3 fiti_slice.py --slices slices.json --check        # 切片连通性 + 隔离检查
  sudo python3 fiti_slice.py --slices slices.json --check-srv6   # 额外做 SRv6 检查
  sudo python3 fiti_slice.py --slices slices.json --srv6         # 开启 SRv6 后进入 CLI

库用法:
  from fiti_slice import load_slice_config, build_slice_net, SliceManager

  cfg = load_slice_config('slices.json')
  net = build_slice_net(cfg)              # 已启动，调用方负责 net.stop()
  mgr = SliceManager(net, cfg)
  print(mgr.ping_slice('nor'))            # 切片内 IPv4 丢包率
  mgr.enable_srv6()                       # 安装 SRv6 地址/路由
  print(mgr.check_srv6_slice('nor'))      # 返回失败列表
  net.stop()

主机命名:
  <slice_id>_<abbr>，例如 nor_bj、eas_sh、wes_xa。
  主机 IPv4 默认 10.<切片序号>.0.<节点id>/24。
  SRv6: underlay 2001:db8:<切片序号>::<节点id>/64
        SID      2001:db8:<切片序号>:<节点id>::1/128
        overlay  fd00:<切片序号>::<节点id>/128
"""

import argparse
import ipaddress
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from functools import partial
from typing import Dict, List

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel, warn
from mininet.net import Mininet
from mininet.node import (DefaultController, OVSBridge, OVSSwitch,
                          RemoteController)
from mininet.topo import Topo

from fiti_topo import (DEFAULT_BW_MAP, DEFAULT_STP_WAIT, link_bandwidth,
                       load_topology, switch_dpid, switch_name)

__all__ = [
    'SliceConfig',
    'Slice',
    'Srv6Config',
    'ControllerConfig',
    'load_slice_config',
    'build_host_plan',
    'SliceTopology',
    'build_slice_net',
    'SliceManager',
    'srv6_host_commands',
    'apply_vlan_tags',
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SLICES_FILE = os.path.join(SCRIPT_DIR, 'slices.json')

SLICE_ID_RE = re.compile(r'^[a-zA-Z0-9_]+$')
CONTROLLER_MODES = ('stp', 'default', 'remote')
ISOLATION_MODES = ('vlan',)  # 仅支持 VLAN 模式


# --------------------------------------------------------------------------
# 配置数据模型
# --------------------------------------------------------------------------

@dataclass
class ControllerConfig:
    mode: str = 'stp'
    ip: str = '127.0.0.1'
    port: int = 6653


@dataclass
class Srv6Config:
    enabled: bool = False
    underlay_prefix: str = '2001:db8'
    overlay_prefix: str = 'fd00'
    decap_table: int = 100
    mtu: int = 1420


@dataclass
class Slice:
    id: str
    name: str
    vlan: int
    subnet: str
    hosts: List[str]
    index: int = 1


@dataclass
class SliceConfig:
    path: str
    topo_file: str
    isolation: str
    controller: ControllerConfig
    srv6: Srv6Config
    slices: List[Slice]
    defaults: Dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 配置解析与校验
# --------------------------------------------------------------------------

def _error(message):
    raise ValueError(message)


def _parse_controller(raw, where='controller'):
    if raw is None:
        return ControllerConfig()
    if not isinstance(raw, dict):
        _error('%s 必须是对象' % where)
    mode = str(raw.get('mode', 'stp')).lower()
    if mode not in CONTROLLER_MODES:
        _error('%s.mode 必须是 %s' % (where, ' / '.join(CONTROLLER_MODES)))
    return ControllerConfig(mode=mode,
                            ip=str(raw.get('ip', '127.0.0.1')),
                            port=int(raw.get('port', 6653)))


def _parse_srv6(raw):
    if raw is None:
        return Srv6Config()
    if not isinstance(raw, dict):
        _error('srv6 必须是对象')
    underlay = str(raw.get('underlay_prefix', '2001:db8')).strip().rstrip(':')
    overlay = str(raw.get('overlay_prefix', 'fd00')).strip().rstrip(':')
    for name, value in (('underlay_prefix', underlay), ('overlay_prefix', overlay)):
        try:
            ipaddress.IPv6Address(value + '::')
        except ValueError:
            _error('srv6.%s 不是合法的 IPv6 前缀: %s' % (name, value))
    table = int(raw.get('decap_table', 100))
    if not 1 <= table <= 252:
        _error('srv6.decap_table 必须在 1-252 之间')
    mtu = int(raw.get('mtu', 1420))
    if not 1280 <= mtu <= 1500:
        _error('srv6.mtu 必须在 1280-1500 之间')
    return Srv6Config(enabled=bool(raw.get('enabled', False)),
                      underlay_prefix=underlay,
                      overlay_prefix=overlay,
                      decap_table=table,
                      mtu=mtu)


def _host_ip_in_subnet(subnet, node_id):
    network = ipaddress.ip_network(subnet)
    address = network.network_address + int(node_id)
    if address not in network or address == network.broadcast_address:
        _error('子网 %s 容纳不下节点 %s 的主机地址' % (subnet, node_id))
    return '%s/%d' % (address, network.prefixlen)


def _parse_slice(raw, index, defaults, data, used_ids, used_vlans,
                 used_subnets):
    if not isinstance(raw, dict):
        _error('slices[%d] 必须是对象' % (index - 1))
    sid = str(raw.get('id', '')).strip()
    if not sid or not SLICE_ID_RE.match(sid):
        _error('slices[%d].id 缺失或非法（只允许字母/数字/下划线）' % (index - 1))
    if sid in used_ids:
        _error('切片 id 重复: %s' % sid)
    used_ids.add(sid)

    vlan = int(raw.get('vlan', defaults['vlan_base'] + index - 1))
    if not 1 <= vlan <= 4094:
        _error('切片 %s 的 vlan 必须在 1-4094 之间' % sid)
    if vlan in used_vlans:
        _error('切片 %s 的 vlan %d 与其他切片重复' % (sid, vlan))
    used_vlans.add(vlan)

    subnet_text = raw.get('subnet')
    if not subnet_text:
        subnet_text = '%d.%d.0.0/24' % (defaults['subnet_base'], index)
    try:
        subnet = ipaddress.ip_network(subnet_text, strict=False)
    except ValueError:
        _error('切片 %s 的 subnet 非法: %s' % (sid, subnet_text))
    for other in used_subnets:
        if subnet.overlaps(other):
            _error('切片 %s 的子网 %s 与 %s 重叠' % (sid, subnet, other))
    used_subnets.append(subnet)

    hosts_raw = raw.get('hosts')
    if hosts_raw == 'all':
        hosts = [node['abbr'] for node in data['nodes']]
    elif isinstance(hosts_raw, list) and hosts_raw:
        hosts = [str(h) for h in hosts_raw]
    else:
        _error('切片 %s 的 hosts 必须是非空列表或 "all"' % sid)

    by_abbr = {node['abbr']: node for node in data['nodes']}
    seen = set()
    for abbr in hosts:
        if abbr not in by_abbr:
            _error('切片 %s 引用了不存在的节点 abbr: %s' % (sid, abbr))
        if abbr in seen:
            _error('切片 %s 的 hosts 重复: %s' % (sid, abbr))
        seen.add(abbr)
        node_id = int(by_abbr[abbr]['id'])
        _host_ip_in_subnet(subnet, node_id)
        host_name = '%s_%s' % (sid, abbr)
        # Linux 接口名上限 15 字符，主机侧接口为 <host>-eth0
        if len(host_name) + 5 > 15:
            _error('主机名 %s 过长（%d 字符），接口名会超过 15 字符；'
                   '请缩短切片 id' % (host_name, len(host_name)))

    if raw.get('nodes') is not None:
        _error('切片 %s 的 nodes 字段已不支持；VLAN 模式请使用 hosts' % sid)
    if raw.get('controller') is not None:
        _error('切片 %s 的 controller 已不支持；请使用全局 controller' % sid)
    if raw.get('bw_share') is not None:
        _error('切片 %s 的 bw_share 已不支持；VLAN 模式共享物理链路带宽' % sid)

    return Slice(id=sid,
                 name=str(raw.get('name', sid)),
                 vlan=vlan,
                 subnet=str(subnet),
                 hosts=hosts,
                 index=index)


def parse_slice_config(raw, path):
    """解析并校验配置 dict（供 load_slice_config 与测试使用）。"""
    if not isinstance(raw, dict):
        _error('切片配置根节点必须是对象')

    base_dir = os.path.dirname(os.path.abspath(path))
    topo_file = str(raw.get('topo', 'fiti.json'))
    if not os.path.isabs(topo_file):
        topo_file = os.path.join(base_dir, topo_file)
    data = load_topology(topo_file)

    isolation = str(raw.get('isolation', 'vlan')).lower()
    if isolation not in ISOLATION_MODES:
        _error('仅支持 vlan 隔离模式，不支持: %s' % isolation)

    defaults_raw = raw.get('defaults', {}) or {}
    if 'bw_share' in defaults_raw:
        _error('defaults.bw_share 已不支持；VLAN 模式共享物理链路带宽')
    defaults = {
        'vlan_base': int(defaults_raw.get('vlan_base', 100)),
        'subnet_base': int(defaults_raw.get('subnet_base', 10)),
    }
    if not 1 <= defaults['vlan_base'] <= 4094:
        _error('defaults.vlan_base 必须在 1-4094 之间')

    slices_raw = raw.get('slices')
    if not isinstance(slices_raw, list) or not slices_raw:
        _error('slices 必须是非空列表')

    used_ids, used_vlans, used_subnets = set(), set(), []
    slices = [
        _parse_slice(item, i + 1, defaults, data,
                     used_ids, used_vlans, used_subnets)
        for i, item in enumerate(slices_raw)
    ]

    return SliceConfig(path=os.path.abspath(path),
                       topo_file=topo_file,
                       isolation=isolation,
                       controller=_parse_controller(raw.get('controller')),
                       srv6=_parse_srv6(raw.get('srv6')),
                       slices=slices,
                       defaults=defaults)


def load_slice_config(path=None):
    """读取并校验切片配置，返回 SliceConfig。"""
    path = path or DEFAULT_SLICES_FILE
    if not os.path.isfile(path):
        raise ValueError('找不到切片配置文件: %s' % path)
    with open(path, encoding='utf-8') as handle:
        raw = json.load(handle)
    return parse_slice_config(raw, path)


# --------------------------------------------------------------------------
# 地址规划与 SRv6 指令生成
# --------------------------------------------------------------------------

def srv6_underlay(cfg, slice_index, node_id):
    return '%s:%d::%d' % (cfg.srv6.underlay_prefix, slice_index, node_id)


def srv6_sid(cfg, slice_index, node_id):
    return '%s:%d:%d::1' % (cfg.srv6.underlay_prefix, slice_index, node_id)


def srv6_overlay(cfg, slice_index, node_id):
    return '%s:%d::%d' % (cfg.srv6.overlay_prefix, slice_index, node_id)


def build_host_plan(cfg, data):
    """返回 {host_name: meta}，meta 包含 slice、节点、VLAN、IPv4、SRv6 地址。"""
    by_abbr = {node['abbr']: node for node in data['nodes']}
    plan = {}
    for sl in cfg.slices:
        for abbr in sl.hosts:
            node = by_abbr[abbr]
            node_id = int(node['id'])
            host_name = '%s_%s' % (sl.id, abbr)
            plan[host_name] = {
                'host': host_name,
                'slice': sl,
                'slice_id': sl.id,
                'slice_index': sl.index,
                'node_id': node_id,
                'node_name': node['name'],
                'abbr': abbr,
                'switch': switch_name(node_id),
                'vlan': sl.vlan,
                'ip': _host_ip_in_subnet(sl.subnet, node_id),
                'underlay': srv6_underlay(cfg, sl.index, node_id),
                'sid': srv6_sid(cfg, sl.index, node_id),
                'overlay': srv6_overlay(cfg, sl.index, node_id),
            }
    return plan


def srv6_host_commands(meta, peers, srv6, iface='eth0'):
    """生成一台主机的 SRv6 配置命令。

    meta:  build_host_plan() 中本机的 meta
    peers: 同切片其他主机的 meta 列表，额外需要 'mac' 字段
    """
    cmds = [
        # 关闭 DAD，避免地址加入后的首包丢失
        'sysctl -qw net.ipv6.conf.%s.accept_dad=0' % iface,
        'sysctl -qw net.ipv6.conf.lo.accept_dad=0',
        'ip link set dev %s up' % iface,
        'ip link set dev lo up',
        'ip link set dev %s mtu %d' % (iface, srv6.mtu),
        'ip -6 addr replace %s/64 dev %s' % (meta['underlay'], iface),
        'ip -6 addr replace %s/128 dev lo' % meta['overlay'],
    ]
    for peer in peers:
        cmds.extend([
            # SID 通过邻居的 underlay 地址到达，预置静态邻居避免首包丢失
            'ip -6 neigh replace %s lladdr %s dev %s'
            % (peer['underlay'], peer['mac'], iface),
            'ip -6 route replace %s/128 via %s dev %s'
            % (peer['sid'], peer['underlay'], iface),
            # overlay -> 对端 SID 的 SRv6 封装
            'ip -6 route replace %s/128 encap seg6 mode encap segs %s dev %s'
            % (peer['overlay'], peer['sid'], iface),
        ])
    cmds.extend([
        # 本机 SID：解封装后查 decap 表
        'ip -6 route replace %s/128 encap seg6local action End.DT6 table %d dev %s'
        % (meta['sid'], srv6.decap_table, iface),
        # decap 表只允许本机 overlay 地址，其余丢弃，避免再次封装成环
        'ip -6 route replace local %s/128 dev lo table %d'
        % (meta['overlay'], srv6.decap_table),
        'ip -6 route replace unreachable default table %d metric 1024'
        % srv6.decap_table,
    ])
    return cmds


# --------------------------------------------------------------------------
# Mininet 拓扑
# --------------------------------------------------------------------------

class SliceTopology(Topo):
    """共享 41 台交换机 + 每切片独立主机的 VLAN 切片拓扑。"""

    def build(self, data, cfg):
        self.data = data
        self.cfg = cfg
        self.host_plan = build_host_plan(cfg, data)

        for node in data['nodes']:
            self.addSwitch(switch_name(node['id']),
                           dpid=switch_dpid(node['id']),
                           stp=True)

        for link in data['links']:
            bw = link_bandwidth(link.get('weight'), DEFAULT_BW_MAP)
            opts = {}
            if bw:
                # 用 TBF 代替默认 HTB，避免 "quantum of class ... is big" 警告
                opts = {'bw': bw, 'use_tbf': True, 'latency_ms': 20}
            self.addLink(switch_name(link['src']),
                         switch_name(link['dst']), **opts)

        for host_name, meta in self.host_plan.items():
            self.addHost(host_name, ip=meta['ip'])
            self.addLink(host_name, meta['switch'])


def apply_vlan_tags(net, host_plan):
    """把每台主机接入交换机的端口设为对应切片的 access VLAN。"""
    for host_name, meta in host_plan.items():
        host = net.get(host_name)
        switch = net.get(meta['switch'])
        if not host or not switch:
            raise RuntimeError('找不到主机或交换机: %s / %s'
                               % (host_name, meta['switch']))
        connections = host.connectionsTo(switch)
        if not connections:
            raise RuntimeError('主机 %s 未连接到 %s' % (host_name, meta['switch']))
        switch_intf = connections[0][1]
        switch.cmd('ovs-vsctl set port %s tag=%d vlan_mode=access'
                   % (switch_intf.name, meta['vlan']))
        port_tag = switch.cmd('ovs-vsctl get port %s tag'
                              % switch_intf.name).strip()
        if port_tag != str(meta['vlan']):
            warn('*** 警告: %s 的 VLAN tag 未生效 (%s)\n'
                 % (switch_intf.name, port_tag))


def build_slice_net(cfg, data=None, start=True, wait_connected=False,
                    stp_wait=DEFAULT_STP_WAIT, enable_srv6=None):
    """构建并（默认）启动切片网络，返回 Mininet 实例。

    wait_connected:
      True  -> 等待就绪，最长 stp_wait 秒
               （STP 模式等待 STP 收敛，其他模式等待控制器连接）
      数字  -> 等待指定秒数
      False -> 不等待
    enable_srv6:
      None  -> 使用 cfg.srv6.enabled
      True/False -> 覆盖配置
    """
    data = data or load_topology(cfg.topo_file)
    topo = SliceTopology(data=data, cfg=cfg)

    if cfg.controller.mode == 'stp':
        switch_cls = OVSBridge
        controller = None
        wait = stp_wait if wait_connected is True else wait_connected
    elif cfg.controller.mode == 'default':
        switch_cls = OVSSwitch
        controller = DefaultController
        wait = stp_wait if wait_connected is True else wait_connected
        warn('*** VLAN 模式下使用 ovs-testcontroller，'
             '请确认控制器按 VLAN 隔离租户\n')
    else:
        switch_cls = OVSSwitch
        controller = partial(RemoteController,
                             ip=cfg.controller.ip,
                             port=cfg.controller.port)
        wait = stp_wait if wait_connected is True else wait_connected
        warn('*** VLAN 模式下使用外部控制器，'
             '请确认控制器按 VLAN 隔离租户\n')

    net = Mininet(topo=topo,
                  switch=switch_cls,
                  controller=controller,
                  link=TCLink,
                  autoSetMacs=True,
                  waitConnected=wait,
                  build=False)
    net.build()
    if start:
        net.start()
        apply_vlan_tags(net, topo.host_plan)
        srv6_on = cfg.srv6.enabled if enable_srv6 is None else enable_srv6
        if srv6_on:
            SliceManager(net, cfg, data).enable_srv6()
    # 便于调用方复用规划结果
    net.slice_topo = topo
    net.slice_config = cfg
    return net


# --------------------------------------------------------------------------
# 运行时管理
# --------------------------------------------------------------------------

class SliceManager:
    """切片运行时查询与校验。"""

    def __init__(self, net, cfg, data=None):
        self.net = net
        self.cfg = cfg
        self.data = data or load_topology(cfg.topo_file)
        self.plan = build_host_plan(cfg, self.data)
        self.srv6_enabled = False

    # ---- 主机查询 ----

    def slice_by_id(self, slice_id):
        for item in self.cfg.slices:
            if item.id == slice_id:
                return item
        raise KeyError('未知切片: %s' % slice_id)

    def slice_host_names(self, slice_id):
        return [name for name, meta in self.plan.items()
                if meta['slice_id'] == slice_id]

    def hosts(self, slice_id):
        return [self.net.get(name) for name in self.slice_host_names(slice_id)]

    def host(self, slice_id, abbr):
        for name, meta in self.plan.items():
            if meta['slice_id'] == slice_id and meta['abbr'] == abbr:
                return self.net.get(name)
        raise KeyError('切片 %s 中没有节点 %s 的主机' % (slice_id, abbr))

    def host_meta(self, name):
        return self.plan[name]

    # ---- 连通性检查 ----

    def ping_slice(self, slice_id, timeout=1):
        """切片内 IPv4 全量 ping，返回丢包率（0-100），不打印逐对结果。"""
        hosts = self.hosts(slice_id)
        if len(hosts) < 2:
            return 0.0
        total = received = 0
        for src in hosts:
            for dst in hosts:
                if src is dst:
                    continue
                total += 1
                output = src.cmd('ping -c 1 -W %d %s' % (timeout, dst.IP()))
                if ' 0% packet loss' in output:
                    received += 1
        return 100.0 * (total - received) / total

    def ping_pair(self, host_a, host_b, timeout=1):
        """两台主机之间的 IPv4 丢包率（0 或 100）。"""
        output = host_a.cmd('ping -c 1 -W %d %s' % (timeout, host_b.IP()))
        return 0.0 if ' 0% packet loss' in output else 100.0

    def vlan_tag_errors(self):
        """检查每台主机接入端口的 OVS VLAN tag 是否与规划一致。"""
        errors = []
        for name, meta in self.plan.items():
            host = self.net.get(name)
            switch = self.net.get(meta['switch'])
            if not host or not switch:
                errors.append('%s: 主机或交换机不存在' % name)
                continue
            connections = host.connectionsTo(switch)
            if not connections:
                errors.append('%s: 未连接到 %s' % (name, meta['switch']))
                continue
            port_name = connections[0][1].name
            tag = switch.cmd('ovs-vsctl get port %s tag' % port_name).strip()
            if tag != str(meta['vlan']):
                errors.append('%s: tag=%r, 期望 %d'
                              % (port_name, tag, meta['vlan']))
        return errors

    # ---- SRv6 ----

    def enable_srv6(self):
        """为所有切片主机安装 SRv6 地址、邻居、封装/解封装路由。"""
        if self.srv6_enabled:
            return
        groups = {}
        for name, meta in self.plan.items():
            groups.setdefault(meta['slice_id'], []).append((name, meta))

        for slice_id, entries in groups.items():
            peers = []
            for name, meta in entries:
                peer = dict(meta)
                peer['mac'] = self.net.get(name).MAC()
                peers.append(peer)
            for name, meta in entries:
                host = self.net.get(name)
                iface = host.defaultIntf().name
                other_peers = [p for p in peers if p['host'] != name]
                for command in srv6_host_commands(meta, other_peers,
                                                  self.cfg.srv6, iface):
                    host.cmd(command)

        # 预热一次，确保邻居/路由立即生效
        for slice_id, entries in groups.items():
            if len(entries) > 1:
                src_name, dst_name = entries[0][0], entries[1][0]
                self.net.get(src_name).cmd(
                    'ping6 -c 1 -W 1 %s >/dev/null 2>&1'
                    % self.plan[dst_name]['overlay'])
        self.srv6_enabled = True

    def check_srv6_slice(self, slice_id, count=1):
        """切片内两两 ping6 overlay 地址，返回失败列表 [(src, dst, reason)]。"""
        names = self.slice_host_names(slice_id)
        failures = []
        for src_name in names:
            src = self.net.get(src_name)
            for dst_name in names:
                if src_name == dst_name:
                    continue
                dst_overlay = self.plan[dst_name]['overlay']
                output = src.cmd('ping6 -c %d -W 1 %s'
                                 % (count, dst_overlay))
                if ' 0% packet loss' not in output:
                    reason = output.strip().splitlines()[-1] if output.strip() else '无输出'
                    failures.append((src_name, dst_name, reason))
        return failures

    def stats(self):
        result = []
        for item in self.cfg.slices:
            names = self.slice_host_names(item.id)
            result.append({
                'id': item.id,
                'name': item.name,
                'vlan': item.vlan,
                'subnet': item.subnet,
                'hosts': len(names),
                'srv6': self.srv6_enabled,
            })
        return result


# --------------------------------------------------------------------------
# 输出与检查
# --------------------------------------------------------------------------

def dump_slices(cfg, data):
    plan = build_host_plan(cfg, data)

    def width(text):
        return sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1
                   for ch in str(text))

    def row(cells, widths):
        return '  '.join(str(cell) + ' ' * max(0, w - width(cell))
                         for cell, w in zip(cells, widths))

    print('切片配置: %s' % cfg.path)
    print('物理拓扑: %s (%d 节点 / %d 链路)'
          % (cfg.topo_file, len(data['nodes']), len(data['links'])))
    print('隔离模式: %s' % cfg.isolation)
    print('控制器: %s' % cfg.controller.mode)
    print('SRv6: %s' % ('开启' if cfg.srv6.enabled else '关闭（可用 --srv6 开启）'))
    print('切片数: %d   主机数: %d' % (len(cfg.slices), len(plan)))
    print()
    print(row(['id', 'name', 'vlan', 'subnet', 'hosts'], [6, 14, 6, 16, 6]))
    for item in cfg.slices:
        print(row([item.id, item.name, item.vlan, item.subnet, len(item.hosts)],
                  [6, 14, 6, 16, 6]))
    print()
    print('主机规划:')
    print(row(['host', 'id', 'name', 'switch', 'ip', 'vlan'],
              [10, 4, 12, 8, 18, 6]))
    for host_name, meta in plan.items():
        print(row([host_name, meta['node_id'], meta['node_name'],
                   meta['switch'], meta['ip'], meta['vlan']],
                  [10, 4, 12, 8, 18, 6]))
    if cfg.srv6.enabled:
        print()
        print('SRv6 规划:')
        print(row(['host', 'underlay', 'sid', 'overlay'], [10, 20, 24, 20]))
        for host_name, meta in plan.items():
            print(row([host_name, meta['underlay'], meta['sid'], meta['overlay']],
                      [10, 20, 24, 20]))


def run_checks(mgr, cfg, check_srv6=False):
    """执行切片内连通性、跨切片隔离和可选 SRv6 检查，返回是否全部通过。"""
    ok = True

    print('=== 切片内连通性（IPv4）===')
    for item in cfg.slices:
        loss = mgr.ping_slice(item.id)
        status = 'PASS' if loss is not None and loss < 0.01 else 'FAIL'
        ok = ok and status == 'PASS'
        print('[%s] %d 台主机: %.1f%% 丢包  %s'
              % (item.id, len(item.hosts), loss or 0.0, status))

    print()
    print('=== VLAN 标签 ===')
    tag_errors = mgr.vlan_tag_errors()
    tag_status = 'PASS' if not tag_errors else 'FAIL'
    ok = ok and tag_status == 'PASS'
    print('主机接入端口: %d 个, 标签错误 %d 个  %s'
          % (len(mgr.plan), len(tag_errors), tag_status))
    for error in tag_errors[:5]:
        print('    %s' % error)

    print()
    print('=== 跨切片 IPv4 不可达（期望 100% 丢包）===')
    for i, first in enumerate(cfg.slices):
        for second in cfg.slices[i + 1:]:
            host_a = mgr.hosts(first.id)[0]
            host_b = mgr.hosts(second.id)[0]
            loss = mgr.ping_pair(host_a, host_b)
            status = 'PASS' if loss is not None and loss > 99.9 else 'FAIL'
            ok = ok and status == 'PASS'
            print('[%s vs %s] %.1f%% 丢包  %s'
                  % (first.id, second.id, loss or 0.0, status))

    if check_srv6:
        print()
        print('=== SRv6 overlay（期望 0%% 丢包）===')
        for item in cfg.slices:
            failures = mgr.check_srv6_slice(item.id)
            total = len(item.hosts) * (len(item.hosts) - 1)
            status = 'PASS' if not failures else 'FAIL'
            ok = ok and status == 'PASS'
            print('[%s] %d 个方向, 失败 %d 个  %s'
                  % (item.id, total, len(failures), status))
            for src, dst, reason in failures[:3]:
                print('    %s -> %s: %s' % (src, dst, reason))

    print()
    print('结果: %s' % ('全部通过' if ok else '存在失败项'))
    return ok


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description='FITI 拓扑 VLAN 网络切片（可选 SRv6 主机 overlay）',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--slices', default=DEFAULT_SLICES_FILE,
                        help='切片配置文件 (默认: %(default)s)')
    parser.add_argument('--dump', action='store_true',
                        help='只打印切片规划后退出（免 root）')
    parser.add_argument('--srv6', action='store_true',
                        help='开启 SRv6 主机 overlay（覆盖配置）')
    parser.add_argument('--check', action='store_true',
                        help='启动后执行切片连通性与隔离检查，然后退出')
    parser.add_argument('--check-srv6', action='store_true',
                        help='在 --check 基础上增加 SRv6 overlay 检查')
    parser.add_argument('--controller', choices=CONTROLLER_MODES,
                        help='覆盖配置中的控制器模式')
    parser.add_argument('--controller-ip', help='覆盖外部控制器地址')
    parser.add_argument('--controller-port', type=int,
                        help='覆盖外部控制器端口')
    parser.add_argument('--stp-wait', type=int, default=DEFAULT_STP_WAIT,
                        help='STP 收敛等待上限秒数，默认 %(default)s')
    parser.add_argument('--wait-connected', action='store_true',
                        help='启动后等待 STP 收敛/控制器连接')
    parser.add_argument('--log', default='info',
                        choices=('debug', 'info', 'warning', 'error', 'critical'),
                        help='Mininet 日志级别')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    setLogLevel(args.log)

    try:
        cfg = load_slice_config(args.slices)
    except ValueError as exc:
        raise SystemExit('配置错误: %s' % exc)

    if args.controller:
        cfg.controller.mode = args.controller
    if args.controller_ip:
        cfg.controller.ip = args.controller_ip
    if args.controller_port:
        cfg.controller.port = args.controller_port
    if args.srv6 or args.check_srv6:
        cfg.srv6.enabled = True

    data = load_topology(cfg.topo_file)

    if args.dump:
        dump_slices(cfg, data)
        return 0

    if os.geteuid() != 0:
        raise SystemExit('Mininet 需要 root 权限，请使用 sudo 运行。'
                         '（只想查看切片规划可以加 --dump）')

    want_check = args.check or args.check_srv6
    wait_connected = args.wait_connected or want_check

    info('*** 切片网络: %d 个切片, %d 台主机, 隔离模式=%s\n'
         % (len(cfg.slices),
            sum(len(item.hosts) for item in cfg.slices),
            cfg.isolation))
    if cfg.controller.mode == 'stp' and not wait_connected:
        info('*** STP 模式: 首次 ping 前通常需等待 30-45 秒收敛 '
             '（或用 --check / --wait-connected 自动等待）\n')

    net = build_slice_net(cfg, data=data, start=True,
                          wait_connected=wait_connected,
                          stp_wait=args.stp_wait,
                          enable_srv6=cfg.srv6.enabled)
    try:
        mgr = SliceManager(net, cfg, data)
        mgr.srv6_enabled = cfg.srv6.enabled
        if want_check:
            ok = run_checks(mgr, cfg, check_srv6=args.check_srv6)
            return 0 if ok else 1
        CLI(net)
    finally:
        net.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
