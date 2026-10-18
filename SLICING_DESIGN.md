# FITI bmv2 网络切片设计说明

状态：已实现（bmv2 + P4 VLAN 多租户 + 可选 SRv6 主机 overlay）。
节点数据平面已从 OVS 改为 bmv2 `simple_switch`；OVS standalone / STP、
外部 OpenFlow 控制器模式不再使用。

配套文件：

- `p4src/fiti_slice.p4`：VLAN 感知 L2 转发 P4 程序
- `fiti_p4.py`：P4 编译、生成树、端口映射、流表下发与运行检查
- `p4_mininet.py`：Mininet 的 `P4Host` / `P4Switch`
- `fiti_slice.py`：切片实现、CLI 与库接口
- `fiti_traffic.py`：随机通信模式（不同节点随机互 ping）
- `fiti_log.py`：控制台摘要与详细日志分离
- `fiti_topo.py`：基础拓扑与库接口
- `slices.json`：切片配置
- `README.md`：安装、命令和配置字段说明

## 1. 范围

目标：

- 41 个 FITI 节点全部由 bmv2 `simple_switch` 实现
- 每台交换机加载同一个 P4 程序，做 VLAN 感知的二层转发
- 同一台交换机可同时承载多个切片的主机（每节点多切片）
- 每个切片有独立主机、IPv4 子网和 VLAN
- 切片内互通，切片间默认不可达
- 切片内可选启用 SRv6 主机 overlay
- 不依赖外部控制器；启动时通过 Thrift 静态下发规则

非目标（当前版本明确不做）：

- OVS / STP / OpenFlow 控制器模式
- 每切片链路带宽保障
- 每切片独立控制器
- 交换机逐跳 SRv6
- 动态 MAC 学习、动态切片增删
- 5G 端到端切片编排、FlowVisor 兼容

## 2. 架构

- 物理层复用 `fiti.json` 的 41 个节点和 43 条骨干链路
- 每个节点运行一台 bmv2 `simple_switch`，进程名 `s<节点id>`
- 每个节点的主机接在该交换机的 access 端口上，端口配置为对应切片的 VLAN
- 交换机之间为 802.1Q trunk，承载所有切片 VLAN
- 控制面 `fiti_p4.py` 预计算一棵 BFS 生成树，只启用树边
- 每台交换机为每个 VLAN 创建一个多播组，用于广播/未知单播泛洪
- 单播表项按 `(VLAN, 目的 MAC)` 静态下发
- SRv6 只在主机命名空间内封装/解封装，bmv2 不感知 SRH

```text
 nor_bj ─┐  access VLAN 100          trunk (VLAN 100..104)          ┌─ eas_sh
 demo_bj ─┤                                                        ├─ demo_sh
          ├─ s1 ═════ s2 ═════ s3 ═════ ... ═════ s41 ────────────┤
 sou_gz ──┘                                                        └─ wes_xa
            每台 s<id> 都是 bmv2 simple_switch + fiti_slice.p4
```

bmv2 没有 STP。`fiti_p4.py` 以 `--spanning-root`（默认第一个节点，即 `s1`）
为根做 BFS，得到 40 条树边和 3 条非树边。以 `s1` 为根时非树边为：

```text
s16 - s5
s17 - s7
s15 - s4
```

非树边两端的端口不写入 `port_cfg`，由表的默认动作 `drop` 丢弃；同时也不加入
任何多播组。这样所有广播/未知单播只沿生成树传播，不会形成环路。

## 3. 命名与地址

| 对象          | 规则                                             |
| ------------- | ------------------------------------------------ |
| 交换机        | `s<节点id>`，41 台，共享                         |
| 基础主机      | 节点 `abbr`，IP `10.0.0.<节点id>/24`，VLAN 100   |
| 切片主机      | `<切片id>_<abbr>`，如 `nor_bj`、`demo_bj`        |
| 切片主机 IPv4 | `10.<切片序号>.0.<节点id>/24`                    |
| VLAN          | 1–4094，切片间唯一，默认从 100 起                |
| bmv2 端口     | 由 Mininet 分配，控制面从 `intf.link` 反查端口号 |
| Thrift 端口   | `base + 节点id`，默认 base=9090，自动避让占用    |
| SRv6 underlay | `2001:db8:<切片序号>::<节点id>/64`               |
| SRv6 SID      | `2001:db8:<切片序号>:<节点id>::1/128`            |
| SRv6 overlay  | `fd00:<切片序号>::<节点id>/128`                  |

