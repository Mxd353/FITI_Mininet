# 中关村主干网并行网络 Mininet + BMv2 仿真设计方案

本文根据 [中关村主干网并行网络设计方案.pdf](中关村主干网并行网络设计方案.pdf) 整理，给出一套可在单台 Linux
主机上运行的仿真方案。目标是验证并行网络的 VLAN 接入、L3VPN/L2VPN 隔离、
跨站点转发和公网接入，而不是复现硬件路由器的转发性能或完整 EVPN 控制面。

当前执行状态：

- 已完成 41 站点拓扑、`parallel/` 子项目、E 的 BMv2 P4 程序；
- 站点接入交换机 S1/S2 使用 OVS，VLAN 放行/拒绝由 trunk 列表实现；
- 每站点一台云服务器，用 VLAN 子接口区分并行网络切片；
- 网络 1/2 为 L3VPN，网络 3/4 为 L2VPN，均覆盖全部站点；
- L3VPN/L2VPN 使用静态 SRv6，VXLAN 保留为回退模式；
- C2/FIX 公网接入已用静态路由实现；
- EVPN 路由模型已实现并驱动部分静态表项，真实 BGP/EVPN 控制面尚未实现。

L2VPN 为了验证跨站点二层互通，仿真使用一个扁平 /64：

```text
网络 3：240a:a806:3:1::/64
网络 4：240a:a806:4:1::/64
```

这不是 PDF 中按站点拆分的业务前缀，而是 L2VPN 广播域的仿真适配；L3VPN
仍严格使用 PDF 的 `{网络 ID}:{站点 ID}::/64` 规划。

## 1. PDF 要点

### 1.1 网络资源

| 资源 | 规划 |
| --- | --- |
| 公网 IPv6 | `240a:a806::/32`（FITI-G，切片 2055） |
| 公网 IPv6 | `240a:a807::/32`（切片 2056） |
| 定制并行网络 VLAN | 3400-3500 |
| L2VPN + L3VPN VLAN | 3501-3900 |
| 虚拟化自用 VLAN | 2000-3000 |

并行网络 ID 与 VLAN 的正式公式是：

```text
VLAN = 并行网络 ID + 3500
并行网络 ID = 1..400
VLAN = 3501..3900
```

因此仿真应以 3501-3900 作为 L2VPN/L3VPN 的业务 VLAN。3400-3500 是另一段
定制并行网络，不应与 VPN VLAN 混用。

### 1.2 站点拓扑

PDF 的站点拓扑不是简单的 C-E-A-S 链，而是：

```text
                    主干网 C 路由器
                   /             \
            主干网 E 路由器    主干网 A 路由器
                  |                 |
             站点交换机 S1     站点交换机 S2
                  \               /
                   \             /
                  云计算服务器群
```

关键点：

- `E` 路由器是试验路由器，承载并行网络；
- `S1` 连接 E，`S2` 连接 A；
- 云计算服务器同时接入 S1/S2（PDF 标注 `M-LA`、`G`，可理解为
  多链路/双归接入，具体链路聚合方式需要原方案补充）；
- `S1` 上行放行并行网络 VLAN；
- `S2` 上行拒绝并行网络 VLAN。

### 1.3 L3VPN

PDF 的 L3VPN 使用 EVPN SRv6：

- 并行网络 1：哈尔滨 E、西安 E、海口 E、北京 C；
- 并行网络 2：北京 E、上海 E、成都 E、北京 C；
- E 路由器创建 VPN instance，例如 `zslice0300`；
- `route-distinguisher 9425:300`、`vpn-target 9425:300`；
- E 的上行子接口绑定 VPN instance；
- E 通过 EVPN SRv6 发布/接收 VPN 路由；
- 北京 C 作为核心，并连接公网接入点。

PDF 的上海站点示例（网络 ID 300，站点 ID 2）：

```text
VLAN:                 3800
业务 IPv6:            240a:a806:300:2::/64
互联 IPv6:            240a:a806:300:102::/64
主干网侧地址:         240a:a806:300:102::1/64
用户侧地址:           240a:a806:300:102::2/64
```

