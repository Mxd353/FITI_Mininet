# slices.json 配置写法

`configs/slices.json` 是 FITI 切片场景的配置文件，由 `fiti/slice.py`
和 `fiti/runtime.py` 读取。完整示例见 `configs/slices.json`，本文给出
最小写法、字段说明和运行时新增切片格式。

## 最小示例

```json
{
  "topo": "fiti.json",
  "datapath": "bmv2",
  "isolation": "vlan",
  "defaults": {
    "vlan_base": 100,
    "subnet_base": 10
  },
  "slices": [
    {
      "id": "nor",
      "name": "华北",
      "vlan": 100,
      "subnet": "10.1.0.0/24",
      "hosts": ["bj", "tjn", "sjz"]
    },
    {
      "id": "eas",
      "name": "华东",
      "vlan": 101,
      "subnet": "10.2.0.0/24",
      "hosts": ["sh", "nj", "hzh"]
    }
  ]
}
```

`topo` 相对配置文件所在目录解析。`hosts` 使用 `configs/fiti.json` 中的
节点 `abbr`，主机名自动生成如下：

```text
<slice id>_<node abbr>
例如：nor_bj、eas_sh
```

## 完整字段

```json
{
  "version": 1,
  "topo": "fiti.json",
  "datapath": "bmv2",
  "isolation": "vlan",
  "p4runtime": {
    "thrift_base": null,
    "grpc_base": null
  },
  "control": {
    "socket": "/tmp/fiti.sock"
  },
  "p4": {
    "json": "build/fiti_slice.json",
    "thrift_base": null,
    "log_level": "info",
    "log_dir": null,
    "pcap": false,
    "pcap_dir": "pcap",
    "spanning_root": null
  },
  "defaults": {
    "vlan_base": 100,
    "subnet_base": 10
  },
  "srv6": {
    "enabled": false,
    "underlay_prefix": "2001:db8",
    "overlay_prefix": "fd00",
    "decap_table": 100,
    "mtu": 1420
  },
  "slices": [
    {
      "id": "demo",
      "name": "跨区域演示",
      "vlan": 104,
      "subnet": "10.5.0.0/24",
      "hosts": ["bj", "sh", "xa", "gz"],
      "paths": [
        {
          "src": "bj",
          "dst": "gz",
          "segments": ["wh", "cd", "gz"]
        }
      ]
    }
  ]
}
```

## 顶层字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `topo` | 否 | 拓扑文件，默认 `fiti.json`，相对配置文件目录解析 |
| `datapath` | 否 | 目前只支持 `bmv2`；`p4` 会归一化为 `bmv2` |
| `isolation` | 否 | 目前只支持 `vlan` |
| `defaults` | 否 | 默认 VLAN 和子网基数 |
| `p4` | 否 | 静态 Thrift 模式的 P4/bmv2 参数 |
| `p4runtime` | 否 | P4Runtime 运行时模式的 Thrift/gRPC 基址 |
| `control` | 否 | P4Runtime 运行时的 Unix socket |
| `srv6` | 否 | 主机 SRv6 overlay 配置 |
| `slices` | 是 | 非空切片列表 |
| `version` | 否 | 配置版本，当前解析器不依赖该字段 |

## slices[] 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 切片 ID，只允许字母、数字、下划线；同时作为主机名前缀 |
| `name` | 否 | 显示名称，默认等于 `id` |
| `vlan` | 否 | VLAN ID，1～4094，必须全局唯一；默认 `vlan_base + index - 1` |
| `subnet` | 否 | IPv4 子网，不能与其他切片重叠；默认 `subnet_base.index.0.0/24` |
| `hosts` | 是 | 节点 `abbr` 列表，或字符串 `"all"`；同一节点可出现在多个切片 |
| `paths` | 否 | P4Runtime 运行时的多段 SRv6 路径列表 |

### hosts 写法

```json
"hosts": ["bj", "sh", "gz"]
```

或者使用所有已启用节点：

```json
"hosts": "all"
```

注意：

- 节点必须在 `configs/fiti.json` 中存在；
- `enabled: false` 的节点不能出现在切片中；
- 主机名 `<id>_<abbr>` 加上 `-eth0` 后不能超过 15 个字符；
- 同一节点可以同时属于多个切片，用于验证节点内多切片。

### paths 写法

