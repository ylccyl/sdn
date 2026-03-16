import time
import json
import socket
import logging

import netifaces
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

Initial_bandwidth = 800

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),  # 输出到控制台
    ]
)
logger = logging.getLogger("server_agent")

# 添加server配置
SERVER_CONFIG = {
    'server_ip': '10.5.1.131',  # 修改为server_agent的实际IP
    'server_port': 5001,
    'reconnect_interval': 5
}

class Host(object):  # 主机信息封装：用于存储主机的MAC地址、端口和IP地址，并提供了相关的方法（如转换为字典、字符串等）。
    # This is data class passed by EventHostXXX,EventHostXXX 类在特定事件发生时被触发，例如交换机连接、流表更新等。
    def __init__(self, mac, port, ipv4):
        super(Host, self).__init__()
        self.port = port  # 主机连接的网络端口
        self.mac = mac
        self.ipv4 = ipv4

    def to_dict(self):  # Host 对象的属性转换为字典格式,self 代表调用该方法的 Host 类的实例
        d = {'mac': self.mac,
             'ipv4': self.ipv4,
             'port': self.port.to_dict()}  # 这一行代码假设 self.port 是一个对象，并且该对象有一个 to_dict 方法，可以将端口信息转换为字典。
        return d

    # def update_ip(self, ip):
    #     self.ipv4 = ip

    def __eq__(self, host):
        return self.mac == host.mac and self.port == host.port

    def __str__(self):
        msg = 'Host<mac=%s, port=%s,' % (self.mac, str(self.port))
        msg += ','.join(self.ipv4) # 将这些地址连接成一个字符串，以逗号分隔。
        msg += '>'
        return msg


class TopoAwareness(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(TopoAwareness, self).__init__(*args, **kwargs)
        self.name = 'topo_awareness'
        self.topology_api_app = self  # 将当前实例赋值给属性
        self.local_mac = ''
        # Link、switch and host
        self.dpid_to_switch_ip = {}
        self.dpid_to_switch = {}  # Store switch in topology using OpenFlow
        self.switch_mac_to_port = {}  # {dpid:{port1:hw_addr1,port2:hwaddr2,...},...}嵌套字典,外层键(dpid)和内层键(port1)
        self.host_to_sw_port = {}  # {dpid1:{port1:[mac, ipv4],port2:[mac,ipv4]...},...}嵌套字典
        self.topo_inter_link = {}  # {(src.dpid, dst.dpid): (src.port_no, timestamp, delay, bw, loss)}存储交换机之间的内部链路信息.包括端口、时间戳、延迟、带宽和丢包率。
        self.topo_access_link = {}  # 存储接入链路信息（域间交换机的链路）
        # self.detection_access_link = {}  # 带有时间戳的外部链路信息，用于超时检测，超时检测后被赋值给真正的外部链路
        # self.detection_inter_link = {}

        # calculate delay
        self.echo_timestamp = {} # {dpid:recvtime,1:0.5,2:0.3,....}控制器收到交换机echo回复的时间戳，根据时间是否超过30秒交换机是否断开连接
        self.echo_latency = {}  # {dpid:delaytime,1:0.5,2:0.3,....}每个交换机与控制器之间的Echo时延
        self.lldp_delay = {}  # {(src,dst):time,(1,2):0.5,...}
        self.link_delay = {}  # {(src,dst):time,(1,2):0.5,....}交换机之间链路的延迟时间

        # calculate bw
        self.port_stats = {}
        self.free_bandwidth = {}  # {dpid: {port_no: (free_bandwidth, usage), ...}, ...} (Mbit/s),每个交换机端口的带宽使用情况
        self.port_loss_stats = {}  # 新增的端口丢包统计字典

        ###########
        self.mac_to_port = {}
        self.arp_table = {}  # ARP表{ (dpid, src_mac, dst_ip):in_port }


        self.graph = nx.DiGraph()  # graph用于存储网络拓扑的图结构,用 networkx 库中的 DiGraph 类创建了一个有向图。

        self.update_thread = hub.spawn(self.link_timeout_detection, self.topo_access_link)
        self.measure_thread = hub.spawn(self._detector)   # 启动一个线程，定期执行网络指标的测量任务
        self.monitor_thread = hub.spawn(self._monitor_thread)  # 启动一个线程，定期执行网络监控任务(未启用)
        self.show_info = hub.spawn(self.show)  # show负责定期输出网络中交换机的状态和其他相关信息。
        self.check_switch_thread = hub.spawn(self._check_switch_state, self.echo_timestamp)
        self.get_mac_thread = hub.spawn(self.get_local_mac_address)

        # 添加server连接相关的属性
        self.server_socket = None
        self.is_connected = False
        self.server_addr = (SERVER_CONFIG['server_ip'], SERVER_CONFIG['server_port'])
        
        # 启动server连接线程
        self.connect_thread = hub.spawn(self._connect_to_server)
        self.topo_update_thread = hub.spawn(self._send_topo_loop)

    def get_local_mac_address(self):
        # 使用 netifaces 库获取本地设备上网络接口信息,获取本地MAC地址
        interfaces = netifaces.interfaces()

        # 遍历接口并获取 MAC 地址
        for interface in interfaces:
            if interface == 'lo':  # 回环接口 lo 通常用于本地回环测试，不包含实际的物理MAC地址，因此需要跳过。
                continue  # 跳过回环接口
            try:
                self.local_mac = netifaces.ifaddresses(interface)[netifaces.AF_LINK][0]['addr']  # 从返回的字典中提取 MAC 地址信息
                break                                                                            # 从(列表中的第一个元素)第一个地址字典中提取 addr 键的值
            except KeyError:
                pass

    """
        收集网络带宽信息
    """
    # 定期请求交换机的端口统计信息，计算带宽使用情况，并保存到数据结构中。
    def _monitor_thread(self):
        while True:
            self._request_stats()
            self.add_bandwidth_info(self.free_bandwidth)  # 将当前的各个端口的可用带宽信息传递给 add_bandwidth_info 方法,实时更新带宽的使用情况
            hub.sleep(1.2)

    # Stat request: 向网络中的交换机请求端口统计信息
    def _request_stats(self):
        datapaths = list(self.dpid_to_switch.values())
        for datapath in datapaths:
            self.logger.debug('send stats request: %016x', datapath.id)
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)  # 创建一个 OpenFlow 消息，用于请求特定交换机的所有端口统计信息
            datapath.send_msg(req)  # 将创建的统计请求消息发送到对应的交换机
            hub.sleep(0.5)

    def _save_stats(self, _dict, key, value, history_length=2):  # 将统计数据以键值对的形式存储，并限制历史记录的长度
        if key not in _dict:
            _dict[key] = []

        _dict[key].append(value)  # 将 value 添加到 _dict[key] 列表的末尾

        if len(_dict[key]) > history_length:  # 用于指定保留的历史记录长度
            _dict[key].pop(0)   # 检查 _dict[key] 列表的长度。如果超过 history_length，则使用 pop(0) 方法删除列表的第一个元素（最旧的记录）

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
    # 处理来自交换机的端口统计信息，计算端口的速度，并更新带宽使用情况。
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        """
            保存端口统计信息
            计算端口速度并保存
            计算丢包率并保存
        """
        body = ev.msg.body
        dpid = ev.msg.datapath.id

        # 添加显示统计信息的日志
        # self.logger.info("\n=== Switch %s Port Statistics ===", dpid)
        # self.logger.info("Port     Rx-Pkts     Tx-Pkts     Rx-Bytes     Tx-Bytes     Rx-Dropped  Tx-Dropped")
        # self.logger.info("----     -------     -------     --------     --------     ----------  ----------")
