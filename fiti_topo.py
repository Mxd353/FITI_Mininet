#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Mininet 模拟 fiti.json 描述的 FITI 骨干拓扑。

拓扑规模: 41 个节点 / 43 条链路 (以 fiti.json 的 nodes/links 为准)。

建模方式:
  - 每个节点 <id> 建一台 OVS 交换机 s<id>, dpid 固定为 <id> 的 16 位十六进制;
  - 每个节点再挂一台主机, 主机名取节点的 abbr (如 bj/sh/xa), IP 为 10.0.0.<id>/24;
  - 交换机之间按 fiti.json 的 links 互联, link.weight (4/2/1) 映射为链路带宽等级;
  - 交换机与主机之间也走 TCLink, 但不限带宽。

命令行用法:
  sudo python3 fiti_topo.py                    # 启动 Mininet CLI (默认 OVS standalone + STP)
  sudo python3 fiti_topo.py --pingall          # 启动后等 STP 收敛并全量 ping, 再进 CLI
  sudo python3 fiti_topo.py --no-tc            # 使用普通链路, 不做带宽限制
  sudo python3 fiti_topo.py --no-hosts         # 只建 41 台交换机, 构建纯骨干网
  sudo python3 fiti_topo.py --dump             # 只打印拓扑统计, 不启动 Mininet (免 root)
  sudo python3 fiti_topo.py --slices slices.json   # 统一入口: 转交 fiti_slice.py 运行切片模式

  sudo python3 fiti_topo.py --controller remote --controller-ip 127.0.0.1 --controller-port 6653
  sudo python3 fiti_topo.py --controller default   # Mininet 自带 ovs-testcontroller
  sudo python3 fiti_topo.py --bw-map 4:1000,2:500,1:100

  也可以作为 mn 的 custom 拓扑加载:
  sudo mn --custom fiti_topo.py --topo fiti --link tc

======================================================================
作为库使用 (其他 Python 代码直接 import):
======================================================================

  from fiti_topo import build_net, fiti_network, build_topology

  # 1) 拿到一个已经启动的 Mininet 实例, 用完自己 stop()
  from fiti_topo import build_net, get_host
  net = build_net(controller='remote')          # 对接外部控制器
  bj, sh = get_host(net, 1), get_host(net, 2)   # 按节点 id 取主机
  print(bj.cmd('ping -c 3', sh.IP()))
  net.stop()

  # 2) 上下文管理器, 自动 stop()
  with fiti_network(controller='stp', wait_connected=60) as net:
      net.pingAll()

  # 3) 只要 Topo 对象, 自己组装 Mininet / 传给别的工具
  from mininet.net import Mininet
  from mininet.node import RemoteController
  topo = build_topology(with_hosts=True)
  net = Mininet(topo=topo, controller=RemoteController)

  # 4) 纯元数据接口 (不需要 root)
  from fiti_topo import (load_topology, compute_degrees, node_by_id,
                         switch_name, host_name, host_ip)

  data = load_topology()
  print(len(data['nodes']), 'nodes,', len(data['links']), 'links')
  print(node_by_id(data, 1)['name'], host_name(data, 1), host_ip(1))

