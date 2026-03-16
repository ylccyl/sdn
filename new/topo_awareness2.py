import time

import netifaces   #  这是一个用于获取网络接口信息的库，能够提供有关网络接口的详细信息，如 IP 地址、MAC 地址等
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3, ether
from ryu.lib.packet import ethernet, ether_types, arp, packet
from ryu.lib import hub
from ryu.topology import event
from ryu.topology.switches import LLDPPacket
import networkx as nx
from operator import attrgetter

Initial_bandwidth = 10
# SWITCHES_IP = ['10.0.0.1', '10.0.0.2', '10.0.0.3', '10.0.0.4', '10.0.0.5', '10.0.0.6']
SWITCHES_IP = ['172.17.0.3','172.17.0.4','172.17.0.5','172.17.0.6','172.17.0.7','172.17.0.8','172.17.0.10','172.17.0.11','172.17.0.12','172.17.0.13','172.17.0.14','172.17.0.15']
CONTROLLER_IP = ['172.17.0.1','172.17.0.2','172.17.0.9']
LOCAL_IP = '172.17.0.9'
FAKE_MAC = 'ab:cd:ef:gh:io:mg'


class Host(object):
    # This is data class passed by EventHostXXX
    def __init__(self, mac, port, ipv4):
        super(Host, self).__init__()
        self.port = port
        self.mac = mac
        self.ipv4 = ipv4

    def to_dict(self):
        d = {'mac': self.mac,
             'ipv4': self.ipv4,
             'port': self.port.to_dict()}
        return d

    # def update_ip(self, ip):
    #     self.ipv4 = ip

    def __eq__(self, host):
        return self.mac == host.mac and self.port == host.port

    def __str__(self):
        msg = 'Host<mac=%s, port=%s,' % (self.mac, str(self.port))
        msg += ','.join(self.ipv4)
        msg += '>'
        return msg


