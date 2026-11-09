#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容量验收：把配置放大到 N 个切片，验证表项下发和抽样连通。

和其它用例不同，这里不跑 Mininet 之外的固定场景，而是：

1. 用 ``configs/parallel-min.json`` 当模板，生成一个 N 个 L3 切片的临时配置
   （只保留北京/上海两个站点、每站点 1 台 PM、1 个端点，避免设备数量爆炸）；
2. 启动拓扑，检查 E 的 ``vni_map``/``local_sid`` 表项数量；
3. 抽样 ping 第 1、中间、最后一个切片，确认都能跨站点到达；
4. 打印汇总，非 0 退出码表示失败。

用法：
    sudo python3 verify/scale.py --networks 4096
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from parallel.bmv2 import switch_cli          # noqa: E402
from parallel.config import load_config       # noqa: E402


def _build_config(path, networks, sites, hosts, endpoints, settle_mtu):
    with open(os.path.join(ROOT, 'configs', 'parallel-min.json'),
              encoding='utf-8') as handle:
        raw = json.load(handle)
    # 站点保留完整的最小集合（bj/tjn/jnn/sh）：FITI 里 bj→sh 要经 tjn、jnn 中转，
    # 少一个站点核心路径就不可达。切片成员只取 --sites 指定的两端。
    known = {site['abbr'] for site in raw['sites']}
    unknown = [abbr for abbr in sites if abbr not in known]
    if unknown:
        raise SystemExit('未知站点: %s' % ','.join(unknown))
    raw['hosts_per_site'] = hosts
    raw['endpoints_per_host'] = endpoints
    raw['endpoint_mtu'] = settle_mtu
    raw['access_mode'] = 'vxlan'
    raw['access_vtep'] = False
    raw.pop('te_paths', None)
    raw['fiti_topology'] = os.path.join(ROOT, 'configs', 'fiti.json')
    raw['networks'] = [
        {'id': index, 'vpn': 'l3', 'sites': list(sites)}
        for index in range(1, networks + 1)
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return path


def _table_count(switch, table):
    output = switch_cli(switch.thrift_port, ['table_dump %s' % table])
    return output.count('Action entry')


def _ping(host, source, target, count=3):
    output = host.cmd('ping -6 -c %d -W 2 -I %s %s' % (count, source, target))
    return '%d received' % count in output or ' 0% packet loss' in output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--networks', type=int, default=4096,
                        help='切片数量（默认 4096）')
    parser.add_argument('--sites', default='bj,sh',
                        help='参与站点，逗号分隔（默认 bj,sh）')
    parser.add_argument('--config', default='/tmp/opencode/parallel-scale.json',
                        help='生成的临时配置路径')
    parser.add_argument('--thrift-base', type=int, default=None)
    parser.add_argument('--log-dir', default=None)
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise SystemExit('容量验收会启动 Mininet，需要用 sudo 运行')

    sites = tuple(item.strip() for item in args.sites.split(',') if item.strip())
    config_path = _build_config(args.config, args.networks, sites,
                                hosts=1, endpoints=1, settle_mtu=1400)
    cfg = load_config(config_path)
    print('*** 容量配置：%d 个 L3 切片，成员站点 %s，每站点 1 台 PM/1 个端点'
          % (args.networks, ','.join(sites)))

    from fiti.log import ensure_log_dir
    from parallel.runtime import build_parallel_net
    from verify.runner import wait_for_ready

    log_dir = ensure_log_dir(args.log_dir)
    net = None
    failures = []
    started = time.time()
    try:
        net = build_parallel_net(cfg, thrift_base=args.thrift_base,
                                 log_dir=log_dir, install_rules=True)
        pending = wait_for_ready(net, cfg)
        if pending:
            print('*** 警告：%d 台主机的端点仍是 tentative' % len(pending))
        build_seconds = time.time() - started
        print('*** 拓扑就绪：%.1fs' % build_seconds)

        for abbr in sites:
            switch = net.get('E_%s' % abbr)
            vni = _table_count(switch, 'vni_map')
            sid = _table_count(switch, 'local_sid')
            fib = _table_count(switch, 'l3_fib')
            expected = args.networks
            print('*** E_%s: vni_map=%d local_sid=%d l3_fib=%d'
                  % (abbr, vni, sid, fib))
            if vni < expected:
                failures.append('E_%s vni_map 只有 %d 条（期望 %d）'
                                % (abbr, vni, expected))
            if sid < expected:
                failures.append('E_%s local_sid 只有 %d 条（期望 %d）'
                                % (abbr, sid, expected))
            if fib < 2 * expected:
                failures.append('E_%s l3_fib 只有 %d 条（期望 >= %d）'
                                % (abbr, fib, 2 * expected))

        src_host = net.get('cl_%s_1' % sites[0])
        samples = sorted({1, max(1, args.networks // 2), args.networks})
        for index in samples:
            network = cfg.network(index)
            source = cfg.cloud_address(network, cfg.site(sites[0]), 1, 1)
            target = cfg.cloud_address(network, cfg.site(sites[-1]), 1, 1)
            ok = _ping(src_host, source, target)
            print('*** 切片 %4d: %s -> %s %s'
                  % (index, source, target, 'PASS' if ok else 'FAIL'))
            if not ok:
                failures.append('切片 %d 不通' % index)
    finally:
        if net is not None:
            try:
                net.stop()
            except Exception:      # noqa: BLE001 - 清理失败不影响结论
                pass

    total = time.time() - started
    if failures:
        print('*** 容量验收失败（%.1fs）：' % total)
        for item in failures:
            print('  - %s' % item)
        return 1
    print('*** 容量验收通过：%d 切片，%.1fs' % (args.networks, total))
    return 0


if __name__ == '__main__':
    sys.exit(main())
