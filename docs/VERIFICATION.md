# 并行网络功能验收

本文描述接入真实平台前的功能验证（conformance）流程。判定标准是 **pass / fail**，不是吞吐和时延；性能数字留给真实设备。

## 快速开始

```bash
make parallel-stop      # 清理旧实例
make verify-dry         # 免 root，只校验用例与配置
make verify-min         # 4 站点，26 条用例
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

运行时会逐条打印进度，跑完给汇总：

```text
==> 功能验收：26 条用例，配置 configs/parallel-min.json，标签 全部
[ 1/26] l3vpn-net1-bj-sh         PASS    2.1s
[ 2/26] l3vpn-net1-bj-tjn        PASS    2.0s
[ 3/26] l3vpn-net1-bj-jnn        PASS    2.0s
[ 4/26] iso-l2-net3-to-net4      FAIL    4.0s
        期望 100% 丢包，实际 0%（收到 3）
[ 5/26] vlan-policy-bj           SKIP    0.0s  缺少站点: gz

通过 24，失败 1，跳过 1，共 26（用例耗时 52.3s）
最慢用例 4.0s
报告: runs/20261029-160000
```

执行中终端会显示 spinner 和 ETA；非 TTY 环境（CI、重定向）自动关闭动画。

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
| `01-reachability.yaml` | L3VPN/L2VPN 同网络互通、BUM、公网 | 8 |
| `02-isolation.yaml` | 跨网络必须 100% 丢包 | 4 |
| `03-vlan.yaml` | S1 放行、S2 拒绝并行 VLAN | 4 |
| `04-control-plane.yaml` | SID、FIB、OVS trunk、策略路由 | 8 |
| `05-dynamic.yaml` | EVPN agent 动态增删路由 | 2 |
| `06-faults.yaml` | 链路 down/up（默认跳过） | 1 |

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
        iface: v3501
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
最终落到主表；而主表里有内核为所有 VLAN 子接口自动生成的直连路由，
会把报文从**错误切片的子接口**发出去，造成跨切片注入。默认路由指向本网络的
互联地址（`240a:a806:{网络}:{100+站点}::1`），报文就留在自己的 VSI 内。


## 需求追溯矩阵

| PDF 要求 | 用例 | 证据 |
| --- | --- | --- |
| VLAN 3501-3900 | `vlan-policy-*` | OVS trunks |
| S1 上行放行 | `vlan-policy-*` | `contains: 3501..3504` |
| S2 上行拒绝 | `vlan-policy-*` | `not_contains: 3501..3504` |
| L3VPN 同网络互通 | `l3vpn-*` | ping |
| L3VPN 跨网络隔离 | `iso-*` | ping 100% loss |
| L2VPN 同广播域 | `l2vpn-*` | ping |
| L2VPN VNI 隔离 | `iso-l2-net3-to-net4` | ping 100% loss |
| BUM 复制 | `bum-net3` | ff02::1 |
| 公网接入 | `public-fix` | ping FIX |
| SRv6 SID 安装 | `e-bj-local-sid-*` | `local_sid` |
| L3/L2 FIB 安装 | `e-bj-l3-route-to-sh`、`e-bj-l2-tunnel-to-sh` | `l3_fib`/`l2_tunnel` |
| 策略路由 | `cl-bj-policy-rule` | `ip -6 rule` |
| 动态路由增删 | `evpn-*-add-withdraw` | agent + 表项 |
| BGP EVPN 会话 | 无 | 未实现 |
| 链路故障收敛 | `link-down-bj-tjn` | 只能验证 down/up，不做收敛 |

## 能验证与不能验证

| 能验证 | 不能验证 |
| --- | --- |
| VLAN 接入与过滤 | 线速转发、队列、缓存 |
| VRF/VSI 隔离 | 真实 BGP 邻居与 NLRI |
| SRv6 封装/解封装语义 | SRv6 压缩 SID、TE |
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

## 动态路由更新的并发

EVPN agent 默认并发更新 16 台 E（每台一个 `simple_switch_CLI` 进程）：

```bash
PARALLEL_EVPN_WORKERS=8 sudo python3 verify/runner.py ...
PARALLEL_EVPN_WORKERS=1 sudo python3 verify/runner.py ...   # 退回串行
```

41 站点下一次 L3 advertise 预计从约 12.6 秒降到数秒级，具体以实测为准。

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