E 路由器接口示例：

```text
interface 100GE3/0/40.3800
 vlan-type dot1q 3800
 ip binding vpn-instance zslice0300
 ipv6 address 240A:A806:300:102::1/64
```

### 1.4 L2VPN

PDF 的 L2VPN 同样使用 EVPN SRv6：

- 并行网络 3：哈尔滨 E、西安 E、海口 E、广州 E；
- 并行网络 4：北京 E、上海 E、成都 E、南宁 E；
- E 路由器创建 VSI，例如 `zslice0300`；
- `evpn encapsulation srv6`；
- 接入子接口通过 `xconnect vsi zslice0300` 关联 VSI；
- 跨站点通过 EVPN SRv6 进行二层互通。

示例：

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

### 1.5 公网接入

北京核心路由器 C2 是并行网络的公网接入点：

- VPN instance `zslicefix`；
- 公网接口使用 VLAN 20 + 外层 VLAN 7（QinQ）；
- 公网地址示例 `2001:255:0:100::A806/64`；
- 与 FIX 建立 BGP，peer `2001:255:0:100::2000`；
- AS 38255，fake-as 144704；
- 导入默认路由 `::/0`；
- 只向公网发布 `240a:a806::/32` 和 `240a:a807::/32`；
- 对两个 /32 做 aggregate，`detail-suppressed`。

## 2. 仿真总体架构

### 2.1 分层

```text
Mininet                 负责网络命名空间、veth、链路和生命周期
BMv2 + P4               负责站点 PE（E）的可编程数据平面
OVS                     负责 S1/S2 接入交换机的 VLAN 转发与过滤
Linux netns             负责传统路由器、云服务器、公网边界
Python 控制面           负责生成并下发 E 的静态 P4 表项、配置 OVS
```

### 2.2 推荐设备映射

为了同时验证 PDF 的拓扑语义和可编程 PE，采用以下映射：

| 逻辑设备 | 仿真实现 | 说明 |
| --- | --- | --- |
| E_bj / E_sh / E_gz / E_cd | BMv2 + P4 | 试验路由器，承载 VRF、L3VPN、L2VPN/SRv6 数据平面 |
| S1_bj / S1_sh / S1_gz / S1_cd | OVS | 站点接入交换机，放行并行网络 VLAN |
| S2_bj / S2_sh / S2_gz / S2_cd | OVS | 站点接入交换机，上行拒绝并行网络 VLAN |
| C_bj / C_sh / C_gz / C_cd | Linux netns | 传统核心路由器，IPv6 转发/静态路由 |
| A_bj / A_sh / A_gz / A_cd | Linux netns | 传统汇聚路由器 |
| cl-* | Linux netns | 云计算服务器；全量每站点 1 台，验证每站点 2 台（带 Linux bridge + VXLAN VTEP） |
| pub_border / fix | Linux netns | 公网边界，BGP/静态路由、前缀过滤；`pub_border` 是短名，避免接口名超过 15 字符 |

这样：

- P4 集中在 E，负责真正的可编程部分：VRF/VSI、L3 转发、L2 桥接、
  SRv6/VXLAN 封装、BUM 复制；
- S1/S2 在 PDF 中本来就是普通站点交换机，用 OVS 实现 VLAN 转发、
  MAC 学习和 S2 上行过滤，语义更贴近原方案；
- C/A 保持传统三层转发节点，符合“普通三层节点用 netns”的要求；
- 云服务器和公网边界用 netns，便于运行 `ping6`、`tcpdump`、FRR 等工具。

如果要求严格按“E 也是传统路由器”建模，也可以使用备选映射：

- E 用 Linux VRF + 静态路由；
- S1/S2 用 OVS 或 Linux bridge，只做 VLAN/过滤/L2；
- L3VPN 由 E 的 Linux VRF 验证，P4 只验证接入和隔离。

两种映射不能混称为同一种实现。建议主方案使用“E 用 P4、S1/S2 用 OVS”的
映射，备选映射作为“PDF 配置语义对照”。

## 3. 仿真拓扑

### 3.1 41 站点拓扑

