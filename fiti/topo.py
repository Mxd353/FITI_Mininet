#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Mininet + bmv2 模拟 configs/fiti.json 描述的 FITI 骨干拓扑。

拓扑规模: 默认 41 个节点 / 43 条链路; 节点可设置 "enabled": false 按需跳过。

建模方式:
  - 每个启用节点 <id> 运行一台 bmv2 simple_switch, 名为 s<id>;
  - 禁用节点不创建交换机和主机, 与其相连的链路也不参与有效拓扑;
  - 每台交换机加载同一个 P4 程序 (p4src/fiti_slice.p4), 做 VLAN 感知的二层转发;
  - 每个节点再挂一台主机, 主机名取节点的 abbr (如 bj/sh/xa), IP 为 10.0.0.<id>/24;
  - 交换机之间按 fiti.json 的 links 互联, link.weight (4/2/1) 映射为链路带宽等级;
  - bmv2 没有 STP, 控制面 (fiti/p4.py) 预先计算生成树并只启用树边;
  - 每台交换机通过 Thrift (simple_switch_CLI) 下发端口、MAC 与泛洪规则.

  "每节点多切片" 由 p4src/fiti_slice.p4 的 VLAN 逻辑实现: 同一台 bmv2 交换机可以
  挂接任意多个切片的主机, 每个主机接在独立的 access 端口上, 交换机间链路是 trunk.

命令行用法:
  sudo python3 fiti_topo.py                    # 启动 Mininet CLI
  sudo python3 fiti_topo.py --pingall          # 启动后全量 ping, 再进 CLI
  sudo python3 fiti_topo.py --random-traffic   # 不同节点随机互 ping 后退出
  sudo python3 fiti_topo.py --random-traffic --traffic-continuous  # 持续发包
  sudo python3 fiti_topo.py --no-tc            # 使用普通链路, 不做带宽限制
  sudo python3 fiti_topo.py --no-hosts         # 只建启用节点的 bmv2 交换机
  python3 fiti_topo.py --dump                  # 只打印拓扑统计, 不启动 Mininet (免 root)
  sudo python3 fiti_topo.py --slices configs/slices.json   # 统一入口: 转交 fiti_slice.py

  sudo python3 fiti_topo.py --bw-map 4:1000,2:500,1:100
  sudo python3 fiti_topo.py --pcap --pcap-dir pcap
  sudo python3 fiti_topo.py --thrift-base 10000 --p4-log-level warn

  也可以作为 mn 的 custom 拓扑加载 (需要自行提供 P4Switch):
  sudo mn --custom fiti_topo.py --topo fiti --link tc

======================================================================
作为库使用 (其他 Python 代码直接 import):
======================================================================

  from fiti.topo import build_net, fiti_network, get_host

  net = build_net()                              # 已启动并下发好 P4 规则
  bj, sh = get_host(net, 1), get_host(net, 2)
  print(bj.cmd('ping -c 3', sh.IP()))
  net.stop()

  with fiti_network() as net:
      net.pingAll()

  # 只要 Topo 对象, 自己组装 Mininet (需要 fiti_p4 的交换机工厂):
  from fiti.topo import build_topology, build_base_host_plan
  from fiti.p4 import compile_p4, make_switch_factory
  from fiti.p4_mininet import P4Host
  topo = build_topology(with_hosts=True)
  net = Mininet(topo=topo, host=P4Host,
                switch=make_switch_factory(compile_p4(), 9090),
                controller=None)

  # 纯元数据接口 (不需要 root)
  from fiti.topo import (load_topology, compute_degrees, node_by_id,
                         switch_name, host_name, host_ip)

  data = load_topology()
  print(len(data['nodes']), 'nodes,', len(data['links']), 'links')

