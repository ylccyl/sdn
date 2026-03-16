import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER, HANDSHAKE_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3
from ryu.topology.switches import LLDPPacket
from ryu.base.app_manager import lookup_service_brick
from ryu.topology.api import get_switch, get_link, get_host


class DelayDetector(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(DelayDetector, self).__init__(*args, **kwargs)
        self.name = 'delay_detector'
        self.switches = lookup_service_brick('switches')
        self.dpidSwitch = {}
        self.echoDelay = {}
        self.src_dstDelay = {}
        self.lldp_timestamps = {}  # 新增字典用于存储LLDP发送时间戳

        self.detector_thread = hub.spawn(self.detector)

    def detector(self):
        while True:
            self.send_echo_request()
            hub.sleep(3)

    def add_flow(self, datapath, priority, match, actions):
        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser
        command = ofp.OFPFC_ADD
        inst = [ofp_parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        req = ofp_parser.OFPFlowMod(datapath=datapath, command=command,
                                    priority=priority, match=match, instructions=inst)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofp = datapath.ofproto
        ofp_parser = datapath.ofproto_parser

        # add table-miss
        match = ofp_parser.OFPMatch()
        actions = [ofp_parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(datapath=datapath, priority=0, match=match, actions=actions)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if not datapath.id in self.dpidSwitch:
                self.dpidSwitch[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.dpidSwitch:
                del self.dpidSwitch[datapath.id]

    def send_echo_request(self):
        for datapath in self.dpidSwitch.values():
            parser = datapath.ofproto_parser
            echo_req = parser.OFPEchoRequest(datapath, data=bytes("%.12f" % time.time(), encoding="utf8"))
            datapath.send_msg(echo_req)
            hub.sleep(0.5)

    @set_ev_cls(ofp_event.EventOFPEchoReply, [MAIN_DISPATCHER, CONFIG_DISPATCHER, HANDSHAKE_DISPATCHER])
    def echo_reply_handler(self, ev):
        now_timestamp = time.time()
        try:
            echo_delay = now_timestamp - eval(ev.msg.data)
            self.echoDelay[ev.msg.datapath.id] = echo_delay
            print('*******************echo delay*****************')
            print(self.echoDelay)
        except Exception as error:
            print(error)
            return

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        try:
            parsed_values = LLDPPacket.lldp_parse(msg.data)
            if len(parsed_values) < 2:
                raise ValueError("Expected at least 2 values from lldp_parse")
            src_dpid = parsed_values[0]
            src_outport = parsed_values[1]
            dst_dpid = msg.datapath.id

            if self.switches is None:
                self.switches = lookup_service_brick("switches")

                # 检查是否有对应的时间戳
                key = (src_dpid, src_outport)
                if key in self.lldp_timestamps:
                    send_time = self.lldp_timestamps.pop(key)  # 获取并移除时间戳以避免重复计算
                    receive_time = time.time()
                    link_delay = receive_time - send_time
                    print(f"Link delay between {src_dpid}:{src_outport} and {dst_dpid}: {link_delay:.6f} seconds")
                    self._save_delay_data(src=src_dpid, dst=dst_dpid, src_port=src_outport, lldp_dealy=link_delay)
                else:
                    print(f"No timestamp found for LLDP from {src_dpid}:{src_outport}")
        except Exception as error:
            print(error)
            return

    def _save_delay_data(self, src, dst, src_port, lldp_dealy):
        key = "%s-%s-%s" % (src, src_port, dst)
        self.src_dstDelay[key] = lldp_dealy
        print('------------------lldp delay--------------------')
        print(self.src_dstDelay)

    def send_lldp_packet(self, datapath, port_no):
        # 记录发送时间戳
        send_time = time.time()
        self.lldp_timestamps[(datapath.id, port_no)] = send_time