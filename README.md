# FITI Mininet Topology

使用 Mininet 模拟 `fiti.json` 中的 FITI 骨干拓扑，同时提供命令行与 Python 库两种使用方式，并支持基于 VLAN 的网络切片（可选 SRv6 主机 overlay）。

## 拓扑概况

| 项目 | 说明 |
|---|---|
| 节点 | 41 个（每个节点对应一台 OVS 交换机 `s<id>` 和一台主机 `<abbr>`） |
| 骨干链路 | 43 条 |
| 主机地址 | `10.0.0.<id>/24` |
| 链路权重 | `weight` 4/2/1 映射为 100/50/10 Mbps |
| 节点示例 | 北京 `s1` / `bj`，上海 `s2` / `sh`，西安 `s3` / `xa` |

文件：

- `fiti.json`：拓扑数据（nodes / links）
- `fiti_topo.py`：Mininet 拓扑、CLI 与库接口
- `fiti_slice.py`：网络切片 CLI 与库接口
- `slices.json`：切片配置示例
- `SLICING_DESIGN.md`：切片设计方案与限制

## 环境要求

- Linux
- Mininet 2.3+
- Open vSwitch
- root 权限（`sudo`）

若上次运行异常退出，先清理残留：

```bash
sudo mn -c
```

## 快速开始

```bash
cd ~/workspace/FITI_Mininet

# 查看拓扑统计（无需 root）
python3 fiti_topo.py --dump

# 启动 CLI：OVS standalone + STP，适合连通性测试
sudo python3 fiti_topo.py

# 启动后等待 STP 收敛并全量 ping，再进入 CLI
sudo python3 fiti_topo.py --pingall

# 对接外部控制器（Ryu / ONOS / ODL 等）
sudo python3 fiti_topo.py --controller remote \
    --controller-ip 127.0.0.1 --controller-port 6653

# 使用 Mininet 自带 ovs-testcontroller
sudo python3 fiti_topo.py --controller default

# 只建交换机，不建主机
sudo python3 fiti_topo.py --no-hosts

# 不做带宽限制
sudo python3 fiti_topo.py --no-tc
```

作为 `mn` 的 custom 拓扑加载：

```bash
sudo mn --custom fiti_topo.py --topo fiti --link tc
```

## 命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--topo-file PATH` | `fiti.json` | 拓扑 JSON 路径 |
| `--dump` | 关闭 | 只打印拓扑统计并退出 |
| `--no-hosts` | 关闭 | 只创建 41 台交换机 |
| `--no-tc` | 关闭 | 使用普通链路，不限制带宽 |
| `--bw-map MAP` | `4:100,2:50,1:10` | `weight:Mbps` 映射 |
| `--controller MODE` | `stp` | `stp` / `default` / `remote` |
| `--controller-ip IP` | `127.0.0.1` | 外部控制器地址 |
| `--controller-port PORT` | `6653` | 外部控制器端口 |
| `--wait-connected` | 关闭 | stp 模式下等待 STP 收敛；remote 模式下等待交换机连接控制器 |
| `--stp-wait SEC` | `60` | STP 收敛等待上限 |
| `--pingall` | 关闭 | 启动后先执行 pingall |
| `--log LEVEL` | `info` | Mininet 日志级别 |

说明：拓扑存在环路，默认的 `stp` 模式使用 OVS standalone 学习交换并开启 STP，避免广播风暴；STP 收敛通常需要 30–45 秒。使用 `default` 或 `remote` 模式时，需由控制器处理环路（如运行 STP 应用），否则可能出现广播风暴。

## 作为库使用

```python
from fiti_topo import build_net, fiti_network, get_host
```

创建并启动网络（调用方负责 `net.stop()`）：

```python
net = build_net(controller='remote', controller_port=6653)
bj, sh = get_host(net, 1), get_host(net, 2)
print(bj.cmd('ping -c 3', sh.IP()))
net.stop()
```

上下文管理器（自动清理）：

```python
with fiti_network(controller='stp', wait_connected=True) as net:
    net.pingAll()
```

只获取 `Topo` 对象：

```python
from mininet.net import Mininet
from mininet.node import RemoteController
from fiti_topo import build_topology

topo = build_topology(with_hosts=True)
net = Mininet(topo=topo, controller=RemoteController)
```

仅使用元数据（无需 root）：

```python
from fiti_topo import load_topology, node_by_id, host_name, host_ip, switch_name

data = load_topology()
print(node_by_id(data, 1)['name'])              # 北京
print(switch_name(1), host_name(data, 1), host_ip(1))
# s1 bj 10.0.0.1/24
```

### 主要接口

| 接口 | 说明 |
|---|---|
| `build_net(**kwargs)` | 创建并启动网络，返回 `Mininet` 实例 |
| `fiti_network(**kwargs)` | `build_net` 的上下文管理器 |
| `build_topology(...)` | 构造 `FitiTopo` 对象 |
| `load_topology(path)` | 读取拓扑 JSON |
| `validate_topology(data)` | 校验并返回告警列表 |
| `compute_degrees(data)` | 按 `links` 计算节点度数 |
| `node_by_id / switch_name / switch_dpid / host_name / host_ip` | id 与交换机、主机、IP、dpid 互转 |
| `get_host(net, id)` / `get_switch(net, id)` | 从网络对象中按 id 取主机/交换机 |