```text
                    pub_border / FIX
                           |
                        C_bj
                    /    |     \
                C_tjn  C_xa   C_sh ...
                  |      |       |
              E/A/S1/S2 ...   E/A/S1/S2
                  |              |
                cloud          cloud
             (一台主机，v3501~v3504 VLAN 子接口)
```

站点间核心链路直接使用 FITI 实际拓扑：

- 拓扑数据来自 `configs/fiti.json`；
- 41 个站点对应 41 个 FITI 节点；
- 43 条 FITI 链路对应站点 C 路由器之间的链路；
- C 路由按 FITI 图的最短路径计算下一跳；
- `C_bj` 同时连接公网边界；
- 全量每站点 1 台云服务器，4 站点验证每站点 2 台；每台通过 S1/S2 trunk 上的 VLAN 子接口区分切片；
- 每台云主机内部用 Linux bridge 连接租户 veth、VLAN 上联和 VXLAN VTEP，
  同站点主机之间为 VXLAN（VNI = 网络 ID），出站点仍为 VLAN；
- 每站点 `E` 是 BMv2/P4，`S1`/`S2` 是 OVS，`C`/`A`/cloud 是 netns；
- L3VPN 网络 1/2 覆盖全部 41 站点；
- L2VPN 网络 3/4 覆盖全部 41 站点；已知单播按远端 MAC 选择 SID，BUM
  使用 E 的 multicast group + egress_rid 做 ingress replication。

### 3.2 双归与 VLAN 策略

PDF 标注 S1/S2 到云服务器有 `M-LA`、`G` 两类链路，但未给出链路聚合细节。
仿真实现：

- 每台云服务器创建两个 veth，分别接 S1 和 S2；
- 并行网络业务 VLAN 只从 S1 上行放行；
- S2 上行对 3501-3900 做拒绝；
- 云服务器默认路由/策略路由优先走 S1；
- S2 作为非并行网络接入或备用路径，不参与并行网络转发。

S1/S2 是 OVS bridge，VLAN 策略通过 trunk 列表实现：

```text
S1  E 口 / cloud 口   trunks=1-4094
S2  cloud 口          trunks=1-4094
S2  A 口              trunks=1-3500,3901-4094
```

OVS 的端口级 VLAN 过滤同时作用于入向和出向，等价于 P4 里
`uplink_filter` 的 `deny_parallel`，但不需要为接入交换机维护表项。

如果后续需要模拟 LACP/链路聚合，可在 netns 中增加 bond，但首期不建议，
因为 Mininet 对 bond 和硬件卸载的模拟有限。

## 4. 地址与 VLAN 规划

### 4.1 站点 ID

首期建议：

```text
北京 = 站点 ID 1
上海 = 站点 ID 2
广州 = 站点 ID 3
成都 = 站点 ID 4
其余站点 = 站点 ID 5～41
```

上海站点 ID 2 与 PDF 示例一致；其余站点按扩展配置编号，具体见
`configs/parallel.json`。每个站点只有一台云服务器，切片通过 VLAN
子接口区分。

### 4.2 通用公式

```text
VLAN              = 并行网络 ID + 3500
业务 IPv6         = 240a:a806:{网络 ID}:{站点 ID}::/64
主机地址          = 240a:a806:{网络 ID}:{站点 ID}::{0x10 + 主机序号 - 1}
互联 IPv6         = 240a:a806:{网络 ID}:{100 + 站点 ID}::/64
主干网侧地址      = 240a:a806:{网络 ID}:{100 + 站点 ID}::1/64
用户侧地址        = 240a:a806:{网络 ID}:{100 + 站点 ID}::2/64
```

### 4.3 首期网络

| 网络 ID | VLAN | 用途 | 覆盖站点 | 业务地址 |
| --- | --- | --- | --- | --- |
| 1 | 3501 | L3VPN | 全部 41 站点 | `240a:a806:1:{站点 ID}::/64` |
| 2 | 3502 | L3VPN | 全部 41 站点 | `240a:a806:2:{站点 ID}::/64` |
| 3 | 3503 | L2VPN | 全部 41 站点 | `240a:a806:3:1::/64`（扁平） |
| 4 | 3504 | L2VPN | 全部 41 站点 | `240a:a806:4:1::/64`（扁平） |