```json
"paths": [
  {
    "src": "bj",
    "dst": "gz",
    "segments": ["wh", "cd", "gz"]
  }
]
```

约束：

- `src`、`dst` 必须属于该切片；
- `segments` 中的节点也必须属于该切片；
- `src` 不能出现在 `segments` 中；
- 最多 16 段；
- 相邻两段不能落在同一台交换机上；
- 如果省略 `segments`，运行时按 `[dst]` 处理。

`paths` 主要用于 `fiti_runtime.py` 的多段 SRv6 路径规划。静态
`fiti_slice.py` 场景不依赖该字段。

## defaults

```json
"defaults": {
  "vlan_base": 100,
  "subnet_base": 10
}
```

- `vlan_base` 默认 100，范围 1～4094；
- 切片未写 `vlan` 时使用 `vlan_base + 切片序号 - 1`；
- 切片未写 `subnet` 时使用 `subnet_base.切片序号.0.0/24`；
- `subnet_base` 默认 10。

例如第一个切片会自动得到：

```text
VLAN 100
子网 10.1.0.0/24
```

## p4 配置

静态 Thrift 模式使用：

```json
"p4": {
  "json": "build/fiti_slice.json",
  "thrift_base": null,
  "log_level": "info",
  "log_dir": null,
  "pcap": false,
  "pcap_dir": "pcap",
  "spanning_root": null
}
```

| 字段 | 说明 |
| --- | --- |
| `json` | P4 编译产物路径 |
| `thrift_base` | bmv2 Thrift 基址；`null` 自动分配 |
| `log_level` | `trace/debug/info/warn/error/off` |
| `log_dir` | bmv2 日志目录；`null` 使用 `build/logs/` |
| `pcap` | 是否抓包 |
| `pcap_dir` | 抓包目录 |
| `spanning_root` | 生成树根节点；`null` 使用第一个节点 |

## P4Runtime 运行时配置

```json
"p4runtime": {
  "thrift_base": null,
  "grpc_base": null
},
"control": {
  "socket": "/tmp/fiti.sock"
}
```

- `p4runtime.thrift_base`：运行时端口热插拔使用的 Thrift 基址；
- `p4runtime.grpc_base`：P4Runtime gRPC 基址；
- `control.socket`：运行时控制接口路径。

## SRv6 配置

```json
"srv6": {
  "enabled": false,
  "underlay_prefix": "2001:db8",
  "overlay_prefix": "fd00",
  "decap_table": 100,
  "mtu": 1420
}
```

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否默认开启主机 SRv6 overlay |
| `underlay_prefix` | SRv6 underlay 前缀 |
| `overlay_prefix` | overlay 地址前缀 |
| `decap_table` | 主机解封装路由表 ID，1～252 |
| `mtu` | 主机 MTU，1280～1500 |

## 运行时新增切片

P4Runtime 运行时除了读取 `slices.json`，还可以通过 Unix socket 动态
新增切片。请求格式：

```json
{
  "cmd": "add_slice",
  "slice": {
    "id": "demo2",
    "name": "运行时演示",
    "vlan": 105,
    "subnet": "10.6.0.0/24",
    "hosts": ["bj", "sh"],
    "paths": []
  }
}
```

命令行：

```bash
python3 fiti_ctl.py add-slice \
  --id demo2 \
  --vlan 105 \
  --subnet 10.6.0.0/24 \
  --hosts bj,sh
```

运行时新增切片只保存在内存，不写回 `slices.json`。

## 校验规则

加载配置时会检查：

- 切片 ID 非空且格式合法；
- VLAN 在 1～4094 且不重复；
- 子网合法且不重叠；
- `hosts` 非空；
- 节点存在且未禁用；
- 节点不重复；
- 主机名长度满足 Linux 接口限制；
- `paths` 是列表。

## 常用命令

```bash
# 只打印规划，免 root
python3 fiti_slice.py --slices configs/slices.json --dump

# 启动静态切片场景
sudo python3 fiti_slice.py --slices configs/slices.json

# 连通性、隔离和 SRv6 检查
sudo python3 fiti_slice.py --slices configs/slices.json --check
sudo python3 fiti_slice.py --slices configs/slices.json --check-srv6

# P4Runtime 运行时，初始切片来自 slices.json
sudo .venv-p4runtime/bin/python fiti_runtime.py \
  --slices configs/slices.json
```
