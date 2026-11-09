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
const bit<12> QINQ_OUTER_VLAN = 3500;
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
    vlan_t     vlan_inner;
    ipv6_t     ipv6;
    srh_t      srh;
    srh_segment_t segment0;
    srh_segment_t segment1;
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
    bit<1>  transit;
    bit<1>  bad_srh;
    bit<24> vni;
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
            ETHERTYPE_VLAN: parse_vlan_inner;
            ETHERTYPE_IPV6: parse_ipv6;
            default: accept;
        }
    }

    state parse_vlan_inner {
        packet.extract(hdr.vlan_inner);
        transition select(hdr.vlan_inner.etherType) {
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
        // hdrExtLen = 2 -> 1 段（BE）；4 -> 2 段（带一个 waypoint）。
        // 其它长度（多段压缩/3 段以上）当前不支持，标记后丢弃。
        transition select(hdr.srh.hdrExtLen) {
            2: parse_srh_inner;
            4: parse_segment1;
            default: parse_srh_bad;
        }
    }

    state parse_srh_bad {
        // 只支持 1 段（BE）和 2 段（一个 waypoint）；其它长度标记后丢弃。
        meta.bad_srh = 1;
        transition accept;
    }

    state parse_segment1 {
        packet.extract(hdr.segment1);
        transition parse_srh_inner;
    }

    state parse_srh_inner {
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

    // 接入口同时接受 tagged（L2VPN VLAN）和 untagged（L3VPN VXLAN）。
    action set_hybrid() {
        if (hdr.vlan.isValid()) {
            meta.vlan_id = hdr.vlan.vid;
        } else {
            meta.vlan_id = VLAN_NONE;
        }
        meta.port_ok = 1;
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
            set_hybrid;
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

    // 接入 VXLAN 解封装：把内层帧搬回主头，VNI 已由调用方存进 meta.vni。
    action vxlan_access_decap() {
        hdr.ethernet.dstAddr = hdr.inner_ethernet.dstAddr;
        hdr.ethernet.srcAddr = hdr.inner_ethernet.srcAddr;
        hdr.ethernet.etherType = hdr.inner_ethernet.etherType;
        hdr.vlan.setInvalid();
        hdr.vlan_inner.setInvalid();
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
        hdr.segment1.setInvalid();
        hdr.udp.setInvalid();
        hdr.vxlan.setInvalid();
        hdr.inner_ethernet.setInvalid();
        hdr.inner_vlan.setInvalid();
        hdr.inner_ipv6.setInvalid();
    }

    table vni_map {
        key = {
            meta.vni: exact;
        }
        actions = {
            map_l3;
            drop;
        }
        size = 4096;
        default_action = drop;
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
                      bit<48> smac, bit<48> dmac, bit<9> port) {
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
        // Copy the tenant DSCP into the outer header so the underlay can
        // classify the tunnel exactly like the inner flow.
        hdr.ipv6.trafficClass = hdr.inner_ipv6.trafficClass;
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
        hdr.segment1.setInvalid();

        hdr.vlan.setInvalid();
        hdr.vlan_inner.setInvalid();
        hdr.ethernet.srcAddr = smac;
        hdr.ethernet.dstAddr = dmac;
        hdr.ethernet.etherType = ETHERTYPE_IPV6;
        standard_metadata.egress_spec = port;
    }

    // 多段封装：segment[0] = 最终 SID，segment[1] = waypoint SID，
    // 外层 DA = segment[segmentsLeft] = waypoint SID（RFC 8754 顺序）。
    action srv6_encap_te(bit<128> src_sid, bit<128> final_sid,
                         bit<128> via_sid, bit<48> smac, bit<48> dmac,
                         bit<9> port) {
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
        hdr.ipv6.trafficClass = hdr.inner_ipv6.trafficClass;
        hdr.ipv6.flowLabel = 0;
        // 外层 = 40B IPv6 + 8B SRH + 2×16B segment + 内层
        hdr.ipv6.payloadLen = hdr.inner_ipv6.payloadLen + 80;
        hdr.ipv6.nextHdr = IPPROTO_SRH;
        hdr.ipv6.hopLimit = 64;
        hdr.ipv6.srcAddr = src_sid;
        hdr.ipv6.dstAddr = via_sid;

        hdr.srh.setValid();
        hdr.srh.nextHdr = IPPROTO_IPV6;
        hdr.srh.hdrExtLen = 4;
        hdr.srh.routingType = SRH_ROUTING_TYPE;
        hdr.srh.segmentsLeft = 1;
        hdr.srh.lastEntry = 1;
        hdr.srh.flags = 0;
        hdr.srh.tag = 0;
        hdr.segment0.setValid();
        hdr.segment0.sid = final_sid;
        hdr.segment1.setValid();
        hdr.segment1.sid = via_sid;

        hdr.vlan.setInvalid();
        hdr.vlan_inner.setInvalid();
        hdr.ethernet.srcAddr = smac;
        hdr.ethernet.dstAddr = dmac;
        hdr.ethernet.etherType = ETHERTYPE_IPV6;
        standard_metadata.egress_spec = port;
    }

    // End：waypoint 上把 segmentsLeft 减一、DA 换成下一段
    action srv6_end() {
        if (hdr.ipv6.hopLimit <= 1) {
            mark_to_drop(standard_metadata);
        } else {
            hdr.ipv6.hopLimit = hdr.ipv6.hopLimit - 1;
            hdr.srh.segmentsLeft = hdr.srh.segmentsLeft - 1;
            if (hdr.srh.segmentsLeft == 0) {
                hdr.ipv6.dstAddr = hdr.segment0.sid;
            } else {
                hdr.ipv6.dstAddr = hdr.segment1.sid;
            }
            meta.transit = 1;
        }
    }

    action forward_sid(bit<9> port, bit<48> smac, bit<48> dmac) {
        standard_metadata.egress_spec = port;
        hdr.ethernet.srcAddr = smac;
        hdr.ethernet.dstAddr = dmac;
    }

    table transit_sid {
        key = {
            hdr.ipv6.dstAddr: lpm;
        }
        actions = {
            srv6_end;
            drop;
        }
        size = 1024;
        default_action = drop;
    }

    table sid_fib {
        key = {
            hdr.ipv6.dstAddr: lpm;
        }
        actions = {
            forward_sid;
            drop;
        }
        size = 1024;
        default_action = drop;
    }

    // L3 交付到 PM：自包含的 VXLAN 封装（VNI = 网络 ID），
    // 内层目的 MAC 改成 VM，外层是 E→PM 的 underlay 头。
    action forward_l3_vxlan(bit<128> src_vtep, bit<128> dst_vtep,
                            bit<48> outer_smac, bit<48> outer_dmac,
                            bit<48> vm_mac, bit<48> inner_smac,
                            bit<9> port, bit<24> vni) {
        hdr.inner_ethernet.setValid();
        hdr.inner_ethernet.dstAddr = vm_mac;
        hdr.inner_ethernet.srcAddr = inner_smac;
        hdr.inner_ethernet.etherType = hdr.vlan.isValid() ?
                                       hdr.vlan.etherType :
                                       hdr.ethernet.etherType;
        hdr.inner_vlan.setInvalid();
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
        hdr.ipv6.trafficClass = hdr.inner_ipv6.trafficClass;
        hdr.ipv6.flowLabel = 0;
        // 外层 = 8B UDP + 8B VXLAN + 14B 内层 Eth + 内层 IPv6
        hdr.ipv6.payloadLen = hdr.inner_ipv6.payloadLen + 70;
        hdr.ipv6.nextHdr = IPPROTO_UDP;
        hdr.ipv6.hopLimit = 64;
        hdr.ipv6.srcAddr = src_vtep;
        hdr.ipv6.dstAddr = dst_vtep;

        hdr.udp.setValid();
        hdr.udp.srcPort = UDP_VXLAN;
        hdr.udp.dstPort = UDP_VXLAN;
        // UDP 长度 = 8(UDP) + 8(VXLAN) + 14(内层 Eth) + 内层 IPv6
        hdr.udp.length = hdr.inner_ipv6.payloadLen + 70;
        hdr.udp.checksum = 0;
        hdr.vxlan.setValid();
        hdr.vxlan.flags = VXLAN_FLAGS;
        hdr.vxlan.reserved = 0;
        hdr.vxlan.vni = vni;
        hdr.vxlan.reserved2 = 0;

        hdr.srh.setInvalid();
        hdr.segment0.setInvalid();
        hdr.segment1.setInvalid();
        // 接入 underlay 走 VLAN 4000，PM 侧在 u4000 上终结
        hdr.vlan.setValid();
        hdr.vlan.pcp = 0;
        hdr.vlan.dei = 0;
        hdr.vlan.vid = 4000;
        hdr.vlan.etherType = ETHERTYPE_IPV6;
        hdr.ethernet.srcAddr = outer_smac;
        hdr.ethernet.dstAddr = outer_dmac;
        hdr.ethernet.etherType = ETHERTYPE_VLAN;
        standard_metadata.egress_spec = port;
    }

    action forward_l3(bit<9> port, bit<48> smac, bit<48> dmac,
                      bit<12> out_vlan) {
        standard_metadata.egress_spec = port;
        hdr.ethernet.srcAddr = smac;
        hdr.ethernet.dstAddr = dmac;
        if (out_vlan == VLAN_NONE) {
            // Removing the VLAN header must restore the inner EtherType.
            // Otherwise the packet is untagged but Ethernet still says 0x8100,
            // and the receiver misinterprets the IPv6 header as a VLAN tag.
            hdr.vlan.setInvalid();
            hdr.vlan_inner.setInvalid();
            hdr.ethernet.etherType = ETHERTYPE_IPV6;
        } else {
            hdr.vlan.setValid();
            hdr.vlan.pcp = 0;
            hdr.vlan.dei = 0;
            hdr.vlan.vid = out_vlan;
            hdr.vlan.etherType = ETHERTYPE_IPV6;
            hdr.vlan_inner.setInvalid();
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
            forward_l3_vxlan;
            srv6_encap;
            srv6_encap_te;
            drop;
        }
        size = 65536;
        default_action = drop;
    }

    action srv6_decap(bit<16> vrf_id, bit<9> port, bit<48> smac,
                      bit<48> dmac, bit<12> out_vlan) {
        meta.vrf_id = vrf_id;
        hdr.ethernet.srcAddr = smac;
        hdr.ethernet.dstAddr = dmac;
        hdr.ethernet.etherType = ETHERTYPE_VLAN;
        hdr.vlan.setValid();
        hdr.vlan.pcp = 0;
        hdr.vlan.dei = 0;
        hdr.vlan.vid = out_vlan;
        hdr.vlan.etherType = ETHERTYPE_IPV6;
        hdr.vlan_inner.setInvalid();
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
        hdr.segment1.setInvalid();
        hdr.inner_ipv6.setInvalid();
        standard_metadata.egress_spec = port;
    }

    // SRv6 解封装后直接交给 VXLAN 接入：只恢复内层，不碰 VLAN。
    action srv6_decap_vxlan(bit<16> vrf_id) {
        meta.vrf_id = vrf_id;
        hdr.ethernet.etherType = ETHERTYPE_IPV6;
        hdr.vlan.setInvalid();
        hdr.vlan_inner.setInvalid();
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
        hdr.segment1.setInvalid();
        hdr.inner_ipv6.setInvalid();
    }

    action srv6_l2_encap(bit<128> src_sid, bit<128> dst_sid,
                         bit<48> smac, bit<48> dmac, bit<9> port) {
        // dot1q 子接口终结业务 VLAN，隧道内层不带 802.1Q；
        // 对端 E 解封装时按 VSI 的 VLAN 重新打上。
        bit<16> overhead = 78;
        hdr.inner_ethernet.setValid();
        hdr.inner_ethernet.dstAddr = hdr.ethernet.dstAddr;
        hdr.inner_ethernet.srcAddr = hdr.ethernet.srcAddr;
        hdr.inner_ethernet.etherType = hdr.vlan.isValid() ?
                                       hdr.vlan.etherType :
                                       hdr.ethernet.etherType;
        hdr.inner_vlan.setInvalid();
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
        // Outer DSCP follows the tenant frame, matching the L3VPN path.
        hdr.ipv6.trafficClass = hdr.inner_ipv6.isValid() ?
                                hdr.inner_ipv6.trafficClass : 0;
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
        hdr.segment1.setInvalid();

        hdr.vlan.setInvalid();
        hdr.vlan_inner.setInvalid();
        hdr.ethernet.srcAddr = smac;
        hdr.ethernet.dstAddr = dmac;
        hdr.ethernet.etherType = ETHERTYPE_IPV6;
        standard_metadata.egress_spec = port;
    }

    action srv6_l2_decap(bit<9> port, bit<12> vlan) {
        hdr.ethernet.dstAddr = hdr.inner_ethernet.dstAddr;
        hdr.ethernet.srcAddr = hdr.inner_ethernet.srcAddr;
        hdr.ethernet.etherType = hdr.inner_ethernet.etherType;
        if (hdr.inner_vlan.isValid()) {
            // 对端显式带了内层 VLAN（例如对接真实设备）时按报文里的值恢复。
            hdr.vlan.setValid();
            hdr.vlan.pcp = hdr.inner_vlan.pcp;
            hdr.vlan.dei = hdr.inner_vlan.dei;
            hdr.vlan.vid = hdr.inner_vlan.vid;
            hdr.vlan.etherType = hdr.inner_vlan.etherType;
        } else if (vlan != VLAN_NONE) {
            // 隧道内层不带 VLAN：按本 VSI 的业务 VLAN 打回去。
            hdr.vlan.setValid();
            hdr.vlan.pcp = 0;
            hdr.vlan.dei = 0;
            hdr.vlan.vid = vlan;
            hdr.vlan.etherType = hdr.ethernet.etherType;
            hdr.ethernet.etherType = ETHERTYPE_VLAN;
        } else {
            hdr.vlan.setInvalid();
            hdr.vlan_inner.setInvalid();
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
        hdr.segment1.setInvalid();
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
            srv6_decap_vxlan;
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

    action vxlan_encap(bit<128> src, bit<128> dst, bit<48> smac,
                       bit<48> dmac, bit<9> port, bit<24> vni) {
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
        hdr.ipv6.trafficClass = hdr.inner_ipv6.trafficClass;
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
        hdr.vlan_inner.setInvalid();
        hdr.ethernet.srcAddr = smac;
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
            hdr.vlan_inner.setInvalid();
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
        meta.transit = 0;
        meta.bad_srh = 0;
        meta.vni = 0;
        meta.ingress_port = standard_metadata.ingress_port;

        port_cfg.apply();
        // QinQ：外层是接入域，切片标识取内层 VLAN
        if (hdr.vlan_inner.isValid()) {
            meta.vlan_id = hdr.vlan_inner.vid;
        }
        if (meta.bad_srh == 1) {
            drop();
        }
        if (meta.port_ok == 1 && meta.bad_srh == 0) {
            if (hdr.vxlan.isValid()) {
                if (meta.core_port == 1) {
                    // E↔E 的 VXLAN 回退模式：VNI → S1 端口。
                    vxlan_map.apply();
                } else {
                    // 接入 VXLAN：解封装 + VNI → VRF/VS I。
                    meta.vni = hdr.vxlan.vni;
                    vxlan_access_decap();
                    vni_map.apply();
                }
            } else if (hdr.srh.isValid()) {
                if (hdr.srh.segmentsLeft > 0) {
                    // SRv6 transit（waypoint）：改写 SL/DA 后再查 SID 路由。
                    transit_sid.apply();
                    if (meta.transit == 1) {
                        sid_fib.apply();
                    }
                } else {
                    // 目的 SID：解封装后由下面的公共逻辑按 VRF 投递。
                    local_sid.apply();
                }
            } else {
                if (meta.core_port == 1) {
                    if (hdr.ipv6.isValid()) {
                        vrf_map.apply();
                    }
                } else if (meta.vlan_id != VLAN_NONE) {
                    vlan_map.apply();
                }
            }

            if (meta.transit == 0) {
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
                                bit<48> smac, bit<48> dmac) {
        bit<16> overhead = 78;
        hdr.inner_ethernet.setValid();
        hdr.inner_ethernet.dstAddr = hdr.ethernet.dstAddr;
        hdr.inner_ethernet.srcAddr = hdr.ethernet.srcAddr;
        hdr.inner_ethernet.etherType = hdr.vlan.isValid() ?
                                       hdr.vlan.etherType :
                                       hdr.ethernet.etherType;
        hdr.inner_vlan.setInvalid();
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
        // Outer DSCP follows the tenant frame, matching the L3VPN path.
        hdr.ipv6.trafficClass = hdr.inner_ipv6.isValid() ?
                                hdr.inner_ipv6.trafficClass : 0;
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
        hdr.segment1.setInvalid();

        hdr.vlan.setInvalid();
        hdr.vlan_inner.setInvalid();
        hdr.ethernet.srcAddr = smac;
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
            hdr.vlan_inner.setInvalid();
        }
    }

    action set_trunk() {
    }

    // 接入口同时接受 tagged（L2VPN VLAN）和 untagged（L3VPN VXLAN）。
    action set_hybrid() {
        if (hdr.vlan.isValid()) {
            meta.vlan_id = hdr.vlan.vid;
        } else {
            meta.vlan_id = VLAN_NONE;
        }
        meta.port_ok = 1;
    }

    action set_core() {
        if (hdr.vlan.isValid()) {
            hdr.ethernet.etherType = hdr.vlan.etherType;
            hdr.vlan.setInvalid();
            hdr.vlan_inner.setInvalid();
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
            set_hybrid;
            drop;
        }
        size = 256;
        default_action = drop;
    }

    // QinQ：只有经 Linux VTEP 接入的 L3 切片才加外层 tag。
    // 内层 tag 已由 ingress 写好（vid = 切片网络 ID + 3500）。
    action qinq_push() {
        hdr.vlan_inner.setValid();
        hdr.vlan_inner.pcp = hdr.vlan.pcp;
        hdr.vlan_inner.dei = hdr.vlan.dei;
        hdr.vlan_inner.vid = hdr.vlan.vid;
        hdr.vlan_inner.etherType = hdr.vlan.etherType;
        hdr.vlan.vid = QINQ_OUTER_VLAN;
        hdr.vlan.etherType = ETHERTYPE_VLAN;
        hdr.ethernet.etherType = ETHERTYPE_VLAN;
    }

    action qinq_nop() {
    }

    table qinq_map {
        key = {
            hdr.vlan.vid: exact;
        }
        actions = {
            qinq_push;
            qinq_nop;
        }
        size = 4096;
        default_action = qinq_nop;
    }

    apply {
        if (meta.l2_bum == 1) {
            l2_rep_sid.apply();
        }
        egress_port.apply();
        // 出方向 QinQ：表中登记过的切片才压外层 3500
        if (hdr.vlan.isValid()) {
            qinq_map.apply();
        }
        // 防环：不要把帧从入口原样送回。SRv6 transit（waypoint）
        // 例外——它必须从进来的核心口再发出去。
        if (meta.transit == 0 &&
            meta.ingress_port == standard_metadata.egress_port) {
            mark_to_drop(standard_metadata);
        }
    }
}

control ComputeChecksumImpl(inout headers_t hdr,
                            inout metadata_t meta) {
    apply {
        // TODO: IPv6 下 UDP 校验和是强制的，这里填 0 会被内核丢包。
        // 现在的 update_checksum 方案会破坏转发路径，先退回；
        // 下一步要么手写校验和，要么接入 underlay 改走 IPv4。
    }
}

control DeparserImpl(packet_out packet, in headers_t hdr) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.vlan);
        packet.emit(hdr.vlan_inner);
        packet.emit(hdr.ipv6);
        packet.emit(hdr.srh);
        packet.emit(hdr.segment0);
        packet.emit(hdr.segment1);
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
