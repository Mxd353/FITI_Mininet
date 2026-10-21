/* -*- P4_16 -*- */
/*
 * 中关村主干网并行网络仿真数据平面（第一阶段）。
 *
 * 支持：
 *   - access / trunk / core 端口分类；
 *   - VLAN -> VRF（L3VPN）或 VLAN -> L2 域；
 *   - core 侧按业务 IPv6 前缀恢复 VRF；
 *   - L3 表按 (vrf_id, IPv6 prefix) 转发；
 *   - L2 表按 (VLAN, dst MAC) 转发；
 *   - S1/S2 上行 VLAN 过滤（S2 拒绝并行网络 VLAN）。
 *
 * 本阶段不包含 SRv6 / EVPN / VXLAN；这些在后续阶段加入。
 */

#include <core.p4>
#include <v1model.p4>

const bit<16> ETHERTYPE_VLAN = 0x8100;
const bit<16> ETHERTYPE_IPV6 = 0x86dd;
const bit<12> VLAN_NONE = 0;
const bit<12> PARALLEL_VLAN_MIN = 3501;
const bit<12> PARALLEL_VLAN_MAX = 3900;

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header vlan_t {
    bit<3>  pcp;
    bit<1>  dei;
    bit<12> vid;
    bit<16> etherType;
}

header ipv6_t {
    bit<4>   version;
    bit<8>   trafficClass;
    bit<20>  flowLabel;
    bit<16>  payloadLen;
    bit<8>   nextHdr;
    bit<8>   hopLimit;
    bit<128> srcAddr;
    bit<128> dstAddr;
}

struct headers_t {
    ethernet_t ethernet;
    vlan_t     vlan;
    ipv6_t     ipv6;
}

struct metadata_t {
    bit<9>  ingress_port;
    bit<12> vlan_id;
    bit<16> vrf_id;
    bit<1>  port_ok;
    bit<1>  core_port;
    bit<1>  l2_mode;
}

parser ParserImpl(packet_in packet,
                  out headers_t hdr,
                  inout metadata_t meta,
                  inout standard_metadata_t standard_metadata) {
    state start {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            ETHERTYPE_VLAN: parse_vlan;
            ETHERTYPE_IPV6: parse_ipv6;
            default: accept;
        }
    }

    state parse_vlan {
        packet.extract(hdr.vlan);
        transition select(hdr.vlan.etherType) {
            ETHERTYPE_IPV6: parse_ipv6;
            default: accept;
        }
    }

    state parse_ipv6 {
        packet.extract(hdr.ipv6);
        transition accept;
    }
}

control VerifyChecksumImpl(inout headers_t hdr,
                           inout metadata_t meta) {
    apply {
    }
}

