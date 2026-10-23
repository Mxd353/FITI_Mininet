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
        self.assertEqual(len(self.table.l3_routes()), 4)
        self.assertEqual(len(self.table.imet_routes()), 4)
        self.assertEqual(len(self.table.mac_ip_routes()), 0)
        self.assertEqual(len(self.table.routes), 8)

    def test_route_validation(self):
        self.assertEqual(validate_evpn_routes(self.table), [])

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
