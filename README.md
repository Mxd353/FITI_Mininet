# FITI 并行网络仿真

> **版本说明（feature/vxlan-e-shim 分支）**
>
> 本分支是“E 直接终结 VXLAN”版本：PM 的 VXLAN 由 **E（BMv2/P4）自己解封装**，
> 不再需要 Linux VTEP；BMv2 算不了的 IPv6/UDP 校验和由 E 出口的一小段
> tc/eBPF（`bpf/vxlan_csum_shim.c`）补齐，相当于网卡 checksum offload。
> SRv6 BE/TE、L2 over SRv6 不变。主线 `feature/parallel-network` 保持
> “PM 直接用 VLAN 接入”，两者不合并。
>
> 运行方式与主线相同：`make parallel-run-min` / `make verify-min`
> （`parallel-run*`、`verify-*` 和 `all` 都会自动编译 `parallel-bpf`）；
> 切换版本：`feature/parallel-network`（PM 直接用 VLAN）、
> `feature/qinq-access`（Linux VTEP + QinQ，备选）或
> `feature/vxlan-e-shim`（当前，VXLAN 方案实现）。

本分支只包含 **中关村主干网并行网络仿真**。

当前范围：

- 41 个站点；
- 每站点 `C/E/A/S1/S2` 和两台云服务器，每台两个 VM 端点（宿主机 bridge + VXLAN）；
- 4 个并行网络（2 个 L3VPN + 2 个 L2VPN）；
- 站点 PE（`E`）使用 BMv2/P4；
- 接入交换机（`S1`/`S2`）使用 OVS 二层交换；
- SRv6 L3VPN/L2VPN；
- L2VPN BUM ingress replication；
- C2/FIX 公网静态接入；
- EVPN 路由模型和动态路由 agent。

真实 BGP/EVPN 控制面尚未接入，SID、C 路由和公网路由仍以静态配置为主。

## 分支

```text
feature/vxlan-e-shim        VXLAN 方案实现：PM 用 VXLAN 接入，E 直接终结
                            + tc/eBPF checksum shim（本分支）
feature/qinq-access         备选：Linux VTEP 终结 VXLAN，VTEP↔E 用 QinQ
feature/parallel-network    基线：PM 直接用 VLAN 接入
```

三条分支拓扑相同（41 站点、41 台 BMv2 + 82 台 OVS），只差接入段。

## 快速开始

### 环境要求

- Linux；
- 802.1Q VLAN 支持，必要时执行：

```bash
sudo modprobe 8021q
```

- Mininet 2.3+；
- Open vSwitch（`ovs-vsctl`、`ovs-vswitchd`）；
- p4c（`p4c-bm2-ss`）；
- bmv2（`simple_switch`、`simple_switch_CLI`）；
- 内核支持 tc `clsact` + BPF（`CONFIG_NET_CLS_BPF=y`，主流发行版默认满足）；
- clang（带 BPF target）和 libbpf 头文件，用于编译 checksum shim：

```bash
clang --version          # 需要支持 -target bpf
ls /usr/include/bpf/bpf_helpers.h
```

缺少 clang 时 `make parallel-run*` / `make verify-*` 会停在 `parallel-bpf`
编译步骤；此时只能用 `access_vtep=true` 的配置（不需要 shim）。

### 4 站点最小验证

推荐先启动 4 个站点验证全部功能：

```bash
make parallel-stop
make parallel-run-min
```

规模：

```text
4 个站点：北京、上海、天津、济南
4 台 BMv2（每站点 E）
8 台 OVS（每站点 S1/S2）
18 个 Linux netns
```

这 4 个站点在 FITI 实际拓扑中组成连通路径：

```text
北京 — 天津 — 济南 — 上海
```

对应命令：

```bash
make parallel-dump-min
make parallel-evpn-dump-min
```

### 41 站点全量启动

```bash
make parallel-stop
make parallel-run
```

规模：

```text
41 个站点
41 台 BMv2（每站点 E）
82 台 OVS（每站点 S1/S2）
166 个 Linux netns（每站点 2 台云主机）
246 个宿主机 bridge、656 对 tap veth、328 个 VXLAN 设备
（每台 PM：3 个 bridge、8 对 tap veth、4 个 VXLAN，实测见 VERIFICATION.md）
```

Thrift 端口段被占用时会自动向后寻找空闲端口段，也可以显式指定起始端口：

```bash
sudo python3 parallel_net.py --thrift-base 15000
```

### 停止

```bash
make parallel-stop
```

