# Copyright (C) 2016 Li Cheng at Beijing University of Posts
# and Telecommunications. www.muzixing.com
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from ryu.app.zxf.new.topo_awareness import TopoAwareness
from ryu.base import app_manager
from ryu.base.app_manager import lookup_service_brick
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib.packet import ethernet, ether_types
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.topology.switches import LLDPPacket
import time


class DelayMonitor(app_manager.RyuApp):
    """
        NetworkDelayDetector is a Ryu app for collecting link delay.
    """
    _CONTEXTS = {"topo_awareness": TopoAwareness}

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(DelayMonitor, self).__init__(*args, **kwargs)
        self.name = 'delay_monitor'
        self.sending_echo_request_interval = 0.05
        # self.topo_awareness = lookup_service_brick('topo_awareness')
        self.topo_awareness = kwargs['topo_awareness']
        self.dpid_to_switch = {}
        self.echo_latency = {}   #   {dpid:time,1:0.5,2:0.3,....}
        self.lldp_delay = {}     #   {(src,dst):time,(1,2):0.5,...}
        self.link_delay = {}     #   {(src,dst):time,(1,2):0.5,....}
        self.measure_thread = hub.spawn(self._detector)

    def _detector(self):
        """
            Delay detecting functon.
            Send echo request and calculate link delay periodically
        """
        while True:
            self._send_echo_request()
            self.create_link_delay()
            # self.show_delay_statis()
            hub.sleep(3)

    def _send_echo_request(self):
        """
            Seng echo request msg to datapath.
        """
        datapaths = self.dpid_to_switch.values()
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
            hub.sleep(self.sending_echo_request_interval)

    def create_link_delay(self):
        """
            Create link delay data, and save it into graph object.
        """
        # try:
        #     for src in self.topo_awareness.graph:
        #         for dst in self.topo_awareness.graph[src]:
        #             if (src, dst) in self.topo_awareness.topo_inter_link.keys():
        #                 delay = self._get_delay(src, dst)
        #                 self.link_delay[(src, dst)] = delay
        #
        #             else:
        #                 delay = self._get_access_delay(src, dst)
        #                 self.link_delay[(src, dst)] = delay
        #
        #             self.topo_awareness.graph[src][dst]['delay'] = delay
        # except:
        #     if self.topo_awareness is None:
        #         self.topo_awareness = lookup_service_brick('topo_awareness')
        # if self.topo_awareness is None:
        #     self.topo_awareness = lookup_service_brick('topo_awareness')

        link_to_port = self.topo_awareness.topo_inter_link
        for link in link_to_port.keys():
            (src_dpid, dst_dpid) = link
            try:
                delay = self._get_delay(src_dpid, dst_dpid)
                self.topo_awareness.topo_inter_link[(src_dpid, dst_dpid)][2] = delay
            except:
                pass

        link_to_port = self.topo_awareness.topo_access_link
        for link in link_to_port.keys():
            (src_dpid, dst_dpid) = link
            try:
                delay = self._get_access_delay(src_dpid, dst_dpid)
                self.topo_awareness.topo_inter_link[(src_dpid, dst_dpid)][2] = delay
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
            fwd_delay = self.lldp_delay[(src, dst)]
            re_delay = self.lldp_delay[(dst, src)]
            src_latency = self.echo_latency[src]
            dst_latency = self.echo_latency[dst]

            delay = (fwd_delay + re_delay - src_latency - dst_latency) / 2
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
            fwd_delay = self.lldp_delay[(src, dst)]
            src_latency = self.echo_latency[src]

            delay = (fwd_delay + fwd_delay - src_latency - src_latency) / 2
            return max(delay, 0)
        except:
            return float(0)

    def _save_lldp_delay(self, src=0, dst=0, lldpdelay=0):
        self.lldp_delay[(src, dst)] = lldpdelay



    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def _echo_reply_handler(self, ev):
        """
            Handle the echo reply msg, and get the latency of link.
        """
        now_timestamp = time.time()
        try:
            latency = now_timestamp - eval(ev.msg.data)
            self.echo_latency[ev.msg.datapath.id] = latency
        except:
            return

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        """

            需要对LLDP进行必要的修改，增加时间戳部分，解析LLDP数据包，
            获取发出LLDP数据包交换机的id，利用接收到的时间与LLDP数据
            包中携带的时间戳做差值计算达到两个交换机之间的单项传输时间
        """
        # TODO: 需要对LLDP进行必要的修改，增加时间戳部分
        msg = ev.msg
        dpid = msg.datapath.id
        eth, pkt_type, pkt_data = ethernet.ethernet.parser(msg.data)
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            try:
                src_dpid, src_port_no, timestamp = LLDPPacket.lldp_parse(msg.data)
                now_time = time.time()
                delay = now_time - timestamp
                # print("%s switch receive lldp message from %s switch ,the delaytime is %s" % (dpid, src_dpid, delay))

                self._save_lldp_delay(dst=src_dpid, src=dpid, lldpdelay=delay)
            except LLDPPacket.LLDPUnknownFormat as e:
                return

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.dpid_to_switch:
                # self.logger.info('Register datapath: %016x', datapath.id)
                self.dpid_to_switch[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.dpid_to_switch:
                # self.logger.info('Unregister datapath: %016x', datapath.id)
                del self.dpid_to_switch[datapath.id]

    """
        Accessor get link delay as dict.
    """

    def get_link_delay(self):
        pass

    def show_delay_statis(self):
        # if self.lldp_delay is not None:
        #     print("333333333333333333333333333333333")
        #     for item in self.link_delay.keys():
        #         self.logger.info("%s<-->%s : %s" % (item[0], item[1], self.link_delay[item]))
        print("--------------------------")
        print(self.topo_awareness.graph.edges(data=True))
        # print(self.echo_latency)
        # print(self.lldp_delay)
        # print(self.link_delay)
        print("/////////////////////")