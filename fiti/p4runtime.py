#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal P4Runtime client for the FITI bmv2 runtime.

The module deliberately keeps a small surface:

* push ``p4info.txt`` + bmv2 JSON to one switch;
* insert/delete/modify table entries by table/action/field names;
* update P4Runtime multicast groups.

The current ``p4runtime`` Python wheel uses protobuf generated code that
requires the pure-Python backend with newer protobuf releases.  Set the
environment variable before importing protobuf.
"""

import ipaddress
import os
import queue

os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')

_IMPORT_ERROR = None
try:
    import grpc
    from google.protobuf import text_format
    from p4.config.v1 import p4info_pb2
    from p4.v1 import p4runtime_pb2 as p4rt
    from p4.v1 import p4runtime_pb2_grpc
except ImportError as exc:  # pragma: no cover - depends on host install
    _IMPORT_ERROR = exc
    grpc = None
    text_format = None
    p4info_pb2 = None
    p4rt = None
    p4runtime_pb2_grpc = None


def _require_imports():
    if _IMPORT_ERROR is not None:
        raise RuntimeError(
            'P4Runtime 依赖缺失或 venv 不完整，请运行 make venv '
            '（或安装 grpcio / p4runtime / protobuf）'
        ) from _IMPORT_ERROR


def _short(name):
    return name.rsplit('.', 1)[-1]


def _pack(value, bitwidth):
    """Pack a Python value into P4Runtime's big-endian byte string."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        if '.' in value:
            return ipaddress.IPv4Address(value).packed
        try:
            return ipaddress.IPv6Address(value).packed
        except ValueError:
            parts = value.split(':')
            if len(parts) == 6:
                return bytes(int(part, 16) for part in parts)
            raise
    return int(value).to_bytes((int(bitwidth) + 7) // 8, 'big')


class P4InfoHelper:
    """Name -> id / width mapping from a p4info text file."""

    def __init__(self, path):
        _require_imports()
        self.path = path
        self.info = p4info_pb2.P4Info()
        with open(path, encoding='utf-8') as handle:
            text_format.Parse(handle.read(), self.info)
        self.tables = {}
        self.actions = {}
        self.actions_by_id = {}

        for table in self.info.tables:
            tname = table.preamble.alias or _short(table.preamble.name)
            fields = {}
            for field in table.match_fields:
                fields[_short(field.name)] = {
                    'id': field.id,
                    'type': field.match_type,
                    'bitwidth': field.bitwidth,
                }
            self.tables[tname] = {
                'id': table.preamble.id,
                'fields': fields,
                'action_ids': {ref.id for ref in table.action_refs},
            }

        for action in self.info.actions:
            aname = action.preamble.alias or _short(action.preamble.name)
            params = {}
            for param in action.params:
                params[_short(param.name)] = {
                    'id': param.id,
                    'bitwidth': param.bitwidth,
                }
            record = {'id': action.preamble.id,
                      'name': action.preamble.name,
                      'alias': action.preamble.alias,
                      'params': params}
            self.actions[aname] = record
            self.actions_by_id[action.preamble.id] = record

    def table(self, name):
        try:
            return self.tables[name]
        except KeyError:
            raise KeyError('P4Info 中没有表 %s，现有: %s'
                           % (name, ', '.join(sorted(self.tables))))

    def action(self, name, table_name=None):
        if table_name is not None:
            table = self.table(table_name)
            candidates = [self.actions_by_id[action_id]
                          for action_id in table['action_ids']
                          if action_id in self.actions_by_id]
            for candidate in candidates:
                if (candidate['name'] == name or
                        candidate['alias'] == name or
                        candidate['name'].endswith('.' + name)):
                    return candidate
            raise KeyError('%s 没有动作 %s' % (table_name, name))
        try:
            return self.actions[name]
        except KeyError:
            matches = [record for key, record in self.actions.items()
                       if key.endswith('.' + name) or key == name]
            if len(matches) == 1:
                return matches[0]
            raise KeyError('P4Info 中没有动作 %s，现有: %s'
                           % (name, ', '.join(sorted(self.actions))))


class P4RuntimeClient:
    """One P4Runtime connection to one bmv2 switch."""

    def __init__(self, address, device_id=0, election_low=1,
                 timeout=10.0):
        _require_imports()
        self.address = address
        self.device_id = int(device_id)
        self.election_id = p4rt.Uint128(high=0, low=int(election_low))
        self.timeout = timeout
        self.channel = None
        self.stub = None
        self.stream = None
        self._queue = queue.Queue()
        self._connected = False

    def _requests(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            yield item

    def connect(self):
        self.channel = grpc.insecure_channel(self.address)
        grpc.channel_ready_future(self.channel).result(timeout=self.timeout)
        self.stub = p4runtime_pb2_grpc.P4RuntimeStub(self.channel)
        self.stream = self.stub.StreamChannel(self._requests())
        arbitration = p4rt.MasterArbitrationUpdate(
            device_id=self.device_id,
            election_id=self.election_id)
        self._queue.put(p4rt.StreamMessageRequest(arbitration=arbitration))
        try:
            response = next(self.stream)
        except StopIteration:
            raise RuntimeError('%s: StreamChannel 已关闭' % self.address)
        if response.HasField('arbitration'):
            status = response.arbitration.status
            if getattr(status, 'code', 0) not in (0,):
                raise RuntimeError('%s: 主控选举失败: %s'
                                   % (self.address, status.message))
        self._connected = True
        return self

    def set_pipeline(self, helper, bmv2_json):
        request = p4rt.SetForwardingPipelineConfigRequest(
            device_id=self.device_id,
            election_id=self.election_id,
            action=p4rt.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT)
        request.config.p4info.CopyFrom(helper.info)
        with open(bmv2_json, 'rb') as handle:
            request.config.p4_device_config = handle.read()
        self.stub.SetForwardingPipelineConfig(request, timeout=self.timeout)

    def _build_entry(self, helper, table_name, action_name, matches,
                     params, with_action=True):
        table = helper.table(table_name)
        entry = p4rt.TableEntry(table_id=table['id'])
        for name, value in matches.items():
            field = table['fields'].get(name)
            if field is None:
                raise KeyError('%s 没有字段 %s' % (table_name, name))
            match = p4rt.FieldMatch(field_id=field['id'])
            ftype = field['type']
            if ftype == p4info_pb2.MatchField.LPM:
                if isinstance(value, tuple):
                    value, prefix_len = value
                else:
                    prefix_len = field['bitwidth']
                match.lpm.value = _pack(value, field['bitwidth'])
                match.lpm.prefix_len = int(prefix_len)
            elif ftype == p4info_pb2.MatchField.TERNARY:
                value, mask = value
                match.ternary.value = _pack(value, field['bitwidth'])
                match.ternary.mask = _pack(mask, field['bitwidth'])
            else:
                match.exact.value = _pack(value, field['bitwidth'])
            entry.match.append(match)
        if with_action:
            action = helper.action(action_name, table_name)
            entry.action.action.action_id = action['id']
            for name, value in params.items():
                param = action['params'].get(name)
                if param is None:
                    raise KeyError('%s 没有参数 %s' % (action_name, name))
                entry.action.action.params.append(p4rt.Action.Param(
                    param_id=param['id'],
                    value=_pack(value, param['bitwidth'])))
        return entry

    def _write(self, updates):
        request = p4rt.WriteRequest(
            device_id=self.device_id,
            election_id=self.election_id,
            updates=updates)
        self.stub.Write(request, timeout=self.timeout)

    def write_table(self, helper, table_name, action_name, matches, params,
                    update_type='INSERT'):
        entry = self._build_entry(helper, table_name, action_name, matches,
                                  params,
                                  with_action=(update_type != 'DELETE'))
        kind = {
            'INSERT': p4rt.Update.INSERT,
            'MODIFY': p4rt.Update.MODIFY,
            'DELETE': p4rt.Update.DELETE,
        }[update_type.upper()]
        self._write([p4rt.Update(type=kind,
                                 entity=p4rt.Entity(table_entry=entry))])

    def write_multicast(self, group_id, ports, update_type='INSERT'):
        replicas = [p4rt.Replica(egress_port=int(port), instance=index)
                    for index, port in enumerate(sorted(set(ports)))]
        group = p4rt.MulticastGroupEntry(multicast_group_id=int(group_id),
                                         replicas=replicas)
        entity = p4rt.Entity(
            packet_replication_engine_entry=p4rt.PacketReplicationEngineEntry(
                multicast_group_entry=group))
        kind = {
            'INSERT': p4rt.Update.INSERT,
            'MODIFY': p4rt.Update.MODIFY,
            'DELETE': p4rt.Update.DELETE,
        }[update_type.upper()]
        self._write([p4rt.Update(type=kind, entity=entity)])

    def read_table(self, helper, table_name):
        table = helper.table(table_name)
        entity = p4rt.Entity(table_entry=p4rt.TableEntry(table_id=table['id']))
        request = p4rt.ReadRequest(device_id=self.device_id, entities=[entity])
        return list(self.stub.Read(request, timeout=self.timeout))

    def close(self):
        if self.stream is not None:
            try:
                self.stream.cancel()
            except Exception:
                pass
            self.stream = None
        if self.channel is not None:
            self.channel.close()
            self.channel = None
        self._queue.put(None)
        self._connected = False
