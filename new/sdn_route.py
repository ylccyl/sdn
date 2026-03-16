# Copyright (C) 2011 Nippon Telegraph and Telephone Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import random
import socket
import threading

from ryu import cfg

from ryu.topology import event
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub
from ryu.lib.hub import StreamServer
from ryu.ofproto import ofproto_v1_3, ether
from ryu.lib.packet import packet, arp
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types
from ryu.app.zxf.new.delay_monitor import DelayMonitor
from ryu.app.zxf.new.port_statistic import PortStatistic
from ryu.app.zxf.new.topo_awareness import TopoAwareness

CONTROLLER_IP = '10.0.0.1'
_IP = ['10.0.0.1', '10.0.0.2', '10.32.0.1', '10.64.0.1']
# _IP = ['127.0.0.1']
# CONTROLLER_IP = '127.0.0.1'


class SdnRoute(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    _CONTEXTS = {"topo_awareness": TopoAwareness,
                 "delay_monitor": DelayMonitor,
                 "port_statistic": PortStatistic}

    def __init__(self, *args, **kwargs):
        super(SdnRoute, self).__init__(*args, **kwargs)
        self.name = 'sdn_route'
        self.delay_monitor = kwargs['delay_monitor']
        self.port_statistic = kwargs['port_statistic']
        self.topo_awareness = kwargs['topo_awareness']
        self.to_agent_send_q = hub.Queue(16)
        self._to_agent_send_q_sem = hub.BoundedSemaphore(self.to_agent_send_q.maxsize)
        self.select_master_flag = self.CONF.select_master  #  # 获取配置中的主控制器选择标志
        self.controller_ip = CONTROLLER_IP
        self.lock = threading.Lock()
        self.sub_lock = threading.Lock()
        self.switch_role_reply = {}
        self.mac_to_port = {}
        self.arp_table = {}
        self.thread = hub.spawn(self.show)

    def show(self):  # 定期打印当前网络拓扑的状态信息（如交换机和链路）
        while True:  # 因为这里的while循环一直进行，结合hub.sleep(5)说明每5秒打印一次拓扑
            print("/////////")
            print(self.topo_awareness.graph.edges(data=True))  # 获取当前网络拓扑的边及其相关数据，并打印出来。
            print("***************")
            hub.sleep(5)

    def _submit(self):  # 连接到指定的代理服务器，处理信息的发送和接收逻辑
        while self.sub_lock.acquire():
            # ip = self.select_master.get_leader_ip_by_id()
            ip = '127.0.0.1'
            print("find the master service ip", ip)
            addr = (ip, 5001)
            while True:
                try:
                    self.submit_socket = socket.socket()
                    self.submit_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    self.submit_socket_state = True
                    self.submit_socket.connect(addr)
                    print("succeed !!!!")
                    break
                except socket.error as e:
                    print('Failed to connect to server:', e)
                    hub.sleep(1)
                    continue   # 等待 1 秒后继续尝试连接

            put_thr = hub.spawn(self._put_loop)
            send_thr = threading.Thread(target=self._recv_loop)
            send_thr.start()

            self._send_loop()

            hub.kill(put_thr)
            hub.joinall([put_thr])
            self.cut_connect()
            self.sub_lock.release()
            print("release lock")
            print(self.to_agent_send_q.empty())
            return

    def cut_connect(self):  # 关闭与代理服务器的 socket 连接
        try:
            print("close socket !!!!!")
            self.submit_socket.shutdown(socket.SHUT_WR)
            self.submit_socket.close()
            return
        except:
            return

    def _send_loop(self):  # 从消息队列中获取消息并发送到代理服务器
        print("enter _send_loop event")
        while self.submit_socket_state:
            try:
                try:
                    buf = self.to_agent_send_q.get(block=False)
                except hub.QueueEmpty:
                    hub.sleep(1)
                    continue
                self._to_agent_send_q_sem.release()
                self.submit_socket.sendall(buf)  # 将 buf 中的数据发送到连接的对端
            except OSError:
                print("OSError send")
                break
            except Exception as e:
                print("Socket error while sending data to agent", e)
                break
        self.submit_socket_state = False
        try:
            while self.to_agent_send_q.get(block=False):
                self._to_agent_send_q_sem.release()
        except hub.QueueEmpty:
            pass
            print("exit send_loop event")

    def _put_loop(self):  # 定期获取网络拓扑信息（链路和主机），并将其发送到代理服务器
        print("enter put_loop event")
        while self.submit_socket_state:
            print("put topo info to queue")
            links, hosts = self.topo_awareness.handle_topo_info_for_submit()  # topo530行
            switches, master_switches = self.topo_awareness.handle_switches_for_submit()
            """
            topo_msg = {"link": links,
                        "host": hosts}
            links = [(src_id,dst_id),(1,2),(1,3),...]
            hosts = [(dpid,host_ip),[1,'10.0.1.1'],[2,'10.0.1.2']....]

            """
            topo_msg = {"type": "topo",
                        "switches": switches,
                        "master_switches": master_switches,
                        "link": links,
                        "host": hosts}
            req = json.dumps(topo_msg)
            self.send_info(req)
            hub.sleep(2)
        print("exit put_loop event")

    def _recv_loop(self):  # 接收来自代理服务器的消息，并根据消息类型进行处理
        print("enter _recv_loop event")
        while self.submit_socket_state:
            try:
                ret = self.submit_socket.recv(2048)
            except socket.timeout:
                break
            except (EOFError, IOError):
                print("OSError recv")
                break

            if not ret:
                print("null null")
                break
            try:
                reply_msg = json.loads(ret)
            except Exception as e:
                return
            if reply_msg['type'] == 'route_reply':
                print(reply_msg['path'], "/////////")
                if len(reply_msg['path']) != 0:
                    self.topo_awareness.install_flow_entry(reply_msg['path'], reply_msg['src_ip'],
                                                      reply_msg['dst_ip'], reply_msg['in_port'],
                                                      reply_msg['msg'])
            if reply_msg['type'] == 'role':
                print("收到转换控制器角色指令")
                print(reply_msg, "/////////")
                hub.spawn(self.change_role, reply_msg['path'])
            if reply_msg['type'] == 'topo_reply':
                # print("上传拓扑信息后返回ok")
                continue
        self.submit_socket_state = False
        print("exit recv_loop event")

    def send_info(self, msg):  # 将消息编码为字节并放入发送队列
        self._to_agent_send_q_sem.acquire()
        if self.to_agent_send_q:
            self.to_agent_send_q.put(bytes(msg.encode()))
        else:
            self._to_agent_send_q_sem.release()

    def add_route_req_info(self, msg):  # 将路由请求信息添加到队列中，以便后续处理
        if self.submit_socket_state:
            req = json.dumps(msg)
            self.send_info(req)

    def arp_storm_handle(self, datapath, in_port):
        out = datapath.ofproto_parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=datapath.ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=[], data=None)
        datapath.send_msg(out)

    def arp_reply_fake_mac(self, datapath, dpid, src_ip, dst_ip, dst_mac, out_port):
        print("*************** 构造arp回复一个虚假的网关mac地址 ****************")
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        src_mac = self.topo_awareness.get_mac_by_switch_port(dpid, out_port)
        ether_hd = ethernet.ethernet(dst=dst_mac,
                                     src=src_mac,
                                     ethertype=ether.ETH_TYPE_ARP)
        arp_hd = arp.arp(hwtype=1, proto=2048, hlen=6, plen=4,
                         opcode=2, src_mac=src_mac,
                         src_ip=src_ip, dst_mac=dst_mac,
                         dst_ip=dst_ip)
        # print('src_ip ,dst_ip, src_mac, dst_mac', src_ip, dst_ip, src_mac, dst_mac)
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

    def change_role(self, switch_list):  # 处理交换机角色的请求，尝试转换交换机的角色（主控或从属）
        """
        TODO:如果交换机与控制器之间已经失去连接，但是并没有注销该交换机怎么办？这会导致函数一直被占用
            需要启用向交换机发送echo信息没收到回复最大次数的参数
        """
        while self.lock.acquire():
            new_list = []
            for id in switch_list:
                if id in self.topo_awareness.dpid_to_switch:
                    new_list.append(id)
                    self.switch_role_reply.setdefault(id, None)  # 如果 id 不在字典中，则设置其默认值为 None
                    self.switch_role_reply[id] = 0  # 将 id 对应的值设置为 0(gen_id),表示角色尚未确认
                    datapath = self.topo_awareness.dpid_to_switch[id]  # 获取与 ID 相关的 datapath
                    self.topo_awareness.send_role_request(datapath, datapath.ofproto.OFPCR_ROLE_MASTER, 0)
            hub.sleep(5)  # 暂停 5 秒以等待角色确认
            result = self.check_role_reply(new_list)
            if len(result) != 0:
                self.switch_role_reply.clear()
                self.lock.release()
                self.change_role(result)  # 递归调用自身处理未确认的交换机,直到所有的交换机角色都被确认
            self.switch_role_reply.clear()
            self.lock.release()
            return

    def check_role_reply(self, new_list):  # 检查角色回复的状态，返回未确认的交换机列表
        _new_list = []
        for id in new_list:
            if self.switch_role_reply[id] == 0:  # 0 通常表示该交换机的角色尚未被确认
                _new_list.append(id)
        return _new_list

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.topo_awareness.add_flow(datapath, 0, match, actions)
        # if datapath.id in MASTER_SWITCHES:
        #     print("111111111111111111111111111111111111")
        #     self.topo_data.send_role_request(datapath, ofproto.OFPCR_ROLE_MASTER, 0)
        # else:
        #     self.topo_data.send_role_request(datapath, ofproto.OFPCR_ROLE_SLAVE, 0)

    # @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    # def packet_in_handle(self, ev):
    #     """
    #     针对LLDP数据包和IP数据包进行不同的处理方式
    #     LLDP packets and IP packets are processed in different ways
    #     """
    #     # print("******************收到数据包************************")
    #     msg = ev.msg
    #     datapath = msg.datapath
    #     eth, pkt_type, pkt_data = ethernet.ethernet.parser(msg.data)
    #     dst_mac = eth.dst
    #     src_mac = eth.src
    #     if eth.ethertype == ether_types.ETH_TYPE_LLDP:
    #         return
    #     if eth.ethertype == ether_types.ETH_TYPE_ARP:
    #         # print(f"******************收到数据包{eth.ethertype}************************")
    #         # print("收到ARP数据包")
    #         arp_pkt, _, _ = pkt_type.parser(pkt_data)
    #         src_ip = arp_pkt.src_ip
    #         dst_ip = arp_pkt.dst_ip
    #         dpid = datapath.id
    #         in_port = msg.match['in_port']
    #         """
    #         先进行arp风暴的处理
    #         """
    #         if (dpid, src_mac, dst_ip) in self.arp_table:
    #             if self.arp_table[(dpid, src_mac, dst_ip)] != in_port:
    #                 self.arp_storm_handle(datapath, in_port)
    #                 return
    #
    #         self.arp_table[(dpid, src_mac, dst_ip)] = in_port
    #         self.mac_to_port.setdefault(dpid, {})
    #         self.mac_to_port[dpid][src_mac] = in_port
    #         """
    #         处理控制器之间进行连接的数据包，正常转发并且下发流表
    #         """
    #         if src_ip in _IP:
    #             print("交换机%s的端口%s收到arp数据包，源ip是%s，目标ip是%s,源mac是%s，目的mac是%s" % (dpid, in_port, src_ip, dst_ip, src_mac, dst_mac))
    #             try:
    #                 out_port = self.mac_to_port[dpid][dst_mac]
    #             except:
    #                 out_port = datapath.ofproto.OFPP_FLOOD
    #
    #             actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
    #             if out_port != datapath.ofproto.OFPP_FLOOD:
    #                 match = datapath.ofproto_parser.OFPMatch(in_port=in_port, eth_dst=dst_mac, eth_src=src_mac)
    #                 self.topo_data.add_flow(datapath, 1, match, actions)
    #
    #             self.topo_data.send_packet_to_outport(datapath, msg, in_port, actions)
    #         else:
    #             if arp_pkt.opcode == arp.ARP_REQUEST:
    #                 if src_ip != '0.0.0.0' and src_ip != dst_ip:
    #                     # self.logger.info("收到一个查询网关mac地址的arp询问包")
    #                     self.arp_reply_fake_mac(datapath, dpid, dst_ip, src_ip, src_mac, in_port)
    #                 else:
    #                     if dst_mac in self.mac_to_port[dpid]:
    #                         out_port = self.mac_to_port[dpid][dst_mac]
    #                     else:
    #                         out_port = datapath.ofproto.OFPP_FLOOD
    #
    #                     actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
    #                     self.topo_data.send_packet_to_outport(datapath, msg, in_port, actions)
    #             if arp_pkt.opcode == arp.ARP_REPLY:
    #                 out_port = self.mac_to_port[dpid][dst_mac]
    #                 actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
    #                 self.topo_data.send_packet_to_outport(datapath, msg, in_port, actions)
    #
    #     if eth.ethertype == ether_types.ETH_TYPE_IP:
    #         # print("收到IP数据包,对数据包进行解析")
    #         ipv4_pkt, _, _ = pkt_type.parser(pkt_data)
    #         src_ip = ipv4_pkt.src
    #         dst_ip = ipv4_pkt.dst
    #         proto = ipv4_pkt.proto
    #         dpid = datapath.id
    #         in_port = msg.match['in_port']
    #
    #         if (dpid, src_mac, dst_ip) in self.arp_table:
    #             if self.arp_table[(dpid, src_mac, dst_ip)] != in_port:
    #                 self.arp_storm_handle(datapath, in_port)
    #                 return
    #
    #         self.arp_table[(dpid, src_mac, dst_ip)] = in_port
    #         self.mac_to_port.setdefault(dpid, {})
    #         self.mac_to_port[dpid][src_mac] = in_port
    #
    #         if src_ip in _IP:
    #             print("交换机%s的端口%s收到ip数据包，源ip是%s，目标ip是%s" % (dpid, in_port, src_ip, dst_ip))
    #             try:
    #                 out_port = self.mac_to_port[dpid][dst_mac]
    #             except:
    #                 out_port = datapath.ofproto.OFPP_FLOOD
    #
    #             actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
    #             if out_port != datapath.ofproto.OFPP_FLOOD:
    #                 match = datapath.ofproto_parser.OFPMatch(in_port=in_port, eth_dst=dst_mac, eth_src=src_mac)
    #                 self.topo_data.add_flow(datapath, 1, match, actions)
    #
    #             self.topo_data.send_packet_to_outport(datapath, msg, in_port, actions)
    #         else:
    #             print("交换机%s的端口%s收到ip数据包，源ip是%s，目标ip是%s" % (dpid, in_port, src_ip, dst_ip))
    #             dst_switch_id = self.topo_data.get_switch_id_by_ip(dst_ip)
    #             if dst_switch_id:
    #                 # 本地控制器内进行解决
    #                 src_switch_id = dpid
    #                 path = self.topo_data.get_path(src_switch_id, dst_switch_id)
    #                 print(path, "//////", type(path))
    #                 if len(path) != 0:
    #                     self.topo_data.install_flow_entry(path, src_ip, dst_ip, in_port, msg)
    #             else:
    #                 route_msg = {"type": "route",
    #                              "switch_id": dpid,
    #                              "src_ip": src_ip,
    #                              "dst_ip": dst_ip,
    #                              "in_port": in_port,
    #                              "msg": msg}
    #                 self.add_route_req_info(route_msg)

    # @set_ev_cls(ofp_event.EventOFPRoleReply, MAIN_DISPATCHER)
    # def role_reply_handle(self, ev):
    #     msg = ev.msg
    #     dp = msg.datapath
    #     ofp = dp.ofproto
    #     if msg.role == ofp.OFPCR_ROLE_MASTER:
    #         if dp.id in self.switch_role_reply:
    #             self.switch_role_reply[dp.id] = 1
    #     if msg.role == ofp.OFPCR_ROLE_SLAVE:
    #         if dp.id in self.switch_role_reply:
    #             del self.switch_role_reply[dp.id]

    # @set_ev_cls(event.EventMasterUp)
    # def master_up_handle(self, ev):
    #     """
    #     1、切断与原leader的tcp连接通道
    #     2、尝试与新leader建立连接
    #     3、尝试去接管原leader管理的交换机
    #     """
    #     leader = ev.leader
    #     new_leader_id = leader.new_leader_id
    #     old_leader_switch = leader.old_leader_switch
    #     print("新的leader出现，切断与原leader的tcp连接通道")
    #     self.cut_connect()
    #     print("新的leader出现，尝试与新leader建立连接")
    #     hub.spawn(self._submit)
    #     print("新的leader出现，尝试去接管原leader管理的交换机", old_leader_switch)
    #     if len(old_leader_switch) != 0:
    #         hub.spawn(self.change_role, old_leader_switch)
