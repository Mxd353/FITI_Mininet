#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static control plane for the parallel-network scenario.

It installs the data-plane equivalent of the PDF's EVPN/SRv6 routes:
L3VPN/L2VPN use static SRv6 SIDs (L2VPN can fall back to VXLAN),
and public access uses static FIX routes.  It is not a BGP/EVPN control-plane implementation.
"""

from __future__ import annotations


from fiti.p4 import _mc_handles, switch_cli
from parallel.config import ParallelConfig
from parallel.evpn import build_evpn_routes, validate_evpn_routes
from parallel.p4 import runtime_names
from parallel.topology import (a_name, c_name, cloud_name, e_name,
                               s1_name, s2_name)


def _peer_intf(intf):
    link = intf.link
    if link is None:
        return None
    return link.intf2 if link.intf1 is intf else link.intf1


def _switch_names(cfg):
    return tuple(name for site in cfg.sites
                 for name in (e_name(site), s1_name(site), s2_name(site)))


def _port_maps(net, cfg):
    switch_ports = {}
    for sw_name in _switch_names(cfg):
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


def _add_mc_group(commands, groups, group_id, nodes):
    """Append multicast group/node creation commands for one switch.

    ``nodes`` is a list of ``(replication_id, ports)``.  Multiple nodes may
    share the same physical port; bmv2 exposes the node replication id to
    the egress pipeline, which is how L2 BUM copies select remote SIDs.
    """
    nodes = [(int(rid), [int(port) for port in ports])
             for rid, ports in nodes if ports]
    if not nodes:
        return
    commands.append('mc_mgrp_create %d' % int(group_id))
    for rid, ports in nodes:
        commands.append('mc_node_create %d %s'
                        % (rid, ' '.join(str(port) for port in ports)))
    groups.append((int(group_id), len(nodes)))


def _install_switch_tables(net, cfg, names, switch_ports, macs, evpn_routes):
    commands = {name: [] for name in _switch_names(cfg)}
    groups = {name: [] for name in commands}

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
        for network in cfg.networks_for_site(site):
            if network.vpn == 'l3':
                _add(commands[e], names, 'vlan_map', 'map_l3',
                     [network.vlan], [network.id])
            else:
                _add(commands[e], names, 'vlan_map', 'map_l2',
                     [network.vlan])

        c_port = e_ports[c]
        c_dmac = _host_mac(net.get(c), e)

        # EVPN control-plane route objects drive the L3VPN table entries.
        # VRF and prefix routes come from IP Prefix routes; local SIDs come
        # from the same route objects so static and future BGP modes agree.
        l3_routes = evpn_routes.l3_routes()
        for route in sorted(l3_routes, key=lambda item: (item.vni, item.prefix)):
            _add(commands[e], names, 'vrf_map', 'map_vrf',
                 [route.prefix], [route.vni])

        for route in l3_routes:
            network = cfg.network(route.vni)
            target = cfg.site(route.origin)
            if target.abbr == site.abbr:
                _add(commands[e], names, 'l3_fib', 'forward_l3',
                     [network.id, route.prefix],
                     [e_ports[s1],
                      macs[site.abbr]['clouds'][network.id],
                      network.vlan])
            else:
                local_route = evpn_routes.l3_route(network.id, site.abbr)
                if local_route is None:
                    raise RuntimeError('站点 %s 缺少网络 %d 的本地 EVPN 路由'
                                       % (site.abbr, network.id))
                _add(commands[e], names, 'l3_fib', 'srv6_encap',
                     [network.id, route.prefix],
                     [local_route.sid, route.sid, c_dmac, c_port])

        for network in cfg.l3_networks_for_site(site):
            local_route = evpn_routes.l3_route(network.id, site.abbr)
            if local_route is None:
                raise RuntimeError('站点 %s 缺少网络 %d 的本地 EVPN 路由'
                                   % (site.abbr, network.id))
            local_sid = local_route.sid
            _add(commands[e], names, 'local_sid', 'srv6_decap',
                 ['%s/128' % local_sid],
                 [e_ports[s1], macs[site.abbr]['clouds'][network.id],
                  network.vlan])

            # The Linux core routers use underlay source addresses.  Replies
            # from cloud servers therefore have an underlay destination and
            # must be sent back to C, not dropped by the VRF FIB.
            # Keep local and remote underlay routes: Linux C routers may
            # use underlay source addresses in replies to cloud servers.
            for target in cfg.network_sites(network):
                _add(commands[e], names, 'l3_fib', 'forward_l3',
                     [network.id, cfg.underlay_network(target)],
                     [c_port, c_dmac, 0])
            _add(commands[e], names, 'l3_fib', 'forward_l3',
                 [network.id, '2001:255:0:100::/64'],
                 [c_port, c_dmac, 0])

        # L2VPN: SRv6 by default; VXLAN remains available as a fallback.
        # Known unicast is keyed by remote cloud MAC.  BUM traffic uses a
        # per-ingress multicast group; E ingress replicates to one node per
        # remote site, and the egress pipeline maps egress_rid to a SID.
        for network in cfg.l2_networks_for_site(site):
            local_route = evpn_routes.imet_route(network.id, site.abbr)
            if local_route is None:
                raise RuntimeError('站点 %s 缺少网络 %d 的 L2 EVPN 路由'
                                   % (site.abbr, network.id))
            remote_mac_routes = [
                (cfg.site(route.origin), route)
                for route in evpn_routes.mac_ip_routes(network.id)
                if route.origin != site.abbr
            ]
            remote_mac_routes.sort(key=lambda item: item[0].id)

            if cfg.l2_tunnel == 'srv6':
                _add(commands[e], names, 'local_sid', 'srv6_l2_decap',
                     ['%s/128' % local_route.sid], [e_ports[s1]])
                for remote_site, mac_route in remote_mac_routes:
                    remote_route = evpn_routes.imet_route(
                        network.id, remote_site.abbr)
                    if remote_route is None:
                        raise RuntimeError(
                            '站点 %s 缺少网络 %d 的远端 L2 路由'
                            % (remote_site.abbr, network.id))
                    _add(commands[e], names, 'l2_tunnel', 'srv6_l2_encap',
                         [network.vlan, mac_route.mac],
                         [local_route.sid, remote_route.sid,
                          c_dmac, c_port])

                group_id = network.vlan * 2
                nodes = [(remote_site.id, [c_port])
                         for remote_site, _ in remote_mac_routes]
                _add_mc_group(commands[e], groups[e], group_id, nodes)
                _add(commands[e], names, 'l2_bum', 'set_l2_bum',
                     [network.vlan, e_ports[s1]], [group_id])
                for remote_site, _ in remote_mac_routes:
                    remote_route = evpn_routes.imet_route(
                        network.id, remote_site.abbr)
                    _add(commands[e], names, 'l2_rep_sid',
                         'srv6_l2_encap_egress',
                         [network.vlan, remote_site.id],
                         [local_route.sid, remote_route.sid, c_dmac])
            else:
                src_ip = 'fd00:0:%d:1::2' % site.id
                for remote_site, mac_route in remote_mac_routes:
                    dst_ip = 'fd00:0:%d:1::2' % remote_site.id
                    _add(commands[e], names, 'l2_tunnel', 'vxlan_encap',
                         [network.vlan, mac_route.mac],
                         [src_ip, dst_ip, c_dmac, c_port, network.id])
                _add(commands[e], names, 'vxlan_map', 'vxlan_decap',
                     [network.id], [e_ports[s1]])

        # S1: one trunk port to the single site cloud host.  VLAN tags
        # distinguish all parallel-network slices on that host.
        _add_port(commands[s1], names, s1_ports[e], 'set_trunk', 'set_trunk')
        _add(commands[s1], names, 'uplink_filter', 'allow_parallel',
             [s1_ports[e]])
        cloud = cloud_name(site)
        cloud_port = s1_ports[cloud]
        _add_port(commands[s1], names, cloud_port, 'set_trunk', 'set_trunk')
        for network in cfg.networks_for_site(site):
            _add(commands[s1], names, 'vlan_map', 'map_l2',
                 [network.vlan])
            _add(commands[s1], names, 'l2_fib', 'forward_l2',
                 [network.vlan, macs[site.abbr]['clouds'][network.id]],
                 [cloud_port])
            _add(commands[s1], names, 'l2_fib', 'forward_l2',
                 [network.vlan, macs[site.abbr]['e_s1']], [s1_ports[e]])
            if network.vpn == 'l2':
                for remote_site in cfg.network_sites(network):
                    if remote_site.id == site.id:
                        continue
                    peer_mac = macs[remote_site.abbr]['clouds'][network.id]
                    _add(commands[s1], names, 'l2_fib', 'forward_l2',
                         [network.vlan, peer_mac], [s1_ports[e]])
                if cfg.l2_tunnel == 'srv6':
                    group_id = network.vlan * 2
                    _add_mc_group(commands[s1], groups[s1], group_id,
                                  [(0, [cloud_port, s1_ports[e]])])
                    _add_mc_group(commands[s1], groups[s1], group_id + 1,
                                  [(0, [cloud_port])])
                    _add(commands[s1], names, 'l2_bum', 'set_l2_bum',
                         [network.vlan, cloud_port], [group_id])
                    _add(commands[s1], names, 'l2_bum', 'set_l2_bum',
                         [network.vlan, s1_ports[e]], [group_id + 1])

        # S2: one trunk port to the same cloud host; parallel VLANs still
        # cannot cross the A uplink.
        _add_port(commands[s2], names, s2_ports[a], 'set_trunk', 'set_trunk')
        _add(commands[s2], names, 'uplink_filter', 'deny_parallel',
             [s2_ports[a]])
        cloud_s2_port = s2_ports[cloud]
        _add_port(commands[s2], names, cloud_s2_port, 'set_trunk', 'set_trunk')
        for network in cfg.networks_for_site(site):
            _add(commands[s2], names, 'vlan_map', 'map_l2',
                 [network.vlan])
            _add(commands[s2], names, 'l2_fib', 'forward_l2',
                 [network.vlan, macs[site.abbr]['clouds'][network.id]],
                 [cloud_s2_port])
            _add(commands[s2], names, 'l2_fib', 'forward_l2',
                 [network.vlan, _host_mac(net.get(a), s2)], [s2_ports[a]])
            if network.vpn == 'l2':
                for remote_site in cfg.network_sites(network):
                    if remote_site.id == site.id:
                        continue
                    peer_mac = macs[remote_site.abbr]['clouds'][network.id]
                    _add(commands[s2], names, 'l2_fib', 'forward_l2',
                         [network.vlan, peer_mac], [s2_ports[a]])
                if cfg.l2_tunnel == 'srv6':
                    group_id = network.vlan * 2
                    _add_mc_group(commands[s2], groups[s2], group_id,
                                  [(0, [cloud_s2_port])])
                    _add_mc_group(commands[s2], groups[s2], group_id + 1,
                                  [(0, [cloud_s2_port])])
                    _add(commands[s2], names, 'l2_bum', 'set_l2_bum',
                         [network.vlan, cloud_s2_port], [group_id])
                    _add(commands[s2], names, 'l2_bum', 'set_l2_bum',
                         [network.vlan, s2_ports[a]], [group_id + 1])

    for name, cmds in commands.items():
        thrift_port = net.get(name).thrift_port
        output = switch_cli(thrift_port, cmds)
        handles = _mc_handles(output)
        expected = sum(count for _, count in groups[name])
        if len(handles) != expected:
            raise RuntimeError(
                '%s: 创建了 %d 个 multicast node，解析到 %d 个 handle'
                % (name, expected, len(handles)))
        associations = []
        index = 0
        for group_id, count in groups[name]:
            for _ in range(count):
                associations.append('mc_node_associate %d %d'
                                    % (group_id, handles[index]))
                index += 1
        switch_cli(thrift_port, associations)



def _set_addr(host, intf, address):
    name = intf.name if hasattr(intf, 'name') else str(intf)
    host.cmd('ip -6 addr replace %s dev %s' % (address, name))


def _configure_cloud_interfaces(net, cfg):
    """Create one VLAN subinterface per slice on the single site cloud host."""
    for site in cfg.sites:
        cloud = net.get(cloud_name(site))
        parent = _host_intf(cloud, s1_name(site))
        for network in cfg.networks_for_site(site):
            ifname = cfg.vlan_ifname(network)
            cloud.cmd('ip link del %s 2>/dev/null || true' % ifname)
            cloud.cmd('ip link add link %s name %s type vlan id %d'
                      % (parent.name, ifname, network.vlan))
            cloud.cmd('ip link set dev %s address %s'
                      % (ifname, cfg.cloud_mac(network, site)))
            cloud.cmd('ip link set dev %s up' % ifname)


def _configure_addresses(net, cfg):
    """Assign IPv6 addresses explicitly; Mininet ifconfig ignores IPv6."""
    bj = cfg.site('bj')
    c_bj = net.get(c_name(bj))

    # Beijing-centred core links: C_bj -- all other site C routers.
    for site in cfg.sites:
        if site.abbr == bj.abbr:
            continue
        c_site = net.get(c_name(site))
        _set_addr(c_bj, _host_intf(c_bj, c_name(site)),
                  cfg.core_address(bj, site) + '/64')
        _set_addr(c_site, _host_intf(c_site, c_name(bj)),
                  cfg.core_address(site, bj) + '/64')

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
    _set_addr(net.get('pub_border'),
              _host_intf(net.get('pub_border'), 'fix'),
              '2001:255:0:100::2/64')
    _set_addr(net.get('fix'), _host_intf(net.get('fix'), 'pub_border'),
              '2001:255:0:100::2000/64')

    for site in cfg.sites:
        cloud = net.get(cloud_name(site))
        for network in cfg.networks_for_site(site):
            _set_addr(cloud, cfg.vlan_ifname(network),
                      cfg.cloud_address(network, site) + '/64')


def _core_next(net, cfg, site, target):
    """Return the next-hop address and interface for a core route.

    The core is a Beijing-centred star.  Beijing routes directly to the
    destination C router; every other site sends remote traffic via Beijing.
    """
    hub = cfg.site('bj')
    peer = target if site.abbr == hub.abbr else hub
    return (cfg.core_neighbor(site, peer),
            _host_intf(net.get(c_name(site)), c_name(peer)))


def _configure_core_neighbors(net, cfg):
    hub = cfg.site('bj')
    for site in cfg.sites:
        c = net.get(c_name(site))
        if site.abbr == hub.abbr:
            peers = [item for item in cfg.sites if item.id != site.id]
        else:
            peers = [hub]
        for peer in peers:
            intf = _host_intf(c, c_name(peer))
            addr = cfg.core_neighbor(site, peer)
            mac = _host_mac(net.get(c_name(peer)), c_name(site))
            c.cmd('ip -6 neigh replace %s lladdr %s dev %s nud permanent'
                  % (addr, mac, intf.name))


def _configure_routers(net, cfg, macs):
    """Configure C/A underlay routes and cloud default gateways."""
    _configure_addresses(net, cfg)
    _configure_core_neighbors(net, cfg)
    for site in cfg.sites:
        c = net.get(c_name(site))
        e = e_name(site)
        c_e_intf = _host_intf(c, e)
        fake_e = 'fd00:0:%d:1::2' % site.id
        fake_e_mac = macs[site.abbr]['e_c']
        c.cmd('ip -6 neigh replace %s lladdr %s dev %s nud permanent'
              % (fake_e, fake_e_mac, c_e_intf.name))
        for network in cfg.l3_networks_for_site(site):
            sid = cfg.srv6_sid(network, site)
            c.cmd('ip -6 neigh replace %s lladdr %s dev %s nud permanent'
                  % (sid, fake_e_mac, c_e_intf.name))
        for network in cfg.l2_networks_for_site(site):
            sid = cfg.l2_sid(network, site)
            c.cmd('ip -6 neigh replace %s lladdr %s dev %s nud permanent'
                  % (sid, fake_e_mac, c_e_intf.name))

        # Local L3 prefixes through the local E.
        for network in cfg.l3_networks_for_site(site):
            local_prefix = cfg.business_network(network, site)
            c.cmd('ip -6 route replace %s via %s dev %s'
                  % (local_prefix, fake_e, c_e_intf.name))

        # Remote L3 prefixes through the core.
        for network in cfg.l3_networks_for_site(site):
            for target in cfg.network_sites(network):
                if target.id == site.id:
                    continue
                next_hop, intf = _core_next(net, cfg, site, target)
                c.cmd('ip -6 route replace %s via %s dev %s'
                      % (cfg.business_network(network, target),
                         next_hop, intf.name))

        # Underlay /48 routes carry remote SRv6 SIDs and L2 VXLAN endpoints.
        remote_targets = {}
        for network in cfg.networks_for_site(site):
            for target in cfg.network_sites(network):
                if target.id != site.id:
                    remote_targets[target.id] = target
        for target in sorted(remote_targets.values(), key=lambda item: item.id):
            next_hop, intf = _core_next(net, cfg, site, target)
            c.cmd('ip -6 route replace %s via %s dev %s'
                  % (cfg.underlay_network(target), next_hop, intf.name))

        cloud = net.get(cloud_name(site))
        l3_interfaces = []
        for network in cfg.networks_for_site(site):
            ifname = cfg.vlan_ifname(network)
            if network.vpn == 'l3':
                gateway = cfg.cloud_gateway(network, site)
                e_mac = macs[site.abbr]['e_s1']
                cloud.cmd('ip -6 neigh replace %s lladdr %s dev %s nud permanent'
                          % (gateway, e_mac, ifname))
                table_id = 20000 + network.id
                cloud.cmd('ip -6 route replace %s dev %s table %d'
                          % (cfg.business_network(network, site),
                             ifname, table_id))
                cloud.cmd('ip -6 route replace default via %s dev %s src %s '
                          'table %d' % (gateway, ifname,
                                        cfg.cloud_address(network, site),
                                        table_id))
                # Select the per-slice routing table by source and by the
                # destination /48.  The source rule keeps a packet sourced
                # from one slice from leaking into another slice's table.
                cloud.cmd('ip -6 rule add from %s table %d priority %d'
                          % (cfg.cloud_address(network, site), table_id,
                             10000 + network.id))
                cloud.cmd('ip -6 rule add to 240a:a806:%d::/48 table %d '
                          'priority %d' % (network.id, table_id,
                                          20000 + network.id))
                l3_interfaces.append((network, gateway, ifname))
            else:
                table_id = 30000 + network.id
                cloud.cmd('ip -6 route replace %s dev %s table %d'
                          % (cfg.l2_network(network), ifname, table_id))
                cloud.cmd('ip -6 rule add from %s table %d priority %d'
                          % (cfg.cloud_address(network, site), table_id,
                             10000 + network.id))
                for remote_site in cfg.network_sites(network):
                    if remote_site.id == site.id:
                        continue
                    remote_addr = cfg.cloud_address(network, remote_site)
                    remote_mac = macs[remote_site.abbr]['clouds'][network.id]
                    cloud.cmd(
                        'ip -6 neigh replace %s lladdr %s dev %s nud permanent'
                        % (remote_addr, remote_mac, ifname))

        # A single main-table default keeps public/other traffic usable.
        # L3 destinations are overridden by the per-network rules above.
        if l3_interfaces:
            network, gateway, ifname = l3_interfaces[0]
            cloud.cmd('ip -6 route replace default via %s dev %s src %s'
                      % (gateway, ifname,
                         cfg.cloud_address(network, site)))


def _site_macs(net, cfg):
    return {
        site.abbr: {
            'e_s1': _switch_mac(net, e_name(site), s1_name(site)),
            'e_c': _switch_mac(net, e_name(site), c_name(site)),
            'clouds': {
                network.id: cfg.cloud_mac(network, site)
                for network in cfg.networks_for_site(site)
            },
        }
        for site in cfg.sites
    }


def _configure_public(net, cfg):
    """Configure the C2 public exchange and static FIX routes."""
    hub = cfg.site('bj')
    c_bj = net.get(c_name(hub))
    pub = net.get('pub_border')
    fix = net.get('fix')
    c_pub = _host_intf(c_bj, 'pub_border')
    pub_c = _host_intf(pub, c_name(hub))
    pub_fix = _host_intf(pub, 'fix')
    fix_pub = _host_intf(fix, 'pub_border')

    c_bj.cmd('ip -6 neigh replace 2001:255:0:100::1 lladdr %s dev %s nud permanent'
             % (_host_mac(pub, c_name(hub)), c_pub.name))
    pub.cmd('ip -6 neigh replace 2001:255:0:100::a806 lladdr %s dev %s nud permanent'
            % (_host_mac(c_bj, 'pub_border'), pub_c.name))
    pub.cmd('ip -6 neigh replace 2001:255:0:100::2000 lladdr %s dev %s nud permanent'
            % (_host_mac(fix, 'pub_border'), pub_fix.name))
    fix.cmd('ip -6 neigh replace 2001:255:0:100::2 lladdr %s dev %s nud permanent'
            % (_host_mac(pub, 'fix'), fix_pub.name))

    c_bj.cmd('ip -6 route replace 2001:255:0:100::2000/128 via '
             '2001:255:0:100::1 dev %s' % c_pub.name)
    for site in cfg.sites:
        if site.abbr == hub.abbr:
            continue
        c = net.get(c_name(site))
        intf = _host_intf(c, c_name(hub))
        next_hop = cfg.core_neighbor(site, hub)
        c.cmd('ip -6 route replace 2001:255:0:100::2000/128 via '
              '%s dev %s' % (next_hop, intf.name))
    pub.cmd('ip -6 route replace 2001:255:0:100::2000/128 dev %s'
            % pub_fix.name)
    for prefix in ('240a:a806::/32', '240a:a807::/32'):
        pub.cmd('ip -6 route replace %s via 2001:255:0:100::a806 dev %s'
                % (prefix, pub_c.name))
        fix.cmd('ip -6 route replace %s via 2001:255:0:100::2 dev %s'
                % (prefix, fix_pub.name))


def install_parallel_rules(net, cfg: ParallelConfig, json_path=None):
    names = runtime_names(json_path or cfg.p4_json)
    _configure_cloud_interfaces(net, cfg)
    switch_ports = _port_maps(net, cfg)
    macs = _site_macs(net, cfg)

    # First control-plane stage: build and validate the EVPN route model.
    # The same route objects will later be fed by an FRR/ExaBGP adapter.
    evpn_routes = build_evpn_routes(cfg, net)
    errors = validate_evpn_routes(evpn_routes)
    if errors:
        raise RuntimeError('EVPN 路由校验失败: %s' % '; '.join(errors))
    net.evpn_routes = evpn_routes

    _install_switch_tables(net, cfg, names, switch_ports, macs,
                           evpn_routes)
    _configure_routers(net, cfg, macs)
    _configure_public(net, cfg)
