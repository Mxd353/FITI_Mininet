#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the overlay/underlay path map for one or more parallel networks.

The output mirrors docs/REQUIREMENTS.md: a business path (which sites and
hosts) mapped to a physical device path, with the encapsulation of every
segment.  It is derived from the same config used by the simulation, so the
table cannot drift from the topology.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parallel.config import DEFAULT_CONFIG, ParallelConfig, load_config
from parallel.topology import cloud_name


def parse_host(cfg: ParallelConfig, text: str):
    """Accept ``bj``, ``bj_2`` or ``cl_bj_2``."""
    value = text.strip()
    if value.startswith('cl_'):
        value = value[3:]
    host_id = 1
    if '_' in value:
        abbr, suffix = value.rsplit('_', 1)
        if suffix.isdigit():
            host_id = int(suffix)
            value = abbr
    site = cfg.site(value)
    if host_id not in cfg.host_ids():
        raise SystemExit('主机编号超出范围: %s（hosts_per_site=%d）'
                         % (host_id, cfg.hosts_per_site))
    return site, host_id, cloud_name(site, host_id)


def build_segments(cfg: ParallelConfig, network, src, dst):
    """Return (segments, info) for one host-to-host path."""
    src_site, src_host, src_name = src
    dst_site, dst_host, dst_name = dst
    vlan = network.vlan
    inner_src = cfg.cloud_address(network, src_site, src_host)
    inner_dst = cfg.cloud_address(network, dst_site, dst_host)
    segments = []

    def add(left, right, encap, note=''):
        segments.append({'from': left, 'to': right,
                         'encap': encap, 'note': note})

    def tap(site, host):
        return cfg.tap_ifname(site, host, 1, network)

    def tunnel_note(site, host, peer):
        return ('主机 overlay：%s → %s（本机 veth，自环）'
                % (cfg.underlay_address(site, host),
                   cfg.underlay_address(site, peer)))

    def exit_host(site, host, name):
        """VM → tap → 桥 →（必要时 VXLAN）→ 站点交换机。"""
        add(name, tap(site, host), 'untagged', 'VM 网卡，普通 IP 报文')
        add(tap(site, host), cfg.bridge_name(site, host), 'untagged',
            '宿主机桥 access 口（VLAN %d）' % vlan)
        bridge = cfg.bridge_name(site, host)
        if host != 1:
            peer = cfg.bridge_name(site, 1)
            add(bridge, peer, 'VXLAN VNI %d' % network.id,
                tunnel_note(site, host, 1))
            bridge = peer
        add(bridge, 'S1_%s' % site.abbr, 'VLAN %d' % vlan,
            '桥上联到站点交换机')

    def enter_host(site, host, name):
        """站点交换机 →（必要时 VXLAN）→ 桥 → tap → VM。"""
        bridge = cfg.bridge_name(site, host)
        if host != 1:
            peer = cfg.bridge_name(site, 1)
            add('S1_%s' % site.abbr, peer, 'VLAN %d' % vlan,
                '交付到本站点第一台 PM')
            add(peer, bridge, 'VXLAN VNI %d' % network.id,
                tunnel_note(site, 1, host))
        else:
            add('S1_%s' % site.abbr, bridge, 'VLAN %d' % vlan,
                '交付到目的 PM 的桥')
        add(bridge, tap(site, host), 'untagged', '宿主机桥 access 口')
        add(tap(site, host), name, 'untagged', '交付给目的 VM')

    if src_site.abbr == dst_site.abbr:
        if src_host == dst_host:
            add(src_name, tap(src_site, src_host), 'untagged',
                'VM 网卡，普通 IP 报文')
            add(tap(src_site, src_host),
                cfg.bridge_name(src_site, src_host), 'untagged',
                '宿主机桥 access 口（VLAN %d）' % vlan)
            add(cfg.bridge_name(src_site, src_host),
                tap(dst_site, dst_host), 'untagged',
                '同一台 PM 内的桥转交')
            add(tap(dst_site, dst_host), dst_name, 'untagged',
                '交付给目的 VM')
        else:
            add(src_name, tap(src_site, src_host), 'untagged',
                'VM 网卡，普通 IP 报文')
            add(tap(src_site, src_host),
                cfg.bridge_name(src_site, src_host), 'untagged',
                '宿主机桥 access 口（VLAN %d）' % vlan)
            add(cfg.bridge_name(src_site, src_host),
                cfg.bridge_name(dst_site, dst_host),
                'VXLAN VNI %d' % network.id,
                tunnel_note(src_site, src_host, dst_host))
            add(cfg.bridge_name(dst_site, dst_host),
                tap(dst_site, dst_host), 'untagged', '对端 PM 的 access 口')
            add(tap(dst_site, dst_host), dst_name, 'untagged',
                '交付给目的 VM')
        return segments, {
            'inner': '%s → %s' % (inner_src, inner_dst),
            'note': 'same-site',
        }

    exit_host(src_site, src_host, src_name)
    add('S1_%s' % src_site.abbr, 'E_%s' % src_site.abbr,
        'VLAN %d' % vlan, '上行到 PE')

    if network.vpn == 'l3':
        sa = cfg.srv6_sid(network, src_site)
        da = cfg.srv6_sid(network, dst_site)
        encap = 'SRv6 L3  SA=%s  DA=%s' % (sa, da)
        decap_note = '解封装后按内层 /128 查表投递到主机'
    else:
        sa = cfg.l2_sid(network, src_site)
        da = cfg.l2_sid(network, dst_site)
        encap = 'SRv6 L2  SA=%s  DA=%s' % (sa, da)
        decap_note = '解封装后按 VSI 的 VLAN 送 S1'

    add('E_%s' % src_site.abbr, 'C_%s' % src_site.abbr,
        encap, 'PE 封装并交给核心')

    allowed = {site.abbr for site in cfg.sites}
    core_path = cfg.fiti.path(src_site.abbr, dst_site.abbr, allowed=allowed)
    for left, right in zip(core_path, core_path[1:]):
        add('C_%s' % left, 'C_%s' % right, 'IPv6 外层（SRv6）', '核心转发')

    add('C_%s' % dst_site.abbr, 'E_%s' % dst_site.abbr,
        'IPv6 外层（SRv6）', '到达目的 PE')
    add('E_%s' % dst_site.abbr, 'S1_%s' % dst_site.abbr,
        'VLAN %d' % vlan, decap_note)
    enter_host(dst_site, dst_host, dst_name)

    return segments, {
        'inner': '%s → %s' % (inner_src, inner_dst),
        'note': 'cross-site',
    }


