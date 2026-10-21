# FITI Mininet + bmv2 拓扑

用 Mininet 模拟 `configs/fiti.json` 中的 FITI 骨干拓扑。每个启用节点运行一台 bmv2
`simple_switch` / `simple_switch_grpc`，加载 `p4src/fiti_slice.p4` 做
VLAN 感知 L2 转发和交换机逐跳 SRv6；同一台交换机可以承载多个切片主机，
切片支持运行中增删。

## 拓扑概况

| 项目     | 说明                                                              |
| -------- | ----------------------------------------------------------------- |
| 节点     | 默认 41 个（每个启用节点一台 bmv2 交换机 `s<id>`，可禁用）        |
| 骨干链路 | 默认 43 条（禁用节点后自动过滤相关链路）                          |
| 主机地址 | 基础模式 `10.0.0.<id>/24`；切片模式 `10.<切片序号>.0.<节点id>/24` |
| 链路权重 | `weight` 4/2/1 映射为 100/50/10 Mbps                              |
| 数据平面 | bmv2，P4-16 / v1model，L2 + SRv6                                  |
| 控制平面 | 静态模式 Thrift；运行时模式 P4Runtime + Unix socket                |

### 按需启动节点

`configs/fiti.json` 的每个节点支持 `"enabled": true|false`，默认 `true`。
设为 `false` 时：

- 不创建该节点的 bmv2 交换机和主机，节省 bmv2 进程和 Mininet 资源；
- 该节点相关的链路会被过滤，不参与端口映射、生成树和 P4 规则；
- 切片配置不得引用该节点，否则会报错提示先启用或从切片中移除；
- 如果禁用核心节点导致启用节点不连通，拓扑校验会给出告警。

例如只关闭 `pku`：

```json
{
  "id": "9",
  "name": "北大",
  "abbr": "pku",
  "enabled": false,
  "x": 66,
  "y": -156,
  "degree": 1
}
```

`fiti_topo.py --dump` 会同时显示启用数量、总数和禁用节点列表。

目录结构：

```text
fiti/                       Python 实现包
├── topo.py                 基础拓扑、CLI 与库接口
├── slice.py                切片实现、CLI 与库接口
├── p4.py                   P4 编译、生成树、端口映射、静态流表
├── p4runtime.py            P4Runtime 客户端
├── control.py              运行时切片控制器
├── runtime.py              P4Runtime + 动态切片运行时
├── ctl.py                  Unix socket 控制客户端
├── traffic.py              随机通信模式
├── log.py                  控制台与日志
├── p4_mininet.py           Mininet P4Host / P4Switch / P4RuntimeSwitch
└── paths.py                项目路径常量

configs/
├── fiti.json               拓扑数据
├── slices.json             切片配置示例
└── parallel.json           并行网络配置

docs/
├── SLICING_DESIGN.md       切片与 P4 数据平面设计
├── PARALLEL_NETWORK_DESIGN.md  中关村并行网络仿真设计
└── fiti_topology.svg       平面拓扑图

parallel/                   并行网络仿真子项目
├── config.py               网络 ID、VLAN、IPv6 规划
├── topology.py             C/E/A/S1/S2/云服务器拓扑
├── switch.py               BMv2 交换机封装
├── control.py              静态 L3VPN/L2VPN 表项下发
└── runtime.py              Mininet 构建与启动

parallel_net.py             并行网络 CLI
p4src/parallel_vpn.p4       VLAN + L2 + L3VPN 数据平面
p4src/fiti_slice.p4         VLAN L2 + 多段 SRv6 P4 程序
fiti_topo.py                基础拓扑 CLI 兼容入口
fiti_slice.py               切片 CLI 兼容入口
fiti_runtime.py             P4Runtime 运行时入口
fiti_ctl.py                 Unix socket 控制入口
Makefile                    编译、运行、检查、清理
requirements-p4runtime.txt  P4Runtime Python 依赖
```

## 并行网络仿真

中关村主干网并行网络场景是独立子项目，默认只启动北京/上海两个站点。
详细设计见 [docs/PARALLEL_NETWORK_DESIGN.md](docs/PARALLEL_NETWORK_DESIGN.md)。

```bash
# 免 root 查看拓扑和地址规划
make parallel-dump

# 编译并行网络 P4
make parallel-p4

# 启动 Mininet/BMv2（需要 root）
make parallel-run

# 后台模式，不进入 CLI
make parallel-run-no-cli
```

