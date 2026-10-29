#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clean stale interfaces left by an interrupted parallel-network run."""

from __future__ import annotations

import argparse
import os
import re
import subprocess

#: Node interface names created by the topology (``A_bj-eth1``, ``S1_sh-eth2``,
#: ``cl_jnn-eth0``, ``pub_border-eth1`` ...).  Keep this narrow so unrelated
#: interfaces on the host are never touched.
STALE_RE = re.compile(
    r'^(?:A|C|E|S1|S2|cl)_[A-Za-z0-9_]+-eth\d+$'
    r'|^(?:pub_border|fix)-eth\d+$')


def _run(command):
    return subprocess.run(command, capture_output=True, text=True)


def stale_links():
    """Return the interface names that look like leftovers from our topology."""
    output = _run(['ip', '-o', 'link', 'show']).stdout
    links = []
    for line in output.splitlines():
        parts = line.split(':', 2)
        if len(parts) < 2:
            continue
        name = parts[1].strip().split('@', 1)[0]
        if STALE_RE.match(name):
            links.append(name)
    return sorted(links)


def cleanup_stale_links(verbose=True):
    links = stale_links()
    for name in links:
        _run(['ip', 'link', 'del', name])
    if verbose and links:
        shown = ' '.join(links[:6])
        if len(links) > 6:
            shown += ' ...'
        print('*** 清理残留接口 %d 个: %s' % (len(links), shown))
    return links


def _ancestors():
    pids = set()
    pid = os.getpid()
    while pid > 1:
        pids.add(pid)
        try:
            with open('/proc/%d/stat' % pid, encoding='utf-8') as handle:
                fields = handle.read().split()
            pid = int(fields[3])
        except (OSError, ValueError, IndexError):
            break
    return pids


def other_instances(pattern='[p]arallel_net.py'):
    """Return PIDs of other parallel_net.py runs, excluding our own ancestors."""
    output = _run(['pgrep', '-f', pattern]).stdout
    pids = {int(item) for item in output.split() if item.isdigit()}
    return sorted(pids - _ancestors())


def main(argv=None):
    parser = argparse.ArgumentParser(description='清理残留的并行网络接口')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if args.dry_run:
        for name in stale_links():
            print(name)
        return 0
    cleanup_stale_links()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
