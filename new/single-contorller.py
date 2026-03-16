
#!/home/changeme/Desktop/ryu_env/bin/python3.8
# base py\Dijstra-no-probe-delay-dynamic-bandwidth-base-hop.py
# ryu-manager your_controller.py --controller controllerA
import threading
from threading import Lock
import os
import random
import time
import heapq
import re
import json
import socket
import argparse
import struct


from collections import defaultdict
from operator import itemgetter
from dataclasses import dataclass


from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import arp
from ryu.lib.packet import ethernet
from ryu.lib.packet import ipv4
from ryu.lib.packet import ether_types
from ryu.lib.packet import udp
from ryu.lib.packet import tcp
from ryu.lib import hub
from ryu.ofproto import inet
from ryu.topology.api import get_switch, get_link
from ryu.topology import event
from ryu.topology.switches import Switches, LLDPPacket
from ryu.lib import mac
from ryu.lib import addrconv
from ryu.lib.packet import lldp

MAX_PATHS = 1
VIRTUAL_GW_MAC = "00:00:00:00:00:01"
VIRTUAL_GW_IP_1 = "192.168.1.254"
VIRTUAL_GW_IP_2 = "192.168.2.254"
CONTROLLER_PORT = 6655  # 用于控制器间通信的端口
UPDATE_INTERVAL = 3  # 与主控制器通信间隔（秒）
BROADCAST_TTL = 1  # 广播TTL（秒）
IP_CONFIG_PATH = 'ip_config.json'  # 配置文件路径
MSG_TYPE_UPDATE = 1  # 定时更新消息类型
MSG_TYPE_PATH_REQUEST = 2  # 域间路径请求消息类型
MSG_TYPE_PATH_RESPONSE = 3  # 域间路径响应消息类型
MSG_TYPE_INTER_DOMAIN_LINK = 4  # 域间链路消息类型


@dataclass
class Paths:
    ''' Paths container '''
    path: list
    cost: float