注意：`parallel-stop` 里的 `mn -c` 会删除本机**所有** OVS bridge。
如果这台机器上还跑着别的 OVS 实验，请先确认再执行。

不要同时运行：

```text
make parallel-run
make parallel-run-min
```

## 拓扑和站点

每个站点：

```text
 / E -- S1 \ 
C         cloud × 2
 \ A -- S2 / 
```

- `E`：BMv2/P4，负责 VRF、L3VPN、L2VPN、SRv6 和 BUM 复制；
- `S1`：OVS，上行放行并行 VLAN；
- `S2`：OVS，上行拒绝并行 VLAN；
- `C`、`A`：Linux netns；
- `cloud`：每站点两台（`cl_<站点>_1`、`cl_<站点>_2`），每台一个宿主机 bridge、两个 VM 端点；
- `pub_border`、`fix`：北京公网边界。

站点之间的核心链路直接使用 FITI 实际拓扑：

```text
configs/fiti.json
41 个节点，43 条链路
```

每个并行网络站点对应一个 FITI 节点，站点 C 路由器按照 FITI 的实际链路
互联，C 路由按 FITI 图的最短路径计算下一跳。

例如北京到上海在最小验证拓扑中经过天津和济南。

## 云主机与宿主机 VXLAN

每站点两台云服务器（PM），两台都在同一台物理机上。每台 PM 内部用
一个开启 VLAN 过滤的 Linux bridge 顶替物理机里的 OVS：每个虚机一条
veth（VM 侧持地址/MAC，bridge 侧是 access 口），每个网络一个 VXLAN
设备，隧道对端是本站点的另一台 PM——两个 VTEP 都在本机，报文在
veth 上转一圈，不产生站外流量。

```text
cl_bj_1（PM-A，带业务上联）                cl_bj_2（PM-B，只有主机 overlay）
├── eth0 -> S1 trunk                       ├── eth1 -> cl_bj_1-eth1（host underlay）
├── eth1 -> cl_bj_2-eth1（underlay）        ├── br_bj_2
├── br_bj_1                                 │   ├── tp_bj_2_1_1 -> ep_bj_2_1_1  ::20
│   ├── tp_bj_1_1_1 -> ep_bj_1_1_1  ::10    │   └── vx_bj_2_1  (VNI 1)
│   ├── tp_bj_1_2_1 -> ep_bj_1_2_1  ::11    │
│   ├── vx_bj_1_1  (VNI 1)                  │
│   └── eth0        (trunk，VLAN 3501-3504)  │
└── ...
```

```text
VM 侧（ep_*）      : 业务地址 + 独立策略路由表，MTU 1400
access 口（tp_*）  : bridge 口，pvid = 业务 VLAN，untagged
宿主机桥（br_*）   : vlan_filtering=1，静态 FDB 固定本机与对端 VM 的 MAC
VXLAN（vx_*）      : VNI = 网络 ID，local/remote = fd00:0:{站点}:9::1/::2
上联               : PM-A 的 eth0 直接进桥，由桥按 VID 打 tag（单层 802.1Q）
```

转发路径：

| 流量 | 路径 | 线上报文 |
| --- | --- | --- |
| 同站点 VM → VM（跨 PM） | tap → bridge → VXLAN → 对端 bridge → tap | VXLAN（VNI = 网络 ID） |
| 同 PM 的两个 VM | tap → bridge → tap | 普通以太网，不出桥 |
| L3 VM → 其它站点 | tap → bridge → VXLAN（VNI = 网络 ID）→ S1 → E（解封装）→ SRv6 | 段内 VXLAN（underlay 是 VLAN 4000），跨站点 SRv6 |
| L2 VM → 其它站点 | tap → bridge → VXLAN（VNI = 网络 ID）→ S1 → E（解封装）→ SRv6 | 段内 VXLAN（underlay 是 VLAN 4000），跨站点 SRv6 |
| 其它站点 → VM | E 解 SRv6 后再封 VXLAN 回 PM | 同上 |

两种 VPN 的段内封装现在都是 VXLAN（VNI = 网络 ID），区别只在 **E 拿 VNI 干什么**：

- **L3VPN**：`vni_map` 的 `map_l3` 把 VNI 映射成 VRF，按内层目的地址查 `l3_fib`
  做 SRv6 封装；每台 PM 都有 `u4000` 接入 underlay、各自建 VXLAN 到 E，
  同站点和跨站点的 L3 流量都不必先绕到另一台 PM。
