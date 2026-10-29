#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FITI physical topology used as the parallel-network core."""

from __future__ import annotations

import heapq
import ipaddress
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class FitiLink:
    src_abbr: str
    dst_abbr: str
    src_id: int
    dst_id: int
    weight: int
    index: int


class FitiTopology:
    """Load configs/fiti.json and provide underlay routing helpers."""

    def __init__(self, path):
        self.filename = path
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
        self.nodes = {}
        for node in data.get('nodes', []):
            if not node.get('enabled', True):
                continue
            self.nodes[str(node['abbr'])] = {
                'id': int(node['id']),
                'name': str(node.get('name', node['abbr'])),
                'abbr': str(node['abbr']),
            }
        if not self.nodes:
            raise ValueError('FITI 拓扑没有启用节点: %s' % path)
        raw_links = []
        for link in data.get('links', []):
            src = str(link['src'])
            dst = str(link['dst'])
            raw_links.append((int(src), int(dst),
                              int(link.get('weight', 1))))
        raw_links.sort(key=lambda item: (min(item[0], item[1]),
                                         max(item[0], item[1])))
        id_to_abbr = {item['id']: abbr for abbr, item in self.nodes.items()}
        self.links: List[FitiLink] = []
        self._link_by_pair = {}
        for index, (src_id, dst_id, weight) in enumerate(raw_links, 1):
            if src_id not in id_to_abbr or dst_id not in id_to_abbr:
                continue
            link = FitiLink(src_abbr=id_to_abbr[src_id],
                            dst_abbr=id_to_abbr[dst_id],
                            src_id=src_id, dst_id=dst_id,
                            weight=weight, index=index)
            self.links.append(link)
            self._link_by_pair[frozenset((link.src_abbr, link.dst_abbr))] = link
        self.adjacency: Dict[str, List[Tuple[str, int]]] = {
            abbr: [] for abbr in self.nodes}
        for link in self.links:
            self.adjacency[link.src_abbr].append(
                (link.dst_abbr, link.weight))
            self.adjacency[link.dst_abbr].append(
                (link.src_abbr, link.weight))
        self._next_cache = {}
        self._path_cache = {}

    def node_id(self, abbr):
        return int(self.nodes[abbr]['id'])

    def link_network(self, left, right):
        pair = frozenset((left, right))
        link = self._link_by_pair.get(pair)
        if link is None:
            raise ValueError('FITI 拓扑中不存在链路: %s-%s' % (left, right))
        return ipaddress.IPv6Network('fd00:0:100:%d::/64' % link.index)

    def core_address(self, local, remote):
        net = self.link_network(local, remote)
        # Keep both ends deterministic and independent of call direction.
        offset = 1 if self.node_id(local) < self.node_id(remote) else 2
        return str(net.network_address + offset)

    def core_neighbor(self, local, remote):
        return self.core_address(remote, local)

    def path(self, source, target, allowed=None):
        """Return the full FITI node sequence from source to target."""
        key = (source, target, frozenset(allowed) if allowed else None)
        if key in self._path_cache:
            return list(self._path_cache[key])
        if source == target:
            raise ValueError('source 和 target 相同: %s' % source)
        if source not in self.adjacency or target not in self.adjacency:
            raise ValueError('FITI 拓扑缺少站点: %s/%s' % (source, target))
        distances = {source: 0}
        previous = {}
        queue = [(0, source)]
        while queue:
            distance, node = heapq.heappop(queue)
            if distance != distances.get(node):
                continue
            if node == target:
                break
            for neighbor, weight in self.adjacency[node]:
                if allowed is not None and neighbor not in allowed:
                    continue
                candidate = distance + weight
                if candidate < distances.get(neighbor, 1 << 60):
                    distances[neighbor] = candidate
                    previous[neighbor] = node
                    heapq.heappush(queue, (candidate, neighbor))
        if target not in distances:
            raise ValueError('FITI 拓扑中 %s 到 %s 不可达'
                             % (source, target))
        path = [target]
        current = target
        while current != source:
            current = previous.get(current)
            if current is None:
                raise ValueError('无法计算 %s 到 %s 的路径'
                                 % (source, target))
            path.append(current)
        path.reverse()
        self._path_cache[key] = tuple(path)
        return path

    def next_hop(self, source, target, allowed=None):
        """Return the next FITI node from source towards target."""
        key = (source, target, frozenset(allowed) if allowed else None)
        if key in self._next_cache:
            return self._next_cache[key]
        path = self.path(source, target, allowed)
        if len(path) < 2:
            raise ValueError('无法计算 %s 到 %s 的下一跳'
                             % (source, target))
        self._next_cache[key] = path[1]
        return path[1]

    def core_links(self):
        return list(self.links)


def load_fiti_topology(path):
    return FitiTopology(path)