互联地址：

| 网络 ID | 北京 | 上海 | 广州 | 成都 |
| --- | --- | --- | --- | --- |
| 1 | `240a:a806:1:101::/64` | `240a:a806:1:102::/64` | `240a:a806:1:103::/64` | `240a:a806:1:104::/64` |
| 2 | `240a:a806:2:101::/64` | `240a:a806:2:102::/64` | `240a:a806:2:103::/64` | `240a:a806:2:104::/64` |
| 3 | `240a:a806:3:101::/64` | `240a:a806:3:102::/64` | `240a:a806:3:103::/64` | `240a:a806:3:104::/64` |
| 4 | `240a:a806:4:101::/64` | `240a:a806:4:102::/64` | `240a:a806:4:103::/64` | `240a:a806:4:104::/64` |

### 4.4 仿真专用地址

PDF 没有给出完整的 underlay/loopback 地址。仿真中单独规划：

```text
站点互联:        fd00:0:{站点 ID}:1::/64
站点到 A:        fd00:0:{站点 ID}:2::/64
A 到 S2:         fd00:0:{站点 ID}:3::/64
remote underlay: fd00:0:{站点 ID}::/48
核心链路:        fd00:0:100:{FITI 链路序号}::/64
公网边界:        2001:255:0:100::/64
```

SRv6 SID 使用 `fd00:0:{站点 ID}:1::1:{网络 ID}`（L3）和
`fd00:0:{站点 ID}:1::2:{网络 ID}`（L2）。类型字（1/2）与网络 ID 分开，
1..400 范围内不会互相冲突。

这些地址只用于仿真 underlay，不改变 PDF 的业务/互联 IPv6 规划。

## 5. P4 数据平面设计

### 5.1 解析顺序

```text
Ethernet
  -> 802.1Q VLAN
  -> IPv6
  -> SRH（可选）
  -> inner IPv6（SRv6 解封装后）
```

如果首期使用 VXLAN 作为 L2VPN 过渡：

```text
Ethernet -> VLAN -> IPv6 -> UDP -> VXLAN -> inner Ethernet -> inner VLAN -> inner IPv6
```

### 5.2 表设计

E（BMv2/P4）上的表：

| 表 | key | 作用 |
| --- | --- | --- |
| `port_cfg` | ingress_port | access/trunk、默认 VLAN |
| `vlan_map` | VLAN | 网络 ID、VRF、VPN 类型 |
| `l3_fib` | vrf + IPv6 前缀 | L3VPN 下一跳/端口/DMAC/SRv6 SID |
| `l2_tunnel` | VLAN + MAC | L2VPN 已知单播到远端 SID |
| `l2_bum` / `l2_rep_sid` | VLAN + 入端口 / rid | BUM ingress replication |
| `local_sid` | IPv6 DA | 本端 SRv6 SID 解封装 |
| `vxlan_map` | VNI | L2VPN 过渡封装/解封装 |

`uplink_filter`、`l2_fib` 仍在 P4 程序中，但接入交换机改为 OVS 后不再下发：
VLAN 放行/拒绝由 OVS trunk 列表完成，MAC 转发由 OVS 学习完成。

### 5.3 隔离原则

- VLAN 只负责接入区分，不能单独作为 L3VPN 隔离；
- L3VPN 必须使用 `vrf_id` 隔离路由表；
- L2VPN 必须使用 VNI/VSI 隔离 MAC 表；
- S2 上行 VLAN 过滤必须显式拒绝，而不是依赖没有表项；当前由 OVS
  的 trunk 列表完成，入向和出向都生效；
- 不同并行网络的同一 SID/MAC 不能互相命中。

## 6. L3VPN 仿真流程

以网络 ID 300、上海站点（站点 ID 2）为例：

