#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FITI 拓扑网络切片（bmv2 + P4 VLAN 多租户 + 可选 SRv6 主机 overlay）。

功能:
  - 每个 FITI 节点运行一台 bmv2 simple_switch，加载 p4src/fiti_slice.p4；
  - 同一台交换机可以承载任意多个切片的主机（每主机一个 access 端口 / VLAN）；
  - 交换机间链路是 802.1Q trunk，P4 按 (VLAN, 目的 MAC) 转发并做 VLAN 内泛洪；
  - 控制面预计算生成树，只启用树边（bmv2 没有 STP）；
  - 可选 SRv6 主机 overlay：Linux 命名空间内封装/解封装，bmv2 只做 L2 承载；
  - 切片内全量 ping、跨切片隔离检查、接入端口规则检查、SRv6 ping 检查。

命令行:
  python3 fiti_slice.py --slices slices.json --dump     # 免 root 查看切片规划
  sudo python3 fiti_slice.py --slices slices.json       # 启动并进入 CLI
  sudo python3 fiti_slice.py --slices slices.json --check        # 连通性 + 隔离检查
  sudo python3 fiti_slice.py --slices slices.json --check-srv6   # 额外做 SRv6 检查
  sudo python3 fiti_slice.py --slices slices.json --srv6         # 开启 SRv6 后进入 CLI
  sudo python3 fiti_slice.py --slices slices.json --random-traffic
        # 不同节点随机互 ping（默认只在切片内选流）
  sudo python3 fiti_slice.py --slices slices.json --random-traffic \
       --traffic-scope global   # 允许跨切片；跨切片流期望 100% 丢包
  sudo python3 fiti_slice.py --slices slices.json --random-traffic \
       --traffic-continuous     # 持续随机发包，Ctrl-C 停止

库用法:
  from fiti_slice import load_slice_config, build_slice_net, SliceManager

  cfg = load_slice_config('slices.json')
  net = build_slice_net(cfg)              # 已启动并下发 P4 规则，调用方负责 stop
  mgr = SliceManager(net, cfg)
  print(mgr.ping_slice('nor'))            # 切片内 IPv4 丢包率
  mgr.random_traffic(count=10, duration=5.0, scope='slice')
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
from typing import Dict, List, Optional

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.topo import Topo

from fiti_log import Progress, quiet_mininet_console
from fiti_p4 import (DEFAULT_P4_JSON, P4_LOG_LEVELS, access_vlan_errors,
                     choose_thrift_base, compile_p4, install_fabric,
                     make_switch_factory)
from fiti_topo import (DEFAULT_BW_MAP, link_bandwidth, load_topology,
                       switch_dpid, switch_name)
from fiti_traffic import TrafficHost, run_random_traffic
from p4_mininet import P4Host

