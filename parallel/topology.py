#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并行网络 Mininet 逻辑拓扑。"""

from __future__ import annotations

from mininet.topo import Topo

from parallel.config import ParallelConfig


def c_name(site) -> str:
    return 'C_%s' % site.abbr


def a_name(site) -> str:
    return 'A_%s' % site.abbr


def e_name(site) -> str:
    return 'E_%s' % site.abbr


def s1_name(site) -> str:
    return 'S1_%s' % site.abbr


def s2_name(site) -> str:
    return 'S2_%s' % site.abbr


def cloud_name(site, network=None) -> str:
    """One cloud host per site; all slices use VLAN subinterfaces on it."""
    return 'cl_%s' % site.abbr


ROLE_OFFSET = {'E': 0, 'S1': 1, 'S2': 2}


def switch_id(site, role) -> int:
    """Stable dpid/Thrift offset for a site switch."""
    return (int(site.id) - 1) * 3 + ROLE_OFFSET[role] + 1


def switch_ids(cfg) -> dict:
    result = {}
    for site in cfg.sites:
        result[e_name(site)] = switch_id(site, 'E')
        result[s1_name(site)] = switch_id(site, 'S1')
        result[s2_name(site)] = switch_id(site, 'S2')
    return result

PUBLIC_BORDER = 'pub_border'
PUBLIC_FIX = 'fix'


class ParallelTopology(Topo):
    r"""PDF 中 C-E/A-S 站点结构的可扩展仿真拓扑。

    站点内部：

        C
       / \
      E   A
      |   |
     S1  S2
      \  /
     cloud

    北京 C 额外连接 pub_border / FIX。
    """

    def __init__(self, cfg: ParallelConfig, **kwargs):
        self.cfg = cfg
        super().__init__(**kwargs)

    def build(self):
        cfg = self.cfg
        sites = {site.abbr: site for site in cfg.sites}
        bj = sites['bj']

        # Routers / public border / servers are Linux netns.
        for site in cfg.sites:
            self.addHost(c_name(site))
            self.addHost(a_name(site))
        self.addHost(PUBLIC_BORDER)
        self.addHost(PUBLIC_FIX)

        # BMv2 P4 devices.
        for site in cfg.sites:
            for role, name in (('E', e_name(site)),
                               ('S1', s1_name(site)),
                               ('S2', s2_name(site))):
                # Mininet requires a canonical name (sN) or an explicit
                # datapath id for non-canonical switch names.
                self.addSwitch(name, dpid='%016x'
                               % switch_id(site, role))

        # Core and site uplinks.  The core is a Beijing-centred star:
        # C_bj -- every other site C router.  IPv6 addresses are
        # configured with ``ip -6 addr replace`` after net.start; Mininet's
        # ifconfig-based link parameters only handle IPv4 reliably.
        for site in cfg.sites:
            if site.abbr != bj.abbr:
                self.addLink(c_name(bj), c_name(site))
        for site in cfg.sites:
            self.addLink(c_name(site), e_name(site))
            self.addLink(c_name(site), a_name(site))
            self.addLink(e_name(site), s1_name(site))
            self.addLink(a_name(site), s2_name(site))

        # Public access at Beijing C2.
        self.addLink(c_name(bj), PUBLIC_BORDER)
        self.addLink(PUBLIC_BORDER, PUBLIC_FIX)

        # One cloud host per site.  It uses 802.1Q VLAN subinterfaces on
        # the S1/S2 trunk to distinguish all parallel-network slices.
        for site in cfg.sites:
            host = cloud_name(site)
            self.addHost(host)
            self.addLink(host, s1_name(site))
            self.addLink(host, s2_name(site))
