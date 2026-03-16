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
from ryu.lib.packet import ethernet, ether_types, arp, packet, lldp
from ryu.lib import hub
from ryu.topology import event
from ryu.topology.switches import LLDPPacket
from ryu.base.app_manager import lookup_service_brick
import networkx as nx
from operator import attrgetter

Initial_bandwidth = 800

# 配置日志：同时输出到控制台和本地文件 controller.log
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),  # 输出到控制台
        logging.FileHandler("./controller.log", mode='w', encoding='utf-8'),  # 输出到文件
    ]
)
logger = logging.getLogger("server_agent")

# 添加server配置
SERVER_CONFIG = {
    'server_ip': '10.5.1.163',  # 修改为server_agent的实际IP
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
        # 【新增】专门记录本端的域间接入端口集合 {(dpid, port), ...}
        # 因为移除了反向链路，我们需要这个集合来判断端口是否是链路端口
        self.access_ports = set()
        # self.detection_access_link = {}  # 带有时间戳的外部链路信息，用于超时检测，超时检测后被赋值给真正的外部链路
        # self.detection_inter_link = {}

        # calculate delay
        self.echo_timestamp = {} # {dpid:recvtime,1:0.5,2:0.3,....}控制器收到交换机echo回复的时间戳，根据时间是否超过30秒交换机是否断开连接
        self.echo_latency = {}  # {dpid:delaytime,1:0.5,2:0.3,....}每个交换机与控制器之间的Echo时延
        self.lldp_delay = {}  # {(src,dst):time,(1,2):0.5,...}
        self.link_delay = {}  # {(src,dst):time,(1,2):0.5,....}交换机之间链路的延迟时间
        
        # 用于存储待处理的PortData查询请求（等待server响应）
        # key: (src_dpid, src_port_no, dst_dpid), value: (接收LLDP包的时间戳, 查询请求时间戳)
        self.pending_portdata_queries = {}

        # calculate bw
        self.port_stats = {}
        self.free_bandwidth = {}  # {dpid: {port_no: (free_bandwidth, usage), ...}, ...} (Mbit/s),每个交换机端口的带宽使用情况
        self.port_loss_stats = {}  # 新增的端口丢包统计字典

        ###########
        self.mac_to_port = {}
        self.arp_table = {}  # ARP表{ (dpid, src_mac, dst_ip):in_port }

        self.graph = nx.DiGraph()  # graph用于存储网络拓扑的图结构,用 networkx 库中的 DiGraph 类创建了一个有向图。
        

        #各种标志位的开关
        self.show_enable = True  # 控制show方法的开关，True为开启，False为关闭
        self.host_migration_log_enable = True  # 控制主机迁移相关日志的开关
        self.ip_packet_log_enable = False  # 控制IP数据包日志的开关
        
        # 获取switches实例，用于访问PortData中的时间戳和echo延迟
        self.switches = None

        self.update_thread = hub.spawn(self.link_timeout_detection, self.topo_access_link)
        self.measure_thread = hub.spawn(self._detector)   # 启动一个线程，定期执行网络指标的测量任务
        self.monitor_thread = hub.spawn(self._monitor_thread)  # 启动一个线程，定期执行网络监控任务(未启用)

        self.show_info = hub.spawn(self.show)   
        self.check_switch_thread = hub.spawn(self._check_switch_state, self.echo_timestamp)
        self.get_mac_thread = hub.spawn(self.get_local_mac_address)
        self.cleanup_host_thread = hub.spawn(self._cleanup_invalid_hosts)  # 定期清理错误学习的主机

        # ========== IP 白名单配置 ==========
        # 使用 CIDR 网段限制允许学习的主机 IP，防止物理网络噪声干扰
        import ipaddress
        self.allowed_networks = [
            ipaddress.ip_network('10.0.0.0/16'),       # Mininet / 内部 10.x.x.x
            ipaddress.ip_network('172.16.0.0/12'),    # 172.16.0.0 - 172.31.255.255
            ipaddress.ip_network('192.168.0.0/16'),   # 192.168.x.x
        ]
        logger.info(f"允许学习的IP网段: {[str(net) for net in self.allowed_networks]}")

        # 添加server连接相关的属性
        self.server_socket = None
        self.is_connected = False
        self.server_addr = (SERVER_CONFIG['server_ip'], SERVER_CONFIG['server_port'])
        
        # 启动server连接线程
        self.connect_thread = hub.spawn(self._connect_to_server)
        self.topo_update_thread = hub.spawn(self._send_topo_loop)
        self.heartbeat_thread = hub.spawn(self._heartbeat_loop)
        # 添加主机学习延迟缓冲区
        self.pending_host_learning = {}  # {(dpid, port, mac, ip): first_seen_time}
        self.host_learning_delay = 3  # 延迟3秒学习,等待LLDP确认后学习
        self.cleanup_pending_thread = hub.spawn(self._cleanup_pending_host_learning)  # 定期清理待学习缓冲区
        

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

    def is_allowed_ip(self, ip_str):
        """
        检查IP是否在允许的网段内，用于过滤物理网络噪声产生的主机学习。
        """
        if not ip_str or ip_str == "0.0.0.0":
            return False

        try:
            import ipaddress
            ip = ipaddress.ip_address(ip_str)

            for network in self.allowed_networks:
                if ip in network:
                    return True

            # 不在任何允许网段内
            self.logger.warning("【IP过滤】拒绝学习非法IP: %s", ip_str)
            return False
        except ValueError as e:
            self.logger.error("【IP过滤】无效的IP地址: %s, 错误: %s", ip_str, e)
            return False

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
        # 1. 域内链路 (保持不变)
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

        # 2. 域间链路 (修改：使用本端端口查询)
        link_to_port = self.topo_access_link
        for link in link_to_port.keys():
            (src_dpid, dst_dpid) = link
            link_info = link_to_port[link]

            try:
                # 判断链路格式：
                # - 正向链路 (Remote->Local): 6个元素，index 5 是 LocalPort
                # - 反向链路 (Local->Remote): 5个元素，index 0 是 LocalPort（因为 Local 是源）
                if len(link_info) > 5:
                    # 正向链路：使用本端端口查询
                    local_port = link_info[5]
                    if dst_dpid in free_bandwidth and local_port in free_bandwidth[dst_dpid]:
                        local_free_bandwidth, _ = free_bandwidth[dst_dpid][local_port]
                        self.topo_access_link[link][3] = local_free_bandwidth
                        self.graph[src_dpid][dst_dpid]['free_bandwith'] = local_free_bandwidth
                else:
                    # 反向链路：使用源端口查询（Local 是源，有统计数据）
                    local_port = link_info[0]
                    if src_dpid in free_bandwidth and local_port in free_bandwidth[src_dpid]:
                        local_free_bandwidth, _ = free_bandwidth[src_dpid][local_port]
                        self.topo_access_link[link][3] = local_free_bandwidth
                        self.graph[src_dpid][dst_dpid]['free_bandwith'] = local_free_bandwidth
            except Exception as e:
                # self.logger.debug("计算域间带宽出错: %s", e)
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
            为双向链路添加延迟信息
            Create link delay data, and save it into graph object.
        """
        # 域内链路延迟
        for link in list(self.topo_inter_link.keys()):
            (src_dpid, dst_dpid) = link
            try:
                delay = self._get_delay(src_dpid, dst_dpid)
                self.topo_inter_link[(src_dpid, dst_dpid)][2] = delay
                if self.graph.has_edge(src_dpid, dst_dpid):
                    self.graph[src_dpid][dst_dpid]['delay'] = delay
            except Exception as e:
                self.logger.debug("计算域内链路延迟失败: %s -> %s, error=%s", src_dpid, dst_dpid, e)

        # 域间链路延迟
        for link in list(self.topo_access_link.keys()):
            (local_dpid, remote_dpid) = link
            # topo_access_link的键格式: (local_dpid, remote_dpid)
            # 其中local_dpid是本域交换机，remote_dpid是其他域交换机
            try:
                # 调用_get_access_delay时，参数顺序是(本域交换机, 其他域交换机)
                delay = self._get_access_delay(local_dpid, remote_dpid)
                self.topo_access_link[(local_dpid, remote_dpid)][2] = delay
                if self.graph.has_edge(local_dpid, remote_dpid):
                    self.graph[local_dpid][remote_dpid]['delay'] = delay
            except Exception as e:
                self.logger.debug("计算域间链路延迟失败: %s -> %s, error=%s", local_dpid, remote_dpid, e)

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
            # 检查lldp_delay字典中是否有该链路的延迟信息
            if (src, dst) not in self.lldp_delay:
                self.logger.debug("链路 (%s, %s) 没有LLDP延迟信息", src, dst)
                return float(0)
            
            fwd_delay = self.lldp_delay[(src, dst)][0]
            
            # 检查echo_latency字典中是否有源交换机的echo延迟
            if src not in self.echo_latency:
                self.logger.debug("交换机 %s 没有echo延迟信息", src)
                return float(0)
            
            src_latency = self.echo_latency[src]
            dst_latency = self.lldp_delay[(src, dst)][1]

            delay = fwd_delay - (src_latency + dst_latency) / 2
            # print(f"Calculating inter delay: fwd={fwd_delay:.12f}    src_lat={src_latency:.12f}  dst_lat={dst_latency:.12f}  delay={delay:.12f}")
            return max(delay, 0)
        except KeyError as e:
            self.logger.debug("计算延迟时缺少键: %s, src=%s, dst=%s", e, src, dst)
            return float(0)
        except Exception as e:
            self.logger.debug("计算延迟时发生异常: %s, src=%s, dst=%s", e, src, dst)
            return float(0)
    # 计算接入链路的延迟
    def _get_access_delay(self, src, dst):
        """
            Get link delay for inter-domain links.
            
            域间链路的情况：
            - src: 本域交换机（dst_dpid）
            - dst: 其他域交换机（src_dpid）
            
            topo_access_link存储格式: (dst_dpid, src_dpid) = [port, timestamp, delay, bw, loss]
            其中dst_dpid是本域交换机，src_dpid是其他域交换机
            
            lldp_delay存储格式: (dst_dpid, src_dpid) = [lldpdelay, echodelay]
            其中lldpdelay是LLDP包从src_dpid到dst_dpid的延迟
            echodelay是src_dpid（其他域交换机）的echo延迟
            
                   ControllerA                        ControllerB
                        |                                 |
        dst echo latency|                                 |src echo latency (其他域)
                        |                                 |
                   SwitchA (dst)----------------------SwitchB (src)
                                <----forward delay
        """
        try:
            # 注意：topo_access_link的键是(dst_dpid, src_dpid)，其中dst是本域，src是其他域
            # 但调用时传入的是(src, dst)，需要检查两个方向
            # 检查lldp_delay字典中是否有该链路的延迟信息
            # 尝试(src, dst)和(dst, src)两个方向
            if (src, dst) in self.lldp_delay:
                fwd_delay = self.lldp_delay[(src, dst)][0]
                src_echodelay = self.lldp_delay[(src, dst)][1]  # 源交换机（其他域）的echo延迟
            elif (dst, src) in self.lldp_delay:
                # 如果存储的是反向，需要调整
                fwd_delay = self.lldp_delay[(dst, src)][0]
                src_echodelay = self.lldp_delay[(dst, src)][1]
            else:
                self.logger.debug("接入链路 (%s, %s) 没有LLDP延迟信息", src, dst)
                return float('inf')
            
            # src是本域交换机，可以获取其echo延迟
            # dst是其他域交换机，echo延迟从lldp_delay中获取
            if src not in self.echo_latency:
                self.logger.debug("本域交换机 %s 没有echo延迟信息（接入链路）", src)
                return float('inf')
            
            src_latency = self.echo_latency[src]  # 本域交换机的echo延迟
            dst_latency = src_echodelay  # 其他域交换机的echo延迟（从LLDP包中获取）
            
            # 计算实际链路延迟
            # fwd_delay是LLDP包从其他域交换机到本域交换机的总延迟
            # 需要减去两个交换机的echo延迟
            delay = fwd_delay - (src_latency + dst_latency) / 2
            self.logger.debug("计算接入链路延迟: src=%s, dst=%s, fwd_delay=%.6f, src_lat=%.6f, dst_lat=%.6f, delay=%.6f",
                            src, dst, fwd_delay, src_latency, dst_latency, delay)
            return max(delay, 0)
        except KeyError as e:
            self.logger.debug("计算接入链路延迟时缺少键: %s, src=%s, dst=%s", e, src, dst)
            return float('inf')
        except Exception as e:
            self.logger.debug("计算接入链路延迟时发生异常: %s, src=%s, dst=%s", e, src, dst)
            return float('inf')

    def _save_lldp_delay(self, src=0, dst=0, lldpdelay=0, echodelay=0):
        self.lldp_delay[(src, dst)] = [lldpdelay, echodelay]
    
    def _send_lldp_report_to_server(self, src_dpid, src_port_no, dst_dpid, dst_inport,
                                    send_time, echodelay_src, receive_time):
        """
        将LLDP探测信息上报给根控制器，由根控制器统一计算链路时延。
        
        Args:
            src_dpid: 发送LLDP包的交换机ID
            src_port_no: 发送LLDP包的端口号
            dst_dpid: 接收LLDP包的交换机ID（本域）
            dst_inport: 接收端口
            send_time: LLDP包的发送时间戳（来自发送端PortData）
            echodelay_src: 发送端交换机的echo时延
            receive_time: 本域接收LLDP包的时间戳
        """
        if not self.is_connected:
            self.logger.warning("未连接到server_agent，无法上报LLDP信息")
            return
        
        dst_echo = self.echo_latency.get(dst_dpid, 0.0)
        report_msg = {
            "type": "lldp_report",
            "src_dpid": src_dpid,
            "src_port_no": src_port_no,
            "dst_dpid": dst_dpid,
            "dst_inport": dst_inport,
            "send_time": send_time,
            "receive_time": receive_time,
            "src_echo": echodelay_src,
            "dst_echo": dst_echo
        }
        self.logger.debug("上报LLDP信息给server_agent: %s", report_msg)
        self._send_to_server(report_msg)
    
    def _handle_portdata_query(self, query_msg):
        """
        处理来自其他控制器的PortData查询请求
        
        Args:
            query_msg: 查询消息，包含src_dpid和src_port_no
        """
        src_dpid = query_msg.get('src_dpid')
        src_port_no = query_msg.get('src_port_no')
        request_id = query_msg.get('request_id')
        
        self.logger.debug("收到PortData查询请求: src_dpid=%s, src_port_no=%s", src_dpid, src_port_no)
        
        # 从switches实例的ports中查找PortData
        timestamp = None
        echodelay = 0.0
        
        if self.switches is not None:
            for port_obj in self.switches.ports.keys():
                if src_dpid == port_obj.dpid and src_port_no == port_obj.port_no:
                    port_data = self.switches.ports[port_obj]
                    timestamp = port_data.timestamp
                    echodelay = getattr(port_data, 'echo_delay', 0.0)
                    break
        
        # 构建响应消息
        response_msg = {
            "type": "portdata_response",
            "request_id": request_id,
            "src_dpid": src_dpid,
            "src_port_no": src_port_no,
            "timestamp": timestamp,
            "echodelay": echodelay,
            "status": "ok" if timestamp is not None else "not_found"
        }
        
        self.logger.debug("发送PortData查询响应: timestamp=%s, echodelay=%s", timestamp, echodelay)
        self._send_to_server(response_msg)
    
    def _handle_portdata_response(self, response_msg):
        """
        处理PortData查询响应，更新lldp_delay
        
        Args:
            response_msg: 响应消息，包含timestamp和echodelay
        """
        request_id = response_msg.get('request_id')
        src_dpid = response_msg.get('src_dpid')
        src_port_no = response_msg.get('src_port_no')
        timestamp = response_msg.get('timestamp')
        echodelay = response_msg.get('echodelay', 0.0)
        status = response_msg.get('status')
        
        # 查找对应的查询请求
        query_key = None
        for key in self.pending_portdata_queries.keys():
            if str(key) == request_id:
                query_key = key
                break
        
        if query_key is None:
            self.logger.warning("收到未匹配的PortData响应: request_id=%s", request_id)
            return
        
        # 从待处理列表中移除
        query_data = self.pending_portdata_queries.pop(query_key, None)
        if query_data is None:
            self.logger.warning("查询数据不存在: request_id=%s", request_id)
            return
        
        lldp_receive_time, query_time = query_data
        dst_dpid = query_key[2]  # (src_dpid, src_port_no, dst_dpid)
        
        if status == "ok" and timestamp is not None:
            # 计算LLDP延迟
            # lldp_receive_time是收到LLDP包的时间
            # timestamp是发送LLDP包的时间（从其他控制器的PortData获取）
            # 直接计算：LLDP延迟 = 收到LLDP包的时间 - 发送LLDP包的时间
            lldpdelay = lldp_receive_time - timestamp
            
            # 更新延迟信息
            self._save_lldp_delay(src=dst_dpid, dst=src_dpid, lldpdelay=lldpdelay, echodelay=echodelay)
            self.logger.debug("收到PortData响应并更新延迟: src=%s, dst=%s, lldpdelay=%.6f, echodelay=%.6f, "
                            "lldp_receive_time=%.6f, timestamp=%.6f",
                            src_dpid, dst_dpid, lldpdelay, echodelay, lldp_receive_time, timestamp)
        else:
            self.logger.warning("PortData查询失败: src_dpid=%s, src_port_no=%s, status=%s", 
                              src_dpid, src_port_no, status)

    def _handle_lldp_delay_update(self, response_msg):
        """
        处理根控制器返回的LLDP延迟计算结果
        """
        status = response_msg.get('status', 'ok')
        if status != 'ok':
            self.logger.warning("LLDP延迟更新失败: %s", response_msg.get('message'))
            return

        src_dpid = response_msg.get('src_dpid')
        dst_dpid = response_msg.get('dst_dpid')
        lldp_delay = response_msg.get('fwd_delay', 0.0)
        src_echo = response_msg.get('src_echo', 0.0)
        dst_echo = response_msg.get('dst_echo', 0.0)
        calc_delay = response_msg.get('delay', 0.0)

        if src_dpid is None or dst_dpid is None:
            self.logger.warning("LLDP延迟更新缺少必要字段: %s", response_msg)
            return

        # 保存原始LLDP转发时延及发送端echo，用于后续计算
        self._save_lldp_delay(src=dst_dpid, dst=src_dpid, lldpdelay=lldp_delay, echodelay=src_echo)

        # 同时更新链路计算出的实际延迟
        try:
            if (dst_dpid, src_dpid) in self.topo_access_link:
                self.topo_access_link[(dst_dpid, src_dpid)][2] = calc_delay
                self.graph[dst_dpid][src_dpid]['delay'] = calc_delay
        except Exception:
            pass

        self.logger.debug("更新LLDP延迟: src=%s, dst=%s, fwd=%.6f, src_echo=%.6f, dst_echo=%.6f, delay=%.6f",
                          src_dpid, dst_dpid, lldp_delay, src_echo, dst_echo, calc_delay)

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
        """显示当前拓扑信息"""
        while True:
            print("\n" + "="*80)
            print(f"控制器拓扑状态 - {time.strftime('%H:%M:%S')}")
            print("="*80)
            
            # 交换机
            print(f"\n【交换机】共 {len(self.dpid_to_switch)} 个")
            for dpid in self.dpid_to_switch.keys():
                print(f"  - 交换机 {dpid}")
            
            # 域内链路
            print(f"\n【域内链路 (topo_inter_link)】共 {len(self.topo_inter_link)} 条")
            if len(self.topo_inter_link) == 0:
                print("  (空)")
            for (src, dst), info in self.topo_inter_link.items():
                port, timestamp, delay, bw, loss = info
                age = time.time() - timestamp if timestamp > 0 else -1
                print(f"  - {src} -> {dst} | 端口:{port} | 延迟:{delay:.3f}ms | "
                      f"带宽:{bw:.1f}Mbps | 更新:{age:.1f}秒前")
            
            # 域间链路 (关键！)
            print(f"\n【域间链路 (topo_access_link)】共 {len(self.topo_access_link)} 条")
            if len(self.topo_access_link) == 0:
                print("  ⚠️  警告: 没有检测到域间链路!")
                print("  可能原因:")
                print("    1. LLDP包没有跨域传输")
                print("    2. _lldp_packet_in_handle 没有正确处理")
                print("    3. 端口配置有问题")
            else:
                for (local_dpid, remote_dpid), info in self.topo_access_link.items():
                    port, timestamp, delay, bw, loss = info
                    age = time.time() - timestamp
                    print(f"  - 本域{local_dpid}(端口{port}) <-> 远程域{remote_dpid} | "
                          f"延迟:{delay:.3f}ms | 带宽:{bw:.1f}Mbps | 更新:{age:.1f}秒前")
            
            # 主机
            total_hosts = sum(len(hosts) for ports in self.host_to_sw_port.values() 
                             for hosts in ports.values())
            print(f"\n【主机】共 {total_hosts} 个")
            for dpid, ports in self.host_to_sw_port.items():
                for port, hosts in ports.items():
                    for mac, ip in hosts:
                        is_link = self.is_link_port(dpid, port)
                        status = " [⚠️链路端口-应该删除]" if is_link else ""
                        print(f"  - 交换机{dpid}:端口{port} -> MAC:{mac}, IP:{ip}{status}")
            
            print("\n" + "="*80)
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
    def is_link_port(self, dpid, port):
        """
        检查指定端口是否是链路端口
        """
        # 1. 检查是否是域内互联端口
        for link in self.topo_inter_link.keys():
            if dpid == link[0] and port == self.topo_inter_link[link][0]:
                return True
        
        # 2. 检查是否是域间接入端口 (使用新集合判断)
        if (dpid, port) in self.access_ports:
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
    
    def del_flow(self, datapath):
        """辅助函数：清空流表"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
        )
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
    def get_switch_id_by_ip(self, ip_address):
        for switch_id in self.host_to_sw_port:
            for port in self.host_to_sw_port[switch_id]:
                for host in self.host_to_sw_port[switch_id][port]:
                    if host[1] == ip_address:
                        return switch_id
    
    def get_switch_port_by_ip(self, ip_address):
        for switch_id in self.host_to_sw_port:
            for port in self.host_to_sw_port[switch_id]:
                for host in self.host_to_sw_port[switch_id][port]:
                    if host[1] == ip_address:
                        return port
    
    def get_mac_by_ip(self, ip_address):
        for switch_id in self.host_to_sw_port:
            for port in self.host_to_sw_port[switch_id]:
                for host in self.host_to_sw_port[switch_id][port]:
                    if host[1] == ip_address:
                        return host[0]
    # 检查每个交换机的每个端口连接的主机IP地址，如果找到匹配的IP地址，则返回对应的交换机ID
    # def get_switch_id_by_ip(self, ip_address):
        # sw = self.host_to_sw_port.keys()
        # for switch_id in sw:
            # for port in self.host_to_sw_port[switch_id].keys():
                # if ip_address in self.host_to_sw_port[switch_id][port]:   # 指定的IP地址（主机的IP地址）是否与某个交换机的特定端口连接的主机相关联。
                    # return switch_id

    # def get_switch_port_by_ip(self, ip_address):  # 通过目的IP地址找到与之关联的交换机端口
        # sw = self.host_to_sw_port.keys()
        # for switch_id in sw:
            # for port in self.host_to_sw_port[switch_id].keys():
                # if ip_address in self.host_to_sw_port[switch_id][port]:
                    # return port

    # def get_mac_by_ip(self, ip_address):
        # sw = list(self.host_to_sw_port.keys())
        # for switch_id in sw:
            # for port in self.host_to_sw_port[switch_id].keys():
                # if ip_address in self.host_to_sw_port[switch_id][port]:
                    # return self.host_to_sw_port[switch_id][port][0]  # 根据给定的IP地址（主机的IP地址）查找并返回与该 IP 地址关联的主机的 MAC 地址

    def get_port_from_link(self, dpid, next_id):
        if (dpid, next_id) in self.topo_inter_link.keys():
            return self.topo_inter_link[(dpid, next_id)][0]  # 返回的是（当前交换机）和 next_id（下一个设备 ID）之间连接的第一个端口的信息。返回的端口属于第一个交换机
        if (dpid, next_id) in self.topo_access_link.keys():
            return self.topo_access_link[(dpid, next_id)][0]
            
    def install_flow_entry(self, path, src_ip, dst_ip, port=None, msg=None):
        """
        install flow entry 在 OpenFlow 交换机的流表中添加一条流表项
        """
        self.logger.info("【流表】开始安装流表: 路径=%s, 源IP=%s, 目标IP=%s", path, src_ip, dst_ip)
        num = len(path)
        if num == 1:  # 当路径中只有一个交换机时的流表安装和数据包转发
            dpid = path[0]
            datapath = self.dpid_to_switch[dpid]
            in_port = port
            
            # 直接查找目标IP对应的端口和MAC地址
            dst_port = None
            dst_mac_addr = None
            
            for p in self.host_to_sw_port.get(dpid, {}):
                for host_info in self.host_to_sw_port[dpid][p]:
                    if host_info[1] == dst_ip:
                        dst_port = p
                        dst_mac_addr = host_info[0]
                        break
            # for p in self.host_to_sw_port.get(dpid, {}):
                # host_info = self.host_to_sw_port[dpid][p]
                # if host_info[1] == dst_ip:
                    # dst_port = p
                    # dst_mac_addr = host_info[0]
                    # break
            
            if not dst_port or not dst_mac_addr:
                return
            
            # 获取源主机的MAC地址
            src_mac_addr = None
            for p in self.host_to_sw_port.get(dpid, {}):
                for host_info in self.host_to_sw_port[dpid][p]:
                    if host_info[1] == src_ip:
                        src_mac_addr = host_info[0]
                        break
                if src_mac_addr:
                    break
            
            if not src_mac_addr:
                # src_mac_addr = "未知"  # 如果找不到源MAC，使用默认值
                self.logger.warning("【流表】未能找到源主机MAC地址，跳过流表下发: 源IP=%s", src_ip)
                return
                           
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
                
            self.logger.info("【流表】单交换机流表安装完成: 源IP=%s, 目标IP=%s", src_ip, dst_ip)
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
            # 查找目标主机
            for p in self.host_to_sw_port.get(dst_dpid, {}):
                for host_info in self.host_to_sw_port[dst_dpid][p]:
                    if host_info[1] == dst_ip:
                        dst_out_port = p
                        dst_mac_addr = host_info[0]
                        break
                if dst_out_port:
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
                for host_info in self.host_to_sw_port[src_dpid][p]:
                    if host_info[1] == src_ip:
                        src_mac_addr = host_info[0]
                        break
                if src_mac_addr:
                    break
                    
            if not src_mac_addr:
                # src_mac_addr = "未知"  # 如果找不到源MAC，使用默认值
                self.logger.warning(f"未能找到源主机 {src_ip} 的MAC地址，跳过流表下发")
                return
                
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
                
            self.logger.info("【流表】两交换机流表安装完成: 源IP=%s, 目标IP=%s, 路径=%s", src_ip, dst_ip, path)
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
                            for host_info in self.host_to_sw_port[dpid][p]:
                                if host_info[1] == dst_ip:
                                    dst_port = p
                                    dst_mac_addr = host_info[0]
                                    break
                            if dst_port:
                                break
                        
                        if not dst_port or not dst_mac_addr:
                            continue
                        
                        # 查找源IP对应的MAC地址（用于反向流表）
                        src_mac_addr = None
                        first_dpid = path[0]
                        for p in self.host_to_sw_port.get(first_dpid, {}):
                            for host_info in self.host_to_sw_port[first_dpid][p]:
                                if host_info[1] == src_ip:
                                    src_mac_addr = host_info[0]
                                    break
                            if src_mac_addr:
                                break
                        
                        if not src_mac_addr:
                            self.logger.warning("【流表】未能找到源主机MAC地址，跳过反向流表: 源IP=%s", src_ip)
                        
                        # 正向流表
                        actions = [datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac_addr),
                                  datapath.ofproto_parser.OFPActionOutput(dst_port)]
                        match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                               in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
                        self.add_flow(datapath, 1, match, actions)
                        
                        # 反向流表：需要修改目标MAC为源主机MAC
                        if src_mac_addr:
                            actions_reverse = [datapath.ofproto_parser.OFPActionSetField(eth_dst=src_mac_addr),
                                              datapath.ofproto_parser.OFPActionOutput(in_port)]
                        else:
                            actions_reverse = [datapath.ofproto_parser.OFPActionOutput(in_port)]
                        match_reverse = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                                       in_port=dst_port, ipv4_dst=src_ip, ipv4_src=dst_ip)
                        self.add_flow(datapath, 1, match_reverse, actions_reverse)
            
            self.logger.info("【流表】多交换机流表安装完成: 源IP=%s, 目标IP=%s, 路径=%s", src_ip, dst_ip, path)

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

    def add_inter_link(self, link):   # 添加交换机之间的双向链路信息
        """添加交换机之间的双向链路信息"""
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no
        dst_port = link.dst.port_no  # 获取目标端口
        
        # 添加正向链路
        if (src_dpid, dst_dpid) not in self.topo_inter_link:
            self.topo_inter_link[(src_dpid, dst_dpid)] = [src_port, 0, 0, 0, 0]
            self.graph.add_edge(src_dpid, dst_dpid)
            self.logger.info("【域内链路】添加正向: %s(端口%s) -> %s", src_dpid, src_port, dst_dpid)
        
        # 添加反向链路
        if (dst_dpid, src_dpid) not in self.topo_inter_link:
            self.topo_inter_link[(dst_dpid, src_dpid)] = [dst_port, 0, 0, 0, 0]
            self.graph.add_edge(dst_dpid, src_dpid)
            self.logger.info("【域内链路】添加反向: %s(端口%s) -> %s", dst_dpid, dst_port, src_dpid)

    def delete_inter_link(self, link):
        """删除双向域内链路"""
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        
        # 删除正向链路
        if (src_dpid, dst_dpid) in self.topo_inter_link:
            del self.topo_inter_link[(src_dpid, dst_dpid)]
            if self.graph.has_edge(src_dpid, dst_dpid):
                self.graph.remove_edge(src_dpid, dst_dpid)
            self.logger.info("【域内链路】删除正向: %s -> %s", src_dpid, dst_dpid)
        
        # 删除反向链路
        if (dst_dpid, src_dpid) in self.topo_inter_link:
            del self.topo_inter_link[(dst_dpid, src_dpid)]
            if self.graph.has_edge(dst_dpid, src_dpid):
                self.graph.remove_edge(dst_dpid, src_dpid)
            self.logger.info("【域内链路】删除反向: %s -> %s", dst_dpid, src_dpid)

    def link_timeout_detection(self, access_link):  #  access_link，这是一个字典，通常用于存储链路信息，其中键是源和目标节点的元组，值是与链路相关的属性（例如时间戳）
        """
        链路超时检测，清理超时的双向链路
        用于链路超时检测，如果某条链路超过一定时间没有进行更新，就会判定该链路失效，从而删除该链路信息，同步更新对外端口信息
        """
        while True:
            link_lists = access_link
            now_timestamp = time.time()
            links_to_remove = []
            
            for (src, dst) in list(link_lists.keys()):
                if (now_timestamp - link_lists[(src, dst)][1]) > 70:  # 当前的时间戳与该链接的最后更新时间戳。
                    links_to_remove.append((src, dst))
            
            # 批量删除超时链路
            for (src, dst) in links_to_remove:
                try:
                    self.logger.info("域间交换机链路超时,删除链路: 从交换机 %s 到交换机 %s", src, dst)  # 添加打印信息
                    access_link.pop((src, dst))
                    if self.graph.has_edge(src, dst):
                        self.graph.remove_edge(src, dst)
                except Exception as e:
                    self.logger.error("删除超时链路失败: %s", e)
            hub.sleep(3)

    @set_ev_cls([event.EventSwitchEnter])
    def _switch_enter_handle(self, ev):
        switch = ev.switch
        self._add_switch_map(switch)  # 将新加入的交换机信息添加到应用内部的数据结构中

    @set_ev_cls([event.EventSwitchReconnected])
    def _switch_reconnected_handle(self, ev):
        self.logger.info("【交换机】交换机重连: DPID=%016x", ev.switch.dp.id)
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
        """交换机连接时下发流表，确保 LLDP 只上报不转发"""
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        # 1. 极其重要：先清空交换机残留的旧流表
        # 这能防止 Mininet 重启后残留的 FLOOD 规则导致 LLDP 乱飞
        self.del_flow(datapath)
        
        # 2. 安装默认流表 (Table-miss)
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        
        # 3. 【核心修复】安装 LLDP 专用拦截流表
        # 优先级设为最高 (65535)
        # Action 只有 OFPP_CONTROLLER，这代表"拦截"
        match_lldp = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_LLDP)
        actions_lldp = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 65535, match_lldp, actions_lldp)
        
        self.logger.info("初始化交换机 %016x: 已清理流表并安装 LLDP 拦截规则", datapath.id)

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
    
    def _dpid_to_int(self, dpid_val):
        """
        辅助函数：确保 dpid 是整数
        处理 bytes、str、int 等不同类型的 dpid
        """
        if isinstance(dpid_val, int):
            return dpid_val
        if isinstance(dpid_val, bytes):
            import struct
            # 处理8字节的dpid
            if len(dpid_val) == 8:
                return struct.unpack('!Q', dpid_val)[0]
            # 处理其他长度，尝试转十六进制字符串再转int
            return int(dpid_val.hex(), 16)
        if isinstance(dpid_val, str):
            # 处理 '0000000000000001' 这种字符串
            return int(dpid_val, 16)
        return dpid_val

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _lldp_packet_in_handle(self, ev):
        """
        [完全修复版] 
        1. 恢复反向链路 (Local->Remote) 以修复路由和流表安装
        2. 保留正向链路 (Remote->Local) 的端口记录以修复带宽计算
        3. 兼容混合环境 LLDP 格式
        """
        msg = ev.msg
        datapath = msg.datapath
        try:
            if not msg.data:
                return
            
            pkt = packet.Packet(msg.data)
            eth = pkt.get_protocol(ethernet.ethernet)
            if not eth or eth.ethertype != ether_types.ETH_TYPE_LLDP:
                return

            lldp_pkt = pkt.get_protocol(lldp.lldp)
            if not lldp_pkt:
                return

            dst_dpid = datapath.id
            dst_port = msg.match['in_port']
            
            src_dpid_int = None
            src_port_no = None

            # --- 解析部分 (保持不变) ---
            for tlv in lldp_pkt.tlvs:
                if isinstance(tlv, lldp.ChassisID):
                    if tlv.subtype == lldp.ChassisID.SUB_MAC_ADDRESS:
                        src_dpid_int = int.from_bytes(tlv.chassis_id, 'big')
                    elif tlv.subtype == lldp.ChassisID.SUB_LOCALLY_ASSIGNED:
                        try:
                            val = tlv.chassis_id.decode('utf-8')
                            src_dpid_int = int(val.split(':')[1], 16) if val.startswith('dpid:') else int(val, 16)
                        except:
                            src_dpid_int = int.from_bytes(tlv.chassis_id, 'big')

                elif isinstance(tlv, lldp.PortID):
                    try:
                        if tlv.subtype == lldp.PortID.SUB_PORT_COMPONENT:
                            src_port_no = int.from_bytes(tlv.port_id, 'big')
                        else:
                            if isinstance(tlv.port_id, bytes):
                                try:
                                    src_port_no = int(tlv.port_id.decode('utf-8'))
                                except:
                                    src_port_no = int.from_bytes(tlv.port_id, 'big')
                            else:
                                src_port_no = int(tlv.port_id)
                    except:
                        pass

            # --- 处理逻辑 ---
            if src_dpid_int is not None and src_port_no is not None:
                is_local = src_dpid_int in self.dpid_to_switch
                
                if not is_local:
                    # 1. 冲突检测
                    for link in self.topo_inter_link:
                        if link[0] == dst_dpid and self.topo_inter_link[link][0] == dst_port:
                            return

                    # 2. 记录接入端口 (用于 is_link_port)
                    self.access_ports.add((dst_dpid, dst_port))
                    
                    now_time = time.time()

                    # 3. 添加正向链路 (Remote -> Local)
                    fwd_key = (src_dpid_int, dst_dpid)
                    if fwd_key not in self.topo_access_link:
                        # 格式: [RemotePort, time, delay, bw, loss, LocalPort]
                        # 记录 LocalPort 是为了计算这个方向的带宽 (因为没有 Remote 端数据)
                        self.topo_access_link[fwd_key] = [src_port_no, now_time, 0, 0, 0, dst_port]
                        self.graph.add_edge(src_dpid_int, dst_dpid)
                        self.logger.info("【Link】发现域间链路(Rx): %s -> %s", src_dpid_int, dst_dpid)
                    else:
                        self.topo_access_link[fwd_key][1] = now_time
                        # 补全数据结构防越界
                        if len(self.topo_access_link[fwd_key]) < 6:
                            self.topo_access_link[fwd_key].append(dst_port)

                    # 4. 【关键】添加反向链路 (Local -> Remote)
                    # 这是修复通信的关键！有了它，get_port_from_link 才能工作。
                    rev_key = (dst_dpid, src_dpid_int)
                    if rev_key not in self.topo_access_link:
                        # 格式: [LocalPort, time, delay, bw, loss]
                        # 这个方向的带宽计算原生支持 (因为 Local 是源)，不需要特殊处理
                        self.topo_access_link[rev_key] = [dst_port, now_time, 0, 0, 0]
                        self.graph.add_edge(dst_dpid, src_dpid_int)
                        self.logger.info("【Link】添加反向链路(Tx): %s -> %s", dst_dpid, src_dpid_int)
                        
                        # 链路发现时清理误学主机
                        if dst_dpid in self.host_to_sw_port and dst_port in self.host_to_sw_port[dst_dpid]:
                            del self.host_to_sw_port[dst_dpid][dst_port]
                    else:
                        self.topo_access_link[rev_key][1] = now_time

                    self._send_lldp_report_to_server(
                        src_dpid=src_dpid_int,
                        src_port_no=src_port_no,
                        dst_dpid=dst_dpid,
                        dst_inport=dst_port,
                        send_time=now_time,
                        echodelay_src=0.0,
                        receive_time=now_time
                    )

        except Exception:
            pass

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

         # 过滤掉无效IP
        if src_ip == "0.0.0.0":
            self.logger.info("过滤掉IP为0.0.0.0的主机: MAC=%s, 端口=%s", src_mac, in_port)
            return

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

        # 只在开启时打印IP数据包日志
        if self.ip_packet_log_enable and eth.ethertype == ether_types.ETH_TYPE_IP:
            self.logger.info("00_switch_packet_in_handle收到数据包:源IP=%s,目标IP=%s,源MAC=%s,目标MAC=%s,交换机=%s,端口=%s",   
                                src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)

        self.arp_table[(dpid, src_mac, dst_ip)] = in_port
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid].setdefault(src_mac, set())
        self.mac_to_port[dpid][src_mac].add(in_port)
        # self.mac_to_port[dpid][src_mac] = in_port  # src_mac发起网络请求的主机的MAC 地址

        if dst_mac in self.mac_to_port[dpid]:  # 这里的mac_to_port与851行中的mac_to_port存放的MAC地址和port的映射不一样
            out_port = self.mac_to_port[dpid][dst_mac]
            # 以前 out_port 是单个端口
            # 现在 out_port 是集合/列表，需要遍历
            for out_port in self.mac_to_port[dpid][dst_mac]:
                # 针对每个端口下发动作 # 如果存在，说明控制器已经记录了目标 MAC 地址
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
            # for out_port in self.mac_to_port[dpid][dst_mac]:   这里遍历其实没有意义，因为泛洪只需要一次即可
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
    # ==================== 修复后的主机学习逻辑 ====================
    
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _host_arp_packet_in_handle(self, ev):
        """
        主机发现逻辑：移除延迟，立即学习
        """
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match['in_port']
        
        try:
            pkt = packet.Packet(msg.data)
            eth = pkt.get_protocol(ethernet.ethernet)
            if not eth or eth.ethertype != ether_types.ETH_TYPE_ARP:
                return
            arp_pkt = pkt.get_protocol(arp.arp)
            src_mac, src_ip, opcode = eth.src, arp_pkt.src_ip, arp_pkt.opcode
        except:
            return
        
        if not self.is_allowed_ip(src_ip) or dpid not in self.dpid_to_switch or src_ip == "0.0.0.0":
            return
        
        if opcode not in [arp.ARP_REQUEST, arp.ARP_REPLY]:
            return
        if self.is_link_port(dpid, in_port):
            return
        
        # 【核心修复】直接学习，不再使用 pending_host_learning
        self._check_host_migration(src_mac, src_ip, dpid, in_port)
        
        self.host_to_sw_port.setdefault(dpid, {})
        self.host_to_sw_port[dpid].setdefault(in_port, [])
        hosts = self.host_to_sw_port[dpid][in_port]
        
        found = False
        for h in hosts:
            if h[0] == src_mac:
                h[1] = src_ip
                found = True
                break
        
        if not found:
            hosts.append([src_mac, src_ip])
            self.logger.info("【Host】成功学习主机: IP=%s MAC=%s @ SW=%s Port=%s", src_ip, src_mac, dpid, in_port)

    def _check_host_migration(self, mac, ip, new_dpid, new_port):
        """
        检查主机是否迁移，如果是则删除旧位置的记录
        确保MAC地址的唯一性
        """
        # 如果新位置本身是链路端口，不应该学习
        if self.is_link_port(new_dpid, new_port):
            return
        
        # 遍历所有交换机和端口，查找该MAC的旧位置
        for sw_id in list(self.host_to_sw_port.keys()):
            for port in list(self.host_to_sw_port.get(sw_id, {}).keys()):
                hosts = self.host_to_sw_port[sw_id][port]
                for h in list(hosts):
                    # 找到相同MAC的记录
                    if h[0] == mac:
                        # 如果不是同一位置，说明主机迁移了
                        if sw_id != new_dpid or port != new_port:
                            is_old_link = self.is_link_port(sw_id, port)
                            
                            if self.host_migration_log_enable:
                                if is_old_link:
                                    self.logger.warning("【删除错误学习】MAC=%s, IP=%s, 旧位置: dpid=%s, port=%s(链路端口) -> 新位置: dpid=%s, port=%s",
                                                      mac, ip, sw_id, port, new_dpid, new_port)
                                else:
                                    self.logger.info("【主机迁移】MAC=%s, IP=%s, 从 dpid=%s, port=%s -> dpid=%s, port=%s",
                                                   mac, ip, sw_id, port, new_dpid, new_port)
                            
                            # 删除旧位置的主机记录
                            hosts.remove(h)
                            
                            # 如果该端口下没有主机了，删除端口记录
                            if not hosts:
                                del self.host_to_sw_port[sw_id][port]
                            
                            # 如果交换机没有连接任何主机，清理交换机记录
                            if not self.host_to_sw_port[sw_id]:
                                del self.host_to_sw_port[sw_id]
                            
                            # 清理mac_to_port
                            if sw_id in self.mac_to_port and mac in self.mac_to_port[sw_id]:
                                self.mac_to_port[sw_id][mac].discard(port)
                                if not self.mac_to_port[sw_id][mac]:
                                    del self.mac_to_port[sw_id][mac]
                            
                            # 清理ARP表
                            for key in list(self.arp_table.keys()):
                                if key[0] == sw_id and key[1] == mac:
                                    del self.arp_table[key]
                            
                            # 如果旧位置不是链路端口，说明是正常迁移，可以返回了
                            if not is_old_link:
                                return
                        else:
                            # 同一位置，但IP可能不同，更新IP
                            if h[1] != ip:
                                if self.host_migration_log_enable:
                                    self.logger.info("【更新IP】MAC=%s, 旧IP=%s -> 新IP=%s, dpid=%s, port=%s",
                                                   mac, h[1], ip, sw_id, port)
                                h[1] = ip
                            return

    def _cleanup_pending_host_learning(self):
        """
        清理超时的待学习记录，避免 pending_host_learning 无限增长。
        说明：延迟学习是为了等待链路发现/LLDP确认端口类型，超时清理仅用于兜底。
        """
        while True:
            try:
                hub.sleep(5)
                now_time = time.time()
                timeout = 10  # 10秒超时

                keys_to_remove = []
                for key, first_seen in list(self.pending_host_learning.items()):
                    if (now_time - first_seen) > timeout:
                        keys_to_remove.append(key)

                for key in keys_to_remove:
                    self.pending_host_learning.pop(key, None)
                    dpid, port, mac, ip = key
                    if self.host_migration_log_enable:
                        self.logger.debug("【超时清理】待学习记录: MAC=%s, IP=%s, dpid=%s, port=%s",
                                          mac, ip, dpid, port)
            except Exception as e:
                self.logger.error("清理待学习记录时出错: %s", e)
    
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _switch_packet_in_handle(self, ev):
        """
        处理交换机发出的ARP、IP数据包
        注意：这个方法主要用于MAC学习和流表下发，不用于主机发现
        """
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        in_port = msg.match['in_port']
        
        try:
            eth, pkt_type, pkt_data = ethernet.ethernet.parser(msg.data)
        except:
            return
            
        src_mac = eth.src
        dst_mac = eth.dst

        if eth.ethertype not in [ether_types.ETH_TYPE_ARP, ether_types.ETH_TYPE_IP]:
            return
            
        try:
            pkt, _, _ = pkt_type.parser(pkt_data)
            src_ip = pkt.src_ip if hasattr(pkt, 'src_ip') else pkt.src
            dst_ip = pkt.dst_ip if hasattr(pkt, 'dst_ip') else pkt.dst
        except:
            return

        # 过滤无效IP
        if src_ip == "0.0.0.0":
            return

        # ============ 关键修复4: 不在这里学习主机 ============
        # 主机学习已经在 _host_arp_packet_in_handle 中完成
        # 这里只负责MAC地址到端口的映射（用于转发）
        
        # 更新ARP表（用于防止ARP风暴）
        self.arp_table[(dpid, src_mac, dst_ip)] = in_port
        
        # 更新mac_to_port（用于数据包转发）
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid].setdefault(src_mac, set())
        self.mac_to_port[dpid][src_mac].add(in_port)

        # 查找目标MAC的输出端口
        if dst_mac in self.mac_to_port[dpid]:
            out_ports = self.mac_to_port[dpid][dst_mac]
        else:
            out_ports = [ofproto.OFPP_FLOOD]

        # 日志记录
        if self.ip_packet_log_enable and eth.ethertype == ether_types.ETH_TYPE_IP:
            self.logger.info("转发数据包: 源IP=%s, 目标IP=%s, 源MAC=%s, 目标MAC=%s, 交换机=%s, 入端口=%s",
                           src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)

        # 安装流表和转发数据包
        for out_port in out_ports:
            actions1 = [parser.OFPActionOutput(out_port)]
            
            if out_port != ofproto.OFPP_FLOOD:
                # 正向流表
                match1 = parser.OFPMatch(in_port=in_port, eth_dst=dst_mac, eth_src=src_mac)
                # 反向流表
                match2 = parser.OFPMatch(in_port=out_port, eth_dst=src_mac, eth_src=dst_mac)
                actions2 = [parser.OFPActionOutput(in_port)]
                
                if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                    self.add_flow(datapath, 1, match1, actions1, hard_timeout=5, buffer_id=msg.buffer_id)
                    self.add_flow(datapath, 1, match2, actions2, hard_timeout=5, buffer_id=msg.buffer_id)
                    return
                else:
                    self.add_flow(datapath, 1, match1, actions1, hard_timeout=5)
                    self.add_flow(datapath, 1, match2, actions2, hard_timeout=5)
            
            # 发送数据包
            data = None
            if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                data = msg.data
            
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                     in_port=in_port, actions=actions1, data=data)
            datapath.send_msg(out)
            break  # 只发送一次

    def _cleanup_invalid_hosts(self):
        """
        定期清理错误学习的主机
        1. 链路端口上的主机
        2. 非本域交换机的主机
        3. 不在IP白名单内的主机
        """
        while True:
            try:
                hub.sleep(10)
                
                cleaned_count = 0
                cleaned_details = []
                
                for sw_id in list(self.host_to_sw_port.keys()):
                    # 检查1: 交换机是否是本域的
                    if sw_id not in self.dpid_to_switch:
                        hosts_count = sum(len(hosts) for hosts in self.host_to_sw_port[sw_id].values())
                        self.logger.warning("【定期清理】非本域交换机的主机: 交换机=%s, 主机数=%d",
                                           sw_id, hosts_count)
                        cleaned_count += hosts_count
                        cleaned_details.append(f"非本域交换机{sw_id}: {hosts_count}个主机")
                        del self.host_to_sw_port[sw_id]
                        continue
                    
                    for port in list(self.host_to_sw_port.get(sw_id, {}).keys()):
                        # 检查2: 端口是否是链路端口
                        if self.is_link_port(sw_id, port):
                            hosts = self.host_to_sw_port[sw_id][port]
                            if hosts:
                                self.logger.warning("【定期清理】链路端口上的主机: 交换机=%s, 端口=%s, 主机=%s",
                                                   sw_id, port, hosts)
                                cleaned_details.append(f"链路端口{sw_id}:{port}: {hosts}")
                                
                                # 清理关联数据
                                for h in hosts:
                                    mac = h[0]
                                    if sw_id in self.mac_to_port and mac in self.mac_to_port[sw_id]:
                                        self.mac_to_port[sw_id][mac].discard(port)
                                        if not self.mac_to_port[sw_id][mac]:
                                            del self.mac_to_port[sw_id][mac]
                                    
                                    for key in list(self.arp_table.keys()):
                                        if key[0] == sw_id and key[1] == mac:
                                            del self.arp_table[key]
                                
                                cleaned_count += len(hosts)
                                del self.host_to_sw_port[sw_id][port]
                                
                                if not self.host_to_sw_port[sw_id]:
                                    del self.host_to_sw_port[sw_id]
                            continue
                    
                        # 检查3: IP是否在白名单内
                        hosts = self.host_to_sw_port[sw_id][port]
                        invalid_hosts = []
                        
                        for host in list(hosts):
                            mac, ip = host[0], host[1]
                            
                            if not self.is_allowed_ip(ip):
                                self.logger.warning("【定期清理】非法IP主机: 交换机=%s, 端口=%s, MAC=%s, IP=%s",
                                                   sw_id, port, mac, ip)
                                invalid_hosts.append(host)
                                cleaned_details.append(f"非法IP {sw_id}:{port} {mac}={ip}")
                                
                                # 清理关联数据
                                if sw_id in self.mac_to_port and mac in self.mac_to_port[sw_id]:
                                    self.mac_to_port[sw_id][mac].discard(port)
                                    if not self.mac_to_port[sw_id][mac]:
                                        del self.mac_to_port[sw_id][mac]
                                
                                for key in list(self.arp_table.keys()):
                                    if key[0] == sw_id and key[1] == mac:
                                        del self.arp_table[key]
                        
                        if invalid_hosts:
                            for invalid_host in invalid_hosts:
                                hosts.remove(invalid_host)
                            cleaned_count += len(invalid_hosts)
                            
                            if not hosts:
                                del self.host_to_sw_port[sw_id][port]
                                if not self.host_to_sw_port[sw_id]:
                                    del self.host_to_sw_port[sw_id]
                
                if cleaned_count > 0:
                    self.logger.info("【定期清理完成】清理了 %d 个错误学习的主机记录", cleaned_count)
                    for detail in cleaned_details:
                        self.logger.info("  - %s", detail)
                    
            except Exception as e:
                self.logger.error("定期清理主机时出错: %s", e)
                import traceback
                self.logger.error(traceback.format_exc())

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
            
             # 只在开启时打印IP数据包日志
            if self.ip_packet_log_enable and eth.ethertype == ether_types.ETH_TYPE_IP:
                self.logger.info("11_host_ip_packet_in_handle收到IP数据包: 源IP=%s,目标IP=%s,源MAC=%s,目标MAC=%s,交换机=%s,端口=%s", 
                                    src_ip, dst_ip, src_mac, dst_mac, dpid, in_port)
            
            
            #关键：检查是否是交换机间的链路端口
            if self.is_link_port(dpid, in_port):
                self.logger.info("_host_ip_packet_in_handle忽略交换机链路端口的数据包: dpid=%s, in_port=%s", dpid, in_port)
                return
                
            # 直接查找目标IP对应的交换机和端口
            dst_switch_id = None
            dst_port = None
            dst_mac_addr = None
            
            for sw_id in self.host_to_sw_port:
                for port in self.host_to_sw_port[sw_id]:
                    for host_info in self.host_to_sw_port[sw_id][port]:
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
                    # self.logger.error("未连接到server_agent，无法处理跨域请求")
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
                    self.logger.info("="*60)
                    self.logger.info("准备发送拓扑信息到server_agent")
                    
                    # 收集本域内的交换机列表
                    local_switches = set(self.dpid_to_switch.keys())
                    self.logger.info("本域交换机: %s", local_switches)
                    
                    # 过滤主机信息
                    filtered_host_info = []
                    for dpid, ports in self.host_to_sw_port.items():
                        if dpid not in local_switches:
                            self.logger.warning("【过滤】跳过非本域交换机的主机: dpid=%s", dpid)
                            continue
                        
                        for port, hosts in ports.items():
                            if self.is_link_port(dpid, port):
                                self.logger.warning("【过滤】跳过链路端口的主机: dpid=%s, port=%s", dpid, port)
                                continue
                            
                            for host in hosts:
                                mac, ip = host[0], host[1]
                                if ip == "0.0.0.0":
                                    continue
                                
                                # 再次验证该端口是否在链路表中
                                is_inter_link = any(
                                    (dpid == link[0] and port == self.topo_inter_link[link][0])
                                    for link in self.topo_inter_link.keys()
                                )
                                is_access_link = any(
                                    (dpid == link[0] and port == self.topo_access_link[link][0])
                                    for link in self.topo_access_link.keys()
                                )
                                
                                if is_inter_link or is_access_link:
                                    self.logger.warning("【过滤】链路端口的主机: dpid=%s, port=%s, MAC=%s, IP=%s",
                                                        dpid, port, mac, ip)
                                    continue
                                
                                filtered_host_info.append({
                                    'dpid': dpid,
                                    'port': port,
                                    'mac': mac,
                                    'ip': ip
                                })
                    
                    self.logger.info("过滤后主机数量: %d", len(filtered_host_info))
                    
                    # ===== 关键：收集所有链路 =====
                    link_info = []
                    
                    # 1. 域内链路 (topo_inter_link)
                    for link in self.topo_inter_link.keys():
                        link_data = {
                            'src': link[0],
                            'dst': link[1],
                            'src_port': self.topo_inter_link[link][0],
                            'delay': self.topo_inter_link[link][2],
                            'bw': self.topo_inter_link[link][3],
                            'loss': self.topo_inter_link[link][4],
                            'type': 'intra'
                        }
                        link_info.append(link_data)
                        self.logger.info("【域内链路】上报: %s(端口%s) -> %s", 
                                         link[0], self.topo_inter_link[link][0], link[1])
                    
                    # 2. 域间链路 (topo_access_link) - 关键！
                    for link in self.topo_access_link.keys():
                        link_data = {
                            'src': link[0],
                            'dst': link[1],
                            'src_port': self.topo_access_link[link][0],
                            'delay': self.topo_access_link[link][2],
                            'bw': self.topo_access_link[link][3],
                            'loss': self.topo_access_link[link][4],
                            'type': 'inter'
                        }
                        link_info.append(link_data)
                        self.logger.info("【域间链路】上报: 本域交换机%s(端口%s) -> 远程域交换机%s", 
                                        link[0], self.topo_access_link[link][0], link[1])
                    
                    # 统计
                    intra_count = sum(1 for l in link_info if l.get('type') == 'intra')
                    inter_count = sum(1 for l in link_info if l.get('type') == 'inter')
                    self.logger.info("链路统计: 域内=%d, 域间=%d, 总计=%d", 
                                   intra_count, inter_count, len(link_info))
                    
                    # 构建拓扑信息
                    topo_msg = {
                        "type": "topo",
                        "switches": list(self.dpid_to_switch.keys()),
                        "link": link_info,
                        "host": filtered_host_info
                    }
                    
                    self.logger.info("发送拓扑: 交换机%d个, 链路%d条(域内%d+域间%d), 主机%d个",
                                   len(topo_msg["switches"]), 
                                   len(link_info), intra_count, inter_count,
                                   len(filtered_host_info))
                    self.logger.info("="*60)
                    
                    self._send_to_server(topo_msg)
                    
                except Exception as e:
                    self.logger.error(f"发送拓扑信息失败: {e}")
                    import traceback
                    self.logger.error(traceback.format_exc())
            
            hub.sleep(10)

    def _send_to_server(self, msg):
        """发送消息到server"""
        if self.is_connected:
            try:
                data = json.dumps(msg) + '\n'  # 添加换行符作为消息分隔符
                self.server_socket.sendall(data.encode())
            except Exception as e:
                self.logger.error(f"发送失败: {e}")
                self.is_connected = False
                if self.server_socket:
                    self.server_socket.close()

    def _heartbeat_loop(self):
        """定期向根控制器发送心跳，保持连接活跃"""
        while True:
            try:
                if self.is_connected:
                    self._send_to_server({"type": "heartbeat"})
            except Exception as e:
                self.logger.error(f"发送心跳失败: {e}")
                self.is_connected = False
                if self.server_socket:
                    self.server_socket.close()
            finally:
                hub.sleep(2)

    def _receive_from_server(self):
        """接收server消息的循环"""
        buffer = ""  # 用于累积未完成的消息
        while self.is_connected:
            try:
                data = self.server_socket.recv(4096)
                if not data:
                    break
                
                # 将接收到的数据添加到缓冲区
                buffer += data.decode('utf-8')
                
                # 按换行符分割消息
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.strip()
                    if line:  # 如果不是空行
                        try:
                            msg = json.loads(line)
                            self._handle_server_msg(msg)
                        except json.JSONDecodeError as json_err:
                            self.logger.error(f"JSON解析失败: {json_err}，接收到的数据: {line[:100]}")
                        except Exception as e:
                            self.logger.error(f"处理消息失败: {e}")
                            
            except Exception as e:
                self.logger.error(f"接收失败: {e}")
                break
        
        self.is_connected = False
        if self.server_socket:
            self.server_socket.close()

    def _handle_server_msg(self, msg):
        """处理从server收到的消息"""
        # self.logger.info(f"收到server消息: {msg}")
        if not isinstance(msg, dict):
            self.logger.error(f"收到非字典类型消息: {msg}")
            return
        
        msg_type = msg.get('type')
        
        # 处理PortData查询请求（来自其他控制器）
        if msg_type == 'portdata_query':
            self._handle_portdata_query(msg)
            return
        
        # 处理PortData查询响应（来自server_agent）
        if msg_type == 'portdata_response':
            self._handle_portdata_response(msg)
            return
        
        # 处理根控制器计算后的LLDP延迟
        if msg_type == 'lldp_delay_update':
            self._handle_lldp_delay_update(msg)
            return
        
        # 处理路径响应
        if msg.get('status') == 'ok' and 'path' in msg:
            path = msg['path']
            if path:
                self.logger.info(f"收到路径: {path}")
                # 获取源IP和目标IP
                src_ip = msg.get('src_ip')
                dst_ip = msg.get('dst_ip')
                switch_id = msg.get('switch_id')
                in_port = msg.get('in_port')
                
                # 处理完整的路径信息
                self._process_path(path, src_ip, dst_ip)
        elif msg.get('status') == 'error':
            self.logger.error(f"server_agent返回错误: {msg.get('message')}")

    # def _process_path(self, path, src_ip, dst_ip):
        # """处理路径信息"""
        # print("**********1111111_process_path***************")
        # 找到本controller负责的交换机段
        # path结构: [host1_mac, sw1, sw2, ..., swN, host2_mac]
        # 找到本域内连续交换机片段
        # start_idx = -1
        # end_idx = -1
        
        # for i in range(1, len(path)-1): #因为路径的第一个和最后一个元素是主机地址（不是交换机）
            # dpid = path[i]
            # if dpid in self.dpid_to_switch:
                # if start_idx == -1:
                    # start_idx = i
                # end_idx = i
            # elif start_idx != -1:
                # 发现一段连续的本域交换机，下发流表
                # self._install_path_segment(path, start_idx, end_idx, src_ip, dst_ip)
                # 重置索引，寻找下一段
                # start_idx = -1
        
        # 理最后一段连续的本控制器交换机段
        # if start_idx != -1:
            # self._install_path_segment(path, start_idx, end_idx, src_ip, dst_ip)
        
    # def _process_path(self, path, src_ip, dst_ip):
    #     # path: [host1_ip, sw1, sw2, ..., swN, host2_ip]
    #     # 找到本控制器负责的交换机在路径中的索引
    #     for i in range(1, len(path) - 1):
    #         dpid = path[i]
    #         if dpid in self.dpid_to_switch:
    #             # 推断 in_port
    #             if i == 1:
    #                 in_port = self.get_switch_port_by_ip(src_ip)
    #             else:
    #                 prev_dpid = path[i - 1]
    #                 in_port = self.get_port_from_link(dpid, prev_dpid)
    #             # 只对本交换机下发流表
    #             self.install_flow_entry([dpid], src_ip, dst_ip, in_port)
    #             break  # 只处理一个交换机，直接退出
    
    def _process_path(self, path, src_ip, dst_ip, msg=None):
        """
        处理server_agent返回的全局路径，为本控制器负责的交换机下发正向和反向流表
        path: [host1_ip, sw1, sw2, ..., swN, host2_ip]
        """
        print("**********111111_process_path**********")
        print(f"找到路径: {path}")
        for i in range(1, len(path) - 1):
            dpid = path[i]
            if dpid in self.dpid_to_switch:
                datapath = self.dpid_to_switch[dpid]
                # 推断 in_port
                if i == 1:
                    in_port = self.get_switch_port_by_ip(src_ip)
                    src_mac_addr = self.get_mac_by_ip(src_ip)
                else:
                    prev_dpid = path[i - 1]
                    in_port = self.get_port_from_link(dpid, prev_dpid)
                # 推断 out_port
                if i == len(path) - 2:
                    # 最后一跳，出端口是目标主机端口
                    out_port = self.get_switch_port_by_ip(dst_ip)
                    dst_mac_addr = self.get_mac_by_ip(dst_ip)
                    
                    # 正向流表
                    actions = [
                        datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac_addr),
                        datapath.ofproto_parser.OFPActionOutput(out_port)
                    ]
                    match = datapath.ofproto_parser.OFPMatch(
                        eth_type=ether.ETH_TYPE_IP,
                        in_port=in_port,
                        ipv4_dst=dst_ip,
                        ipv4_src=src_ip
                    )
                    self.add_flow(datapath, 1, match, actions)
                    # 反向流表
                    actions_reverse = [
                        datapath.ofproto_parser.OFPActionSetField(eth_dst=src_mac_addr),#这个地方是5.19加的
                        datapath.ofproto_parser.OFPActionOutput(in_port)
                    ]
                    match_reverse = datapath.ofproto_parser.OFPMatch(
                        eth_type=ether.ETH_TYPE_IP,
                        in_port=out_port,
                        ipv4_dst=src_ip,
                        ipv4_src=dst_ip
                    )
                    self.add_flow(datapath, 1, match_reverse, actions_reverse)
                else:
                    # 中间节点，出端口是到下一个交换机的端口
                    next_dpid = path[i + 1]
                    out_port = self.get_port_from_link(dpid, next_dpid)
                    # 正向流表
                    actions = [
                        datapath.ofproto_parser.OFPActionOutput(out_port)
                    ]
                    match = datapath.ofproto_parser.OFPMatch(
                        eth_type=ether.ETH_TYPE_IP,
                        in_port=in_port,
                        ipv4_dst=dst_ip,
                        ipv4_src=src_ip
                    )
                    self.add_flow(datapath, 1, match, actions)
                    # 反向流表
                    if i == 1:
                        # 首跳反向需要改目标MAC
                        src_mac_addr = self.get_mac_by_ip(src_ip)
                        actions_reverse = [
                            datapath.ofproto_parser.OFPActionSetField(eth_dst=src_mac_addr),
                            datapath.ofproto_parser.OFPActionOutput(in_port)
                        ]
                    else:
                        actions_reverse = [
                            datapath.ofproto_parser.OFPActionOutput(in_port)
                        ]
                    match_reverse = datapath.ofproto_parser.OFPMatch(
                        eth_type=ether.ETH_TYPE_IP,
                        in_port=out_port,
                        ipv4_dst=src_ip,
                        ipv4_src=dst_ip
                    )
                    self.add_flow(datapath, 1, match_reverse, actions_reverse)
                # 发送当前数据包
                if msg and i == 1:
                    self.send_packet_to_outport(datapath, msg, in_port, actions)
                self.logger.info("【跨域流表安装】交换机=%s, in_port=%s, out_port=%s, src_ip=%s, dst_ip=%s, actions=%s",
                                dpid, in_port, out_port, src_ip, dst_ip, actions)
                # break  # 只处理本控制器的交换机

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