主机名同时受 Linux 接口名 15 字符限制（主机侧接口为 `<host>-eth0`），配置校验会检查。

## 4. 配置模型

`slices.json` 的核心字段：

- 全局：`topo`、`datapath`（固定 `bmv2`）、`isolation`（固定 `vlan`）、`p4`、`defaults`、`srv6`
- 切片：`id`、`name`、`vlan`、`subnet`、`hosts`
- `p4`：`json`、`thrift_base`、`log_level`、`log_dir`、`pcap`、`pcap_dir`、`spanning_root`

校验规则：

- `id`、`vlan`、`subnet` 全局唯一，子网不得重叠
- `hosts` 必须是合法节点 `abbr` 列表或 `"all"`；同一 `abbr` 可出现在多个切片中
- VLAN 必须在 1–4094，主机 IP 必须落在对应子网内
- 已放弃的字段 `controller`、`nodes`、切片级 `controller`、`bw_share` 会直接报错
- `bridge` 隔离模式、非 `bmv2` 数据平面会直接报错

字段细节和示例见 `README.md`。

## 5. P4 数据平面

`p4src/fiti_slice.p4` 使用 P4-16 / v1model，结构如下。

### 5.1 解析与 VLAN

- 解析 `ethernet_t`
- 若 `etherType == 0x8100`，继续解析 `vlan_t`（pcp / dei / vid / inner etherType）
- access 主机发出的帧不带 tag；trunk 上的帧必须带一个 802.1Q tag

### 5.2 Ingress 表

`port_cfg`（key = `standard_metadata.ingress_port`）：

| 动作               | 语义                                                       |
| ------------------ | ---------------------------------------------------------- |
| `set_access(vlan)` | 主机端口；帧必须无 tag，压入 VLAN tag，记录 `meta.vlan_id` |
| `set_trunk()`      | 节点间端口；帧必须带合法 VLAN tag，记录 `meta.vlan_id`     |
| `drop`             | 默认动作；未配置端口 / 被生成树阻塞的端口丢弃              |

`l2_fwd`（key = `meta.vlan_id` + `ethernet.dstAddr`）：

| 动作            | 语义                                   |
| --------------- | -------------------------------------- |
| `forward(port)` | 命中 `(VLAN, MAC)`，设置 `egress_spec` |
| `nop`           | 默认动作；保持 `egress_spec == 0`      |

`l2_fwd` 未命中时：

```text
standard_metadata.mcast_grp = meta.vlan_id
```

即多播组 id 直接使用 VLAN id。控制面在每台交换机上为每个 VLAN 创建对应的组。

### 5.3 Egress 表

`egress_port`（key = `standard_metadata.egress_port`）：

| 动作           | 语义                                                          |
| -------------- | ------------------------------------------------------------- |
| `set_access()` | access 端口；`ethernet.etherType = vlan.etherType`，剥掉 VLAN |
| `set_trunk()`  | trunk 端口；保留 VLAN tag                                     |
| `drop`         | 默认动作；未配置出端口丢弃                                    |

另外在 egress 检查：

```text
meta.ingress_port == standard_metadata.egress_port -> drop
```

多播组包含所有转发端口（包括入端口），这个检查去掉回灌副本。相比为
`(VLAN, ingress_port)` 组合分别建组，这种做法的控制面规则更少，且不依赖
bmv2 是否自动排除入端口的行为。

### 5.4 泛洪组

每台交换机、每个 VLAN 一个多播组：

```text
group(VLAN) = 活动树边 trunk 端口 + 该 VLAN 的本地 access 端口
```

- 组内包含入端口，回灌副本由 egress 自环抑制丢弃
- 非树边端口不加入任何组，因此不会承载广播
- 单播流量同样只走树边，避免出现与生成树不一致的转发路径

## 6. 控制平面

`fiti_p4.py` 在 `net.start()` 之后执行，流程如下。

### 6.1 编译 P4

```bash
p4c-bm2-ss --arch v1model -o build/fiti_slice.json p4src/fiti_slice.p4
```

若 JSON 已存在且不早于源文件，则跳过编译。`runtime_names()` 读取编译后的
JSON，解析 p4c 加过前缀的表名/动作名（如 `IngressImpl.port_cfg`、
`IngressImpl.set_access`、`EgressImpl.egress_port`），不写死名字。

### 6.2 端口映射

