/* -*- P4_16 -*- */
/*
 * FITI bmv2 data plane.
 *
 * L2:
 *   - access/trunk VLAN classification and forwarding
 *   - per-slice (VLAN) MAC table and flooding
 *
 * SRv6:
 *   - H.Encaps: outer IPv6 + SRH + inner IPv6
 *   - transit: sid_fib forwarding
 *   - End: decrement segmentsLeft and update outer DA
 *   - End.X: End + explicit egress port / next-hop MAC
 *   - End.DT6: decap and VRF lookup in vrf_fib
 *   - End.DX6: decap and direct access-port delivery
 */

#include <core.p4>
#include <v1model.p4>

const bit<16> ETHERTYPE_VLAN = 0x8100;
const bit<16> ETHERTYPE_IPV6 = 0x86dd;
const bit<8>  IPPROTO_SRH = 43;
const bit<8>  IPPROTO_IPV6 = 41;
const bit<8>  SRH_ROUTING_TYPE = 4;
const bit<12> VLAN_NONE = 0;
const bit<8>  MAX_SEGMENTS = 16;

const bit<8> SID_NONE = 0;
const bit<8> SID_END = 1;
const bit<8> SID_END_X = 2;
const bit<8> SID_END_DT6 = 3;
const bit<8> SID_END_DX6 = 4;

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

struct headers_t {
    ethernet_t        ethernet;
    vlan_t            vlan;
    ipv6_t            ipv6;
    srh_t             srh;
    srh_segment_t[16] segmentList;
    ipv6_t            ipv6_inner;
}

