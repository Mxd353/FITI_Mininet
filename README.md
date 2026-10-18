# FITI Mininet + bmv2 拓扑

用 Mininet 模拟 `fiti.json` 中的 FITI 骨干拓扑。每个节点运行一台 bmv2
`simple_switch`，加载 `p4src/fiti_slice.p4` 做 VLAN 感知的二层转发；
同一台交换机可以同时承载多个切片的主机。

## 拓扑概况

| 项目     | 说明                                                              |
| -------- | ----------------------------------------------------------------- |
| 节点     | 41 个（每个节点一台 bmv2 交换机 `s<id>`）                         |
| 骨干链路 | 43 条                                                             |
| 主机地址 | 基础模式 `10.0.0.<id>/24`；切片模式 `10.<切片序号>.0.<节点id>/24` |
| 链路权重 | `weight` 4/2/1 映射为 100/50/10 Mbps                              |
| 数据平面 | bmv2 `simple_switch`，P4-16 / v1model                             |
| 控制平面 | `fiti_p4.py`：生成树 + Thrift 静态流表，不需要外部控制器          |

文件：

- `fiti.json`：拓扑数据
- `fiti_topo.py`：基础拓扑、CLI 与库接口
- `fiti_slice.py`：网络切片 CLI 与库接口
- `fiti_p4.py`：P4 编译、生成树、端口映射、流表下发
- `fiti_traffic.py`：随机通信模式
- `fiti_log.py`：控制台输出与日志文件
- `p4_mininet.py`：Mininet 的 `P4Host` / `P4Switch`
- `p4src/fiti_slice.p4`：VLAN 感知 L2 转发 P4 程序
- `slices.json`：切片配置示例
- `SLICING_DESIGN.md`：切片与 P4 数据平面设计
- `Makefile`：编译、运行、检查、清理
- `fiti_topology.svg`：平面拓扑图

## 环境要求

- Linux
- Mininet 2.3+
- p4c（`p4c-bm2-ss`）
- bmv2（`simple_switch`、`simple_switch_CLI`）
- Python 3.8+
- root 权限（`sudo`）

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

# 切片拓扑（5 个切片 / 47 台主机）
sudo python3 fiti_slice.py --slices slices.json
sudo python3 fiti_slice.py --slices slices.json --random-traffic
sudo python3 fiti_slice.py --slices slices.json --check
sudo python3 fiti_slice.py --slices slices.json --check-srv6
```

`fiti_topo.py` 检测到 `--slices` 时会转交 `fiti_slice.py`：

```bash
python3 fiti_topo.py --slices slices.json --dump
sudo python3 fiti_topo.py --slices slices.json --check
```

## 命令行参数

### 基础拓扑

| 参数                     | 默认值                  | 说明                                                   |
| ------------------------ | ----------------------- | ------------------------------------------------------ |
| `--topo-file PATH`       | `fiti.json`             | 拓扑 JSON 路径                                         |
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

bmv2 没有 STP：`fiti_p4.py` 按 `fiti.json` 计算 BFS 生成树，只启用树边；
FITI 拓扑中 3 条非树边在数据平面被丢弃。

### 切片模式

| 参数 | 说明 |
| --- | --- |
| `--slices PATH` | 切片配置，默认 `slices.json` |
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

完整流水线和表结构见 [SLICING_DESIGN.md](SLICING_DESIGN.md)。

## 作为库使用

```python
from fiti_topo import build_net, fiti_network, get_host

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

在 41 节点拓扑上用 VLAN 隔离多个切片。每个切片有独立主机、IP 子网和 VLAN，
共享物理链路带宽。设计细节见 [SLICING_DESIGN.md](SLICING_DESIGN.md)。

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
| `srv6.enabled`             | 是否默认开启 SRv6 overlay                                |
| `srv6.decap_table`         | 解封装路由表，默认 100                                   |
| `slices[].id`              | 切片标识，也是主机名前缀                                 |
| `slices[].vlan` / `subnet` | 可选，需全局唯一                                         |
| `slices[].hosts`           | 节点 `abbr` 列表或 `"all"`；同一节点可出现在多个切片     |

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
from fiti_slice import load_slice_config, build_slice_net, SliceManager

cfg = load_slice_config('slices.json')
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

## 随机通信模式

源和目的主机必须位于不同节点。

```bash
# 基础拓扑
sudo python3 fiti_topo.py --random-traffic

# 切片内
sudo python3 fiti_slice.py --slices slices.json --random-traffic

# 允许跨切片：同切片期望可达，跨切片期望隔离
sudo python3 fiti_slice.py --slices slices.json \
    --random-traffic --traffic-scope global

# 持续发包，Ctrl-C 停止
sudo python3 fiti_slice.py --slices slices.json --traffic-continuous
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

sudo python3 fiti_slice.py --slices slices.json --pcap --pcap-dir pcap
```

## 已知限制

- 生成树静态，非树边不转发，链路故障不会自动切换。
- 单播表项静态下发，不做 MAC 学习。
- 泛洪在整棵生成树上扩散，没有按 VLAN 剪枝。
- 所有切片共享物理链路带宽。
- 每台交换机是一个独立的 `simple_switch` 进程，41 节点占用较多内存和 CPU。
- bmv2 只做 L2；SRv6 只在主机侧生效。