当前阶段：

- `E`/`S1`/`S2` 使用 BMv2 + P4；
- `C`/`A`/云服务器/公网边界使用 Linux netns；
- 网络 1/2 为 L3VPN，网络 3/4 为 L2VPN（VXLAN 过渡）；
- L2VPN 使用扁平 /64 模拟跨站点广播域；
- L3VPN、L2VPN 和 C2/FIX 公网接入均使用静态表项验证；EVPN/SRv6 控制面后续实现；
- S1 上行放行并行 VLAN，S2 上行拒绝并行 VLAN。

公网接入验证：

```text
cloud_bj_1 ping6 -c 3 2001:255:0:100::2000
cloud_sh_1 ping6 -c 3 2001:255:0:100::2000
fix ping6 -c 3 240a:a806:1:1::10
fix ping6 -c 3 240a:a806:2:2::10
```

## 环境要求

- Linux
- Mininet 2.3+
- p4c（`p4c-bm2-ss`）
- bmv2（`simple_switch`、`simple_switch_grpc`、`simple_switch_CLI`）
- Python 3.8+
- P4Runtime Python 依赖（建议 venv）
- root 权限（`sudo`）

P4Runtime 依赖建议装在保留系统 site-packages 的 venv 中，这样仍能 import
Mininet：

```bash
make venv
# 等价于：
python3 -m venv --system-site-packages .venv-p4runtime
PYTHONNOUSERSITE=1 .venv-p4runtime/bin/pip install -r requirements-p4runtime.txt
```

`PYTHONNOUSERSITE=1` 避免 pip 把 `~/.local` 中的 protobuf 当成已安装；
否则 `sudo` 运行 venv 时看不到用户 site-packages，会误报 P4Runtime 依赖缺失。

系统 Python 若提示 `externally-managed-environment`，不要直接使用
`pip install --break-system-packages`，优先使用上面的 venv。

异常退出后清理残留：

```bash
sudo pkill -f simple_switch
sudo mn -c
```

## 快速开始

```bash
# 编译 P4
make p4

# 查看拓扑统计（无需 root）
python3 fiti_topo.py --dump

# 基础拓扑
sudo python3 fiti_topo.py
sudo python3 fiti_topo.py --pingall
sudo python3 fiti_topo.py --random-traffic

# 切片拓扑（静态 Thrift 模式）
sudo python3 fiti_slice.py --slices configs/slices.json
sudo python3 fiti_slice.py --slices configs/slices.json --random-traffic
sudo python3 fiti_slice.py --slices configs/slices.json --check

# P4Runtime 运行时：初始切片来自 configs/slices.json，支持运行中增删
sudo .venv-p4runtime/bin/python fiti_runtime.py --slices configs/slices.json

# 另一个终端：查询 / 增删切片
python3 fiti_ctl.py list
python3 fiti_ctl.py add-slice --id demo2 --vlan 105 \
    --subnet 10.6.0.0/24 --hosts bj,sh
python3 fiti_ctl.py remove-slice --id demo2
```

`fiti/topo.py` 检测到 `--slices` 时会转交 `fiti/slice.py`：

```bash
python3 fiti_topo.py --slices configs/slices.json --dump
sudo python3 fiti_topo.py --slices configs/slices.json --check
```

## 命令行参数

### 基础拓扑

| 参数                     | 默认值                  | 说明                                                   |
| ------------------------ | ----------------------- | ------------------------------------------------------ |
| `--topo-file PATH`       | `configs/fiti.json`             | 拓扑 JSON 路径                                         |
| `--dump`                 | 关闭                    | 只打印拓扑统计并退出                                   |
| `--no-hosts`             | 关闭                    | 只创建交换机                                           |
| `--no-tc`                | 关闭                    | 不做链路带宽限制                                       |
| `--bw-map MAP`           | `4:100,2:50,1:10`       | `weight:Mbps` 映射                                     |
| `--p4-json PATH`         | `build/fiti_slice.json` | P4 编译产物路径                                        |
| `--thrift-base PORT`     | 自动分配                | bmv2 Thrift 基址；`s<id>` 使用 `base+id`               |
| `--p4-log-level LEVEL`   | `info`                  | bmv2 日志级别                                          |
| `--pcap` / `--pcap-dir`  | 关闭 / `pcap`           | 抓包与抓包目录                                         |
| `--log-dir DIR`          | `build/logs`            | bmv2 日志目录                                          |
| `--spanning-root ID`     | 第一个节点              | 生成树根节点                                           |
| `--pingall`              | 关闭                    | 启动后执行 pingall                                     |
| `--random-traffic`       | 关闭                    | 不同节点随机互 ping                                    |
| `--traffic-continuous`   | 关闭                    | 持续随机发包，隐含 `--random-traffic`                  |
| `--log LEVEL`            | `info`                  | Mininet 日志级别                                       |

