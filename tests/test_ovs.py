#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the OVS access-switch configuration (no root required)."""

import re
import unittest

from parallel.config import load_config
from parallel.ovs import (ALL_VLANS, NON_PARALLEL_VLANS, PARALLEL_VLAN_MIN,
                          configure_ovs_switches, set_trunk, trunk_vlans)
from parallel.topology import (ParallelTopology, a_name, cloud_name, e_name,
                               s1_name, s2_name)
from mininet.node import OVSBridge


class _Intf(object):
    def __init__(self, node, name):
        self.node = node
        self.name = name
        self.link = None


class _Link(object):
    def __init__(self, left, right):
        self.intf1, self.intf2 = left, right
        left.link = right.link = self


class _Peer(object):
    def __init__(self, name):
        self.name = name

    def cmd(self, _command):
        return ''


class _Ovs(object):
    def __init__(self, name):
        self.name = name
        self.intfs = {}
        self.trunks = {}

    def connect(self, peer_name):
        intf = _Intf(self, '%s-%s' % (self.name, peer_name))
        self.intfs[len(self.intfs) + 1] = intf
        _Link(intf, _Intf(_Peer(peer_name), 'peer'))
        return intf

    def intf_to(self, peer_name):
        for intf in self.intfs.values():
            peer = (intf.link.intf2 if intf.link.intf1 is intf
                    else intf.link.intf1)
            if peer.node.name == peer_name:
                return intf
        raise AssertionError('%s 没有连接到 %s' % (self.name, peer_name))

    def cmd(self, command):
        match = re.match(r'ovs-vsctl set port (\S+) .*trunks=(\S+)$', command)
        if match:
            self.trunks[match.group(1)] = match.group(2)
            return ''
        match = re.match(r'ovs-vsctl --if-exists get port (\S+) trunks$',
                         command)
        if match:
            return self.trunks.get(match.group(1), '')
        return ''


class _Net(object):
    def __init__(self):
        self.nodes = {}

    def add(self, node):
        self.nodes[node.name] = node
        return node

    def get(self, name):
        return self.nodes.get(name)


def _build_net(cfg):
    net = _Net()
    for site in cfg.sites:
        s1 = net.add(_Ovs(s1_name(site)))
        s2 = net.add(_Ovs(s2_name(site)))
        s1.connect(e_name(site))
        s1.connect(cloud_name(site))
        s2.connect(cloud_name(site))
        s2.connect(a_name(site))
    return net


class TopologySwitchTest(unittest.TestCase):
    def test_switch_classes_and_unique_dpid(self):
        cfg = load_config('configs/parallel-min.json')
        topo = ParallelTopology(cfg)
        dpids = []
        for name in topo.switches():
            info = topo.nodeInfo(name)
            self.assertIn('dpid', info)
            dpids.append(info['dpid'])
            if name.startswith('S'):
                self.assertIs(info.get('cls'), OVSBridge)
            else:
                self.assertIsNone(info.get('cls'))
        self.assertEqual(len(dpids), len(set(dpids)))


class OvsConfigTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config('configs/parallel-min.json')
        self.net = _build_net(self.cfg)

    def test_trunk_roundtrip(self):
        switch = self.net.get('S1_bj')
        intf = switch.intf_to(e_name(self.cfg.site('bj')))
        set_trunk(switch, intf, NON_PARALLEL_VLANS)
        vlans = trunk_vlans(switch, intf)
        self.assertIn(3500, vlans)
        self.assertIn(3901, vlans)
        self.assertNotIn(PARALLEL_VLAN_MIN, vlans)

    def test_configure_policies(self):
        configure_ovs_switches(self.net, self.cfg)
        for site in self.cfg.sites:
            s1 = self.net.get(s1_name(site))
            s2 = self.net.get(s2_name(site))

            self.assertEqual(sorted(s1.trunks.values()),
                             sorted([ALL_VLANS, ALL_VLANS]))

            uplink = s2.intf_to(a_name(site))
            self.assertEqual(s2.trunks[uplink.name], NON_PARALLEL_VLANS)
            self.assertEqual(s2.trunks[s2.intf_to(cloud_name(site)).name],
                             ALL_VLANS)

            vlans = trunk_vlans(s2, uplink)
            self.assertNotIn(PARALLEL_VLAN_MIN, vlans)
            self.assertIn(3500, vlans)
            self.assertIn(3901, vlans)


if __name__ == '__main__':
    unittest.main()
