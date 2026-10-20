#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FITI 随机通信模式：在不同节点之间随机选择主机互 ping。

支持两种范围：

* ``slice``  -- 每条流都在同一个切片内，源/目的节点必须不同（默认）
* ``global`` -- 从所有主机中随机选择，允许跨切片；同切片流期望可达，
  跨切片流期望被 VLAN 隔离（100% 丢包）

这个模块不依赖 Mininet 的具体类型，只要求传入的 host 对象具有
``popen()`` 方法，并带有 name / node_id / group / ip 元数据。

``run_random_traffic(..., continuous=True)`` 会一轮接一轮持续发送，直到
收到 Ctrl-C 或 ``stop_event`` 被设置。
"""

import os
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

__all__ = [
    'TrafficHost',
    'FlowResult',
    'TrafficTotals',
    'parse_ping_output',
    'select_random_pairs',
    'run_random_traffic',
]

PING_SUMMARY_RE = re.compile(
    r'(\d+)\s+packets transmitted,\s*(\d+)\s+(?:packets\s+)?received',
    re.IGNORECASE)
PING_LOSS_RE = re.compile(
    r'([0-9]+(?:\.[0-9]+)?)%\s+packet loss', re.IGNORECASE)


@dataclass(frozen=True)
class TrafficHost:
    """一台参与随机通信的主机。"""

    name: str
    node_id: int
    group: str          # 切片 id；基础模式为 'base'
    host: object        # Mininet Host 对象
    ip: str


@dataclass
class FlowResult:
    """一条随机流的结果。"""

    src: TrafficHost
    dst: TrafficHost
    sent: int = 0
    received: int = 0
    loss: float = 100.0
    expected: str = 'reachable'   # reachable / isolated
    ok: bool = False
    error: Optional[str] = None
    output: str = ''


@dataclass
class TrafficTotals:
    """持续模式下的累计统计，避免无限保存每条 FlowResult。"""

    flows: int = 0
    reachable: int = 0
    isolated: int = 0
    sent: int = 0
    received: int = 0
    reachable_sent: int = 0
    reachable_received: int = 0
    isolated_sent: int = 0
    isolated_received: int = 0
    failures: int = 0

    def add(self, result):
        self.flows += 1
        self.sent += result.sent
        self.received += result.received
        if result.expected == 'reachable':
            self.reachable += 1
            self.reachable_sent += result.sent
            self.reachable_received += result.received
        else:
            self.isolated += 1
            self.isolated_sent += result.sent
            self.isolated_received += result.received
        if not result.ok:
            self.failures += 1

    @staticmethod
    def _loss(sent, received, flows):
        if sent:
            return 100.0 * (sent - received) / sent
        return 100.0 if flows else 0.0

    @property
    def loss(self):
        return self._loss(self.sent, self.received, self.flows)

    @property
    def reachable_loss(self):
        return self._loss(self.reachable_sent, self.reachable_received,
                          self.reachable)

    @property
    def isolated_loss(self):
        return self._loss(self.isolated_sent, self.isolated_received,
                          self.isolated)

    @property
    def ok(self):
        return self.flows > 0 and self.failures == 0


# --------------------------------------------------------------------------
# 选流
# --------------------------------------------------------------------------

def _pairs_within(hosts):
    """同一组内、不同节点之间的有序主机对。"""
    return [(src, dst) for src in hosts for dst in hosts
            if src is not dst and src.node_id != dst.node_id]


def select_random_pairs(hosts, count=10, scope='slice', seed=None):
    """随机选择 ``count`` 条源/目的节点不同的流。

    scope='slice':
      按 group（切片）分组，尽量让每个切片至少有一条流，然后随机填充。
    scope='global':
      从所有主机中随机选择，允许同组或跨组。
    """
    if scope not in ('slice', 'global'):
        raise ValueError("scope 必须是 'slice' 或 'global'")
    count = max(0, int(count))
    if count == 0:
        return []

    hosts = list(hosts)
    if len(hosts) < 2:
        return []
    rng = random.Random(seed)

    if scope == 'global':
        candidates = _pairs_within(hosts)
        if not candidates:
            return []
        if count <= len(candidates):
            return rng.sample(candidates, count)
        return [rng.choice(candidates) for _ in range(count)]

    by_group = defaultdict(list)
    for host in hosts:
        by_group[host.group].append(host)

    group_pairs = {}
    for group, group_hosts in by_group.items():
        pairs = _pairs_within(group_hosts)
        if pairs:
            group_pairs[group] = pairs
    if not group_pairs:
        return []

    groups = sorted(group_pairs)
    rng.shuffle(groups)
    selected = []

    # 先保证每个切片（或尽可能多的切片）至少有一条流
    for group in groups:
        if len(selected) >= count:
            break
        selected.append(rng.choice(group_pairs[group]))

    # 剩余名额从所有候选里随机补齐
    all_pairs = [pair for group in groups for pair in group_pairs[group]]
    remaining = [pair for pair in all_pairs if pair not in selected]
    rng.shuffle(remaining)
    selected.extend(remaining[:max(0, count - len(selected))])

    # count 大于候选数时允许重复，避免直接失败
    while len(selected) < count and all_pairs:
        selected.append(rng.choice(all_pairs))
    return selected[:count]


# --------------------------------------------------------------------------
# ping 执行与解析
# --------------------------------------------------------------------------

def parse_ping_output(text):
    """返回 (sent, received, loss)。无法解析时按 100% 丢包处理。"""
    match = PING_SUMMARY_RE.search(text or '')
    if match:
        sent = int(match.group(1))
        received = int(match.group(2))
        loss = 100.0 * (sent - received) / sent if sent else 100.0
        return sent, received, max(0.0, loss)

    loss_match = PING_LOSS_RE.search(text or '')
    loss = float(loss_match.group(1)) if loss_match else 100.0
    return 0, 0, loss


def _flow_expectation(scope, src, dst):
    """返回 'reachable' 或 'isolated'。"""
    if scope == 'global':
        return 'reachable' if src.group == dst.group else 'isolated'
    return 'reachable'


def _flow_ok(loss, expected):
    if expected == 'reachable':
        return loss < 1.0
    return loss > 99.0


def _format_flow(result):
    status = 'PASS' if result.ok else 'FAIL'
    extra = '' if not result.error else ' (%s)' % result.error
    return ('  [%s] %s(s%d) -> %s(s%d): %d/%d 包, %.1f%% 丢包, '
            '期望=%s %s%s'
            % (result.src.group, result.src.name, result.src.node_id,
               result.dst.name, result.dst.node_id,
               result.received, result.sent, result.loss,
               result.expected, status, extra))


def _summarize(results, log=print):
    reachable = [r for r in results if r.expected == 'reachable']
    isolated = [r for r in results if r.expected == 'isolated']

    def stats(items):
        sent = sum(r.sent for r in items)
        received = sum(r.received for r in items)
        loss = 100.0 * (sent - received) / sent if sent else (
            100.0 if items else 0.0)
        return len(items), sent, received, loss

    log('')
    log('随机通信汇总:')
    for label, items in (('可达流', reachable), ('隔离流', isolated)):
        if not items:
            continue
        count, sent, received, loss = stats(items)
        log('  %s: %d 条, %d/%d 包, %.1f%% 丢包'
            % (label, count, received, sent, loss))
    ok = all(result.ok for result in results)
    log('结果: %s' % ('全部符合预期' if ok else '存在不符合预期的流'))
    return ok


def _run_batch(pairs, packets, interval, size, timeout, scope, env):
    """并发跑一批 ping 并收集 FlowResult。Ctrl-C 时杀掉子进程再抛出。"""
    running = []
    results = []
    try:
        for src, dst in pairs:
            cmd = ['ping', '-c', str(packets), '-i', str(interval),
                   '-W', '1', '-s', str(int(size)), dst.ip]
            try:
                process = src.host.popen(cmd, env=env)
            except Exception as exc:  # pragma: no cover - 取决于运行环境
                running.append((src, dst, None, str(exc)))
                continue
            running.append((src, dst, process, None))

        for src, dst, process, error in running:
            output = ''
            if process is not None:
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                    raw = stdout or stderr or b''
                    output = raw.decode('utf-8', 'replace') \
                        if isinstance(raw, bytes) else str(raw)
                except Exception as exc:  # timeout / process error
                    try:
                        process.kill()
                        process.wait(timeout=2)
                    except Exception:
                        pass
                    output = ''
                    error = str(exc)

            sent, received, loss = parse_ping_output(output)
            if sent == 0:
                # 没有 ping 摘要（不可达、命令失败或超时）：按整条流全丢处理
                sent, received, loss = packets, 0, 100.0
            expected = _flow_expectation(scope, src, dst)
            results.append(FlowResult(
                src=src, dst=dst, sent=sent, received=received,
                loss=loss, expected=expected,
                ok=_flow_ok(loss, expected),
                error=error, output=output))
    except KeyboardInterrupt:
        for _, _, process, _ in running:
            if process is None:
                continue
            try:
                if process.poll() is None:
                    process.kill()
            except Exception:
                pass
        raise
    return results


def _round_line(round_no, totals):
    return ('第 %d 轮: %d 条流 (可达 %d / 隔离 %d), %d/%d 包, '
            '%.1f%% 丢包, 失败 %d'
            % (round_no, totals.flows, totals.reachable, totals.isolated,
               totals.received, totals.sent, totals.loss, totals.failures))


def _summarize_totals(totals, failures, log=print):
    if totals.flows == 0:
        log('结果: 未产生有效流')
        return False
    log('')
    log('随机通信汇总:')
    log('  总流数: %d' % totals.flows)
    if totals.reachable:
        log('  可达流: %d 条, %d/%d 包, %.1f%% 丢包'
            % (totals.reachable, totals.reachable_received,
               totals.reachable_sent, totals.reachable_loss))
    if totals.isolated:
        log('  隔离流: %d 条, %d/%d 包, %.1f%% 丢包 (期望 100%%)'
            % (totals.isolated, totals.isolated_received,
               totals.isolated_sent, totals.isolated_loss))
    if totals.failures:
        log('  不符合预期: %d 条' % totals.failures)
        for result in failures[:10]:
            log(_format_flow(result))
    log('结果: %s' % ('全部符合预期' if totals.ok else '存在不符合预期的流'))
    return totals.ok


def run_random_traffic(hosts, count=10, duration=5.0, interval=0.2,
                       size=56, scope='slice', seed=None, continuous=False,
                       stop_event=None, log=print):
    """随机 ping；continuous=True 时按轮持续发送直到 Ctrl-C / stop_event。

    非持续模式：跑一轮，返回 (results, ok)。
    持续模式：每轮持续时间仍为 duration，然后重新随机选流继续；
              返回 (failures, ok)，failures 只保留不符合预期的流。
    """
    seed_value = seed
    if seed_value is None:
        seed_value = random.SystemRandom().randrange(1, 2 ** 31)

    interval = max(0.01, float(interval))
    size = max(0, int(size))
    packets = max(1, int(round(float(duration) / interval)))
    timeout = max(5.0, packets * interval + 5.0)

    env = dict(os.environ)
    env['LC_ALL'] = 'C'  # 保证 ping 摘要可解析

    if not continuous:
        pairs = select_random_pairs(hosts, count=count, scope=scope,
                                    seed=seed_value)
        if not pairs:
            log('没有可用的不同节点主机对（检查主机数 / scope）')
            return [], False

        log('=== 随机通信（不同节点）===')
        log('流数=%d, 每流包数=%d, 间隔=%.2fs, 包大小=%dB, scope=%s, seed=%d'
            % (len(pairs), packets, interval, int(size), scope, seed_value))
        results = _run_batch(pairs, packets, interval, size, timeout,
                             scope, env)
        for result in results:
            log(_format_flow(result))
        return results, _summarize(results, log=log)

    # ---- 持续模式 ----
    log('=== 持续随机通信（不同节点，Ctrl-C 停止）===')
    log('每轮流数=%d, 每轮包数=%d, 间隔=%.2fs, 包大小=%dB, scope=%s, seed=%d'
        % (count, packets, interval, int(size), scope, seed_value))

    totals = TrafficTotals()
    failures = []
    round_no = 0
    round_duration = max(float(duration), 0.1)
    try:
        while not (stop_event is not None and stop_event.is_set()):
            round_no += 1
            round_started = time.monotonic()
            pairs = select_random_pairs(hosts, count=count, scope=scope,
                                        seed=seed_value + round_no)
            if not pairs:
                log('没有可用的不同节点主机对（检查主机数 / scope）')
                break

            results = _run_batch(pairs, packets, interval, size, timeout,
                                 scope, env)
            round_totals = TrafficTotals()
            round_failures = []
            for result in results:
                totals.add(result)
                round_totals.add(result)
                if not result.ok:
                    round_failures.append(result)
                    if len(failures) < 100:
                        failures.append(result)

            log(_round_line(round_no, round_totals))
            for result in round_failures[:10]:
                log(_format_flow(result))

            # 跨切片不可达时 ping 可能立刻返回；补足到本轮 duration，
            # 避免持续模式变成忙循环。
            remaining = round_duration - (time.monotonic() - round_started)
            if remaining > 0:
                if stop_event is not None and hasattr(stop_event, 'wait'):
                    stop_event.wait(remaining)
                else:
                    time.sleep(remaining)
    except KeyboardInterrupt:
        log('')
        log('*** 收到 Ctrl-C，停止随机通信')

    return failures, _summarize_totals(totals, failures, log=log)