class TopoAwareness(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(TopoAwareness, self).__init__(*args, **kwargs)
        self.name = 'topo_awareness'
        self.topology_api_app = self
        self.local_mac = ''
        # Link、switch and host
        self.dpid_to_switch_ip = {}
        self.dpid_to_switch = {}  # Store switch in topology using OpenFlow
        self.switch_mac_to_port = {}  # {dpid:{port1:hw_addr1,port2:hwaddr2,...},...}
        self.host_to_sw_port = {}  # {dpid1:{port1:[mac, ipv4],port2:[mac,ipv4]...},...}
        self.topo_inter_link = {}  # {(src.dpid, dst.dpid): (src.port_no, timestamp, delay, bw, loss)}
        self.topo_access_link = {}
        # self.detection_access_link = {}  # 带有时间戳的外部链路信息，用于超时检测，超时检测后被赋值给真正的外部链路
        # self.detection_inter_link = {}

        # calculate delay
        self.echo_timestamp = {} # {dpid:recvtime,1:0.5,2:0.3,....}
        self.echo_latency = {}  # {dpid:delaytime,1:0.5,2:0.3,....}
        self.lldp_delay = {}  # {(src,dst):time,(1,2):0.5,...}
        self.link_delay = {}  # {(src,dst):time,(1,2):0.5,....}

        # calculate bw
        self.port_stats = {}
        self.free_bandwidth = {}  # {dpid: {port_no: (free_bandwidth, usage), ...}, ...}} (Mbit/s)

        ###########
        self.mac_to_port = {}
        self.arp_table = {}

        # 保存网络节点信息
        self.graph = nx.DiGraph()
        #  开启新的线程
        self.update_thread = hub.spawn(self.link_timeout_detection, self.topo_access_link)
        self.measure_thread = hub.spawn(self._detector)
        # self.monitor_thread = hub.spawn(self._monitor_thread)
        self.show_info = hub.spawn(self.show)
        self.check_switch_thread = hub.spawn(self._check_switch_state, self.echo_timestamp)
        self.get_mac_thread = hub.spawn(self.get_local_mac_address)

    def get_local_mac_address(self):
        # 获取本地网络接口信息
        interfaces = netifaces.interfaces()

        # 遍历接口并获取 MAC 地址
        for interface in interfaces:
            if interface == 'lo':
                continue  # 跳过回环接口
            try:
                self.local_mac = netifaces.ifaddresses(interface)[netifaces.AF_LINK][0]['addr']
                break
            except KeyError:
                pass

    """
        收集网络带宽信息
    """

    def _monitor_thread(self):
        while True:
            self._request_stats()
            self.add_bandwidth_info(self.free_bandwidth)
            hub.sleep(8)

    # Stat request:
    def _request_stats(self):
        datapaths = list(self.dpid_to_switch.values())
        for datapath in datapaths:
            self.logger.debug('send stats request: %016x', datapath.id)
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
            datapath.send_msg(req)
            hub.sleep(0.1)

    def _save_stats(self, _dict, key, value, history_length=2):
        if key not in _dict:
            _dict[key] = []

        _dict[key].append(value)

        if len(_dict[key]) > history_length:
            _dict[key].pop(0)

    def _cal_speed(self, now, pre, period):
        if period:
            return (now - pre) / (period)
        else:
            return 0

    def _get_period(self, curr_time, pre_time):
        period = curr_time - pre_time
        return period

    # Bandwidth graph:
    def _save_freebandwidth(self, dpid, port_no, speed):
        capacity = Initial_bandwidth  # Kbp/s to Mbit/s
        speed = float(speed * 8) / (10 ** 6)  # byte/s to Mbit/s
        curr_bw = max(capacity - speed, 0)
        self.free_bandwidth[dpid].setdefault(port_no, None)
        self.free_bandwidth[dpid][port_no] = (curr_bw, speed)  # Save as Mbit/s

    def add_bandwidth_info(self, free_bandwidth):
        """
            Save bandwidth data into networkx graph object.
        """
        link_to_port = self.topo_inter_link
        for link in link_to_port.keys():
            (src_dpid, dst_dpid) = link
            (src_port, _, _, _, _) = link_to_port[link]
            try:
                src_free_bandwidth, _ = free_bandwidth[src_dpid][src_port]
                self.topo_inter_link[(src_dpid, dst_dpid)][3] = src_free_bandwidth
                self.graph[src_dpid][dst_dpid]['free_bandwith'] = src_free_bandwidth
            except:
                pass

        link_to_port = self.topo_access_link
        for link in link_to_port.keys():
            (src_dpid, dst_dpid) = link
            (src_port, _, _, _, _) = link_to_port[link]

            try:
                src_free_bandwidth, _ = free_bandwidth[src_dpid][src_port]
                self.topo_access_link[(src_dpid, dst_dpid)][3] = src_free_bandwidth
                self.graph[src_dpid][dst_dpid]['free_bandwith'] = src_free_bandwidth
            except:
                pass

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        """
            Save port's stats info
            Calculate port's speed and save it.
            port_stats: {(dpid port_no): [(tx_packets, rx_packets ,tx_bytes, rx_bytes, rx_errors, duration_sec, duration_nsec),...]}
            [history][stat_type]
            value is a tuple (tx_packets, rx_packets ,tx_bytes, rx_bytes, now_timestamp)
                                  0          1           2         3          4
        """
        body = ev.msg.body
        dpid = ev.msg.datapath.id

        self.free_bandwidth.setdefault(dpid, {})
        now_timestamp = time.time()
        # !FIXME: add rx_packets
        for stat in sorted(body, key=attrgetter('port_no')):
            port_no = stat.port_no
            if port_no != ofproto_v1_3.OFPP_LOCAL:

                key = (dpid, port_no)
                value = (stat.tx_packets, stat.rx_packets, stat.tx_bytes, stat.rx_bytes, now_timestamp)

                # Monitoring current port.
                self._save_stats(self.port_stats, key, value, 5)

                port_stats = self.port_stats[key]

                # if len(port_stats) == 1:
                #     self._save_freebandwidth(dpid, port_no, 0)

                if len(port_stats) > 1:
                    curr_stat = port_stats[-1][2]
                    prev_stat = port_stats[-2][2]

                    period = self._get_period(port_stats[-1][4], port_stats[-2][4])

                    speed = self._cal_speed(curr_stat, prev_stat, period)

                    # Using maping to save detal_port_stats.
                    # self._save_stats(self.delta_port_stats, key,
                    #                  tuple(m(operator.sub, port_stats[-1], port_stats[-2])), 5)
                    # save free bandwidth (link capacity, can be used for load balancing, calculate link utilization) - Not work in mininet (reason: no link bandwidth)
                    self._save_freebandwidth(dpid, port_no, speed)

    """
        收集网络时延信息
    """

    def _detector(self):
        """
            Delay detecting functon.
            Send echo request and calculate link delay periodically
        """
        while True:
            self._send_echo_request()
            self.add_delay_info()
            hub.sleep(8)

    def _send_echo_request(self):
        """
            Seng echo request msg to datapath.
        """
        datapaths = list(self.dpid_to_switch.values())
        for datapath in datapaths:
            parser = datapath.ofproto_parser

            data_time = "%.12f" % time.time()
            byte_arr = bytearray(data_time.encode())

            echo_req = parser.OFPEchoRequest(datapath, data=byte_arr)
            datapath.send_msg(echo_req)

            # Important! Don't send echo request together, Because it will
            # generate a lot of echo reply almost in the same time.
            # which will generate a lot of delay of waiting in queue
            # when processing echo reply in echo_reply_handler.
            hub.sleep(0.1)

    def add_delay_info(self):
        """
            Create link delay data, and save it into graph object.
        """
        link_to_port = self.topo_inter_link
        for link in link_to_port.keys():
            (src_dpid, dst_dpid) = link
            try:
                delay = self._get_delay(src_dpid, dst_dpid)
                self.topo_inter_link[(src_dpid, dst_dpid)][2] = delay
                self.graph[src_dpid][dst_dpid]['delay'] = delay
            except:
                pass

        link_to_port = self.topo_access_link
        for link in link_to_port.keys():
            (src_dpid, dst_dpid) = link
            try:
                delay = self._get_access_delay(src_dpid, dst_dpid)
                self.topo_access_link[(src_dpid, dst_dpid)][2] = delay
                self.graph[src_dpid][dst_dpid]['delay'] = delay
            except:
                pass

    def _get_delay(self, src, dst):
        """
            Get link delay.
                        Controller
                        |        |
        src echo latency|        |dst echo latency
                        |        |
                   SwitchA-------SwitchB

                    fwd_delay--->
                        <----reply_delay
            delay = (forward delay + reply delay - src datapath's echo latency
        """
        try:
            fwd_delay = self.lldp_delay[(src, dst)][0]
            # re_delay = self.lldp_delay[(dst, src)]
            src_latency = self.echo_latency[src]
            dst_latency = self.lldp_delay[(src, dst)][1]

            delay = fwd_delay - (src_latency + dst_latency) / 2
            return max(delay, 0)
        except:
            # fwd_delay = self.lldp_delay[(dst, src)]
            # src_latency = self.echo_latency[src]
            #
            # delay = (fwd_delay + fwd_delay - src_latency - src_latency) / 2
            return float(0)

    def _get_access_delay(self, src, dst):
        """
            Get link delay.
                   ControllerA                        ControllerB
                        |                                 |
        src echo latency|                                 |dst echo latency
                        |                                 |
                   SwitchA------------------------------SwitchB
                                <----forward delay
            delay = (forward delay + forward delay - src echo latency - src echo latency )
        """
        try:
            fwd_delay = self.lldp_delay[(src, dst)][0]
            src_latency = self.echo_latency[src]
            dst_latency = self.lldp_delay[(src, dst)][1]
            delay = fwd_delay - (src_latency + dst_latency) / 2
            return max(delay, 0)
        except:
            return float('inf')

    def _save_lldp_delay(self, src=0, dst=0, lldpdelay=0, echodelay=0):
        self.lldp_delay[(src, dst)] = [lldpdelay, echodelay]

    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def _echo_reply_handler(self, ev):
        """
            Handle the echo reply msg, and get the latency of link.
        """
        now_timestamp = time.time()
        try:
            latency = now_timestamp - eval(ev.msg.data)
            self.echo_latency[ev.msg.datapath.id] = latency
            self.echo_timestamp[ev.msg.datapath.id] = now_timestamp
        except:
            print("echo reply error")
            return

    """
        获取交换机相关信息，包括ID编号、端口号、mac地址
    """

    def show(self):
        while True:
            # switches, master_switches = self.handle_switches_for_submit()
            # link, host = self.handle_topo_info_for_submit()
            # print("***********************")
            # print('link:', link)
            # print('host:', host)
            # print('switch_mac_to_port:', self.switch_mac_to_port)
            # print('dpid_to_switch:', self.dpid_to_switch)
            print("***********************")
            print("交换机列表", self.dpid_to_switch.keys())
            # print("交换机端口地址对应列表",self.switch_mac_to_port)
            # print("内部链路", self.topo_inter_link)
            # print("主机链路", self.host_to_sw_port)
            self.arp_table.clear()
            # print("外部链路",self.topo_access_link)
            # print("图中的链路信息",self.graph.edges(data=True))
            print("-------------------------")
            print("\n")
            print("\n")
            hub.sleep(5)

    def switches_role_detection(self):
        for i in self.dpid_to_switch.keys():
            datapath = self.dpid_to_switch[i]
            self.send_role_request(datapath, datapath.ofproto.OFPCR_ROLE_NOCHANGE, 0)

    def get_path(self, src, dst):
        try:
            path = nx.shortest_path(self.graph, src, dst)
            return path
        except:
            return []

    def get_port(self, dpid, port_no):
        if port_no in self.switch_mac_to_port[dpid].keys():
            return True
        return False

    def is_link_port(self, dpid, port):
        for link in self.topo_inter_link.keys():
            if dpid == link[0] and port == self.topo_inter_link[link][0]:
                return True
        for link in self.topo_access_link.keys():
            if dpid == link[0] and port == self.topo_access_link[link][0]:
                return True
        return False

    def add_flow(self, datapath, priority, match, actions, proto=0, hard_timeout=0, idle_timeout=0, buffer_id=None):
        """
        向交换机下发流表
        Deliver the flow table to the switch
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        # if proto == 6:
        #     hard_timeout = 5

        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, idle_timeout=idle_timeout,
                                    hard_timeout=hard_timeout, match=match,
                                    instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    idle_timeout=idle_timeout,
                                    hard_timeout=hard_timeout, match=match,
                                    instructions=inst)
        datapath.send_msg(mod)

    def send_packet_to_outport(self, datapath, msg, in_port, actions):
        """
        进行广播设置
        Setting up a broadcast
        """
        data = None
        if msg.buffer_id == datapath.ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = datapath.ofproto_parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id, in_port=in_port,
                                                   actions=actions, data=data)
        datapath.send_msg(out)

    def get_switch_id_by_ip(self, ip_address):
        sw = self.host_to_sw_port.keys()
        for switch_id in sw:
            for port in self.host_to_sw_port[switch_id].keys():
                if ip_address in self.host_to_sw_port[switch_id][port]:
                    return switch_id

    def get_switch_port_by_ip(self, ip_address):
        sw = self.host_to_sw_port.keys()
        for switch_id in sw:
            for port in self.host_to_sw_port[switch_id].keys():
                if ip_address in self.host_to_sw_port[switch_id][port]:
                    return port

    def get_mac_by_ip(self, ip_address):
        sw = list(self.host_to_sw_port.keys())
        for switch_id in sw:
            for port in self.host_to_sw_port[switch_id].keys():
                if ip_address in self.host_to_sw_port[switch_id][port]:
                    return self.host_to_sw_port[switch_id][port][0]

    def get_port_from_link(self, dpid, next_id):
        if (dpid, next_id) in self.topo_inter_link.keys():
            return self.topo_inter_link[(dpid, next_id)][0]
        if (dpid, next_id) in self.topo_access_link.keys():
            return self.topo_access_link[(dpid, next_id)][0]

        """
        install flow entry
        """

    def get_mac_by_switch_port(self, dpid, port):
        return self.switch_mac_to_port[dpid][port]

    def install_flow_entry(self, path, src_ip, dst_ip, port=None, msg=None):
        print("--------------------------complete_flow_rule-----------------------------")
        num = len(path)
        if num == 1:
            # print("相关路径中交换机个数是1个")
            dpid = path[0]
            datapath = self.dpid_to_switch[dpid]
            in_port = port
            out_port = self.get_switch_port_by_ip(dst_ip)
            dst_mac = self.get_mac_by_ip(dst_ip)
            actions = [datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac),
                       datapath.ofproto_parser.OFPActionOutput(out_port)]
            match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                     in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
            self.add_flow(datapath, 1, match, actions)
            self.send_packet_to_outport(datapath, msg, in_port, actions)
        else:
            # print("相关路径中交换机个数是多个")
            for i in range(num - 1, -1, -1):
                dpid = path[i]
                print("****************", dpid)
                # print(self.switch_mac_to_port.keys())
                if dpid in self.dpid_to_switch.keys():
                    datapath = self.dpid_to_switch[dpid]
                    # print("*********", datapath)
                    if i == 0:
                        next_id = path[i + 1]
                        in_port = port
                        out_port = self.get_port_from_link(dpid, next_id)
                        # print("*********", dpid, in_port, out_port)
                        actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
                        match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                                 in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
                        self.add_flow(datapath, 1, match, actions)
                        self.send_packet_to_outport(datapath, msg, in_port, actions)
                    elif i == num - 1:
                        last_id = path[i - 1]
                        in_port = self.get_port_from_link(dpid, last_id)
                        out_port = self.get_switch_port_by_ip(dst_ip)
                        dst_mac = self.get_mac_by_ip(dst_ip)
                        # print("******", dst_mac)
                        actions = [datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac),
                                   datapath.ofproto_parser.OFPActionOutput(out_port)]
                        match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                                 in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
                        self.add_flow(datapath, 1, match, actions)
                    else:
                        next_id = path[i + 1]
                        last_id = path[i - 1]
                        in_port = self.get_port_from_link(dpid, last_id)
                        out_port = self.get_port_from_link(dpid, next_id)
                        actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
                        match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                                 in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
                        self.add_flow(datapath, 1, match, actions)

    def handle_topo_info_for_submit(self):
        host_info = []
        link_info = []
        merged_dict = self.topo_inter_link.copy()
        merged_dict.update(self.topo_access_link)
        for temp in merged_dict.keys():
            element = list(temp) + merged_dict[temp][2:]
            link_info.append(element)

        for i in self.host_to_sw_port.keys():
            for j in self.host_to_sw_port[i].keys():
                host_info.append([i, self.host_to_sw_port[i][j][1]])

        return link_info, host_info

    def handle_switches_for_submit(self):
        switch_info = list(self.dpid_to_switch.keys())
        # master_list = self.master_to_switches
        # return switch_list, master_list
        return switch_info

    def send_role_request(self, datapath, role, gen_id):
        # print("to switch %s send message for role message" % datapath.id)
        ofp_parser = datapath.ofproto_parser
        msg = ofp_parser.OFPRoleRequest(datapath, role, gen_id)
        datapath.send_msg(msg)

    def arp_storm_handle(self, datapath, in_port):
        out = datapath.ofproto_parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=datapath.ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=[], data=None)
        datapath.send_msg(out)

    def arp_reply_fake_mac(self, datapath, src_ip, dst_ip, src_mac, dst_mac, out_port):
        print("*************** 构造arp回复一个虚假的网关mac地址 ****************")
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        # if mac == "ff:ff:ff:ff:ff:ff":
        #     src_mac = self.get_mac_by_switch_port(dpid, out_port)
        # else:
        #     src_mac = mac

        ether_hd = ethernet.ethernet(dst=dst_mac,
                                     src=src_mac,
                                     ethertype=ether.ETH_TYPE_ARP)
        arp_hd = arp.arp(hwtype=1, proto=2048, hlen=6, plen=4,
                         opcode=2, src_mac=src_mac,
                         src_ip=src_ip, dst_mac=dst_mac,
                         dst_ip=dst_ip)
        arp_reply = packet.Packet()
        arp_reply.add_protocol(ether_hd)
        arp_reply.add_protocol(arp_hd)
        arp_reply.serialize()

        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(datapath, ofproto.OFP_NO_BUFFER,
                                  ofproto.OFPP_CONTROLLER, actions,
                                  arp_reply.data)
        # print("************** sending arp packet out **************")
        datapath.send_msg(out)

    """
        收集网络拓扑信息（包括交换机、主机、链路等信息），并且构建本地网络拓扑结构图
    """

    def _add_switch_map(self, sw):
        dpid = sw.dp.id
        self.logger.info('Register datapath: %016x, the ip address is %s', dpid, sw.dp.address)
        self.switch_mac_to_port.setdefault(dpid, {})
        self.host_to_sw_port.setdefault(dpid, {})
        self.mac_to_port.setdefault(dpid, {})
        if dpid not in self.dpid_to_switch:
            self.dpid_to_switch[dpid] = sw.dp
            self.dpid_to_switch_ip[dpid] = sw.dp.address
            for p in sw.ports:
                self.switch_mac_to_port[dpid][p.port_no] = p.hw_addr

    def _delete_switch_map(self, sw):
        if sw.dp.id in self.dpid_to_switch:
            self.logger.info('Unregister datapath: %016x', sw.dp.id)
            try:
                self.host_to_sw_port.pop(sw.dp.id,0)
                self.switch_mac_to_port.pop(sw.dp.id,0)
                self.mac_to_port.pop(sw.dp.id,0)
                self.dpid_to_switch.pop(sw.dp.id,0)
                self.dpid_to_switch_ip.pop(sw.dp.id,0)
                self.echo_timestamp.pop(sw.dp.id,0)
                print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
            except Exception as e:
                print("An error occured:", e)
                print("yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy")
                return

    def _update_switch_map(self, sw):
        dpid = sw.dp.id
        if dpid not in self.dpid_to_switch:
            self.logger.info('register again for datapath: %016x', sw.dp.id)
            self.switch_mac_to_port.setdefault(dpid, {})
            self.host_to_sw_port.setdefault(dpid, {})
            self.mac_to_port.setdefault(dpid, {})
            self.dpid_to_switch_ip[dpid] = sw.dp.address
            self.dpid_to_switch[dpid] = sw.dp
            self.echo_timestamp[dpid] = time.time()
            for p in sw.ports:
                self.switch_mac_to_port[dpid][p.port_no] = p.hw_addr
            print("aleardy not in !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        else:
            print("aleardy in !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")

    def delete_switch(self, dpid):
        if dpid in self.dpid_to_switch:
            self.logger.info('connect time out  Unregister datapath: %016x', dpid)
            # try:
            #     self.host_to_sw_port.pop(dpid,0)
            #     self.switch_mac_to_port.pop(dpid,0)
            #     self.mac_to_port.pop(dpid,0)
            #     self.dpid_to_switch.pop(dpid,0)
            #     self.dpid_to_switch_ip.pop(dpid,0)
            # except:
            #     pass
            datapath = self.dpid_to_switch[dpid]
            datapath.socket.close()
            datapath.close()

    def _check_switch_state(self, echo_timestamp):
        while True:
            check_switch_list = echo_timestamp
            curr_time = time.time()
            for dpid in list(check_switch_list.keys()):
                if (curr_time - check_switch_list[dpid]) > 30:
                    echo_timestamp.pop(dpid, 0)
                    hub.spawn(self.delete_switch, dpid)
            hub.sleep(5)

    def add_inter_link(self, link):
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no
        if (src_dpid, dst_dpid) not in self.topo_inter_link:
            self.topo_inter_link[(src_dpid, dst_dpid)] = [src_port, 0, 0, 0, 0]
            self.graph.add_edge(src_dpid, dst_dpid)

    def delete_inter_link(self, link):
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        if (src_dpid, dst_dpid) in self.topo_inter_link:
            del self.topo_inter_link[(src_dpid, dst_dpid)]
            self.graph.remove_edge(src_dpid, dst_dpid)

    def link_timeout_detection(self, access_link):
        """
        用于链路超时检测，如果某条链路超过一定时间没有进行更新，就会判定该链路失效，从而删除该链路信息，同步更新对外端口信息
        """
        while True:
            link_lists = access_link
            now_timestamp = time.time()
            for (src, dst) in list(link_lists.keys()):
                if (now_timestamp - link_lists[(src, dst)][1]) > 5:
                    try:
                        access_link.pop((src, dst))
                        self.graph.remove_edge(src, dst)
                    except:
                        pass
            hub.sleep(3)

    @set_ev_cls([event.EventSwitchEnter])
    def _switch_enter_handle(self, ev):
        switch = ev.switch
        self._add_switch_map(switch)

    @set_ev_cls([event.EventSwitchReconnected])
    def _switch_reconnected_handle(self, ev):
        print("reconnected the switch !!!")
        switch = ev.switch
        self._update_switch_map(switch)

    @set_ev_cls([event.EventSwitchLeave])
    def _switch_leave_handle(self, ev):
        switch = ev.switch
        self._delete_switch_map(switch)

    @set_ev_cls([event.EventLinkAdd])
    def add_link(self, ev):
        link = ev.link
        self.add_inter_link(link)

    @set_ev_cls([event.EventLinkDelete])
    def delete_link(self, ev):
        link = ev.link
        self.delete_inter_link(link)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    # @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    # def _state_change_handler(self, ev):
    #     datapath = ev.datapath
    #     if ev.state == MAIN_DISPATCHER:
    #         if datapath.id not in self.dpid_to_switch:
    #             self.logger.info('Register datapath: %016x, the ip address is %s', datapath.id, datapath.address)
    #             self.dpid_to_switch.setdefault(datapath.id,None)
    #             self.switch_mac_to_port.setdefault(datapath.id,{})
    #             self.host_to_sw_port.setdefault(datapath.id,{})
    #             self.dpid_to_switch_ip.setdefault(datapath.id,{})
    #             self.mac_to_port.setdefault(datapath.id,{})
    #
    #             self.dpid_to_switch[datapath.id] = datapath
    #             self.dpid_to_switch_ip[datapath.id] = datapath.address
    #     elif ev.state == DEAD_DISPATCHER:
    #         if datapath.id in self.dpid_to_switch:
    #             self.logger.info('Unregister datapath: %016x', datapath.id)
    #             try:
    #
    #                 del self.host_to_sw_port[datapath.id]
    #                 del self.switch_mac_to_port[datapath.id]
    #                 del self.mac_to_port[datapath.id]
    #                 del self.dpid_to_switch_ip[datapath.id]
    #                 del self.dpid_to_switch[datapath.id]
    #                 print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    #             except Exception as e:
    #                 print("An error occured:", e)
    #                 print("yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy")
    #                 return


    """
        对数据包进行处理
    """

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _lldp_packet_in_handle(self, ev):
        """
        针对LLDP数据包和IP数据包进行不同的处理方式:
        利用LLDP数据包发现域间链路
        利用IP或ARP数据包发现主机的相关信息，包括mac地址、ip地址以及相连接的交换机id与端口
        """
        # print("******************收到数据包************************")
        msg = ev.msg
        datapath = msg.datapath
        eth, pkt_type, pkt_data = ethernet.ethernet.parser(msg.data)
        dpid = datapath.id
        port = msg.match['in_port']

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            try:
                src_dpid, src_port_no, timestamp, echodelay = LLDPPacket.lldp_parse(msg.data)
                now_time = time.time()
                lldpdelay = now_time - timestamp
                # if src_dpid not in self.master_to_switches:
                # print("%s switch receive lldp message from %s switch ,the delaytime is %s" % (dpid, src_dpid, delay))
                self._save_lldp_delay(src=dpid, dst=src_dpid, lldpdelay=lldpdelay, echodelay=echodelay)
                if src_dpid not in self.dpid_to_switch.keys():
                    # print("%s switch receive lldp message from %s switch ,the delaytime is %s, the src_c echodelay is %s" % (
                    #     dpid, src_dpid, lldpdelay, echodelay))
                    if (dpid, src_dpid) not in self.topo_access_link:
                        self.topo_access_link[(dpid, src_dpid)] = [port, now_time, 0, 0, 0]
                        self.graph.add_edge(dpid, src_dpid)
                    else:
                        self.topo_access_link[(dpid, src_dpid)][1] = now_time
                # print("收到LLDP数据包,src_dpid = %s,dst_dpid = %s,src_port = %s,dst_port=%s "
                #       % (src_dpid, dpid, src_port_no, port))
            except LLDPPacket.LLDPUnknownFormat as e:
                return

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _switch_packet_in_handle(self, ev):
        """
        针对交换机发出的ARP、IP数据包进行处理。
        目的：针对控制器之间、交换机之间的信息交互，
        查表下发流表，流表设置保存时间，并对arp风暴进行处理
        :param ev:
        :return:
        """
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        in_port = msg.match['in_port']
        eth, pkt_type, pkt_data = ethernet.ethernet.parser(msg.data)
        src_mac = eth.src
        dst_mac = eth.dst
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            pkt, _, _ = pkt_type.parser(pkt_data)
            src_ip = pkt.src_ip
            dst_ip = pkt.dst_ip
            if src_ip in SWITCHES_IP and dst_ip == LOCAL_IP:
                self.arp_reply_fake_mac(datapath, dst_ip, src_ip, self.local_mac, src_mac, in_port)

        # if eth.ethertype not in [ether_types.ETH_TYPE_ARP, ether_types.ETH_TYPE_IP]:
        #     return
        # pkt, _, _ = pkt_type.parser(pkt_data)
        # try:
        #     src_ip = pkt.src_ip
        #     dst_ip = pkt.dst_ip
        # except:
        #     src_ip = pkt.src
        #     dst_ip = pkt.dst

        # 只处理交换机发出的ARP、IP数据包
        # if (src_ip in SWITCHES_IP and dst_ip == LOCAL_IP) or (dst_ip in SWITCHES_IP and src_ip == LOCAL_IP):
        # # if src_ip in SWITCHES_IP or dst_ip in SWITCHES_IP:
        #     if (dpid, src_mac, dst_ip) in self.arp_table:
        #         if self.arp_table[(dpid, src_mac, dst_ip)] != in_port:
        #             self.arp_storm_handle(datapath, in_port)
        #             # self.logger.info("交换机%s从%s号端口收到了从%s发来的%s数据包，询问%s的mac地址，因为冲突所以不处理", dpid, in_port, src_ip, eth.ethertype, dst_ip)
        #             # self.logger.info("type :%s packet in switch :%s in_port:%s, src_ip: %s dst_ip:%s src_mac:%s dst_mac:%s ,因为冲突所以不处理", eth.ethertype, dpid, in_port, src_ip, dst_ip, src_mac,dst_mac)
        #             return
        #
        #     self.arp_table[(dpid, src_mac, dst_ip)] = in_port
        #     self.mac_to_port.setdefault(dpid, {})
        #     self.mac_to_port[dpid][src_mac] = in_port
        #
        #
        #
        #     if dst_mac in self.mac_to_port[dpid]:
        #         out_port = self.mac_to_port[dpid][dst_mac]
        #     else:
        #         out_port = ofproto.OFPP_FLOOD
        #
        #     actions1 = [parser.OFPActionOutput(out_port)]
        #     actions2 = [parser.OFPActionOutput(in_port)]
        #     # self.logger.info("交换机%s从%s号端口收到了从%s发来的%s数据包，询问%s的mac地址",
        #     #                  dpid, in_port, src_ip, eth.ethertype, dst_ip)
        #     self.logger.info("type :%s packet in switch :%s in_port:%s, src_ip: %s dst_ip:%s src_mac:%s dst_mac:%s , out_port: %s",
        #                      eth.ethertype, dpid, in_port, src_ip, dst_ip, src_mac,dst_mac, out_port)
        #
        #
        #
        #     # install a flow to avoid packet_in next time
        #     if out_port != ofproto.OFPP_FLOOD:
        #         match1 = parser.OFPMatch(in_port=in_port, eth_dst=dst_mac, eth_src=src_mac)
        #         match2 = parser.OFPMatch(in_port=out_port, eth_dst=src_mac, eth_src=dst_mac)
        #         # verify if we have a valid buffer_id, if yes avoid to send both
        #         # flow_mod & packet_out
        #         if msg.buffer_id != ofproto.OFP_NO_BUFFER:
        #             self.add_flow(datapath, 1, match1, actions1, hard_timeout=5, buffer_id=msg.buffer_id)
        #             self.add_flow(datapath, 1, match2, actions2, hard_timeout=5, buffer_id=msg.buffer_id)
        #             return
        #         else:
        #             self.add_flow(datapath, 1, match1, actions1, hard_timeout=5)
        #             self.add_flow(datapath, 1, match2, actions2, hard_timeout=5)
        #     data = None
        #     if msg.buffer_id == ofproto.OFP_NO_BUFFER:
        #         data = msg.data
        #
        #     out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
        #                               in_port=in_port, actions=actions1, data=data)
        #     datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _host_arp_packet_in_handle(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match['in_port']
        eth, pkt_type, pkt_data = ethernet.ethernet.parser(msg.data)
        src_mac = eth.src
        dst_mac = eth.dst
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            pkt, _, _ = pkt_type.parser(pkt_data)
            src_ip = pkt.src_ip
            dst_ip = pkt.dst_ip
            if src_ip in SWITCHES_IP or src_ip in CONTROLLER_IP:
                return

            if not self.get_port(dpid, in_port):
                return

            # ignore switch-to-switch port
            if self.is_link_port(dpid, in_port):
                return
            self.logger.info("收到主机发来的数据包,构造arp回复一个虚假的网关mac地址", eth.ethertype, dpid, in_port, src_ip, dst_ip, src_mac, dst_mac)
            self.arp_reply_fake_mac(datapath, dst_ip, src_ip, FAKE_MAC, src_mac, in_port)

            host_mac = src_mac
            # print("src_mac %s src_ip %s" % (host_mac, ipv4))
            host = Host(host_mac, in_port, src_ip)
            self.host_to_sw_port.setdefault(dpid, {})
            self.host_to_sw_port[dpid][in_port] = [host.mac, host.ipv4]

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _host_ip_packet_in_handle(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match['in_port']
        eth, pkt_type, pkt_data = ethernet.ethernet.parser(msg.data)
        src_mac = eth.src
        dst_mac = eth.dst
        if eth.ethertype == ether_types.ETH_TYPE_IP:
            pkt, _, _ = pkt_type.parser(pkt_data)
            src_ip = pkt.src
            dst_ip = pkt.dst
            if src_ip in SWITCHES_IP or src_ip in CONTROLLER_IP:
                return
            self.logger.info("收到主机发来的数据包",eth.ethertype, dpid, in_port, src_ip, dst_ip, src_mac, dst_mac)
            dst_switch_id = self.get_switch_id_by_ip(dst_ip)
            if dst_switch_id:
                # 本地控制器内进行解决
                src_switch_id = dpid
                path = self.get_path(src_switch_id, dst_switch_id)
                print(path, "//////", type(path))
                if len(path) != 0:
                    self.install_flow_entry(path, src_ip, dst_ip, in_port, msg)
            else:
                route_msg = {"type": "route",
                             "switch_id": dpid,
                             "src_ip": src_ip,
                             "dst_ip": dst_ip,
                             "in_port": in_port,
                             "msg": msg}
                self.add_route_req_info(route_msg)

    @set_ev_cls(ofp_event.EventOFPRoleReply, MAIN_DISPATCHER)
    def role_reply_handle(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        ofp = dp.ofproto
        role = msg.role
        gen_id = msg.generation_id

        if role == ofp.OFPCR_ROLE_EQUAL:
            # print(' %s now is equal, gen_id is %s' % (dpid, gen_id))
            pass
        elif role == ofp.OFPCR_ROLE_MASTER:
            print('%s now is master, gen_id is %s' % (dpid, gen_id))
            # if dpid not in self.master_to_switches:
            #     self.master_to_switches.append(dpid)

        elif role == ofp.OFPCR_ROLE_SLAVE:
            print('%s now is slave, gen_id is %s' % (dpid, gen_id))
            # if dpid in self.master_to_switches:
            #     self.master_to_switches.remove(dpid)
        # print('')