def render_json(cfg, network, src, dst, segments, info):
    return json.dumps({
        'network': {'id': network.id, 'vlan': network.vlan,
                    'vpn': network.vpn},
        'business_path': '%s → %s' % (src[2], dst[2]),
        'inner': info['inner'],
        'segments': segments,
    }, ensure_ascii=False, indent=2) + '\n'


def render_text(cfg, network, src, dst, segments, info):
    lines = ['网络 %d（%s，VLAN %d）'
             % (network.id, network.vpn.upper(), network.vlan),
             '业务路径: %s → %s' % (src[2], dst[2]),
             '内层地址: %s' % info['inner'],
             '']
    for index, segment in enumerate(segments, 1):
        lines.append('%2d. %s → %s  [%s]  %s'
                     % (index, segment['from'], segment['to'],
                        segment['encap'], segment['note']))
    return '\n'.join(lines) + '\n'


def render_markdown(cfg, network, src, dst, segments, info):
    lines = ['## 网络 %d · %s · VLAN %d'
             % (network.id, network.vpn.upper(), network.vlan),
             '',
             '**业务路径**：`%s` → `%s`  ' % (src[2], dst[2]),
             '**内层地址**：`%s`' % info['inner'].replace(' → ', '` → `'),
             '',
             '| 段 | 物理路径 | 封装 | 说明 |',
             '| --- | --- | --- | --- |']
    for index, segment in enumerate(segments, 1):
        lines.append('| %d | `%s → %s` | %s | %s |'
                     % (index, segment['from'], segment['to'],
                        segment['encap'], segment['note']))
    lines.append('')
    return '\n'.join(lines)


RENDERERS = {
    'markdown': render_markdown,
    'text': render_text,
    'json': render_json,
}


def _default_pair(cfg):
    sites = cfg.sites
    if not sites:
        raise SystemExit('配置没有站点')
    src = (sites[0], 1, cloud_name(sites[0], 1))
    if len(sites) > 1:
        dst = (sites[1], 1, cloud_name(sites[1], 1))
    else:
        dst = (sites[0], min(2, cfg.hosts_per_site),
               cloud_name(sites[0], min(2, cfg.hosts_per_site)))
    return src, dst


def main(argv=None):
    parser = argparse.ArgumentParser(description='生成 overlay/underlay 路径映射')
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    parser.add_argument('--network', default='all',
                        help='网络 ID，或 all')
    parser.add_argument('--src', default=None, help='例如 bj、bj_2')
    parser.add_argument('--dst', default=None, help='例如 sh、sh_2')
    parser.add_argument('--same-site', action='store_true',
                        help='未指定 dst 时生成同站点主机间路径')
    parser.add_argument('--format', default='markdown',
                        choices=sorted(RENDERERS))
    parser.add_argument('--out', default=None)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.src:
        src = parse_host(cfg, args.src)
    else:
        src = _default_pair(cfg)[0]
    if args.dst:
        dst = parse_host(cfg, args.dst)
    elif args.same_site:
        dst = (src[0], 2 if cfg.hosts_per_site >= 2 else 1,
               cloud_name(src[0], 2 if cfg.hosts_per_site >= 2 else 1))
    else:
        dst = _default_pair(cfg)[1]

    if args.network == 'all':
        networks = list(cfg.networks)
    else:
        networks = [cfg.network(int(args.network))]

    renderer = RENDERERS[args.format]
    chunks = []
    for network in networks:
        segments, info = build_segments(cfg, network, src, dst)
        chunks.append(renderer(cfg, network, src, dst, segments, info))
    text = '\n'.join(chunks)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)),
                    exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as handle:
            handle.write(text)
        print('写入 %s（%d 行）' % (args.out, len(text.splitlines())))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