Mininet 创建 veth 后，`build_port_maps()` 遍历每台交换机的 `intfs`：

- 通过 `intf.link` 找到对端节点
- 对端是交换机 -> 记录 trunk 邻接关系
- 对端是主机 -> 记录 `host -> (switch, port)`

### 6.3 生成树

`compute_spanning_tree()` 以 `s1`（可配置）为根做 BFS：

- 返回 parent / children / depth / tree edges
- 非树边在 `build_rule_plan()` 中不生成 `port_cfg` / `egress_port` / 多播成员
- 单播下一跳由 parent 指针计算：若目的在源节点子树内则走对应子节点，
  否则走 parent

### 6.4 规则规划

`build_rule_plan()` 为每台交换机生成：

- `access_ports`: `[(port, vlan), ...]`
- `trunk_ports`: 活动树边端口列表
- `l2_entries`: `[(vlan, dst_mac, egress_port), ...]`，覆盖主机规划中的所有主机
- `groups`: `{vlan: [ports]}`

以 5 个切片 / 47 台主机为例，每台交换机约 47 条 `l2_fwd` 表项；
`port_cfg` / `egress_port` 表项数等于该交换机的 access + 活动 trunk 端口数。

### 6.5 Thrift 下发

`install_fabric()` 对每台交换机批量执行：

```text
table_add IngressImpl.port_cfg IngressImpl.set_access <port> => <vlan>
table_add IngressImpl.port_cfg IngressImpl.set_trunk  <port> =>
table_add IngressImpl.l2_fwd   IngressImpl.forward   <vlan> <mac> => <port>
table_add EgressImpl.egress_port EgressImpl.set_access <port> =>
table_add EgressImpl.egress_port EgressImpl.set_trunk  <port> =>
mc_mgrp_create <vlan>
mc_node_create <vlan> <ports...>
mc_node_associate <vlan> <node_handle>
```

`simple_switch_CLI` 即使动作没有参数也要求 `=>` 分隔符。多播 node handle 从
CLI 输出的 `node was created with handle N` 行解析，不能误用 `table_add` 输出里的
`Entry has been added with handle N`。

日志默认写入项目 `build/logs/`：

```text
build/logs/mininet.log        # 构建/启动的完整 Mininet 输出
build/logs/fiti-install.log   # P4 流表下发：每台交换机一行
build/logs/bmv2-s<id>-<base+id>-<uid>.log   # 每台 bmv2 自身日志
```

`*** Adding hosts:` 和 `*** Adding switches:` 按 Mininet 原样显示在控制台，
`*** Adding links:` 及链路明细被过滤。启动、流表下发和 SRv6 安装阶段显示
进度条 / 旋转动画：

```text
*** Creating network
*** Adding hosts:
bj buaa bupt cd chc ...
*** Adding switches:
s1 s2 s3 s4 s5 ...
*** 启动 bmv2 交换机 / (12.4s)
*** 41 台 bmv2 交换机已启动 (12.4s)
*** 下发 P4 流表 [##########----------] 21/41 (8.1s)
*** P4 流表下发完成 (8.1s)
```

原来的 `*** Adding links:`、`*** Starting 41 switches` 等 Mininet 输出、
以及逐台流表下发信息仍然完整写入日志文件，内容不变。可用 `p4.log_dir`
或 `--log-dir` 覆盖目录。日志目录不存在时会自动创建；若不可写，
`P4Switch` 会退回临时文件并在启动日志里打印实际路径。

### 6.6 运行检查

`access_vlan_errors()` 在每台交换机上执行 `table_dump IngressImpl.port_cfg`，
解析端口号、动作和 VLAN 参数，与规划比较。它取代了原来的
`ovs-vsctl get port ... tag` 检查。`--check` 还会做切片内 ping 和跨切片 ping。

## 7. 每节点多切片

P4 数据平面不假设「一个节点只属于一个切片」：

- 一台交换机可以有多个 access 端口，每个端口由 `port_cfg` 映射到不同 VLAN
- `l2_fwd` 的 key 包含 VLAN，因此同一台交换机内部不同切片的 MAC 表项互不影响
- 每个 VLAN 的泛洪组只包含该 VLAN 的本地 access 端口和 trunk 端口
- 跨切片帧在 ingress 就被分到不同 VLAN；即使目的 MAC 恰好相同也不会串切片

示例配置中 `demo` 切片与另外四个切片在 6 个节点上重叠：

