P4C      ?= p4c-bm2-ss
PYTHON   ?= python3
VENV      = .venv-p4runtime
VENV_PYTHON = $(CURDIR)/$(VENV)/bin/python
P4_SRC    = p4src/fiti_slice.p4
P4_JSON   = build/fiti_slice.json
P4INFO    = build/fiti_slice.p4info.txt
SLICES    = configs/slices.json

.PHONY: all p4 p4-force venv run run-slices runtime runtime-no-cli \
        traffic traffic-base traffic-global \
        traffic-continuous traffic-base-continuous traffic-global-continuous \
        check check-srv6 dump clean

all: p4

p4: $(P4_JSON)
	@echo "P4 JSON 已就绪: $(P4_JSON)"

# 强制重新编译（源码时间戳没变但想刷新产物时用）
p4-force:
	@mkdir -p build
	$(P4C) --arch v1model --p4runtime-files $(P4INFO) -o $(P4_JSON) $(P4_SRC)

$(P4_JSON): $(P4_SRC)
	@mkdir -p build
	$(P4C) --arch v1model --p4runtime-files $(P4INFO) -o $@ $<

# P4Runtime Python 依赖：venv 保留系统 site-packages，便于继续使用 Mininet。
# PYTHONNOUSERSITE=1 避免 pip 把 ~/.local 里的 protobuf 当成已安装，
# 否则 root 运行 venv 时看不到用户 site-packages。
VENV_STAMP = $(VENV)/.fiti-ready

$(VENV_STAMP): requirements-p4runtime.txt
	$(PYTHON) -m venv --system-site-packages $(VENV)
	PYTHONNOUSERSITE=1 $(VENV_PYTHON) -m pip install -r requirements-p4runtime.txt
	@touch $@

venv: $(VENV_STAMP)
	@echo "venv 已就绪: $(VENV)"

# 基础拓扑：每节点一台主机，全部在 VLAN 100
run: $(P4_JSON)
	sudo python3 fiti_topo.py

# 切片拓扑：5 个切片，47 台主机；其中 6 个节点承载两个切片
run-slices: $(P4_JSON)
	sudo python3 fiti_slice.py --slices $(SLICES)

# P4Runtime 运行时：初始切片 + Unix socket 动态增删
runtime: $(P4_JSON) $(VENV_STAMP)
	sudo $(VENV_PYTHON) fiti_runtime.py --slices $(SLICES)

runtime-no-cli: $(P4_JSON) $(VENV_STAMP)
	sudo $(VENV_PYTHON) fiti_runtime.py --slices $(SLICES) --no-cli

# 随机通信：不同节点随机互 ping
traffic: $(P4_JSON)
	sudo python3 fiti_slice.py --slices $(SLICES) --random-traffic

traffic-base: $(P4_JSON)
	sudo python3 fiti_topo.py --random-traffic

# 跨切片随机通信：同切片期望可达，跨切片期望隔离
traffic-global: $(P4_JSON)
	sudo python3 fiti_slice.py --slices $(SLICES) --random-traffic --traffic-scope global

# 持续随机发包，Ctrl-C 停止
traffic-continuous: $(P4_JSON)
	sudo python3 fiti_slice.py --slices $(SLICES) --random-traffic --traffic-continuous

traffic-base-continuous: $(P4_JSON)
	sudo python3 fiti_topo.py --random-traffic --traffic-continuous

traffic-global-continuous: $(P4_JSON)
	sudo python3 fiti_slice.py --slices $(SLICES) --random-traffic --traffic-continuous --traffic-scope global

check: $(P4_JSON)
	sudo python3 fiti_slice.py --slices $(SLICES) --check

check-srv6: $(P4_JSON)
	sudo python3 fiti_slice.py --slices $(SLICES) --check-srv6

dump:
	python3 fiti_topo.py --dump
	python3 fiti_slice.py --slices $(SLICES) --dump

clean:
	rm -rf build pcap
	-sudo pkill -f simple_switch
	-sudo mn -c
