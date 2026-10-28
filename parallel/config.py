#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并行网络配置、VLAN 和 IPv6 地址规划。"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from fiti.paths import PROJECT_ROOT

DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, 'configs', 'parallel.json')
DEFAULT_P4_JSON = os.path.join(PROJECT_ROOT, 'build', 'parallel_vpn.json')
DEFAULT_P4_SRC = os.path.join(PROJECT_ROOT, 'p4src', 'parallel_vpn.p4')


@dataclass(frozen=True)
class Site:
    id: int
    abbr: str
    name: str


@dataclass(frozen=True)
class Network:
    id: int
    vpn: str
    vlan: int
    sites: Tuple[str, ...] = ()


@dataclass
class ParallelConfig:
    path: str
    sites: List[Site]
    networks: List[Network]
    p4_json: str
    thrift_base: int
    p4_log_level: str
    l2_tunnel: str = 'srv6'
    underlay: Dict[str, str] = field(default_factory=dict)

    def site(self, abbr: str) -> Site:
        for item in self.sites:
            if item.abbr == abbr:
                return item
        raise KeyError('未知站点: %s' % abbr)

    def network(self, network_id: int) -> Network:
        for item in self.networks:
            if item.id == int(network_id):
                return item
        raise KeyError('未知并行网络: %s' % network_id)

    def l3_networks(self) -> List[Network]:
        return [item for item in self.networks if item.vpn == 'l3']

    def l2_networks(self) -> List[Network]:
        return [item for item in self.networks if item.vpn == 'l2']

    def network_sites(self, network: Network) -> List[Site]:
        return [self.site(abbr) for abbr in network.sites]

    def networks_for_site(self, site: Site) -> List[Network]:
        return [item for item in self.networks if site.abbr in item.sites]

    def l3_networks_for_site(self, site: Site) -> List[Network]:
        return [item for item in self.networks_for_site(site)
                if item.vpn == 'l3']

    def l2_networks_for_site(self, site: Site) -> List[Network]:
        return [item for item in self.networks_for_site(site)
                if item.vpn == 'l2']

    def business_network(self, network: Network, site: Site) -> ipaddress.IPv6Network:
        return ipaddress.IPv6Network(
            '240a:a806:%d:%d::/64' % (network.id, site.id))

    def l2_network(self, network: Network) -> ipaddress.IPv6Network:
        """L2VPN uses one flat /64 so all member sites share a broadcast domain."""
        return ipaddress.IPv6Network('240a:a806:%d:1::/64' % network.id)

    def vlan_ifname(self, network: Network) -> str:
        """Short Linux VLAN subinterface name inside the cloud netns."""
        return 'v%d' % network.vlan

    def cloud_mac(self, network: Network, site: Site) -> str:
        """Deterministic per-slice MAC on the single cloud host."""
        return '02:00:%02x:%02x:00:01' % (int(site.id) & 0xff,
                                          int(network.id) & 0xff)

    def srv6_sid(self, network: Network, site: Site) -> str:
        """Per-site, per-network SRv6 SID used by the static L3VPN PE."""
        return str(ipaddress.IPv6Address(
            'fd00:0:%d:1::%x' % (site.id, 0x100 + network.id)))

    def l2_sid(self, network: Network, site: Site) -> str:
        """Per-site, per-network SRv6 SID used by L2VPN."""
        return str(ipaddress.IPv6Address(
            'fd00:0:%d:1::%x' % (site.id, 0x200 + network.id)))

    def interconnect_network(self, network: Network,
                             site: Site) -> ipaddress.IPv6Network:
        return ipaddress.IPv6Network(
            '240a:a806:%d:%d::/64' % (network.id, 100 + site.id))

    def core_network(self, left: Site,
                     right: Site) -> ipaddress.IPv6Network:
        """北京星型核心链路地址；当前只定义 hub-remote 直连。"""
        hub = self.site('bj')
        if left.abbr == hub.abbr:
            peer = right
        elif right.abbr == hub.abbr:
            peer = left
        else:
            raise ValueError('核心采用北京星型拓扑，%s-%s 没有直连链路'
                             % (left.abbr, right.abbr))
        if peer.abbr == 'sh':
            return ipaddress.IPv6Network('fd00:0:1:0::/64')
        return ipaddress.IPv6Network('fd00:0:1:%d::/64' % peer.id)

    def core_address(self, local: Site, remote: Site) -> str:
        net = self.core_network(local, remote)
        offset = 1 if local.abbr == 'bj' else 2
        return str(net.network_address + offset)

    def core_neighbor(self, local: Site, remote: Site) -> str:
        return self.core_address(remote, local)

    def underlay_network(self, site: Site) -> ipaddress.IPv6Network:
        return ipaddress.IPv6Network('fd00:0:%d::/48' % site.id)

    def central_address(self, network: Network, site: Site) -> str:
        net = self.interconnect_network(network, site)
        return str(net.network_address + 1)

    def user_address(self, network: Network, site: Site) -> str:
        net = self.interconnect_network(network, site)
        return str(net.network_address + 2)

    def cloud_address(self, network: Network, site: Site,
                      offset: int = None) -> str:
        if network.vpn == 'l2':
            net = self.l2_network(network)
            if offset is None:
                # Keep the original bj ::10 / sh ::11 addresses, and give
                # additional flat-/64 sites unique offsets (gz ::12, cd ::13).
                offset = 0x10 + max(0, site.id - 1)
        else:
            net = self.business_network(network, site)
            if offset is None:
                offset = 0x10
        return str(net.network_address + int(offset))

    def cloud_gateway(self, network: Network, site: Site) -> str:
        return self.cloud_address(network, site, 1)

    def network_map(self) -> Dict[int, Network]:
        return {item.id: item for item in self.networks}

    def site_map(self) -> Dict[str, Site]:
        return {item.abbr: item for item in self.sites}


