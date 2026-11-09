# 并行网络功能验收

本文描述接入真实平台前的功能验证（conformance）流程。判定标准是 **pass / fail**，不是吞吐和时延；性能数字留给真实设备。

## 快速开始

```bash
make parallel-stop      # 清理旧实例
make verify-dry         # 免 root，只校验用例与配置
make verify-min         # 4 站点，43 条用例（故障用例需 --tag faults）
make verify-full        # 41 站点
```

报告写到：

```text
runs/<时间戳>/report.json
runs/<时间戳>/report.xml
runs/<时间戳>/evidence/<用例>.txt
```

`report.json` 的 `metadata` 记录配置路径、站点数、网络数、git commit、
是否 dirty、p4c/bmv2/OVS 版本和 Python 版本，便于事后追溯这份结果对应
哪个代码和配置。JUnit 报告里以 `<properties>` 形式提供同样的信息。

控制输出：

```bash
--no-progress    只在最后打印汇总
--quiet          逐条只显示失败的用例
--no-color       关闭颜色
--no-animate     关闭 spinner
```

只看某一类：

```bash
sudo python3 verify/runner.py --config configs/parallel-min.json --tag isolation
sudo python3 verify/runner.py --config configs/parallel-min.json --tag vlan
sudo python3 verify/runner.py --config configs/parallel-min.json --tag faults
```

故障用例默认不执行，需要 `--tag faults` 或 `--all`。

`link: down` 用 `tc netem loss 100%` 注入丢包，而不是 `ip link set down`：
IPv6 接口 down 之后地址和路由会被删除，up 也不会自动恢复，会把测试环境
永久留在不可达状态。netem 只影响转发，恢复时删掉 qdisc 即可。

## 用例覆盖

| 文件 | 内容 | 数量 |
| --- | --- | --- |
| `01-reachability.yaml` | L3VPN/L2VPN 互通、多端点、VXLAN、QinQ 双层 tag、E 侧 checksum shim、SRv6 TE、BUM、公网、隧道报文格式 | 20 |
| `02-isolation.yaml` | 跨网络必须 100% 丢包 | 4 |
| `03-vlan.yaml` | S1 放行、S2 拒绝并行 VLAN | 4 |
| `04-control-plane.yaml` | SID、FIB、OVS trunk、策略路由 | 9 |
| `05-dynamic.yaml` | EVPN agent 动态增删路由 | 2 |
| `06-faults.yaml` | 链路 down/up（默认跳过） | 1 |
| `07-traffic.yaml` | 真实 TCP/HTTP 传输、端点 MTU 边界 | 4 |

`requires_sites` 会在缺少站点的配置里自动跳过，例如全量用例在 `parallel-min.json` 下会显示 `SKIP`。

## 用例格式

```yaml
cases:
  - name: l3vpn-net1-bj-sh
    tags: [core, l3]
    requires_sites: [sh]
    steps:
      - type: ping
        node: cl_bj
        iface: ep_bj_1_1_1
        target: 240a:a806:1:2::10
        expect: pass
    teardown: []
```

支持的步骤：

| type | 字段 | 作用 |
| --- | --- | --- |
| `ping` | `node` `iface` `target` `expect` `count` `max_loss` `min_received` `retries` | 正向或负向连通性 |
| `exec` | `node` `command` `expect_contains` `expect_not_contains` | 任意命令断言 |
| `ovs_trunks` | `switch` `peer` `contains` `not_contains` | OVS 端口 VLAN 集合 |
| `p4_entry` | `switch` `table` `keys` `present` | P4 表项存在性 |
| `linux_route` | `node` `match` `table` `present` | Linux 路由表 |
| `evpn_advertise` | `route` | 动态下发路由 |
| `evpn_withdraw` | `route` | 动态撤销路由 |
| `link` | `node` `peer` `action` | 链路故障注入（netem 100% 丢包 / 恢复） |
| `sleep` | `seconds` | 等待 |

`ping` 会先发一个预热包，再发 `count` 个；`expect: pass` 失败时默认重试一次
（`retries: 1`），避免冷启动的 FDB/ND 抖动造成误判。

`ping6` 报 `Network is unreachable` 时视为 100% 不可达。

两种隔离要区分：

