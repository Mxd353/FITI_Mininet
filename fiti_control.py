#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime slice controller for the FITI bmv2/P4Runtime testbed.

The switch/topology set is fixed.  A slice add/remove only changes:

* host namespaces and access ports on existing switches;
* VLAN/L2/multicast entries;
* SRv6 SID/encap/VRF entries.

Initial slices come from ``slices.json``.  Runtime additions are kept in
memory only and are not written back to the JSON file.
"""

import copy
import ipaddress
import re
import threading
from collections import OrderedDict

from mininet.log import warning

from fiti_p4 import (build_port_maps, build_rule_plan, compute_spanning_tree,
                     next_hop_on_tree, switch_name)
from fiti_slice import _host_ip_in_subnet
from p4_mininet import P4Host

try:
    from fiti_p4runtime import P4InfoHelper
except RuntimeError:
    P4InfoHelper = None

SLICE_ID_RE = re.compile(r'^[a-zA-Z0-9_]+$')
MAX_SEGMENTS = 16

TABLE_ADD_ORDER = ('port_cfg', 'egress_port', 'l2_fwd', 'srv6_mode',
                   'local_sid', 'sid_fib', 'vrf_fib', 'encap')
TABLE_DEL_ORDER = tuple(reversed(TABLE_ADD_ORDER))


class SliceError(RuntimeError):
    pass


def _state_key(table, matches):
    return (table, tuple(sorted((key, repr(value))
                                for key, value in matches.items())))


def _ipv6_for(meta):
    return meta['ipv6']


class SliceController:
    """Fixed-node runtime slice add/remove."""

    def __init__(self, net, data, cfg, helper, clients, tree=None,
                 path_mode='shortest'):
        self.net = net
        self.data = data
        self.cfg = cfg
        self.helper = helper
        self.clients = clients
        self.tree = tree or compute_spanning_tree(
            data, root=getattr(cfg.p4, 'spanning_root', None))
        self.path_mode = path_mode
        self.lock = threading.RLock()

        self.by_abbr = {node['abbr']: node for node in data['nodes']}
        self.switch_names = {switch_name(node['id']) for node in data['nodes']}
        self.slices = OrderedDict()
        self.host_plan = {}
        self.hosts = {}
        self.links = {}
        self.port_map = {}
        self.paths = {}
        self.state = {}
        self.mc_state = {}
        self._next_index = max([s.index for s in cfg.slices] or [0]) + 1

    # ------------------------------------------------------------------
    # validation / allocation
    # ------------------------------------------------------------------

    def _validate(self, sl):
        if not SLICE_ID_RE.match(sl.id):
            raise SliceError('切片 id 非法: %s' % sl.id)
        if sl.id in self.slices:
            raise SliceError('切片已存在: %s' % sl.id)
        if not 1 <= sl.vlan <= 4094:
            raise SliceError('VLAN 必须在 1-4094: %s' % sl.vlan)
        for existing in self.slices.values():
            if existing.vlan == sl.vlan:
                raise SliceError('VLAN %d 已被切片 %s 使用'
                                 % (sl.vlan, existing.id))
            try:
                if ipaddress.ip_network(sl.subnet).overlaps(
                        ipaddress.ip_network(existing.subnet)):
                    raise SliceError('子网 %s 与 %s 重叠'
                                     % (sl.subnet, existing.subnet))
            except ValueError:
                raise SliceError('子网非法: %s' % sl.subnet)
        if not sl.hosts:
            raise SliceError('切片 %s 没有主机' % sl.id)
        seen = set()
        for abbr in sl.hosts:
            if abbr not in self.by_abbr:
                raise SliceError('未知节点: %s' % abbr)
            if abbr in seen:
                raise SliceError('节点重复: %s' % abbr)
            seen.add(abbr)
            name = self._hostname(sl, abbr)
            if len(name) + 5 > 15:
                raise SliceError('主机名过长: %s' % name)
        for path in (sl.paths or []):
            src = path.get('src')
            dst = path.get('dst')
            if src not in sl.hosts or dst not in sl.hosts:
                raise SliceError('path 的 src/dst 必须是切片节点: %s -> %s'
                                 % (src, dst))
            segments = path.get('segments') or [dst]
            for abbr in segments:
                if abbr not in sl.hosts:
                    raise SliceError('path 路径节点不在切片中: %s' % abbr)
            if src in segments:
                raise SliceError('path 的 segments 不应包含源节点 %s' % src)
            if len(segments) > MAX_SEGMENTS:
                raise SliceError('path 超过 %d 段' % MAX_SEGMENTS)
            # local_sid only performs one SID lookup per ingress pass;
            # two consecutive segments on the same switch would need a
            # second local SID lookup (recirculation), which is not
            # implemented.
            seg_switches = [switch_name(int(self.by_abbr[abbr]['id']))
                            for abbr in segments]
            for left, right in zip(seg_switches, seg_switches[1:]):
                if left == right:
                    raise SliceError(
                        'path 连续两段位于同一交换机: %s, %s'
                        % (left, right))

    def _next_free_vlan(self):
        used = {sl.vlan for sl in self.slices.values()}
        for vlan in range(100, 4095):
            if vlan not in used:
                return vlan
        raise SliceError('没有空闲 VLAN')

    def _next_free_index(self):
        index = self._next_index
        self._next_index += 1
        return index

    def _hostname(self, sl, abbr):
        return '%s_%s' % (sl.id, abbr)

    def _host_meta(self, sl, abbr):
        node = self.by_abbr[abbr]
        node_id = int(node['id'])
        name = self._hostname(sl, abbr)
        ip = _host_ip_in_subnet(sl.subnet, node_id)
        ipv6 = 'fd00:%x::%x' % (sl.vlan, node_id)
        sid = 'fc00:%x:%x::1' % (node_id, sl.vlan)
        mac = '00:00:00:%02x:%02x:%02x' % (
            (sl.vlan >> 8) & 0xff, sl.vlan & 0xff, node_id)
        return {
            'host': name,
            'slice_id': sl.id,
            'slice_index': sl.index,
            'node_id': node_id,
            'node_name': node['name'],
            'abbr': abbr,
            'switch': switch_name(node_id),
            'vlan': sl.vlan,
            'ip': ip,
            'ipv6': ipv6,
            'sid': sid,
            'mac': mac,
        }

    # ------------------------------------------------------------------
    # host lifecycle
    # ------------------------------------------------------------------

    def _create_host(self, meta):
        name = meta['host']
        switch = self.net.get(meta['switch'])
        host = self.net.addHost(name, cls=P4Host)
        link = self.net.addLink(host, switch)
        try:
            host.config(ip=meta['ip'], mac=meta['mac'])
            iface = host.defaultIntf().name
            host.cmd('sysctl -qw net.ipv6.conf.%s.accept_dad=0' % iface)
            host.cmd('ip -6 addr replace %s/64 dev %s'
                     % (meta['ipv6'], iface))
            host.cmd('ip link set dev %s up' % iface)
            sw_intf = host.connectionsTo(switch)[0][1]
            port = switch.ports[sw_intf]
        except Exception:
            try:
                self.net.delLink(link)
                self.net.delHost(host)
            except Exception:
                pass
            raise
        self.hosts[name] = host
        self.links[name] = link
        self.port_map[name] = (switch.name, port)
        return host

    def _destroy_host(self, name):
        host = self.hosts.get(name)
        link = self.links.get(name)
        switch_port = self.port_map.get(name)
        if host is not None:
            try:
                host.cmd('ip link set dev %s down' % host.defaultIntf().name)
            except Exception:
                pass
        if switch_port is not None:
            switch = self.net.get(switch_port[0])
            if switch is not None and hasattr(switch, 'detach_port'):
                try:
                    switch.detach_port(switch_port[1])
                except Exception as exc:
                    warning('detach %s: %s\n' % (name, exc))
        if link is not None:
            try:
                self.net.delLink(link)
            except Exception as exc:
                warning('delLink %s: %s\n' % (name, exc))
        if host is not None:
            try:
                self.net.delHost(host)
            except Exception as exc:
                warning('delHost %s: %s\n' % (name, exc))
        self.hosts.pop(name, None)
        self.links.pop(name, None)
        self.port_map.pop(name, None)

    # ------------------------------------------------------------------
    # state planning
    # ------------------------------------------------------------------

    def _switch_mac(self, switch_name_, port):
        switch = self.net.get(switch_name_)
        intf = switch.intfs.get(port) if switch is not None else None
        mac = intf.MAC() if intf is not None else None
        return mac or '00:00:00:00:00:00'

    def _path_segments(self, sl, src_abbr, dst_abbr):
        configured = self.paths.get((sl.id, src_abbr, dst_abbr))
        if configured:
            return list(configured)
        return [dst_abbr]

    def _srv6_entries(self, desired, desired_mc, plans, switch_ports,
                      host_port):
        # plan entries are filled per switch in desired
        if not self.host_plan:
            return
        by_slice = {}
        for name, meta in self.host_plan.items():
            by_slice.setdefault(meta['slice_id'], []).append(meta)
        slice_by_id = {sl.id: sl for sl in self.slices.values()}

        for slice_id, metas in by_slice.items():
            sl = slice_by_id[slice_id]
            vlan = sl.vlan
            meta_by_abbr = {meta['abbr']: meta for meta in metas}

            # Enable switch SRv6 for this slice on every fixed switch.
            for switch in self.switch_names:
                matches = {'slice_id': vlan}
                desired.setdefault(switch, {})
                desired[switch][_state_key('srv6_mode', matches)] = (
                    matches, 'srv6_on', {})

            for meta in metas:
                switch = meta['switch']
                port = host_port[meta['host']][1]
                desired.setdefault(switch, {})
                # local SID -> End (final SL=0 falls into vrf_fib).
                # The slice/VLAN is part of the key so a host in one VLAN
                # cannot inject an SRH that targets another slice's SID.
                matches = {'slice_id': vlan,
                           'dstAddr': (meta['sid'], 128)}
                desired[switch][_state_key('local_sid', matches)] = (
                    matches, 'sid_end', {'slice_id': vlan})
                # local VRF entry -> host access port
                matches = {'slice_id': vlan,
                           'dstAddr': (meta['ipv6'], 128)}
                desired[switch][_state_key('vrf_fib', matches)] = (
                    matches, 'forward_inner',
                    {'port': port, 'dmac': meta['mac']})

            # sid_fib on every switch (except owner) for every SID
            for dst_meta in metas:
                for switch in self.switch_names:
                    if switch == dst_meta['switch']:
                        continue
                    nxt = next_hop_on_tree(self.tree, switch,
                                           dst_meta['switch'])
                    port = None
                    for candidate, peer in switch_ports[switch].items():
                        if peer == nxt:
                            port = candidate
                            break
                    if port is None:
                        continue
                    matches = {'slice_id': vlan,
                               'dstAddr': (dst_meta['sid'], 128)}
                    desired.setdefault(switch, {})
                    desired[switch][_state_key('sid_fib', matches)] = (
                        matches, 'sid_forward',
                        {'port': port,
                         'dmac': self._switch_mac(switch, port)})

            # encap on switches that host a source host
            for src_meta in metas:
                src_switch = src_meta['switch']
                for dst_meta in metas:
                    if dst_meta['switch'] == src_switch:
                        continue
                    nxt = next_hop_on_tree(self.tree, src_switch,
                                           dst_meta['switch'])
                    port = None
                    for candidate, peer in switch_ports[src_switch].items():
                        if peer == nxt:
                            port = candidate
                            break
                    if port is None:
                        continue

                    segments = self._path_segments(sl, src_meta['abbr'],
                                                   dst_meta['abbr'])
                    if segments[-1] != dst_meta['abbr']:
                        segments = segments + [dst_meta['abbr']]
                    sid_list = []
                    for abbr in segments:
                        other = meta_by_abbr.get(abbr)
                        if other is None:
                            raise SliceError('path 节点没有切片主机: %s' % abbr)
                        sid_list.append(other['sid'])
                    # P4 action expects segmentList order: last ... first.
                    seg_list = list(reversed(sid_list))
                    params = {
                        'count': len(seg_list),
                        'src': src_meta['sid'],
                        'dmac': self._switch_mac(src_switch, port),
                        'port': port,
                        'hop_limit': 64,
                    }
                    for index in range(MAX_SEGMENTS):
                        params['s%d' % index] = (
                            seg_list[index] if index < len(seg_list) else 0)
                    matches = {'slice_id': vlan,
                               'dstAddr': (dst_meta['ipv6'], 128)}
                    desired[src_switch][_state_key('encap', matches)] = (
                        matches, 'srv6_encap', params)

    def _build_desired(self):
        _port_peer, switch_ports, host_port = build_port_maps(
            self.net, self.data)
        desired = {}
        desired_mc = {}

        if self.host_plan:
            macs = {name: meta['mac']
                    for name, meta in self.host_plan.items()}
            plans = build_rule_plan(self.data, self.host_plan, switch_ports,
                                    host_port, self.tree, macs)
            for switch, plan in plans.items():
                entries = desired.setdefault(switch, {})
                groups = desired_mc.setdefault(switch, {})
                for port, vlan in plan.access_ports:
                    matches = {'ingress_port': port}
                    entries[_state_key('port_cfg', matches)] = (
                        matches, 'set_access', {'vlan': vlan})
                    matches = {'egress_port': port}
                    entries[_state_key('egress_port', matches)] = (
                        matches, 'set_access', {})
                for port in plan.trunk_ports:
                    matches = {'ingress_port': port}
                    entries[_state_key('port_cfg', matches)] = (
                        matches, 'set_trunk', {})
                    matches = {'egress_port': port}
                    entries[_state_key('egress_port', matches)] = (
                        matches, 'set_trunk', {})
                for vlan, mac, egress in plan.l2_entries:
                    matches = {'slice_id': vlan, 'dstAddr': mac}
                    entries[_state_key('l2_fwd', matches)] = (
                        matches, 'forward', {'port': egress})
                for vlan, ports in plan.groups.items():
                    groups[vlan] = tuple(ports)
            self._srv6_entries(desired, desired_mc, plans, switch_ports,
                               host_port)
        else:
            # No slices: only configure active trunk ports.
            for switch, ports in switch_ports.items():
                for port, peer in ports.items():
                    edge = tuple(sorted((switch, peer)))
                    if edge not in self.tree.edges:
                        continue
                    entries = desired.setdefault(switch, {})
                    matches = {'ingress_port': port}
                    entries[_state_key('port_cfg', matches)] = (
                        matches, 'set_trunk', {})
                    matches = {'egress_port': port}
                    entries[_state_key('egress_port', matches)] = (
                        matches, 'set_trunk', {})

        return desired, desired_mc

    # ------------------------------------------------------------------
    # P4Runtime apply
    # ------------------------------------------------------------------

    def _write(self, switch, kind, table, matches, action, params):
        client = self.clients[switch]
        client.write_table(self.helper, table, action, matches, params, kind)

    def _apply_tables(self, desired):
        for switch in sorted(desired, key=lambda name: int(name[1:])):
            old = self.state.get(switch, {})
            new = desired.get(switch, {})
            removed = [key for key in old if key not in new]
            added = [key for key in new if key not in old]
            changed = [key for key in new if key in old and
                       repr(old[key]) != repr(new[key])]

            for key in sorted(removed, key=lambda item: TABLE_DEL_ORDER.index(
                    item[0]) if item[0] in TABLE_DEL_ORDER else -1):
                matches, action, params = old[key]
                self._write(switch, 'DELETE', key[0], matches, action, params)
            for key in sorted(changed, key=lambda item: TABLE_DEL_ORDER.index(
                    item[0]) if item[0] in TABLE_DEL_ORDER else -1):
                matches, action, params = old[key]
                self._write(switch, 'DELETE', key[0], matches, action, params)
            for key in sorted(added, key=lambda item: TABLE_ADD_ORDER.index(
                    item[0]) if item[0] in TABLE_ADD_ORDER else -1):
                matches, action, params = new[key]
                self._write(switch, 'INSERT', key[0], matches, action, params)
            for key in sorted(changed, key=lambda item: TABLE_ADD_ORDER.index(
                    item[0]) if item[0] in TABLE_ADD_ORDER else -1):
                matches, action, params = new[key]
                self._write(switch, 'INSERT', key[0], matches, action, params)
            self.state[switch] = copy.deepcopy(new)

    def _apply_multicast(self, desired_mc):
        for switch in sorted(desired_mc, key=lambda name: int(name[1:])):
            old = self.mc_state.get(switch, {})
            new = desired_mc.get(switch, {})
            for vlan in sorted(set(old) - set(new)):
                self.clients[switch].write_multicast(vlan, [], 'DELETE')
            for vlan in sorted(new):
                ports = new[vlan]
                if vlan not in old:
                    self.clients[switch].write_multicast(vlan, ports, 'INSERT')
                elif old[vlan] != ports:
                    self.clients[switch].write_multicast(vlan, ports, 'MODIFY')
            self.mc_state[switch] = copy.deepcopy(new)

    def _apply_desired(self):
        desired, desired_mc = self._build_desired()
        self._apply_tables(desired)
        self._apply_multicast(desired_mc)
        return desired, desired_mc

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def add_slice(self, sl, apply=True):
        with self.lock:
            if sl.index <= 0:
                sl.index = self._next_free_index()
            if sl.vlan <= 0:
                sl.vlan = self._next_free_vlan()
            if not sl.subnet:
                sl.subnet = '10.%d.0.0/24' % sl.index
            self._validate(sl)

            metas = {self._hostname(sl, abbr): self._host_meta(sl, abbr)
                     for abbr in sl.hosts}
            self.slices[sl.id] = sl
            for path in (sl.paths or []):
                segments = path.get('segments') or [path['dst']]
                if segments[-1] != path['dst']:
                    segments = segments + [path['dst']]
                self.paths[(sl.id, path['src'], path['dst'])] = segments
            self.host_plan.update(metas)

            old_state = copy.deepcopy(self.state)
            old_mc = copy.deepcopy(self.mc_state)
            try:
                for meta in metas.values():
                    self._create_host(meta)
                if apply:
                    self._apply_desired()
            except Exception:
                for name in metas:
                    self._destroy_host(name)
                for name in metas:
                    self.host_plan.pop(name, None)
                self.slices.pop(sl.id, None)
                self.state = old_state
                self.mc_state = old_mc
                raise
            return self.slice_info(sl.id)

    def remove_slice(self, slice_id, apply=True):
        with self.lock:
            if slice_id not in self.slices:
                raise SliceError('切片不存在: %s' % slice_id)
            sl = self.slices[slice_id]
            names = [name for name, meta in self.host_plan.items()
                     if meta['slice_id'] == slice_id]
            old_state = copy.deepcopy(self.state)
            old_mc = copy.deepcopy(self.mc_state)
            old_metas = {name: self.host_plan[name] for name in names}
            for name in names:
                self.host_plan.pop(name, None)
            self.slices.pop(slice_id, None)
            for key in [key for key in self.paths
                        if key[0] == slice_id]:
                self.paths.pop(key, None)
            try:
                if apply:
                    self._apply_desired()
            except Exception:
                self.slices[slice_id] = sl
                self.host_plan.update(old_metas)
                self.state = old_state
                self.mc_state = old_mc
                raise
            for name in names:
                self._destroy_host(name)
            return {'id': slice_id, 'hosts': names}

    def apply_initial(self):
        with self.lock:
            for sl in self.cfg.slices:
                self.add_slice(sl, apply=False)
            self._apply_desired()

    def list_slices(self):
        return [self.slice_info(slice_id) for slice_id in self.slices]

    def slice_info(self, slice_id):
        sl = self.slices[slice_id]
        hosts = [name for name, meta in self.host_plan.items()
                 if meta['slice_id'] == slice_id]
        return {'id': sl.id, 'name': sl.name, 'vlan': sl.vlan,
                'subnet': sl.subnet, 'hosts': hosts}

    def status(self):
        return {
            'switches': len(self.switch_names),
            'slices': self.list_slices(),
            'hosts': len(self.host_plan),
            'p4runtime_clients': len(self.clients),
        }
