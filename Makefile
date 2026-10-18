P4C      ?= p4c-bm2-ss
P4_SRC    = p4src/fiti_slice.p4
P4_JSON   = build/fiti_slice.json

.PHONY: all p4 p4-force run run-slices traffic traffic-base traffic-global \
        traffic-continuous traffic-base-continuous traffic-global-continuous \
        check check-srv6 dump clean

all: p4

p4: $(P4_JSON)
	@echo "P4 JSON 已就绪: $(P4_JSON)"

# 强制重新编译（源码时间戳没变但想刷新产物时用）
p4-force:
	@mkdir -p build
	$(P4C) --arch v1model -o $(P4_JSON) $(P4_SRC)

$(P4_JSON): $(P4_SRC)
	@mkdir -p build
	$(P4C) --arch v1model -o $@ $<

# 基础拓扑：每节点一台主机，全部在 VLAN 100
run: $(P4_JSON)
	sudo python3 fiti_topo.py

# 切片拓扑：5 个切片，47 台主机；其中 6 个节点承载两个切片
run-slices: $(P4_JSON)
	sudo python3 fiti_slice.py --slices slices.json

# 随机通信：不同节点随机互 ping
traffic: $(P4_JSON)
	sudo python3 fiti_slice.py --slices slices.json --random-traffic

traffic-base: $(P4_JSON)
	sudo python3 fiti_topo.py --random-traffic

# 跨切片随机通信：同切片期望可达，跨切片期望隔离
traffic-global: $(P4_JSON)
	sudo python3 fiti_slice.py --slices slices.json --random-traffic --traffic-scope global

# 持续随机发包，Ctrl-C 停止
traffic-continuous: $(P4_JSON)
	sudo python3 fiti_slice.py --slices slices.json --random-traffic --traffic-continuous

traffic-base-continuous: $(P4_JSON)
	sudo python3 fiti_topo.py --random-traffic --traffic-continuous

traffic-global-continuous: $(P4_JSON)
	sudo python3 fiti_slice.py --slices slices.json --random-traffic --traffic-continuous --traffic-scope global

check: $(P4_JSON)
	sudo python3 fiti_slice.py --slices slices.json --check

check-srv6: $(P4_JSON)
	sudo python3 fiti_slice.py --slices slices.json --check-srv6

dump:
	python3 fiti_topo.py --dump
	python3 fiti_slice.py --slices slices.json --dump

clean:
	rm -rf build pcap
	-sudo pkill -f simple_switch
	-sudo mn -c
