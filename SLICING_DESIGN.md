# FITI 拓扑网络切片设计说明

状态：已实现（VLAN 多租户 + SRv6 主机 overlay）。桥接模式、每切片独立控制器和每切片带宽**不再实现**。

配套文件：

- `fiti_slice.py`：切片实现、CLI 与库接口
- `slices.json`：切片配置
- `README.md`：安装、命令和配置字段说明

## 1. 范围

目标：

- 在 41 节点共享物理拓扑上并存多个 VLAN 隔离的切片
- 每个切片有独立主机、IPv4 子网和 VLAN
- 切片内可选启用 SRv6 主机 overlay
- 切片内互通，切片间默认不可达

非目标（已明确放弃）：

- 桥接模式（每切片独立 OVS bridge、独立链路）
- 每切片链路带宽保障
- 每切片独立控制器
- 5G 端到端切片编排、FlowVisor 兼容

## 2. 架构

- 物理层复用 `fiti.json` 的 41 台 OVS 交换机和 43 条骨干链路
- 交换机运行 OVS standalone + STP，避免环路广播风暴
- 交换机之间为 trunk，承载所有切片 VLAN
- 每个切片在 `hosts` 指定的节点上创建独立主机 `<切片id>_<abbr>`
- 主机接入端口配置为 access 端口，打对应 VLAN tag
- VLAN 提供二层隔离；切片间没有三层路由
- SRv6 只在主机命名空间内封装/解封装，OVS 不感知 SRH

```text
nor_bj ─┐                    ┌─ eas_sh
        ├─ s1 ═════ s2 ═════ s3 ─┤
sou_gz ─┘   (trunk, VLAN 100/101/102/103)   └─ wes_xa
```

## 3. 命名与地址

| 对象 | 规则 |
|---|---|
| 主机名 | `<切片id>_<abbr>`，如 `nor_bj`、`eas_sh` |
| 主机 IPv4 | `10.<切片序号>.0.<节点id>/24` |
| VLAN | 1–4094，切片间唯一，默认从 100 起 |
| 交换机 | `s<节点id>`，共享 |
| SRv6 underlay | `2001:db8:<切片序号>::<节点id>/64` |
| SRv6 SID | `2001:db8:<切片序号>:<节点id>::1/128` |
| SRv6 overlay | `fd00:<切片序号>::<节点id>/128` |

主机名同时受 Linux 接口名 15 字符限制（主机侧接口为 `<host>-eth0`），配置校验会检查。

## 4. 配置模型

`slices.json` 的核心字段：

- 全局：`topo`、`isolation`（固定 `vlan`）、`controller`、`defaults`、`srv6`
- 切片：`id`、`name`、`vlan`、`subnet`、`hosts`

校验规则：

- `id`、`vlan`、`subnet` 全局唯一，子网不得重叠
- `hosts` 必须是合法节点 `abbr` 列表或 `"all"`
- VLAN 必须在 1–4094，主机 IP 必须落在对应子网内
- 已放弃的字段 `nodes`、切片级 `controller`、`bw_share` 会直接报错，避免误配置
- `bridge` 隔离模式会直接报错

字段细节和示例见 `README.md`。

## 5. SRv6 主机 overlay

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
- 配置命令已在用户命名空间 + veth 环境验证：双向 `ping6` 0% 丢包

能力边界：

- 只支持单段 SID，不支持交换机逐跳 SRv6（End/End.X/End.DX6）和多段路径
- 无 SRv6 控制面（IS-IS/OSPFv3/BGP SRv6），路由由 `SliceManager` 静态下发
- 每切片 O(N²) 条路由，建议每切片不超过 20 台主机

## 6. 容量估算

以下为当前开发机（20 核 / 7.6 GB / WSL2 / OVS kernel datapath）的工程估算，实际值需压测：

| 模式 | 每切片主机数 | 估算切片数 | 主要瓶颈 |
|---|---|---|---|
| VLAN | 6 | 30–50 | 主机 namespace、veth、OVS 端口 |
| VLAN | 41（全节点） | 10–20 | 内存、主机总数 |

- 协议上限是 802.1Q 的 4094 个可用 VLAN，实际会先撞到主机和内存限制
- 桥接模式已放弃，不参与容量规划
- 当前实测 4 个切片、41 台主机可正常启动并通过连通性检查

## 7. 已知限制

- 所有切片共享物理链路带宽，没有每切片带宽保障
- VLAN 模式推荐单个控制器；共享交换机的 packet-in 无法天然按切片隔离，控制器需 VLAN-aware
- STP 是所有 VLAN 共用的一棵生成树，不能按切片独立选路
- 切片只隔离网络命名空间，CPU / 内存默认不隔离
- SRv6 只在主机侧生效，不能用于验证 P4/Tofino 的逐跳行为

## 8. 验收结果

2026-10-17 在本机运行 `--check`（4 切片 / 41 主机）：

| 检查项 | 结果 |
|---|---|
| 切片内 IPv4 连通性 | 4 个切片全部 0% 丢包（182/110/30/90） |
| VLAN 标签 | 41 个接入端口全部正确 |
| 跨切片 IPv4 | 6 组两两检查全部 100% 丢包 |
| 总体 | 全部通过 |

SRv6 检查可运行：

```bash
sudo python3 fiti_slice.py --slices slices.json --check-srv6
```

## 9. 后续

不再规划桥接模式或每切片带宽。若后续确有需求，应作为独立项目评估：

- 动态增删切片
- 基于 OVS QoS 的 VLAN 限速
- 桥接模式与每切片独立控制器