```text
L3 之间    报文实际发出，在 E 的 VRF 查不到对端前缀被丢弃
           iso-l3-net1-to-net2 / iso-l3-net1-to-l2 / iso-l3-net2-to-net1
L2 之间    每个 L2 表都有指向本网络互联地址的默认路由，跨切片流量
           留在自己的 VSI 内，由 E 在 VSI 内泛洪，到不了其它切片
           iso-l2-net3-to-net4
```

隔离用例统一用**源地址**绑定（`-I <源地址>`），而不是网卡名：网卡名会让内核
先按目的前缀选表，可能和设备绑定冲突而提前在主机侧失败；源地址能保证
`from` 规则命中，报文真正发到 E。

L2 表必须显式加默认路由。否则 `from` 表查不到目的时会继续匹配后面的规则，
最终落到主表；主表的默认路由指向网络 1 的网关，会把报文从**错误切片的
端点**发出去，造成跨切片注入。默认路由指向本网络的互联地址
（`240a:a806:{网络}:{100+站点}::1`），报文就留在自己的 VSI 内。

同一主机的多个端点共享同一 /64 前缀，内核不允许重复的前缀路由，
所以端点地址用 `ip -6 addr replace <地址>/64 noprefixroute` 只加地址；
每个端点需要的前缀路由由它自己的策略路由表提供。


## 实测结果

| 配置 | 规模 | 结果 | 说明 |
| --- | --- | --- | --- |
| `parallel-min.json` | 4 站点 × 2 主机 × 2 端点，17 netns（无 VTEP） | 42 通过 / 1 跳过 | QinQ 用例需要 access_vtep=true，耗时约 81 秒 |
| `parallel-min.json --tag faults` | 同上 | 1/1 通过 | netem 注入 100% 丢包后恢复 |
| `parallel.json` | 41 站点 × 2 主机 × 2 端点，166 netns（无 VTEP） | 42 通过 / 1 跳过 | 用例耗时约 86 秒，峰值内存约 4.5 GB |

失败用例会自动 dump 相关节点的 `ip -6 route/rule/addr` 和 P4 表项，
放在 `runs/<时间戳>/evidence/`。

资源占用按 4 站点配置（8 台 PM）逐台统计：

| 对象 | 每台 PM | 41 站点合计 |
| --- | --- | --- |
| 宿主机 bridge | 3（1 个 VLAN 桥 + 每 L3 网络 1 个） | 246 |
| tap veth | 8 对（4 网络 × 2 端点） | 656 对 |
| VXLAN 设备 | 4（每网络 1 个） | 328 |
| 端点路由表 | 8（每端点 1 张） | 656 |
| Linux netns | 4 站点 18 个 | 166 |

## 需求追溯矩阵

| 需求 | 用例 | 证据 |
| --- | --- | --- |
| VLAN 3501-3900 | `vlan-policy-*` | OVS trunks |
| S1 上行放行 | `vlan-policy-*` | `contains: 3501..3504` |
| S2 上行拒绝 | `vlan-policy-*` | `not_contains: 3501..3504` |
| L3VPN 同网络互通 | `l3vpn-*` | ping |
| L3VPN 跨网络隔离 | `iso-*` | ping 100% loss |
| L2VPN 同广播域 | `l2vpn-*` | ping |
| L2VPN VNI 隔离 | `iso-l2-net3-to-net4` | ping 100% loss |
| L2VPN 隧道内层不带业务 VLAN | `l2-tunnel-no-inner-vlan` | `C_bj` 抓 SRv6 包，内层无 `vlan` |
| 同站点流量走 VXLAN | `vxlan-intra-site` | `cl_bj_1` 抓 `udp 4789`，VNI = 网络 ID |
| VTEP↔E 用 QinQ | `qinq-access-double-tag` | `V_bj` 抓 `eth1`，报文为 `vlan 3500` + `vlan 3501` |
| E 直接终结 VXLAN | `vxlan-e-checksum-shim` | PM 抓 `u4000`，E→PM 的 VXLAN 为 `udp sum ok` |
| SRv6 TE 经 waypoint | `srv6-te-waypoint` | waypoint C 上抓包，`segleft` 1→0、DA 改写 |
| 真实业务流量（TCP/HTTP） | `tcp-*`、`http-*` | iperf 传输量非零、curl 200 + 1MB |
| 端点 MTU 覆盖 SRv6 开销 | `mtu-endpoint-vs-srv6` | 1352 字节通、1400 字节被拒 |
| BUM 复制 | `bum-net3` | ff02::1 |
| 公网接入 | `public-fix` | ping FIX |
| SRv6 SID 安装 | `e-bj-local-sid-*` | `local_sid` |
| L3/L2 FIB 安装 | `e-bj-l3-route-to-sh`、`e-bj-l2-tunnel-to-sh` | `l3_fib`/`l2_tunnel` |
| 策略路由 | `cl-bj-policy-rule` | `ip -6 rule` |
| 动态路由增删 | `evpn-*-add-withdraw` | agent + 表项 |
| BGP EVPN 会话 | 无 | 未实现 |
| 链路故障收敛 | `link-down-bj-tjn` | 只能验证 down/up，不做收敛 |

