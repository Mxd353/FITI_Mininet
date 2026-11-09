// SPDX-License-Identifier: GPL-2.0
// 模拟网卡 checksum offload：给 E 发出的 VXLAN 重算 IPv6/UDP 校验和。
//
// 背景：BMv2 的 ComputeChecksum 阶段看不到 deparser 之后拼好的字节，
// 所以 P4 只能把 UDP checksum 填 0；IPv6 下 0 非法，对端内核直接丢包。
// 这个程序挂在 E 的出口（tc clsact egress），在报文已经成型后重算。
//
// 只处理：Eth [+VLAN...] + IPv6 + UDP(dport 4789)。其余一律放行。
#include <linux/bpf.h>
#include <linux/pkt_cls.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

#define ETH_VLAN_8021Q 0x8100
#define ETH_VLAN_8021AD 0x88a8
#define ETH_VLAN_QINQ 0x9100
#define ETH_IPV6 0x86dd
#define IPPROTO_UDP_ 17
#define VXLAN_DPORT 4789

#define CHUNK 256
#define MAX_CHUNKS 8  /* 2048B，覆盖 endpoint_mtu + 封装开销 */

static __always_inline __u32 fold32(__u32 sum)
{
	sum = (sum & 0xffff) + (sum >> 16);
	sum = (sum & 0xffff) + (sum >> 16);
	return sum;
}

SEC("tc")
int vxlan_csum_shim(struct __sk_buff *skb)
{
	__u8 buf[CHUNK];
	__u8 ip6[40];
	__u8 ph[40];
	__u16 eth_type, tmp;
	__u32 off = 14; /* MAC 头之后 */
	int i;

	if (bpf_skb_load_bytes(skb, 12, &eth_type, 2) < 0)
		return TC_ACT_OK;
	eth_type = bpf_ntohs(eth_type);

	/* 跳过 802.1Q / QinQ 标签（最多两层） */
#pragma unroll
	for (i = 0; i < 2; i++) {
		if (eth_type != ETH_VLAN_8021Q &&
		    eth_type != ETH_VLAN_8021AD &&
		    eth_type != ETH_VLAN_QINQ)
			break;
		if (bpf_skb_load_bytes(skb, off + 2, &tmp, 2) < 0)
			return TC_ACT_OK;
		eth_type = bpf_ntohs(tmp);
		off += 4;
	}

	if (eth_type != ETH_IPV6)
		return TC_ACT_OK;

	if (bpf_skb_load_bytes(skb, off, ip6, sizeof(ip6)) < 0)
		return TC_ACT_OK;
	/* 只处理固定头 + 直接 UDP，扩展头交给内核 */
	if (ip6[6] != IPPROTO_UDP_)
		return TC_ACT_OK;

	__u16 plen = ((__u16)ip6[4] << 8) | ip6[5];
	__u32 udp_off = off + 40;
	if (plen < 8)
		return TC_ACT_OK;

	if (bpf_skb_load_bytes(skb, udp_off + 2, &tmp, 2) < 0)
		return TC_ACT_OK;
	if (bpf_ntohs(tmp) != VXLAN_DPORT)
		return TC_ACT_OK;

	/* 伪首部：src + dst + 32bit 长度 + 3 字节 0 + next header */
	__builtin_memset(ph, 0, sizeof(ph));
	if (bpf_skb_load_bytes(skb, off + 8, ph, 32) < 0)
		return TC_ACT_OK;
	ph[35] = plen & 0xff;
	ph[34] = (plen >> 8) & 0xff;
	ph[39] = IPPROTO_UDP_;

	__u32 sum = 0;
	sum = bpf_csum_diff(NULL, 0, (__be32 *)ph, sizeof(ph), sum);

	/* UDP 头 + 载荷，校验和字段按 0 计 */
#pragma unroll
	for (i = 0; i < MAX_CHUNKS; i++) {
		__u32 co = (__u32)i * CHUNK;
		__u32 n;
		__u32 n4;

		if (co >= plen)
			break;
		n = plen - co;
		if (n > CHUNK)
			n = CHUNK;

		__builtin_memset(buf, 0, sizeof(buf));
		if (bpf_skb_load_bytes(skb, udp_off + co, buf, n) < 0)
			return TC_ACT_OK;
		if (co == 0) {
			buf[6] = 0;
			buf[7] = 0;
		}
		n4 = (n + 3) & ~3u;
		if (n4 > CHUNK)
			n4 = CHUNK;
		sum = bpf_csum_diff(NULL, 0, (__be32 *)buf, n4, sum);
	}

	sum = fold32(sum);
	__u16 csum = (__u16)(~sum);
	if (csum == 0)
		csum = 0xffff; /* RFC 768：算出 0 要发 0xffff */

	if (bpf_skb_store_bytes(skb, udp_off + 6, &csum, sizeof(csum), 0) < 0)
		return TC_ACT_OK;

	return TC_ACT_OK;
}

char _license[] SEC("license") = "GPL";
