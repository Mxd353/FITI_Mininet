#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for stale-interface cleanup (no root required)."""

import unittest

from parallel.cleanup import STALE_RE


class StalePatternTest(unittest.TestCase):
    def test_matches_topology_interfaces(self):
        for name in ('A_bj-eth1', 'C_sh-eth0', 'E_tjn-eth2',
                     'S1_jnn-eth1', 'S2_bj-eth1', 'cl_bj-eth0',
                     'pub_border-eth1', 'fix-eth1'):
            self.assertIsNotNone(STALE_RE.match(name), name)

    def test_ignores_unrelated_interfaces(self):
        for name in ('eth0', 'docker0', 'veth1234', 'br0',
                     'E_bj', 'S1_sh', 'lo'):
            self.assertIsNone(STALE_RE.match(name), name)

    def test_ignores_suffixes(self):
        self.assertIsNone(STALE_RE.match('E_bj-eth10x'))
        self.assertIsNone(STALE_RE.match('cl_bj-eth'))


if __name__ == '__main__':
    unittest.main()
