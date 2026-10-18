#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台输出过滤与 build/logs 日志文件。"""

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager

from p4_mininet import DEFAULT_LOG_DIR

__all__ = ['ensure_log_dir', 'quiet_mininet_console', 'open_detail_log',
           'Progress']


def ensure_log_dir(log_dir=None):
    """创建并返回日志目录（默认 build/logs）。"""
    path = os.path.abspath(log_dir or DEFAULT_LOG_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def _make_file_handler(path):
    """Open a log file for append; fall back to a uid-suffixed file."""
    candidates = [path, '%s.%d' % (path, os.geteuid())]
    for candidate in candidates:
        try:
            return logging.FileHandler(candidate, mode='a', encoding='utf-8')
        except OSError:
            continue
    return None


def _attach_mininet_file_log(log_dir=None):
    from mininet.log import lg

    directory = ensure_log_dir(log_dir)
    handler = _make_file_handler(os.path.join(directory, 'mininet.log'))
    if handler is None:
        return None
    handler.setLevel(logging.DEBUG)
    # 保持原始消息内容，不额外加时间戳 / 级别前缀
    handler.setFormatter(logging.Formatter('%(message)s'))
    lg.addHandler(handler)
    return handler


class _BuildConsoleHandler(logging.Handler):
    """构建阶段只输出创建网络、主机列表和交换机列表。"""

    def __init__(self, allow_names=None, stream=None):
        super().__init__(level=logging.DEBUG)
        self.allow_names = set(allow_names or ())
        self.stream = stream or sys.stderr
        self.in_hosts = False
        self.in_switches = False
        self.setFormatter(logging.Formatter('%(message)s'))

    def _write(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            pass

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return

        if record.levelno >= logging.WARNING:
            self._write(message if message.endswith('\n') else message + '\n')
            return

        stripped = message.strip()
        if stripped.startswith('*** Creating network'):
            self._write('*** Creating network\n')
            return
        if stripped.startswith('*** Adding hosts:'):
            self._write('*** Adding hosts:\n')
            self.in_hosts = True
            self.in_switches = False
            return
        if stripped.startswith('*** Adding switches:'):
            if self.in_hosts:
                self._write('\n')
            self._write('*** Adding switches:\n')
            self.in_hosts = False
            self.in_switches = True
            return
        if stripped.startswith('*** Adding links:'):
            if self.in_switches:
                self._write('\n')
            self.in_hosts = False
            self.in_switches = False
            return
        if stripped in self.allow_names:
            self._write(stripped + ' ')


@contextmanager
def quiet_mininet_console(log_dir=None, console_level=logging.WARNING,
                          allow_build_lists=False, allow_names=None):
    """简化 Mininet 控制台输出，同时把完整输出写入 mininet.log。

    ``allow_build_lists=True`` 时只保留创建网络、主机和交换机列表。
    """
    from mininet.log import lg

    handler = _attach_mininet_file_log(log_dir)
    old_logger_level = lg.level
    old_console_level = lg.ch.level
    build_handler = None
    if allow_build_lists:
        build_handler = _BuildConsoleHandler(
            allow_names, stream=getattr(lg.ch, 'stream', None) or sys.stderr)
        build_handler.setLevel(logging.DEBUG)

    lg.setLevel(logging.DEBUG)
    if build_handler is not None:
        lg.ch.setLevel(logging.CRITICAL + 1)
        lg.addHandler(build_handler)
    else:
        lg.ch.setLevel(console_level)

    try:
        yield handler
    finally:
        if build_handler is not None:
            lg.removeHandler(build_handler)
            build_handler.close()
        lg.ch.setLevel(old_console_level)
        lg.setLevel(old_logger_level)
        if handler is not None:
            lg.removeHandler(handler)
            handler.close()


def open_detail_log(log_dir=None, name='fiti-install.log'):
    """打开安装明细日志；不可写时尝试 uid 后缀文件，再失败返回 None。"""
    directory = ensure_log_dir(log_dir)
    for candidate in (os.path.join(directory, name),
                      os.path.join(directory, '%s.%d' % (name, os.geteuid()))):
        try:
            return open(candidate, 'a', encoding='utf-8')
        except OSError:
            continue
    return None


class Progress:
    """终端进度条 / 小动画。

    终端（TTY）下显示：
      * total 为 None：旋转动画 + 已用时间
      * total 给定：``[####----] 12/41`` + 已用时间

    非 TTY（重定向到文件 / CI）时只打印开始和结束行，避免大量控制字符。

    用法::

        with Progress('*** 构建拓扑', done='*** 拓扑构建完成') as p:
            do_work()
            p.advance(1)
    """

    FRAMES = '|/-\\'

    def __init__(self, message, total=None, done=None, stream=None,
                 interval=0.15, width=24, animate=True):
        self.message = message
        self.total = total
        self.done = done
        self.stream = stream or sys.stderr
        self.interval = interval
        self.width = width
        self.animate = animate
        self.current = 0
        self.label = ''
        self._stop = threading.Event()
        self._thread = None
        self._started = 0.0
        self._frame = 0
        self._lock = threading.Lock()
        try:
            self._tty = self.animate and self.stream.isatty()
        except Exception:
            self._tty = False

    def __enter__(self):
        self._started = time.monotonic()
        if self._tty:
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()
        elif self.animate:
            self._write(self.message + '\n')
        return self

    def advance(self, step=1, label=None):
        with self._lock:
            self.current += step
            if label:
                self.label = label
        if self._tty:
            self._render()

    def _write(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            pass

    def _animate(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                self._frame += 1
            self._render()

    def _render(self):
        elapsed = time.monotonic() - self._started
        with self._lock:
            if self.total:
                ratio = min(1.0, max(0.0, self.current / self.total))
                filled = int(self.width * ratio)
                bar = '#' * filled + '-' * (self.width - filled)
                text = '%s [%s] %d/%d (%.1fs)' % (
                    self.message, bar, self.current, self.total, elapsed)
            else:
                frame = self.FRAMES[self._frame % len(self.FRAMES)]
                text = '%s %s (%.1fs)' % (self.message, frame, elapsed)
            if self.label:
                text += ' ' + self.label
        self._write('\r' + text + '\033[K')

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

        elapsed = time.monotonic() - self._started
        if exc_type is not None:
            if issubclass(exc_type, KeyboardInterrupt):
                text = '%s 已中断' % self.message
            else:
                text = '%s 失败' % self.message
        else:
            text = self.done or ('%s 完成' % self.message)

        if self._tty:
            self._write('\r%s (%.1fs)\033[K\n' % (text, elapsed))
        else:
            self._write('%s (%.1fs)\n' % (text, elapsed))
        return False
