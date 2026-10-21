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


def cloud_name(site, network) -> str:
    return 'cloud_%s_%d' % (site.abbr, network.id)


SWITCH_IDS = {
    'E_bj': 1,
    'S1_bj': 2,
    'S2_bj': 3,
    'E_sh': 4,
    'S1_sh': 5,
    'S2_sh': 6,
}

PUBLIC_BORDER = 'pub_border'
PUBLIC_FIX = 'fix'


class ParallelTopology(Topo):
    r"""PDF 中 C-E/A-S 站点结构的北京/上海仿真拓扑。

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
        sh = sites['sh']

        # Routers / public border / servers are Linux netns.
        for site in cfg.sites:
            self.addHost(c_name(site))
            self.addHost(a_name(site))
        self.addHost(PUBLIC_BORDER)
        self.addHost(PUBLIC_FIX)

        # BMv2 P4 devices.
        for site in cfg.sites:
            for name in (e_name(site), s1_name(site), s2_name(site)):
                # Mininet requires a canonical name (sN) or an explicit
                # datapath id for non-canonical switch names.
                self.addSwitch(name, dpid='%016x' % SWITCH_IDS[name])

        # Core and site uplinks. IPv6 addresses are configured with
        # ``ip -6 addr replace`` after net.start; Mininet's ifconfig-based
        # link parameters only handle IPv4 reliably.
        self.addLink(c_name(bj), c_name(sh))
        for site in cfg.sites:
            self.addLink(c_name(site), e_name(site))
            self.addLink(c_name(site), a_name(site))
            self.addLink(e_name(site), s1_name(site))
            self.addLink(a_name(site), s2_name(site))

        # Public access at Beijing C2.
        self.addLink(c_name(bj), PUBLIC_BORDER)
        self.addLink(PUBLIC_BORDER, PUBLIC_FIX)

        # Each cloud server has two NICs: eth0 -> S1, eth1 -> S2.
        for site in cfg.sites:
            for network in cfg.networks:
                host = cloud_name(site, network)
                self.addHost(host)
                self.addLink(host, s1_name(site))
                self.addLink(host, s2_name(site))
