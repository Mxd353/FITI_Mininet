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
 * L3VPN 使用静态 SRv6 SID 做 H.Encaps / End.DT6 等价解封装；
 * L2VPN 使用静态 SRv6 SID 做 H.Encaps / End.DT2 等价解封装；
 * VXLAN 保留为 L2VPN 回退模式；EVPN/BGP 控制面后续加入。
 */

#include <core.p4>
#include <v1model.p4>

const bit<16> ETHERTYPE_VLAN = 0x8100;
const bit<16> ETHERTYPE_IPV6 = 0x86dd;
const bit<12> VLAN_NONE = 0;
const bit<12> PARALLEL_VLAN_MIN = 3501;
const bit<12> PARALLEL_VLAN_MAX = 3900;
const bit<8>  IPPROTO_SRH = 43;
const bit<8>  IPPROTO_IPV6 = 41;
const bit<8>  IPPROTO_ETHERNET = 143;
const bit<8>  SRH_ROUTING_TYPE = 4;
const bit<8>  IPPROTO_UDP = 17;
const bit<16> UDP_VXLAN = 4789;
const bit<8>  VXLAN_FLAGS = 0x08;

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

header srh_t {
    bit<8>  nextHdr;
    bit<8>  hdrExtLen;
    bit<8>  routingType;
    bit<8>  segmentsLeft;
    bit<8>  lastEntry;
    bit<8>  flags;
    bit<16> tag;
}

header srh_segment_t {
    bit<128> sid;
}

header udp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<16> length;
    bit<16> checksum;
}

header vxlan_t {
    bit<8>  flags;
    bit<24> reserved;
    bit<24> vni;
    bit<8>  reserved2;
}

struct headers_t {
    ethernet_t ethernet;
    vlan_t     vlan;
    ipv6_t     ipv6;
    srh_t      srh;
    srh_segment_t segment0;
    udp_t      udp;
    vxlan_t    vxlan;
    ethernet_t inner_ethernet;
    vlan_t     inner_vlan;
    ipv6_t     inner_ipv6;
}