| 节点      | 切片          |
| --------- | ------------- |
| `s1` 北京 | `nor`, `demo` |
| `s2` 上海 | `eas`, `demo` |
| `s3` 西安 | `wes`, `demo` |
| `s5` 广州 | `sou`, `demo` |
| `s7` 武汉 | `sou`, `demo` |
| `s8` 成都 | `wes`, `demo` |

例如 `nor_bj`（10.1.0.1）和 `demo_bj`（10.5.0.1）接在同一个 `s1` 的两个
access 端口上，VLAN 分别为 100 和 104。`nor_bj ping demo_bj` 会 100% 丢包，
但两者都能访问各自切片内的其他主机。

`--dump` 会打印「每节点承载切片数」和具体的节点/切片列表，便于确认重叠关系。

## 8. 随机通信模式

`fiti_traffic.py` 提供「不同节点随机互 ping」的流量模式，CLI 入口为
`--random-traffic`。

选择规则：

- 源/目的主机必须位于不同节点（`node_id` 不同）。
- `--traffic-scope slice`（切片模式默认）：只在同一 `slice_id` 内选流；
  先尽量保证每个切片至少一条流，再随机补齐。
- `--traffic-scope global`：所有主机一起随机选流，允许跨切片。
  同切片流期望可达，跨切片流期望被 VLAN 隔离（100% 丢包）。

执行方式：

```text
ping -c <duration/interval> -i <interval> -W 1 -s <size> <dst_ip>
```

多条流并发启动，结束后解析每条 `ping` 的 `packets transmitted / received`
摘要，输出逐流结果和汇总：

```text
  [nor] nor_bj(s1) -> nor_zhz(s25): 25/25 包, 0.0% 丢包, 期望=reachable PASS
```

`--traffic-flows`、`--traffic-duration`、`--traffic-interval`、
`--traffic-size`、`--traffic-seed` 分别控制流数、持续时间、发包间隔、
负载大小和随机种子。seed 省略时自动生成并打印，便于复现。

`--traffic-continuous` 开启持续模式：每轮持续 `--traffic-duration` 秒，
结束后用 `seed + 轮次` 重新随机选流，继续下一轮，直到 Ctrl-C。
每轮打印一行汇总，失败流单独打印；Ctrl-C 后打印累计流数/收发包/丢包。
若某轮 ping 因为不可达而提前结束，会补足到本轮时长，避免忙循环。
持续模式只累计 `TrafficTotals`，最多保留 100 条失败明细，不会随运行时间
无限增长内存。

该模式不修改 P4 流表，只是从主机侧发起真实流量；因此它同时验证了
VLAN 接入、`l2_fwd` 单播路径、生成树泛洪和跨切片端到端不可达。
它基于 `ping`，目标是连通性/丢包，不做吞吐压测。

## 9. SRv6 主机 overlay

仅在 `srv6.enabled=true` 或 `--srv6` 时启用。

封装：

- 每台主机配置 underlay 地址、SID 和 overlay 地址
- 到每个对端 SID 添加 `via <对端 underlay>` 路由
- overlay 前缀通过 `encap seg6 mode encap segs <对端 SID>` 封装

解封装：

- 本机 SID 配置 `encap seg6local action End.DT6 table 100`
- table 100 中只保留本机 overlay 的 `local` 路由和 `unreachable default`
- 防止 inner 包再次命中封装路由形成环路

可靠性处理：

- 关闭 IPv6 DAD（`accept_dad=0`），避免地址初始化期间丢包
- 预置静态邻居，避免首包 NDP 丢失
- 主机 MTU 设为 1420，为 SRv6 封装预留约 64 字节
- P4 只按外层以太网目的 MAC 转发；SRv6 overlay 与 VLAN 切片叠加
- `P4Host` 不关闭 IPv6，否则 SRv6 地址无法配置

能力边界：

- 只支持单段 SID，不支持交换机逐跳 SRv6（End/End.X/End.DX6）和多段路径
- 无 SRv6 控制面（IS-IS/OSPFv3/BGP SRv6），路由由 `SliceManager` 静态下发
- 每切片 O(N²) 条路由，建议每切片不超过 20 台主机

## 10. 容量估算

以下为工程估算，实际值需在本机压测：

