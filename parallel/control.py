#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static control plane for the phase-1 parallel-network scenario.

The first implementation intentionally installs static table entries.  It is
the data-plane equivalent of the EVPN/SRv6 routes in the PDF; it is not a
BGP/EVPN control-plane implementation.
"""

from __future__ import annotations


from fiti.p4 import switch_cli
from parallel.config import ParallelConfig
from parallel.p4 import runtime_names
from parallel.topology import (a_name, c_name, cloud_name, e_name,
                               s1_name, s2_name)


def _peer_intf(intf):
    link = intf.link
    if link is None:
        return None
    return link.intf2 if link.intf1 is intf else link.intf1


def _port_maps(net):
    switch_ports = {}
    for sw_name in SWITCH_NAMES:
        switch = net.get(sw_name)
        ports = {}
        for port, intf in switch.intfs.items():
            port = int(port)
            if port == 0 or intf is None or intf.name == 'lo':
                continue
            peer = _peer_intf(intf)
            if peer is not None:
                ports[peer.node.name] = port
        switch_ports[sw_name] = ports
    return switch_ports


SWITCH_NAMES = ('E_bj', 'S1_bj', 'S2_bj', 'E_sh', 'S1_sh', 'S2_sh')


def _host_intf(host, peer_name):
    for intf in host.intfs.values():
        peer = _peer_intf(intf)
        if peer is not None and peer.node.name == peer_name:
            return intf
    raise RuntimeError('%s 没有连接到 %s 的接口' % (host.name, peer_name))


def _host_mac(host, peer_name):
    return _host_intf(host, peer_name).MAC()


def _switch_mac(net, switch_name_, peer_name):
    switch = net.get(switch_name_)
    for intf in switch.intfs.values():
        peer = _peer_intf(intf)
        if peer is not None and peer.node.name == peer_name:
            return intf.MAC()
    raise RuntimeError('%s 没有连接到 %s 的接口'
                       % (switch_name_, peer_name))


def _cmd(table, action, keys, params=None):
    words = ['table_add', table, action] + [str(item) for item in keys]
    words.append('=>')
    if params:
        words.extend(str(item) for item in params)
    return ' '.join(words)


def _action_name(names, table, action):
    table_name = names['tables'][table]
    prefix = table_name.rsplit('.', 1)[0]
    candidate = '%s.%s' % (prefix, action)
    if candidate in names['actions']:
        return candidate
    for name in names['actions']:
        if name.endswith('.' + action):
            return name
    raise RuntimeError('找不到 P4 动作 %s' % action)


def _add(cmds, names, table, action, keys, params=None):
    cmds.append(_cmd(names['tables'][table],
                     _action_name(names, table, action), keys, params))


def _add_port(cmds, names, port, action, egress_action, vlan=None):
    _add(cmds, names, 'port_cfg', action, [port],
         [vlan] if vlan is not None else None)
    _add(cmds, names, 'egress_port', egress_action, [port])


def _install_switch_tables(net, cfg, names, switch_ports, macs):
    commands = {name: [] for name in SWITCH_NAMES}

    for site in cfg.sites:
        e = e_name(site)
        s1 = s1_name(site)
        s2 = s2_name(site)
        c = c_name(site)
        a = a_name(site)
        e_ports = switch_ports[e]
        s1_ports = switch_ports[s1]
        s2_ports = switch_ports[s2]

        # E: C-facing core port + S1-facing trunk.
        _add_port(commands[e], names, e_ports[c], 'set_core', 'set_core')
        _add_port(commands[e], names, e_ports[s1], 'set_trunk', 'set_trunk')
        for network in cfg.l3_networks():
            _add(commands[e], names, 'vlan_map', 'map_l3',
                 [network.vlan], [network.id])
        for network in cfg.l2_networks():
            _add(commands[e], names, 'vlan_map', 'map_l2',
                 [network.vlan])

        # E core ingress: recover VRF from the business prefix.
        for network in cfg.l3_networks():
            for target in cfg.sites:
                _add(commands[e], names, 'vrf_map', 'map_vrf',
                     [cfg.business_network(network, target)], [network.id])

        # E L3 FIB: local site -> S1, remote site -> C.
        c_port = e_ports[c]
        c_dmac = _host_mac(net.get(c), e)
        for network in cfg.l3_networks():
            for target in cfg.sites:
                prefix = cfg.business_network(network, target)
                if target.abbr == site.abbr:
                    out_port = e_ports[s1]
                    dmac = macs[site.abbr]['clouds'][network.id]
                    out_vlan = network.vlan
                else:
                    out_port = c_port
                    dmac = c_dmac
                    out_vlan = 0
                _add(commands[e], names, 'l3_fib', 'forward_l3',
                     [network.id, prefix], [out_port, dmac, out_vlan])

            # The Linux core routers use underlay source addresses.  Replies
            # from cloud servers therefore have an underlay destination and
            # must be sent back to C, not dropped by the VRF FIB.
            for underlay in ('fd00:0:1::/48', 'fd00:0:2::/48'):
                _add(commands[e], names, 'l3_fib', 'forward_l3',
                     [network.id, underlay], [c_port, c_dmac, 0])

        # L2VPN: all frames in the VLAN are VXLAN-encapsulated to the peer E.
        for network in cfg.l2_networks():
            peer = next(item for item in cfg.sites
                        if item.id != site.id)
            src_ip = 'fd00:0:%d:1::2' % site.id
            dst_ip = 'fd00:0:%d:1::2' % peer.id
            _add(commands[e], names, 'l2_tunnel', 'vxlan_encap',
                 [network.vlan], [src_ip, dst_ip, c_dmac, c_port,
                                  network.id])
            _add(commands[e], names, 'vxlan_map', 'vxlan_decap',
                 [network.id], [e_ports[s1]])

        # S1: E-facing trunk, cloud access ports; parallel VLANs allowed.
        _add_port(commands[s1], names, s1_ports[e], 'set_trunk', 'set_trunk')
        _add(commands[s1], names, 'uplink_filter', 'allow_parallel',
             [s1_ports[e]])
        for network in cfg.networks:
            host = cloud_name(site, network)
            port = s1_ports[host]
            _add_port(commands[s1], names, port, 'set_access', 'set_access',
                      network.vlan)
            _add(commands[s1], names, 'vlan_map', 'map_l2',
                 [network.vlan])
            _add(commands[s1], names, 'l2_fib', 'forward_l2',
                 [network.vlan, macs[site.abbr]['clouds'][network.id]], [port])
            _add(commands[s1], names, 'l2_fib', 'forward_l2',
                 [network.vlan, macs[site.abbr]['e_s1']], [s1_ports[e]])
            if network.vpn == 'l2':
                peer = next(item for item in cfg.sites
                            if item.id != site.id)
                peer_mac = _host_mac(
                    net.get(cloud_name(peer, network)), s1_name(peer))
                _add(commands[s1], names, 'l2_fib', 'forward_l2',
                     [network.vlan, peer_mac], [s1_ports[e]])

        # S2: A-facing trunk, cloud access ports; parallel VLANs denied.
        _add_port(commands[s2], names, s2_ports[a], 'set_trunk', 'set_trunk')
        _add(commands[s2], names, 'uplink_filter', 'deny_parallel',
             [s2_ports[a]])
        for network in cfg.networks:
            host = cloud_name(site, network)
            port = s2_ports[host]
            _add_port(commands[s2], names, port, 'set_access', 'set_access',
                      network.vlan)
            _add(commands[s2], names, 'vlan_map', 'map_l2',
                 [network.vlan])
            _add(commands[s2], names, 'l2_fib', 'forward_l2',
                 [network.vlan, _host_mac(net.get(host), s2)], [port])
            _add(commands[s2], names, 'l2_fib', 'forward_l2',
                 [network.vlan, _host_mac(net.get(a), s2)], [s2_ports[a]])
            if network.vpn == 'l2':
                peer = next(item for item in cfg.sites
                            if item.id != site.id)
                peer_mac = _host_mac(
                    net.get(cloud_name(peer, network)), s2_name(peer))
                _add(commands[s2], names, 'l2_fib', 'forward_l2',
                     [network.vlan, peer_mac], [s2_ports[a]])

    for name, cmds in commands.items():
        switch_cli(net.get(name).thrift_port, cmds)



def _set_addr(host, intf, address):
    host.cmd('ip -6 addr replace %s dev %s' % (address, intf.name))


def _configure_addresses(net, cfg):
    """Assign IPv6 addresses explicitly; Mininet ifconfig ignores IPv6."""
    bj = cfg.site('bj')
    sh = cfg.site('sh')
    c_bj = net.get(c_name(bj))
    c_sh = net.get(c_name(sh))
    c_bj_sh = _host_intf(c_bj, c_name(sh))
    c_sh_bj = _host_intf(c_sh, c_name(bj))
    _set_addr(c_bj, c_bj_sh, 'fd00:0:1:0::1/64')
    _set_addr(c_sh, c_sh_bj, 'fd00:0:1:0::2/64')

    for site in cfg.sites:
        c = net.get(c_name(site))
        e = e_name(site)
        a = a_name(site)
        _set_addr(c, _host_intf(c, e), 'fd00:0:%d:1::1/64' % site.id)
        _set_addr(c, _host_intf(c, a), 'fd00:0:%d:2::1/64' % site.id)
        _set_addr(net.get(a), _host_intf(net.get(a), c_name(site)),
                  'fd00:0:%d:2::2/64' % site.id)
        _set_addr(net.get(a), _host_intf(net.get(a), s2_name(site)),
                  'fd00:0:%d:3::1/64' % site.id)

    _set_addr(c_bj, _host_intf(c_bj, 'pub_border'),
              '2001:255:0:100::a806/64')
    _set_addr(net.get('pub_border'), _host_intf(net.get('pub_border'), c_name(bj)),
              '2001:255:0:100::1/64')
    _set_addr(net.get('fix'), _host_intf(net.get('fix'), 'pub_border'),
              '2001:255:0:100::2000/64')

    for site in cfg.sites:
        for network in cfg.networks:
            cloud = net.get(cloud_name(site, network))
            _set_addr(cloud, _host_intf(cloud, s1_name(site)),
                      cfg.cloud_address(network, site) + '/64')
            if network.vpn == 'l3':
                _set_addr(cloud, _host_intf(cloud, s2_name(site)),
                          cfg.cloud_address(network, site, 0x11) + '/64')


def _configure_routers(net, cfg, macs):
    """Configure C/A underlay routes and cloud default gateways."""
    _configure_addresses(net, cfg)
    for site in cfg.sites:
        site_other = next(item for item in cfg.sites if item.id != site.id)
        c = net.get(c_name(site))
        c_other = net.get(c_name(site_other))
        e = e_name(site)
        c_e_intf = _host_intf(c, e)
        c_other_intf = _host_intf(c, c_name(site_other))
        fake_e = 'fd00:0:%d:1::2' % site.id
        fake_e_mac = macs[site.abbr]['e_c']
        c.cmd('ip -6 neigh replace %s lladdr %s dev %s nud permanent'
              % (fake_e, fake_e_mac, c_e_intf.name))

        # Local site prefixes through E; remote site prefixes through C.
        for network in cfg.l3_networks():
            local_prefix = cfg.business_network(network, site)
            remote_prefix = cfg.business_network(network, site_other)
            c.cmd('ip -6 route replace %s via %s dev %s'
                  % (local_prefix, fake_e, c_e_intf.name))
            if site.abbr == 'bj':
                c_other_addr = 'fd00:0:1:0::2'
            else:
                c_other_addr = 'fd00:0:1:0::1'
            c.cmd('ip -6 route replace %s via %s dev %s'
                  % (remote_prefix, c_other_addr, c_other_intf.name))

        # Underlay route for VXLAN outer IPv6 (L2VPN).
        remote_underlay = 'fd00:0:%d::/48' % site_other.id
        c.cmd('ip -6 route replace %s via %s dev %s'
              % (remote_underlay, c_other_addr, c_other_intf.name))

        for network in cfg.networks:
            cloud = net.get(cloud_name(site, network))
            s1 = s1_name(site)
            intf = _host_intf(cloud, s1)
            if network.vpn == 'l3':
                gateway = cfg.cloud_gateway(network, site)
                e_mac = macs[site.abbr]['e_s1']
                cloud.cmd('ip -6 neigh replace %s lladdr %s dev %s nud permanent'
                          % (gateway, e_mac, intf.name))
                cloud.cmd('ip -6 route replace default via %s dev %s'
                          % (gateway, intf.name))
            else:
                peer = next(item for item in cfg.sites
                            if item.id != site.id)
                remote_addr = cfg.cloud_address(network, peer)
                remote_mac = _host_mac(
                    net.get(cloud_name(peer, network)), s1_name(peer))
                cloud.cmd('ip -6 neigh replace %s lladdr %s dev %s nud permanent'
                          % (remote_addr, remote_mac, intf.name))


def _site_macs(net, cfg):
    return {
        site.abbr: {
            'e_s1': _switch_mac(net, e_name(site), s1_name(site)),
            'e_c': _switch_mac(net, e_name(site), c_name(site)),
            'clouds': {
                network.id: _host_mac(net.get(cloud_name(site, network)),
                                      s1_name(site))
                for network in cfg.networks
            },
        }
        for site in cfg.sites
    }


def install_parallel_rules(net, cfg: ParallelConfig, json_path=None):
    names = runtime_names(json_path or cfg.p4_json)
    switch_ports = _port_maps(net)
    macs = _site_macs(net, cfg)
    _install_switch_tables(net, cfg, names, switch_ports, macs)
    _configure_routers(net, cfg, macs)
