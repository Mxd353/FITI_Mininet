P4C      ?= p4c-bm2-ss
PARALLEL_P4_JSON  = build/parallel_vpn.json
PARALLEL_P4INFO   = build/parallel_vpn.p4info.txt

.PHONY: all clean parallel-p4 parallel-dump parallel-evpn-dump \
        parallel-dump-min parallel-evpn-dump-min test-evpn \
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

# 4 站点最小验证子集：北京/上海/广州/成都，12 台 BMv2
parallel-dump-min:
	python3 parallel_net.py --config configs/parallel-min.json --dump

parallel-evpn-dump-min:
	python3 parallel_evpn.py --config configs/parallel-min.json --dump --validate

test-evpn:
	python3 -m unittest tests.test_evpn

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
