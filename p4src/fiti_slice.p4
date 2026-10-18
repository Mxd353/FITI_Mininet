/* -*- P4_16 -*- */
/*
 * FITI bmv2 data plane: VLAN-aware L2 switch with per-slice access ports.
 *
 * One simple_switch (bmv2) instance runs on every FITI node.  A node may
 * carry any number of slice hosts, each on its own access port / VLAN; the
 * inter-node links are 802.1Q trunks carrying all slice VLANs.
 *
 * Ingress
 *   - port_cfg maps the ingress port to one of:
 *       * set_access(vlan): untagged host port, push the VLAN tag
 *       * set_trunk():      tagged inter-node port, read the VLAN tag
 *       * drop:             blocked / unknown port
 *   - l2_fwd looks up (VLAN, dst MAC).  On a miss (broadcast, multicast or
 *     unknown unicast) the packet is flooded to multicast group == VLAN id.
 *
 * Egress
 *   - egress_port knows whether the outgoing port is access or trunk, so the
 *     tag is popped before the frame reaches a host.
 *   - A packet is never sent back to its ingress port (the multicast group on
 *     each switch contains every forwarding port; this check removes the
 *     ingress copy).
 *
 * The control plane (fiti_p4.py) computes a spanning tree, installs port_cfg
 * and l2_fwd entries, and creates one multicast group per VLAN per switch.
 */

#include <core.p4>
#include <v1model.p4>

const bit<16> ETHERTYPE_VLAN = 0x8100;
const bit<12> VLAN_NONE = 0;

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

struct headers_t {
    ethernet_t ethernet;
    vlan_t     vlan;
}

struct metadata_t {
    bit<9>  ingress_port;
    bit<12> vlan_id;
    bit<1>  port_ok;
}

parser ParserImpl(packet_in packet,
                  out headers_t hdr,
                  inout metadata_t meta,
                  inout standard_metadata_t standard_metadata) {
    state start {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            ETHERTYPE_VLAN: parse_vlan;
            default: accept;
        }
    }

    state parse_vlan {
        packet.extract(hdr.vlan);
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

    /* Untagged host port: classify the frame and push a VLAN tag. */
    action set_access(bit<12> vlan) {
        if (hdr.vlan.isValid()) {
            /* Access ports are untagged; a tagged frame is a misconfiguration. */
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
            meta.port_ok = 1;
        }
    }

    /* Inter-node trunk port: require a valid 802.1Q tag. */
    action set_trunk() {
        if (!hdr.vlan.isValid() ||
            hdr.vlan.vid == VLAN_NONE || hdr.vlan.vid == 4095) {
            mark_to_drop(standard_metadata);
        } else {
            meta.vlan_id = hdr.vlan.vid;
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
        size = 128;
        default_action = drop;
    }

    action forward(bit<9> port) {
        standard_metadata.egress_spec = port;
    }

    action nop() {
    }

    table l2_fwd {
        key = {
            meta.vlan_id:           exact;
            hdr.ethernet.dstAddr:   exact;
        }
        actions = {
            forward;
            nop;
        }
        size = 4096;
        default_action = nop;
    }

    apply {
        meta.ingress_port = standard_metadata.ingress_port;
        meta.vlan_id = VLAN_NONE;
        meta.port_ok = 0;

        if (!hdr.ethernet.isValid()) {
            mark_to_drop(standard_metadata);
        } else {
            port_cfg.apply();
            if (meta.port_ok == 1) {
                l2_fwd.apply();
                /* No unicast match: flood inside this VLAN.  The control
                 * plane creates one multicast group per VLAN on every switch,
                 * so the group id is simply the VLAN id. */
                if (standard_metadata.egress_spec == 0 &&
                    standard_metadata.mcast_grp == 0) {
                    standard_metadata.mcast_grp = (bit<16>) meta.vlan_id;
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
        size = 128;
        default_action = drop;
    }

    apply {
        egress_port.apply();
        /* Multicast groups contain all forwarding ports, so drop the copy
         * that would go back out of the ingress port. */
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