- **L2VPN**：`vni_map` 的 `map_l2_vsi` 把 VNI 当成 VSI，`l2_tunnel`/`l2_bum`
  用同一把键查远端 SID，再整帧套进 SRv6（`next header = Ethernet`）。
  L2 的目的 MAC 是远端 VM 自己（不是网关），所以只有 PM-1 建接入 VXLAN
  （`ax_*`），并把“未知单播/广播”的 FDB 兜底指向 E；PM-2 的站外 L2 流量先经
  站点内 VXLAN 到 PM-1 再出站点，避免 S1 上出现重复帧。

站点内 PM↔PM 的 L2 隧道和接入隧道会在同一 netns 里同时存在，而内核不允许
两个 VXLAN 设备复用同一个 VNI，所以站点内那段用 `0x800000 | 网络 ID`
（只在本站点两台 PM 之间有意义）。

跨站点这一段两种 VPN 都交给 E 做 SRv6：L3 是 SRH + 内层 IPv6，
L2 是 SRH + `next header = Ethernet`，所以“跨站点 SRv6”对两类流量都成立。

地址和 MAC：

```text
L3：站点 /64 内，主机 h 端点 e -> ::{0x10 + (h-1)*0x10 + (e-1)}
L2：扁平 /64 内，::{0x100 + ((站点-1)*主机数 + (主机-1))*端点数 + (端点-1)}
MAC：02:{站点}:{网络高}{网络低}:{主机}:{端点}
underlay：fd00:0:{站点}:9::1（PM-A）/ ::2（PM-B）
```

- 每个端点有独立的策略路由表和源地址规则，报文按源地址选表；
- 端点地址是 `/64` 加 `noprefixroute`，前缀路由放在各自的策略路由表里；
- 出站点为 VLAN → SRv6；L2VPN 隧道内层不带业务 VLAN，对端 E 解封装后
  按自己的 VSI 重新打上（与真实 `dot1q` 子接口一致）；
- VLAN 子接口不再存在，上联是桥的 trunk 口，避免“桥 + 子接口”各打一次 tag；
- VXLAN 外层 IPv6 继承内层 DSCP（`tos inherit`），与 SRv6 段一致；
- 桥、tap、VXLAN 口都关闭 IPv6，避免 RS/MLD 噪声被当成 BUM 复制进隧道；
- underlay veth 关闭 tx 校验和卸载，抓包看到的 UDP 校验和是真实值。

## E 直接终结 VXLAN + checksum shim（access_vtep=false）

```text
PM --VXLAN(VNI = 网络 ID)--> S1 --> E(BMv2)：P4 解封装 → VRF → SRv6
                      ↑ tc/eBPF shim：重算 IPv6/UDP 校验和
```

- PM 侧：**L3 和 L2 的接入段都是 VXLAN**（VNI = 网络 ID），underlay 走 VLAN 4000，
  隧道对端是 **E**（`fd00:0:{站点}:8::1`）而不是 Linux VTEP；
  L3 是每台 PM 各自接入，L2 只由 PM-1 接入（`ax_*` 设备）；
- E 用 `vni_map` 把 VNI 映射成 VRF（`map_l3`）或 VSI（`map_l2_vsi`），
  `vxlan_access_decap()` 把内层帧搬回主头：L3 查 `l3_fib`、L2 查 `l2_tunnel`；
  反方向 L3 由 `srv6_decap_vxlan`、L2 由 `srv6_l2_decap_vxlan`
  解 SRv6 后再封 VXLAN 交回 PM；
- **校验和**：BMv2 的 `ComputeChecksum` 阶段看不到 deparser 之后拼好的字节，
  P4 只能把 UDP checksum 填 0；IPv6 下 0 非法，PM 内核直接丢包。
  因此在 E 的 S1 出口挂一段 tc/eBPF（`bpf/vxlan_csum_shim.c`，clsact egress），
  在报文成型后按伪首部重算并写回，等价于网卡 checksum offload。

```bash
make parallel-bpf        # clang -target bpf 编译 shim
make parallel-run-min    # 启动时自动给每台 E 挂上
```

shim 只处理 `Eth [+VLAN/VLAN] + IPv6 + UDP(dport 4789)`，其余流量原样放行；
它同时兼容单层 tag 和 QinQ，所以两条接入路线可以共用。确认是否挂上：

```bash
tc filter show dev E_bj-eth2 egress     # 应看到 vxlan_csum_shim.o:[tc] direct-action
```

验证时也可以在 PM 侧抓包，E→PM 的 VXLAN 应为 `[udp sum ok]`。

