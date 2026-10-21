#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BMv2 switch wrappers for the parallel-network scenario."""

from __future__ import annotations

import shutil
from functools import partial

from fiti.p4_mininet import P4Switch
from parallel.topology import SWITCH_IDS


class ParallelP4Switch(P4Switch):
    """simple_switch with a stable Thrift port for each logical switch."""

    def __init__(self, name, json_path=None, thrift_base=11000,
                 p4_log_level='info', pcap_dump=None, log_dir=None,
                 sw_path=None, **kwargs):
        if name not in SWITCH_IDS:
            raise ValueError('未定义的并行网络交换机: %s' % name)
        switch_id = SWITCH_IDS[name]
        super(ParallelP4Switch, self).__init__(
            name,
            sw_path=sw_path or (shutil.which('simple_switch')
                                or 'simple_switch'),
            json_path=json_path,
            thrift_port=int(thrift_base) + switch_id,
            device_id=switch_id,
            log_level=p4_log_level,
            pcap_dump=pcap_dump,
            log_dir=log_dir,
            **kwargs)


def make_switch_factory(json_path, thrift_base=11000, p4_log_level='info',
                        pcap_dump=None, log_dir=None):
    return partial(ParallelP4Switch,
                   json_path=json_path,
                   thrift_base=thrift_base,
                   p4_log_level=p4_log_level,
                   pcap_dump=pcap_dump,
                   log_dir=log_dir)