class Controller13(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(Controller13, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.neigh = defaultdict(dict)
        self.inter_domain_link = {}
        self.hosts = {}
        self.switches = []
        self.arp_table = {}
        self.path_table = {}
        self.paths_table = {}
        self.path_with_ports_table = {}
        self.datapath_list = {}
        self.path_calculation_keeper = []
        self.port_name_map = defaultdict(dict)
        self.dpid_to_switch_name = {}
        self.virtual_gw_ip = [
    "192.168.103.2",
    "192.168.2.2",
    "192.168.3.2",
    "192.168.4.2",
]


        self.broadcast_packet = {}  # 记录广播数据包信息
        self.pending_packets = {}  # 存储等待ARP回复的数据包

        self.pending_path_requests = {}

        self.pending_path_requests_lock = Lock()
        self.pending_packets_lock = Lock()

    def _ip_in_subnet(self, ip, subnet):
        """检查IP是否属于指定子网"""
        subnet_ip, mask = subnet.split('/')
        mask = int(mask)
        subnet_int = self._ip_to_int(subnet_ip)
        ip_int = self._ip_to_int(ip)
        return (ip_int & (0xFFFFFFFF << (32 - mask))) == subnet_int

    def _ip_to_int(self, ip):
        """将IP地址转换为整数"""
        octets = ip.split('.')
        return sum(int(octet) << (24 - 8 * i) for i, octet in enumerate(octets))

    def _record_broadcast(self, src, dst, ethertype):
        key = (src, dst, ethertype)
        if key not in self.broadcast_packet:
            self.broadcast_packet[key] = time.time()

    def _check_broadcast_suppression(self, src, dst, ethertype):
        """检查是否需要抑制广播数据包,返回True表示需要抑制，同时下发流表"""
        key = (src, dst)
        if key in self.broadcast_packet:
            elapsed = time.time() - self.broadcast_packet[key]
            if BROADCAST_TTL < elapsed:
                self.logger.info(f"Suppressing broadcast from {src} to {dst}")
                
                # 下发流表，匹配 ARP 数据包
                for dpid, datapath in self.datapath_list.items():
                    parser = datapath.ofproto_parser
                    ofproto = datapath.ofproto

                    # 创建匹配规则
                    match = parser.OFPMatch(
                        eth_type=ethertype,
                        arp_spa=src,  # 源 IP
                        arp_tpa=dst   # 目的 IP
                    )

                    # 空的 actions 表示丢弃数据包
                    actions = []

                    # 添加流表，持续时间为 BROADCAST_TTL/2
                    self.add_flow(datapath, priority=10, match=match, actions=actions, idle_timeout=int(BROADCAST_TTL / 2))
                    self.logger.info(f"Flow added to suppress broadcast: src={src}, dst={dst}, duration={BROADCAST_TTL / 2}s")

                return True
        return False

    def find_path_cost(self, path):
        path_cost = []
        single_hop_costs = []
        for i in range(len(path) - 1):
            src_dpid = path[i]
            dst_dpid = path[i + 1]
            path_cost.append(1)
            single_hop_costs.append((src_dpid, dst_dpid, 1))
        total_cost = sum(path_cost) if path_cost else 0
        return total_cost, single_hop_costs

    def find_paths_and_costs(self, src, dst):
        if src == dst:
            return [Paths(path=[src], cost=0)]

        queue = [(0, src, [src])]
        heapq.heapify(queue)
        visited = set()
        best_paths = {}

        while queue:
            (cost, current, path) = heapq.heappop(queue)
            if current in visited:
                continue
            visited.add(current)
            best_paths[current] = (cost, path)

            if current == dst:
                total_cost, single_hop_costs = self.find_path_cost(path)
                path_obj = Paths(path=path, cost=total_cost)
                path_obj.single_hop_costs = single_hop_costs
                return [path_obj]

            for next_node in self.neigh[current]:
                if next_node not in visited:
                    new_cost = cost + 1
                    new_path = path + [next_node]
                    heapq.heappush(queue, (new_cost, next_node, new_path))

        return []

    def find_n_optimal_paths(self, paths, number_of_optimal_paths=MAX_PATHS):
        return paths[:min(number_of_optimal_paths, len(paths))]

    def add_ports_to_paths(self, paths, first_port, last_port):
        paths_n_ports = list()
        if not paths:
            return paths_n_ports
        bar = dict()
        in_port = first_port
        for s1, s2 in zip(paths[0].path[:-1], paths[0].path[1:]):
            out_port = self.neigh[s1][s2]
            bar[s1] = (in_port, out_port)
            in_port = self.neigh[s2][s1]
        bar[paths[0].path[-1]] = (in_port, last_port)
        paths_n_ports.append(bar)
        return paths_n_ports

    def install_paths(self, src, first_port, dst, last_port, ip_src, ip_dst, type, pkt, dst_mac=None):
        if (src, first_port, dst, last_port) not in self.path_calculation_keeper:
            self.path_calculation_keeper.append((src, first_port, dst, last_port))
            self.topology_discover(src, first_port, dst, last_port)
            self.topology_discover(dst, last_port, src, first_port)

        if (src, first_port, dst, last_port) not in self.path_table or not self.path_table[(src, first_port, dst, last_port)]:
            self.logger.warning(f"No path found between {src} and {dst}")
            return None

        for node in self.path_table[(src, first_port, dst, last_port)][0].path:
            dp = self.datapath_list[node]
            ofp = dp.ofproto
            ofp_parser = dp.ofproto_parser

            actions = []
            in_port = self.path_with_ports_table[(src, first_port, dst, last_port)][0][node][0]
            out_port = self.path_with_ports_table[(src, first_port, dst, last_port)][0][node][1]
            actions = [ofp_parser.OFPActionOutput(out_port)]

            if type == 'IP':
                # nw = pkt.get_protocol(ipv4.ipv4)
                if dst_mac:
                    match = ofp_parser.OFPMatch(in_port=in_port, eth_type=ether_types.ETH_TYPE_IP,
                                                ipv4_src=ip_src, ipv4_dst=ip_dst, eth_dst=dst_mac)
                else:
                    match = ofp_parser.OFPMatch(in_port=in_port, eth_type=ether_types.ETH_TYPE_IP,
                                                ipv4_src=ip_src, ipv4_dst=ip_dst)
                # if dst_mac and dst_mac != VIRTUAL_GW_MAC:  # 仅在目的子网修改MAC
                #     actions.insert(0, ofp_parser.OFPActionSetField(eth_dst=dst_mac))
                self.add_flow(dp, 100, match, actions, 0)
            elif type == 'ARP':
                match_arp = ofp_parser.OFPMatch(in_port=in_port, eth_type=ether_types.ETH_TYPE_ARP, arp_spa=ip_src, arp_tpa=ip_dst)
                self.add_flow(dp, 100, match_arp, actions, 0)

        return self.path_with_ports_table[(src, first_port, dst, last_port)][0][src][1]

    def add_flow(self, datapath, priority, match, actions, idle_timeout, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match, idle_timeout=idle_timeout,
                                    instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, idle_timeout=idle_timeout, instructions=inst)
        datapath.send_msg(mod)

    def format_path(self, path_obj, ports_dict):
        path = path_obj.path
        total_cost = path_obj.cost
        path_str = []
        for i, dpid in enumerate(path):
            switch_name = self.dpid_to_switch_name.get(dpid, f"dpid_{dpid}")
            if dpid in ports_dict:
                in_port, out_port = ports_dict[dpid]
                in_port_name = self.port_name_map[dpid].get(in_port, f"port_{in_port}")
                out_port_name = self.port_name_map[dpid].get(out_port, f"port_{out_port}")
                if i == 0:
                    path_str.append(f"{switch_name}({out_port_name})")
                elif i == len(path) - 1:
                    path_str.append(f"{switch_name}({in_port_name})")
                else:
                    path_str.append(f"{switch_name}({in_port_name}->{out_port_name})")
            else:
                path_str.append(switch_name)
        return " -> ".join(path_str), total_cost

    def topology_discover(self, src, first_port, dst, last_port):
        paths = self.find_paths_and_costs(src, dst)
        if not paths:
            self.logger.warning(f"No path found from {src} to {dst}")
            return
        optimal_paths = self.find_n_optimal_paths(paths)
        path_with_port = self.add_ports_to_paths(optimal_paths, first_port, last_port)

        # 修改日志：基于跳数的最短路径
        self.logger.info("Shortest Path (based on hop count):")
        for i, opt_path in enumerate(optimal_paths):
            path_str, total_cost = self.format_path(opt_path, path_with_port[0] if path_with_port else {})
            # 修改为跳数描述，去掉时延单位 (ms)
            self.logger.info(f"  Path {i+1}: {path_str}, Total Hops: {int(total_cost)}")
        
        self.paths_table[(src, first_port, dst, last_port)] = paths
        self.path_table[(src, first_port, dst, last_port)] = optimal_paths
        self.path_with_ports_table[(src, first_port, dst, last_port)] = path_with_port

    def find_next_hop(self, dpid): # dpid是下一个管理域的边缘交换机
        for neighbor in self.neigh:
            for next_dpid in self.neigh[neighbor]:
                if next_dpid == dpid:
                    self.logger.info(f"Next hop for {dpid} is {neighbor} via port {self.neigh[neighbor][next_dpid]}")
                    return neighbor, self.neigh[neighbor][next_dpid]
        return None, None

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)

        # 处理 LLDP 数据包以测量链路时延
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src
        dpid = datapath.id

        if src not in self.hosts:
            self.hosts[src] = (dpid, in_port)

        out_port = ofproto.OFPP_FLOOD

        if eth.ethertype == ether_types.ETH_TYPE_IP:
            nw = pkt.get_protocol(ipv4.ipv4)
            src_ip = nw.src
            dst_ip = nw.dst
            if self._check_broadcast_suppression(src_ip, dst_ip, eth.ethertype):
                return
            self.arp_table[src_ip] = src
            h1 = self.hosts[src]

            # 跨网段
            if dst == VIRTUAL_GW_MAC:
                if dst_ip in self.arp_table:
                    self.logger.info(f"receive cross domain IP packet from {src_ip} to {dst_ip} in local subnet")
                    h2 = self.hosts[self.arp_table[dst_ip]]
                    # 在这里修改目的mac地址为self.arp_table[dst_ip]
                    self.logger.info(f"eth dst: from {eth.dst} to {self.arp_table[dst_ip]}")
                    eth.dst = self.arp_table[dst_ip]
                    pkt.serialize()
                    out_port = self.install_paths(h1[0], h1[1], h2[0], h2[1], src_ip, dst_ip, 'IP', pkt, self.arp_table[dst_ip])
                    actions = [parser.OFPActionOutput(out_port)]
                    out = parser.OFPPacketOut(datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER,
                                in_port=in_port, actions=actions, data=pkt.data)
                    datapath.send_msg(out)
                    return
                else:
                    self.logger.info(f"receive cross domain IP packet from {src_ip} to {dst_ip} in local subnet, but no ARP reply")
                    with self.pending_packets_lock:
                        if (src_ip, dst_ip) not in self.pending_packets:
                            self.pending_packets[(src_ip, dst_ip)] = []
                            self._send_arp_request(datapath, in_port, dst_ip, src, ether_types.ETH_TYPE_ARP)
                            hub.spawn(self._packet_ttl_monitor, src_ip, dst_ip)
                        self.pending_packets[(src_ip, dst_ip)].append({
                            "pkt": pkt,
                            "dpid": dpid,
                            "in_port": in_port,
                            "timestamp": time.time()
                        })
                    return
            # 本网段路由
            else:
                h2 = self.hosts[dst]
                dst_mac = self.arp_table.get(dst_ip, None)
                out_port = self.install_paths(h1[0], h1[1], h2[0], h2[1], src_ip, dst_ip, 'IP', pkt, dst_mac)
                self.install_paths(h2[0], h2[1], h1[0], h1[1], dst_ip, src_ip, 'IP', pkt, dst_mac)
        elif eth.ethertype == ether_types.ETH_TYPE_ARP:
            src_ip = arp_pkt.src_ip
            dst_ip = arp_pkt.dst_ip
            if self._check_broadcast_suppression(src_ip, dst_ip, eth.ethertype):
                return
            if arp_pkt.opcode == arp.ARP_REPLY:
                self.arp_table[src_ip] = src
                if dst_ip in self.virtual_gw_ip: # 跨域路由目标在本域内，泛洪的ARP得到回复
                    self._process_pending_packet(src_ip, msg)
                    return
                h1 = self.hosts[src]
                h2 = self.hosts[dst]
                out_port = self.install_paths(h1[0], h1[1], h2[0], h2[1], src_ip, dst_ip, 'ARP', pkt)
                self.install_paths(h2[0], h2[1], h1[0], h1[1], dst_ip, src_ip, 'ARP', pkt)
            elif arp_pkt.opcode == arp.ARP_REQUEST and src_ip in self.virtual_gw_ip and dst_ip in self.arp_table:
                return # 跨网段路由，发送的ARP请求，目的IP已在ARP表中，直接返回（丢弃）
            elif arp_pkt.opcode == arp.ARP_REQUEST and dst_ip not in self.virtual_gw_ip:
                self.arp_table[src_ip] = src # 普通ARP请求
            elif arp_pkt.opcode == arp.ARP_REQUEST and dst_ip in self.virtual_gw_ip: # dst_ip是主机设置的虚拟网关
                # self._send_arp_reply(datapath, in_port, src, dst_ip, src_ip) # 发起跨网段路由前，请求虚拟网关mac地址
                self._send_arp_reply(datapath, in_port, src, src_ip, VIRTUAL_GW_MAC, dst_ip)
                return
            elif arp_pkt.opcode == arp.ARP_REQUEST and dst_ip in self.arp_table: # 普通ARP请求
                self.arp_table[src_ip] = src
                self._send_arp_reply(datapath, in_port, src, src_ip, dst, dst_ip)
                return
                # dst_mac = self.arp_table[dst_ip]
                # h1 = self.hosts[src]
                # h2 = self.hosts[dst_mac]
                # out_port = self.install_paths(h1[0], h1[1], h2[0], h2[1], src_ip, dst_ip, 'ARP', pkt)
                # self.install_paths(h2[0], h2[1], h1[0], h1[1], dst_ip, src_ip, 'ARP', pkt)
            if out_port == ofproto.OFPP_FLOOD:
                self._record_broadcast(src_ip, dst_ip, eth.ethertype)

        if self._check_broadcast_suppression(src_ip, dst_ip, eth.ethertype):
            return
        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None)
        datapath.send_msg(out)

    def _path_request_timeout_monitor(self, src_ip, dst_ip):
        """监控路径请求超时"""
        key = (src_ip, dst_ip)
        while key in self.pending_path_requests:
            pkt_list = self.pending_path_requests[key]
            pkt_info = pkt_list[0] if pkt_list else None
            if pkt_info:
                elapsed = time.time() - pkt_info["timestamp"]
                if elapsed >= BROADCAST_TTL:  # 5秒超时
                    del self.pending_path_requests[key]
                    self.logger.warning(f"Path request for {src_ip} to {dst_ip} timed out")
            else:
                del self.pending_path_requests[key]
                break
            hub.sleep(1)

    def _send_arp_reply(self, datapath, in_port, dst_mac, dst_ip, src_mac, src_ip):
        """"发送ARP回复"""
        dpid = datapath.id
        self.logger.info(f"Sending ARP reply to {dst_mac} for {dst_ip}, src: {src_mac}, src_ip: {src_ip}")
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_ARP,
            dst=dst_mac,
            src=src_mac  # 必须与虚拟网关 MAC 一致
        ))
        pkt.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=src_mac,
            src_ip=src_ip,  # 必须与网关 IP 一致
            dst_mac=dst_mac,
            dst_ip=dst_ip
        ))
        # 序列化数据包时必须处理异常
        pkt.serialize()  # 若此处失败，data 为空
        actions = [parser.OFPActionOutput(in_port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,  # 必须携带完整数据
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=pkt.data  # 或 pkt.serialize()
        )
        datapath.send_msg(out)

    def _send_arp_request(self, datapath, in_port, dst_ip, src, ethertype):
        """跨网段路由，没有目标IP对应的ARP项,发送ARP请求"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                           dst="ff:ff:ff:ff:ff:ff", src=src))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REQUEST, src_mac=src,
                                 src_ip=self.virtual_gw_ip[0], dst_mac="00:00:00:00:00:00", dst_ip=dst_ip))
        pkt.serialize()
        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER,
                                  in_port=in_port, actions=actions, data=pkt.data)
        datapath.send_msg(out)
        self.logger.info(f"Sending ARP request for {dst_ip}")

    def _packet_ttl_monitor(self, src_ip, dst_ip):
        key = (src_ip, dst_ip)
        while key in self.pending_packets:
            pkt_list = self.pending_packets[key]
            pkt_info = pkt_list[0] if pkt_list else None
            if pkt_info:
                elapsed = time.time() - pkt_info["timestamp"]
                if elapsed >= BROADCAST_TTL:
                    del self.pending_packets[key]
                    self.logger.warning(f"Packet TTL expired for {src_ip} to {dst_ip}")
            else:
                del self.pending_packets[(src_ip, dst_ip)]
                break
            hub.sleep(1)

    def _process_pending_packet(self, dst_ip, msg):
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        for (src_ip, dst), pending_list in self.pending_packets.items():
            pending_list = None
            if dst == dst_ip:
                pending_list = self.pending_packets.pop((src_ip, dst_ip), None)
                if not pending_list:
                    self.logger.info(f"No pending packets for {src_ip} to {dst_ip} in local subnet, something wrong")
                    del self.pending_packets[(src_ip, dst_ip)]
                    return
                datapath = self.datapath_list[pending_list[0]["dpid"]]
                in_port = pending_list[0]["in_port"]
                pkt = pending_list[0]["pkt"]

                h1 = self.hosts[pkt.get_protocol(ethernet.ethernet).src]
                h2 = self.hosts[self.arp_table[dst_ip]]
                out_port = self.install_paths(h1[0], h1[1], h2[0], h2[1], src_ip, dst_ip, 'IP', pkt, self.arp_table[dst_ip])
                for pending in pending_list:
                    pkt = pending["pkt"]
                    eth = pkt.get_protocols(ethernet.ethernet)[0]
                    self.logger.info(f"eth dst: from {eth.dst} to {self.arp_table[dst_ip]}")
                    eth.dst = self.arp_table[dst_ip]  # 修改目的MAC地址为ARP表中的值
                    pkt.serialize()
                    actions = [parser.OFPActionOutput(out_port)]
                    out = parser.OFPPacketOut(datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER,
                                            in_port=in_port, actions=actions, data=pkt.data)
                    datapath.send_msg(out)
                del self.pending_packets[(src_ip, dst_ip)]

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IPV6)
        actions = []
        self.add_flow(datapath, 20, match, actions, 0)

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions, 0)

        # 丢弃目标IP为114.114.114.114的所有IPv4包
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_dst="114.114.114.114")
        actions = []  # 空actions表示丢弃
        self.add_flow(datapath, 100, match, actions, 0)

        # 丢弃目标IP为47.98.232.26的所有IPv4包
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_dst="47.98.232.26")
        actions = []  # 空actions表示丢弃
        self.add_flow(datapath, 100, match, actions, 0)

        req = parser.OFPPortDescStatsRequest(datapath, 0)
        datapath.send_msg(req)
        # 启动周期性请求线程
        hub.spawn(self._port_stats_monitor, datapath)

    def _port_stats_monitor(self, datapath):
        """周期性发送端口统计请求"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        while True:
            req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
            datapath.send_msg(req)
            self.logger.debug(f"Sent periodic OFPPortStatsRequest to dpid {datapath.id}")
            hub.sleep(5)  # 每5秒请求一次        

    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        switch_dp = ev.switch.dp
        switch_dpid = switch_dp.id
        self.logger.info(f"Switch has been plugged in PID: {switch_dpid}")
        self._log_switch_status()
        if switch_dpid not in self.switches:
            self.datapath_list[switch_dpid] = switch_dp
            self.switches.append(switch_dpid)

    @set_ev_cls(event.EventSwitchLeave, MAIN_DISPATCHER)
    def switch_leave_handler(self, ev):
        switch = ev.switch.dp.id
        if switch in self.switches:
            try:
                self.switches.remove(switch)
                del self.datapath_list[switch]
                del self.neigh[switch]
            except KeyError:
                self.logger.info(f"Switch has been already plugged off PID {switch}!")
            finally:
                self._log_switch_status()

    @set_ev_cls(event.EventLinkAdd, MAIN_DISPATCHER)
    def link_add_handler(self, ev):
        self.neigh[ev.link.src.dpid][ev.link.dst.dpid] = ev.link.src.port_no
        self.neigh[ev.link.dst.dpid][ev.link.src.dpid] = ev.link.dst.port_no
        self.logger.info(f"Link between switches has been established, SW1 DPID: {ev.link.src.dpid}:{ev.link.src.port_no} SW2 DPID: {ev.link.dst.dpid}:{ev.link.dst.port_no}")
        self._log_neigh_status()

    @set_ev_cls(event.EventLinkDelete, MAIN_DISPATCHER)
    def link_delete_handler(self, ev):
        return
        try:
            del self.neigh[ev.link.src.dpid][ev.link.dst.dpid]
            del self.neigh[ev.link.dst.dpid][ev.link.src.dpid]
            self._log_neigh_status()
            self._log_switch_status()
        except KeyError:
            self.logger.info("Link has been already plugged off!")
            pass

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_stats_reply_handler(self, ev):
        self.logger.info("Port description stats")
        dpid = ev.msg.datapath.id
        switch_name = None
        for port in ev.msg.body:
            port_name = port.name.decode().strip()
            port_no = port.port_no
            self.port_name_map[dpid][port_no] = port_name
            self.logger.info(f"Port: {port_name}")
            if re.match(r'^s\d+$', port_name):
                switch_name = port_name
                self.dpid_to_switch_name[dpid] = switch_name
                if switch_name is None:
                    self.logger.warning(f"Switch name not found for dpid {dpid}")
                    self.dpid_to_switch_name[dpid] = f"switch_{dpid}"
        if dpid not in self.dpid_to_switch_name:
            self.dpid_to_switch_name[dpid] = f"switch_{dpid}"
        self.logger.info(f"Switch name for dpid={dpid}: {self.dpid_to_switch_name[dpid]}")

    def _log_neigh_status(self, dpid=None):
        self.logger.info("Current neigh status:")
        if not self.neigh:
           self.logger.info("  <empty neigh>")
           return
        for src_dpid in self.neigh:
            if dpid is None or src_dpid == dpid:
                src_name = self.dpid_to_switch_name.get(src_dpid, f"dpid_{src_dpid}")
                for dst_dpid, port in self.neigh[src_dpid].items():
                    dst_name = self.dpid_to_switch_name.get(dst_dpid, f"dpid_{dst_dpid}")
                    port_name = self.port_name_map[src_dpid].get(port, f"port_{port}" if port is not None else "<unknown>")
                    self.logger.info(f"  {src_name} -> {dst_name} via {port_name}")
        self._log_switch_status()

    def _log_switch_status(self):
        self.logger.info("Current switch:")
        if not self.switches:
            self.logger.info("  <empty switches>")
            return
        for src_dpid in self.switches:
            self.logger.info(f"  {self.dpid_to_switch_name.get(src_dpid, f'dpid_{src_dpid}')}")