control IngressImpl(inout headers_t hdr,
                    inout metadata_t meta,
                    inout standard_metadata_t standard_metadata) {
    action drop() {
        mark_to_drop(standard_metadata);
    }

    action set_access(bit<12> vlan) {
        if (hdr.vlan.isValid()) {
            drop();
        } else if (vlan == VLAN_NONE || vlan == 4095) {
            drop();
        } else {
            hdr.vlan.setValid();
            hdr.vlan.pcp = 0;
            hdr.vlan.dei = 0;
            hdr.vlan.vid = vlan;
            hdr.vlan.etherType = hdr.ethernet.etherType;
            hdr.ethernet.etherType = ETHERTYPE_VLAN;
            meta.vlan_id = vlan;
            meta.port_ok = 1;
        }
    }

    action set_trunk() {
        if (!hdr.vlan.isValid() ||
            hdr.vlan.vid == VLAN_NONE || hdr.vlan.vid == 4095) {
            drop();
        } else {
            meta.vlan_id = hdr.vlan.vid;
            meta.port_ok = 1;
        }
    }

    action set_core() {
        if (hdr.vlan.isValid()) {
            drop();
        } else {
            meta.vlan_id = 0;
            meta.core_port = 1;
            meta.port_ok = 1;
        }
    }

    table port_cfg {
        key = {
            standard_metadata.ingress_port: exact;
        }
        actions = {
            set_access;
            set_trunk;
            set_core;
            drop;
        }
        size = 256;
        default_action = drop;
    }

    action map_l3(bit<16> vrf_id) {
        meta.vrf_id = vrf_id;
        meta.l2_mode = 0;
    }

    action map_l2() {
        meta.vrf_id = 0;
        meta.l2_mode = 1;
    }

    table vlan_map {
        key = {
            meta.vlan_id: exact;
        }
        actions = {
            map_l3;
            map_l2;
            drop;
        }
        size = 4096;
        default_action = drop;
    }

    action map_vrf(bit<16> vrf_id) {
        meta.vrf_id = vrf_id;
        meta.l2_mode = 0;
    }

    table vrf_map {
        key = {
            hdr.ipv6.dstAddr: lpm;
        }
        actions = {
            map_vrf;
            drop;
        }
        size = 16384;
        default_action = drop;
    }

    action forward_l3(bit<9> port, bit<48> dmac, bit<12> out_vlan) {
        standard_metadata.egress_spec = port;
        hdr.ethernet.dstAddr = dmac;
        if (out_vlan == VLAN_NONE) {
            // Removing the VLAN header must restore the inner EtherType.
            // Otherwise the packet is untagged but Ethernet still says 0x8100,
            // and the receiver misinterprets the IPv6 header as a VLAN tag.
            hdr.vlan.setInvalid();
            hdr.ethernet.etherType = ETHERTYPE_IPV6;
        } else {
            hdr.vlan.setValid();
            hdr.vlan.pcp = 0;
            hdr.vlan.dei = 0;
            hdr.vlan.vid = out_vlan;
            hdr.vlan.etherType = ETHERTYPE_IPV6;
            hdr.ethernet.etherType = ETHERTYPE_VLAN;
        }
    }

    table l3_fib {
        key = {
            meta.vrf_id:      exact;
            hdr.ipv6.dstAddr: lpm;
        }
        actions = {
            forward_l3;
            drop;
        }
        size = 65536;
        default_action = drop;
    }

    action forward_l2(bit<9> port) {
        standard_metadata.egress_spec = port;
    }

    table l2_fib {
        key = {
            meta.vlan_id:        exact;
            hdr.ethernet.dstAddr: exact;
        }
        actions = {
            forward_l2;
            drop;
        }
        size = 65536;
        default_action = drop;
    }

    action allow_parallel() {
    }

    action deny_parallel() {
        if (meta.vlan_id >= PARALLEL_VLAN_MIN &&
            meta.vlan_id <= PARALLEL_VLAN_MAX) {
            drop();
        }
    }

    table uplink_filter {
        key = {
            standard_metadata.egress_spec: exact;
        }
        actions = {
            allow_parallel;
            deny_parallel;
        }
        size = 256;
        default_action = allow_parallel;
    }

    apply {
        meta.vlan_id = VLAN_NONE;
        meta.vrf_id = 0;
        meta.port_ok = 0;
        meta.core_port = 0;
        meta.l2_mode = 0;

        port_cfg.apply();
        if (meta.port_ok == 1) {
            if (meta.core_port == 1) {
                if (hdr.ipv6.isValid()) {
                    vrf_map.apply();
                }
            } else if (meta.vlan_id != VLAN_NONE) {
                vlan_map.apply();
            }

            if (meta.vrf_id != 0 && hdr.ipv6.isValid()) {
                if (hdr.ipv6.hopLimit <= 1) {
                    drop();
                } else {
                    hdr.ipv6.hopLimit = hdr.ipv6.hopLimit - 1;
                    l3_fib.apply();
                }
            } else if (meta.l2_mode == 1) {
                l2_fib.apply();
                if (standard_metadata.egress_spec != 0) {
                    uplink_filter.apply();
                }
            }
        }
    }
}

control EgressImpl(inout headers_t hdr,
                   inout metadata_t meta,
                   inout standard_metadata_t standard_metadata) {
    action set_access() {
        if (hdr.vlan.isValid()) {
            hdr.ethernet.etherType = hdr.vlan.etherType;
            hdr.vlan.setInvalid();
        }
    }

    action set_trunk() {
    }

    action set_core() {
        if (hdr.vlan.isValid()) {
            hdr.ethernet.etherType = hdr.vlan.etherType;
            hdr.vlan.setInvalid();
        }
    }

    action drop() {
        mark_to_drop(standard_metadata);
    }

    table egress_port {
        key = {
            standard_metadata.egress_port: exact;
        }
        actions = {
            set_access;
            set_trunk;
            set_core;
            drop;
        }
        size = 256;
        default_action = drop;
    }

    apply {
        egress_port.apply();
        if (meta.ingress_port == standard_metadata.egress_port) {
            mark_to_drop(standard_metadata);
        }
    }
}

control ComputeChecksumImpl(inout headers_t hdr,
                            inout metadata_t meta) {
    apply {
    }
}

control DeparserImpl(packet_out packet, in headers_t hdr) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.vlan);
        packet.emit(hdr.ipv6);
    }
}

V1Switch(
    ParserImpl(),
    VerifyChecksumImpl(),
    IngressImpl(),
    EgressImpl(),
    ComputeChecksumImpl(),
    DeparserImpl()
) main;
