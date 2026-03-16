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

from ryu.base.app_manager import lookup_service_brick

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types

# CONTROLLER_IP = '10.0.0.1'
CONTROLLER_IP = '172.17.0.9'
# SWITCHES_IP = ['10.0.0.1', '10.0.0.2', '10.0.0.3', '10.0.0.4', '10.0.0.5','10.0.0.6']
SWITCHES_IP = ['172.17.0.3','172.17.0.4','172.17.0.5','172.17.0.6','172.17.0.7','172.17.0.8','172.17.0.10','172.17.0.11','172.17.0.12','172.17.0.13','172.17.0.14','172.17.0.15']

class Main(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # _CONTEXTS = {"topo_awareness": TopoAwareness}

    def __init__(self, *args, **kwargs):
        super(Main, self).__init__(*args, **kwargs)
        self.name = 'main'
        # self.topo_awareness = kwargs['topo_awareness']
        self.topo_awareness = lookup_service_brick('topo_awareness')
        self.to_agent_send_q = hub.Queue(16)
        self._to_agent_send_q_sem = hub.BoundedSemaphore(self.to_agent_send_q.maxsize)
        self.select_master_flag = self.CONF.select_master
        self.controller_ip = CONTROLLER_IP
        self.lock = threading.Lock()
        self.sub_lock = threading.Lock()
        self.thread = hub.spawn(self.print)
        self.thread = hub.spawn(self.start_main_fun)

    def start_main_fun(self):
        # server_agent = server_Agent.ServerAgent()
        # hub.spawn(server_agent.begin)
        hub.spawn(self._submit)

    def print(self):
        hub.sleep(30)
        route_msg = {"type": "route",
                     "switch_id": 1,
                     "src_ip": 5,
                     "dst_ip": 6,
                     "in_port": 10,
                     "msg": 'jbjkdsbjkd'}
        self.add_route_req_info(route_msg)

    def _submit(self):
        print("--------------------倒计时10s-------------------------------")
        hub.sleep(10)
        print("--------------------还10s-------------------------------")
        hub.sleep(10)
        while self.sub_lock.acquire():
            # ip = self.select_master.get_leader_ip_by_id()
            ip = '172.17.0.2'
            print("find the master service ip", ip)
            addr = (ip, 5001)
            while True:
                self.submit_socket = socket.socket()
                self.submit_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.submit_socket_state = False
                try:
                    self.submit_socket.connect(addr)
                    print("succeed !!!!")
                    self.submit_socket_state = True
                    break
                except socket.error as e:
                    print('Failed to connect to server:', e)
                    self.submit_socket.close()
                    hub.sleep(1)
                    continue

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
            hub.sleep(2)

    def cut_connect(self):
        try:
            print("close socket !!!!!")
            self.submit_socket.shutdown(socket.SHUT_WR)
            self.submit_socket.close()
            return
        except:
            return

    def _send_loop(self):
        print("enter _send_loop event")
        while self.submit_socket_state:
            try:
                try:
                    buf = self.to_agent_send_q.get(block=False)
                except hub.QueueEmpty:
                    hub.sleep(1)
                    continue
                self._to_agent_send_q_sem.release()
                self.submit_socket.sendall(buf)
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

    def _put_loop(self):
        print("enter put_loop event")
        while self.submit_socket_state:
            print("put topo info to queue")
            if self.topo_awareness is None:
                self.topo_awareness = lookup_service_brick('topo_awareness')

            links_info, hosts_info = self.topo_awareness.handle_topo_info_for_submit()
            switches_info = self.topo_awareness.handle_switches_for_submit()
            """
            topo_msg = {"link": links,
                        "host": hosts}
            links = [(src_id,dst_id),(1,2),(1,3),...]
            hosts = [(dpid,host_ip),[1,'10.0.1.1'],[2,'10.0.1.2']....]

            """
            topo_msg = {"type": "topo",
                        "switches": switches_info,
                        "link": links_info,
                        "host": hosts_info}
            req = json.dumps(topo_msg)
            self.send_info(req)
            hub.sleep(2)
        print("exit put_loop event")
        return

    def _recv_loop(self):
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
                print(reply_msg, "/////////")
                # if len(reply_msg['path']) != 0:
                #     if self.topo_awareness is None:
                #         self.topo_awareness = lookup_service_brick('topo_awareness')
                #
                #     self.topo_awareness.install_flow_entry(reply_msg['path'], reply_msg['src_ip'],
                #                                       reply_msg['dst_ip'], reply_msg['in_port'],
                #                                       reply_msg['msg'])
            # if reply_msg['type'] == 'role':
            #     print("收到转换控制器角色指令")
            #     print(reply_msg, "/////////")
            #     hub.spawn(self.change_role, reply_msg['path'])
            if reply_msg['type'] == 'topo_reply':
                print("上传拓扑信息后返回ok")
                continue
        self.submit_socket_state = False
        print("exit recv_loop event")

    def send_info(self, msg):
        self._to_agent_send_q_sem.acquire()
        if self.to_agent_send_q:
            self.to_agent_send_q.put(bytes(msg.encode()))
        else:
            self._to_agent_send_q_sem.release()

    def add_route_req_info(self, msg):
        if self.submit_socket_state:
            req = json.dumps(msg)
            self.send_info(req)
    #
    # @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    # def _host_ip_packet_in_handle(self, ev):
    #     msg = ev.msg
    #     datapath = msg.datapath
    #     dpid = datapath.id
    #     in_port = msg.match['in_port']
    #     eth, pkt_type, pkt_data = ethernet.ethernet.parser(msg.data)
    #     src_mac = eth.src
    #     dst_mac = eth.dst
    #     if eth.ethertype == ether_types.ETH_TYPE_IP:
    #         pkt, _, _ = pkt_type.parser(pkt_data)
    #         src_ip = pkt.src
    #         dst_ip = pkt.dst
    #         if src_ip in SWITCHES_IP:
    #             return
    #
    #         if self.topo_awareness is None:
    #             self.topo_awareness = lookup_service_brick('topo_awareness')
    #
    #         dst_switch_id = self.topo_awareness.get_switch_id_by_ip(dst_ip)
    #         if dst_switch_id:
    #             # 本地控制器内进行解决
    #             src_switch_id = dpid
    #             path = self.topo_awareness.get_path(src_switch_id, dst_switch_id)
    #             print(path, "//////", type(path))
    #             if len(path) != 0:
    #                 self.topo_awareness.install_flow_entry(path, src_ip, dst_ip, in_port, msg)
    #         else:
    #             route_msg = {"type": "route",
    #                          "switch_id": dpid,
    #                          "src_ip": src_ip,
    #                          "dst_ip": dst_ip,
    #                          "in_port": in_port,
    #                          "msg": msg}
    #             self.add_route_req_info(route_msg)