```text
cl_sh(240a:a806:300:2::10)
  -> S1_sh access VLAN 3800
  -> E_sh P4: vlan_map(3800) -> vrf=300
  -> l3_fib(vrf=300, 240a:a806:300:1::/64)
  -> 下一跳：E_bj 的 SRv6 locator / 互联地址
  -> C_sh/C_bj 或 E 之间的 underlay 转发
  -> E_bj P4: vrf=300
  -> l3_fib(vrf=300, 240a:a806:300:1::/64) 本地云服务器
  -> S1_bj -> cl_bj
```

北京到上海的反向路径同理。

首期控制面可以用 Python 生成静态表项：

```text
vlan_map: 3800 -> vrf 300, l3
l3_fib:   (300, 240a:a806:300:1::/64) -> E_bj 下一跳
l3_fib:   (300, 240a:a806:300:2::/64) -> E_sh 下一跳
```

这只是 EVPN 控制面安装结果的静态等价物，不代表已经实现 BGP EVPN。

## 7. L2VPN 仿真流程

以网络 ID 300、VLAN 3800 为例：

```text
cl_sh
  -> S1_sh OVS: VLAN 3800 接入，二层转发
  -> E_sh P4: vlan_map(3800) -> l2vpn, vsi=300
  -> l2_tunnel(vlan=3800, dst_mac)
  -> 远端 MAC：SRv6 封装到 E_bj 的 locator
  -> E_bj 解封装
  -> S1_bj OVS: 按目的 MAC 转发 -> cl_bj
```

多站点扩展后，E 的 `l2_tunnel` 以 `(VLAN, 目的 MAC)` 为键选择远端 SID；
已知单播直接查表，广播/组播/未知单播进入 `l2_bum` 表，按 VLAN 和入端口
选择 multicast group。E 为每个远端站点创建一个 multicast node（相同 C
端口、不同 rid）；egress 的 `l2_rep_sid` 按 rid 选择远端 SID，从而完成
ingress replication。S1/S2 只是 OVS 二层交换机：MAC 学习、泛洪和 VLAN
过滤由 OVS 完成，不封装 SRv6，也不再安装静态 `l2_fib` 或 multicast group。

PDF 的目标封装是 **EVPN SRv6**，不是 VXLAN。建议分两级实现：

1. 过渡阶段：用 VXLAN 验证 L2 隧道封装/解封装、VNI/VLAN 映射和隔离；
2. 目标阶段：替换为 SRv6 封装，按 EVPN 路由安装 SID，验证 `End.DT2`
   或等价的 L2 解封装行为。

报告中必须区分“VXLAN 过渡验证”和“EVPN SRv6 目标验证”，不能把 VXLAN
结果直接表述为 EVPN SRv6 已完成。

## 8. 公网接入仿真

`C_bj` 连接 `pub_border` 和 `fix`：

- `pub_border` 使用 netns，配置 `2001:255:0:100::A806/64`；
- `FIX` 使用 netns，配置 peer `2001:255:0:100::2000`；
- 首期可用静态路由模拟 BGP 结果；
- 需要验证 BGP 时，在 netns 中运行 FRR/BIRD；
- 导入策略：允许默认路由 `::/0`；
- 导出策略：只允许 `240a:a806::/32`、`240a:a807::/32`；
- 对两个 /32 做 aggregate，detail-suppressed；
- 验证未授权前缀（例如其他 /32）不会被发布。

## 9. 控制面策略

### 9.1 首期：静态表项与 EVPN 路由模型

Python 根据 `configs/parallel.json` 生成：

- E 的 `vlan_map`；
- E 的 `l3_fib` / `l2_tunnel` / `l2_bum` / `local_sid`；
- SRv6/VXLAN 封装参数；
- OVS 接入交换机的 trunk VLAN 列表；
- 公网前缀过滤规则。

E 的表项通过 Thrift 下发（P4Runtime 可作为后续南向替代）；S1/S2 通过
`ovs-vsctl` 配置。这种方式适合验证数据平面和隔离。

第一阶段还增加了 `parallel/evpn.py` 的 EVPN 路由模型：

