# FITI 多切片与并行网络仿真

基于 Mininet + BMv2/P4 的 FITI 骨干网仿真项目，包含两条主线：

- **基础多切片**：41 节点拓扑、VLAN 切片、P4Runtime 动态增删切片、多段 SRv6；
- **并行网络**：41 站点、L3VPN/L2VPN、SRv6、BUM 复制、公网接入和 EVPN 路由模型。

当前分支：

```text
main                        基础多切片
- feature/parallel-network    41 站点并行网络当前实现
feature/parallel-41-switch  41 台站点交换机的未来实现分支
```

并行网络的 41 站点静态数据平面已经完成 L3VPN、L2VPN、BUM 和公网验证。
真实 BGP/EVPN 控制面尚未接入。

## 快速开始

### 环境要求

- Linux
- 802.1Q VLAN 支持（必要时 `sudo modprobe 8021q`）
- Mininet 2.3+
- p4c（`p4c-bm2-ss`）
- bmv2（`simple_switch`、`simple_switch_grpc`、`simple_switch_CLI`）
- Python 3.8+
- root 权限

### 并行网络最小验证

先启动 4 个站点，资源占用低，适合验证全部功能：

```bash
make parallel-stop       # 清理旧进程和端口
make parallel-run-min    # 北京/上海/广州/成都，12 台 BMv2
```

对应的免 root 命令：

```bash
make parallel-dump-min
make parallel-evpn-dump-min
```

### 并行网络全量启动

```bash
make parallel-stop
make parallel-run         # 41 站点，123 台 BMv2
```

全量启动较重。Thrift 端口段被占用时，程序会自动向后寻找空闲端口段，
例如：

```text
*** Thrift 端口段被占用，自动改用 11501～11623
```

这是正常行为。

也可以显式指定起始端口：

```bash
sudo python3 parallel_net.py --thrift-base 15000
```

### 基础多切片

```bash
make p4
make run-slices
make check
make check-srv6
```

P4Runtime 动态切片：

```bash
make venv
make runtime
```

## 并行网络架构

### 站点和交换机

41 个站点，每个站点包含：

```text
C — E — S1 — cloud
C — A — S2 — cloud
```

- `E`、`S1`、`S2`：BMv2 + P4；
- `C`、`A`：Linux netns；
- `cloud`：每站点一台 Linux 主机；
- `pub_border`、`fix`：北京公网边界。

因此全量规模为：

```text
41 个站点
123 台 BMv2（E/S1/S2）
125 个 Linux netns（C/A/cloud + 公网边界）
```

### 单云主机和多 VLAN 子接口

每个站点只有一台云服务器，通过 VLAN 子接口区分切片：

```text
cl_bj
├── eth0   -> S1 trunk
├── eth1   -> S2 trunk
├── v3501  -> 网络 1
├── v3502  -> 网络 2
├── v3503  -> 网络 3
└── v3504  -> 网络 4
```

L3 网络使用按源地址和目的前缀分流的策略路由表；L2 网络使用 VLAN 子接口和
静态远端邻居。云服务器 MAC 按 `(站点, 网络)` 确定性生成。

### 路由模型和 EVPN agent

`parallel/evpn.py` 生成：

- EVPN IP Prefix 路由；
- EVPN MAC/IP 路由；
- EVPN Inclusive Multicast 路由；
- RD/RT、VNI、SRv6 SID。

41 站点免 root 输出：

```text
82 条 L3 IP Prefix
82 条 L2 IMET
共 164 条本地路由
```

Mininet 启动后增加 82 条 MAC/IP 路由，共 246 条。

`make parallel-run` 会自动启动动态路由 agent：

```text
/tmp/parallel_evpn.sock
```

客户端：

```bash
cd ~/workspace/FITI_Mininet
python3 parallel_evpn_ctl.py status
python3 parallel_evpn_ctl.py dump
```

动态增加/撤销 L3 路由：

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

agent 支持 L3 IP Prefix 和 L2 MAC/IP 已知单播；IMET/BUM multicast group
仍由静态控制面安装。

## 功能验证

最小配置验证：

```text
# L3VPN
cl_bj ping6 -c 3 -I v3501 240a:a806:1:2::10
cl_bj ping6 -c 3 -I v3501 240a:a806:1:3::10

# L2VPN
cl_bj ping6 -c 3 -I v3503 240a:a806:3:1::11
cl_bj ping6 -c 3 -I v3503 240a:a806:3:1::12

# BUM
cl_bj ping6 -c 3 -I v3503 ff02::1

# 公网
cl_bj ping6 -c 3 -I v3501 2001:255:0:100::2000
```

全量 41 站点示例：

```text
cl_bj ping6 -c 3 -I v3501 240a:a806:1:2::10
cl_bj ping6 -c 3 -I v3501 240a:a806:1:5::10
cl_bj ping6 -c 3 -I v3501 240a:a806:1:41::10
```

