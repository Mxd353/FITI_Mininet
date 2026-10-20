#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime FITI testbed with P4Runtime and dynamic slices.

Start the fixed 41-node topology, push the P4 pipeline to every
``simple_switch_grpc``, apply the initial slices from ``configs/slices.json``, then
serve ``add_slice`` / ``remove_slice`` over a Unix socket.
"""

import argparse
import json
import os
import socketserver
import sys
import threading
import time
import traceback

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import info, setLogLevel
from mininet.net import Mininet

from fiti.control import SliceController, SliceError
from fiti.log import Progress, ensure_log_dir, quiet_mininet_console
from fiti.p4 import (DEFAULT_GRPC_BASE, DEFAULT_P4_JSON,
                     choose_port_base, choose_thrift_base, compile_p4,
                     make_runtime_switch_factory, switch_name)
from fiti.p4runtime import P4InfoHelper, P4RuntimeClient
from fiti.slice import Slice, load_slice_config
from fiti.topo import build_topology, load_topology
from fiti.p4_mininet import P4Host
from fiti.paths import DEFAULT_PCAP_DIR, DEFAULT_SLICES_FILE

DEFAULT_SOCKET = '/tmp/fiti.sock'


class SliceRequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline()
        if not line:
            return
        try:
            request = json.loads(line.decode('utf-8'))
            response = self.server.dispatch(request)
        except Exception as exc:
            response = {'ok': False, 'error': str(exc),
                        'traceback': traceback.format_exc()}
        self.wfile.write((json.dumps(response) + '\n').encode('utf-8'))


class SliceSocketServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, controller):
        self.controller = controller
        if os.path.exists(path):
            os.unlink(path)
        super(SliceSocketServer, self).__init__(path, SliceRequestHandler)
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
        self.path = path

    def dispatch(self, request):
        cmd = request.get('cmd')
        if cmd == 'add_slice':
            raw = request.get('slice') or request
            sl = Slice(id=str(raw.get('id', '')),
                       name=str(raw.get('name', raw.get('id', ''))),
                       vlan=int(raw.get('vlan', 0) or 0),
                       subnet=str(raw.get('subnet', '') or ''),
                       hosts=list(raw.get('hosts') or []),
                       index=int(raw.get('index', 0) or 0),
                       paths=list(raw.get('paths') or []))
            return {'ok': True, 'slice': self.controller.add_slice(sl)}
        if cmd == 'remove_slice':
            return {'ok': True,
                    'slice': self.controller.remove_slice(
                        str(request.get('id') or request.get('slice_id')))}
        if cmd == 'list_slices':
            return {'ok': True, 'slices': self.controller.list_slices()}
        if cmd == 'status':
            return {'ok': True, 'status': self.controller.status()}
        raise SliceError('未知命令: %s' % cmd)


def build_runtime_net(cfg, data, json_path, p4info_path, thrift_base,
                      grpc_base, p4_log_level='info', pcap_dump=None,
                      log_dir=None):
    topo = build_topology(data=data, with_hosts=False)
    factory = make_runtime_switch_factory(
        json_path, thrift_base, grpc_base,
        p4_log_level=p4_log_level, pcap_dump=pcap_dump, log_dir=log_dir)
    net = Mininet(topo=topo, switch=factory, host=P4Host,
                  controller=None, link=TCLink,
                  autoSetMacs=True, build=False)
    net.p4_json = os.path.abspath(json_path)
    net.p4info_path = os.path.abspath(p4info_path)
    net.thrift_base = thrift_base
    net.grpc_base = grpc_base
    try:
        with quiet_mininet_console(log_dir, allow_build_lists=True,
                                   allow_names=set(topo.switches())):
            net.build()
        with quiet_mininet_console(log_dir):
            with Progress('*** 启动 bmv2-grpc 交换机',
                          done='*** %d 台 bmv2-grpc 交换机已启动'
                               % len(data['nodes'])):
                net.start()
    except Exception:
        net.stop()
        raise
    return net


def connect_p4runtime(net, data, helper, json_path, grpc_base):
    clients = {}
    try:
        for node in data['nodes']:
            node_id = int(node['id'])
            name = switch_name(node_id)
            address = '127.0.0.1:%d' % (grpc_base + node_id)
            client = P4RuntimeClient(address, device_id=node_id).connect()
            client.set_pipeline(helper, json_path)
            clients[name] = client
            info('*** P4Runtime 已连接 %s (%s)\n' % (name, address))
    except Exception:
        for client in clients.values():
            try:
                client.close()
            except Exception:
                pass
        raise
    return clients


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='FITI bmv2 + P4Runtime runtime with dynamic slices')
    parser.add_argument('--slices', default=DEFAULT_SLICES_FILE)
    parser.add_argument('--p4-json', default=DEFAULT_P4_JSON)
    parser.add_argument('--p4info',
                        default=os.path.splitext(DEFAULT_P4_JSON)[0] + '.p4info.txt')
    parser.add_argument('--thrift-base', type=int, default=None)
    parser.add_argument('--grpc-base', type=int, default=None)
    parser.add_argument('--socket', default=None)
    parser.add_argument('--no-cli', action='store_true')
    parser.add_argument('--pcap', action='store_true')
    parser.add_argument('--pcap-dir', default=DEFAULT_PCAP_DIR)
    parser.add_argument('--p4-log-level', default='info',
                        choices=('trace', 'debug', 'info', 'warn', 'error', 'off'))
    parser.add_argument('--log-dir', default=None)
    parser.add_argument('--log', default='info',
                        choices=('debug', 'info', 'warning', 'error', 'critical'))
    args = parser.parse_args(argv)
    setLogLevel(args.log)

    if os.geteuid() != 0:
        raise SystemExit('Mininet/P4Runtime 需要 root 权限，请使用 sudo 运行')

    try:
        cfg = load_slice_config(args.slices)
    except ValueError as exc:
        raise SystemExit('配置错误: %s' % exc)
    data = load_topology(cfg.topo_file)

    with open(args.slices, encoding='utf-8') as handle:
        raw_cfg = json.load(handle)
    p4runtime_cfg = raw_cfg.get('p4runtime', {}) or {}
    control_cfg = raw_cfg.get('control', {}) or {}
    srv6_cfg = raw_cfg.get('srv6', {}) or {}

    log_dir = args.log_dir or cfg.p4.log_dir
    ensure_log_dir(log_dir)
    json_path = compile_p4(out=args.p4_json, p4info_out=args.p4info)
    p4info_path = os.path.abspath(args.p4info)
    thrift_base = (args.thrift_base or p4runtime_cfg.get('thrift_base')
                   or choose_thrift_base(data))
    grpc_base = (args.grpc_base or p4runtime_cfg.get('grpc_base')
                 or choose_port_base(data, DEFAULT_GRPC_BASE,
                                     label='gRPC '))
    pcap_dump = None
    if args.pcap:
        pcap_dump = os.path.abspath(args.pcap_dir)
        os.makedirs(pcap_dump, exist_ok=True)

    try:
        helper = P4InfoHelper(p4info_path)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    net = None
    server = None
    clients = {}
    try:
        net = build_runtime_net(cfg, data, json_path, p4info_path,
                                thrift_base, grpc_base,
                                p4_log_level=args.p4_log_level,
                                pcap_dump=pcap_dump, log_dir=log_dir)
        clients = connect_p4runtime(net, data, helper, json_path, grpc_base)
        controller = SliceController(
            net, data, cfg, helper, clients,
            path_mode=srv6_cfg.get('path_mode', 'shortest'))
        with Progress('*** 应用初始切片', done='*** 初始切片已应用'):
            controller.apply_initial()
        net.slice_controller = controller

        socket_path = (args.socket or control_cfg.get('socket')
                       or DEFAULT_SOCKET)
        server = SliceSocketServer(socket_path, controller)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        info('*** 控制接口: %s\n' % socket_path)
        info('*** 切片: %s\n' % ', '.join(controller.slices))

        if args.no_cli:
            while True:
                time.sleep(1)
        else:
            CLI(net)
    except KeyboardInterrupt:
        print('\n*** 已中断', file=sys.stderr)
        return 130
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        for client in clients.values():
            try:
                client.close()
            except Exception:
                pass
        if net is not None:
            net.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