踩过的坑：`vxlan_t` 里曾多一个 `bit<32> pseudo_len`（早期算校验和时留下的），
使 VXLAN 头变成 12 字节，解析整体偏移 4 字节、内层 IPv6 根本出不来。
用 P4 `register` 探针把 `inner_ethernet.etherType` 读出来（得到 `0x4372` 而不是
`0x86dd`）才定位到。删掉该字段后 VXLAN 头恢复 8 字节。

## 备选：Linux VTEP 终结 VXLAN（access_vtep=true）

`access_mode=vxlan` 且 `access_vtep=true` 时，L3VPN 的接入段交给本站点的
Linux VTEP 终结，再以 QinQ 交给 E：

```text
PM --VXLAN(VNI = 网络 ID)--> V_<站点>(Linux VTEP) --[3500][切片 VLAN]--> S1 --> E(BMv2) --SRv6-->
```

- PM 侧：每个 L3 网络一个 VXLAN 设备（`vx_*`），VNI = 网络 ID，
  underlay 走 `u4000`（VLAN 4000），FDB 指向本站点 VTEP 和另一台 PM；
- `V_<站点>`：Linux netns，两条上联到 S1——`eth0` 跑 underlay（`u4000`），
  `eth1` 物理口不进桥，只在它上面叠一个外层子接口 `qinq3500`，再把该子接口
  作为桥端口按内层 VID 打 tag。于是出物理口就是 `3500 + <切片 VLAN>` 的
  双层 tag（QinQ），入方向 `3500` 被子接口剥掉、桥再按内层 VID 转给对应 VXLAN；
- E：接入口同时接受单层 tag（L2VPN 直连 PM）和双层 tag（经 VTEP 的 L3VPN），
  `vlan_map` 用**内层** VLAN 查 VRF/VSI；出方向只对登记过的 L3 切片压外层 3500，
  L2/核心/公网方向保持原样，SRv6（含 L2 over SRv6、TE waypoint）一行未改；
- L2VPN 也改成 VXLAN 接入（VNI = 网络 ID），不经过 VTEP。

### 为什么用 QinQ

单层 VLAN 只有 12 bit，切片标识最多 4094 个（还要扣掉 underlay 4000 和余量，
实际规划 400 个）。QinQ 把「接入域（外层）」和「切片（内层）」分开编码：

| 段 | 标识 | 容量 |
| --- | --- | --- |
| PM ↔ VTEP | VXLAN VNI | 24 bit |
| VTEP ↔ E | QinQ 外层 3500 + 内层切片 VLAN | 每个外层 4094 个切片 |
| E 内部 | 内层 VLAN → VRF/VSI（`vlan_map`）；前缀 → VRF（`vrf_map`） | 4096 / 16384 条 |

当前外层固定为 3500（`qinq_outer`），单个外层够 4094 个切片；再往上扩就需要
「多外层」：换一个外层值再多 4094 个。但要注意三个联动点——`ovs.NON_PARALLEL_VLANS`
的拒绝区间要跟着扩、E 的 `vlan_map` 键要从「内层 VID」改成「(外层, 内层)」、
VTEP 也要多建一个外层子接口。因此当前实现只启用单外层 3500。

为什么会有两条路线：BMv2 是软件数据面，**不会计算 IPv6 UDP 校验和**
（IPv6 下校验和为 0 会被内核丢包）。VTEP 路线把终结放到内核里绕过它；
本分支的默认路线（上一节）则在 E 的出口挂 tc/eBPF 重算校验和。
两者的数据面等价，只是“谁来算校验和”不同：

- **E 直接终结 + shim**（默认，`access_vtep=false`）：少一跳、少一套 VTEP 进程；
- **VTEP + QinQ**（备选，`access_vtep=true`）：不需要在 E 侧挂 tc/eBPF，
  适合不允许改动 E 宿主环境的场景。

配置字段：

```json
"access_mode": "vxlan",
"access_vtep": false
```

- `access_vtep=false`（默认）：E 自己终结 VXLAN，需要 `make parallel-bpf` 的 checksum shim；
- `access_vtep=true`：Linux VTEP 终结 VXLAN，VTEP↔E 用 QinQ（见“备选”一节）。

## 接入交换机（OVS）

- `S1` 上行放行并行 VLAN 3501-3900；
- `S2` 上行拒绝并行 VLAN 3500-3900（3500 是 QinQ 外层），其余 VLAN 正常；
- VLAN 转发、MAC 学习、广播泛洪由 OVS 完成；
- 接入交换机不再下发静态 `l2_fib`，也不再创建 multicast group；
- 只有 `E` 运行 P4 并占用 Thrift 端口。

