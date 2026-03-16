import time
import netifaces
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3, ether
from ryu.lib.packet import ethernet, ether_types
from ryu.lib import hub
from ryu.topology.switches import LLDPPacket


class SimpleTopoApp(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SimpleTopoApp, self).__init__(*args, **kwargs)
        self.switch = None
        self.hosts = {}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        msg = ev.msg
        self.switch = msg.datapath
        # 添加流表以处理到达的流量
        match = self.switch.ofproto_parser.OFPMatch()
        actions = [self.switch.ofproto_parser.OFPActionOutput(self.switch.ofproto.OFPP_CONTROLLER)]
        self.add_flow(self.switch, 0, match, actions)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        eth = ethernet.ethernet.parser(msg.data)
        # eth 解析返回的元组中，通常第一个元素是以太网帧，第二个是报文类型
        eth_pkt = eth[0]  # 获取以太网帧
        pkt_type = eth[1]  # 获取数据包类型

        # 处理 LLDP 数据包
        if eth_pkt.ethertype == ether_types.ETH_TYPE_LLDP:
            self.handle_lldp(msg)

        # 处理 ARP 和 IP 数据包
        if eth_pkt.ethertype in [ether_types.ETH_TYPE_ARP, ether_types.ETH_TYPE_IP]:
            self.handle_ip_packet(msg)

    def handle_lldp(self, msg):
        parsed_values = LLDPPacket.lldp_parse(msg.data)
        if len(parsed_values) < 2:
            return
        src_dpid = parsed_values[0]
        src_port = parsed_values[1]
        self.logger.info("收到 LLDP 数据包：源交换机 %s，源端口 %s", src_dpid, src_port)

    def handle_ip_packet(self, msg):
        eth = ethernet.ethernet.parser(msg.data)
        src_mac = eth.src
        dst_mac = eth.dst
        dpid = msg.datapath.id
        in_port = msg.match['in_port']

        # 处理主机信息
        if dpid not in self.hosts:
            self.hosts[dpid] = {}
        self.hosts[dpid][in_port] = src_mac
        self.logger.info("主机信息：交换机 %s 端口 %s MAC %s", dpid, in_port, src_mac)

    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match, instructions=inst)
        datapath.send_msg(mod)