注意: 若上次异常退出导致 OVS 残留, 先执行 sudo mn -c 清理。
"""

import argparse
import json
import os
import sys
from contextlib import contextmanager
from functools import partial

from mininet.cli import CLI
from mininet.link import Link, TCLink
from mininet.log import info, setLogLevel, warn
from mininet.net import Mininet
from mininet.node import (DefaultController, OVSBridge, OVSSwitch,
                          RemoteController)
from mininet.topo import Topo

__all__ = [
    'DEFAULT_TOPO_FILE',
    'DEFAULT_BW_MAP',
    'FitiTopo',
    'build_topology',
    'build_net',
    'fiti_network',
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TOPO_FILE = os.path.join(SCRIPT_DIR, 'fiti.json')

# link.weight -> 带宽 (Mbps)。weight 是 fiti.json 里的链路容量等级。
DEFAULT_BW_MAP = {4: 100, 2: 50, 1: 10}
DEFAULT_BW_MAP_STR = '4:100,2:50,1:10'
DEFAULT_STP_WAIT = 60

# 主机 IP 模板: 10.0.0.<节点id>/24
HOST_IP_FORMAT = '10.0.0.%d/24'


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


# --------------------------------------------------------------------------
# 拓扑文件读取与校验
# --------------------------------------------------------------------------

def load_topology(path=None):
    """读取 fiti.json 并做基本结构检查, 返回原始 dict。"""
    path = path or DEFAULT_TOPO_FILE
    if not os.path.isfile(path):
        raise SystemExit('找不到拓扑文件: %s' % path)
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    for key in ('nodes', 'links'):
        if key not in data or not isinstance(data[key], list):
            raise SystemExit('拓扑文件缺少 "%s" 列表: %s' % (key, path))
    return data


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
    degrees = compute_degrees(data)
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

    for node in nodes:
        declared = node.get('degree')
        actual = degrees[str(node['id'])]
        if declared is not None and int(declared) != actual:
            warnings.append(
                '节点 %s(%s) 的 degree 字段为 %s, 按 links 实际为 %d'
                % (node['id'], node['name'], declared, actual))

    stats = data.get('statistics', {})
    if stats.get('total_links') is not None and int(stats['total_links']) != len(links):
        warnings.append('statistics.total_links=%s 与 links 实际条数 %d 不一致'
                        % (stats['total_links'], len(links)))
    if stats.get('total_nodes') is not None and int(stats['total_nodes']) != len(nodes):
        warnings.append('statistics.total_nodes=%s 与 nodes 实际个数 %d 不一致'
                        % (stats['total_nodes'], len(nodes)))
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
          % ('id', 'name', 'abbr', 'switch', 'host', 'degree(declared)'))
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
      stp:        是否开启 STP (拓扑有环, standalone 模式下必须开启)
    """

    def build(self, data=None, with_hosts=True, bw_map=None, stp=True):
        if data is None:
            data = load_topology()
        if bw_map is None:
            bw_map = DEFAULT_BW_MAP
        by_id = {str(node['id']): node for node in data['nodes']}

        for node in data['nodes']:
            nid = str(node['id'])
            self.addSwitch(switch_name(nid), dpid=switch_dpid(nid), stp=stp)
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
                # 用 TBF 代替默认 HTB，避免 "quantum of class ... is big" 警告
                opts = {'bw': bw, 'use_tbf': True, 'latency_ms': 20}
            self.addLink(switch_name(src), switch_name(dst), **opts)


# 供 `mn --custom fiti_topo.py --topo fiti` 使用
topos = {'fiti': FitiTopo}


def build_topology(data=None, with_hosts=True, bw_map=None, stp=True):
    """构造并返回 FitiTopo 对象 (不启动任何进程, 无需 root)。

    data 为 None 时读取 DEFAULT_TOPO_FILE; bw_map 为 None 时使用 DEFAULT_BW_MAP。
    """
    return FitiTopo(data=data, with_hosts=with_hosts,
                    bw_map=bw_map, stp=stp)


def build_net(data=None, with_hosts=True, bw_map=None, use_tc=True,
              controller='stp', controller_ip='127.0.0.1',
              controller_port=6653, stp=True, wait_connected=False,
              stp_wait=DEFAULT_STP_WAIT, auto_set_macs=True, switch_cls=None,
              start=True):
    """创建 (并默认启动) FITI 拓扑对应的 Mininet 网络。

    参数:
      data:            拓扑 dict; None 时读取 DEFAULT_TOPO_FILE
      with_hosts:      是否为每个节点挂主机
      bw_map:          {weight: Mbps}; None 时使用 DEFAULT_BW_MAP
      use_tc:          是否使用 TCLink (False 时不做带宽限制)
      controller:      'stp' (OVS standalone + STP, 默认)
                       'default' (Mininet 自带 ovs-testcontroller)
                       'remote'  (外部控制器, 配合 controller_ip/port)
                       或任意 Controller 类 / 实例 / partial
      stp:             是否给交换机开启 STP (拓扑有环)
      wait_connected:  True 表示等待 STP 收敛 (stp 模式, 上限 stp_wait 秒);
                       数字表示等待秒数; False 表示不等待
      stp_wait:        stp 模式等待 STP 收敛的最长秒数
      auto_set_macs:   是否按 IP 自动生成确定性 MAC
      switch_cls:      自定义交换机类, None 时按 controller 自动选择
      start:           True 时创建后立即 net.start(); False 则只 build

    返回:
      Mininet 实例; 调用方负责 net.stop()。
    """
    if data is None:
        data = load_topology()
    _warn_issues(data)

    if not use_tc:
        bw_map = {}
    elif bw_map is None:
        bw_map = DEFAULT_BW_MAP

    topo = build_topology(data=data, with_hosts=with_hosts,
                          bw_map=bw_map, stp=stp)

    if isinstance(controller, str):
        if controller == 'stp':
            switch_cls = switch_cls or OVSBridge
            controller_obj = None
            # True -> 用 stp_wait 秒作为 STP 收敛等待上限
            wait = stp_wait if wait_connected is True else wait_connected
        elif controller == 'default':
            switch_cls = switch_cls or OVSSwitch
            controller_obj = DefaultController
            wait = wait_connected
        elif controller == 'remote':
            switch_cls = switch_cls or OVSSwitch
            controller_obj = partial(RemoteController,
                                     ip=controller_ip,
                                     port=controller_port)
            wait = wait_connected
        else:
            raise ValueError(
                "controller 必须是 'stp' / 'default' / 'remote' "
                '或自定义 Controller 类/实例')
    else:
        switch_cls = switch_cls or OVSSwitch
        controller_obj = controller
        wait = wait_connected

    net = Mininet(topo=topo,
                  switch=switch_cls,
                  controller=controller_obj,
                  link=TCLink if use_tc else Link,
                  autoSetMacs=auto_set_macs,
                  waitConnected=wait,
                  build=False)
    net.build()
    if start:
        net.start()
    return net


