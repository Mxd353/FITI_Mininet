#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the overlay/underlay path map (no root required)."""

import os
import unittest

from parallel.config import load_config
from tools.path_map import (build_segments, parse_host, render_markdown,
                            render_text)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _path(*parts):
    return os.path.join(ROOT, *parts)


def _segments(cfg, network_id, src, dst):
    network = cfg.network(network_id)
    return build_segments(cfg, network, parse_host(cfg, src),
                          parse_host(cfg, dst))


class PathMapTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(_path('configs', 'parallel-min.json'))

    def test_cross_site_l3_uses_fiti_path_and_srv6(self):
        segments, info = _segments(self.cfg, 1, 'bj_1', 'sh_1')
        devices = ['%s → %s' % (item['from'], item['to'])
                   for item in segments]
        self.assertIn('C_bj → C_tjn', devices)
        self.assertIn('C_tjn → C_jnn', devices)
        self.assertIn('C_jnn → C_sh', devices)
        encap = ' '.join(item['encap'] for item in segments)
        self.assertIn('SRv6 L3', encap)
        self.assertIn('fd00:0:1:1::1:1', encap)
        self.assertIn('fd00:0:2:1::1:1', encap)
        self.assertIn('240a:a806:1:2::10', info['inner'])

    def test_cross_site_l2_uses_l2_sid(self):
        segments, _ = _segments(self.cfg, 3, 'bj_1', 'sh_1')
        encap = ' '.join(item['encap'] for item in segments)
        self.assertIn('SRv6 L2', encap)
        self.assertIn('fd00:0:1:1::2:3', encap)

    def test_same_site_is_two_l2_segments(self):
        segments, _ = _segments(self.cfg, 1, 'bj_1', 'bj_2')
        self.assertEqual(len(segments), 2)
        self.assertIn('VXLAN', segments[1]['note'])
        self.assertEqual(segments[-1]['to'], 'cl_bj_2')

    def test_host_parsing(self):
        site, host, name = parse_host(self.cfg, 'cl_sh_2')
        self.assertEqual((site.abbr, host, name), ('sh', 2, 'cl_sh_2'))
        site, host, name = parse_host(self.cfg, 'bj')
        self.assertEqual((site.abbr, host, name), ('bj', 1, 'cl_bj_1'))

    def test_markdown_golden(self):
        with open(_path('configs', 'expected', 'path-map-min.md'),
                  encoding='utf-8') as handle:
            expected = handle.read()
        network = self.cfg.network(1)
        segments, info = build_segments(
            self.cfg, network, parse_host(self.cfg, 'bj_1'),
            parse_host(self.cfg, 'sh_1'))
        self.assertEqual(render_markdown(self.cfg, network,
                                         parse_host(self.cfg, 'bj_1'),
                                         parse_host(self.cfg, 'sh_1'),
                                         segments, info), expected)

    def test_text_renderer(self):
        network = self.cfg.network(1)
        segments, info = build_segments(
            self.cfg, network, parse_host(self.cfg, 'bj_1'),
            parse_host(self.cfg, 'sh_1'))
        text = render_text(self.cfg, network, parse_host(self.cfg, 'bj_1'),
                           parse_host(self.cfg, 'sh_1'), segments, info)
        self.assertIn('业务路径', text)
        self.assertIn('SRv6 L3', text)


if __name__ == '__main__':
    unittest.main()
