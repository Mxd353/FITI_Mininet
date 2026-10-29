#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EVPN route model for the parallel-network control plane.

This is the first control-plane stage: EVPN route objects, RD/RT, VNI and
SRv6 SID allocation, route add/withdraw events, and JSON import/export.
It is not a BGP finite-state machine.  A later FRR/ExaBGP adapter can feed
the same RouteTable and translate updates into P4Runtime/Thrift changes.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple, Union

from parallel.config import DEFAULT_CONFIG, ParallelConfig, load_config

ROUTE_IP_PREFIX = 'ip-prefix'
ROUTE_MAC_IP = 'mac-ip'
ROUTE_IMET = 'imet'


@dataclass(frozen=True)
class EvpnIpPrefixRoute:
    rd: str
    rt: Tuple[str, ...]
    prefix: str
    vni: int
    sid: str
    origin: str
    next_hop: str
    route_type: str = field(default=ROUTE_IP_PREFIX, init=False)

    def key(self):
        return (self.route_type, self.rd, self.prefix, self.vni)

    def to_dict(self):
        return {
            'type': self.route_type,
            'rd': self.rd,
            'rt': list(self.rt),
            'prefix': self.prefix,
            'vni': self.vni,
            'sid': self.sid,
            'origin': self.origin,
            'next_hop': self.next_hop,
        }


@dataclass(frozen=True)
class EvpnMacIpRoute:
    rd: str
    rt: Tuple[str, ...]
    mac: str
    ip: str
    vni: int
    sid: str
    origin: str
    next_hop: str
    host: int = 1
    route_type: str = field(default=ROUTE_MAC_IP, init=False)

    def key(self):
        return (self.route_type, self.rd, self.mac, self.ip,
                self.vni, self.origin)

    def to_dict(self):
        return {
            'type': self.route_type,
            'rd': self.rd,
            'rt': list(self.rt),
            'mac': self.mac,
            'ip': self.ip,
            'vni': self.vni,
            'sid': self.sid,
            'origin': self.origin,
            'next_hop': self.next_hop,
            'host': self.host,
        }


@dataclass(frozen=True)
class EvpnInclusiveMulticastRoute:
    rd: str
    rt: Tuple[str, ...]
    vni: int
    sid: str
    origin: str
    route_type: str = field(default=ROUTE_IMET, init=False)

    def key(self):
        return (self.route_type, self.rd, self.vni, self.origin)

    def to_dict(self):
        return {
            'type': self.route_type,
            'rd': self.rd,
            'rt': list(self.rt),
            'vni': self.vni,
            'sid': self.sid,
            'origin': self.origin,
        }


EvpnRoute = Union[EvpnIpPrefixRoute, EvpnMacIpRoute,
                  EvpnInclusiveMulticastRoute]


@dataclass
class RouteUpdate:
    action: str
    route: EvpnRoute


class RouteTable:
    """In-memory EVPN route table with add/withdraw event generation."""

    def __init__(self):
        self.routes: Dict[tuple, EvpnRoute] = {}

    def advertise(self, route: EvpnRoute) -> Optional[RouteUpdate]:
        key = route.key()
        old = self.routes.get(key)
        if old == route:
            return None
        self.routes[key] = route
        return RouteUpdate('replace' if old is not None else 'add', route)

    def withdraw(self, route: EvpnRoute) -> Optional[RouteUpdate]:
        key = route.key()
        if key not in self.routes:
            return None
        del self.routes[key]
        return RouteUpdate('remove', route)

    def apply(self, updates: Iterable[Optional[RouteUpdate]], callback):
        for update in updates:
            if update is not None:
                callback(update)

    def values(self) -> List[EvpnRoute]:
        return list(self.routes.values())

    def l3_routes(self) -> List[EvpnIpPrefixRoute]:
        return [route for route in self.values()
                if isinstance(route, EvpnIpPrefixRoute)]

    def l3_route(self, vni: int, origin: str):
        for route in self.l3_routes():
            if route.vni == int(vni) and route.origin == origin:
                return route
        return None

    def imet_routes(self, vni: int = None) -> List[EvpnInclusiveMulticastRoute]:
        return [route for route in self.values()
                if isinstance(route, EvpnInclusiveMulticastRoute)
                and (vni is None or route.vni == int(vni))]

    def imet_route(self, vni: int, origin: str):
        for route in self.imet_routes(vni):
            if route.origin == origin:
                return route
        return None

    def remote_imet_routes(self, vni: int, origin: str):
        return [route for route in self.imet_routes(vni)
                if route.origin != origin]

    def mac_ip_routes(self, vni: int = None) -> List[EvpnMacIpRoute]:
        return [route for route in self.values()
                if isinstance(route, EvpnMacIpRoute)
                and (vni is None or route.vni == int(vni))]

    def to_dict(self):
        return {'routes': [route.to_dict()
                            for route in sorted(self.values(), key=lambda r: r.key())]}

    @classmethod
    def from_dict(cls, payload):
        table = cls()
        for raw in payload.get('routes', []):
            kind = raw.get('type')
            rt = tuple(raw.get('rt', []))
            if kind == ROUTE_IP_PREFIX:
                route = EvpnIpPrefixRoute(
                    rd=raw['rd'], rt=rt, prefix=raw['prefix'],
                    vni=int(raw['vni']), sid=raw['sid'],
                    origin=raw['origin'], next_hop=raw['next_hop'])
            elif kind == ROUTE_MAC_IP:
                route = EvpnMacIpRoute(
                    rd=raw['rd'], rt=rt, mac=raw['mac'], ip=raw['ip'],
                    vni=int(raw['vni']), sid=raw['sid'],
                    origin=raw['origin'], next_hop=raw['next_hop'],
                    host=int(raw.get('host', 1)))
            elif kind == ROUTE_IMET:
                route = EvpnInclusiveMulticastRoute(
                    rd=raw['rd'], rt=rt, vni=int(raw['vni']),
                    sid=raw['sid'], origin=raw['origin'])
            else:
                raise ValueError('未知 EVPN 路由类型: %s' % kind)
            table.advertise(route)
        return table


