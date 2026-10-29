# 并行网络需求与观测规范

本文由两份需求材料合并整理：

- 《中关村主干网并行网络设计方案》（网络资源、站点拓扑、地址 VLAN 规划、设备配置样例、公网接入）；
- 《UNIT bitmap》（观测位图定义、端到端报文结构、业务路径与物理路径对照）。

实现映射见 [PARALLEL_NETWORK_DESIGN.md](PARALLEL_NETWORK_DESIGN.md)，验收流程见
[VERIFICATION.md](VERIFICATION.md)。

---

## 1. 网络资源

### 1.1 公网 IPv6 地址

| 前缀 | 说明 |
| --- | --- |
| `240a:a806::/32` | 从 FITI-G 借用，切片 2055 |
| `240a:a807::/32` | 切片 2056 |

### 1.2 VLAN 规划

| 用途 | VLAN 范围 |
| --- | --- |
| 定制并行网络 | 3400-3500 |
| L2VPN + L3VPN | 3501-3900 |
| 虚拟化自用 | 2000-3000 |

### 1.3 并行网络 ID、VLAN 与地址规则

```text
并行网络 ID 范围 = 1 ~ 400
VLAN            = 并行网络 ID + 3500
业务 IP         = 240a:a806:{并行网络 ID}:{站点 ID}::/64
互联 IP         = 240a:a806:{并行网络 ID}:{100 + 站点 ID}::/64
                  主干网侧：240a:a806:{并行网络 ID}:{100 + 站点 ID}::1/64
                  用户侧：  240a:a806:{并行网络 ID}:{100 + 站点 ID}::2/64
```

### 1.4 地址与 VLAN 规划表（节选）

| ID | VLAN | 站点 ID-0 | 站点 ID-1 | … | 站点 ID-40 |
| --- | --- | --- | --- | --- | --- |
| 1 | 3501 | `240a:a806:1:0::/64`<br>`240a:a806:1:100::/64` | `240a:a806:1:1::/64`<br>`240a:a806:1:101::/64` | … | `240a:a806:1:40::/64`<br>`240a:a806:1:140::/64` |
| 2 | 3502 | `240a:a806:2:0::/64`<br>`240a:a806:2:100::/64` | `240a:a806:2:1::/64`<br>`240a:a806:2:101::/64` | … | `240a:a806:2:40::/64`<br>`240a:a806:2:140::/64` |
| 3 | 3503 | `240a:a806:3:0::/64`<br>`240a:a806:3:100::/64` | `240a:a806:3:1::/64`<br>`240a:a806:3:101::/64` | … | `240a:a806:3:40::/64`<br>`240a:a806:3:140::/64` |
| 4 | 3504 | `240a:a806:4:0::/64`<br>`240a:a806:4:100::/64` | `240a:a806:4:1::/64`<br>`240a:a806:4:101::/64` | … | `240a:a806:4:40::/64`<br>`240a:a806:4:140::/64` |
| … | … | … | … | … | … |
| 399 | 3899 | `240a:a806:399:0::/64`<br>`240a:a806:399:100::/64` | `240a:a806:399:1::/64`<br>`240a:a806:399:101::/64` | … | `240a:a806:399:40::/64`<br>`240a:a806:399:140::/64` |
| 400 | 3900 | `240a:a806:400:0::/64`<br>`240a:a806:400:100::/64` | `240a:a806:400:1::/64`<br>`240a:a806:400:101::/64` | … | `240a:a806:400:40::/64`<br>`240a:a806:400:140::/64` |

每行两格分别是业务 IP 和互联 IP。

---

## 2. 站点网络与业务拓扑

### 2.1 站点结构

每个站点的设备层次固定：

```text
主干网 C 路由器
├── 主干网 E 路由器（试验路由器，承载全部并行网络）
│   └── 站点交换机 S1（上行放行并行网络 VLAN）
└── 主干网 A 路由器
    └── 站点交换机 S2（上行拒绝并行网络 VLAN）
```

- 云计算服务器同时接入 S1 和 S2，图中标注 `M-LA`（多链路聚合）和 `G`（网关）两类链路，
  原始材料未给出聚合细节；