struct metadata_t {
    bit<9>  ingress_port;
    bit<12> vlan_id;
    bit<16> vrf_id;
    bit<1>  port_ok;
    bit<1>  core_port;
    bit<1>  l2_mode;
    bit<1>  l2_bum;
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
        transition select(hdr.ipv6.nextHdr) {
            IPPROTO_SRH: parse_srh;
            IPPROTO_UDP: parse_udp;
            default: accept;
        }
    }

    state parse_srh {
        packet.extract(hdr.srh);
        packet.extract(hdr.segment0);
        transition select(hdr.srh.nextHdr) {
            IPPROTO_IPV6: parse_inner_ipv6;
            IPPROTO_ETHERNET: parse_inner_ethernet;
            default: accept;
        }
    }

    state parse_udp {
        packet.extract(hdr.udp);
        transition select(hdr.udp.dstPort) {
            UDP_VXLAN: parse_vxlan;
            default: accept;
        }
    }

    state parse_vxlan {
        packet.extract(hdr.vxlan);
        transition parse_inner_ethernet;
    }

    state parse_inner_ethernet {
        packet.extract(hdr.inner_ethernet);
        transition select(hdr.inner_ethernet.etherType) {
            ETHERTYPE_VLAN: parse_inner_vlan;
            ETHERTYPE_IPV6: parse_inner_ipv6;
            default: accept;
        }
    }

    state parse_inner_vlan {
        packet.extract(hdr.inner_vlan);
        transition select(hdr.inner_vlan.etherType) {
            ETHERTYPE_IPV6: parse_inner_ipv6;
            default: accept;
        }
    }

    state parse_inner_ipv6 {
        packet.extract(hdr.inner_ipv6);
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

    action srv6_encap(bit<128> src_sid, bit<128> dst_sid,
                      bit<48> dmac, bit<9> port) {
        hdr.inner_ipv6.setValid();
        hdr.inner_ipv6.version = hdr.ipv6.version;
        hdr.inner_ipv6.trafficClass = hdr.ipv6.trafficClass;
        hdr.inner_ipv6.flowLabel = hdr.ipv6.flowLabel;
        hdr.inner_ipv6.payloadLen = hdr.ipv6.payloadLen;
        hdr.inner_ipv6.nextHdr = hdr.ipv6.nextHdr;
        hdr.inner_ipv6.hopLimit = hdr.ipv6.hopLimit;
        hdr.inner_ipv6.srcAddr = hdr.ipv6.srcAddr;
        hdr.inner_ipv6.dstAddr = hdr.ipv6.dstAddr;

        hdr.ipv6.version = 6;
        hdr.ipv6.trafficClass = 0;
        hdr.ipv6.flowLabel = 0;
        hdr.ipv6.payloadLen = hdr.inner_ipv6.payloadLen + 64;
        hdr.ipv6.nextHdr = IPPROTO_SRH;
        hdr.ipv6.hopLimit = 64;
        hdr.ipv6.srcAddr = src_sid;
        hdr.ipv6.dstAddr = dst_sid;

        hdr.srh.setValid();
        hdr.srh.nextHdr = IPPROTO_IPV6;
        hdr.srh.hdrExtLen = 2;
        hdr.srh.routingType = SRH_ROUTING_TYPE;
        hdr.srh.segmentsLeft = 0;
        hdr.srh.lastEntry = 0;
        hdr.srh.flags = 0;
        hdr.srh.tag = 0;
        hdr.segment0.setValid();
        hdr.segment0.sid = dst_sid;

        hdr.vlan.setInvalid();
        hdr.ethernet.dstAddr = dmac;
        hdr.ethernet.etherType = ETHERTYPE_IPV6;
        standard_metadata.egress_spec = port;
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
            srv6_encap;
            drop;
        }
        size = 65536;
        default_action = drop;
    }

    action srv6_decap(bit<9> port, bit<48> dmac, bit<12> out_vlan) {
        hdr.ethernet.dstAddr = dmac;
        hdr.ethernet.etherType = ETHERTYPE_VLAN;
        hdr.vlan.setValid();
        hdr.vlan.pcp = 0;
        hdr.vlan.dei = 0;
        hdr.vlan.vid = out_vlan;
        hdr.vlan.etherType = ETHERTYPE_IPV6;
        if (hdr.inner_ipv6.isValid()) {
            hdr.ipv6.version = hdr.inner_ipv6.version;
            hdr.ipv6.trafficClass = hdr.inner_ipv6.trafficClass;
            hdr.ipv6.flowLabel = hdr.inner_ipv6.flowLabel;
            hdr.ipv6.payloadLen = hdr.inner_ipv6.payloadLen;
            hdr.ipv6.nextHdr = hdr.inner_ipv6.nextHdr;
            hdr.ipv6.hopLimit = hdr.inner_ipv6.hopLimit;
            hdr.ipv6.srcAddr = hdr.inner_ipv6.srcAddr;
            hdr.ipv6.dstAddr = hdr.inner_ipv6.dstAddr;
        } else {
            hdr.ipv6.setInvalid();
        }
        hdr.srh.setInvalid();
        hdr.segment0.setInvalid();
        hdr.inner_ipv6.setInvalid();
        standard_metadata.egress_spec = port;
    }

    action srv6_l2_encap(bit<128> src_sid, bit<128> dst_sid,
                         bit<48> dmac, bit<9> port) {
        bit<16> overhead = 78;
        hdr.inner_ethernet.setValid();
        hdr.inner_ethernet.dstAddr = hdr.ethernet.dstAddr;
        hdr.inner_ethernet.srcAddr = hdr.ethernet.srcAddr;
        hdr.inner_ethernet.etherType = hdr.ethernet.etherType;
        if (hdr.vlan.isValid()) {
            hdr.inner_vlan.setValid();
            hdr.inner_vlan.pcp = hdr.vlan.pcp;
            hdr.inner_vlan.dei = hdr.vlan.dei;
            hdr.inner_vlan.vid = hdr.vlan.vid;
            hdr.inner_vlan.etherType = hdr.vlan.etherType;
            overhead = 82;
        } else {
            hdr.inner_vlan.setInvalid();
        }
        hdr.inner_ipv6.setValid();
        hdr.inner_ipv6.version = hdr.ipv6.version;
        hdr.inner_ipv6.trafficClass = hdr.ipv6.trafficClass;
        hdr.inner_ipv6.flowLabel = hdr.ipv6.flowLabel;
        hdr.inner_ipv6.payloadLen = hdr.ipv6.payloadLen;
        hdr.inner_ipv6.nextHdr = hdr.ipv6.nextHdr;
        hdr.inner_ipv6.hopLimit = hdr.ipv6.hopLimit;
        hdr.inner_ipv6.srcAddr = hdr.ipv6.srcAddr;
        hdr.inner_ipv6.dstAddr = hdr.ipv6.dstAddr;

        hdr.ipv6.setValid();
        hdr.ipv6.version = 6;
        hdr.ipv6.trafficClass = 0;
        hdr.ipv6.flowLabel = 0;
        hdr.ipv6.payloadLen = overhead;
        if (hdr.inner_ipv6.isValid()) {
            hdr.ipv6.payloadLen = hdr.inner_ipv6.payloadLen + overhead;
        }
        hdr.ipv6.nextHdr = IPPROTO_SRH;
        hdr.ipv6.hopLimit = 64;
        hdr.ipv6.srcAddr = src_sid;
        hdr.ipv6.dstAddr = dst_sid;

        hdr.srh.setValid();
        hdr.srh.nextHdr = IPPROTO_ETHERNET;
        hdr.srh.hdrExtLen = 2;
        hdr.srh.routingType = SRH_ROUTING_TYPE;
        hdr.srh.segmentsLeft = 0;
        hdr.srh.lastEntry = 0;
        hdr.srh.flags = 0;
        hdr.srh.tag = 0;
        hdr.segment0.setValid();
        hdr.segment0.sid = dst_sid;

        hdr.vlan.setInvalid();
        hdr.ethernet.dstAddr = dmac;
        hdr.ethernet.etherType = ETHERTYPE_IPV6;
        standard_metadata.egress_spec = port;
    }

    action srv6_l2_decap(bit<9> port) {
        hdr.ethernet.dstAddr = hdr.inner_ethernet.dstAddr;
        hdr.ethernet.srcAddr = hdr.inner_ethernet.srcAddr;
        hdr.ethernet.etherType = hdr.inner_ethernet.etherType;
        if (hdr.inner_vlan.isValid()) {
            hdr.vlan.setValid();
            hdr.vlan.pcp = hdr.inner_vlan.pcp;
            hdr.vlan.dei = hdr.inner_vlan.dei;
            hdr.vlan.vid = hdr.inner_vlan.vid;
            hdr.vlan.etherType = hdr.inner_vlan.etherType;
        } else {
            hdr.vlan.setInvalid();
        }
        if (hdr.inner_ipv6.isValid()) {
            hdr.ipv6.version = hdr.inner_ipv6.version;
            hdr.ipv6.trafficClass = hdr.inner_ipv6.trafficClass;
            hdr.ipv6.flowLabel = hdr.inner_ipv6.flowLabel;
            hdr.ipv6.payloadLen = hdr.inner_ipv6.payloadLen;
            hdr.ipv6.nextHdr = hdr.inner_ipv6.nextHdr;
            hdr.ipv6.hopLimit = hdr.inner_ipv6.hopLimit;
            hdr.ipv6.srcAddr = hdr.inner_ipv6.srcAddr;
            hdr.ipv6.dstAddr = hdr.inner_ipv6.dstAddr;
        } else {
            hdr.ipv6.setInvalid();
        }
        hdr.srh.setInvalid();
        hdr.segment0.setInvalid();
        hdr.udp.setInvalid();
        hdr.vxlan.setInvalid();
        hdr.inner_ethernet.setInvalid();
        hdr.inner_vlan.setInvalid();
        hdr.inner_ipv6.setInvalid();
        standard_metadata.egress_spec = port;
    }

    table local_sid {
        key = {
            hdr.ipv6.dstAddr: lpm;
        }
        actions = {
            srv6_decap;
            srv6_l2_decap;
            drop;
        }
        size = 4096;
        default_action = drop;
    }

    action forward_l2(bit<9> port) {
        standard_metadata.egress_spec = port;
    }

    action l2_fib_nop() {
    }

    table l2_fib {
        key = {
            meta.vlan_id:        exact;
            hdr.ethernet.dstAddr: exact;
        }
        actions = {
            forward_l2;
            l2_fib_nop;
            drop;
        }
        size = 65536;
        default_action = l2_fib_nop;
    }

    action vxlan_encap(bit<128> src, bit<128> dst, bit<48> dmac,
                       bit<9> port, bit<24> vni) {
        bit<16> overhead = 70;
        hdr.inner_ethernet.setValid();
        hdr.inner_ethernet.dstAddr = hdr.ethernet.dstAddr;
        hdr.inner_ethernet.srcAddr = hdr.ethernet.srcAddr;
        hdr.inner_ethernet.etherType = hdr.ethernet.etherType;
        if (hdr.vlan.isValid()) {
            hdr.inner_vlan.setValid();
            hdr.inner_vlan.pcp = hdr.vlan.pcp;
            hdr.inner_vlan.dei = hdr.vlan.dei;
            hdr.inner_vlan.vid = hdr.vlan.vid;
            hdr.inner_vlan.etherType = hdr.vlan.etherType;
            overhead = 74;
        } else {
            hdr.inner_vlan.setInvalid();
        }
        hdr.inner_ipv6.setValid();
        hdr.inner_ipv6.version = hdr.ipv6.version;
        hdr.inner_ipv6.trafficClass = hdr.ipv6.trafficClass;
        hdr.inner_ipv6.flowLabel = hdr.ipv6.flowLabel;
        hdr.inner_ipv6.payloadLen = hdr.ipv6.payloadLen;
        hdr.inner_ipv6.nextHdr = hdr.ipv6.nextHdr;
        hdr.inner_ipv6.hopLimit = hdr.ipv6.hopLimit;
        hdr.inner_ipv6.srcAddr = hdr.ipv6.srcAddr;
        hdr.inner_ipv6.dstAddr = hdr.ipv6.dstAddr;

        hdr.ipv6.version = 6;
        hdr.ipv6.trafficClass = 0;
        hdr.ipv6.flowLabel = 0;
        hdr.ipv6.payloadLen = hdr.inner_ipv6.payloadLen + overhead;
        hdr.ipv6.nextHdr = IPPROTO_UDP;
        hdr.ipv6.hopLimit = 64;
        hdr.ipv6.srcAddr = src;
        hdr.ipv6.dstAddr = dst;

        hdr.udp.setValid();
        hdr.udp.srcPort = UDP_VXLAN;
        hdr.udp.dstPort = UDP_VXLAN;
        hdr.udp.length = hdr.ipv6.payloadLen;
        hdr.udp.checksum = 0;

        hdr.vxlan.setValid();
        hdr.vxlan.flags = VXLAN_FLAGS;
        hdr.vxlan.reserved = 0;
        hdr.vxlan.vni = vni;
        hdr.vxlan.reserved2 = 0;

        hdr.vlan.setInvalid();
        hdr.ethernet.dstAddr = dmac;
        hdr.ethernet.etherType = ETHERTYPE_IPV6;
        standard_metadata.egress_spec = port;
    }

    action vxlan_decap(bit<9> port) {
        hdr.ethernet.dstAddr = hdr.inner_ethernet.dstAddr;
        hdr.ethernet.srcAddr = hdr.inner_ethernet.srcAddr;
        hdr.ethernet.etherType = hdr.inner_ethernet.etherType;
        if (hdr.inner_vlan.isValid()) {
            hdr.vlan.setValid();
            hdr.vlan.pcp = hdr.inner_vlan.pcp;
            hdr.vlan.dei = hdr.inner_vlan.dei;
            hdr.vlan.vid = hdr.inner_vlan.vid;
            hdr.vlan.etherType = hdr.inner_vlan.etherType;
        } else {
            hdr.vlan.setInvalid();
        }
        if (hdr.inner_ipv6.isValid()) {
            hdr.ipv6.version = hdr.inner_ipv6.version;
            hdr.ipv6.trafficClass = hdr.inner_ipv6.trafficClass;
            hdr.ipv6.flowLabel = hdr.inner_ipv6.flowLabel;
            hdr.ipv6.payloadLen = hdr.inner_ipv6.payloadLen;
            hdr.ipv6.nextHdr = hdr.inner_ipv6.nextHdr;
            hdr.ipv6.hopLimit = hdr.inner_ipv6.hopLimit;
            hdr.ipv6.srcAddr = hdr.inner_ipv6.srcAddr;
            hdr.ipv6.dstAddr = hdr.inner_ipv6.dstAddr;
        } else {
            hdr.ipv6.setInvalid();
        }
        hdr.udp.setInvalid();
        hdr.vxlan.setInvalid();
        hdr.inner_ethernet.setInvalid();
        hdr.inner_vlan.setInvalid();
        hdr.inner_ipv6.setInvalid();
        standard_metadata.egress_spec = port;
    }

    action l2_tunnel_nop() {
    }

    action set_l2_bum(bit<16> group) {
        standard_metadata.mcast_grp = group;
        meta.l2_bum = 1;
    }

    table l2_tunnel {
        key = {
            meta.vlan_id:        exact;
            hdr.ethernet.dstAddr: exact;
        }
        actions = {
            vxlan_encap;
            srv6_l2_encap;
            l2_tunnel_nop;
        }
        size = 65536;
        default_action = l2_tunnel_nop;
    }

    table l2_bum {
        key = {
            meta.vlan_id:                 exact;
            standard_metadata.ingress_port: exact;
        }
        actions = {
            set_l2_bum;
            l2_tunnel_nop;
        }
        size = 1024;
        default_action = l2_tunnel_nop;
    }

    table vxlan_map {
        key = {
            hdr.vxlan.vni: exact;
        }
        actions = {
            vxlan_decap;
            drop;
        }
        size = 4096;
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
        meta.l2_bum = 0;
        meta.ingress_port = standard_metadata.ingress_port;

        port_cfg.apply();
        if (meta.port_ok == 1) {
            if (hdr.vxlan.isValid()) {
                // Outer VXLAN packet from the remote E router.
                vxlan_map.apply();
            } else if (hdr.srh.isValid()) {
                // SRv6 packet destined to a local SID.
                local_sid.apply();
            } else {
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
                    // Known unicast on E matches (VLAN, dst MAC); S1/S2
                    // fall through to l2_fib.  BUM/unknown traffic then
                    // uses a per-ingress multicast group.
                    l2_tunnel.apply();
                    if (standard_metadata.egress_spec == 0 &&
                        standard_metadata.mcast_grp == 0) {
                        l2_fib.apply();
                    }
                    if (standard_metadata.egress_spec == 0 &&
                        standard_metadata.mcast_grp == 0) {
                        l2_bum.apply();
                    }
                    if (standard_metadata.egress_spec != 0 ||
                        standard_metadata.mcast_grp != 0) {
                        uplink_filter.apply();
                    }
                }
            }
        }
    }
}