bmv2 没有 STP：`fiti/p4.py` 按 `configs/fiti.json` 计算 BFS 生成树，只启用树边；
FITI 拓扑中 3 条非树边在数据平面被丢弃。

### 切片模式

| 参数 | 说明 |
| --- | --- |
| `--slices PATH` | 切片配置，默认 `configs/slices.json` |
| `--dump` | 只打印规划并退出（免 root） |
| `--check` | 连通性 + 接入端口规则 + 跨切片隔离检查 |
| `--check-srv6` | 在 `--check` 基础上检查 SRv6 |
| `--srv6` | 开启 SRv6 overlay 后进入 CLI |
| `--p4-json` / `--thrift-base` / `--p4-log-level` | 覆盖 bmv2 配置 |
| `--pcap` / `--pcap-dir` / `--log-dir` | 抓包与日志目录 |
| `--spanning-root ID` | 生成树根节点 |
| `--random-traffic` / `--traffic-continuous` | 随机通信 / 持续随机通信 |
| `--log LEVEL` | Mininet 日志级别 |

## 数据平面

- access 端口接主机，P4 压/剥 VLAN tag。
- trunk 端口连接交换机，承载所有切片 VLAN。
- `port_cfg`：按入端口映射 access VLAN 或 trunk。
- `l2_fwd`：按 `(VLAN, 目的 MAC)` 转发；未命中时在 VLAN 内泛洪。
- `egress_port`：access 端口剥 tag，trunk 端口保留 tag。
- 每台交换机、每个 VLAN 一个多播组，用于广播和未知单播。
- SRv6：`encap` 做 H.Encaps，`sid_fib` 转发 SID，`local_sid` 执行
  End/End.X，`vrf_fib` 做 End.DT6。

完整流水线和表结构见 [SLICING_DESIGN.md](docs/SLICING_DESIGN.md)。

## 作为库使用

```python
from fiti.topo import build_net, fiti_network, get_host

net = build_net()                    # 已启动并下发 P4 规则
bj, sh = get_host(net, 1), get_host(net, 2)
print(bj.cmd('ping -c 3', sh.IP()))
net.stop()

with fiti_network() as net:          # 自动清理
    net.pingAll()
```

常用接口：`build_net`、`fiti_network`、`build_topology`、`build_base_host_plan`、
`get_host`、`get_switch`、`load_topology`、`node_by_id`、`switch_name`、
`host_name`、`host_ip`。启动成功后 `net.p4_fabric` 保存生成树和规则规划。

## 网络切片

在启用节点拓扑上用 VLAN 隔离多个切片。每个切片有独立主机、IP 子网和 VLAN，
共享物理链路带宽。设计细节见 [SLICING_DESIGN.md](docs/SLICING_DESIGN.md)。

### 示例切片

| 切片   | VLAN | 子网        | 主机数 | 覆盖                      |
| ------ | ---- | ----------- | ------ | ------------------------- |
| `nor`  | 100  | 10.1.0.0/24 | 14     | 华北 / 东北 / 北京高校    |
| `eas`  | 101  | 10.2.0.0/24 | 11     | 华东                      |
| `sou`  | 102  | 10.3.0.0/24 | 6      | 华南 / 华中               |
| `wes`  | 103  | 10.4.0.0/24 | 10     | 西部                      |
| `demo` | 104  | 10.5.0.0/24 | 6      | 跨区域 / 每节点多切片演示 |

`demo` 与另外四个切片在 6 个节点上重叠，用来验证同一节点承载多个切片。
主机名为 `<切片id>_<abbr>`，如 `nor_bj`、`demo_bj`。

### 配置字段