# 
        self.free_bandwidth.setdefault(dpid, {})
        self.port_loss_stats.setdefault(dpid, {})
        now_timestamp = time.time()

        for stat in sorted(body, key=attrgetter('port_no')):
            port_no = stat.port_no
            if port_no != ofproto_v1_3.OFPP_LOCAL:
                # 显示原始统计数据
                # self.logger.info("%4d  %10d  %10d  %10d  %10d  %10d  %10d",
                            #    port_no,
                            #    stat.rx_packets, stat.tx_packets,
                            #    stat.rx_bytes, stat.tx_bytes,
                            #    stat.rx_dropped, stat.tx_dropped)

                key = (dpid, port_no)
                value = (stat.tx_packets, stat.rx_packets, stat.tx_bytes, stat.rx_bytes, 
                        stat.rx_dropped, stat.tx_dropped, now_timestamp)

                # 保存端口统计数据
                self._save_stats(self.port_stats, key, value, 5)

                # 计算丢包率
                if key[0] in self.port_loss_stats and key[1] in self.port_loss_stats[key[0]]:
                    prev_rx_dropped, prev_tx_dropped = self.port_loss_stats[key[0]][key[1]]
                    prev_stats = self.port_stats[key][-2]
                    
                    # 计算这个周期内的变化量
                    rx_packets_delta = stat.rx_packets - prev_stats[1]
                    tx_packets_delta = stat.tx_packets - prev_stats[0]
                    rx_dropped_delta = stat.rx_dropped - prev_rx_dropped
                    tx_dropped_delta = stat.tx_dropped - prev_tx_dropped

                    # 分别计算接收和发送方向的丢包率
                    rx_loss_rate = 0.0
                    tx_loss_rate = 0.0

                    if rx_packets_delta + rx_dropped_delta > 0:
                        rx_loss_rate = float(rx_dropped_delta) / (rx_packets_delta + rx_dropped_delta)
                    if tx_packets_delta + tx_dropped_delta > 0:
                        tx_loss_rate = float(tx_dropped_delta) / (tx_packets_delta + tx_dropped_delta)

                    # 取两个方向中的最大值作为链路的丢包率
                    loss_rate = max(rx_loss_rate, tx_loss_rate)
                    
                    # 更新链路丢包率
                    self._update_link_loss_rate(dpid, port_no, loss_rate)

                # 保存当前的丢包计数
                self.port_loss_stats[key[0]][key[1]] = (stat.rx_dropped, stat.tx_dropped)

                # 计算带宽相关信息
                port_stats = self.port_stats[key]
                if len(port_stats) > 1:
                    curr_stat = port_stats[-1][2]
                    # self.logger.info("Current Stat: %s", curr_stat)
                    prev_stat = port_stats[-2][2]
                    # self.logger.info("Previous Stat: %s", prev_stat)
                    period = self._get_period(port_stats[-1][6], port_stats[-2][6])
                    # self.logger.info("Period: %s", period)
                    speed = self._cal_speed(curr_stat, prev_stat, period)
                    # self.logger.info("Speed: %s", speed)

                    # 输出 curr_stat、prev_stat、period 和 speed





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
            hub.sleep(1)

    def _send_echo_request(self):
        """
            Seng echo request msg to datapath. 控制器发送Echo Request以探测链路延迟，交换机接收到请求后，发送 Echo Reply 来响应控制器的请求。
        """
        datapaths = list(self.dpid_to_switch.values())
        for datapath in datapaths:
            parser = datapath.ofproto_parser
            data_time = "%.12f" % time.time()  # data_time 表示的是控制器发送 Echo Request 消息的时刻
            byte_arr = bytearray(data_time.encode())  # 时间戳转换为字节数组 byte_arr，这是发送数据的一部分

            echo_req = parser.OFPEchoRequest(datapath, data=byte_arr)  # 这里的data 是控制器向交换机发送 Echo Request消息的时间戳
            datapath.send_msg(echo_req)

            # Important! Don't send echo request together, Because it will
            # generate a lot of echo reply almost in the same time.
            # which will generate a lot of delay of waiting in queue
            # when processing echo reply in echo_reply_handler.
            hub.sleep(0.5)

    def add_delay_info(self):  # 遍历所有内部链路，获取每条链路的时延信息(285,313行方法)，并将这些时延信息更新到 self.topo_inter_link 字典和 self.graph 图对象中
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

                    fwd_delay---> 指的是数据包从控制器到交换机A，再到交换机B，最后到控制器的总的时延
                        <----reply_delay
            delay = (forward delay + reply delay - src datapath's echo latency 解释有问题
        """
        try:
            fwd_delay = self.lldp_delay[(src, dst)][0]
            src_latency = self.echo_latency[src]
            dst_latency = self.lldp_delay[(src, dst)][1]

            delay = fwd_delay - (src_latency + dst_latency) / 2
            # print(f"Calculating inter delay: fwd={fwd_delay:.12f}    src_lat={src_latency:.12f}  dst_lat={dst_latency:.12f}  delay={delay:.12f}")
            return max(delay, 0)
        except:
            return float(0)
    # 计算接入链路的延迟
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
            # print(f"Calculating  access delay: fwd={fwd_delay:.12f}    src_lat={src_latency:.12f}  dst_lat={dst_latency:.12f}  delay={delay:.12f}")
            return max(delay, 0)
        except:
            return float('inf')

    def _save_lldp_delay(self, src=0, dst=0, lldpdelay=0, echodelay=0):
        self.lldp_delay[(src, dst)] = [lldpdelay, echodelay]

    # 处理 Echo 回复消息，计算链路延迟
    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def _echo_reply_handler(self, ev):
        """
            Handle the echo reply msg, and get the latency of link.
        """
        now_timestamp = time.time()
        try:
            latency = now_timestamp - eval(ev.msg.data)
            # 将交换机对应的echo时延写入字典保存起来
            self.echo_latency[ev.msg.datapath.id] = latency
            self.echo_timestamp[ev.msg.datapath.id] = now_timestamp  # 控制器收到交换机echo回复的时间戳
        except:
            print("echo reply error")
            return

    """
        获取交换机相关信息，包括ID编号、端口号、mac地址
    """

    def show(self):
        while True:
            print("***********************")
            print("交换机列表", self.dpid_to_switch.keys())
            print("交换机端口地址对应列表",self.switch_mac_to_port)
            print("内部链路", self.topo_inter_link)
            print("主机链路", self.host_to_sw_port)
            self.arp_table.clear()
            print("外部链路",self.topo_access_link)
            print("图中的链路信息",self.graph.edges(data=True))
            

            print("\n")
            hub.sleep(5)

    # def switches_role_detection(self):  # 未被调用 确定每台交换机当前的角色（主控、从属等）
    #     for i in self.dpid_to_switch.keys():
    #         datapath = self.dpid_to_switch[i]  # datapath 中存放的是 值,是该 DPID 关联的交换机对象
    #         self.send_role_request(datapath, datapath.ofproto.OFPCR_ROLE_NOCHANGE, 0)

    def get_path(self, src, dst):
        """
        计算从源交换机到目标交换机的最短路径
        """
        # 如果源和目标是同一个交换机，直接返回包含该交换机的列表
        if src == dst:
            return [src]
            
        try:
            path = nx.shortest_path(self.graph, src, dst)  # dijkstra
            return path   # 如果找到路径，返回计算得到的路径列表（list）
        except:
            self.logger.error("【错误】无法找到从交换机 %s 到交换机 %s 的路径", src, dst)
            return []

    def get_port(self, dpid, port_no):  # 检查给定的交换机（通过其 DPID）是否包含指定的端口号
        if port_no in self.switch_mac_to_port[dpid].keys():
            return True
        return False
    # 验证一个交换机和端口的组合是否存在于网络拓扑中
    def is_link_port(self, dpid, port):  # 检查指定的端口是否是交换机的链路端口
        for link in self.topo_inter_link.keys():
            if dpid == link[0] and port == self.topo_inter_link[link][0]:
                return True # 该端口是交换机之间的链接端口
        for link in self.topo_access_link.keys():
            if dpid == link[0] and port == self.topo_access_link[link][0]:
                return True # 该端口是接入端口
        return False

    def add_flow(self, datapath, priority, match, actions, proto=0, hard_timeout=0, idle_timeout=0, buffer_id=None):
        """
        向交换机下发流表
        Deliver the flow table to the switch
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]  # OFPInstructionActions 是一个 OpenFlow 指令，用于定义流表条目匹配后应执行的动作
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
                                    instructions=inst)   # 创建一个 OFPFlowMod 消息
        datapath.send_msg(mod)
    #将数据包发送到指定的输出端口。
    def send_packet_to_outport(self, datapath, msg, in_port, actions):
        """
        进行广播设置
        Setting up a broadcast
        """
        data = None
        if msg.buffer_id == datapath.ofproto.OFP_NO_BUFFER:  # 如果数据包没有被交换机缓存，控制器需要使用 msg.data 来重新构建数据包并发送出去。
            data = msg.data  # msg包含接收到的数据包的信息，数据包的原始字节数据，存储在 msg.data

        out = datapath.ofproto_parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id, in_port=in_port,
                                                   actions=actions, data=data)
        datapath.send_msg(out)
    # 检查每个交换机的每个端口连接的主机IP地址，如果找到匹配的IP地址，则返回对应的交换机ID
    def get_switch_id_by_ip(self, ip_address):
        sw = self.host_to_sw_port.keys()
        for switch_id in sw:
            for port in self.host_to_sw_port[switch_id].keys():
                if ip_address in self.host_to_sw_port[switch_id][port]:   # 指定的IP地址（主机的IP地址）是否与某个交换机的特定端口连接的主机相关联。
                    return switch_id

    def get_switch_port_by_ip(self, ip_address):  # 通过目的IP地址找到与之关联的交换机端口
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
                    return self.host_to_sw_port[switch_id][port][0]  # 根据给定的IP地址（主机的IP地址）查找并返回与该 IP 地址关联的主机的 MAC 地址

    def get_port_from_link(self, dpid, next_id):
        if (dpid, next_id) in self.topo_inter_link.keys():
            return self.topo_inter_link[(dpid, next_id)][0]  # 返回的是（当前交换机）和 next_id（下一个设备 ID）之间连接的第一个端口的信息。返回的端口属于第一个交换机
        if (dpid, next_id) in self.topo_access_link.keys():
            return self.topo_access_link[(dpid, next_id)][0]
            
    def install_flow_entry(self, path, src_ip, dst_ip, port=None, msg=None):
        """
        install flow entry 在 OpenFlow 交换机的流表中添加一条流表项
        """
        num = len(path)
        if num == 1:  # 当路径中只有一个交换机时的流表安装和数据包转发
            dpid = path[0]
            datapath = self.dpid_to_switch[dpid]
            in_port = port
            
            # 直接查找目标IP对应的端口和MAC地址
            dst_port = None
            dst_mac_addr = None
            
            for p in self.host_to_sw_port.get(dpid, {}):
                host_info = self.host_to_sw_port[dpid][p]
                if host_info[1] == dst_ip:
                    dst_port = p
                    dst_mac_addr = host_info[0]
                    break
            
            if not dst_port or not dst_mac_addr:
                return
            
            # 获取源主机的MAC地址
            src_mac_addr = None
            for p in self.host_to_sw_port.get(dpid, {}):
                host_info = self.host_to_sw_port[dpid][p]
                if host_info[1] == src_ip:
                    src_mac_addr = host_info[0]
                    break
            
            if not src_mac_addr:
                src_mac_addr = "未知"  # 如果找不到源MAC，使用默认值
                           
            # 创建正向流表
            actions = [datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac_addr),
                      datapath.ofproto_parser.OFPActionOutput(dst_port)]
            match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                   in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
            self.add_flow(datapath, 1, match, actions)
            
            # 创建反向流表
            actions_reverse = [datapath.ofproto_parser.OFPActionSetField(eth_dst=src_mac_addr),
                              datapath.ofproto_parser.OFPActionOutput(in_port)]
            match_reverse = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                           in_port=dst_port, ipv4_dst=src_ip, ipv4_src=dst_ip)
            self.add_flow(datapath, 1, match_reverse, actions_reverse)
            
            # 发送当前数据包
            if msg:
                self.send_packet_to_outport(datapath, msg, in_port, actions)
                
            self.logger.info("【成功】单交换机流表安装完成: %s <-> %s", src_ip, dst_ip)
        elif num == 2:  # 特殊处理：直接路径，只有源交换机和目标交换机
            # 处理源交换机
            src_dpid = path[0]
            dst_dpid = path[1]
            
            # 检查源交换机和目标交换机之间是否有直接连接
            src_to_dst_port = self.get_port_from_link(src_dpid, dst_dpid)
            
            if not src_to_dst_port:
                return
                
            # 源交换机流表
            src_datapath = self.dpid_to_switch[src_dpid]
            src_in_port = port
            src_out_port = src_to_dst_port
            
            # 正向流表
            src_actions = [src_datapath.ofproto_parser.OFPActionOutput(src_out_port)]
            src_match = src_datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                          in_port=src_in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
            self.add_flow(src_datapath, 1, src_match, src_actions)
            
            # 目标交换机流表
            dst_datapath = self.dpid_to_switch[dst_dpid]
            dst_in_port = self.get_port_from_link(dst_dpid, src_dpid)
            
            if not dst_in_port:
                return
                
            # 查找目标IP对应的端口和MAC地址
            dst_out_port = None
            dst_mac_addr = None
            
            for p in self.host_to_sw_port.get(dst_dpid, {}):
                host_info = self.host_to_sw_port[dst_dpid][p]
                if host_info[1] == dst_ip:
                    dst_out_port = p
                    dst_mac_addr = host_info[0]
                    break
            
            if not dst_out_port or not dst_mac_addr:
                return
                
            # 正向流表
            dst_actions = [dst_datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac_addr),
                          dst_datapath.ofproto_parser.OFPActionOutput(dst_out_port)]
            dst_match = dst_datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                          in_port=dst_in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
            self.add_flow(dst_datapath, 1, dst_match, dst_actions)
            
            # 查找源IP对应的MAC地址
            src_mac_addr = None
            for p in self.host_to_sw_port.get(src_dpid, {}):
                host_info = self.host_to_sw_port[src_dpid][p]
                if host_info[1] == src_ip:
                    src_mac_addr = host_info[0]
                    break
                    
            if not src_mac_addr:
                src_mac_addr = "未知"  # 如果找不到源MAC，使用默认值
                
            # 反向流表 - 目标交换机
            dst_actions_reverse = [dst_datapath.ofproto_parser.OFPActionOutput(dst_in_port)]
            dst_match_reverse = dst_datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                                  in_port=dst_out_port, ipv4_dst=src_ip, ipv4_src=dst_ip)
            self.add_flow(dst_datapath, 1, dst_match_reverse, dst_actions_reverse)
            
            # 反向流表 - 源交换机
            src_actions_reverse = [src_datapath.ofproto_parser.OFPActionSetField(eth_dst=src_mac_addr),
                                  src_datapath.ofproto_parser.OFPActionOutput(src_in_port)]
            src_match_reverse = src_datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                                  in_port=src_out_port, ipv4_dst=src_ip, ipv4_src=dst_ip)
            self.add_flow(src_datapath, 1, src_match_reverse, src_actions_reverse)
            
            # 发送当前数据包
            if msg:
                self.send_packet_to_outport(src_datapath, msg, src_in_port, src_actions)
                
            self.logger.info("【成功】两交换机流表安装完成: %s <-> %s, 路径: %s", src_ip, dst_ip, path)
        else:
            # 从最后一个交换机开始，逆序安装流表
            for i in range(num - 1, -1, -1):
                dpid = path[i]  # 当前处理的交换机ID
                
                if dpid in self.dpid_to_switch.keys():
                    datapath = self.dpid_to_switch[dpid]  # 获取交换机的datapath对象
                    
                    if i == 0:  # 第一个交换机（源交换机）
                        next_id = path[i + 1]
                        in_port = port
                        out_port = self.get_port_from_link(dpid, next_id)
                        
                        if not out_port:
                            continue
                        
                        # 正向流表
                        actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
                        match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                               in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
                        self.add_flow(datapath, 1, match, actions)
                        
                        # 反向流表
                        actions_reverse = [datapath.ofproto_parser.OFPActionOutput(in_port)]
                        match_reverse = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                                       in_port=out_port, ipv4_dst=src_ip, ipv4_src=dst_ip)
                        self.add_flow(datapath, 1, match_reverse, actions_reverse)
                        
                        # 发送当前数据包
                        if msg:
                            self.send_packet_to_outport(datapath, msg, in_port, actions)
                        
                    elif i == num - 1:  # 最后一个交换机（目标交换机）
                        last_id = path[i - 1]
                        in_port = self.get_port_from_link(dpid, last_id)
                        
                        if not in_port:
                            continue
                        
                        # 查找目标IP对应的端口和MAC地址
                        dst_port = None
                        dst_mac_addr = None
                        
                        for p in self.host_to_sw_port.get(dpid, {}):
                            host_info = self.host_to_sw_port[dpid][p]
                            if host_info[1] == dst_ip:
                                dst_port = p
                                dst_mac_addr = host_info[0]
                                break
                        
                        if not dst_port or not dst_mac_addr:
                            continue
                        
                        # 正向流表
                        actions = [datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac_addr),
                                  datapath.ofproto_parser.OFPActionOutput(dst_port)]
                        match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                               in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
                        self.add_flow(datapath, 1, match, actions)
                        
                        # 反向流表
                        actions_reverse = [datapath.ofproto_parser.OFPActionOutput(in_port)]
                        match_reverse = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                                       in_port=dst_port, ipv4_dst=src_ip, ipv4_src=dst_ip)
                        self.add_flow(datapath, 1, match_reverse, actions_reverse)
            
            self.logger.info("【成功】多交换机流表安装完成: %s <-> %s, 路径: %s", src_ip, dst_ip, path)

    """
        收集网络拓扑信息（包括交换机、主机、链路等信息），并且构建本地网络拓扑结构图
    """

    def _add_switch_map(self, sw):
        dpid = sw.dp.id
        self.logger.info('Register datapath: %016x, the ip address is %s', dpid, sw.dp.address)
        self.switch_mac_to_port.setdefault(dpid, {})   # 如果 dpid 已经在字典中，则返回对应的值。如果不存在，则将 dpid 添加到字典中，并赋值为一个新的空字典 {}
        self.host_to_sw_port.setdefault(dpid, {})
        self.mac_to_port.setdefault(dpid, {})
        if dpid not in self.dpid_to_switch:
            self.dpid_to_switch[dpid] = sw.dp
            self.dpid_to_switch_ip[dpid] = sw.dp.address
            for p in sw.ports:   # 遍历交换机的所有端口，并将每个端口的端口号与其对应的硬件地址（MAC 地址）关联起来
                self.switch_mac_to_port[dpid][p.port_no] = p.hw_addr

    def _delete_switch_map(self, sw):
        if sw.dp.id in self.dpid_to_switch:
            self.logger.info('Unregister datapath: %016x', sw.dp.id)
            try:
                self.host_to_sw_port.pop(sw.dp.id,0)  # 从字典中删除某个键，并返回该键对应条目的值，如果指定的键不存在，则返回该默认值（在此处是 0）
                self.switch_mac_to_port.pop(sw.dp.id,0)
                self.mac_to_port.pop(sw.dp.id,0)
                self.dpid_to_switch.pop(sw.dp.id,0)
                self.dpid_to_switch_ip.pop(sw.dp.id,0)
                self.echo_timestamp.pop(sw.dp.id,0)

            except Exception as e:
                print("An error occured:", e)
    
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
            #     pass  当前的 except 语句没有指定异常类型，建议明确捕获特定异常，以提高代码的可读性和维护性
            datapath = self.dpid_to_switch[dpid]
            datapath.socket.close()  # 关闭与交换机之间的网络连接
            datapath.close()   # 释放与 datapath 相关的其他资源，执行必要的清理操作

    def _check_switch_state(self, echo_timestamp):  # 参数最后一次收到该交换机 Echo 回复的时间戳
        while True:
            check_switch_list = echo_timestamp  # echo_timestamp：键是交换机的DPID（Data Path ID），值是最后一次收到该交换机Echo回复的时间戳。
            curr_time = time.time()
            for dpid in list(check_switch_list.keys()):  # 遍历交换机的 DPID 是为了检查每个交换机的状态，尤其是判断它们是否在指定的时间内没有响应回声请求。将键的视图转换为一个列表
                if (curr_time - check_switch_list[dpid]) > 70:
                    self.logger.info("_check_switch_state方法中删除交换机: %016x", dpid)  # 添加打印信息
                    echo_timestamp.pop(dpid, 0)
                    hub.spawn(self.delete_switch, dpid)
            hub.sleep(5)

    def add_inter_link(self, link):   # 添加交换机之间的链路信息
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

    def link_timeout_detection(self, access_link):  #  access_link，这是一个字典，通常用于存储链路信息，其中键是源和目标节点的元组，值是与链路相关的属性（例如时间戳）
        """
        用于链路超时检测，如果某条链路超过一定时间没有进行更新，就会判定该链路失效，从而删除该链路信息，同步更新对外端口信息
        """
        while True:
            link_lists = access_link
            now_timestamp = time.time()
            for (src, dst) in list(link_lists.keys()):
                if (now_timestamp - link_lists[(src, dst)][1]) > 70:  # 当前的时间戳与该链接的最后更新时间戳。
                    try:
                        self.logger.info("域间交换机链路超时，删除交换机链路: 从交换机 %s 到交换机 %s", src, dst)  # 添加打印信息
                        access_link.pop((src, dst))
                        self.graph.remove_edge(src, dst)
                    except:
                        pass
            hub.sleep(3)

    @set_ev_cls([event.EventSwitchEnter])
    def _switch_enter_handle(self, ev):
        switch = ev.switch
        self._add_switch_map(switch)  # 将新加入的交换机信息添加到应用内部的数据结构中

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
    def switch_features_handler(self, ev):  # 该方法未被调用,在交换机连接时获取其特征（如端口信息）???
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        # 初始化一个新的匹配条件对象。
        match = parser.OFPMatch()  # OFPMatch 允许控制器指定要匹配的数据包头字段，例如源 IP 地址、目标 IP 地址、源端口、目标端口、协议类型等
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]  # 将数据包发送到控制器,控制器不应缓存数据包，而是立即处理它
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
        #print("******************收到数据包************************")
        msg = ev.msg
        datapath = msg.datapath
        eth, pkt_type, pkt_data = ethernet.ethernet.parser(msg.data)
        dpid = datapath.id
        port = msg.match['in_port']

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            # self.logger.info("Received LLDP packet000000: dpid=%s, dpid")
            try:
                # print("******************收到LLDP数据包************************")
                src_dpid, src_port_no, timestamp, echodelay = LLDPPacket.lldp_parse(msg.data)
                # print("src_dpid=%s,src_port_no=%s,timestamp=%s,echodelay=%s",src_dpid,src_port_no,timestamp,echodelay)
                now_time = time.time()
                lldpdelay = now_time - timestamp
                # if src_dpid not in self.master_to_switches:
                # print("%s switch receive lldp message from %s switch ,the delaytime is %s" % (dpid, src_dpid, delay))
                self._save_lldp_delay(src=dpid, dst=src_dpid, lldpdelay=lldpdelay, echodelay=echodelay)
                # print(f"7777delay: lldpdelay={lldpdelay:.12f}    echodelay={echodelay:.12f}")
                # self.logger.info("Received LLDP packet111111: dpid=%s, src_dpid=%s, port=%s", dpid, src_dpid, port)
                if src_dpid not in self.dpid_to_switch.keys():  # 该交换机并不属于自己管理的自组域.
                    # print("%s switch receive lldp message from %s switch ,the delaytime is %s, the src_c echodelay is %s" % (
                    #     dpid, src_dpid, lldpdelay, echodelay))
                    if (dpid, src_dpid) not in self.topo_access_link:
                        # self.logger.info("Received LLDP packet222222: dpid=%s, src_dpid=%s, port=%s", dpid, src_dpid, port)
                        self.topo_access_link[(dpid, src_dpid)] = [port, now_time, 0, 0, 0]
                        self.graph.add_edge(dpid, src_dpid)
                    else:
                        self.topo_access_link[(dpid, src_dpid)][1] = now_time  # 对链路生成的时间戳信息进行更新
                # print("收到LLDP数据包,src_dpid = %s,dst_dpid = %s,src_port = %s,dst_port=%s "
                #       % (src_dpid, dpid, src_port_no, port))
            except LLDPPacket.LLDPUnknownFormat as e:
                return

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _switch_packet_in_handle(self, ev):
        """
        针对交换机发出的ARP、IP数据包进行处理。(根据数据包的内容进行流表的查询和更新，以及防止ARP风暴)
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
        in_port = msg.match['in_port']  # 数据包进入交换机的端口号
        eth, pkt_type, pkt_data = ethernet.ethernet.parser(msg.data)  # pkt_data从以太网帧中提取的有效载荷部分,是网络层的完整数据包
        src_mac = eth.src
        dst_mac = eth.dst
        # if eth.ethertype == ether_types.ETH_TYPE_ARP:
        #     pkt, _, _ = pkt_type.parser(pkt_data)
        #     src_ip = pkt.src_ip
        #     dst_ip = pkt.dst_ip
        #     if src_ip in SWITCHES_IP and dst_ip == LOCAL_IP:
        #         self.arp_reply_fake_mac(datapath, dst_ip, src_ip, self.local_mac, src_mac, in_port)

        if eth.ethertype not in [ether_types.ETH_TYPE_ARP, ether_types.ETH_TYPE_IP]:
            return
        pkt, _, _ = pkt_type.parser(pkt_data)
        try:
            src_ip = pkt.src_ip
            dst_ip = pkt.dst_ip
        except:
            src_ip = pkt.src
            dst_ip = pkt.dst

         # 只处理交换机之间的链路端口
        # if not self.is_link_port(dpid, in_port):
            # 
            # self.logger.info("_switch_packet_in_handle忽略非交换机链路端口的数据包: 源 IP=%s, 目标 IP=%s, 源 MAC=%s, 目标 MAC=%s, 交换机=%s, 端口=%s",  
                                # src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)
            # return
        # print("type :%s packet in switch :%s  in_port:%s,  src_ip: %s  dst_ip:%s  src_mac:%s  dst_mac:%s ",
        #       eth.ethertype, dpid, in_port, src_ip, dst_ip, src_mac, dst_mac);
        # 只处理交换机发出的ARP、IP数据包
        # if (src_ip in SWITCHES_IP and dst_ip == LOCAL_IP) or (dst_ip in SWITCHES_IP and src_ip == LOCAL_IP):
            # if (dpid, src_mac, dst_ip) in self.arp_table:
                # if self.arp_table[(dpid, src_mac, dst_ip)] != in_port:  #检查与该记录相关联的输入端口 (in_port) 是否与当前输入端口一致。如果不一致，说明同一源 MAC 地址在不同的端口上出现，可能会导致 ARP 风暴。
                    # return
            # 更新 ARP 表
        self.logger.info("00_switch_packet_in_handle收到数据包:源IP=%s,目标IP=%s,源MAC=%s,目标MAC=%s,交换机=%s,端口=%s",   
                            src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)

        self.arp_table[(dpid, src_mac, dst_ip)] = in_port
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src_mac] = in_port  # src_mac发起网络请求的主机的MAC 地址

        if dst_mac in self.mac_to_port[dpid]:  # 这里的mac_to_port与851行中的mac_to_port存放的MAC地址和port的映射不一样
            out_port = self.mac_to_port[dpid][dst_mac]  # 如果存在，说明控制器已经记录了目标 MAC 地址
            if eth.ethertype == ether_types.ETH_TYPE_ARP:
                self.logger.info("*************************ARP**************************************************")
                self.logger.info("22目标MAC地址已知,数据包类型=ARP,out_port=%s,源IP=%s,目标IP=%s,源MAC=%s,目标MAC=%s,交换机=%s,in_port=%s",
                    out_port, src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)
            elif eth.ethertype == ether_types.ETH_TYPE_IP:
                self.logger.info("22目标MAC地址已知,数据包类型=IP,out_port=%s,源IP=%s,目标IP=%s,源MAC=%s,目标MAC=%s,交换机=%s,in_port=%s",
                    out_port, src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)
            else:
                self.logger.info("22目标MAC地址已知,数据包类型=未知,out_port=%s,源IP=%s,目标IP=%s,源MAC=%s,目标MAC=%s,交换机=%s,in_port=%s",
                    out_port, src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)
        else:
            out_port = ofproto.OFPP_FLOOD
            if eth.ethertype == ether_types.ETH_TYPE_ARP:
                self.logger.info("*************************ARP**************************************************")
                self.logger.info("22目标MAC地址未知,数据包类型=ARP,out_port=OFPP_FLOOD,源IP=%s,目标IP=%s,源MAC=%s,目标MAC=%s,交换机=%s,in_port=%s",
                    src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)
            elif eth.ethertype == ether_types.ETH_TYPE_IP:
                return
                self.logger.info("22目标MAC地址未知,数据包类型=IP,out_port=OFPP_FLOOD,源IP=%s,目标IP=%s,源MAC=%s,目标MAC=%s,交换机=%s,in_port=%s",
                    src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)
                
            else:
                self.logger.info("22目标MAC地址未知,数据包类型=未知,out_port=OFPP_FLOOD,源IP=%s,目标IP=%s,源MAC=%s,目标MAC=%s,交换机=%s,in_port=%s",
                    src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)
        
        # if not self.is_link_port(dpid, in_port):
            # self.logger.info("_switch_packet_in_handle忽略非交换机链路端口的数据包: dpid=%s, in_port=%s", dpid, in_port)
            # return
        actions1 = [parser.OFPActionOutput(out_port)]
        actions2 = [parser.OFPActionOutput(in_port)]
        self.logger.info("交换机%s从%s号端口收到了从%s发来的%s数据包，询问%s的mac地址",
                         dpid, in_port, src_ip, eth.ethertype, dst_ip)
        # self.logger.info("type :%s packet in switch :%s in_port:%s, src_ip: %s dst_ip:%s src_mac:%s dst_mac:%s , out_port: %s",
                #  eth.ethertype, dpid, in_port, src_ip, dst_ip, src_mac, dst_mac, 
                #  out_port if out_port != ofproto_v1_3.OFPP_FLOOD else "FLOOD")
        

        # install a flow to avoid packet_in next time
        if out_port != ofproto.OFPP_FLOOD:  # OFPMatch 是 OpenFlow 中用于定义数据包匹配条件的类。它允许控制器根据特定字段来识别和处理数据包
            match1 = parser.OFPMatch(in_port=in_port, eth_dst=dst_mac, eth_src=src_mac)
            match2 = parser.OFPMatch(in_port=out_port, eth_dst=src_mac, eth_src=dst_mac)  # match2 可以用于处理回复数据包或响应，确保数据包能够正确返回
            # 定义了两个方向的流量匹配条件，适用于实现双向通信的场景，例如在交换机中设置流规则来处理请求和响应
            # verify if we have a valid buffer_id, if yes avoid to send both
            # flow_mod & packet_out
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match1, actions1, hard_timeout=5, buffer_id=msg.buffer_id)
                self.add_flow(datapath, 1, match2, actions2, hard_timeout=5, buffer_id=msg.buffer_id)
                return
            else:
                self.add_flow(datapath, 1, match1, actions1, hard_timeout=5)
                self.add_flow(datapath, 1, match2, actions2, hard_timeout=5)
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data   # 如果没有缓冲区 ID，则直接使用消息中的数据。
        # if not self.is_link_port(dpid, in_port):
        #    self.logger.info("_switch_packet_in_handle忽略非交换机链路端口的数据包: dpid=%s, in_port=%s", dpid, in_port)
        #    return
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions1, data=data)   # 创建数据包输出消息
        datapath.send_msg(out)   # 将构建好的 OpenFlow 消息（在这里是 OFPPacketOut 消息）发送到指定的交换机
#_lldp_packet_in_handle 和 _host_arp_packet_in_handle 方法：分别处理LLDP和ARP数据包，用于发现链路和主机信息。
    # 发现主机，存主机信息（存host_to_sw_port里）
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)  # 记录主机的MAC地址、IP地址以及对应的输入端口，控制器可以逐步构建网络的主机信息数据库
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
            
            if not self.get_port(dpid, in_port):  # 交换机是否有此端口
                return

            # 关键：检查是否是交换机间的链路端口
            
            for link in self.topo_access_link.keys():
                if dpid == link[0] and in_port == self.topo_access_link[link][0]:
                # 更新时间戳信息
                    self.topo_access_link[link][1] = time.time()  # 更新链路的时间戳信息
                    self.logger.info("交换机 %s 的链接端口 %s 收到链路保活ARP 包: src_mac=%s, src_ip=%s", dpid, in_port, src_mac, src_ip)
                    # return  # 注意这里的return被注释掉了

            # 打印主机信息
            # self.logger.info("55555555检查主机是否已经存在于其他位置前主机信息: src_mac=%s, src_ip=%s, dpid=%s, in_port=%s", src_mac, src_ip, dpid, in_port)   

            # 检查主机是否已经存在于其他位置
            self._check_host_migration(src_mac, src_ip, dpid, in_port)
            
            # 更新主机信息
            host_mac = src_mac
            host = Host(host_mac, in_port, src_ip)
            self.host_to_sw_port.setdefault(dpid, {})
            self.host_to_sw_port[dpid][in_port] = [host.mac, host.ipv4]   # 更新主机到交换机的端口映射

            # 再次打印主机信息
            # self.logger.info("6666666更新后的主机信息: src_mac=%s, src_ip=%s, dpid=%s, in_port=%s", src_mac, src_ip, dpid, in_port)

    def _check_host_migration(self, mac, ip, new_dpid, new_port):
        """
        检查主机是否已经迁移，如果是，则删除旧的链路信息
        
        Args:
            mac: 主机MAC地址
            ip: 主机IP地址
            new_dpid: 新的交换机ID
            new_port: 新的端口号
        """
        # 遍历所有交换机和端口，查找该主机的旧位置
        for sw_id in list(self.host_to_sw_port.keys()):
            for port in list(self.host_to_sw_port.get(sw_id, {}).keys()):
                host_info = self.host_to_sw_port[sw_id].get(port)
                
                # 如果找到相同MAC地址的主机，但位置不同
                if host_info and host_info[0] == mac:
                    # 如果是同一个交换机的不同端口，或者不同交换机
                    if sw_id != new_dpid or port != new_port:
                        self.logger.info("主机迁移: MAC=%s, IP=%s 从交换机=%s,端口=%s 迁移到 交换机=%s,端口=%s",
                                        mac, ip, sw_id, port, new_dpid, new_port)
                        
                        # 删除旧的主机信息
                        del self.host_to_sw_port[sw_id][port]
                        
                        # 如果交换机没有连接任何主机，清理该交换机的条目
                        if not self.host_to_sw_port[sw_id]:
                            del self.host_to_sw_port[sw_id]
                        
                        # 清理相关的MAC到端口映射
                        if sw_id in self.mac_to_port and mac in self.mac_to_port[sw_id]:
                            del self.mac_to_port[sw_id][mac]
                        
                        # 清理相关的ARP表条目
                        for key in list(self.arp_table.keys()):
                            if key[0] == sw_id and key[1] == mac:
                                del self.arp_table[key]
                        
                        return  # 找到并处理了迁移，退出函数

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)  # 处理接收到的 IP 数据包，通过解析并判断数据包的目的地，决定是直接安装流表项进行转发，还是请求路由信息
    def _host_ip_packet_in_handle(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match['in_port']
        eth, pkt_type, pkt_data = ethernet.ethernet.parser(msg.data)
        src_mac = eth.src
        dst_mac = eth.dst
        # self.logger.info("000000_host_ip_packet_in_handle收到 IP 数据包: 源 IP=%s, 目标 IP=%s, 源 MAC=%s, 目标 MAC=%s, 交换机=%s, 端口=%s", 
                    # src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)
        if eth.ethertype == ether_types.ETH_TYPE_IP:
            pkt, _, _ = pkt_type.parser(pkt_data)
            src_ip = pkt.src
            dst_ip = pkt.dst
            
            self.logger.info("11_host_ip_packet_in_handle收到IP数据包: 源IP=%s,目标IP=%s,源MAC=%s,目标MAC=%s,交换机=%s,端口=%s", 
                                src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)
            
            
            #关键：检查是否是交换机间的链路端口
            # if self.is_link_port(dpid, in_port):
                # self.logger.info("_host_ip_packet_in_handle忽略交换机链路端口的数据包: dpid=%s, in_port=%s", dpid, in_port)
                # return
                
            # 直接查找目标IP对应的交换机和端口
            dst_switch_id = None
            dst_port = None
            dst_mac_addr = None
            
            for sw_id in self.host_to_sw_port:
                for port in self.host_to_sw_port[sw_id]:
                    host_info = self.host_to_sw_port[sw_id][port]
                    if host_info[1] == dst_ip:
                        dst_switch_id = sw_id
                        dst_port = port
                        dst_mac_addr = host_info[0]
                        self.logger.info("【找到】目标主机信息: 交换机=%s, 端口=%s", 
                                       dst_switch_id, dst_port)
                        break
                if dst_switch_id:
                    break
            
            if not dst_switch_id:
                if self.is_connected:  # 确保与server_agent连接正常
                    # self.logger.info("【转发】目标IP %s 不在本地拓扑中，请求server_agent处理", dst_ip)
                    self._request_path(src_ip, dst_ip, dpid, in_port, msg)
                    return
                else:
                    self.logger.error("未连接到server_agent，无法处理跨域请求")
                    return
            
            # 源交换机ID
            src_switch_id = dpid
            
            # 特殊处理：如果源交换机和目标交换机是同一个
            if src_switch_id == dst_switch_id:
                self.logger.info("【处理】源主机和目标主机在同一个交换机上: dpid=%s", dpid)
                
                if not dst_port or not dst_mac_addr:
                     return
                
                # 创建流表项 - 正向流表
                datapath = self.dpid_to_switch[dpid]
                actions = [datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac_addr),
                          datapath.ofproto_parser.OFPActionOutput(dst_port)]
                match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                       in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
                
                self.add_flow(datapath, 1, match, actions)
                self.logger.info("【流表安装】正向流表: match=%s, actions=%s", match, actions)
                
                # 创建流表项 - 反向流表
                actions_reverse = [datapath.ofproto_parser.OFPActionSetField(eth_dst=src_mac),
                                  datapath.ofproto_parser.OFPActionOutput(in_port)]
                match_reverse = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                               in_port=dst_port, ipv4_dst=src_ip, ipv4_src=dst_ip)
                
                self.add_flow(datapath, 1, match_reverse, actions_reverse)
                self.logger.info("【流表安装】反向流表: match_reverse=%s, actions_reverse=%s", match_reverse, actions_reverse)

                
                # 发送当前数据包
                self.send_packet_to_outport(datapath, msg, in_port, actions)
                self.logger.info("【成功】同一交换机流表安装完成: %s <-> %s", src_ip, dst_ip)
                return
            
            # 正常处理：源交换机和目标交换机不同
            self.logger.info("【计算】路径: 源交换机=%s, 目标交换机=%s", src_switch_id, dst_switch_id)
            path = self.get_path(src_switch_id, dst_switch_id)
            
            if path and len(path) > 0:
                self.logger.info("【成功】找到路径: %s -> %s, 路径: %s", src_ip, dst_ip, path)
                self.install_flow_entry(path, src_ip, dst_ip, in_port, msg)
            else:
                self.logger.info("【尝试】未找到最短路径，使用直接路径")
                # 尝试直接安装流表，即使没有找到路径
                direct_path = [src_switch_id, dst_switch_id]
                self.install_flow_entry(direct_path, src_ip, dst_ip, in_port, msg)

    def _update_link_loss_rate(self, dpid, port_no, loss_rate):
        """
        更新链路的丢包率
        :param dpid: 交换机ID
        :param port_no: 端口号
        :param loss_rate: 丢包率
        """
        # 更新内部链路丢包率
        for link in self.topo_inter_link:
            if link[0] == dpid and self.topo_inter_link[link][0] == port_no:
                self.topo_inter_link[link][4] = loss_rate
                # 更新图中的丢包率信息
                if link[0] in self.graph and link[1] in self.graph[link[0]]:
                    self.graph[link[0]][link[1]]['loss_rate'] = loss_rate
                break

        # 更新接入链路丢包率
        for link in self.topo_access_link:
            if link[0] == dpid and self.topo_access_link[link][0] == port_no:
                self.topo_access_link[link][4] = loss_rate
                # 更新图中的丢包率信息
                if link[0] in self.graph and link[1] in self.graph[link[0]]:
                    self.graph[link[0]][link[1]]['loss_rate'] = loss_rate
                break

    def _connect_to_server(self):
        """连接到server_agent的方法"""
        while True:
            try:
                if not self.is_connected:
                    self.logger.info("尝试连接到server_agent...")
                    self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    self.server_socket.connect(self.server_addr)
                    self.is_connected = True
                    self.logger.info("成功连接到server_agent")
                    
                    # 启动接收消息的线程
                    hub.spawn(self._receive_from_server)
            except Exception as e:
                self.logger.error(f"连接server_agent失败: {e}")
                if self.server_socket:
                    self.server_socket.close()
                self.is_connected = False
            hub.sleep(SERVER_CONFIG['reconnect_interval'])

    def _send_topo_loop(self):
        """定期发送拓扑信息到server"""
        while True:
            if self.is_connected:
                try:
                    self.logger.info("准备发送拓扑信息到server_agent")
                    # 构建主机信息
                    host_info = []
                    for dpid, ports in self.host_to_sw_port.items():
                        for port, host in ports.items():
                            host_info.append({
                                'dpid': dpid,
                                'port': port,
                                'mac': host[0],
                                'ip': host[1]
                            })
                    self.logger.info(f"主机信息: {host_info}")

                    # 构建链路信息
                    link_info = [{'src': link[0], 
                                 'dst': link[1], 
                                 'src_port': self.topo_access_link[link][0],
                                 'delay': self.topo_access_link[link][2],
                                 'bw': self.topo_access_link[link][3],
                                 'loss': self.topo_access_link[link][4]
                                } for link in self.topo_access_link.keys()]
                    # link_info = [{'src': link[0], 
                                #  'dst': link[1], 
                                #  'src_port': self.topo_inter_link[link][0],
                                #  'delay': self.topo_inter_link[link][2],
                                #  'bw': self.topo_inter_link[link][3],
                                #  'loss': self.topo_inter_link[link][4]
                                # } for link in self.topo_inter_link.keys()]
                    self.logger.info(f"链路信息: {link_info}")

                    # 构建拓扑信息
                    topo_msg = {
                        "type": "topo",
                        "switches": list(self.dpid_to_switch.keys()),
                        "link": link_info,
                        "host": host_info
                    }
                    self.logger.info("发送拓扑信息到server_agent")
                    self._send_to_server(topo_msg)
                except Exception as e:
                    self.logger.error(f"发送拓扑信息失败: {e}")
            hub.sleep(10)

    def _send_to_server(self, msg):
        """发送消息到server"""
        if self.is_connected:
            try:
                data = json.dumps(msg)
                self.server_socket.sendall(data.encode())
            except Exception as e:
                self.logger.error(f"发送失败: {e}")
                self.is_connected = False
                if self.server_socket:
                    self.server_socket.close()

    def _receive_from_server(self):
        """接收server消息的循环"""
        while self.is_connected:
            try:
                data = self.server_socket.recv(4096)
                if not data:
                    break
                
                # 打印接收到的原始数据
                # self.logger.info(f"接收到的数据: {data.decode()}")
                
                # 尝试解析多个 JSON 对象
                messages = data.decode().strip().split('}')  # 按 '}' 分割
                for message in messages:
                    if message.strip():  # 确保不是空字符串
                        message += '}'  # 重新添加 '}' 以形成完整的 JSON 对象
                        try:
                            msg = json.loads(message)
                            self._handle_server_msg(msg)
                        except json.JSONDecodeError as json_err:
                            self.logger.error(f"JSON解析失败: {json_err}，接收到的数据: {message}")
                            break
            except Exception as e:
                self.logger.error(f"接收失败: {e}")
                break
        
        self.is_connected = False
        if self.server_socket:
            self.server_socket.close()

    def _handle_server_msg(self, msg):
        """处理从server收到的消息"""
        # self.logger.info(f"收到server消息: {msg}")
        
        if msg.get('status') == 'ok' and 'path' in msg:
            path = msg['path']
            if path:
                self.logger.info(f"收到路径: {path}")
                # 处理路径信息
                self._process_path(path)

    def _process_path(self, path):
        """处理路径信息"""
        # 这里需要根据实际情况实现路径处理逻辑
        pass

    def _request_path(self, src_ip, dst_ip, dpid, in_port, msg):
        """请求路径计算"""
        path_msg = {
            "type": "path_request",
            "src": src_ip,
            "dst": dst_ip,
            "switch_id": dpid,
            "in_port": in_port
        }
        self._send_to_server(path_msg)
        # self.logger.info(f"已发送路径请求: {src_ip} -> {dst_ip}")