## 配置

### 并行网络

`configs/parallel.json` 包含 41 站点和 4 个并行网络。每个网络可用
`sites` 指定成员站点；当前扩展配置为了覆盖全部节点，4 个网络都选择了
全部 41 站点。

`configs/parallel-min.json` 是 4 站点验证子集：

```text
bj、sh、gz、cd
```

### 基础多切片

`configs/slices.json` 定义基础切片。最小格式：

```json
{
  "topo": "fiti.json",
  "datapath": "bmv2",
  "isolation": "vlan",
  "slices": [
    {
      "id": "nor",
      "name": "华北",
      "vlan": 100,
      "subnet": "10.1.0.0/24",
      "hosts": ["bj", "tjn", "sjz"]
    }
  ]
}
```

完整字段说明见 [docs/SLICING_DESIGN.md](docs/SLICING_DESIGN.md)。

## 目录结构

```text
fiti/                       FITI 基础多切片实现
├── topo.py                 拓扑
├── slice.py                切片静态模式
├── p4.py                   编译、生成树、静态流表
├── p4runtime.py            P4Runtime 客户端
├── control.py              动态切片控制器
├── runtime.py              P4Runtime 运行时
└── ctl.py                  Unix socket 客户端

parallel/                   并行网络子项目
├── config.py               站点、网络、VLAN、IPv6 规划
├── topology.py             C/E/A/S1/S2/cloud 拓扑
├── switch.py               BMv2 交换机封装
├── evpn.py                 EVPN 路由模型
├── evpn_agent.py           动态路由 agent
├── control.py              静态 P4 表项
├── runtime.py              Mininet 构建与启动
└── p4.py                   P4 编译与名称解析

configs/
├── fiti.json               41 节点 FITI 拓扑
├── slices.json             基础切片示例
├── parallel.json           41 站点并行网络
└── parallel-min.json       4 站点验证子集

docs/
├── SLICING_DESIGN.md       基础切片设计
├── PARALLEL_NETWORK_DESIGN.md  并行网络设计
├── 中关村主干网并行网络设计方案.pdf
└── fiti_topology.svg

parallel_net.py             并行网络 CLI
parallel_evpn.py            EVPN 路由模型 CLI
parallel_evpn_ctl.py        agent 客户端
p4src/fiti_slice.p4         VLAN + 多段 SRv6
p4src/parallel_vpn.p4       L3/L2VPN + SRv6 + BUM
```

## 常用命令

```bash
make p4                     # 编译基础 P4
make parallel-p4            # 编译并行网络 P4
make test-evpn              # 路由模型测试

make parallel-dump          # 41 站点拓扑
make parallel-dump-min      # 4 站点拓扑
make parallel-evpn-dump     # 41 站点路由模型
make parallel-evpn-dump-min # 4 站点路由模型

make parallel-run           # 41 站点
make parallel-run-min       # 4 站点
make parallel-stop          # 清理进程、端口和 socket
```

## 已知限制

- 并行网络当前是 4 个静态网络，不是 PDF 的 400 个网络；
- 所有网络当前覆盖全部 41 站点，尚未按 PDF 精确配置网络成员；
- SID、C 路由和公网路由仍是静态配置；
- 没有真实 BGP/EVPN 会话、Route Reflector 或 EVPN NLRI 编解码；
- L2 BUM multicast group 是静态生成的，不支持动态增删站点；
- VXLAN 回退模式只支持已知单播；
- 动态 agent 目前只支持 L3 IP Prefix 和 L2 MAC/IP 已知单播；
- 123 台 BMv2 资源占用较高，全量启动较慢；
- `feature/parallel-41-switch` 只是未来优化分支，尚未实现。

## 故障排查

### 端口占用

```bash
make parallel-stop
sleep 10
```

再启动一次。不要同时运行：

```text
make parallel-run
make parallel-run-min
```

查实际端口：

```bash
sudo ss -tanp | grep ':端口号'
sudo lsof -nP -iTCP:端口号
```

### `Network is unreachable`

检查：

```text
cl_bj ip -6 addr show v3501
cl_bj ip -6 rule show
cl_bj ip -6 route show table 20001
cl_bj ip -6 route get 240a:a806:1:2::10
```

### agent 连接失败

确保在项目目录执行：

```bash
cd ~/workspace/FITI_Mininet
python3 parallel_evpn_ctl.py status
```

或使用绝对路径：

```bash
python3 /home/mxd/workspace/FITI_Mininet/parallel_evpn_ctl.py status
```

### VLAN 子接口不存在

```bash
sudo modprobe 8021q
```

然后退出 Mininet，执行 `make parallel-stop` 后重新启动。