control EgressImpl(inout headers_t hdr,
                   inout metadata_t meta,
                   inout standard_metadata_t standard_metadata) {
    action srv6_l2_encap_egress(bit<128> src_sid, bit<128> dst_sid,
                                bit<48> dmac) {
        bit<16> overhead = 78;
        hdr.inner_ethernet.setValid();
        hdr.inner_ethernet.dstAddr = hdr.ethernet.dstAddr;
        hdr.inner_ethernet.srcAddr = hdr.ethernet.srcAddr;
        hdr.inner_ethernet.etherType = hdr.ethernet.etherType;
        if (hdr.vlan.isValid()) {
            hdr.inner_vlan.setValid();
            hdr.inner_vlan.pcp = hdr.vlan.pcp;
            hdr.inner_vlan.dei = hdr.vlan.dei;
            hdr.inner_vlan.vid = hdr.vlan.vid;
            hdr.inner_vlan.etherType = hdr.vlan.etherType;
            overhead = 82;
        } else {
            hdr.inner_vlan.setInvalid();
        }
        if (hdr.ipv6.isValid()) {
            hdr.inner_ipv6.setValid();
            hdr.inner_ipv6.version = hdr.ipv6.version;
            hdr.inner_ipv6.trafficClass = hdr.ipv6.trafficClass;
            hdr.inner_ipv6.flowLabel = hdr.ipv6.flowLabel;
            hdr.inner_ipv6.payloadLen = hdr.ipv6.payloadLen;
            hdr.inner_ipv6.nextHdr = hdr.ipv6.nextHdr;
            hdr.inner_ipv6.hopLimit = hdr.ipv6.hopLimit;
            hdr.inner_ipv6.srcAddr = hdr.ipv6.srcAddr;
            hdr.inner_ipv6.dstAddr = hdr.ipv6.dstAddr;
        } else {
            hdr.inner_ipv6.setInvalid();
        }

        hdr.ipv6.setValid();
        hdr.ipv6.version = 6;
        hdr.ipv6.trafficClass = 0;
        hdr.ipv6.flowLabel = 0;
        hdr.ipv6.payloadLen = overhead;
        if (hdr.inner_ipv6.isValid()) {
            hdr.ipv6.payloadLen = hdr.inner_ipv6.payloadLen + overhead;
        }
        hdr.ipv6.nextHdr = IPPROTO_SRH;
        hdr.ipv6.hopLimit = 64;
        hdr.ipv6.srcAddr = src_sid;
        hdr.ipv6.dstAddr = dst_sid;

        hdr.srh.setValid();
        hdr.srh.nextHdr = IPPROTO_ETHERNET;
        hdr.srh.hdrExtLen = 2;
        hdr.srh.routingType = SRH_ROUTING_TYPE;
        hdr.srh.segmentsLeft = 0;
        hdr.srh.lastEntry = 0;
        hdr.srh.flags = 0;
        hdr.srh.tag = 0;
        hdr.segment0.setValid();
        hdr.segment0.sid = dst_sid;

        hdr.vlan.setInvalid();
        hdr.ethernet.dstAddr = dmac;
        hdr.ethernet.etherType = ETHERTYPE_IPV6;
    }

    action l2_rep_nop() {
    }

    table l2_rep_sid {
        key = {
            meta.vlan_id:              exact;
            standard_metadata.egress_rid: exact;
        }
        actions = {
            srv6_l2_encap_egress;
            l2_rep_nop;
        }
        size = 4096;
        default_action = l2_rep_nop;
    }

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
        if (meta.l2_bum == 1) {
            l2_rep_sid.apply();
        }
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
        packet.emit(hdr.srh);
        packet.emit(hdr.segment0);
        packet.emit(hdr.udp);
        packet.emit(hdr.vxlan);
        packet.emit(hdr.inner_ethernet);
        packet.emit(hdr.inner_vlan);
        packet.emit(hdr.inner_ipv6);
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
