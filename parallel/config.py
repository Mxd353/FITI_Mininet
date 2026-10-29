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
from parallel.fiti_topology import load_fiti_topology

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
    fiti_topology: str
    fiti: object
    l2_tunnel: str = 'srv6'
    underlay: Dict[str, str] = field(default_factory=dict)
    hosts_per_site: int = 1
    endpoints_per_host: int = 1

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

    def endpoint_ifname(self, site: Site, host: int, endpoint: int,
                        network: Network) -> str:
        return 'ep_%s_%d_%d_%d' % (site.abbr, int(host), int(endpoint),
                                   network.id)

    def endpoint_table(self, network: Network, host: int,
                       endpoint: int) -> int:
        base = 20000 if network.vpn == 'l3' else 30000
        index = ((int(host) - 1) * int(self.endpoints_per_host)
                 + (int(endpoint) - 1))
        return base + int(network.id) * 16 + index

    def endpoint_rule_priority(self, network: Network, host: int,
                               endpoint: int) -> int:
        index = ((int(host) - 1) * int(self.endpoints_per_host)
                 + (int(endpoint) - 1))
        return 10000 + int(network.id) * 16 + index

    def network_table(self, network: Network) -> int:
        base = 20000 if network.vpn == 'l3' else 30000
        return base + int(network.id) * 16

    def host_ids(self):
        """Host indexes inside one site (1..hosts_per_site)."""
        return range(1, int(self.hosts_per_site) + 1)

    def endpoint_ids(self):
        """Endpoint indexes inside one host (1..endpoints_per_host)."""
        return range(1, int(self.endpoints_per_host) + 1)

    def cloud_mac(self, network: Network, site: Site,
                  host: int = 1, endpoint: int = 1) -> str:
        """Deterministic MAC for one endpoint.

        Six bytes: locally-administered prefix, 8-bit site, 16-bit network,
        8-bit host, 8-bit endpoint.
        """
        site_id = int(site.id)
        network_id = int(network.id)
        host_id = int(host)
        endpoint_id = int(endpoint)
        for name, value in (('site', site_id), ('host', host_id),
                            ('endpoint', endpoint_id)):
            if not 1 <= value <= 255:
                raise ValueError('%s 编号必须在 1-255: %s' % (name, value))
        return '02:%02x:%02x:%02x:%02x:%02x' % (
            site_id & 0xff, (network_id >> 8) & 0xff,
            network_id & 0xff, host_id, endpoint_id)

    def srv6_sid(self, network: Network, site: Site) -> str:
        """Per-site, per-network SRv6 SID used by the static L3VPN PE.

        The SID lives in the site locator ``fd00:0:{site}:1::/64`` and uses
        two 16-bit words: ``::1:{network}`` for L3VPN.  The type word keeps
        L3 and L2 SIDs apart for the full 1..400 range (and beyond).
        """
        return str(ipaddress.IPv6Address(
            'fd00:0:%d:1::1:%x' % (site.id, network.id)))

    def l2_sid(self, network: Network, site: Site) -> str:
        """Per-site, per-network SRv6 SID used by L2VPN (``::2:{network}``)."""
        return str(ipaddress.IPv6Address(
            'fd00:0:%d:1::2:%x' % (site.id, network.id)))

    def interconnect_network(self, network: Network,
                             site: Site) -> ipaddress.IPv6Network:
        return ipaddress.IPv6Network(
            '240a:a806:%d:%d::/64' % (network.id, 100 + site.id))

    def core_network(self, left: Site,
                     right: Site) -> ipaddress.IPv6Network:
        return self.fiti.link_network(left.abbr, right.abbr)

    def core_address(self, local: Site, remote: Site) -> str:
        return self.fiti.core_address(local.abbr, remote.abbr)

    def core_neighbor(self, local: Site, remote: Site) -> str:
        return self.fiti.core_neighbor(local.abbr, remote.abbr)

    def core_next_hop(self, local: Site, remote: Site) -> str:
        return self.fiti.next_hop(local.abbr, remote.abbr,
                                  allowed={site.abbr for site in self.sites})

    def underlay_network(self, site: Site) -> ipaddress.IPv6Network:
        return ipaddress.IPv6Network('fd00:0:%d::/48' % site.id)

    def central_address(self, network: Network, site: Site) -> str:
        net = self.interconnect_network(network, site)
        return str(net.network_address + 1)

    def user_address(self, network: Network, site: Site) -> str:
        net = self.interconnect_network(network, site)
        return str(net.network_address + 2)

    def cloud_address(self, network: Network, site: Site,
                      host: int = 1, endpoint: int = 1,
                      offset: int = None) -> str:
        """Address of one endpoint inside its slice.

        L3 endpoints stay in the site /64: host*16 + endpoint gives
        ::10, ::11, ::20, ::21.  L2 slices are a flat /64, so every
        (site, host, endpoint) gets a unique high offset.
        """
        host_id = int(host)
        endpoint_id = int(endpoint)
        if network.vpn == 'l2':
            net = self.l2_network(network)
            if offset is None:
                index = ((int(site.id) - 1) * int(self.hosts_per_site)
                         + (host_id - 1)) * int(self.endpoints_per_host) \
                    + (endpoint_id - 1)
                offset = 0x100 + index
        else:
            net = self.business_network(network, site)
            if offset is None:
                offset = 0x10 + (host_id - 1) * 0x10 + (endpoint_id - 1)
        return str(net.network_address + int(offset))

    def cloud_gateway(self, network: Network, site: Site,
                      host: int = 1, endpoint: int = 1) -> str:
        # All endpoints of a site share the same gateway address; the static
        # neighbor entry points at the site PE.
        return self.cloud_address(network, site, 1, 1, offset=1)

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
    hosts_per_site = int(raw.get('hosts_per_site', 1))
    if not 1 <= hosts_per_site <= 255:
        raise ValueError('hosts_per_site 必须在 1-255: %s' % hosts_per_site)
    endpoints_per_host = int(raw.get('endpoints_per_host', 1))
    if not 1 <= endpoints_per_host <= 255:
        raise ValueError('endpoints_per_host 必须在 1-255: %s'
                         % endpoints_per_host)
    fiti_topology = str(raw.get('fiti_topology', 'fiti.json'))
    if not os.path.isabs(fiti_topology):
        fiti_topology = os.path.join(os.path.dirname(path), fiti_topology)
    fiti_topology = os.path.abspath(fiti_topology)
    if not os.path.isfile(fiti_topology):
        raise ValueError('找不到 FITI 拓扑文件: %s' % fiti_topology)
    fiti = load_fiti_topology(fiti_topology)
    missing = [site.abbr for site in sites
               if site.abbr not in fiti.nodes]
    if missing:
        raise ValueError('parallel.json 站点不在 FITI 拓扑中: %s'
                         % ', '.join(missing))
    return ParallelConfig(
        path=path,
        sites=sites,
        networks=networks,
        p4_json=os.path.abspath(p4_json),
        thrift_base=int(p4.get('thrift_base', 11000)),
        p4_log_level=str(p4.get('log_level', 'info')),
        fiti_topology=fiti_topology,
        fiti=fiti,
        l2_tunnel=l2_tunnel,
        underlay=dict(raw.get('underlay', {}) or {}),
        hosts_per_site=hosts_per_site,
        endpoints_per_host=endpoints_per_host,
    )


def dump_config(cfg: ParallelConfig) -> None:
    print('配置: %s' % cfg.path)
    print('站点: %s' % ', '.join(
        '%s(%s)' % (item.name, item.abbr) for item in cfg.sites))
    print('FITI 核心: %s（%d 条链路）'
          % (cfg.fiti_topology, len(cfg.fiti.links)))
    print('L2 隧道: %s' % cfg.l2_tunnel)
    print('每站点主机: %d，每主机端点: %d'
          % (cfg.hosts_per_site, cfg.endpoints_per_host))
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
            print('    %s: 业务 %s  互联 %s'
                  % (site.abbr, subnet,
                     cfg.interconnect_network(network, site)))
            for host_id in cfg.host_ids():
                for endpoint in cfg.endpoint_ids():
                    print('      主机 %d 端点 %d: %s' % (
                        host_id, endpoint,
                        cfg.cloud_address(network, site, host_id,
                                          endpoint)))
