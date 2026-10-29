#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live progress output for the verification runner."""

from __future__ import annotations

import sys
import threading
import time

FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
GREEN = '\033[32m'
RED = '\033[31m'
YELLOW = '\033[33m'
CYAN = '\033[36m'
DIM = '\033[2m'
RESET = '\033[0m'


class Reporter(object):
    """Print one line per case, with a spinner while the case runs."""

    def __init__(self, total, width=34, stream=None, animate=None,
                 color=None, quiet=False):
        self.total = total
        self.width = max(10, int(width))
        self.stream = stream or sys.stdout
        self.is_tty = bool(getattr(self.stream, 'isatty', lambda: False)())
        self.animate = self.is_tty if animate is None else bool(animate)
        self.color = self.is_tty if color is None else bool(color)
        self.quiet = quiet
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._index = 0
        self._name = ''
        self._started = 0.0
        self._durations = []

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def header(self, config_path, tags=None, faults=False):
        tag_text = ','.join(tags) if tags else '全部'
        self._writeln('%s 功能验收：%d 条用例，配置 %s，标签 %s%s'
                      % (self._paint('==>', CYAN), self.total, config_path,
                         tag_text, '，含故障用例' if faults else ''))

    def start_case(self, index, name):
        self._index = index
        self._name = name
        self._started = time.time()
        if self.animate and not self.quiet:
            self._stop.clear()
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()

    def finish_case(self, result):
        self._stop_spinner()
        elapsed = time.time() - self._started if self._started else result.seconds
        self._durations.append(elapsed)
        if self.quiet and result.status == 'pass':
            return
        status = {'pass': self._paint('PASS', GREEN),
                  'fail': self._paint('FAIL', RED),
                  'skip': self._paint('SKIP', YELLOW)}.get(
                      result.status, result.status)
        line = '[%2d/%d] %-*s %s  %5.1fs' % (
            self._index, self.total, self.width, result.name,
            status, elapsed)
        if result.status == 'skip' and result.reason:
            line += '  %s' % result.reason
        self._writeln(line)
        if result.status == 'fail' and result.reason:
            first = result.reason.strip().splitlines()[0]
            self._writeln('        %s' % self._paint(first, RED))

    def summary(self, results):
        passed = sum(1 for item in results if item.status == 'pass')
        failed = sum(1 for item in results if item.status == 'fail')
        skipped = sum(1 for item in results if item.status == 'skip')
        total_time = sum(self._durations)
        self._writeln('')
        color = RED if failed else GREEN
        self._writeln(self._paint(
            '通过 %d，失败 %d，跳过 %d，共 %d（用例耗时 %.1fs）'
            % (passed, failed, skipped, len(results), total_time), color))
        if self._durations:
            slowest = max(self._durations)
            self._writeln(self._paint('最慢用例 %.1fs' % slowest, DIM))

    def abort(self):
        self._stop_spinner()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _spin(self):
        index = 0
        while not self._stop.wait(0.1):
            frame = FRAMES[index % len(FRAMES)]
            elapsed = time.time() - self._started
            eta = self._eta()
            text = '\r\033[K%s [%2d/%d] %-*s %s %.1fs%s' % (
                self._paint(frame, CYAN), self._index, self.total,
                self.width, self._name, self._paint('running', DIM),
                elapsed, ('  eta %s' % eta) if eta else '')
            with self._lock:
                self.stream.write(text)
                self.stream.flush()
            index += 1

    def _eta(self):
        done = len(self._durations)
        if done < 1:
            return ''
        remaining = max(0, self.total - done - 1)
        seconds = sum(self._durations) / done * remaining
        if seconds < 1:
            return ''
        if seconds < 60:
            return '%ds' % int(seconds)
        return '%d:%02d' % (int(seconds) // 60, int(seconds) % 60)

    def _stop_spinner(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=0.5)
            self._thread = None
        if self.animate and not self.quiet:
            with self._lock:
                self.stream.write('\r\033[K')
                self.stream.flush()

    def _paint(self, text, style):
        if not self.color:
            return text
        return '%s%s%s' % (style, text, RESET)

    def _writeln(self, text):
        with self._lock:
            self.stream.write(text + '\n')
            self.stream.flush()