- 所有并行网络由 E 路由器承载；S1 上行放行并行网络 VLAN，S2 上行拒绝。

### 2.2 业务拓扑（北京 / 广州）

站点内的业务层次为：E 的 VRF — 防火墙 — 汇聚 — 接入 — 网络（VLAN 域）— 虚机。

```text
北京站点                                   广州站点
北京路由器E (VRF-A)                        广州路由器E (VRF-A)
  └── eth5/eth6 firewall1                    └── eth6/eth7 firewall2
        ├── 汇聚1 (eth1/eth2)                     ├── 汇聚3 (eth1/eth2)
        │     └── 接入1 (eth1)                    │     └── 接入3 (eth1)
        │           └── network1 (eth3)           │           └── network3 (eth3)
        │                 └── vm1 vm2 vm3         │                 └── vm11 vm12 vm13
        └── 汇聚2 (eth3/eth4)                     └── 汇聚4 (eth3/eth4)
              └── 接入2 (eth2)                          └── 接入4 (eth2)
                    └── network2 (eth4)                     └── network4 (eth4)
                          └── vm4 vm5 vm6                       └── vm14 vm15 vm16
```

主干网 C 层形成环路：北京 C — 郑州 C — 武汉 C — 广州 C；
武汉 C 另接南昌 C，南昌 C 接南昌 E。两地 E 之间通过“主干网切片 A（VRF-A）”互通，
切片可以是 BE 或 TE 路径。

### 2.3 虚机分布

- 北京站点：VM1/VM2/VM3 在 PM1，VM4/VM5/VM6 在 PM2，firewall-1 在 PM3；
  每台 PM 内部用 OVS 连接虚机网卡（tap）、网关和两条上联（eno1/eno2）。
- 广州站点：虚机对称分布在 PM6、PM7、PM8、PM9、PM10 上。

### 2.4 L3VPN / L2VPN 业务示意

- L3VPN：并行网络一（slice0001）覆盖哈尔滨 E、西安 E、海口 E 等站点，并行网络二
  （slice0002）覆盖北京 E、上海 E、成都 E 等站点；两地 E 之间为 EVPN SRv6。
- L2VPN：并行网络三（slice0003）覆盖哈尔滨 E、西安 E、成都 E、广州 E，并行网络四
  （slice0004）覆盖北京 E、上海 E、成都 E、南宁 E；同为 EVPN SRv6。
- 公网接入：北京 C 上另有 slice-public（`zslicefix`）负责与 FIX 互通，
  通过 BGP 发布汇总路由 `240a:a806::/32`、`240a:a807::/32`，并接收默认路由。

---

## 3. 报文结构

### 3.1 端到端逐段报文

以 `vm1(192.168.1.10) → vm3(192.168.3.30)` 为例，路径为
`vm1 → 宿主机1 → 宿主机2 → vm2 → 路由器A → 路由器B → 宿主机3 → vm3`，
跨北京站点 → 主干网 → 上海站点。

| 段 | 说明 | 报文结构（从左到右为转发方向） |
| --- | --- | --- |
| ① vm1 → 宿主机1 | 初始报文（二层信息），发往虚机 vm2 | `vm2-mac` `vm1-mac` `vm3-ip` `vm1-ip` `dscp` `icmp` |
| ② 宿主机1 → 宿主机2 | VXLAN 隧道：外层隧道封装 + 内层帧 | underlay：`宿主机2-mac` `宿主机1-mac` `宿主机2-ip` `宿主机1-ip` `UDP` `VNI`<br>overlay：`vm2-mac` `vm1-mac` `vm3-ip` `vm1-ip` `dscp` `icmp` |
| ③ 宿主机2 → vm2 | 解封装后送原始内层帧，交付给 vm2 | `vm2-mac` `vm1-mac` `vm3-ip` `vm1-ip` `dscp` `icmp` |
| ④ vm2 → 路由器A | 二层转发，报文进入 VLAN 段 | `路由器A-mac` `vm2-mac` `VLAN` `vm3-ip` `vm1-ip` `dscp` `icmp` |
| ⑤ 路由器A → 路由器B | SRv6 段路由：目的 MAC 改为路由器B，携带 SRv6 SID | underlay：`路由器B-mac` `路由器A-mac` `路由器A-srv6-srcip` `路由器B-sid`<br>overlay：`vm3-ip` `vm1-ip` `dscp` `icmp` |
| ⑥ 路由器B → 宿主机3 | 目的 MAC 改为 vm3，携带 VLAN，交付目标宿主机 | `vm3-mac` `路由器B-mac` `VLAN` `vm3-ip` `vm1-ip` `dscp` `icmp` |
| ⑦ 宿主机3 → vm3 | 最终交付，送达 192.168.3.30 | `vm3-mac` `路由器B-mac` `vm3-ip` `vm1-ip` `dscp` `icmp` |