def build_evpn_routes(cfg: ParallelConfig, net=None,
                      include_l2_mac=True) -> RouteTable:
    """Build local EVPN routes for all configured sites.

    L3VPN produces IP Prefix routes.  L2VPN produces Inclusive Multicast
    routes and, when ``net`` is provided, MAC/IP routes from the cloud
    server MAC addresses.
    """
    table = RouteTable()
    for network in cfg.l3_networks():
        rd = '9425:%d' % network.id
        rt = (rd,)
        for site in cfg.network_sites(network):
            sid = cfg.srv6_sid(network, site)
            table.advertise(EvpnIpPrefixRoute(
                rd=rd, rt=rt,
                prefix=str(cfg.business_network(network, site)),
                vni=network.id, sid=sid, origin=site.abbr,
                next_hop=sid))

    for network in cfg.l2_networks():
        rd = '9425:%d' % network.id
        rt = (rd,)
        for site in cfg.network_sites(network):
            sid = cfg.l2_sid(network, site)
            table.advertise(EvpnInclusiveMulticastRoute(
                rd=rd, rt=rt, vni=network.id, sid=sid,
                origin=site.abbr))
            if net is not None and include_l2_mac:
                for host_id in cfg.host_ids():
                    table.advertise(EvpnMacIpRoute(
                        rd=rd, rt=rt,
                        mac=cfg.cloud_mac(network, site, host_id),
                        ip=cfg.cloud_address(network, site, host_id),
                        vni=network.id, sid=sid, origin=site.abbr,
                        next_hop=sid, host=host_id))
    return table


def validate_evpn_routes(table: RouteTable) -> List[str]:
    errors = []
    # IMET and MAC/IP routes for the same VNI/site intentionally share the
    # PE's L2 SID.  A SID must not be reused by a different VNI/site pair.
    sids = {}
    for route in table.values():
        sid = getattr(route, 'sid', None)
        if sid is not None:
            owner = (route.vni, route.origin)
            if sid in sids and sids[sid] != owner:
                errors.append('SID 跨 VNI/站点复用: %s (%s / %s)'
                              % (sid, sids[sid], owner))
            sids[sid] = owner
        if not getattr(route, 'rd', None):
            errors.append('路由缺少 RD: %s' % (route.key(),))
        if not getattr(route, 'rt', None):
            errors.append('路由缺少 RT: %s' % (route.key(),))
    return errors


def dump_routes(table: RouteTable) -> None:
    print('EVPN 路由数: %d' % len(table.routes))
    for route in sorted(table.values(), key=lambda item: item.key()):
        if isinstance(route, EvpnIpPrefixRoute):
            print('  ip-prefix  rd=%s rt=%s vni=%d prefix=%s sid=%s origin=%s'
                  % (route.rd, ','.join(route.rt), route.vni,
                     route.prefix, route.sid, route.origin))
        elif isinstance(route, EvpnMacIpRoute):
            print('  mac-ip     rd=%s rt=%s vni=%d mac=%s ip=%s sid=%s'
                  % (route.rd, ','.join(route.rt), route.vni,
                     route.mac, route.ip, route.sid))
        else:
            print('  imet       rd=%s rt=%s vni=%d sid=%s origin=%s'
                  % (route.rd, ','.join(route.rt), route.vni,
                     route.sid, route.origin))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='并行网络 EVPN 路由模型（非 BGP 协议实现）')
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    parser.add_argument('--dump', action='store_true',
                        help='打印本地 EVPN 路由')
    parser.add_argument('--validate', action='store_true',
                        help='校验 SID/RD/RT 冲突')
    parser.add_argument('--json', default=None,
                        help='把路由模型写入 JSON 文件')
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    table = build_evpn_routes(cfg)
    errors = validate_evpn_routes(table)
    if args.validate and errors:
        for item in errors:
            print('ERROR: %s' % item)
        return 1
    if args.json:
        with open(os.path.abspath(args.json), 'w', encoding='utf-8') as handle:
            json.dump(table.to_dict(), handle, ensure_ascii=False, indent=2)
        print('EVPN 路由已写入: %s' % os.path.abspath(args.json))
    if args.dump or not args.json:
        dump_routes(table)
    if args.validate:
        print('校验通过: %d 条路由' % len(table.routes))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
