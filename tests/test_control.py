#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the static control-plane installer (no root required)."""

import unittest

import parallel.control as control


class _Switch(object):
    thrift_port = 11501


class _Net(object):
    def get(self, name):
        return _Switch()


class InstallOneSwitchTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._original = control.switch_cli

    def tearDown(self):
        control.switch_cli = self._original

    def _install(self, output, groups):
        control.switch_cli = lambda port, cmds: (
            self.calls.append((port, list(cmds))) or output)
        control._install_one_switch(
            _Net(), 'E_bj',
            ['mc_mgrp_create 100', 'mc_node_create 0 1',
             'mc_node_create 1 2'],
            {'E_bj': groups})

    def test_associates_handles_in_order(self):
        self._install('node was created with handle 1\n'
                      'node was created with handle 2',
                      [(100, 2)])
        self.assertEqual(self.calls[0][0], 11501)
        self.assertEqual(self.calls[0][1][0], 'mc_mgrp_create 100')
        self.assertEqual(self.calls[1][1],
                         ['mc_node_associate 100 1',
                          'mc_node_associate 100 2'])

    def test_handle_count_mismatch_raises(self):
        with self.assertRaises(RuntimeError):
            self._install('node was created with handle 1', [(100, 2)])


if __name__ == '__main__':
    unittest.main()
