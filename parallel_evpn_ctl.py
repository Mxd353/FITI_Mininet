#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unix-socket client for the parallel-network EVPN route agent."""

from __future__ import annotations

import argparse
import json
import socket
import sys

DEFAULT_SOCKET = '/tmp/parallel_evpn.sock'


def request(path, payload):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    sock.sendall((json.dumps(payload, ensure_ascii=False) + '\n')
                 .encode('utf-8'))
    data = b''
    while not data.endswith(b'\n'):
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    sock.close()
    return json.loads(data.decode('utf-8'))


def _load_route(text, path):
    if path:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    if not text:
        raise SystemExit('advertise/withdraw 需要 --route 或 --file')
    return json.loads(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description='并行网络 EVPN 动态路由客户端')
    parser.add_argument('--socket', default=DEFAULT_SOCKET)
    sub = parser.add_subparsers(dest='cmd', required=True)

    add = sub.add_parser('advertise')
    add.add_argument('--route', default=None,
                     help='单条路由 JSON')
    add.add_argument('--file', default=None,
                     help='从文件读取单条路由 JSON')

    remove = sub.add_parser('withdraw')
    remove.add_argument('--route', default=None,
                        help='单条路由 JSON')
    remove.add_argument('--file', default=None,
                        help='从文件读取单条路由 JSON')

    sub.add_parser('dump')
    sub.add_parser('status')

    args = parser.parse_args(argv)
    if args.cmd in ('advertise', 'withdraw'):
        route = _load_route(args.route, args.file)
        payload = {'cmd': args.cmd, 'route': route}
    else:
        payload = {'cmd': args.cmd}

    try:
        response = request(args.socket, payload)
    except Exception as exc:
        raise SystemExit('EVPN agent 控制接口错误: %s' % exc)
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