查看：

```bash
sudo ovs-vsctl show
sudo ovs-vsctl get port S2_bj-eth1 trunks
sudo ovs-appctl fdb/show S1_bj
```

`S2` 的过滤是 OVS 端口级 VLAN 过滤，入向和出向都会生效。

## 并行网络配置

`configs/parallel.json` 包含 41 站点和 4 个并行网络：

```text
网络 1：L3VPN VLAN 3501
网络 2：L3VPN VLAN 3502
网络 3：L2VPN VLAN 3503
网络 4：L2VPN VLAN 3504
```

地址规划：

```text
业务 IPv6 = 240a:a806:{网络 ID}:{站点 ID}::/64
互联 IPv6 = 240a:a806:{网络 ID}:{100 + 站点 ID}::/64
```

每个网络可以用 `sites` 指定成员站点。当前扩展配置为了覆盖全部节点，
4 个网络都选择了全部 41 站点；非成员站点不会为它下发 VRF/前缀路由。

### 网络 ID 与业务 VLAN

- 网络 ID 上限是 **65535**（业务 IPv6 的第三段是 16 bit），实际还受接口名
  长度约束（`ep_<站点>_<主机>_<端点>_<ID>` ≤ 15 字节，4 位 ID 没问题）；
- **业务 VLAN 在 VXLAN 接入下不再需要**：`vlan` 缺省时自动按 `ID + 3500`
  分配，装不下（ID > 594）就置 0，表示该切片不使用业务 VLAN；
- `access_mode=vlan` 或 `access_vtep=true` 时仍然要求每个网络都有 VLAN，
  缺省会直接报错；显式写的 `vlan` 必须是 1-4094、不能重复，也不能用
  4000（保留给接入 underlay）；
- 例：`{"id": 600, "vpn": "l3", "sites": ["bj", "sh"]}` 在 VXLAN 接入下
  会得到 `vlan = 0`，实测同站点和跨站点都能通。

其他可选字段：

```json
"hosts_per_site": 2,
"endpoints_per_host": 2,
"endpoint_mtu": 1400,
"te_paths": [
    {"network": 1, "src": "bj", "dst": "sh", "via": "jnn"}
]
```

`te_paths` 是静态 SRv6 TE 路径：入口站点用两段 SRH（目的 SID + waypoint SID），
waypoint 站点的 E 做 `End`（`segmentsLeft--`、改写 DA）后再转发出去。
只支持 L3VPN、只支持一个 waypoint。

`configs/parallel-min.json` 是 4 站点验证子集：

```text
bj、sh、tjn、jnn
```

在 FITI 实际拓扑中连通的北京—天津—济南—上海路径。

## EVPN 路由模型

`parallel/evpn.py` 生成：

- EVPN IP Prefix 路由；
- EVPN MAC/IP 路由；
- EVPN Inclusive Multicast 路由；
- RD/RT、VNI、SRv6 SID。

输出：

```bash
make parallel-evpn-dump
```

41 站点结果：

```text
82 条 L3 IP Prefix
82 条 L2 IMET
共 164 条本地路由
```

Mininet 启动后每个端点一条 MAC/IP 路由：41 站点 × 2 主机 × 2 端点 × 2 个 L2
网络共 328 条，加上 82 条 L3 和 82 条 IMET，共 492 条。

## 动态路由 agent

`make parallel-run` 启动后会自动启动 agent：

```text
/tmp/parallel_evpn.sock
```

从项目目录执行：

```bash
cd ~/workspace/FITI_Mininet
python3 parallel_evpn_ctl.py status
python3 parallel_evpn_ctl.py dump
```

动态增加一条 L3 路由：

```bash
cat >/tmp/evpn-route.json <<'JSON'
{
  "type": "ip-prefix",
  "rd": "9425:1",
  "rt": ["9425:1"],
  "prefix": "240a:a806:1:5:abcd::/80",
  "vni": 1,
  "sid": "fd00:0:5:1::1:1",
  "origin": "xa",
  "next_hop": "fd00:0:5:1::1:1"
}
JSON

python3 parallel_evpn_ctl.py advertise --file /tmp/evpn-route.json
python3 parallel_evpn_ctl.py withdraw --file /tmp/evpn-route.json
```

支持：

- L3 IP Prefix 的 advertise/withdraw；
- L2 MAC/IP 已知单播的 advertise/withdraw；
- 路由校验和 RouteTable 回滚。

IMET/BUM multicast group 仍由静态控制面安装。

