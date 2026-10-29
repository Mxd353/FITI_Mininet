#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OVS configuration for the S1/S2 access switches.

The PDF describes S1/S2 as ordinary site switches: S1 allows the parallel
VLANs on its uplink, S2 rejects them.  Both therefore run as Mininet OVS
bridges instead of BMv2/P4:

* S1: trunk on both ports, parallel VLANs pass towards E;
* S2: trunk towards the cloud, parallel VLANs are removed from the A uplink.

VLAN forwarding, MAC learning and flooding are handled by OVS; no static
L2 FIB and no multicast group are installed on the access switches.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor

from parallel.topology import a_name, cloud_name, e_name, s1_name, s2_name

#: How many sites to configure concurrently.  S1/S2 of different sites are
#: independent OVS bridges, so per-site parallelism is safe.
MAX_WORKERS = max(1, int(os.environ.get('PARALLEL_OVS_WORKERS', '16')))

#: All usable 802.1Q VLANs on a trunk.
ALL_VLANS = '1-4094'

#: PDF: the S2 uplink rejects parallel-network VLAN 3501-3900.
NON_PARALLEL_VLANS = '1-3500,3901-4094'

PARALLEL_VLAN_MIN = 3501
PARALLEL_VLAN_MAX = 3900




def _peer_intf(intf):
    link = intf.link
    if link is None:
        return None
    return link.intf2 if link.intf1 is intf else link.intf1


def intf_to(node, peer_name):
    for intf in node.intfs.values():
        peer = _peer_intf(intf)
        if peer is not None and peer.node.name == peer_name:
            return intf
    raise RuntimeError('%s 没有连接到 %s 的接口' % (node.name, peer_name))


def _vsctl(switch, *args):
    command = 'ovs-vsctl ' + ' '.join(str(item) for item in args)
    output = (switch.cmd(command) or '').strip()
    if output and ('ovs-vsctl' in output or 'Error' in output
                   or 'error' in output):
        raise RuntimeError('%s: %s 失败: %s'
                           % (switch.name, command, output))
    return output


def set_trunk(switch, intf, trunks):
    """Make ``intf`` an 802.1Q trunk restricted to ``trunks``."""
    _vsctl(switch, 'set', 'port', intf.name,
           'vlan_mode=trunk', 'trunks=%s' % trunks)


def ensure_normal_flow(switch):
    """Fall through to OVS's NORMAL action (MAC learning + VLAN flood).

    ``OVSBridge`` runs with ``fail_mode=standalone``, which already acts as a
    learning bridge without a controller.  Installing the flow explicitly
    also covers OVS builds that leave the table empty when no controller is
    configured.
    """
    output = (switch.cmd('ovs-ofctl add-flow %s priority=0,actions=NORMAL'
                         % switch.name) or '').strip()
    if output and 'Error' in output:
        raise RuntimeError('%s: 安装 NORMAL 流表失败: %s'
                           % (switch.name, output))


def trunk_vlans(switch, intf):
    """Read back the VLANs allowed on a trunk.

    ``ovs-vsctl`` may print an expanded set or the original ranges, so both
    forms are accepted here.
    """
    output = _vsctl(switch, '--if-exists', 'get', 'port', intf.name, 'trunks')
    vlans = set()
    for token in re.findall(r'\d+(?:\s*-\s*\d+)?', output):
        if '-' in token:
            low, high = (int(part) for part in token.split('-'))
            vlans.update(range(low, high + 1))
        else:
            vlans.add(int(token))
    return vlans


def configure_ovs_switches(net, cfg):
    """Configure S1/S2 as VLAN-filtering OVS bridges."""
    sites = list(cfg.sites)
    workers = min(MAX_WORKERS, len(sites)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_configure_site, net, cfg, site) for site in sites]
        for future in futures:
            future.result()


def _configure_site(net, cfg, site):
    """Configure one site's S1/S2 bridges."""
    s1 = net.get(s1_name(site))
    s2 = net.get(s2_name(site))
    uplink_s2 = intf_to(s2, a_name(site))

    ensure_normal_flow(s1)
    ensure_normal_flow(s2)

    set_trunk(s1, intf_to(s1, e_name(site)), ALL_VLANS)
    set_trunk(s2, uplink_s2, NON_PARALLEL_VLANS)
    for host_id in cfg.host_ids():
        cloud = cloud_name(site, host_id)
        set_trunk(s1, intf_to(s1, cloud), ALL_VLANS)
        set_trunk(s2, intf_to(s2, cloud), ALL_VLANS)

    # S2's uplink is the only restrictive policy in the design; make a
    # silent ovs-vsctl typo visible at startup instead of at ping time.
    vlans = trunk_vlans(s2, uplink_s2)
    if not vlans:
        raise RuntimeError('%s 上行没有配置 OVS trunk' % s2.name)
    if (PARALLEL_VLAN_MIN in vlans) or (PARALLEL_VLAN_MAX in vlans):
        raise RuntimeError('%s 上行仍然放行并行 VLAN %d-%d'
                           % (s2.name, PARALLEL_VLAN_MIN,
                              PARALLEL_VLAN_MAX))