- IP Prefix 路由：L3VPN 的 RD/RT、VNI、业务前缀和本端/远端 SID；
- MAC/IP 路由：L2VPN 云服务器 MAC、IP、VNI 和 L2 SID；
- Inclusive Multicast 路由：L2VPN BUM 流量的站点级 SID；
- `RouteTable` 提供 advertise/withdraw/replace 事件、JSON 导入导出和 SID 冲突校验。

当前 `l3_fib`、L3 `local_sid` 以及 L2 SRv6 隧道/解封装由路由对象驱动；
VLAN、公网表项和 OVS trunk 策略仍由静态控制面生成。可免 root 查看：

```bash
make parallel-evpn-dump
python3 parallel_evpn.py --json /tmp/evpn-routes.json
```

免 root 模式输出 164 条本地路由（82 条 IP Prefix + 82 条 IMET）；Mininet
启动后增加 82 条云服务器 MAC/IP 路由，共 246 条。

### 9.2 动态路由更新 agent

`parallel/evpn_agent.py` 在 Mininet 进程内启动 Unix socket，默认路径为
`/tmp/parallel_evpn.sock`。它把 `advertise` / `withdraw` 路由事件转换为
bmv2 表项和 Linux C 路由更新：

- L3 IP Prefix：更新 `vrf_map`、`l3_fib` 和 C 路由；`local_sid` 仍由静态控制面按网络/站点安装；
- L2 MAC/IP：更新 E 的 `l2_tunnel` 和云服务器邻居；S1/S2 由 OVS 学习，
  不再下发静态 `l2_fib`；
- 支持替换和撤销；路由不存在或校验失败时返回错误。

```bash
python3 parallel_evpn_ctl.py status
python3 parallel_evpn_ctl.py dump
python3 parallel_evpn_ctl.py advertise --file /tmp/evpn-route.json
python3 parallel_evpn_ctl.py withdraw --file /tmp/evpn-route.json
```

当前 agent 不动态修改 IMET/BUM multicast group；BUM 组仍由静态控制面安装。
真实 BGP 会话、Route Reflector 和 EVPN NLRI 编解码仍未实现。

### 9.3 后续：协议控制面

- underlay：FRR/BIRD 在 netns 中运行 OSPFv3/IS-IS/BGP；
- 公网：FRR/BIRD 与 FIX 建立 BGP；
- EVPN：需要支持 EVPN 的控制面（FRR、BIRD 或自定义控制器）；
- SRv6：需要 locator 分配、SID 发布和 EVPN 路由到 SID 的映射；
- BMv2 只负责数据平面，不能单独完成 EVPN/BGP 控制面。

推荐接入顺序：先用 `RouteTable`/agent 在北京/上海验证路由增删/撤销；
再验证 41 站点 L3VPN/L2VPN；最后接入 FRR/ExaBGP 并
批量生成 41 个站点配置。当前路由模型和 agent 不包含 BGP FSM、Route
Reflector 和 EVPN NLRI 编解码。

## 10. 实施阶段

### 阶段 0：拓扑骨架

- 创建 `parallel/` 子项目和 `configs/parallel.json`；
- 创建 C/E/A/S/云服务器/公网边界；
- 所有设备 veth 互联；
- 验证接口、IPv6 地址和 forwarding。

### 阶段 1：VLAN 接入与过滤

- 配置 VLAN 3501-3504；
- S1 两个端口 trunk 放行全部 VLAN；
- S2 的 A 上行 trunk 排除 3501-3900；
- 抓包验证 tag 和 drop。

### 阶段 2：L3VPN

- 配置网络 1/2 的 VRF；
- 安装 L3 路由和下一跳；
- 验证 41 站点同网络互通；
- 验证网络 1 与网络 2 隔离。

### 阶段 3：L2VPN

- 先做 VXLAN 过渡；
- 再做 SRv6 封装/解封装；
- 验证同 VLAN/同 MAC 跨站点互通；
- 验证 VNI/VSI 隔离。

### 阶段 4：公网接入

- C2 连接 pub_border/FIX；
- 配置默认路由、前缀过滤、aggregate；
- 验证公网访问和未授权前缀过滤。

### 阶段 5：EVPN/SRv6 控制面