| 字段                       | 说明                                                     |
| -------------------------- | -------------------------------------------------------- |
| `datapath`                 | 固定 `bmv2`                                              |
| `isolation`                | 固定 `vlan`                                              |
| `p4.json`                  | P4 编译产物路径                                          |
| `p4.thrift_base`           | Thrift 基址；`null` 自动分配                             |
| `p4.log_level`             | `trace/debug/info/warn/error/off`                        |
| `p4.log_dir`               | 日志目录；`null` 使用 `build/logs/`                      |
| `p4.pcap` / `p4.pcap_dir`  | 抓包配置                                                 |
| `p4.spanning_root`         | 生成树根节点；`null` 使用第一个节点                      |
| `defaults.vlan_base`       | 未指定 VLAN 时的起始值                                   |
| `defaults.subnet_base`     | 未指定子网时的起始值                                     |
| `srv6.enabled`             | 静态模式是否默认开启主机 overlay                         |
| `srv6.mode` / `path_mode`  | `switch`；`shortest` / 显式 `paths`                      |
| `srv6.decap_table`         | 静态主机 overlay 解封装路由表，默认 100                  |
| `p4runtime`                | `thrift_base` / `grpc_base`                              |
| `control.socket`           | 运行时 Unix socket 路径                                  |
| `nodes[].enabled`          | 节点是否启动；默认 `true`，`false` 时跳过交换机和主机    |
| `slices[].id`              | 切片标识，也是主机名前缀                                 |
| `slices[].vlan` / `subnet` | 可选，需全局唯一                                         |
| `slices[].hosts`           | 节点 `abbr` 列表或 `"all"`；同一节点可出现在多个切片     |
| `slices[].paths`           | 可选多段路径，`src`/`dst`/`segments`                     |

校验会检查 VLAN / 子网冲突、未知节点、主机名过长，以及不支持的字段。

### SRv6 主机 overlay

用 `--srv6` 或 `"srv6": {"enabled": true}` 开启。SRv6 只在主机命名空间内
封装/解封装，bmv2 只做 L2 承载。

| 用途     | 地址                                  |
| -------- | ------------------------------------- |
| underlay | `2001:db8:<切片序号>::<节点id>/64`    |
| SID      | `2001:db8:<切片序号>:<节点id>::1/128` |
| overlay  | `fd00:<切片序号>::<节点id>/128`       |

仅支持单段 SID；每切片路由数为 O(N²)，建议不超过 20 台主机；主机 MTU 默认 1420。

### 库用法

```python
from fiti.slice import load_slice_config, build_slice_net, SliceManager

cfg = load_slice_config('configs/slices.json')
net = build_slice_net(cfg)              # 已启动并下发 P4 规则
mgr = SliceManager(net, cfg)

print(mgr.ping_slice('nor'))
print(mgr.p4_port_errors())
mgr.enable_srv6()
print(mgr.check_srv6_slice('nor'))
net.stop()
```

常用接口：`build_slice_net`、`ping_slice`、`hosts`、`host`、`p4_port_errors`、
`enable_srv6`、`check_srv6_slice`、`stats`。

## P4Runtime 运行时与动态切片

`fiti/runtime.py` 启动启用节点拓扑，每个启用节点运行 `simple_switch_grpc`，
通过 P4Runtime 下发 pipeline、表项和 multicast group；主机 access 端口动态
创建，端口热插拔走 Thrift 的 `port_add` / `port_remove`。

```bash
sudo .venv-p4runtime/bin/python fiti_runtime.py --slices configs/slices.json
# 或后台模式
sudo .venv-p4runtime/bin/python fiti_runtime.py --slices configs/slices.json --no-cli
```

启动后：

- `configs/slices.json` 中的切片作为初始切片创建；
- 运行时新增切片只保存在内存，不写回 `configs/slices.json`；
- 控制接口默认是 `/tmp/fiti.sock`；
- 切片主机名统一为 `<slice_id>_<abbr>`。

常用 Makefile 目标：

```bash
make runtime
make runtime-no-cli
```

### 控制接口

```bash
python3 fiti_ctl.py list
python3 fiti_ctl.py status

python3 fiti_ctl.py add-slice --id demo2 --vlan 105 \
    --subnet 10.6.0.0/24 --hosts bj,sh

python3 fiti_ctl.py remove-slice --id demo2
```

也可以直接发 JSON：

```json
{"cmd":"add_slice","slice":{"id":"demo2","vlan":105,
  "subnet":"10.6.0.0/24","hosts":["bj","sh"]}}
```

支持 `add_slice`、`remove_slice`、`list_slices`、`status`。
`--vlan`/`--subnet` 省略时自动分配。