__all__ = [
    'SliceConfig',
    'Slice',
    'Srv6Config',
    'P4Config',
    'load_slice_config',
    'build_host_plan',
    'SliceTopology',
    'build_slice_net',
    'SliceManager',
    'srv6_host_commands',
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SLICES_FILE = os.path.join(SCRIPT_DIR, 'slices.json')

SLICE_ID_RE = re.compile(r'^[a-zA-Z0-9_]+$')
DATAPATHS = ('bmv2',)  # 仅支持 bmv2
ISOLATION_MODES = ('vlan',)  # P4 VLAN 隔离


# --------------------------------------------------------------------------
# 配置数据模型
# --------------------------------------------------------------------------

@dataclass
class P4Config:
    json: str = DEFAULT_P4_JSON
    thrift_base: Optional[int] = None
    log_level: str = 'info'
    log_dir: Optional[str] = None
    pcap: bool = False
    pcap_dir: str = 'pcap'
    spanning_root: Optional[int] = None


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
    paths: List[dict] = field(default_factory=list)


@dataclass
class SliceConfig:
    path: str
    topo_file: str
    datapath: str
    isolation: str
    p4: P4Config
    srv6: Srv6Config
    slices: List[Slice]
    defaults: Dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 配置解析与校验
# --------------------------------------------------------------------------

def _error(message):
    raise ValueError(message)


def _parse_p4(raw, base_dir):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _error('p4 必须是对象')

    json_path = str(raw.get('json', os.path.join('build', 'fiti_slice.json')))
    if not os.path.isabs(json_path):
        json_path = os.path.join(base_dir, json_path)
    json_path = os.path.abspath(json_path)

    thrift_base = raw.get('thrift_base')
    if thrift_base is not None:
        thrift_base = int(thrift_base)
        if not 1 <= thrift_base <= 65000:
            _error('p4.thrift_base 必须在 1-65000 之间')

    log_level = str(raw.get('log_level', 'info')).lower()
    if log_level not in P4_LOG_LEVELS:
        _error('p4.log_level 必须是 %s' % ' / '.join(P4_LOG_LEVELS))

    root = raw.get('spanning_root')
    if root is not None:
        root = int(root)

    log_dir = raw.get('log_dir')
    if log_dir:
        log_dir = str(log_dir)
        if not os.path.isabs(log_dir):
            log_dir = os.path.join(base_dir, log_dir)
        log_dir = os.path.abspath(log_dir)

    pcap = bool(raw.get('pcap', False))
    pcap_dir = str(raw.get('pcap_dir', 'pcap'))
    if not os.path.isabs(pcap_dir):
        pcap_dir = os.path.join(base_dir, pcap_dir)

    return P4Config(json=json_path, thrift_base=thrift_base,
                    log_level=log_level, log_dir=log_dir, pcap=pcap,
                    pcap_dir=os.path.abspath(pcap_dir),
                    spanning_root=root)


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
        _error('切片 %s 的 nodes 字段已不支持；请使用 hosts' % sid)
    if raw.get('controller') is not None:
        _error('切片 %s 的 controller 已不支持；节点已改为 bmv2，'
               '由 fiti_p4.py 统一下发规则' % sid)
    if raw.get('bw_share') is not None:
        _error('切片 %s 的 bw_share 已不支持；切片共享物理链路带宽' % sid)

    paths = raw.get('paths', [])
    if paths is None:
        paths = []
    if not isinstance(paths, list):
        _error('切片 %s 的 paths 必须是列表' % sid)

    return Slice(id=sid,
                 name=str(raw.get('name', sid)),
                 vlan=vlan,
                 subnet=str(subnet),
                 hosts=hosts,
                 index=index,
                 paths=paths)


def parse_slice_config(raw, path):
    """解析并校验配置 dict（供 load_slice_config 与测试使用）。"""
    if not isinstance(raw, dict):
        _error('切片配置根节点必须是对象')

    base_dir = os.path.dirname(os.path.abspath(path))
    topo_file = str(raw.get('topo', 'fiti.json'))
    if not os.path.isabs(topo_file):
        topo_file = os.path.join(base_dir, topo_file)
    data = load_topology(topo_file)

    datapath = str(raw.get('datapath', 'bmv2')).lower()
    if datapath == 'p4':
        datapath = 'bmv2'
    if datapath not in DATAPATHS:
        _error('仅支持 bmv2 数据平面，不支持: %s' % datapath)
    if raw.get('controller') is not None:
        _error('controller 字段已不支持；节点已改为 bmv2，'
               '由 fiti_p4.py 统一下发规则')

    isolation = str(raw.get('isolation', 'vlan')).lower()
    if isolation not in ISOLATION_MODES:
        _error('仅支持 vlan 隔离模式，不支持: %s' % isolation)

    defaults_raw = raw.get('defaults', {}) or {}
    if 'bw_share' in defaults_raw:
        _error('defaults.bw_share 已不支持；切片共享物理链路带宽')
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
                       datapath=datapath,
                       isolation=isolation,
                       p4=_parse_p4(raw.get('p4'), base_dir),
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
    """返回 {host_name: meta}，meta 包含切片、节点、VLAN、IPv4、SRv6 地址。"""
    by_abbr = {node['abbr']: node for node in data['nodes']}
    plan = {}
    for sl in cfg.slices:
        for abbr in sl.hosts:
            node = by_abbr[abbr]
            node_id = int(node['id'])
            name = '%s_%s' % (sl.id, abbr)
            plan[name] = {
                'host': name,
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
    """共享 41 台 bmv2 交换机 + 每切片独立主机的 VLAN 切片拓扑。"""

    def build(self, data, cfg):
        self.data = data
        self.cfg = cfg
        self.host_plan = build_host_plan(cfg, data)

        for node in data['nodes']:
            self.addSwitch(switch_name(node['id']),
                           dpid=switch_dpid(node['id']))

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


def build_slice_net(cfg, data=None, start=True, enable_srv6=None,
                    install_rules=True):
    """构建并（默认）启动切片网络，返回 Mininet 实例。

    enable_srv6:
      None  -> 使用 cfg.srv6.enabled
      True/False -> 覆盖配置
    install_rules:
      True  -> 启动后通过 Thrift 下发 port_cfg / l2_fwd / VLAN 泛洪组
    """
    data = data or load_topology(cfg.topo_file)
    topo = SliceTopology(data=data, cfg=cfg)

    json_path = cfg.p4.json
    if start:
        json_path = compile_p4(out=json_path)

    base = cfg.p4.thrift_base
    if base is None:
        base = choose_thrift_base(data)

    pcap_dump = None
    if cfg.p4.pcap:
        os.makedirs(cfg.p4.pcap_dir, exist_ok=True)
        pcap_dump = cfg.p4.pcap_dir

    switch_factory = make_switch_factory(
        json_path, base, p4_log_level=cfg.p4.log_level,
        pcap_dump=pcap_dump, log_dir=cfg.p4.log_dir)

    net = Mininet(topo=topo,
                  switch=switch_factory,
                  host=P4Host,
                  controller=None,
                  link=TCLink,
                  autoSetMacs=True,
                  build=False)
    net.slice_topo = topo
    net.slice_config = cfg
    net.p4_json = os.path.abspath(json_path)
    net.thrift_base = base
    net.p4_fabric = None

    build_names = set(topo.hosts()) | set(topo.switches())
    try:
        with quiet_mininet_console(cfg.p4.log_dir, allow_build_lists=True,
                                   allow_names=build_names):
            net.build()
        if start:
            with quiet_mininet_console(cfg.p4.log_dir):
                with Progress(
                        '*** 启动 bmv2 交换机',
                        done='*** %d 台 bmv2 交换机 / %d 台主机已启动'
                             % (len(data['nodes']), len(topo.host_plan))):
                    net.start()

        if start and install_rules:
            with Progress('*** 下发 P4 流表',
                          total=len(data['nodes']),
                          done='*** P4 流表下发完成') as progress:
                net.p4_fabric = install_fabric(
                    net, data, topo.host_plan, json_path=json_path,
                    root=cfg.p4.spanning_root, log_dir=cfg.p4.log_dir,
                    progress=progress)

        srv6_on = cfg.srv6.enabled if enable_srv6 is None else enable_srv6
        if start and srv6_on:
            with Progress('*** 安装 SRv6 overlay',
                          done='*** SRv6 overlay 已启用'):
                SliceManager(net, cfg, data).enable_srv6()
    except (Exception, KeyboardInterrupt):
        net.stop()
        raise
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
        self.fabric = getattr(net, 'p4_fabric', None)
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

    # ---- 随机通信 ----

    def traffic_hosts(self):
        """构造随机通信使用的主机列表（含节点 / 切片元数据）。"""
        entries = []
        for name, meta in self.plan.items():
            host = self.net.get(name)
            if host is None:
                continue
            entries.append(TrafficHost(name=name,
                                       node_id=int(meta['node_id']),
                                       group=meta['slice_id'],
                                       host=host,
                                       ip=host.IP()))
        return entries

    def random_traffic(self, count=10, duration=5.0, interval=0.2,
                       size=56, scope='slice', seed=None, continuous=False,
                       stop_event=None, log=print):
        """在不同节点之间随机选流并发 ping。

        scope='slice'  -> 流都落在同一 VLAN 切片内，期望可达；
        scope='global' -> 允许跨切片，同切片期望可达、跨切片期望隔离。
        continuous=True 时按轮持续发送，直到 Ctrl-C 或 stop_event 被设置。
        """
        return run_random_traffic(self.traffic_hosts(),
                                  count=count, duration=duration,
                                  interval=interval, size=size,
                                  scope=scope, seed=seed,
                                  continuous=continuous,
                                  stop_event=stop_event, log=log)

    def p4_port_errors(self):
        """检查每台交换机 port_cfg 表中的 access VLAN / trunk 端口。"""
        if self.fabric is None:
            return ['P4 流表尚未下发（net.p4_fabric 为空）']
        return access_vlan_errors(self.net, self.fabric, self.cfg.p4.json)

    # 兼容旧名称：原 VLAN tag 检查已被 port_cfg 检查取代
    def vlan_tag_errors(self):
        return self.p4_port_errors()

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
                'datapath': self.cfg.datapath,
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
    print('数据平面: %s (P4: %s)' % (cfg.datapath, cfg.p4.json))
    print('Thrift 基址: %s'
          % ('自动分配' if cfg.p4.thrift_base is None else cfg.p4.thrift_base))
    print('隔离模式: %s' % cfg.isolation)
    print('SRv6: %s' % ('开启' if cfg.srv6.enabled else '关闭（可用 --srv6 开启）'))
    print('切片数: %d   主机数: %d' % (len(cfg.slices), len(plan)))
    print()

    # 同一节点被多个切片使用时，在这里直接标出来
    by_node = {}
    for name, meta in plan.items():
        by_node.setdefault(meta['node_id'], []).append(meta['slice_id'])
    shared = {node: slices for node, slices in by_node.items()
              if len(slices) > 1}
    print('每节点承载切片数: %d 个节点承载多切片'
          % len(shared))
    for node in sorted(shared):
        print('  节点 %s: %s' % (node, ', '.join(sorted(shared[node]))))
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
    print('=== P4 接入端口规则（access VLAN / trunk）===')
    port_errors = mgr.p4_port_errors()
    port_status = 'PASS' if not port_errors else 'FAIL'
    ok = ok and port_status == 'PASS'
    print('主机接入端口: %d 个, 规则错误 %d 个  %s'
          % (len(mgr.plan), len(port_errors), port_status))
    for error in port_errors[:5]:
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
        description='FITI bmv2 网络切片（P4 VLAN 隔离 + 可选 SRv6 主机 overlay）',
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
    parser.add_argument('--p4-json', help='覆盖编译后的 P4 JSON 路径')
    parser.add_argument('--thrift-base', type=int,
                        help='覆盖 bmv2 Thrift 端口基址')
    parser.add_argument('--p4-log-level', choices=P4_LOG_LEVELS,
                        help='覆盖 bmv2 日志级别')
    parser.add_argument('--pcap', action='store_true',
                        help='开启 bmv2 抓包')
    parser.add_argument('--pcap-dir', help='抓包目录（覆盖配置）')
    parser.add_argument('--log-dir', help='bmv2 日志目录（默认项目 build/logs/）')
    parser.add_argument('--spanning-root', type=int,
                        help='生成树根节点 id（覆盖配置）')
    parser.add_argument('--random-traffic', action='store_true',
                        help='启动后在不同节点之间随机选主机互 ping，完成后退出')
    parser.add_argument('--traffic-continuous', action='store_true',
                        help='持续随机发包；每轮结束后重新选流，Ctrl-C 停止')
    parser.add_argument('--traffic-scope', choices=('slice', 'global'),
                        default='slice',
                        help='随机流范围：slice=仅切片内（默认），'
                             'global=允许跨切片（跨切片期望隔离）')
    parser.add_argument('--traffic-flows', type=int, default=10,
                        help='随机流条数（默认: %(default)s）')
    parser.add_argument('--traffic-duration', type=float, default=5.0,
                        help='每条流的近似持续时间，秒（默认: %(default)s）')
    parser.add_argument('--traffic-interval', type=float, default=0.2,
                        help='ping 发包间隔，秒（默认: %(default)s）')
    parser.add_argument('--traffic-size', type=int, default=56,
                        help='ping 负载大小，字节（默认: %(default)s）')
    parser.add_argument('--traffic-seed', type=int, default=None,
                        help='随机种子；省略时自动生成并打印')
    parser.add_argument('--log', default='info',
                        choices=('debug', 'info', 'warning', 'error', 'critical'),
                        help='Mininet 日志级别')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    setLogLevel(args.log)
    if args.traffic_continuous:
        args.random_traffic = True

    try:
        cfg = load_slice_config(args.slices)
    except ValueError as exc:
        raise SystemExit('配置错误: %s' % exc)

    if args.p4_json:
        cfg.p4.json = os.path.abspath(args.p4_json)
    if args.thrift_base:
        cfg.p4.thrift_base = args.thrift_base
    if args.p4_log_level:
        cfg.p4.log_level = args.p4_log_level
    if args.pcap:
        cfg.p4.pcap = True
    if args.pcap_dir:
        cfg.p4.pcap_dir = os.path.abspath(args.pcap_dir)
    if args.log_dir:
        cfg.p4.log_dir = os.path.abspath(args.log_dir)
    if args.spanning_root is not None:
        cfg.p4.spanning_root = args.spanning_root
    if args.srv6 or args.check_srv6:
        cfg.srv6.enabled = True

    data = load_topology(cfg.topo_file)

    if args.dump:
        dump_slices(cfg, data)
        return 0

    if os.geteuid() != 0:
        raise SystemExit('Mininet/bmv2 需要 root 权限，请使用 sudo 运行。'
                         '（只想查看切片规划可以加 --dump）')

    net = None
    try:
        net = build_slice_net(cfg, data=data, start=True,
                              enable_srv6=cfg.srv6.enabled)
        mgr = SliceManager(net, cfg, data)
        mgr.srv6_enabled = cfg.srv6.enabled

        check_ok = True
        if args.check or args.check_srv6:
            check_ok = run_checks(mgr, cfg, check_srv6=args.check_srv6)

        traffic_ok = True
        if args.random_traffic:
            _, traffic_ok = mgr.random_traffic(
                count=args.traffic_flows,
                duration=args.traffic_duration,
                interval=args.traffic_interval,
                size=args.traffic_size,
                scope=args.traffic_scope,
                seed=args.traffic_seed,
                continuous=args.traffic_continuous)

        if args.check or args.check_srv6 or args.random_traffic:
            return 0 if (check_ok and traffic_ok) else 1
        CLI(net)
        return 0
    except KeyboardInterrupt:
        print('\n*** 已中断', file=sys.stderr)
        return 130
    finally:
        if net is not None:
            net.stop()


if __name__ == '__main__':
    sys.exit(main())