- 引入 EVPN 路由模型（已完成第一阶段）；
- 增加 RouteTable -> P4 动态更新 agent（已完成）；
- 引入 SRv6 locator/SID；
- 验证控制面安装结果与静态表项一致；
- 接入 FRR/ExaBGP，验证 BGP EVPN 会话和路由撤销；
- 评估是否需要外部控制器。

## 11. 测试计划

### 11.1 基础连通性

```bash
ip -6 addr show
ip -6 route show
ping6 -c 3 <peer>
```

### 11.2 VLAN 与过滤

- 在 S1 上行抓包，确认 VLAN 3501-3504 存在；
- 在 S2 上行抓包，确认并行 VLAN 被丢弃；
- 检查 OVS trunk 列表：

```bash
ovs-vsctl get port S1_bj-eth1 trunks
ovs-vsctl get port S2_bj-eth1 trunks
```

### 11.3 L3VPN

- 41 站点同网络 `ping6` 成功；
- 不同网络 `ping6` 失败；
- 检查 `vrf`/`l3_fib` 表项；
- `traceroute6` 验证跨站点路径。

### 11.4 L2VPN

- 同 VLAN 跨站点 `ping6` 成功；
- 检查 E 的 `l2_tunnel`、BUM multicast group 和 OVS MAC 表；
- 抓包确认 SRv6 外层：BJ/上海/广州/成都之间使用不同远端 SID；
- VXLAN 回退模式只验证已知单播；
- 不同 VNI/VSI 互不通。

### 11.5 公网

- 云服务器访问公网前缀；
- FIX 只收到两个 /32 聚合路由；
- 未授权前缀不可达；
- 抓包检查 QinQ/VLAN 20/7。

## 12. 限制与假设

- BMv2 只验证协议逻辑和数据平面，不能代表硬件转发性能、队列、时延；
- PDF 的 `M-LA`、`G` 链路细节未给出，首期按双归/主备建模；
- PDF 的 C 路由器数量、C1/C2 关系需要原拓扑确认；
- L2VPN 目标是 EVPN SRv6；VXLAN 只是过渡验证；
- L2VPN 多站点 BUM 使用静态 multicast group + egress_rid；VXLAN
  回退模式只支持已知单播；
- 接入交换机 S1/S2 是 OVS，MAC 学习和泛洪由 OVS 完成，不是 EVPN
  下发的静态 FIB；
- 完整 EVPN/SRv6 控制面不能只靠 BMv2，需要 FRR/BIRD/自定义控制器；
- Linux netns 的 VRF、策略路由和 bond 行为与硬件路由器仍有差异；
- 400 个并行网络需要关注 P4 表容量，建议 `vlan_map`/`l3_fib`/`l2_tunnel`
  使用较大表项或按需加载。

## 13. 与现有工程的关系

建议新增独立子场景，不修改当前 FITI 41 节点案例：

```text
parallel/
  __init__.py
  bmv2.py             Thrift CLI 辅助
  config.py           站点、网络、VLAN、IPv6
  fiti_topology.py    FITI 实际拓扑与最短路径
  topology.py         C/E/A/S1/S2/cloud
  switch.py           BMv2（E）封装
  ovs.py              S1/S2 的 OVS VLAN 配置
  evpn.py             EVPN 路由模型
  evpn_agent.py       动态路由 agent
  control.py          E 的静态 P4 表项
  runtime.py          Mininet 构建
  p4.py               P4 编译与表名解析

configs/fiti.json
configs/parallel.json
configs/parallel-min.json
p4src/parallel_vpn.p4
parallel_net.py
parallel_evpn.py
parallel_evpn_ctl.py
docs/PARALLEL_NETWORK_DESIGN.md
```

可复用：

- `fiti/p4_mininet.py`：Mininet P4Host/P4Switch；
- `fiti/log.py`：日志和进度；
- `fiti/paths.py`：项目路径。

首期实现 41 站点、网络 1/2 的 L3VPN 和网络 3/4 的 L2VPN；
公网和 EVPN/SRv6 控制面按阶段推进。