`build_net` 常用参数：`with_hosts`、`bw_map`、`use_tc`、`controller`、`controller_ip`、`controller_port`、`stp`、`wait_connected`、`stp_wait`、`start`、`switch_cls`。

## 网络切片

在同一个 41 节点物理拓扑上，用 VLAN 隔离多个切片。每个切片有独立主机、IP 子网和 VLAN，共享物理链路带宽。当前只支持 VLAN 模式，桥接模式不再实现；设计取舍与限制见 [SLICING_DESIGN.md](SLICING_DESIGN.md)。

### 快速开始

```bash
# 查看切片规划（无需 root）
python3 fiti_slice.py --dump

# 启动并进入 CLI（STP 收敛约 30-45 秒）
sudo python3 fiti_slice.py --slices slices.json

# 自动等待收敛，检查切片内连通性、VLAN 标签与跨切片不可达
sudo python3 fiti_slice.py --slices slices.json --check

# 开启 SRv6 主机 overlay 并检查
sudo python3 fiti_slice.py --slices slices.json --check-srv6
```

也可以使用统一入口（等价，内部转交 `fiti_slice.py`）：

```bash
python3 fiti_topo.py --slices slices.json --dump
sudo python3 fiti_topo.py --slices slices.json --check
```

### 示例切片

| 切片 | VLAN | 子网 | 主机数 | 覆盖 |
|---|---|---|---|---|
| `nor` | 100 | 10.1.0.0/24 | 14 | 华北 / 东北 / 北京高校 |
| `eas` | 101 | 10.2.0.0/24 | 11 | 华东 |
| `sou` | 102 | 10.3.0.0/24 | 6 | 华南 / 华中 |
| `wes` | 103 | 10.4.0.0/24 | 10 | 西部 |

主机名为 `<切片id>_<abbr>`，例如 `nor_bj`（北京）、`eas_sh`（上海）；IP 为 `10.<切片序号>.0.<节点id>/24`。主机名与 IP 的对应关系可用 `--dump` 查看；在 Mininet CLI 中可用 `nor_bj ping -c 3 10.1.0.14` 这类命令（目标地址用 IP）。

### 配置字段

| 字段 | 说明 |
|---|---|
| `isolation` | `vlan`（唯一支持） |
| `controller.mode` | `stp` / `default` / `remote`，默认 `stp` |
| `defaults.vlan_base` | 未指定 VLAN 时从 100 起分配 |
| `defaults.subnet_base` | 未指定子网时按 `10.<序号>.0.0/24` 分配 |
| `srv6.enabled` | 是否默认开启 SRv6 overlay |
| `srv6.decap_table` | 解封装路由表，默认 100 |
| `slices[].id` | 切片标识，也是主机名前缀 |
| `slices[].vlan` / `subnet` | 可选，自动分配；需全局唯一 |
| `slices[].hosts` | 节点 `abbr` 列表，或 `"all"` |

校验会检查 VLAN / 子网冲突、未知节点、主机名过长，以及不支持的 `bridge` 模式。

### SRv6 主机 overlay

用 `--srv6` 或 `"srv6": {"enabled": true}` 开启。只有主机命名空间做 SRv6 封装/解封装，OVS 只做 L2 承载：

| 用途 | 地址 |
|---|---|
| underlay | `2001:db8:<切片序号>::<节点id>/64` |
| SID | `2001:db8:<切片序号>:<节点id>::1/128` |
| overlay | `fd00:<切片序号>::<节点id>/128` |

- 切片内主机两两建立 `encap seg6` 路由，目的主机用 `End.DT6` 解封装。
- 关闭 IPv6 DAD 并预置静态邻居，首包即可通。
- 仅支持单段 SID；不支持交换机逐跳 SRv6 和多段路径。
- 每切片路由数为 O(N²)，建议每切片不超过 20 台主机。
- 封装增加约 64 字节，主机 MTU 默认 1420。

### 库用法

```python
from fiti_slice import load_slice_config, build_slice_net, SliceManager

cfg = load_slice_config('slices.json')
net = build_slice_net(cfg, wait_connected=True)   # 已启动，调用方负责 stop
mgr = SliceManager(net, cfg)

print(mgr.ping_slice('nor'))          # 切片内 IPv4 丢包率
mgr.enable_srv6()                     # 安装 SRv6 地址与路由
print(mgr.check_srv6_slice('nor'))    # 返回失败列表
net.stop()
```

常用接口：`build_slice_net(cfg)`、`SliceManager.ping_slice(id)`、`hosts(id)`、`host(id, abbr)`、`enable_srv6()`、`check_srv6_slice(id)`、`stats()`。

## 拓扑数据说明

脚本以 `links` 为准。运行时会提示 `fiti.json` 中的以下不一致：

- 北京（`id=1`）：`degree` 声明为 11，实际为 12（多出 `3 - 1` 链路）
- 广州（`id=5`）：`degree` 声明为 6，实际为 5
- 武汉（`id=7`）：`degree` 声明为 6，实际为 5
- `statistics.degree_distribution` 与实际统计不一致：实际为 1/2/3/4/5/6/12 度 = 24/8/3/1/3/1/1

如需修改拓扑，直接编辑 `fiti.json` 的 `nodes` 与 `links` 即可。
