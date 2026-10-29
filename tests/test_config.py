#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for config validation."""

import json
import os
import tempfile
import unittest

from parallel.config import IFNAME_MAX, load_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _path(*parts):
    return os.path.join(ROOT, *parts)


class IfnameLengthTest(unittest.TestCase):
    def _config(self, abbr):
        with open(_path('configs', 'parallel-min.json'),
                  encoding='utf-8') as handle:
            raw = json.load(handle)
        raw['sites'][0]['abbr'] = abbr
        raw['fiti_topology'] = _path('configs', 'fiti.json')
        for network in raw['networks']:
            network['sites'] = [abbr if item == 'bj' else item
                                for item in network['sites']]
        handle = tempfile.NamedTemporaryFile(
            'w', suffix='.json', delete=False, encoding='utf-8')
        try:
            json.dump(raw, handle)
        finally:
            handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_long_abbr_is_rejected(self):
        # cl_tongji_1-eth0 超过 15 字节，Mininet 建 veth 时会失败
        with self.assertRaises(ValueError) as ctx:
            load_config(self._config('tongji'))
        self.assertIn('接口名', str(ctx.exception))

    def test_four_letter_abbr_is_accepted(self):
        cfg = load_config(self._config('bupt'))
        self.assertEqual(cfg.sites[0].abbr, 'bupt')

    def test_real_configs_fit(self):
        for name in ('parallel.json', 'parallel-min.json'):
            cfg = load_config(_path('configs', name))
            for site in cfg.sites:
                self.assertLessEqual(
                    len('cl_%s_1-eth0' % site.abbr), IFNAME_MAX, site.abbr)


if __name__ == '__main__':
    unittest.main()
