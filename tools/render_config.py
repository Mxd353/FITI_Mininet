#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the parallel-network plan into simulation and device configs.

The point is to keep one source of truth (``configs/parallel*.json``) and
derive both the simulation artifacts and the Huawei-style CLI snippets from
it, so the two cannot drift apart before connecting to a real platform.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parallel.config import DEFAULT_CONFIG, load_config

PHY_PORT_L3 = '100GE3/0/40'
PHY_PORT_L2 = 'ten-gigabitethernet 3/1/1'


def _vrf_name(network):
    return 'zslice%04d' % network.id


def _sid(cfg, network, site):
    return (cfg.srv6_sid(network, site) if network.vpn == 'l3'
            else cfg.l2_sid(network, site))


def render_huawei(cfg):
    lines = []
    for site in sorted(cfg.sites, key=lambda item: item.id):
        lines.append('! ================================================')
        lines.append('! %s (%s)  站点 ID = %d'
                     % (site.name, site.abbr, site.id))
        lines.append('! ================================================')
        for network in sorted(cfg.networks_for_site(site),
                              key=lambda item: item.id):
            if network.vpn == 'l3':
                lines.extend(_huawei_l3(cfg, site, network))
            else:
                lines.extend(_huawei_l2(cfg, site, network))
            lines.append('')
        if site.abbr == 'bj':
            lines.extend(_huawei_public())
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def _huawei_l3(cfg, site, network):
    vrf = _vrf_name(network)
    interconnect = cfg.interconnect_network(network, site)
    business = cfg.business_network(network, site)
    return [
        'ip vpn-instance %s' % vrf,
        ' address-family ipv6',
        '  route-distinguisher 9425:%d' % network.id,
        '  vpn-target 9425:%d' % network.id,
        '  apply-label per-instance',
        '',
        'interface %s.%d' % (PHY_PORT_L3, network.vlan),
        ' vlan-type dot1q %d' % network.vlan,
        ' ip binding vpn-instance %s' % vrf,
        ' ipv6 address %s/64' % cfg.central_address(network, site),
        '',
        'ipv6 route-static vpn-instance %s %s %d %s tag %d'
        % (vrf, business.network_address, business.prefixlen,
           cfg.user_address(network, site), network.id),
    ]


def _huawei_l2(cfg, site, network):
    vsi = _vrf_name(network)
    return [
        'vsi %s' % vsi,
        ' evpn encapsulation srv6',
        '  route-distinguisher 9425:%d' % network.id,
        '  vpn-target 9425:%d' % network.id,
        '  segment-routing ipv6 best-effort',
        '  segment-routing ipv6 locator %s-e1' % site.abbr,
        '',
        'interface %s.%d' % (PHY_PORT_L2, network.vlan),
        ' vlan-type dot1q vid %d' % network.vlan,
        ' xconnect vsi %s' % vsi,
    ]


def _huawei_public():
    return [
        '! 公网接入（仅北京核心 C2）',
        'ip vpn-instance zslicefix',
        ' address-family ipv6',
        '  vpn-target 9425:39000 import-extcommunity',
        '  vpn-target 9425:39001 export-extcommunity',
        '',
        'bgp 9425',
        ' ip vpn-instance zslicefix',
        '  peer 2001:255:0:100::2000 as-number 38255',
        '  peer 2001:255:0:100::2000 fake-as 144704',
    ]


def render_sim(cfg):
    header = ('site\tid\thost\tendpoint\tnetwork\tvlan\tvpn\tbusiness\t'
              'interconnect\tcloud\tgateway\tsid')
    lines = [header]
    for site in sorted(cfg.sites, key=lambda item: item.id):
        for network in sorted(cfg.networks_for_site(site),
                              key=lambda item: item.id):
            for host_id in cfg.host_ids():
                for endpoint in cfg.endpoint_ids():
                    lines.append('\t'.join([
                        site.abbr, str(site.id), str(host_id),
                        str(endpoint), str(network.id),
                        str(network.vlan), network.vpn,
                        str(cfg.business_network(network, site)),
                        str(cfg.interconnect_network(network, site)),
                        cfg.cloud_address(network, site, host_id, endpoint),
                        (cfg.cloud_gateway(network, site, host_id, endpoint)
                         if network.vpn == 'l3'
                         else cfg.central_address(network, site)),
                        _sid(cfg, network, site),
                    ]))
    return '\n'.join(lines) + '\n'


def render_ovs(cfg):
    lines = []
    for site in sorted(cfg.sites, key=lambda item: item.id):
        lines.append('S1_%s  E口        trunks=1-4094' % site.abbr)
        for host_id in cfg.host_ids():
            lines.append('S1_%s  cl_%s_%d口  trunks=1-4094'
                         % (site.abbr, site.abbr, host_id))
        lines.append('S2_%s  A口        trunks=1-3500,3901-4094' % site.abbr)
        for host_id in cfg.host_ids():
            lines.append('S2_%s  cl_%s_%d口  trunks=1-4094'
                         % (site.abbr, site.abbr, host_id))
    return '\n'.join(lines) + '\n'


def render_summary(cfg):
    payload = {
        'config': os.path.abspath(cfg.path),
        'hosts_per_site': cfg.hosts_per_site,
        'endpoints_per_host': cfg.endpoints_per_host,
        'sites': [{'id': site.id, 'abbr': site.abbr, 'name': site.name}
                  for site in sorted(cfg.sites, key=lambda item: item.id)],
        'networks': [],
    }
    for network in sorted(cfg.networks, key=lambda item: item.id):
        payload['networks'].append({
            'id': network.id,
            'vlan': network.vlan,
            'vpn': network.vpn,
            'rd': '9425:%d' % network.id,
            'sites': list(network.sites),
            'sids': {site.abbr: _sid(cfg, network, site)
                     for site in cfg.network_sites(network)},
        })
    return json.dumps(payload, ensure_ascii=False, indent=2) + '\n'


RENDERERS = {
    'huawei': render_huawei,
    'sim': render_sim,
    'ovs': render_ovs,
    'summary': render_summary,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description='渲染并行网络配置')
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    parser.add_argument('--target', default='huawei',
                        choices=sorted(RENDERERS))
    parser.add_argument('--out', default=None, help='输出文件，默认 stdout')
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    text = RENDERERS[args.target](cfg)
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
    sys.exit(main())