| 项目                                | 估算                                                        |
| ----------------------------------- | ----------------------------------------------------------- |
| 交换机进程                          | 41 个 `simple_switch`，每个约 20–60 MB RSS，合计约 1–2.5 GB |
| 主机                                | 基础模式 41 台；示例切片 47 台（含 6 台重叠主机）           |
| 每交换机 `l2_fwd`                   | 主机总数（示例 47）                                         |
| 每交换机多播组                      | 切片数（示例 5）                                            |
| 每交换机 `port_cfg` / `egress_port` | access 端口 + 活动 trunk 端口（最大节点约 14）              |
| 总 Thrift 表项                      | 约 41 × (47 + 14 + 14) ≈ 3000 条                            |

主要瓶颈：bmv2 进程数量、主机 namespace/veth、内存；协议上限仍是 802.1Q 的
4094 个可用 VLAN。

## 11. 已知限制

- 生成树静态，非树边不转发；链路故障不会自动切换
- 单播表项静态下发，不做 MAC 学习；主机增删需要重新下发
- 泛洪在整棵生成树上扩散，没有按 VLAN 剪枝
- 所有切片共享物理链路带宽，没有每切片带宽保障
- 切片只隔离网络命名空间，CPU / 内存默认不隔离
- bmv2 只做 L2；SRv6 只在主机侧生效，不能验证 P4/Tofino 的逐跳 SRv6
- 不再支持 OVS standalone + STP、`ovs-testcontroller`、外部 OpenFlow 控制器

## 12. 验证方法

P4 编译与 Thrift 命令已在开发机验证：

```bash
make p4
python3 fiti_slice.py --slices slices.json --dump
```

P4 数据平面用 `simple_switch --use-files` + 手工 pcap 做过转发验证：

| 场景                           | 结果                                                             |
| ------------------------------ | ---------------------------------------------------------------- |
| access VLAN 100 广播           | 同 VLAN access 端口收到无 tag 帧；trunk 端口收到 VLAN 100 tag 帧 |
| 同交换机 access VLAN 100 / 101 | VLAN 100 的广播不会出现在 VLAN 101 的 access 端口                |
| 单播命中 `l2_fwd`              | 只从目的 access 端口发出，其他端口无副本                         |
| 入端口回灌                     | 多播组包含入端口，但 egress 自环抑制后入端口无输出               |
| trunk 收到 VLAN 101 但无对应组 | 丢弃，不泄露到 VLAN 100                                          |

完整 41 节点运行需要 root：

```bash
sudo python3 fiti_slice.py --slices slices.json --check
```

`--check` 自动检查：

| 检查项                 | 期望                                               |
| ---------------------- | -------------------------------------------------- |
| 切片内 IPv4 连通性     | 每个切片 0% 丢包                                   |
| P4 接入端口规则        | 每台交换机 access VLAN / trunk 端口与规划一致      |
| 跨切片 IPv4            | 所有切片两两 100% 丢包（包括同一节点上的不同切片） |
| 同节点多切片           | `nor_bj` / `demo_bj` 等共享节点的切片仍然互相隔离  |
| SRv6（`--check-srv6`） | 切片内 overlay 地址 0% 丢包                        |

随机通信验收：

```bash
# 切片内随机流：全部应为 0% 丢包
sudo python3 fiti_slice.py --slices slices.json --random-traffic

# 允许跨切片：同切片流 0% 丢包，跨切片流 100% 丢包
sudo python3 fiti_slice.py --slices slices.json \
    --random-traffic --traffic-scope global

# 持续随机发包，Ctrl-C 停止
sudo python3 fiti_slice.py --slices slices.json \
    --random-traffic --traffic-continuous
```

输出中每条流最后为 `PASS` 才符合预期；`--traffic-seed` 可固定随机序列。

手工验证：

```bash
# s1 的端口规则
simple_switch_CLI --thrift-port 9091
RuntimeCmd: table_dump IngressImpl.port_cfg
RuntimeCmd: table_dump IngressImpl.l2_fwd
RuntimeCmd: table_dump EgressImpl.egress_port
RuntimeCmd: mc_dump
```

## 13. 后续

若后续确有需求，可作为独立项目评估：

- 动态 MAC 学习（CPU 端口 + packet_in/packet_out，或 P4Runtime 控制器）
- 生成树/链路故障切换（快速重算树并重下发，或改用 ECMP）
- 按 VLAN 剪枝泛洪（为每个切片计算独立的转发树）
- 基于 P4 的每切片计量/限速
- 动态增删切片
- 逐跳 SRv6（End/End.X/End.DX6）与 P4Runtime 控制面