图例：MAC（二层地址）；IP / SRv6-srcip（三层地址）；UDP / VNI / SID（隧道与路由信息）；
VLAN；dscp；icmp。

要点：

- 虚机发出的报文不带 VLAN，VLAN 是路由器（PE）与宿主机之间的段内封装；
- 隧道（VXLAN 或 SRv6）内部只保留内层业务帧，外层承载隧道地址；
- DSCP 在每一段都随报文保留，用于逐段分类。

### 3.2 物理路径上的报文结构

| 物理路径 | 报文结构 |
| --- | --- |
| `PMx-eno2 -- 交换机 -- PMy-eno2` | VXLAN 封装 |
| `X路由器E-VRF …… Y路由器E-VRF` | SRv6 封装（BE/TE） |
| `PMx-eno1 -- 交换机 -- 路由器E` | VLAN 报文 |
| `VMx-ethx -- PMy-OVS-tapy` | 普通 IP 报文 |
| `PMx-OVS-tap -- PMx-OVS-tun` | OVS 内部流转 |

### 3.3 观测点位置（图示2）

单站点内的观测点链路：

```text
VM1(eth1) -- PM1-OVS-tap1
PM1-OVS-tun -- PM1-eno1 -- 交换机 -- PM2-eno1 -- PM2-OVS-tun
PM2-OVS-tap1 -- VM2(eth1)
VM2(eth1) -- PM2-OVS-tap1
PM2-OVS -- PM2-eno2 -- 交换机 -- 路由器E1
路由器E1 -- 路由器C1 -- 路由器C2 -- 路由器E2
路由器E2 -- 交换机 -- PM2-eno2 -- PM3-OVS
PM3-OVS-tap1 -- VM3(eth1)
```

PM 之间经交换机互联，PM 与路由器经交换机互联，路由器之间为主干网；
图中另有“上行链路 / 下行链路”方向标注。原始材料对 PM 之间的互联口
存在两种写法（图示2 写 `eno1`，报文结构表写 `eno2`），按同一段理解。

---

## 4. 设备配置样例

### 4.1 L3VPN（并行网络 ID=300，上海站点 ID=2）

E 路由器（所有站点的 E 都需下发同类配置）：

```text
ip vpn-instance zslice0300
 address-family ipv6
  route-distinguisher 9425:300
  vpn-target 9425:300
  apply-label per-instance

interface 100GE3/0/40.3800
 vlan-type dot1q 3800
 ip binding vpn-instance zslice0300
 ipv6 address 240A:A806:300:102::1/64

ipv6 route-static vpn-instance zslice0300 240a:a806:300:2:: 64 240a:a806:300:2::2 tag 300

ipv6 prefix-list zslice0300 index 10 permit 240a:a806:300:2:: 64
route-policy zslice0300 permit node 10
 if-match ipv6 address prefix-list zslice0300

bgp 9425
 ip vpn-instance zslice0300
  address-family ipv6 unicast
   import-route direct
   import-route static route-policy zslice0300
   segment-routing ipv6 locator sh-e1 evpn
   segment-routing ipv6 best-effort evpn
```

C 路由器（仅北京站点核心路由器）：

```text
ip vpn-instance zslice0300
 address-family ipv6
  route-distinguisher 9425:300
  vpn-target 9425:300
  vpn-target 9425:39000 export-extcommunity
  vpn-target 9425:39001 import-extcommunity
  apply-label per-instance

bgp 9425
 ip vpn-instance zslice0300
  address-family ipv6 unicast
   segment-routing ipv6 locator bj-c1 evpn
   segment-routing ipv6 best-effort evpn
```

