## 网络 1 · L3 · VLAN 3501

**业务路径**：`cl_bj_1` → `cl_sh_1`  
**内层地址**：`240a:a806:1:1::10` → `240a:a806:1:2::10`

| 段 | 物理路径 | 封装 | 说明 |
| --- | --- | --- | --- |
| 1 | `cl_bj_1 → tp_bj_1_1_1` | untagged | VM 网卡，普通 IP 报文 |
| 2 | `tp_bj_1_1_1 → br_bj_1` | untagged | 宿主机桥 access 口（VLAN 3501） |
| 3 | `br_bj_1 → S1_bj` | VLAN 3501 | 桥上联到站点交换机 |
| 4 | `S1_bj → E_bj` | VLAN 3501 | 上行到 PE |
| 5 | `E_bj → C_bj` | SRv6 L3  SA=fd00:0:1:1::1:1  DA=fd00:0:2:1::1:1 | PE 封装并交给核心 |
| 6 | `C_bj → C_tjn` | IPv6 外层（SRv6） | 核心转发 |
| 7 | `C_tjn → C_jnn` | IPv6 外层（SRv6） | 核心转发 |
| 8 | `C_jnn → C_sh` | IPv6 外层（SRv6） | 核心转发 |
| 9 | `C_sh → E_sh` | IPv6 外层（SRv6） | 到达目的 PE |
| 10 | `E_sh → S1_sh` | VLAN 3501 | 解封装后按内层 /128 查表投递到主机 |
| 11 | `S1_sh → br_sh_1` | VLAN 3501 | 交付到目的 PM 的桥 |
| 12 | `br_sh_1 → tp_sh_1_1_1` | untagged | 宿主机桥 access 口 |
| 13 | `tp_sh_1_1_1 → cl_sh_1` | untagged | 交付给目的 VM |