注意: bmv2 进程不会由 sudo mn -c 清理; 异常退出后可用
sudo pkill -f simple_switch 清理。
"""

import argparse
import json
import os
import sys
from contextlib import contextmanager

from mininet.cli import CLI
from mininet.link import Link, TCLink
from mininet.log import info, setLogLevel, warn
from mininet.net import Mininet
from mininet.topo import Topo

from fiti.log import Progress, quiet_mininet_console
from fiti.p4 import (DEFAULT_P4_JSON, DEFAULT_THRIFT_BASE,
                     choose_thrift_base, compile_p4, install_fabric,
                     make_switch_factory)
from fiti.traffic import TrafficHost, run_random_traffic
from fiti.p4_mininet import P4Host
from fiti.paths import DEFAULT_TOPO_FILE

__all__ = [
    'DEFAULT_TOPO_FILE',
    'DEFAULT_BW_MAP',
    'DEFAULT_P4_JSON',
    'DEFAULT_THRIFT_BASE',
    'node_enabled',
    'effective_topology',
    'FitiTopo',
    'build_topology',
    'build_net',
    'fiti_network',
    'build_base_host_plan',
    'build_traffic_hosts',
    'load_topology',
    'validate_topology',
    'compute_degrees',
    'node_by_id',
    'switch_name',
    'switch_dpid',
    'host_name',
    'host_ip',
    'link_bandwidth',
    'get_host',
    'get_switch',
    'dump_topology',
    'topos',
]

# link.weight -> 带宽 (Mbps)。weight 是 fiti.json 里的链路容量等级。
DEFAULT_BW_MAP = {4: 100, 2: 50, 1: 10}
DEFAULT_BW_MAP_STR = '4:100,2:50,1:10'

# 主机 IP 模板: 10.0.0.<节点id>/24
HOST_IP_FORMAT = '10.0.0.%d/24'

# 基础拓扑中所有主机同属一个 VLAN
BASE_VLAN = 100


# --------------------------------------------------------------------------
# 拓扑元数据接口 (供库调用)
# --------------------------------------------------------------------------

def switch_name(node_id):
    """节点 id -> 交换机名, 如 1 -> 's1'。"""
    return 's%s' % node_id


def switch_dpid(node_id):
    """节点 id -> 16 位十六进制 dpid, 如 1 -> '0000000000000001'。"""
    return '%016x' % int(node_id)


def host_ip(node_id):
    """节点 id -> 主机 IP (含 /24 掩码), 如 1 -> '10.0.0.1/24'。"""
    return HOST_IP_FORMAT % int(node_id)


def node_by_id(data, node_id):
    """按 id 取节点 dict; id 支持 int/str。找不到时抛 KeyError。"""
    target = str(node_id)
    for node in data['nodes']:
        if str(node['id']) == target:
            return node
    raise KeyError('未知节点 id: %s' % node_id)


def host_name(data, node_id):
    """节点 id -> 主机名 (即节点的 abbr), 如 1 -> 'bj'。"""
    return node_by_id(data, node_id)['abbr']


def link_bandwidth(weight, bw_map=None):
    """链路 weight -> 带宽(Mbps); bw_map 为 None 时使用 DEFAULT_BW_MAP。"""
    mapping = DEFAULT_BW_MAP if bw_map is None else bw_map
    return mapping.get(weight)


def get_host(net, node_id, data=None):
    """从 Mininet 实例中按节点 id 取主机对象, 如 get_host(net, 1) -> bj。

    data 省略时读取 DEFAULT_TOPO_FILE。
    """
    data = data or load_topology()
    return net.get(host_name(data, node_id))


def get_switch(net, node_id):
    """从 Mininet 实例中按节点 id 取交换机对象, 如 get_switch(net, 1) -> s1。"""
    return net.get(switch_name(node_id))


def build_base_host_plan(data, vlan=BASE_VLAN):
    """基础拓扑的主机规划: 每个启用节点一台主机, 全部在同一个 VLAN。"""
    data = effective_topology(data)
    plan = {}
    for node in data['nodes']:
        node_id = str(node['id'])
        name = node['abbr']
        plan[name] = {
            'host': name,
            'slice': None,
            'slice_id': 'base',
            'slice_index': 1,
            'node_id': int(node_id),
            'node_name': node['name'],
            'abbr': node['abbr'],
            'switch': switch_name(node_id),
            'vlan': int(vlan),
            'ip': host_ip(node_id),
        }
    return plan


def build_traffic_hosts(net, host_plan, group='base'):
    """把主机规划转换成 fiti_traffic.TrafficHost 列表。"""
    entries = []
    for name, meta in host_plan.items():
        host = net.get(name)
        if host is None:
            continue
        entries.append(TrafficHost(name=name,
                                   node_id=int(meta['node_id']),
                                   group=group,
                                   host=host,
                                   ip=host.IP()))
    return entries


# --------------------------------------------------------------------------
# 拓扑文件读取与校验
# --------------------------------------------------------------------------

def node_enabled(node):
    """返回节点是否应在 Mininet 中启动。

    ``fiti.json`` 的节点字段 ``enabled`` 缺省为 true；显式设为 false 时，
    该节点不会创建 bmv2 交换机和主机，相关链路也会被过滤。
    """
    value = node.get('enabled', True)
    if not isinstance(value, bool):
        raise SystemExit('节点 %s 的 enabled 必须是 true/false'
                         % node.get('id', '?'))
    return value


def _filter_enabled_topology(data):
    raw_nodes = data['nodes']
    raw_links = data['links']
    enabled_ids = set()
    disabled = []
    for node in raw_nodes:
        node_id = str(node['id'])
        if node_enabled(node):
            enabled_ids.add(node_id)
        else:
            disabled.append({'id': node_id,
                             'abbr': node.get('abbr', ''),
                             'name': node.get('name', '')})
    if not enabled_ids:
        raise SystemExit('fiti.json 没有已启用的节点；'
                         '请至少保留一个 enabled=true 的节点')

    nodes = [node for node in raw_nodes
             if str(node['id']) in enabled_ids]
    links = [link for link in raw_links
             if str(link['src']) in enabled_ids
             and str(link['dst']) in enabled_ids]

    # Keep the full-file counts/degrees for validation and display.  The
    # effective nodes/links above are what Mininet and the control plane use.
    source_degrees = {str(node['id']): 0 for node in raw_nodes}
    for link in raw_links:
        src, dst = str(link['src']), str(link['dst'])
        if src in source_degrees:
            source_degrees[src] += 1
        if dst in source_degrees:
            source_degrees[dst] += 1

    data['nodes'] = nodes
    data['links'] = links
    data['_disabled'] = disabled
    data['_source'] = {
        'nodes': len(raw_nodes),
        'links': len(raw_links),
        'degrees': source_degrees,
    }
    return data


def load_topology(path=None):
    """读取 fiti.json，按节点 enabled 字段返回实际启动的拓扑视图。"""
    path = path or DEFAULT_TOPO_FILE
    if not os.path.isfile(path):
        raise SystemExit('找不到拓扑文件: %s' % path)
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    for key in ('nodes', 'links'):
        if key not in data or not isinstance(data[key], list):
            raise SystemExit('拓扑文件缺少 "%s" 列表: %s' % (key, path))
    return _filter_enabled_topology(data)


def effective_topology(data=None):
    """返回已应用 enabled 过滤的拓扑数据。

    ``data`` 为 None 时读取默认 fiti.json；若传入的是原始 dict，
    则复制并过滤，避免调用方绕过 load_topology 时仍创建禁用节点。
    """
    if data is None:
        return load_topology()
    if '_source' in data:
        return data
    return _filter_enabled_topology(dict(data))


def compute_degrees(data):
    """以 links 为准计算每个节点的实际度数, 返回 {id: degree}。"""
    degrees = {str(node['id']): 0 for node in data['nodes']}
    for link in data['links']:
        src, dst = str(link['src']), str(link['dst'])
        if src in degrees:
            degrees[src] += 1
        if dst in degrees:
            degrees[dst] += 1
    return degrees


def validate_topology(data):
    """校验 id / abbr / 链路引用 / 重复链路, 并对比文件中的 degree 字段。

    以 links 为唯一事实来源, 返回告警字符串列表 (不抛异常)。
    """
    warnings = []
    nodes = data['nodes']
    links = data['links']

    ids = [str(node['id']) for node in nodes]
    abbrs = [node['abbr'] for node in nodes]
    if len(ids) != len(set(ids)):
        warnings.append('节点 id 存在重复')
    if len(abbrs) != len(set(abbrs)):
        dup = sorted({a for a in abbrs if abbrs.count(a) > 1})
        warnings.append('节点 abbr 存在重复: %s' % ', '.join(dup))

    known = set(ids)
    source = data.get('_source', {})
    degrees = source.get('degrees') or compute_degrees(data)
    seen = set()
    for link in links:
        src, dst = str(link['src']), str(link['dst'])
        if src not in known or dst not in known:
            warnings.append('链路引用了不存在的节点: %s - %s' % (src, dst))
            continue
        if src == dst:
            warnings.append('存在自环链路: %s - %s' % (src, dst))
        key = tuple(sorted((src, dst)))
        if key in seen:
            warnings.append('存在重复链路: %s - %s' % (src, dst))
        seen.add(key)

    if nodes:
        adjacency = {str(node['id']): [] for node in nodes}
        for link in links:
            src, dst = str(link['src']), str(link['dst'])
            if src in adjacency and dst in adjacency:
                adjacency[src].append(dst)
                adjacency[dst].append(src)
        start = next(iter(adjacency))
        visited = {start}
        stack = [start]
        while stack:
            current = stack.pop()
            for neighbor in adjacency[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        if len(visited) != len(adjacency):
            warnings.append('启用节点构成的拓扑不连通（%d/%d 个节点可达）'
                            % (len(visited), len(adjacency)))

    for node in nodes:
        declared = node.get('degree')
        actual = degrees[str(node['id'])]
        if declared is not None and int(declared) != actual:
            warnings.append(
                '节点 %s(%s) 的 degree 字段为 %s, 按 links 实际为 %d'
                % (node['id'], node['name'], declared, actual))

    stats = data.get('statistics', {})
    source_links = source.get('links', len(links))
    source_nodes = source.get('nodes', len(nodes))
    if stats.get('total_links') is not None and int(stats['total_links']) != source_links:
        warnings.append('statistics.total_links=%s 与拓扑文件 links 实际条数 %d 不一致'
                        % (stats['total_links'], source_links))
    if stats.get('total_nodes') is not None and int(stats['total_nodes']) != source_nodes:
        warnings.append('statistics.total_nodes=%s 与拓扑文件 nodes 实际个数 %d 不一致'
                        % (stats['total_nodes'], source_nodes))
    return warnings


def _warn_issues(data):
    for message in validate_topology(data):
        warn('*** 拓扑告警: %s\n' % message)


def dump_topology(data, bw_map=None, path=None):
    """打印拓扑概况 (无需 root)。"""
    nodes = data['nodes']
    links = data['links']
    degrees = compute_degrees(data)
    weight_count = {}
    for link in links:
        weight = link.get('weight')
        weight_count[weight] = weight_count.get(weight, 0) + 1

    print('拓扑文件: %s' % os.path.abspath(path or DEFAULT_TOPO_FILE))
    print('数据平面: bmv2 simple_switch + p4src/fiti_slice.p4')
    disabled = data.get('_disabled', [])
    source = data.get('_source', {})
    if disabled:
        print('节点: %d 启用 / %d 总数（禁用 %d）   链路: %d 启用 / %d 总数'
              % (len(nodes), source.get('nodes', len(nodes)), len(disabled),
                 len(links), source.get('links', len(links))))
        print('已禁用节点: %s'
              % ', '.join('%s(%s)' % (item.get('name', ''), item['abbr'])
                          for item in disabled))
    else:
        print('节点: %d   链路: %d' % (len(nodes), len(links)))
    print('链路 weight 分布: %s'
          % ', '.join('weight=%s: %d 条' % (w, c)
                      for w, c in sorted(weight_count.items(),
                                         key=lambda kv: (kv[0] is None, kv[0]))))
    if bw_map:
        print('带宽映射 (Mbps): %s'
              % ', '.join('weight=%d -> %g' % (w, bw)
                          for w, bw in sorted(bw_map.items())))
    else:
        print('带宽映射: 未启用 (--no-tc)')

    print()
    print('%-4s %-12s %-8s %-8s %-8s %s'
          % ('id', 'name', 'abbr', 'switch', 'host',
             'degree(active/declared)'))
    for node in nodes:
        nid = str(node['id'])
        print('%-4s %-12s %-8s %-8s %-8s %d(%s)'
              % (nid, node['name'], node['abbr'], switch_name(nid), node['abbr'],
                 degrees[nid], node.get('degree', '?')))

    print()
    print('链路列表 (src - dst, weight, bw):')
    for link in links:
        weight = link.get('weight')
        bw = link_bandwidth(weight, bw_map)
        bandwidth = ('%g Mbps' % bw) if bw else '-'
        print('  %s - %s   weight=%s   bw=%s'
              % (switch_name(link['src']), switch_name(link['dst']),
                 weight, bandwidth))


# --------------------------------------------------------------------------
# Mininet 拓扑
# --------------------------------------------------------------------------

class FitiTopo(Topo):
    """按 fiti.json 生成 Mininet 拓扑。

    参数:
      data:       fiti.json 解析后的 dict; 为 None 时读取同目录 fiti.json
      with_hosts: 是否为每个节点挂一台主机
      bw_map:     {weight: 带宽Mbps}, 用于设置交换机间链路带宽
    """

    def build(self, data=None, with_hosts=True, bw_map=None):
        data = effective_topology(data)
        if bw_map is None:
            bw_map = DEFAULT_BW_MAP
        by_id = {str(node['id']): node for node in data['nodes']}

        for node in data['nodes']:
            nid = str(node['id'])
            self.addSwitch(switch_name(nid), dpid=switch_dpid(nid))
            if with_hosts:
                host = host_name(data, nid)
                self.addHost(host, ip=host_ip(nid))
                self.addLink(host, switch_name(nid))

        for link in data['links']:
            src, dst = str(link['src']), str(link['dst'])
            if src not in by_id or dst not in by_id:
                raise ValueError('链路引用了不存在的节点: %s - %s' % (src, dst))
            opts = {}
            bw = bw_map.get(link.get('weight'))
            if bw:
                # 用 TBF 代替默认 HTB, 避免 "quantum of class ... is big" 警告
                opts = {'bw': bw, 'use_tbf': True, 'latency_ms': 20}
            self.addLink(switch_name(src), switch_name(dst), **opts)


# 供 `mn --custom fiti_topo.py --topo fiti` 使用 (需要外部提供 P4Switch)
topos = {'fiti': FitiTopo}


def build_topology(data=None, with_hosts=True, bw_map=None):
    """构造并返回 FitiTopo 对象 (不启动任何进程, 无需 root)。

    data 为 None 时读取 DEFAULT_TOPO_FILE; bw_map 为 None 时使用 DEFAULT_BW_MAP。
    """
    return FitiTopo(data=data, with_hosts=with_hosts, bw_map=bw_map)


def build_net(data=None, with_hosts=True, bw_map=None, use_tc=True,
              p4_json=None, thrift_base=None, p4_log_level='info',
              pcap_dump=None, log_dir=None, spanning_root=None,
              install_rules=True, start=True):
    """创建 (并默认启动) FITI 拓扑对应的 bmv2 网络。

    参数:
      data:          拓扑 dict; None 时读取 DEFAULT_TOPO_FILE
      with_hosts:    是否为每个节点挂主机
      bw_map:        {weight: Mbps}; None 时使用 DEFAULT_BW_MAP
      use_tc:        是否使用 TCLink (False 时不做带宽限制)
      p4_json:       编译后的 P4 JSON; None 时编译并缓存在 build/fiti_slice.json
      thrift_base:   bmv2 Thrift 端口基址, 交换机 s<id> 使用 base+id;
                     None 时自动寻找一段空闲端口
      p4_log_level:  bmv2 日志级别
      pcap_dump:     抓包目录; None 表示不抓包
      log_dir:       bmv2 日志目录; None 时使用项目 build/logs/
      spanning_root: 生成树根节点 id; None 时使用第一个节点
      install_rules: 启动后是否下发 P4 流表
      start:         True 时创建后立即 net.start(); False 则只 build

    返回:
      Mininet 实例; 调用方负责 net.stop()。
      已启动且 install_rules=True 时, net.p4_fabric 保存规则规划结果。
    """
    data = effective_topology(data)
    _warn_issues(data)

    if not use_tc:
        bw_map = {}
    elif bw_map is None:
        bw_map = DEFAULT_BW_MAP

    topo = build_topology(data=data, with_hosts=with_hosts, bw_map=bw_map)

    json_path = p4_json or DEFAULT_P4_JSON
    if start:
        json_path = compile_p4(out=json_path)

    base = thrift_base
    if base is None:
        base = choose_thrift_base(data)

    if pcap_dump:
        pcap_dump = os.path.abspath(pcap_dump)
        os.makedirs(pcap_dump, exist_ok=True)

    switch_factory = make_switch_factory(json_path, base,
                                         p4_log_level=p4_log_level,
                                         pcap_dump=pcap_dump,
                                         log_dir=log_dir)
    net = Mininet(topo=topo,
                  switch=switch_factory,
                  host=P4Host,
                  controller=None,
                  link=TCLink if use_tc else Link,
                  autoSetMacs=True,
                  build=False)
    net.p4_json = os.path.abspath(json_path)
    net.thrift_base = base
    net.p4_host_plan = build_base_host_plan(data) if with_hosts else {}
    net.p4_fabric = None

    build_names = set(topo.hosts()) | set(topo.switches())
    try:
        with quiet_mininet_console(log_dir, allow_build_lists=True,
                                   allow_names=build_names):
            net.build()
        if start:
            with quiet_mininet_console(log_dir):
                with Progress(
                        '*** 启动 bmv2 交换机',
                        done='*** %d 台 bmv2 交换机已启动'
                             % len(data['nodes'])):
                    net.start()

        if start and with_hosts and install_rules:
            with Progress('*** 下发 P4 流表',
                          total=len(data['nodes']),
                          done='*** P4 流表下发完成') as progress:
                net.p4_fabric = install_fabric(
                    net, data, net.p4_host_plan,
                    json_path=json_path, root=spanning_root,
                    log_dir=log_dir, progress=progress)
    except (Exception, KeyboardInterrupt):
        net.stop()
        raise
    return net


@contextmanager
def fiti_network(**kwargs):
    """build_net() 的上下文管理器, 退出时自动 net.stop()。

    用法:
        with fiti_network() as net:
            net.pingAll()
    """
    net = build_net(**kwargs)
    try:
        yield net
    finally:
        net.stop()


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def parse_bw_map(text):
    """解析 '4:100,2:50,1:10' 形式的带宽映射。"""
    mapping = {}
    for item in text.split(','):
        item = item.strip()
        if not item:
            continue
        try:
            weight, bw = item.split(':')
            mapping[int(weight)] = float(bw)
        except ValueError:
            raise SystemExit('无法解析 --bw-map 项 %r (示例: 4:100,2:50,1:10)' % item)
    if not mapping:
        raise SystemExit('--bw-map 不能为空')
    return mapping


def build_parser():
    parser = argparse.ArgumentParser(
        description='用 Mininet + bmv2 模拟 fiti.json 中的 FITI 骨干拓扑',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='切片模式:\n'
               '  python3 fiti_topo.py --slices configs/slices.json [选项]\n'
               '  等价于运行 fiti_slice.py，切片选项见 fiti_slice.py --help')
    parser.add_argument('--topo-file', default=DEFAULT_TOPO_FILE,
                        help='拓扑 JSON 路径 (默认: %(default)s)')
    parser.add_argument('--dump', action='store_true',
                        help='只打印拓扑统计信息后退出, 不启动 Mininet')
    parser.add_argument('--no-hosts', action='store_true',
                        help='只创建启用节点的交换机, 不创建主机')
    parser.add_argument('--no-tc', action='store_true',
                        help='使用普通链路 (不做带宽限制, 不需要 TCLink)')
    parser.add_argument('--bw-map', default=DEFAULT_BW_MAP_STR,
                        help='weight 到带宽(Mbps)的映射, 默认 4:100,2:50,1:10')
    parser.add_argument('--p4-json', default=DEFAULT_P4_JSON,
                        help='P4 编译产物的输出/缓存路径 (默认: %(default)s)')
    parser.add_argument('--thrift-base', type=int, default=None,
                        help='bmv2 Thrift 端口基址 (默认自动分配, 交换机 s<id> 用 base+id)')
    parser.add_argument('--p4-log-level', default='info',
                        choices=('trace', 'debug', 'info', 'warn', 'error', 'off'),
                        help='bmv2 日志级别 (默认: %(default)s)')
    parser.add_argument('--pcap', action='store_true',
                        help='让 bmv2 抓包到 --pcap-dir')
    parser.add_argument('--pcap-dir', default='pcap',
                        help='抓包目录 (默认: %(default)s)')
    parser.add_argument('--log-dir', default=None,
                        help='bmv2 日志目录 (默认: 项目 build/logs/)')
    parser.add_argument('--spanning-root', type=int, default=None,
                        help='生成树根节点 id (默认使用第一个节点)')
    parser.add_argument('--pingall', action='store_true',
                        help='启动完成后先执行 pingall, 再进入 CLI')
    parser.add_argument('--random-traffic', action='store_true',
                        help='启动后在不同节点之间随机选主机互 ping，完成后退出')
    parser.add_argument('--traffic-continuous', action='store_true',
                        help='持续随机发包；每轮结束后重新选流，Ctrl-C 停止')
    parser.add_argument('--traffic-scope', choices=('global', 'slice'),
                        default='global',
                        help='随机流范围（基础模式所有主机同组，默认 global）')
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
    argv = list(sys.argv[1:] if argv is None else argv)

    # 统一入口：出现 --slices 时转交切片实现，避免维护两套命令行
    if any(arg == '--slices' or arg.startswith('--slices=') for arg in argv):
        from fiti.slice import main as slice_main
        return slice_main(argv)

    args = build_parser().parse_args(argv)
    setLogLevel(args.log)
    if args.traffic_continuous:
        args.random_traffic = True

    data = load_topology(args.topo_file)
    bw_map = {} if args.no_tc else parse_bw_map(args.bw_map)

    if args.dump:
        _warn_issues(data)
        dump_topology(data, bw_map, args.topo_file)
        return 0

    if os.geteuid() != 0:
        raise SystemExit('Mininet/bmv2 需要 root 权限, 请使用 sudo 运行。'
                         '(只想查看拓扑可以加 --dump)')

    net = None
    try:
        net = build_net(data=data,
                        with_hosts=not args.no_hosts,
                        bw_map=bw_map,
                        use_tc=not args.no_tc,
                        p4_json=args.p4_json,
                        thrift_base=args.thrift_base,
                        p4_log_level=args.p4_log_level,
                        pcap_dump=args.pcap_dir if args.pcap else None,
                        log_dir=args.log_dir,
                        spanning_root=args.spanning_root,
                        start=True)

        if args.random_traffic:
            if not net.hosts:
                warn('*** 未创建主机 (--no-hosts), 无法执行随机通信\n')
                return 1
            hosts = build_traffic_hosts(net, net.p4_host_plan)
            _, ok = run_random_traffic(
                hosts,
                count=args.traffic_flows,
                duration=args.traffic_duration,
                interval=args.traffic_interval,
                size=args.traffic_size,
                scope=args.traffic_scope,
                seed=args.traffic_seed,
                continuous=args.traffic_continuous)
            return 0 if ok else 1

        if args.pingall:
            if not net.hosts:
                warn('*** 未创建主机 (--no-hosts), 跳过 pingall\n')
            else:
                info('*** 全量 pingall (%d 台主机)\n' % len(net.hosts))
                net.pingAll()
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
