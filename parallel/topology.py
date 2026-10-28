#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并行网络 Mininet 逻辑拓扑。"""

from __future__ import annotations

from mininet.node import OVSBridge
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


def switch_id(site, role='E') -> int:
    """Stable dpid/Thrift offset for the site PE switch.

    Only the PE (``E``) runs BMv2/P4; the access switches ``S1``/``S2`` are
    OVS bridges and therefore do not consume a Thrift port.
    """
    if role != 'E':
        raise ValueError('%s 不是 BMv2 交换机，只有 E 使用 P4' % role)
    return int(site.id)


def switch_ids(cfg) -> dict:
    return {e_name(site): switch_id(site, 'E') for site in cfg.sites}


OVS_DPID_BASE = 1000


def ovs_dpid(site, role) -> int:
    """Unique OVS datapath id for an access switch.

    ``Switch.defaultDpid`` only looks at the first number in the name, so
    ``S1_bj`` and ``S1_sh`` would both become dpid 1.  Derive an explicit id
    in a range that cannot collide with the BMv2 (E) dpids.
    """
    if role not in ('S1', 'S2'):
        raise ValueError('%s 不是 OVS 接入交换机' % role)
    return OVS_DPID_BASE + (int(site.id) - 1) * 2 + (0 if role == 'S1' else 1)

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

    设备实现：

    * ``E``    BMv2 + P4（VRF/VSI、SRv6、BUM 复制）
    * ``S1``   OVS 二层交换机（上行放行并行 VLAN）
    * ``S2``   OVS 二层交换机（上行拒绝并行 VLAN）
    * ``C``/``A``/``cloud``  Linux netns
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

        # BMv2/P4 devices: the site PE (E) is the only programmable switch.
        for site in cfg.sites:
            # Mininet requires a canonical name (sN) or an explicit
            # datapath id for non-canonical switch names.
            self.addSwitch(e_name(site),
                           dpid='%016x' % switch_id(site, 'E'))

        # Access switches S1/S2 are ordinary 802.1Q bridges.  They run as
        # Mininet OVS bridges instead of BMv2 to save one switch process per
        # port; VLAN forwarding/filtering is configured in parallel/ovs.py.
        # ``listenPort=0`` avoids one unused OpenFlow listener per bridge
        # (there is no controller in this scenario).
        for site in cfg.sites:
            for role, name in (('S1', s1_name(site)), ('S2', s2_name(site))):
                self.addSwitch(name, cls=OVSBridge, listenPort=0,
                               dpid='%016x' % ovs_dpid(site, role))

        # Site-to-site core links follow the actual FITI topology
        # (configs/fiti.json).  Each site's C router is one FITI node.
        site_abbrs = {site.abbr for site in cfg.sites}
        for link in cfg.fiti.links:
            if link.src_abbr not in site_abbrs or link.dst_abbr not in site_abbrs:
                continue
            src = cfg.site(link.src_abbr)
            dst = cfg.site(link.dst_abbr)
            self.addLink(c_name(src), c_name(dst))
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
