#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command-line client for the FITI runtime Unix socket."""

import argparse
import json
import socket
import sys

DEFAULT_SOCKET = '/tmp/fiti.sock'


def request(path, payload):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    sock.sendall((json.dumps(payload) + '\n').encode('utf-8'))
    data = b''
    while not data.endswith(b'\n'):
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    sock.close()
    return json.loads(data.decode('utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='FITI 切片控制客户端')
    parser.add_argument('--socket', default=DEFAULT_SOCKET)
    sub = parser.add_subparsers(dest='cmd', required=True)

    add = sub.add_parser('add-slice')
    add.add_argument('--id', required=True)
    add.add_argument('--name', default='')
    add.add_argument('--vlan', type=int, default=0)
    add.add_argument('--subnet', default='')
    add.add_argument('--hosts', required=True,
                     help='逗号分隔的节点 abbr，例如 bj,sh')
    add.add_argument('--paths', default=None,
                     help='JSON 路径列表，例如 '
                          '\'[{"src":"bj","dst":"sh","segments":["xa","sh"]}]\'')

    remove = sub.add_parser('remove-slice')
    remove.add_argument('--id', required=True)

    sub.add_parser('list')
    sub.add_parser('status')

    args = parser.parse_args(argv)
    if args.cmd == 'add-slice':
        payload = {
            'cmd': 'add_slice',
            'slice': {
                'id': args.id,
                'name': args.name or args.id,
                'vlan': args.vlan,
                'subnet': args.subnet,
                'hosts': [item.strip() for item in args.hosts.split(',')
                          if item.strip()],
                'paths': json.loads(args.paths) if args.paths else [],
            },
        }
    elif args.cmd == 'remove-slice':
        payload = {'cmd': 'remove_slice', 'id': args.id}
    elif args.cmd == 'list':
        payload = {'cmd': 'list_slices'}
    else:
        payload = {'cmd': 'status'}

    try:
        response = request(args.socket, payload)
    except Exception as exc:
        raise SystemExit('控制接口错误: %s' % exc)
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
