#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the EVPN route model (no Mininet/root required)."""

import unittest

from parallel.config import load_config
from parallel.evpn import (RouteTable, build_evpn_routes,
                           validate_evpn_routes)


class EvpnRouteModelTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.table = build_evpn_routes(self.cfg)

    def test_local_route_counts(self):
        # L3: 2 networks x 41 sites; L2: 2 networks x 41 sites.
        self.assertEqual(len(self.table.l3_routes()), 82)
        self.assertEqual(len(self.table.imet_routes()), 82)
        self.assertEqual(len(self.table.mac_ip_routes()), 0)
        self.assertEqual(len(self.table.routes), 164)

    def test_route_validation(self):
        self.assertEqual(validate_evpn_routes(self.table), [])

    def test_fiti_core_topology(self):
        self.assertEqual(len(self.cfg.fiti.links), 43)
        self.assertEqual(self.cfg.core_next_hop(
            self.cfg.site('bj'), self.cfg.site('sh')), 'tjn')
        self.assertNotEqual(
            self.cfg.core_address(self.cfg.site('bj'), self.cfg.site('tjn')),
            self.cfg.core_address(self.cfg.site('tjn'), self.cfg.site('bj')))

    def test_l2_cloud_addresses_are_unique(self):
        for network in self.cfg.l2_networks():
            addresses = [self.cfg.cloud_address(network, site)
                         for site in self.cfg.network_sites(network)]
            self.assertEqual(len(addresses), len(set(addresses)))

    def test_route_update_and_roundtrip(self):
        route = self.table.l3_routes()[0]
        update = self.table.withdraw(route)
        self.assertEqual(update.action, 'remove')
        self.assertIsNone(self.table.withdraw(route))
        update = self.table.advertise(route)
        self.assertEqual(update.action, 'add')

        payload = self.table.to_dict()
        restored = RouteTable.from_dict(payload)
        self.assertEqual(restored.to_dict(), payload)
        self.assertEqual(validate_evpn_routes(restored), [])


if __name__ == '__main__':
    unittest.main()
