P4C      ?= p4c-bm2-ss
PARALLEL_P4_JSON  = build/parallel_vpn.json
PARALLEL_P4INFO   = build/parallel_vpn.p4info.txt

.PHONY: all clean parallel-p4 parallel-dump parallel-evpn-dump \
        parallel-dump-min parallel-evpn-dump-min test test-evpn test-ovs \
        test-verify verify-dry verify-min verify-full verify-isolation \
        verify-faults render-config \
        parallel-run parallel-run-no-cli parallel-run-min \
        parallel-run-no-cli-min parallel-stop

all: parallel-p4

clean:
	rm -rf build pcap
	-sudo pkill -f simple_switch
	-sudo mn -c
	-rm -f /tmp/parallel_evpn.sock

parallel-p4:
	@mkdir -p build
	$(P4C) --arch v1model --p4runtime-files $(PARALLEL_P4INFO) \
		-o $(PARALLEL_P4_JSON) p4src/parallel_vpn.p4

parallel-dump:
	python3 parallel_net.py --dump

parallel-evpn-dump:
	python3 parallel_evpn.py --dump --validate

# 4 站点最小验证子集：北京/上海/天津/济南，4 台 BMv2 + 8 台 OVS
parallel-dump-min:
	python3 parallel_net.py --config configs/parallel-min.json --dump

parallel-evpn-dump-min:
	python3 parallel_evpn.py --config configs/parallel-min.json --dump --validate

test: test-evpn test-ovs test-verify

test-evpn:
	python3 -m unittest tests.test_evpn

test-ovs:
	python3 -m unittest tests.test_ovs

test-verify:
	python3 -m unittest tests.test_verify

# 功能验收：不需要 root，只校验用例与配置
verify-dry:
	python3 verify/runner.py --config configs/parallel-min.json --dry-run

# 功能验收：4 站点最小拓扑
verify-min: parallel-p4
	sudo python3 verify/runner.py --config configs/parallel-min.json

# 功能验收：41 站点全量
verify-full: parallel-p4
	sudo python3 verify/runner.py --config configs/parallel.json

# 只跑隔离或故障用例
verify-isolation:
	sudo python3 verify/runner.py --config configs/parallel-min.json \
		--tag isolation

verify-faults:
	sudo python3 verify/runner.py --config configs/parallel-min.json \
		--tag faults

# 重新生成真实设备配置基线
render-config:
	python3 tools/render_config.py --config configs/parallel-min.json \
		--target huawei --out configs/expected/huawei-min.txt

parallel-run: parallel-p4
	sudo python3 parallel_net.py

parallel-run-no-cli: parallel-p4
	sudo python3 parallel_net.py --no-cli

parallel-run-min: parallel-p4
	sudo python3 parallel_net.py --config configs/parallel-min.json

parallel-run-no-cli-min: parallel-p4
	sudo python3 parallel_net.py --config configs/parallel-min.json --no-cli

parallel-stop:
	-sudo pkill -9 -f simple_switch
	-sudo pkill -9 -f parallel_net.py
	-sudo mn -c
	-rm -f /tmp/parallel_evpn.sock