## 验证示例

### L3VPN（4 站点最小验证）

按源地址绑定（`-I <源地址>`），不要用 `v3501` 这类网卡名：
VLAN 子接口上没有地址，按网卡绑会直接 bind 失败。

```text
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 240a:a806:1:2::10
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 240a:a806:1:2::11
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 240a:a806:1:14::10
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 240a:a806:1:23::10
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 240a:a806:1:1::11
```

### L3VPN（41 站点全量）

```text
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 240a:a806:1:3::10
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 240a:a806:1:5::10
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 240a:a806:1:41::10
```

### L2VPN

```text
cl_bj_1 ping6 -c 3 -I 240a:a806:3:1::100 240a:a806:3:1::101
cl_bj_1 ping6 -c 3 -I 240a:a806:3:1::100 240a:a806:3:1::102
cl_bj_1 ping6 -c 3 -I 240a:a806:3:1::100 240a:a806:3:1::104
```

### BUM

链路本地组播必须绑网卡，不能绑源地址：

```text
cl_bj_1 ping6 -c 3 -I ep_bj_1_1_3 ff02::1
```

### 公网

```text
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 2001:255:0:100::2000
```

### SRv6 TE（经 waypoint）

```text
# 配置里 te_paths 指定 bj -> sh 经 jnn；在济南 C 上抓 SRv6 外层
C_jnn tcpdump -i any -nn 'ip6 and proto 43'
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 240a:a806:1:2::10
```

进 waypoint 时 `segleft=1`、DA 是济南 SID；出 waypoint 时 `segleft=0`、DA 改成上海 SID。

### 同站点 VXLAN

```text
# cl_bj_1 上抓 VXLAN，同时在 cl_bj_1 里 ping cl_bj_2 的端点
cl_bj_1 tcpdump -i any -nn 'udp port 4789'
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 240a:a806:1:1::20
cl_bj_1 bridge fdb show | grep 02:01:00:01:02:01
```

### 业务流量（TCP / HTTP）

```text
# 北京起服务端，上海作为客户端
cl_bj_1 iperf -s -V -B 240a:a806:1:1::10 -p 5201
cl_sh_1 iperf -c 240a:a806:1:1::10 -V -B 240a:a806:1:2::10 -p 5201 -t 3

cl_bj_1 sh -c 'cd /tmp/httpd && python3 -m http.server 8081 -b 240a:a806:1:1::10'
cl_sh_1 curl -6 -o /dev/null http://[240a:a806:1:1::10]:8081/f.bin
```

### 跨网络隔离（负向测试）

用最小验证拓扑里的上海站点：

```text
# 网络 2 内部：应成功
cl_bj_1 ping6 -c 3 -I 240a:a806:2:1::10 240a:a806:2:2::10

# 从网络 1 访问网络 2：应 100% 丢包
cl_bj_1 ping6 -c 3 -I 240a:a806:1:1::10 240a:a806:2:2::10
```

隔离由两处共同保证：

- 云主机按源地址选择 `20000+网络 ID` 的策略路由表，不会把流量切到别的切片；
- E 按 VLAN 映射到不同 VRF，VRF 1 里没有网络 2 的业务前缀，直接丢弃。

## 常用命令

```bash
make parallel-p4              # 编译 P4
make parallel-bpf             # 编译 VXLAN checksum shim（tc/eBPF）
make parallel-dump            # 41 站点拓扑
make parallel-dump-min        # 4 站点拓扑
make parallel-evpn-dump       # 41 站点路由模型
make parallel-evpn-dump-min   # 4 站点路由模型
make test-evpn                # EVPN 路由模型测试
make test-ovs                 # OVS 接入交换机配置测试
make test-verify              # 验收用例和配置渲染测试
make test                     # 全部单元测试

make verify-dry               # 免 root，校验用例和配置
make verify-min               # 4 站点功能验收
make verify-full              # 41 站点功能验收
make render-config            # 重新生成真实设备配置基线

make parallel-run             # 41 站点，41 台 BMv2 + 82 台 OVS
make parallel-run-min         # 4 站点，4 台 BMv2 + 8 台 OVS
make parallel-run-no-cli      # 41 站点后台模式
make parallel-stop            # 清理
```

## 功能验收

接入真实平台前的功能验证，判定 pass / fail，不做性能测量：

```bash
make parallel-stop
make verify-dry
make verify-min      # 4 站点，44 条用例
make verify-full     # 41 站点
```

覆盖：