@contextmanager
def fiti_network(**kwargs):
    """build_net() 的上下文管理器, 退出时自动 net.stop()。

    用法:
        with fiti_network(controller='stp', wait_connected=60) as net:
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
        description='用 Mininet 模拟 fiti.json 中的 FITI 骨干拓扑',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='切片模式:\n'
               '  python3 fiti_topo.py --slices slices.json [选项]\n'
               '  等价于运行 fiti_slice.py，切片选项见 fiti_slice.py --help')
    parser.add_argument('--topo-file', default=DEFAULT_TOPO_FILE,
                        help='拓扑 JSON 路径 (默认: %(default)s)')
    parser.add_argument('--dump', action='store_true',
                        help='只打印拓扑统计信息后退出, 不启动 Mininet')
    parser.add_argument('--no-hosts', action='store_true',
                        help='只创建交换机, 不创建主机')
    parser.add_argument('--no-tc', action='store_true',
                        help='使用普通链路 (不做带宽限制, 不需要 TCLink)')
    parser.add_argument('--bw-map', default=DEFAULT_BW_MAP_STR,
                        help='weight 到带宽(Mbps)的映射, 默认 4:100,2:50,1:10')
    parser.add_argument('--controller', choices=('stp', 'default', 'remote'),
                        default='stp',
                        help='stp=无控制器, OVS standalone 学习交换 + STP (默认); '
                             'default=内置 ovs-testcontroller; '
                             'remote=外部控制器')
    parser.add_argument('--controller-ip', default='127.0.0.1',
                        help='--controller remote 时外部控制器地址')
    parser.add_argument('--controller-port', type=int, default=6653,
                        help='--controller remote 时外部控制器端口')
    parser.add_argument('--wait-connected', action='store_true',
                        help='启动后等待交换机就绪 (stp 模式等待 STP 收敛; '
                             'default/remote 模式等待连上控制器)')
    parser.add_argument('--stp-wait', type=int, default=DEFAULT_STP_WAIT,
                        help='stp 模式下等待 STP 收敛的最长秒数, 默认 %(default)s')
    parser.add_argument('--pingall', action='store_true',
                        help='启动完成后先执行 pingall, 再进入 CLI')
    parser.add_argument('--log', default='info',
                        choices=('debug', 'info', 'warning', 'error', 'critical'),
                        help='Mininet 日志级别')
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # 统一入口：出现 --slices 时转交切片实现，避免维护两套命令行
    if any(arg == '--slices' or arg.startswith('--slices=') for arg in argv):
        from fiti_slice import main as slice_main
        return slice_main(argv)

    args = build_parser().parse_args(argv)
    setLogLevel(args.log)

    data = load_topology(args.topo_file)
    bw_map = {} if args.no_tc else parse_bw_map(args.bw_map)

    if args.dump:
        _warn_issues(data)
        dump_topology(data, bw_map, args.topo_file)
        return 0

    if os.geteuid() != 0:
        raise SystemExit('Mininet 需要 root 权限, 请使用 sudo 运行。'
                         '(只想查看拓扑可以加 --dump)')

    # stp 模式下 --pingall 也自动等待 STP 收敛; 其他模式不隐式等待控制器
    if args.controller == 'stp':
        wait_connected = args.wait_connected or args.pingall
    else:
        wait_connected = args.wait_connected

    info('*** 构建拓扑: %d 台交换机, %d 台主机, 骨干链路 %d 条\n'
         % (len(data['nodes']),
            len(data['nodes']) if not args.no_hosts else 0,
            len(data['links'])))
    if args.controller == 'stp' and not wait_connected:
        info('*** STP 模式: 首次 ping 前通常需等待 30-45 秒收敛 '
             '(或用 --wait-connected / --pingall 自动等待)\n')

    net = build_net(data=data,
                    with_hosts=not args.no_hosts,
                    bw_map=bw_map,
                    use_tc=not args.no_tc,
                    controller=args.controller,
                    controller_ip=args.controller_ip,
                    controller_port=args.controller_port,
                    wait_connected=wait_connected,
                    stp_wait=args.stp_wait,
                    start=True)
    try:
        if args.pingall:
            if not net.hosts:
                warn('*** 未创建主机 (--no-hosts), 跳过 pingall\n')
            else:
                info('*** 全量 pingall (%d 台主机)\n' % len(net.hosts))
                net.pingAll()
        CLI(net)
    finally:
        net.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