## 宿主机 VXLAN 建模

站点内两台 PM 都在同一台物理机上，PM 之间用 VXLAN 互通（VNI = 网络 ID），
两个 VTEP 的 local/remote 是 `fd00:0:{站点}:9::1` 和 `::2`，报文只在本机
veth 上转一圈：

- 每台 PM 一个 `vlan_filtering=1` 的 Linux bridge，顶替物理机里的 OVS；
- 每个 VM 一条 veth：VM 侧（`ep_*`）持地址和策略路由表，bridge 侧（`tp_*`）
  是 pvid = 业务 VLAN 的 access 口；
- 每个网络一个 VXLAN 设备（`vx_*`），桥里配 pvid/untagged，隧道内层帧不带 VLAN；
- 静态 FDB：本机 VM MAC 指向自己的 tap 口，对端 PM 的 VM MAC 指向 VXLAN 口；
- 只有 PM-A 有业务 VLAN 上联（`eth0` 直接进桥，由桥打 tag），PM-B 的站外
  流量先经 VXLAN 到 PM-A 再出站点，避免 S1 上出现重复帧；
- VXLAN 外层继承内层 DSCP（`tos inherit`），桥/tap/VXLAN 口关闭 IPv6，
  underlay veth 关闭 tx 校验和卸载，保证抓包与线上报文一致；
- 手工复核：

```bash
sudo ip netns exec <cl_bj_1 的 ns> bridge fdb show | grep 02:01:00:01:02:01
sudo ip netns exec <cl_bj_1 的 ns> tcpdump -i any -nn 'udp port 4789'
```

## E 直接终结 VXLAN（access_vtep=false + checksum shim）

`access_vtep=false` 时 Linux VTEP 不再参与，PM 的 VXLAN 直接送到 E：

- E 的 `vni_map` 把 VNI 映射到 VRF，`vxlan_access_decap()` 解封装后走 SRv6；
- 回程由 `srv6_decap_vxlan` 解 SRv6、再封 VXLAN 交回 PM；
- P4 只能把 UDP checksum 填 0（BMv2 的 `ComputeChecksum` 看不到 deparser
  之后拼好的字节），IPv6 下 0 非法，因此 E 的 S1 出口挂一段 tc/eBPF
  （`bpf/vxlan_csum_shim.c`，clsact egress）按伪首部重算校验和。

```bash
make parallel-bpf      # 编译 build/vxlan_csum_shim.o
```

shim 只处理 `Eth [+VLAN...] + IPv6 + UDP(dport 4789)`，支持单层 tag 和 QinQ，
其余流量原样放行；验证用例 `vxlan-e-checksum-shim` 在 PM 侧抓包断言
E→PM 的 VXLAN 报文为 `udp sum ok`。

排查记录：`vxlan_t` 里残留的 `bit<32> pseudo_len` 让 VXLAN 头变成 12 字节，
解析偏移 4 字节，`inner_ipv6` 永远无效。用 P4 `register` 把
`inner_ethernet.etherType` 读出（`0x4372` 而非 `0x86dd`）定位后删除该字段。

## 接入 VXLAN（Linux VTEP）

`configs/parallel.json` 与 `parallel-min.json` 默认 `access_mode=vxlan`、
`access_vtep=true`：

- PM 用 VXLAN（VNI = 网络 ID）把业务帧送到本站点的 Linux VTEP；
- VTEP 解封装后以 **QinQ 双层 tag**（外层 3500 + 内层切片 VLAN）交给 E，
  E 用内层 VLAN 查 VRF/VSI，再做 SRv6；
