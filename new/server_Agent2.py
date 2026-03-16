import contextlib
import json
import logging
import socket

import networkx as nx
from ryu.base import app_manager
from ryu.lib import hub
from ryu.lib.hub import StreamServer, StreamClient
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event


# CONTROLLER_IP = '127.0.0.1'
CONTROLLER_IP = '172.17.0.9'
"""
增加了routenotice事件的定义
在一定基础上利用了hub中的部分函数，精简了代码

"""


class Route:
    def __init__(self, src_ip, dst_ip, path):
        self.src_ip = src_ip
        self.dst_ip = dst_ip
        self.path = path


class ServerTask(object):
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.server = None
        self.agent_send_q = hub.Queue(16)
        self._agent_send_q_sem = hub.BoundedSemaphore(self.agent_send_q.maxsize)

    def send_loop(self):
        self.server.logger.info("11111111111111111111")
        while True:
            try:
                buf = self.agent_send_q.get()
                self._agent_send_q_sem.release()
                self.sock.sendall(buf)
            except (EOFError, IOError):
                print("EOFError, IOError")
                break
            except Exception as e:
                self.server.logger.debug("Socket error while sending data to agent")
                continue

        try:
            while self.agent_send_q.get(block=False):
                self._agent_send_q_sem.release()
        except hub.QueueEmpty:
            pass
        self.close()

    def recv_loop(self):
        print("2222222222222222")
        controller_ip = self.addr[0]
        while True:
            try:
                recv_msg = self.sock.recv(10240)

            except (EOFError, IOError):
                print("EOFError, IOError,  recv")
                self.server.del_info(controller_ip)
                break

            except Exception as e:
                continue

            if not recv_msg:
                print("null null")
                self.server.del_info(controller_ip)
                break

            topo_data = json.loads(recv_msg)
            """
            topo_msg = {"type":topo/route,
                        "switches":switches, 
                        "master_switches":master_switches,
                        "link": links,
                        "host": hosts}
            switches = [dpid, 1, 2, 3,...]
            links = [(src_id,dst_id),(1,2),(1,3),...]
            hosts = [(dpid,host_ip),[1,'10.0.1.1'],[2,'10.0.1.2']....]
            """
            if topo_data['type'] == 'topo':
                print(f"连接到socket，地址为{self.addr},上传拓扑信息",topo_data)
                self.server.save_info(topo_data, self.addr)
                reply_msg = {'type': 'topo_reply'}
                rep = json.dumps(reply_msg)
                self.send_info(rep)
            if topo_data['type'] == 'route':
                print(f"收到socket连接，地址为{self.addr}，进行路由指令请求")
                src_ip = topo_data["src_ip"]
                dst_ip = topo_data["dst_ip"]
                dpid = topo_data["switch_id"]
                in_port = topo_data["in_port"]
                msg = topo_data["msg"]
                # path = self.server.get_path_from_topo(dpid, dst_ip)
                # reply_msg = {'type': 'route_reply',
                #              'path': path,
                #              'src_ip': src_ip,
                #              'dst_ip': dst_ip,
                #              'in_port': in_port,
                #              'msg': msg}
                reply_msg = {'type': 'route_reply',
                             'path': [1, 2, 5],
                             'src_ip': src_ip,
                             'dst_ip': dst_ip,
                             'in_port': in_port,
                             'msg': msg}
                rep = json.dumps(reply_msg)
                for addr in self.server.client:
                    self.server.client[addr].send_info(rep)

        self.close()

    def send_info(self, msg):
        self._agent_send_q_sem.acquire()
        if self.agent_send_q:
            self.agent_send_q.put(bytes(msg.encode()))
        else:
            self._agent_send_q_sem.release()

    def close(self):
        try:
            del self.server.client[self.addr[0]]
            self.sock.shutdown(socket.SHUT_WR)
            self.sock.close()
            self.server.logger.info("delete client")
            print("exit ")
        except:
            print("exit excrty")
            return