def _load_sites(raw) -> List[Site]:
    if not isinstance(raw, list) or not raw:
        raise ValueError('parallel.json: sites 必须是非空列表')
    sites = []
    seen_ids = set()
    seen_abbrs = set()
    for entry in raw:
        site_id = int(entry['id'])
        abbr = str(entry['abbr']).strip()
        name = str(entry.get('name', abbr)).strip()
        if site_id in seen_ids:
            raise ValueError('站点 id 重复: %s' % site_id)
        if abbr in seen_abbrs:
            raise ValueError('站点 abbr 重复: %s' % abbr)
        seen_ids.add(site_id)
        seen_abbrs.add(abbr)
        sites.append(Site(id=site_id, abbr=abbr, name=name))
    return sites


def _load_networks(raw, site_abbrs) -> List[Network]:
    if not isinstance(raw, list) or not raw:
        raise ValueError('parallel.json: networks 必须是非空列表')
    networks = []
    seen = set()
    known_sites = set(site_abbrs)
    for entry in raw:
        network_id = int(entry['id'])
        if not 1 <= network_id <= 400:
            raise ValueError('并行网络 ID 必须在 1-400: %s' % network_id)
        if network_id in seen:
            raise ValueError('并行网络 ID 重复: %s' % network_id)
        seen.add(network_id)
        vpn = str(entry.get('vpn', 'l3')).lower()
        if vpn not in ('l3', 'l2'):
            raise ValueError('网络 %s 的 vpn 必须是 l3/l2' % network_id)
        expected_vlan = network_id + 3500
        vlan = int(entry.get('vlan', expected_vlan))
        if vlan != expected_vlan:
            raise ValueError(
                '网络 %s 的 VLAN 必须为 %d（ID + 3500），当前为 %d'
                % (network_id, expected_vlan, vlan))
        site_list = entry.get('sites')
        if site_list is None:
            members = tuple(site_abbrs)
        else:
            if not isinstance(site_list, list) or not site_list:
                raise ValueError('网络 %s 的 sites 必须是非空列表'
                                 % network_id)
            members = tuple(str(item) for item in site_list)
            if len(set(members)) != len(members):
                raise ValueError('网络 %s 的 sites 有重复项' % network_id)
            unknown = [item for item in members if item not in known_sites]
            if unknown:
                raise ValueError('网络 %s 引用了未知站点: %s'
                                 % (network_id, ', '.join(unknown)))
        networks.append(Network(id=network_id, vpn=vpn, vlan=vlan,
                                sites=members))
    return networks


def load_config(path: Optional[str] = None) -> ParallelConfig:
    path = os.path.abspath(path or DEFAULT_CONFIG)
    with open(path, encoding='utf-8') as handle:
        raw = json.load(handle)
    sites = _load_sites(raw.get('sites'))
    networks = _load_networks(raw.get('networks'),
                              [item.abbr for item in sites])
    p4 = raw.get('p4', {}) or {}
    p4_json = str(p4.get('json', os.path.relpath(DEFAULT_P4_JSON,
                                                  PROJECT_ROOT)))
    if not os.path.isabs(p4_json):
        p4_json = os.path.join(PROJECT_ROOT, p4_json)
    l2_tunnel = str(raw.get('l2_tunnel', 'srv6')).lower()
    if l2_tunnel not in ('srv6', 'vxlan'):
        raise ValueError('l2_tunnel 必须是 srv6/vxlan: %s' % l2_tunnel)
    return ParallelConfig(
        path=path,
        sites=sites,
        networks=networks,
        p4_json=os.path.abspath(p4_json),
        thrift_base=int(p4.get('thrift_base', 11000)),
        p4_log_level=str(p4.get('log_level', 'info')),
        l2_tunnel=l2_tunnel,
        underlay=dict(raw.get('underlay', {}) or {}),
    )


def dump_config(cfg: ParallelConfig) -> None:
    print('配置: %s' % cfg.path)
    print('站点: %s' % ', '.join(
        '%s(%s)' % (item.name, item.abbr) for item in cfg.sites))
    print('L2 隧道: %s' % cfg.l2_tunnel)
    print('并行网络:')
    for network in cfg.networks:
        print('  ID=%d VLAN=%d VPN=%s'
              % (network.id, network.vlan, network.vpn))
        print('    站点: %s' % ', '.join(network.sites))
        for site in cfg.network_sites(network):
            if network.vpn == 'l2':
                subnet = cfg.l2_network(network)
            else:
                subnet = cfg.business_network(network, site)
            print('    %s: 业务 %s  互联 %s  云地址 %s'
                  % (site.abbr,
                     subnet,
                     cfg.interconnect_network(network, site),
                     cfg.cloud_address(network, site)))
