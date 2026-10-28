#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small wrappers around the bmv2 Thrift CLI used by the parallel network."""

from __future__ import annotations

import re
import shutil
import subprocess


def _cli_path():
    return shutil.which('simple_switch_CLI') or 'simple_switch_CLI'


def switch_cli(thrift_port, commands):
    """Feed runtime commands to simple_switch_CLI and return its output."""
    commands = list(commands)
    if not commands:
        return ''
    try:
        proc = subprocess.run([_cli_path(), '--thrift-port', str(thrift_port)],
                              input='\n'.join(commands) + '\n',
                              capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError('找不到 simple_switch_CLI，请先安装 bmv2')
    output = proc.stdout + proc.stderr
    if proc.returncode != 0 or 'Error' in output or 'Invalid' in output:
        raise RuntimeError('Thrift 端口 %d 的运行时命令失败:\n%s'
                           % (thrift_port, output.strip()))
    return output


def mc_handles(output):
    """Extract multicast node handles from simple_switch_CLI output."""
    return [int(value) for value in
            re.findall(r'node was created with handle\s+(\d+)', output)]
