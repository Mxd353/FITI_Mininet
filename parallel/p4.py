#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并行网络 P4 编译和运行时名称解析。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from parallel.config import DEFAULT_P4_JSON, DEFAULT_P4_SRC


def compile_p4(src=None, out=None, force=False):
    src = os.path.abspath(src or DEFAULT_P4_SRC)
    out = os.path.abspath(out or DEFAULT_P4_JSON)
    p4info = os.path.splitext(out)[0] + '.p4info.txt'
    if (not force and os.path.exists(out) and os.path.exists(p4info)
            and os.path.getmtime(out) >= os.path.getmtime(src)
            and os.path.getmtime(p4info) >= os.path.getmtime(src)):
        return out
    os.makedirs(os.path.dirname(out), exist_ok=True)
    compiler = shutil.which('p4c-bm2-ss') or 'p4c-bm2-ss'
    proc = subprocess.run(
        [compiler, '--arch', 'v1model',
         '--p4runtime-files', p4info, '-o', out, src],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError('P4 编译失败:\n%s\n%s'
                           % (proc.stdout, proc.stderr))
    return out


def runtime_names(json_path=None):
    """把 P4 JSON 中的完整名称映射为短名，供 Thrift CLI 使用。"""
    json_path = json_path or DEFAULT_P4_JSON
    with open(json_path, encoding='utf-8') as handle:
        program = json.load(handle)
    tables = {}
    for pipeline in program.get('pipelines', []):
        for table in pipeline.get('tables', []):
            name = table['name']
            tables[name.rsplit('.', 1)[-1]] = name
    actions = {}
    for action in program.get('actions', []):
        name = action['name']
        actions[name] = name
    return {'tables': tables, 'actions': actions}
