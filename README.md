# FITI 并行网络仿真

本分支只包含 **中关村主干网并行网络仿真**。

当前范围：

- 41 个站点；
- 每站点 `C/E/A/S1/S2` 和一台云服务器；
- 4 个并行网络（2 个 L3VPN + 2 个 L2VPN）；
- BMv2/P4 数据平面；
- SRv6 L3VPN/L2VPN；
- L2VPN BUM ingress replication；
- C2/FIX 公网静态接入；
- EVPN 路由模型和动态路由 agent。

真实 BGP/EVPN 控制面尚未接入，SID、C 路由和公网路由仍以静态配置为主。

## 分支

```text
feature/parallel-network    41 站点、123 台 BMv2 当前实现
feature/parallel-41-switch  41 台站点交换机的未来优化分支
```

`feature/parallel-41-switch` 目前只是占位分支，尚未实现。

## 快速开始

### 环境要求

- Linux；
- 802.1Q VLAN 支持，必要时执行：

```bash
sudo modprobe 8021q
```

- Mininet 2.3+；
- p4c（`p4c-bm2-ss`）；
- bmv2（`simple_switch`、`simple_switch_CLI`）；
- root 权限。

### 4 站点最小验证

推荐先启动 4 个站点验证全部功能：

```bash
make parallel-stop
make parallel-run-min
```

规模：

```text
4 个站点：北京、上海、天津、济南
12 台 BMv2
14 个 Linux netns
```

这 4 个站点在 FITI 实际拓扑中组成连通路径：

```text
北京 — 天津 — 济南 — 上海
```

对应免 root 命令：

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
123 台 BMv2
125 个 Linux netns
```

全量启动较慢。Thrift 端口段被占用时会自动向后寻找空闲端口段，例如：

```text
*** Thrift 端口段被占用，自动改用 11501～11623
```

也可以显式指定起始端口：

```bash
sudo python3 parallel_net.py --thrift-base 15000
```

### 停止

```bash
make parallel-stop
```

它会清理：

- `simple_switch`；
- `parallel_net.py`；
- Mininet 网络；
- `/tmp/parallel_evpn.sock`。

不要同时运行：

```text
make parallel-run
make parallel-run-min
```

## 拓扑和站点

每个站点：

```text
C — E — S1 — cloud
C — A — S2 — cloud
```

- `E`：BMv2/P4，负责 VRF、L3VPN、L2VPN 和 SRv6；
- `S1`：BMv2/P4，上行放行并行 VLAN；
- `S2`：BMv2/P4，上行拒绝并行 VLAN；
- `C`、`A`：Linux netns；
- `cloud`：每站点一台 Linux 主机；
- `pub_border`、`fix`：北京公网边界。

站点之间的核心链路直接使用 FITI 实际拓扑：

```text
configs/fiti.json
41 个节点，43 条链路
```

每个并行网络站点对应一个 FITI 节点，站点 C 路由器按照 FITI 的实际链路
互联，C 路由按 FITI 图的最短路径计算下一跳。

例如北京到上海在最小验证拓扑中经过天津和济南。

## 单云主机和多 VLAN 子接口

每站点只有一台云服务器，通过 VLAN 子接口区分切片：

```text
cl_bj
├── eth0   -> S1 trunk
├── eth1   -> S2 trunk
├── v3501  -> 网络 1
├── v3502  -> 网络 2
├── v3503  -> 网络 3
└── v3504  -> 网络 4
```

- S1/S2 到云服务器使用 trunk；
- L3 网络使用按源地址和目的前缀分流的策略路由；
- L2 网络使用 VLAN 子接口和静态远端邻居；
- 云服务器 MAC 按 `(站点, 网络)` 确定性生成。

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
4 个网络都选择了全部 41 站点。

`configs/parallel-min.json` 是 4 站点验证子集：

```text
bj、sh、tjn、jnn
```

选择的是 FITI 实际拓扑中连通的北京—天津—济南—上海路径。

## EVPN 路由模型

`parallel/evpn.py` 生成：

- EVPN IP Prefix 路由；
- EVPN MAC/IP 路由；
- EVPN Inclusive Multicast 路由；
- RD/RT、VNI、SRv6 SID。

免 root 输出：

```bash
make parallel-evpn-dump
```

41 站点结果：

```text
82 条 L3 IP Prefix
82 条 L2 IMET
共 164 条本地路由
```

Mininet 启动后增加 82 条云服务器 MAC/IP 路由，共 246 条。

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
  "sid": "fd00:0:5:1::101",
  "origin": "xa",
  "next_hop": "fd00:0:5:1::101"
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

### L3VPN

```text
cl_bj ping6 -c 3 -I v3501 240a:a806:1:2::10
cl_bj ping6 -c 3 -I v3501 240a:a806:1:3::10
cl_bj ping6 -c 3 -I v3501 240a:a806:1:5::10
cl_bj ping6 -c 3 -I v3501 240a:a806:1:41::10
```

### L2VPN

```text
cl_bj ping6 -c 3 -I v3503 240a:a806:3:1::11
cl_bj ping6 -c 3 -I v3503 240a:a806:3:1::12
cl_bj ping6 -c 3 -I v3503 240a:a806:3:1::14
```

### BUM

```text
cl_bj ping6 -c 3 -I v3503 ff02::1
```

### 公网

```text
cl_bj ping6 -c 3 -I v3501 2001:255:0:100::2000
```

### 跨网络隔离

```text
cl_bj ping6 -c 3 -I v3501 240a:a806:2:3::10
# 应 100% 丢包
```

## 常用命令

```bash
make parallel-p4              # 编译 P4
make parallel-dump            # 41 站点拓扑
make parallel-dump-min        # 4 站点拓扑
make parallel-evpn-dump       # 41 站点路由模型
make parallel-evpn-dump-min   # 4 站点路由模型
make test-evpn                # 路由模型测试