- 同网络互通、跨网络隔离；
- S1 放行、S2 拒绝并行 VLAN；
- P4 表项（`local_sid`、`l3_fib`、`l2_tunnel`）；
- 云主机策略路由和 C 路由器 underlay 路由；
- EVPN agent 动态增删路由；
- 链路 down/up（默认跳过，`--tag faults`）。

运行时会逐条显示 `PASS`/`FAIL`/`SKIP`、spinner 和 ETA；非 TTY 环境自动关闭动画。
报告写到 `runs/<时间戳>/`，失败用例会附带表项和路由 dump。
详见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。

### 路径映射

从同一份配置生成 overlay 业务路径和 underlay 物理路径对照表：

```bash
python3 tools/path_map.py --config configs/parallel-min.json \
    --network 1 --src bj_1 --dst sh_1

# 同站点两台主机
python3 tools/path_map.py --config configs/parallel-min.json \
    --network 1 --src bj_1 --same-site

# 所有网络
python3 tools/path_map.py --config configs/parallel-min.json \
    --network all --src bj_1 --dst sh_1
```

输出逐段列出物理设备、封装（VLAN / SRv6 L3 / SRv6 L2 / IPv6 外层）
和说明。同站点显示 S1 二层转发，跨站点显示 VLAN → SRv6 → VLAN。
基线文件：`configs/expected/path-map-min.md`。

配置同源：仿真表项和真实设备 CLI 都由 `configs/parallel*.json` 生成。

```bash
python3 tools/render_config.py --config configs/parallel-min.json --target huawei
python3 tools/render_config.py --config configs/parallel-min.json --target sim
python3 tools/render_config.py --config configs/parallel-min.json --target ovs
```

## 目录结构

```text
fiti/                       共享 Mininet/P4 辅助
├── log.py                  日志
├── p4_mininet.py           P4Host / P4Switch
└── paths.py                项目路径

parallel/                   并行网络实现
├── bmv2.py                 Thrift CLI 辅助
├── config.py               站点、网络、VLAN、IPv6
├── topology.py             C/E/A/S1/S2/cloud（E 为 BMv2，S1/S2 为 OVS）
├── switch.py               BMv2 交换机封装
├── ovs.py                  S1/S2 的 OVS VLAN 配置
├── evpn.py                 EVPN 路由模型
├── evpn_agent.py           动态路由 agent
├── control.py              E 的静态 P4 表项
├── runtime.py              Mininet 构建
└── p4.py                   P4 编译和名称解析

bpf/
└── vxlan_csum_shim.c       模拟网卡 checksum offload：E 终结 VXLAN 时补 UDP 校验和

configs/
├── fiti.json               FITI 实际拓扑数据（41 节点/43 链路）
├── parallel.json           41 站点配置
├── parallel-min.json       4 站点验证配置
└── expected/
    └── huawei-min.txt      真实设备配置基线（golden）

verify/                     功能验收
├── runner.py               用例执行器
├── spec.py                 用例加载与校验
├── report.py               报告与 JUnit
└── cases/                  验收用例

tools/
├── render_config.py        仿真/设备/OVS 配置渲染
└── path_map.py             overlay/underlay 路径映射

docs/
├── PARALLEL_NETWORK_DESIGN.md
├── VERIFICATION.md
└── REQUIREMENTS.md         需求文档（并行网络 + UNIT bitmap 合并）

p4src/parallel_vpn.p4       VLAN + L3/L2VPN + SRv6 + BUM
parallel_net.py             并行网络 CLI
parallel_evpn.py            EVPN 路由模型 CLI
parallel_evpn_ctl.py        agent 客户端
```

## 已知限制

- 当前是 4 个静态网络，不是 需求文档的 400 个网络；
- 4 个网络当前覆盖全部 41 站点，尚未按需求文档精确配置网络成员；
- SID、C 路由和公网路由仍是静态配置；
- 没有真实 BGP/EVPN 会话、Route Reflector 或 EVPN NLRI 编解码；
- L2 BUM multicast group 是静态生成的；
- VXLAN 回退模式（L2 over VXLAN）只支持已知单播，且 E↔E 这段走核心口、没有 checksum shim 覆盖，目前未启用；
- SRv6 TE 目前是静态配置（`te_paths`），只支持 L3VPN 和单个 waypoint；
- 动态 agent 只支持 L3 IP Prefix 和 L2 MAC/IP 已知单播；
- 接入交换机是 OVS，L2 转发靠 MAC 学习，不是 EVPN 下发的静态 FIB；
- 41 台 BMv2 仍然占用较多资源，全量启动较慢；
- 每站点 2 主机 × 2 端点会产生 166 个 netns、246 个 bridge、656 对 tap veth、
  328 个 VXLAN 和 656 张端点路由表；41 站点全量的资源占用见 VERIFICATION.md；