struct metadata_t {
    bit<9>  ingress_port;
    bit<12> vlan_id;
    bit<16> slice_id;
    bit<1>  port_ok;
    bit<1>  srv6;
    bit<1>  srv6_mode;
    bit<1>  sid_hit;
    bit<1>  encap_done;
    bit<8>  sid_action;
    bit<9>  sid_port;
    bit<48> sid_dmac;
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
            default: reject;
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
            default: accept;
        }
    }

    state parse_srh {
        packet.extract(hdr.srh);
        transition select(hdr.srh.routingType, hdr.srh.lastEntry) {
            (SRH_ROUTING_TYPE, 0): parse_seg0;
            (SRH_ROUTING_TYPE, 1): parse_seg0;
            (SRH_ROUTING_TYPE, 2): parse_seg0;
            (SRH_ROUTING_TYPE, 3): parse_seg0;
            (SRH_ROUTING_TYPE, 4): parse_seg0;
            (SRH_ROUTING_TYPE, 5): parse_seg0;
            (SRH_ROUTING_TYPE, 6): parse_seg0;
            (SRH_ROUTING_TYPE, 7): parse_seg0;
            (SRH_ROUTING_TYPE, 8): parse_seg0;
            (SRH_ROUTING_TYPE, 9): parse_seg0;
            (SRH_ROUTING_TYPE, 10): parse_seg0;
            (SRH_ROUTING_TYPE, 11): parse_seg0;
            (SRH_ROUTING_TYPE, 12): parse_seg0;
            (SRH_ROUTING_TYPE, 13): parse_seg0;
            (SRH_ROUTING_TYPE, 14): parse_seg0;
            (SRH_ROUTING_TYPE, 15): parse_seg0;
            default: parse_bad;
        }
    }

    state parse_seg0 {
        packet.extract(hdr.segmentList[0]);
        transition select(hdr.srh.lastEntry) {
            0: parse_inner;
            default: parse_seg1;
        }
    }
    state parse_seg1 {
        packet.extract(hdr.segmentList[1]);
        transition select(hdr.srh.lastEntry) {
            1: parse_inner;
            default: parse_seg2;
        }
    }
    state parse_seg2 {
        packet.extract(hdr.segmentList[2]);
        transition select(hdr.srh.lastEntry) {
            2: parse_inner;
            default: parse_seg3;
        }
    }
    state parse_seg3 {
        packet.extract(hdr.segmentList[3]);
        transition select(hdr.srh.lastEntry) {
            3: parse_inner;
            default: parse_seg4;
        }
    }
    state parse_seg4 {
        packet.extract(hdr.segmentList[4]);
        transition select(hdr.srh.lastEntry) {
            4: parse_inner;
            default: parse_seg5;
        }
    }
    state parse_seg5 {
        packet.extract(hdr.segmentList[5]);
        transition select(hdr.srh.lastEntry) {
            5: parse_inner;
            default: parse_seg6;
        }
    }
    state parse_seg6 {
        packet.extract(hdr.segmentList[6]);
        transition select(hdr.srh.lastEntry) {
            6: parse_inner;
            default: parse_seg7;
        }
    }
    state parse_seg7 {
        packet.extract(hdr.segmentList[7]);
        transition select(hdr.srh.lastEntry) {
            7: parse_inner;
            default: parse_seg8;
        }
    }
    state parse_seg8 {
        packet.extract(hdr.segmentList[8]);
        transition select(hdr.srh.lastEntry) {
            8: parse_inner;
            default: parse_seg9;
        }
    }
    state parse_seg9 {
        packet.extract(hdr.segmentList[9]);
        transition select(hdr.srh.lastEntry) {
            9: parse_inner;
            default: parse_seg10;
        }
    }
    state parse_seg10 {
        packet.extract(hdr.segmentList[10]);
        transition select(hdr.srh.lastEntry) {
            10: parse_inner;
            default: parse_seg11;
        }
    }
    state parse_seg11 {
        packet.extract(hdr.segmentList[11]);
        transition select(hdr.srh.lastEntry) {
            11: parse_inner;
            default: parse_seg12;
        }
    }
    state parse_seg12 {
        packet.extract(hdr.segmentList[12]);
        transition select(hdr.srh.lastEntry) {
            12: parse_inner;
            default: parse_seg13;
        }
    }
    state parse_seg13 {
        packet.extract(hdr.segmentList[13]);
        transition select(hdr.srh.lastEntry) {
            13: parse_inner;
            default: parse_seg14;
        }
    }
    state parse_seg14 {
        packet.extract(hdr.segmentList[14]);
        transition select(hdr.srh.lastEntry) {
            14: parse_inner;
            default: parse_seg15;
        }
    }
    state parse_seg15 {
        packet.extract(hdr.segmentList[15]);
        transition parse_inner;
    }

    state parse_inner {
        transition select(hdr.srh.nextHdr) {
            IPPROTO_IPV6: parse_inner_ipv6;
            default: accept;
        }
    }

    state parse_inner_ipv6 {
        packet.extract(hdr.ipv6_inner);
        transition accept;
    }

    state parse_bad {
        transition reject;
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
            mark_to_drop(standard_metadata);
        } else if (vlan == VLAN_NONE || vlan == 4095) {
            mark_to_drop(standard_metadata);
        } else {
            hdr.vlan.setValid();
            hdr.vlan.pcp = 0;
            hdr.vlan.dei = 0;
            hdr.vlan.vid = vlan;
            hdr.vlan.etherType = hdr.ethernet.etherType;
            hdr.ethernet.etherType = ETHERTYPE_VLAN;
            meta.vlan_id = vlan;
            meta.slice_id = (bit<16>) vlan;
            meta.port_ok = 1;
        }
    }

    action set_trunk() {
        if (!hdr.vlan.isValid() ||
            hdr.vlan.vid == VLAN_NONE || hdr.vlan.vid == 4095) {
            mark_to_drop(standard_metadata);
        } else {
            meta.vlan_id = hdr.vlan.vid;
            meta.slice_id = (bit<16>) hdr.vlan.vid;
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
            drop;
        }
        size = 256;
        default_action = drop;
    }

    action srv6_on() {
        meta.srv6_mode = 1;
    }

    action srv6_off() {
        meta.srv6_mode = 0;
    }

    table srv6_mode {
        key = {
            meta.slice_id: exact;
        }
        actions = {
            srv6_on;
            srv6_off;
        }
        size = 8192;
        default_action = srv6_off;
    }

    action forward(bit<9> port) {
        standard_metadata.egress_spec = port;
    }

    action nop() {
    }

    table l2_fwd {
        key = {
            meta.slice_id:          exact;
            hdr.ethernet.dstAddr:   exact;
        }
        actions = {
            forward;
            nop;
        }
        size = 32768;
        default_action = nop;
    }

    action sid_end(bit<16> slice_id) {
        meta.sid_hit = 1;
        meta.sid_action = SID_END;
        meta.slice_id = slice_id;
    }

    action sid_end_x(bit<16> slice_id, bit<9> port, bit<48> dmac) {
        meta.sid_hit = 1;
        meta.sid_action = SID_END_X;
        meta.slice_id = slice_id;
        meta.sid_port = port;
        meta.sid_dmac = dmac;
    }

    action sid_end_dt6(bit<16> slice_id) {
        meta.sid_hit = 1;
        meta.sid_action = SID_END_DT6;
        meta.slice_id = slice_id;
    }

    action sid_end_dx6(bit<9> port, bit<48> dmac, bit<16> slice_id) {
        meta.sid_hit = 1;
        meta.sid_action = SID_END_DX6;
        meta.sid_port = port;
        meta.sid_dmac = dmac;
        meta.slice_id = slice_id;
    }

    action sid_nop() {
    }

    table local_sid {
        key = {
            meta.slice_id:      exact;
            hdr.ipv6.dstAddr:   lpm;
        }
        actions = {
            sid_end;
            sid_end_x;
            sid_end_dt6;
            sid_end_dx6;
            sid_nop;
        }
        size = 16384;
        default_action = sid_nop;
    }

    action sid_forward(bit<9> port, bit<48> dmac) {
        standard_metadata.egress_spec = port;
        hdr.ethernet.dstAddr = dmac;
    }

    table sid_fib {
        key = {
            meta.slice_id:      exact;
            hdr.ipv6.dstAddr:   lpm;
        }
        actions = {
            sid_forward;
            drop;
        }
        size = 32768;
        default_action = drop;
    }

    action srv6_encap(
            bit<8>   count,
            bit<128> s0, bit<128> s1, bit<128> s2, bit<128> s3,
            bit<128> s4, bit<128> s5, bit<128> s6, bit<128> s7,
            bit<128> s8, bit<128> s9, bit<128> s10, bit<128> s11,
            bit<128> s12, bit<128> s13, bit<128> s14, bit<128> s15,
            bit<128> src, bit<48> dmac, bit<9> port,
            bit<8> hop_limit) {
        // Mark the encap path as taken before validating the segment count,
        // otherwise an invalid action would fall through to l2_fwd() after
        // mark_to_drop() and could be forwarded again.
        meta.encap_done = 1;
        if (count == 0 || count > MAX_SEGMENTS) {
            mark_to_drop(standard_metadata);
        } else {
            hdr.ipv6_inner.setValid();
            hdr.ipv6_inner.version = hdr.ipv6.version;
            hdr.ipv6_inner.trafficClass = hdr.ipv6.trafficClass;
            hdr.ipv6_inner.flowLabel = hdr.ipv6.flowLabel;
            hdr.ipv6_inner.payloadLen = hdr.ipv6.payloadLen;
            hdr.ipv6_inner.nextHdr = hdr.ipv6.nextHdr;
            hdr.ipv6_inner.hopLimit = hdr.ipv6.hopLimit;
            hdr.ipv6_inner.srcAddr = hdr.ipv6.srcAddr;
            hdr.ipv6_inner.dstAddr = hdr.ipv6.dstAddr;

            hdr.ipv6.version = 6;
            hdr.ipv6.trafficClass = 0;
            hdr.ipv6.flowLabel = 0;
            hdr.ipv6.payloadLen = hdr.ipv6_inner.payloadLen +
                                  48 + ((bit<16>) count << 4);
            hdr.ipv6.nextHdr = IPPROTO_SRH;
            hdr.ipv6.hopLimit = hop_limit;
            hdr.ipv6.srcAddr = src;

            hdr.srh.setValid();
            hdr.srh.nextHdr = IPPROTO_IPV6;
            hdr.srh.hdrExtLen = (bit<8>) (count << 1);
            hdr.srh.routingType = SRH_ROUTING_TYPE;
            hdr.srh.segmentsLeft = count - 1;
            hdr.srh.lastEntry = count - 1;
            hdr.srh.flags = 0;
            hdr.srh.tag = 0;

            hdr.segmentList[0].setInvalid();
            hdr.segmentList[1].setInvalid();
            hdr.segmentList[2].setInvalid();
            hdr.segmentList[3].setInvalid();
            hdr.segmentList[4].setInvalid();
            hdr.segmentList[5].setInvalid();
            hdr.segmentList[6].setInvalid();
            hdr.segmentList[7].setInvalid();
            hdr.segmentList[8].setInvalid();
            hdr.segmentList[9].setInvalid();
            hdr.segmentList[10].setInvalid();
            hdr.segmentList[11].setInvalid();
            hdr.segmentList[12].setInvalid();
            hdr.segmentList[13].setInvalid();
            hdr.segmentList[14].setInvalid();
            hdr.segmentList[15].setInvalid();

            hdr.segmentList[0].setValid();
            hdr.segmentList[0].sid = s0;
            if (count > 1)  { hdr.segmentList[1].setValid();  hdr.segmentList[1].sid = s1; }
            if (count > 2)  { hdr.segmentList[2].setValid();  hdr.segmentList[2].sid = s2; }
            if (count > 3)  { hdr.segmentList[3].setValid();  hdr.segmentList[3].sid = s3; }
            if (count > 4)  { hdr.segmentList[4].setValid();  hdr.segmentList[4].sid = s4; }
            if (count > 5)  { hdr.segmentList[5].setValid();  hdr.segmentList[5].sid = s5; }
            if (count > 6)  { hdr.segmentList[6].setValid();  hdr.segmentList[6].sid = s6; }
            if (count > 7)  { hdr.segmentList[7].setValid();  hdr.segmentList[7].sid = s7; }
            if (count > 8)  { hdr.segmentList[8].setValid();  hdr.segmentList[8].sid = s8; }
            if (count > 9)  { hdr.segmentList[9].setValid();  hdr.segmentList[9].sid = s9; }
            if (count > 10) { hdr.segmentList[10].setValid(); hdr.segmentList[10].sid = s10; }
            if (count > 11) { hdr.segmentList[11].setValid(); hdr.segmentList[11].sid = s11; }
            if (count > 12) { hdr.segmentList[12].setValid(); hdr.segmentList[12].sid = s12; }
            if (count > 13) { hdr.segmentList[13].setValid(); hdr.segmentList[13].sid = s13; }
            if (count > 14) { hdr.segmentList[14].setValid(); hdr.segmentList[14].sid = s14; }
            if (count > 15) { hdr.segmentList[15].setValid(); hdr.segmentList[15].sid = s15; }

            hdr.ipv6.dstAddr = hdr.segmentList[count - 1].sid;
            hdr.ethernet.dstAddr = dmac;
            standard_metadata.egress_spec = port;
        }
    }

    action encap_nop() {
    }

    table encap {
        key = {
            meta.slice_id:      exact;
            hdr.ipv6.dstAddr:   lpm;
        }
        actions = {
            srv6_encap;
            encap_nop;
        }
        size = 32768;
        default_action = encap_nop;
    }

    action forward_inner(bit<9> port, bit<48> dmac) {
        hdr.ipv6.setInvalid();
        hdr.srh.setInvalid();
        hdr.segmentList[0].setInvalid();
        hdr.segmentList[1].setInvalid();
        hdr.segmentList[2].setInvalid();
        hdr.segmentList[3].setInvalid();
        hdr.segmentList[4].setInvalid();
        hdr.segmentList[5].setInvalid();
        hdr.segmentList[6].setInvalid();
        hdr.segmentList[7].setInvalid();
        hdr.segmentList[8].setInvalid();
        hdr.segmentList[9].setInvalid();
        hdr.segmentList[10].setInvalid();
        hdr.segmentList[11].setInvalid();
        hdr.segmentList[12].setInvalid();
        hdr.segmentList[13].setInvalid();
        hdr.segmentList[14].setInvalid();
        hdr.segmentList[15].setInvalid();
        hdr.ethernet.dstAddr = dmac;
        standard_metadata.egress_spec = port;
    }

    table vrf_fib {
        key = {
            meta.slice_id:          exact;
            hdr.ipv6_inner.dstAddr: lpm;
        }
        actions = {
            forward_inner;
            drop;
        }
        size = 16384;
        default_action = drop;
    }

    apply {
        meta.ingress_port = standard_metadata.ingress_port;
        meta.vlan_id = VLAN_NONE;
        meta.slice_id = 0;
        meta.port_ok = 0;
        meta.srv6 = 0;
        meta.srv6_mode = 0;
        meta.sid_hit = 0;
        meta.encap_done = 0;
        meta.sid_action = SID_NONE;

        port_cfg.apply();
        if (meta.port_ok == 1) {
            srv6_mode.apply();
            if (hdr.ipv6.isValid() && meta.srv6_mode == 1 ) {
                if (hdr.srh.isValid()) {
                    meta.srv6 = 1;
                    local_sid.apply();

                    if (meta.sid_hit == 1) {
                        if (meta.sid_action == SID_END ||
                            meta.sid_action == SID_END_X) {
                            if (hdr.srh.segmentsLeft > hdr.srh.lastEntry) {
                                // Malformed SRH: segmentsLeft must not point
                                // past the end of the segment list.
                                drop();
                            } else if (hdr.srh.segmentsLeft > 0) {
                                hdr.srh.segmentsLeft =
                                    hdr.srh.segmentsLeft - 1;
                                hdr.ipv6.dstAddr =
                                    hdr.segmentList[hdr.srh.segmentsLeft].sid;
                                if (meta.sid_action == SID_END_X) {
                                    standard_metadata.egress_spec =
                                        meta.sid_port;
                                    hdr.ethernet.dstAddr = meta.sid_dmac;
                                } else {
                                    sid_fib.apply();
                                }
                            } else {
                                vrf_fib.apply();
                            }
                        } else if (meta.sid_action == SID_END_DT6) {
                            vrf_fib.apply();
                        } else if (meta.sid_action == SID_END_DX6) {
                            hdr.ipv6.setInvalid();
                            hdr.srh.setInvalid();
                            hdr.segmentList[0].setInvalid();
                            hdr.segmentList[1].setInvalid();
                            hdr.segmentList[2].setInvalid();
                            hdr.segmentList[3].setInvalid();
                            hdr.segmentList[4].setInvalid();
                            hdr.segmentList[5].setInvalid();
                            hdr.segmentList[6].setInvalid();
                            hdr.segmentList[7].setInvalid();
                            hdr.segmentList[8].setInvalid();
                            hdr.segmentList[9].setInvalid();
                            hdr.segmentList[10].setInvalid();
                            hdr.segmentList[11].setInvalid();
                            hdr.segmentList[12].setInvalid();
                            hdr.segmentList[13].setInvalid();
                            hdr.segmentList[14].setInvalid();
                            hdr.segmentList[15].setInvalid();
                            standard_metadata.egress_spec = meta.sid_port;
                            hdr.ethernet.dstAddr = meta.sid_dmac;
                        }
                    } else {
                        sid_fib.apply();
                    }
                } else {
                    encap.apply();
                    if (meta.encap_done == 0) {
                        l2_fwd.apply();
                    }
                }
            } else {
                l2_fwd.apply();
            }

            if (meta.srv6 == 0 && meta.encap_done == 0 &&
                standard_metadata.egress_spec == 0 &&
                standard_metadata.mcast_grp == 0) {
                standard_metadata.mcast_grp = (bit<16>) meta.vlan_id;
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
        packet.emit(hdr.srh);
        packet.emit(hdr.segmentList[0]);
        packet.emit(hdr.segmentList[1]);
        packet.emit(hdr.segmentList[2]);
        packet.emit(hdr.segmentList[3]);
        packet.emit(hdr.segmentList[4]);
        packet.emit(hdr.segmentList[5]);
        packet.emit(hdr.segmentList[6]);
        packet.emit(hdr.segmentList[7]);
        packet.emit(hdr.segmentList[8]);
        packet.emit(hdr.segmentList[9]);
        packet.emit(hdr.segmentList[10]);
        packet.emit(hdr.segmentList[11]);
        packet.emit(hdr.segmentList[12]);
        packet.emit(hdr.segmentList[13]);
        packet.emit(hdr.segmentList[14]);
        packet.emit(hdr.segmentList[15]);
        packet.emit(hdr.ipv6_inner);
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