- 好处是把 VXLAN 终结放在内核里（校验和由内核算），E 仍然是纯 P4/SRv6；
- L2VPN 暂时仍走 VLAN（不经 VTEP），L2 跨站点仍是 SRv6。

VTEP 侧的关键配置（`parallel/control.py::_configure_vtep`）：

- 物理 trunk（`eth1`）**不进桥**，只在它上面叠 `qinq3500 = vlan 3500 on eth1`；
- `qinq3500` 作为桥端口，对每个 L3 切片 `bridge vlan add dev qinq3500 vid <vlan>`，
  于是桥在子接口上打内层 tag、子接口再压外层 3500，物理口就是 `3500 + <vlan>`；
- VXLAN 设备保持 `pvid untagged`，桥内看到的是 untagged 业务帧。

E 侧（`p4src/parallel_vpn.p4`）：

- `parse_vlan` 遇到第二层 802.1Q 进入 `parse_vlan_inner`，接入口因此同时兼容
  单层（L2VPN 直连）和双层（经 VTEP 的 L3VPN）；
- ingress 在 `port_cfg` 之后用内层 VID 覆盖 `meta.vlan_id`，`vlan_map` 键不变；
- egress 新增 `qinq_map`：只有登记过的 L3 切片才把单层 tag 包成
  `3500 + <切片 VLAN>`，L2/核心/公网方向一律不压外层。

两个排查过的真实坑：

1. PM 的业务 VLAN 子接口进桥时必须按 `pvid untagged` 配（tag 已被子接口剥掉），
   配成 tagged 会让帧落到 VLAN 1 被丢弃；
2. VTEP↔E 的 QinQ 不能“把物理口放进桥、再在桥上打两层 tag”——Linux 桥每个
   端口只打一层，必须让物理口留在桥外，用 VLAN 子接口当桥端口。

## SRv6 TE（静态多段）

`configs/*.json` 的 `te_paths` 给某条 L3VPN 路径指定一个 waypoint 站点：

```json
"te_paths": [{"network": 1, "src": "bj", "dst": "sh", "via": "jnn"}]
```

- 入口 E：`l3_fib` 配 `srv6_encap_te`，`segment[0]` = 目的 SID、`segment[1]` = waypoint SID，
  `hdrExtLen=4`、`lastEntry=1`、`segmentsLeft=1`，外层 DA = waypoint SID；
- waypoint E：`transit_sid` 命中本机 SID → `srv6_end`（`segmentsLeft--`、DA 改写），
  再按 `sid_fib`（站点 /48 → 核心口）转发回 C；C 不参与 SRv6 处理；
- 目的 E：`segmentsLeft=0` 命中 `local_sid` 正常解封装；
- 抓包断言（`srv6-te-waypoint`）：waypoint 的 C 上能看到
  `seg1=1, DA=jnn SID` 进、`segleft=0, DA=sh SID` 出。

限制：只支持 L3VPN 和单个 waypoint；3 段以上/压缩 SID 的报文会被丢弃。

## 业务流量用例

`07-traffic.yaml` 在真实 netns 里跑业务流量，不做协议建模：

- `iperf 2.x`（`-V` 走 IPv6，`-B` 绑端点地址），北京作服务端、上海作客户端，
  L3VPN 与 L2VPN 各一条；
- `python3 -m http.server` + `curl` 取 1MB 文件，验证大包不会被隧道开销黑洞；
- 判据只看“传输是否完成”：iperf 输出必须有非零字节数，curl 必须 200 且文件长度正确；
  **不断言带宽**，BMv2 是软件转发，数字没有参考意义；
- 端点 MTU 由 `configs/*.json` 的 `endpoint_mtu` 控制（默认 1400），
  为 SRv6 的 64B（L3）/78B（L2）开销留余量；超过端点 MTU 的报文在主机侧
  就被拒绝，不需要依赖 ICMPv6 PTB（BMv2 不生成该报文）；
- `http.server` 启动时的反向解析会卡 6 秒，用例里用
  `RES_OPTIONS="timeout:1 attempts:1"` 压到 1 秒，并按端口就绪（`ss -ltn`）轮询，
  不依赖固定 sleep。

## 能验证与不能验证

