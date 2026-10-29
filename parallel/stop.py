#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop the parallel-network scenario with visible progress.

Run as root through ``make parallel-stop``:

1. SIGINT the Mininet CLI so readline can restore the terminal;
2. wait for it to exit, escalating to SIGTERM/SIGKILL only if needed;
3. stop leftover simple_switch processes;
4. run ``mn -c``;
5. delete stale interfaces and the agent socket.

The whole sequence is idempotent: running it when nothing is up only cleans
leftovers and prints progress.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

from parallel.cleanup import _ancestors

FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
AGENT_SOCKET = '/tmp/parallel_evpn.sock'
BASE_PATTERN = '[p]arallel_net.py'
SWITCH_PATTERN = '[s]imple_switch'


def _tty():
    return bool(getattr(sys.stdout, 'isatty', lambda: False)())


def _flush():
    sys.stdout.flush()


def _tick(label, started, index):
    if not _tty():
        return
    elapsed = time.time() - started
    sys.stdout.write('\r\033[K%s %s %.1fs'
                     % (FRAMES[index % len(FRAMES)], label, elapsed))
    _flush()


def _clear_line():
    if _tty():
        sys.stdout.write('\r\033[K')
        _flush()


def pids_for(pattern):
    try:
        output = subprocess.run(['pgrep', '-f', pattern],
                                capture_output=True, text=True).stdout
    except OSError:
        return []
    excluded = _ancestors()
    pids = []
    for item in output.split():
        if not item.isdigit():
            continue
        pid = int(item)
        if pid in excluded:
            continue
        pids.append(pid)
    return sorted(set(pids))


def _alive(pid):
    try:
        with open('/proc/%d/stat' % pid, encoding='utf-8') as handle:
            state = handle.read().split()[2]
    except (OSError, IndexError):
        return False
    return state != 'Z'


def _argv0(pid):
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as handle:
            data = handle.read()
    except OSError:
        return ''
    return data.split(b'\0')[0].decode('utf-8', 'replace')


def signal_targets(pids):
    """Drop the ``sudo`` wrapper so the child gets exactly one signal."""
    targets = [pid for pid in pids
               if os.path.basename(_argv0(pid)) != 'sudo']
    return targets or list(pids)


def send_signal(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def wait_until(label, pattern, timeout, dry_run=False):
    """Wait for ``pattern`` to disappear, showing a spinner."""
    pids = pids_for(pattern)
    if not pids:
        print('  %s ... 无需等待' % label)
        return True
    started = time.time()
    index = 0
    while True:
        if dry_run:
            print('  %s ... 跳过（dry-run，PID %s）'
                  % (label, ','.join(str(pid) for pid in pids)))
            return True
        if not any(_alive(pid) for pid in pids):
            _clear_line()
            print('  %s ... 完成 (%.1fs)' % (label, time.time() - started))
            return True
        if time.time() - started >= timeout:
            _clear_line()
            print('  %s ... 超时 (%.1fs)' % (label, time.time() - started))
            return False
        _tick(label, started, index)
        index += 1
        time.sleep(0.1)


def run_with_spinner(label, command, timeout=120, dry_run=False):
    if dry_run:
        print('  %s ... 跳过（dry-run: %s）' % (label, ' '.join(command)))
        return 0
    started = time.time()
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    except OSError as exc:
        print('  %s ... 无法执行: %s' % (label, exc))
        return 1
    index = 0
    while proc.poll() is None:
        if time.time() - started >= timeout:
            proc.kill()
        _tick(label, started, index)
        index += 1
        time.sleep(0.1)
    output = proc.stdout.read() if proc.stdout is not None else ''
    _clear_line()
    elapsed = time.time() - started
    if proc.returncode == 0:
        print('  %s ... 完成 (%.1fs)' % (label, elapsed))
    else:
        print('  %s ... 失败 (%.1fs, exit %d)' % (label, elapsed,
                                                 proc.returncode))
        tail = output.strip().splitlines()[-5:]
        for line in tail:
            print('      %s' % line)
    return proc.returncode


def stop_processes(label, pattern, first_signal, first_timeout,
                   dry_run=False):
    pids = pids_for(pattern)
    if not pids:
        print('  %s ... 没有进程' % label)
        return
    targets = signal_targets(pids)
    print('  %s ... %s %s（PID %s）'
          % (label, '将发送' if dry_run else '发送', first_signal.name,
             ','.join(str(pid) for pid in targets)))
    if not dry_run:
        send_signal(targets, first_signal)
    if wait_until('%s 退出' % label, pattern, first_timeout, dry_run):
        return
    if not dry_run:
        send_signal(signal_targets(pids_for(pattern)), signal.SIGTERM)
    if wait_until('%s 响应 SIGTERM' % label, pattern, 5, dry_run):
        return
    if not dry_run:
        send_signal(signal_targets(pids_for(pattern)), signal.SIGKILL)
    wait_until('%s 被强制结束' % label, pattern, 3, dry_run)


def restore_terminal():
    try:
        subprocess.run(['stty', 'sane'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except OSError:
        pass
    sys.stdout.write('\033[?1000l\033[?1002l\033[?1003l'
                     '\033[?1006l\033[?2004l')
    _flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description='停止并行网络（带进度）')
    parser.add_argument('--dry-run', action='store_true',
                        help='只打印将要执行的动作')
    args = parser.parse_args(argv)

    print('==> 停止并行网络')
    try:
        stop_processes('parallel_net.py', BASE_PATTERN, signal.SIGINT, 20,
                       args.dry_run)
        stop_processes('simple_switch', SWITCH_PATTERN, signal.SIGTERM, 5,
                       args.dry_run)
        run_with_spinner('清理 Mininet 网络', ['mn', '-c'],
                         timeout=120, dry_run=args.dry_run)

        from parallel.cleanup import cleanup_stale_links
        if args.dry_run:
            print('  清理残留接口 ... 跳过（dry-run）')
        else:
            links = cleanup_stale_links(verbose=False)
            print('  清理残留接口 ... %s'
                  % ('%d 个' % len(links) if links else '无'))

        if os.path.exists(AGENT_SOCKET):
            if args.dry_run:
                print('  删除 agent socket ... 跳过（dry-run）')
            else:
                try:
                    os.unlink(AGENT_SOCKET)
                    print('  删除 agent socket ... 完成')
                except OSError as exc:
                    print('  删除 agent socket ... 失败: %s' % exc)
        else:
            print('  删除 agent socket ... 不存在')
    finally:
        if not args.dry_run:
            restore_terminal()
    print('*** 清理完成，终端已恢复')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