# class ServerAgent:
#     def __init__(self):
#         self.name = 'server_agent'
#         self.controller_ip = CONTROLLER_IP
#         self.logger = logging.getLogger(self.name)
#         self.client = {}
#         self.G = nx.DiGraph()
#         self.topo = {}
#         self.host = {}
#         self.controller_to_switches = {}
#         self.master_controller_to_switches = {}
#         self.server = StreamServer((CONTROLLER_IP, 5001), self._connect)
#
#     def _connect(self, sock, address):
#         print('connected address:%s' % str(address))
#         servertask = ServerTask(sock, address)
#         self.client[address[0]] = servertask
#         print("保存了对应的socket", self.client)
#
#         with contextlib.closing(servertask) as servers:
#             servers.server = self
#             # servers.msg_info_handle()
#             try:
#                 hub.spawn(servers.send_loop)
#                 servers.recv_loop()
#             except Exception as e:
#                 print(e)
#
#     def creat_topo(self):
#         hub.sleep(5)
#         while True:
#             self.G.clear()
#             topo_info = self.topo
#             if len(topo_info) != 0:
#                 for temp in topo_info.values():
#                     self.G.add_edges_from(temp)
#             # print("print !!!!!!!!!!")
#             # print(self.controller_to_switches)
#             # print(self.topo)
#             # print(self.host)
#             # print("print ending !!!!!!!!")
#             hub.sleep(5)
#
#     def get_switch_id_by_ip(self, ip_address):
#         print("error 2")
#         """
#         [[1, '1.1'], [1, '1.2'], [1, '1.3'], [2, '1.4'], [2, '1.5'], [7, '1.7'], [7, '6.2']]
#         """
#         try:
#             host_info = self.host
#             for hosts in host_info.values():
#                 for host in hosts:
#                     if ip_address == host[1]:
#                         return host[0]
#         except:
#             print("error 4")
#             return None
#
#     def get_path_from_topo(self, src_switch_id, dst_ip):
#         # TODO:进行IP地址与掩码的与运算，得出目标ip的网段地址
#         print("在这里对路径进行计算")
#         dst_switch_id = self.get_switch_id_by_ip(dst_ip)
#         print("error 1")
#         if dst_switch_id:
#             print("通过ip找到了对应的switch_id，直接路由到目标主机")
#             try:
#                 path = nx.dijkstra_path(self.G, src_switch_id, dst_switch_id)
#                 print("找到路径", path)
#                 return path
#             except:
#                 print("出错了，路径没找到")
#                 return []
#         else:
#             print("没有找到目标主机对应的switch_id")
#             return []
#
#     def save_info(self, data, addr):
#         controller_ip = addr[0]
#         self.topo[controller_ip] = data["link"]
#         self.host[controller_ip] = data["host"]
#         self.controller_to_switches[controller_ip] = data['switches']
#
#     def del_info(self, controller_ip):
#         if controller_ip in self.topo.keys():
#             del self.topo[controller_ip]
#             del self.host[controller_ip]
#             del self.controller_to_switches[controller_ip]
#
#     def begin(self):
#         print("///////start!!!!")
#         # hub.spawn(self.server.serve_forever)
#         # print("start creat topo !!1")
#         # self.creat_topo()
#         self.server.serve_forever()


class ServerAgent(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    _EVENTS = [event.EventMasterRouteNotice]

    def __init__(self, *args, **kwargs):
        super(ServerAgent, self).__init__(*args, **kwargs)
        self.name = 'server_agent'
        self.controller_ip = CONTROLLER_IP
        self.client = {}
        self.G = nx.DiGraph()
        self.topo = {}
        self.host = {}
        self.controller_to_switches = {}
        self.master_controller_to_switches = {}
        self.server = StreamServer((CONTROLLER_IP, 5001), self._connect)
        hub.spawn(self.begin)
        # self.begin()

    def _connect(self, sock, address):
        print('connected address:%s' % str(address))
        servertask = ServerTask(sock, address)
        self.client[address[0]] = servertask
        print("保存了对应的socket", self.client)

        with contextlib.closing(servertask) as servers:
            servers.server = self
            # servers.msg_info_handle()
            try:
                hub.spawn(servers.send_loop)
                servers.recv_loop()
            except Exception as e:
                print(e)

    def creat_topo(self):
        hub.sleep(5)
        while True:
            self.G.clear()
            topo_info = self.topo
            if len(topo_info) != 0:
                for temp in topo_info.values():
                    self.G.add_edges_from(temp)
            # print("print !!!!!!!!!!")
            # print(self.controller_to_switches)
            # print(self.topo)
            # print(self.host)
            # print("print ending !!!!!!!!")
            hub.sleep(5)

    def get_switch_id_by_ip(self, ip_address):
        print("error 2")
        """
        [[1, '1.1'], [1, '1.2'], [1, '1.3'], [2, '1.4'], [2, '1.5'], [7, '1.7'], [7, '6.2']]
        """
        try:
            host_info = self.host
            for hosts in host_info.values():
                for host in hosts:
                    if ip_address == host[1]:
                        return host[0]
        except:
            print("error 4")
            return None

    def get_path_from_topo(self, src_switch_id, dst_ip):
        # TODO:进行IP地址与掩码的与运算，得出目标ip的网段地址
        print("在这里对路径进行计算")
        dst_switch_id = self.get_switch_id_by_ip(dst_ip)
        print("error 1")
        if dst_switch_id:
            print("通过ip找到了对应的switch_id，直接路由到目标主机")
            try:
                path = nx.dijkstra_path(self.G, src_switch_id, dst_switch_id)
                print("找到路径", path)
                return path
            except:
                print("出错了，路径没找到")
                return []
        else:
            print("没有找到目标主机对应的switch_id")
            return []

    def save_info(self, data, addr):
        controller_ip = addr[0]
        self.topo[controller_ip] = data["link"]
        self.host[controller_ip] = data["host"]
        self.controller_to_switches[controller_ip] = data['switches']

    def del_info(self, controller_ip):
        if controller_ip in self.topo.keys():
            del self.topo[controller_ip]
            del self.host[controller_ip]
            del self.controller_to_switches[controller_ip]

    def begin(self):
        print("///////start!!!!")
        hub.spawn(self.server.serve_forever)
        # print("start creat topo !!1")
        # self.creat_topo()
