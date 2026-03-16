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
CONTROLLER_IP = '172.17.0.2'
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
        self.to_agent_send_q = hub.Queue(16)  # 创建了一个最大长度为16的消息队列（最多可存16条消息），用于存储需要发送给某个代理的消息（控制器发送给主控制器）
        self._to_agent_send_q_sem = hub.BoundedSemaphore(self.to_agent_send_q.maxsize)  # 创建一个信号量初始值为16，消息被放入队列时，信号量值会减少
        self.select_master_flag = self.CONF.select_master
        self.controller_ip = CONTROLLER_IP
        self.lock = threading.Lock()  # 使用锁可以确保某个资源在同一时间只能被一个线程访问
        self.sub_lock = threading.Lock()  # 建的第二个锁，两个锁相互独立
        # self.thread = hub.spawn(self.print)
        self.thread = hub.spawn(self.start_main_fun)

    def start_main_fun(self):  # 启动一个主协程 start_main_fun，并在该协程内进一步启动另一个协程 _submit.通过这种方式可以实现非阻塞的异步操作
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

    def _submit(self):  # 连接到主控制器并启动消息处理线程
        print("---------------------------------------------------")
        hub.sleep(20)  # 等20秒再执行后面的代码，可能是为了等待某些资源准备好或进行初始化
        while self.sub_lock.acquire():
            # ip = self.select_master.get_leader_ip_by_id()
            ip = CONTROLLER_IP
            print("find the master service ip", ip)
            addr = (ip, 5001)
            while True:
                self.submit_socket = socket.socket()  # 创建一个新的 socket 对象
                self.submit_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.submit_socket_state = False  # 设置SO_REUSEADDR选项，允许socket关闭后立即重用该地址
                try:
                    self.submit_socket.connect(addr)
                    print("succeed !!!!")
                    self.submit_socket_state = True
                    break
                except socket.error as e:
                    print('Failed to connect to server:', e)
                    self.submit_socket.close()
                    hub.sleep(1)
                    continue   # 暂停 1 秒钟后重新尝试连接。循环会继续执行，尝试再次连接

            put_thr = hub.spawn(self._put_loop)
            send_thr = threading.Thread(target=self._recv_loop)  # 创建一个新的线程，目标是执行 _recv_loop 方法，监听某个套接字或通道，并处理接收到的消息
            send_thr.start()

            self._send_loop()

            hub.kill(put_thr)
            hub.joinall([put_thr])  # 用于等待 put_thr 协程的完成，确保在后续操作之前，该协程已经执行完毕。
            self.cut_connect()
            self.sub_lock.release()
            print("release lock")
            print(self.to_agent_send_q.empty())  # 检查消息队列 to_agent_send_q 是否为空，并打印结果。这个检查可以帮助确认在释放锁之前是否还有待处理的消息
            hub.sleep(2)   # 给系统留出时间进行必要的清理或状态更新

    def cut_connect(self):
        try:
            print("close socket !!!!!")
            self.submit_socket.shutdown(socket.SHUT_WR)  # 先关闭 socket写入方向
            self.submit_socket.close()  # 然后关闭整个 socket，释放与之关联的所有资源
            return
        except:
            return

    def _send_loop(self):  # 将消息从队列中取出并通过套接字发送给主控制器（连续运行的，只要 self.submit_socket_state 为真且队列中有消息就一直发送），处理的是队列中的所有类型的消息。
        print("enter _send_loop event")
        while self.submit_socket_state:
            try:
                try:
                    buf = self.to_agent_send_q.get(block=False)  # block=False该方法是非阻塞的。如果队列为空，get()方法不等待消息到达，立即抛出一个异常
                except hub.QueueEmpty:
                    hub.sleep(1)
                    continue  # 确保了当消息队列为空时，程序能够暂停并等待新的消息，而不执行后续的发送操作
                self._to_agent_send_q_sem.release()
                self.submit_socket.sendall(buf)
            except OSError:
                print("OSError send")
                break  # break发生时，跳出while循环。continue：跳过当前迭代，控制流返回到循环顶部继续下一次迭代。即返回到while重新判断
            except Exception as e:
                print("Socket error while sending data to agent", e)
                break
        self.submit_socket_state = False  # 将 submit_socket_state 属性设置为 False，通常用于标记发送循环的结束，防止再进行消息发送
        try:
            while self.to_agent_send_q.get(block=False):
                self._to_agent_send_q_sem.release()
        except hub.QueueEmpty:
            pass  # 执行 pass，即不进行任何操作，继续执行后续的代码
            print("exit send_loop event")

    def _put_loop(self):  # 定期将拓扑信息放入消息队列（每 2 秒执行一次）专门处理拓扑信息。
        print("enter put_loop event")
        while self.submit_socket_state:
            print("put topo info to queue")
            if self.topo_awareness is None:
                self.topo_awareness = lookup_service_brick('topo_awareness')

            links_info, hosts_info = self.topo_awareness.handle_topo_info_for_submit()  # 在topo_awareness.py中的530行
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
            req = json.dumps(topo_msg)  # 将Python对象topo_msg（这里是字典）转化为JSON字符串
            self.send_info(req)
            hub.sleep(2)
        print("exit put_loop event")
        return

    def _recv_loop(self):  # 接收来自主控制器的回复消息
        print("enter _recv_loop event")
        while self.submit_socket_state:
            try:
                ret = self.submit_socket.recv(2048)  # self.submit_socket，这是一个已创建的 socket 对象，用于与远程服务器（主控制器）进行网络通信
            except socket.timeout:
                break
            except (EOFError, IOError):  # EOFError: 表示接收到了一个 EOF（文件结束符），通常意味着对方已经关闭了连接。IOError: 通常表示输入/输出操作失败
                print("OSError recv")
                break

            if not ret:
                print("null null")
                break
            try:
                reply_msg = json.loads(ret)  # 将字符串 s 解码为 Python 对象
            except Exception as e:
                return
            if reply_msg['type'] == 'route_reply':  # 检查 reply_msg 字典中键 'type' 的值是否等于字符串 'route_reply'
                print(reply_msg['path'], "/////////")
                print(reply_msg, "/////////")  # 打印整个 reply_msg 字典及其内容
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
        self._to_agent_send_q_sem.acquire() # 获取信号量
        if self.to_agent_send_q:  # 检查队列是否存在且不为空，确保队列对象已经正确初始化
            self.to_agent_send_q.put(bytes(msg.encode()))  # 将消息编码为字节串，将序列化后的消息放入队列。如果队列已满，则此操作会阻塞，直到队列中有空间可用。
        else:
            self._to_agent_send_q_sem.release()  # 如果队列不存在或为空，释放信号量

    def add_route_req_info(self, msg):  # 处理路由请求信息并发送给某个目标
        if self.submit_socket_state:
            req = json.dumps(msg)  # 将 msg 字典序列化为 JSON 格式的字符串
            self.send_info(req)

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