### 交换机 SRv6

`p4src/fiti_slice.p4` 支持：

- Ethernet/VLAN/IPv6/SRH 解析；
- H.Encaps：外层 IPv6 + SRH + 内层 IPv6；
- `sid_fib`：中间节点按 `(slice_id, SID)` 转发；
- `local_sid`：按 `(slice_id, SID)` 执行 End / End.X；
- `vrf_fib`：End.DT6，解封装后交给本地主机；
- `local_sid` / `sid_fib` 都按切片隔离，不能用一个 VLAN 的 SRH
  访问另一个 VLAN 的主机；
- 16 段 segment list。

控制面分配：

```text
主机 IPv6:  fd00:<vlan>::<node_id>/64
节点 SID:   fc00:<node_id>:<vlan>::1/128
```

切片内 IPv6 流量会封装成 SRv6；跨切片仍由 VLAN/VRF 隔离。

### 多段路径

`paths` 可选，节点顺序为“第一段 -> 最后一段”，控制面会按 SRH 规则逆序写入：

```json
{
  "id": "demo",
  "vlan": 104,
  "hosts": ["bj", "sh", "xa", "gz", "wh", "cd"],
  "paths": [
    {"src": "bj", "dst": "xa", "segments": ["wh", "cd", "xa"]}
  ]
}
```

路径节点必须是该切片中的节点；最多 16 段。未配置的源/目的对使用最短路径
（单段目的 SID）。

## 随机通信模式

源和目的主机必须位于不同节点。

```bash
# 基础拓扑
sudo python3 fiti_topo.py --random-traffic

# 切片内
sudo python3 fiti_slice.py --slices configs/slices.json --random-traffic

# 允许跨切片：同切片期望可达，跨切片期望隔离
sudo python3 fiti_slice.py --slices configs/slices.json \
    --random-traffic --traffic-scope global

# 持续发包，Ctrl-C 停止
sudo python3 fiti_slice.py --slices configs/slices.json --traffic-continuous
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--random-traffic` | 关闭 | 运行随机通信 |
| `--traffic-continuous` | 关闭 | 持续随机发包，Ctrl-C 停止 |
| `--traffic-scope` | 切片模式 `slice` / 基础模式 `global` | 流范围 |
| `--traffic-flows` | `10` | 并发流数 |
| `--traffic-duration` | `5.0` | 每轮持续时间，秒 |
| `--traffic-interval` | `0.2` | ping 间隔，秒 |
| `--traffic-size` | `56` | ping 负载大小 |
| `--traffic-seed` | 自动生成 | 随机种子 |

Makefile：

```bash
make traffic
make traffic-global
make traffic-continuous
make traffic-global-continuous
make traffic-base-continuous
```

## 日志与调试

控制台保留 `*** Adding hosts:`、`*** Adding switches:`，隐藏链路明细；
启动、流表下发和 SRv6 安装显示进度条 / 动画。

```text
build/logs/mininet.log        # 完整 Mininet 输出
build/logs/fiti-install.log   # P4 流表下发明细
build/logs/bmv2-*.log         # bmv2 日志
```

Thrift 端口从 9090 起自动分配，`s<id>` 使用 `base+id`；实际基址以启动输出为准。

```bash
simple_switch_CLI --thrift-port 9091
RuntimeCmd: table_dump IngressImpl.port_cfg
RuntimeCmd: table_dump IngressImpl.l2_fwd
RuntimeCmd: mc_dump

sudo python3 fiti_slice.py --slices configs/slices.json --pcap --pcap-dir pcap
```

## 已知限制

- 生成树静态，非树边不转发，链路故障不会自动切换。
- 单播表项静态下发，不做 MAC 学习。
- 泛洪在整棵生成树上扩散，没有按 VLAN 剪枝。
- 所有切片共享物理链路带宽。
- 每台交换机是一个独立的 bmv2 进程，节点数多时占用较多内存和 CPU。
- P4Runtime 运行时需要 `grpcio` / `p4runtime`，并且要保留 Thrift 做端口热插拔。
- 运行时新增切片不持久化，重启后只恢复 `configs/slices.json` 中的初始切片。
- SRv6 封装增加约 40 + 8 + 16N 字节，主机 MTU 需要相应调小。
- SRv6 无动态路由/NDP 控制协议，SID 下一跳和邻居信息由控制面静态计算。