make parallel-run             # 41 站点，123 台 BMv2
make parallel-run-min         # 4 站点，12 台 BMv2
make parallel-run-no-cli      # 41 站点后台模式
make parallel-stop            # 清理
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
├── topology.py             C/E/A/S1/S2/cloud
├── switch.py               BMv2 交换机封装
├── evpn.py                 EVPN 路由模型
├── evpn_agent.py           动态路由 agent
├── control.py              静态 P4 表项
├── runtime.py              Mininet 构建
└── p4.py                   P4 编译和名称解析

configs/
├── fiti.json               FITI 实际拓扑数据（41 节点/43 链路）
├── parallel.json           41 站点配置
└── parallel-min.json       4 站点验证配置

docs/
├── PARALLEL_NETWORK_DESIGN.md
└── 中关村主干网并行网络设计方案.pdf

p4src/parallel_vpn.p4       VLAN + L3/L2VPN + SRv6 + BUM
parallel_net.py             并行网络 CLI
parallel_evpn.py            EVPN 路由模型 CLI
parallel_evpn_ctl.py        agent 客户端
```

## 已知限制

- 当前是 4 个静态网络，不是 PDF 的 400 个网络；
- 4 个网络当前覆盖全部 41 站点，尚未按 PDF 精确配置网络成员；
- SID、C 路由和公网路由仍是静态配置；
- 没有真实 BGP/EVPN 会话、Route Reflector 或 EVPN NLRI 编解码；
- L2 BUM multicast group 是静态生成的；
- VXLAN 回退模式只支持已知单播；
- 动态 agent 只支持 L3 IP Prefix 和 L2 MAC/IP 已知单播；
- 123 台 BMv2 资源占用较高，全量启动较慢；
- `feature/parallel-41-switch` 尚未实现。

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

### `Network is unreachable`

```text
cl_bj ip -6 addr show v3501
cl_bj ip -6 rule show
cl_bj ip -6 route show table 20001
cl_bj ip -6 route get 240a:a806:1:2::10
```

### agent 连接失败

```bash
cd ~/workspace/FITI_Mininet
python3 parallel_evpn_ctl.py status
```

或：

```bash
python3 /home/mxd/workspace/FITI_Mininet/parallel_evpn_ctl.py status
```

### VLAN 子接口不存在

```bash
sudo modprobe 8021q
make parallel-stop
make parallel-run-min
```