### 4.2 L2VPN（并行网络 ID=300）

E 路由器：

```text
vsi zslice0300
 evpn encapsulation srv6
  route-distinguisher 9425:300
  vpn-target 9425:300
  segment-routing ipv6 best-effort
  segment-routing ipv6 locator sh-e1

interface ten-gigabitethernet 3/1/1.3800
 vlan-type dot1q vid 3800
 xconnect vsi zslice0300
```

### 4.3 公网接入（北京核心路由器 C2）

```text
ip vpn-instance zslicefix
 address-family ipv6
  vpn-target 9425:39000 import-extcommunity
  vpn-target 9425:39001 export-extcommunity

interface HundredGigE1/1/4/1.2055
 ip binding vpn-instance zslicefix
 mac-address 9023-b4a7-a806
 vlan-type dot1q vid 20 second-dot1q 7
 ipv6 address 2001:255:0:100::A806/64

interface HundredGigE1/3/2/1.2055
 vlan-type dot1q vid 20 second-dot1q 7
 xconnect vsi 100

ipv6 prefix-list zslicefromfix index 10 permit :: 0
ipv6 prefix-list zslicetofix index 10 permit 240a:a806:: 32
ipv6 prefix-list zslicetofix index 20 permit 240a:a807:: 32

bgp 9425
 ip vpn-instance zslicefix
  router-id 1.18.7.1
  peer 2001:255:0:100::2000 as-number 38255
  peer 2001:255:0:100::2000 fake-as 144704
  address-family ipv6 unicast
   peer 2001:255:0:100::2000 enable
   peer 2001:255:0:100::2000 prefix-list zslicefromfix import
   aggregate 240a:a806:: 32 detail-suppressed
   aggregate 240a:a807:: 32 detail-suppressed
```

`router-id 1.18.7.1` 对应切片 2055；QinQ 为外层 VLAN 20、内层 VLAN 7；
`mac-address 9023-b4a7-a806` 固定公网侧 MAC。

---

## 5. 业务路径与物理路径对照

| 业务路径 | 物理路径 |
| --- | --- |
| VM1 -- VM2（同 PM） | `VM1-eth1 → PM1-OVS-tap1 → PM1-OVS-tap2 → VM2-eth1` |
| VM1 -- 接入1 -- 汇聚1 -- 接入2 -- VM4 | `VM1-eth1 → PM1-OVS-tap1 → PM1-OVS-tun → PM1-eno2 → 北京交换机 → PM4-eno2 → PM4-OVS-tun → PM4-OVS-tap3 → 接入1-eth8 → 接入1-eth1 → PM4-OVS-tap1 → PM4-OVS-tun → PM4-eno2 → 北京交换机 → PM5-eno2 → PM5-OVS-tun → PM5-OVS-tap1 → 汇聚1-eth1 → 汇聚1-eth2 → PM5-OVS-tap2 → PM5-OVS-tun → PM5-eno2 → 北京交换机 → PM4-eno2 → PM4-OVS-tap4 → 接入2-eth2 → 接入2-eth8 → PM4-OVS-tap6 → PM4-OVS-tun → PM4-eno2 → 北京交换机 → PM2-eno2 → PM2-OVS-tun → PM2-OVS-tap1 → VM4-eth1` |
| VM1 -- 接入1 -- 汇聚1 -- firewall1 -- 主干网切片（VRF-A，BE）-- firewall2 -- 汇聚4 -- 接入4 -- VM14 | `VM1-eth1 → PM1-OVS-tap1 → PM1-OVS-tun → PM1-eno2 → 北京交换机 → PM4-eno2 → PM4-OVS-tun → PM4-OVS-tap3 → 接入1-eth8 → 接入1-eth1 → PM4-OVS-tap1 → PM4-OVS-tun → PM4-eno2 → 北京交换机 → PM5-eno2 → PM5-OVS-tun → PM5-OVS-tap1 → 汇聚1-eth1 → 汇聚1-eth5 → PM5-OVS-tap3 → PM5-OVS-tun → PM5-eno2 → 北京交换机 → PM3-eno2 → PM3-OVS-tun → PM3-OVS-tap1 → firewall1-eth5 → firewall1-eth7 → PM3-OVS-tap3 → PM3-eno1 → 北京交换机 → 北京路由器 E-VRFA → 北京路由器 C → 郑州路由器 C → 武汉路由器 C → 广州路由器 C → 广州路由器 E-VRFA → 广州交换机 → PM8-eno1 → PM8-OVS-tap3 → firewall2-eth7 → firewall2-eth5 → PM8-OVS-tap1 → PM8-OVS-tun → PM8-eno2 → 广州交换机 → PM10-eno2 → PM10-OVS-tun → PM10-OVS-tap6 → 汇聚4-eth6 → 汇聚4-eth4 → PM10-OVS-tap5 → PM10-OVS-tun → PM10-eno2 → 广州交换机 → PM9-eno2 → PM9-OVS-tun → PM9-OVS-tap5 → 接入4-eth4 → 接入4-eth8 → PM9-OVS-tap6 → PM9-OVS-tun → PM9-eno2 → 广州交换机 → PM7-eno2 → PM7-OVS-tun → PM7-OVS-tap1 → VM14-eth1` |
| VM1 -- 接入1 -- 汇聚1 -- firewall1 -- 主干网切片（VRF-A，TE+南昌）-- firewall2 -- 汇聚4 -- 接入4 -- VM14 | 与上一行相同，仅主干网段改为 `北京路由器 E-VRFA → 北京路由器 C → 郑州路由器 C → 武汉路由器 C → 南昌路由器 C → 南昌路由器 E → 南昌路由器 C → 武汉路由器 C → 广州路由器 C → 广州路由器 E-VRFA` |