| 能验证 | 不能验证 |
| --- | --- |
| VLAN 接入与过滤 | 线速转发、队列、缓存 |
| VRF/VSI 隔离 | 真实 BGP 邻居与 NLRI |
| SRv6 封装/解封装语义 | SRv6 压缩 SID、TE |
| 宿主机桥 + VXLAN（VNI 隔离） | 真实 OVS/VTEP 实现、大规模 VTEP、硬件卸载 |
| 业务可达与 TCP/HTTP 传输 | 吞吐、时延、抖动（BMv2 软件转发） |
| 表项内容与容量 | 控制面收敛时间 |
| 动态路由增删 | 多控制器主备 |
| 链路 up/down | 路由自动收敛（当前是静态路由） |

## 配置同源

仿真配置和真实设备配置都由 `configs/parallel*.json` 生成：

```bash
python3 tools/render_config.py --config configs/parallel-min.json --target huawei
python3 tools/render_config.py --config configs/parallel-min.json --target sim
python3 tools/render_config.py --config configs/parallel-min.json --target ovs
python3 tools/render_config.py --config configs/parallel-min.json --target summary
```

真实设备配置基线：

```text
configs/expected/huawei-min.txt
```

`tests/test_verify.py` 会做 golden 对比。改了地址、VLAN、RD/RT 或 SID 时：

```bash
make render-config      # 重新生成基线
make test               # 确认差异是有意的
```

这样能保证：

```text
仿真表项、OVS 配置、真实设备 CLI 使用同一份来源
```

## 下发并发

静态表项、EVPN agent 和 S1/S2 的 OVS 配置都按交换机或站点并发，默认 16 路。
每台 E 一个 `simple_switch_CLI` 进程，同一台交换机上的命令仍然有序。

```bash
PARALLEL_CONTROL_WORKERS=8 sudo python3 verify/runner.py ...   # 静态表项
PARALLEL_EVPN_WORKERS=8 sudo python3 verify/runner.py ...      # 动态路由
PARALLEL_OVS_WORKERS=8 sudo python3 verify/runner.py ...       # S1/S2 配置
PARALLEL_CONTROL_WORKERS=1 PARALLEL_EVPN_WORKERS=1 \
PARALLEL_OVS_WORKERS=1 sudo python3 verify/runner.py ...       # 退回串行
```

41 站点实测：

```text
动态 L3 advertise   12.6s -> 2.2s
动态 L2 advertise    7.4s -> 1.4s
静态表项下发        34.7s -> 待实测（预计 10s 以内）
OVS 接入配置        17.3s -> 待实测（预计 3~4s）
```

## 路径映射

`tools/path_map.py` 从同一份配置生成“业务路径 → 物理路径 → 每段封装”的
对照表，格式对应 REQUIREMENTS.md 的 3.1/5：

```bash
python3 tools/path_map.py --config configs/parallel-min.json \
    --network 1 --src bj_1 --dst sh_1

python3 tools/path_map.py --config configs/parallel-min.json \
    --network all --src bj_1 --dst sh_1 --format text

python3 tools/path_map.py --config configs/parallel-min.json \
    --network 1 --src bj_1 --dst sh_1 --format json
```

同站点两台主机：

```bash
python3 tools/path_map.py --config configs/parallel-min.json \
    --network 1 --src bj_1 --same-site
```

同站点显示 S1 二层转发；跨站点显示 VLAN → SRv6 → VLAN。
宿主机 OVS/VXLAN 段尚未建模。

## 失败排查

`runs/<时间戳>/evidence/<用例>.txt` 包含：

```text
失败步骤和原始输出
相关 E 交换机的 l3_fib / local_sid / l2_tunnel / vlan_map
相关节点的 ip -6 route / rule / addr
```

常用手工命令：

```bash
sudo ovs-vsctl get port S2_bj-eth1 trunks
sudo ovs-appctl fdb/show S1_bj
python3 parallel_evpn_ctl.py status
```

## 新增用例

1. 在 `verify/cases/` 新建或修改 YAML；
2. `make verify-dry` 检查语法和站点依赖；
3. `make verify-min` 实跑；
4. 如果是新的验收点，同步更新本文的追溯矩阵。

用例只描述“期望”，不写实现细节；节点名和接口名沿用拓扑命名（`cl_<站点>`、`E_<站点>`、`S1_<站点>`、`C_<站点>`）。