- 同站点跨 PM 的流量走 VXLAN（VNI = 网络 ID），两个 VTEP 都在本机；
- L2 远端主机之间依赖 ND 解析，不再逐主机下发静态邻居；
- SRv6 封装有额外开销（L3 64B、L2 78B），端点 MTU 默认 1400（`endpoint_mtu`），
  超过端点 MTU 的报文在主机侧就被拒绝，不会被隧道黑洞；

## 故障排查

### 端口占用

```bash
make parallel-stop
sleep 10
```

不要在全量实例仍在运行时重复执行启动命令。

检查实际端口：

```bash
sudo ss -tanp | grep ':端口号'
sudo lsof -nP -iTCP:端口号
```

### 启动报 `File exists`

原因是上一次实例没有完全清理，留下了 `A_bj-eth1`、`S2_bj-eth1` 一类接口：

```text
Error creating interface pair (A_bj-eth1,S2_bj-eth1): RTNETLINK answers: File exists
```

现在的处理：

- `make parallel-run*` 启动前会自动删除残留接口；
- 如果检测到另一份 `parallel_net.py` 还在运行，会直接拒绝并打印 PID；
- `make parallel-stop` 会先等 CLI 正常退出（最多 20 秒），再升级到 KILL。

手动检查：

```bash
sudo python3 -m parallel.cleanup --dry-run   # 只列出残留接口
sudo python3 -m parallel.cleanup             # 删除
```

### `Network is unreachable`

```text
cl_bj_1 ip -6 addr show ep_bj_1_1_1
cl_bj_1 ip -6 rule show
cl_bj_1 ip -6 route show table 20016
cl_bj_1 ip -6 route get 240a:a806:1:2::10 from 240a:a806:1:1::10
```

按源地址查表，所以 `route get` 和 `ping` 都要带 `from` / `-I <源地址>`；
只写 `ip -6 route get <目的>` 会走主表，看不到端点自己的表。

### `ip link add ... type vxlan` 报 `Unknown device type`

内核模块没有加载。非初始 namespace 里的设备创建不会触发模块自动加载，
所以启动脚本会先 `modprobe`。手动确认：

```bash
sudo modprobe vxlan
lsmod | grep vxlan
```

### 上联出现双层 VLAN（QinQ）

说明桥的 trunk 口又套了一层 VLAN 子接口。上联必须是**原设备**（`eth0`）
直接进桥，由桥按 VID 打 tag；桥和子接口各打一次就会出现两层 802.1Q，
E 的 `set_trunk` 会把这种帧丢掉。

### `ping` 报 `bind: Cannot assign requested address`

端点地址还在 IPv6 重复地址检测（tentative）阶段。等 1-2 秒即可；
`verify/runner.py` 会自动等所有端点离开 tentative 再开始跑用例。

### 启动报 `name not a valid ifname`

站点简称太长：Mininet 的接口名是 `<节点名>-ethN`，上限 15 字节。
`cl_<站点>_<序号>-eth0` 超长时会在建 veth 时失败（例如 6 个字母的简称）。
`configs/*.json` 加载时会提前校验并报错，遇到时把站点简称改短。

### agent 连接失败

```bash
cd ~/workspace/FITI_Mininet
python3 parallel_evpn_ctl.py status
```

或：

```bash
python3 /home/mxd/workspace/FITI_Mininet/parallel_evpn_ctl.py status
```

### OVS 异常

如果启动时报：

```text
Error connecting to ovs-db with ovs-vsctl
```

说明 `ovs-vswitchd` / `ovsdb-server` 没运行：

```bash
pgrep -af ovs-vswitchd
sudo systemctl restart openvswitch-switch
sudo ovs-vsctl show
```

查看接入交换机的 VLAN 和 MAC 表：

```bash
sudo ovs-vsctl get port S2_bj-eth1 trunks
sudo ovs-appctl fdb/show S1_bj
```

### 并行 VLAN 从 S2 泄漏

确认 S2 上行的 trunk 列表里没有 3501-3900：

```bash
sudo ovs-vsctl get port S2_bj-eth1 trunks
```

残留配置会随 `make parallel-stop` 里的 `mn -c` 清除。

### VLAN 子接口不存在

```bash
sudo modprobe 8021q
make parallel-stop
make parallel-run-min
```