同一业务路径在不同段上依次对应：普通 IP（VM 到 OVS tap）、OVS 内部流转、VXLAN（PM 之间）、
VLAN（PM 到 E）、SRv6（E 到 E）。

---

## 6. 观测位图（UNIT bitmap）

### 6.1 位定义

| bit | 类别 | 内涵 | 位置 | 观测项 |
| --- | --- | --- | --- | --- |
| 0 | Performance | Performance | 云（宿主机） | 虚机网卡速率（pps/bps）、累计吞吐；邻居时延/抖动/丢包 |
| | | | 网（观测装置） | 路由器与邻居链路时延/抖动/丢包 |
| | | | 端（终端） | Linux 网卡速率（pps/bps）、累计吞吐 |
| 1 | Flow | Flow | 云 | 五元组、速率（pps/bps）、累计吞吐、持续时长 |
| | | | 网 | 五元组、速率（pps/bps）、累计吞吐、持续时长 |
| | | | 端 | 五元组、速率（pps/bps）、累计吞吐、持续时长 |
| 2 | Path | Path、Table | 云 | 五元组/接口/方向/内外层 MAC/内外层 TTL/内外层 VLAN；本机 routes／neigh／ovs／nft／iptables 表项 |
| | | | 网 | 五元组/接口/方向/内外层 MAC/内外层 TTL/内外层 VLAN；路由器 routes／neigh／policy 表项 |
| | | | 端 | 无 |
| 3 | Status | Load、Event、Log、Config、Syscall | 云 | CPU／内存／磁盘等使用情况；连接清单及归属进程；系统事件；运行日志 |
| | | | 网 | 路由器的 CPU／内存／磁盘使用情况；系统事件；运行日志；运行配置 |
| | | | 端 | CPU／内存／磁盘等使用情况；连接清单及归属进程；系统调用（open／write／rename／unlink／read／accept／connect／send／recv／exec） |

### 6.2 Path 位的要求

- 云侧要看**内外层** MAC、TTL、VLAN，说明同一时刻报文存在两层封装；
- 云侧还要采集本机 `routes`／`neigh`／`ovs`／`nft`／`iptables` 表项；
- 网侧（观测装置）采集路由器 `routes`／`neigh`／`policy` 表项；
- 端侧不采集 Path。

对应到 3.1 的报文结构：VXLAN 段的内外层是“宿主机 MAC/IP + VNI”与“虚机 MAC/IP”，
SRv6 段的内外层是“路由器 MAC + SRv6 SID”与“虚机 MAC/IP”；VLAN 只出现在
PM 与 E 之间的段上。
