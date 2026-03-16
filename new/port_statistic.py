# Base
from ryu.app.zxf.new.topo_awareness import TopoAwareness
from ryu.base import app_manager


# Ofp
from ryu.base.app_manager import lookup_service_brick
from ryu.controller import ofp_event
from ryu.controller.handler import set_ev_cls, MAIN_DISPATCHER, DEAD_DISPATCHER

# Thread
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

from operator import attrgetter

Initial_bandwidth = 10


class PortStatistic(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    _CONTEXTS = {"topo_awareness": TopoAwareness}

    def __init__(self, *args, **kwargs):
        super(PortStatistic, self).__init__(*args, **kwargs)
        self.name = 'port_statistic'
        # self.topo_awareness = lookup_service_brick('topo_awareness')
        self.topo_awareness = kwargs['topo_awareness']
        self.dpid_to_switch = {}
        """ _port_stat_reply_handle """
        self.port_stats = {}        # {(dpid port_no): [(tx_packets, rx_packets ,tx_bytes, rx_bytes, rx_errors, duration_sec, duration_nsec),...]}
        self.delta_port_stats = {}  # {(dpid, port_no): [(delta_upload, delta_download, delta_error, period),... ]},... }

        """ _create_bandwidth_graph """
        self.free_bandwidth = {}  # {dpid: {port_no: (free_bandwidth, usage), ...}, ...}} (Mbit/s)

        """ _port_desc_stats_reply_handler """
        self.port_features = {}

        # Thread
        self.monitor_thread = hub.spawn(self._monitor_thread)
        self.save_freebandwidth_thread = hub.spawn(self._save_bw_graph)
        self.show_thread = hub.spawn(self.show_stat)

    # Thread:
    def _monitor_thread(self):
        while True:
            datapaths = self.dpid_to_switch.values()
            for dp in datapaths:
                self.port_features.setdefault(dp.id, {})
                self._request_stats(dp)
            hub.sleep(1)

    def _save_bw_graph(self):
        """
            Save bandwidth data into networkx graph object.
        """
        while True:
            self.graph = self._create_bandwidth_graph(self.free_bandwidth)
            self.logger.debug("save_freebandwidth")
            hub.sleep(1)

    # Stat request:
    def _request_stats(self, datapath):
        self.logger.debug('send stats request: %016x', datapath.id)
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

        req = parser.OFPPortDescStatsRequest(datapath, 0)
        datapath.send_msg(req)

    def _save_stats(self, _dict, key, value, history_length=2):
        if key not in _dict:
            _dict[key] = []

        _dict[key].append(value)

        if len(_dict[key]) > history_length:
            _dict[key].pop(0)

    def _cal_delta_stat(self, now, pre, period):
        if period:
            return (now - pre) / (period)
        else:
            return

    def _get_period(self, n_sec, n_nsec, p_sec, p_nsec):
        to_sec = lambda sec, nsec: sec + nsec / (10 ** 9)
        return to_sec(n_sec, n_nsec) - to_sec(p_sec, p_nsec)  # to seconds

    # Bandwidth graph:
    def _save_freebandwidth(self, dpid, port_no, speed):
        # Calculate free bandwidth of port and save it.
        # port_state = self.port_features.get(dpid).get(port_no)
        # if port_state:
        #     capacity = port_state[2] / (10 ** 3)  # Kbp/s to MBit/s
        #     speed = float(speed * 8) / (10 ** 6)  # byte/s to Mbit/s
        #     curr_bw = max(capacity - speed, 0)
        #     self.free_bandwidth[dpid].setdefault(port_no, None)
        #     self.free_bandwidth[dpid][port_no] = (curr_bw, speed)  # Save as Mbit/s
        # else:
        #     self.logger.warning("Fail in getting port state")
        capacity = Initial_bandwidth  # Kbp/s to Mbit/s
        speed = float(speed * 8) / (10 ** 6)  # byte/s to Mbit/s
        curr_bw = max(capacity - speed, 0)
        self.free_bandwidth[dpid].setdefault(port_no, None)
        self.free_bandwidth[dpid][port_no] = (curr_bw, speed)  # Save as Mbit/s

    def _create_bandwidth_graph(self, free_bandwidth):
        """
            Save bandwidth data into networkx graph object.
        """
        # if self.topo_awareness is None:
        #     self.topo_awareness = lookup_service_brick('topo_awareness')

        link_to_port = self.topo_awareness.topo_inter_link
        for link in link_to_port.keys():
            (src_dpid, dst_dpid) = link
            (src_port, _, _, _, _) = link_to_port[link]
            try:
                src_free_bandwidth, _ = free_bandwidth[src_dpid][src_port]
                self.topo_awareness.topo_inter_link[(src_dpid, dst_dpid)][3] = src_free_bandwidth
                self.topo_awareness.graph[src_dpid][dst_dpid]['free_bandwith'] = src_free_bandwidth
            except:
                pass

        link_to_port = self.topo_awareness.topo_access_link
        for link in link_to_port.keys():
            (src_dpid, dst_dpid) = link
            (src_port, _, _, _, _) = link_to_port[link]

            try:
                src_free_bandwidth, _ = free_bandwidth[src_dpid][src_port]
                self.topo_awareness.topo_access_link[(src_dpid, dst_dpid)][3] = src_free_bandwidth
                self.topo_awareness.graph[src_dpid][dst_dpid]['free_bandwith'] = src_free_bandwidth
            except:
                pass

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

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        """
            Save port's stats info
            Calculate port's speed and save it.
            port_stats: {(dpid port_no): [(tx_packets, rx_packets ,tx_bytes, rx_bytes, rx_errors, duration_sec, duration_nsec),...]}
            [history][stat_type]
            value is a tuple (tx_packets, rx_packets ,tx_bytes, rx_bytes, rx_errors)
                                  0          1           2         3          4
        """
        body = ev.msg.body
        dpid = ev.msg.datapath.id

        self.free_bandwidth.setdefault(dpid, {})

        # !FIXME: add rx_packets
        for stat in sorted(body, key=attrgetter('port_no')):
            port_no = stat.port_no
            if port_no != ofproto_v1_3.OFPP_LOCAL:

                key = (dpid, port_no)
                value = (stat.tx_packets, stat.rx_packets, stat.tx_bytes, stat.rx_bytes,
                         stat.rx_errors, stat.duration_sec, stat.duration_nsec)

                # Monitoring current port.
                self._save_stats(self.port_stats, key, value, 5)

                port_stats = self.port_stats[key]

                # if len(port_stats) == 1:
                #     self._save_stats(self.delta_port_stats, key, (
                #     stat.tx_packets, stat.rx_packets, stat.tx_bytes, stat.rx_bytes, stat.rx_errors), 5)

                if len(port_stats) > 1:
                    # curr_stat = port_stats[-1][2] + port_stats[-1][3]
                    # prev_stat = port_stats[-2][2] + port_stats[-2][3]
                    curr_stat = port_stats[-1][2]
                    prev_stat = port_stats[-2][2]

                    # period = self._get_period(port_stats[-1][5], port_stats[-1][6],
                    #                           port_stats[-2][5], port_stats[-2][6])

                    speed = self._cal_delta_stat(curr_stat, prev_stat, 5)

                    # Using maping to save detal_port_stats.
                    # self._save_stats(self.delta_port_stats, key,
                    #                  tuple(m(operator.sub, port_stats[-1], port_stats[-2])), 5)
                    # save free bandwidth (link capacity, can be used for load balancing, calculate link utilization) - Not work in mininet (reason: no link bandwidth)
                    self._save_freebandwidth(dpid, port_no, speed)

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def _port_desc_stats_reply_handler(self, ev):
        """
            Save port description info.
        """
        msg = ev.msg
        dpid = msg.datapath.id
        ofproto = msg.datapath.ofproto

        config_dict = {ofproto.OFPPC_PORT_DOWN: "Down",
                       ofproto.OFPPC_NO_RECV: "No Recv",
                       ofproto.OFPPC_NO_FWD: "No Farward",
                       ofproto.OFPPC_NO_PACKET_IN: "No Packet-in"}

        state_dict = {ofproto.OFPPS_LINK_DOWN: "Down",
                      ofproto.OFPPS_BLOCKED: "Blocked",
                      ofproto.OFPPS_LIVE: "Live"}

        ports = []
        for p in ev.msg.body:
            ports.append('port_no=%d hw_addr=%s name=%s config=0x%08x '
                         'state=0x%08x curr=0x%08x advertised=0x%08x '
                         'supported=0x%08x peer=0x%08x curr_speed=%d '
                         'max_speed=%d' %
                         (p.port_no, p.hw_addr,
                          p.name, p.config,
                          p.state, p.curr, p.advertised,
                          p.supported, p.peer, p.curr_speed,
                          p.max_speed))

            if p.config in config_dict:
                config = config_dict[p.config]
            else:
                config = "up"

            if p.state in state_dict:
                state = state_dict[p.state]
            else:
                state = "up"

            port_feature = (config, state, p.curr_speed)
            self.port_features[dpid][p.port_no] = port_feature

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status_handler(self, ev):
        """
            Handle the port status changed event.
        """
        msg = ev.msg
        reason = msg.reason
        port_no = msg.desc.port_no
        dpid = msg.datapath.id
        ofproto = msg.datapath.ofproto

        reason_dict = {
            ofproto.OFPPR_ADD: "added",
            ofproto.OFPPR_DELETE: "deleted",
            ofproto.OFPPR_MODIFY: "modified",
        }

        if reason in reason_dict:
            print("switch%d: port %s %s" %
                  (dpid, reason_dict[reason], port_no))
        else:
            print("switch%d: Illeagal port state %s %s" % (dpid, port_no, reason))

    """
        Accessor:
        return info as dict
    """

    def get_port_stats(self):
        if self.port_stats is None: return None
        stats = []
        port_stats = self.port_stats
        for dpid, port_no in port_stats:
            tx_packets, rx_packets, tx_bytes, rx_bytes, rx_errors, duration_sec, duration_nsec = \
            port_stats[(dpid, port_no)][-1]
            stats.append({
                'dpid': dpid,
                'port_no': port_no,
                'tx_packets': tx_packets,
                'rx_packets': rx_packets,
                'tx_bytes': tx_bytes,
                'rx_bytes': rx_bytes,
                'rx_error': rx_errors,
                'durration_sec': duration_sec,
                'duration_nsec': duration_nsec
            })
        return statsx

    def get_delta_port_stats(self):
        if self.delta_port_stats is None: return None
        stats = []
        delta_port_stats = self.delta_port_stats
        for dpid, port_no in delta_port_stats:
            tx_packets, rx_packets, tx_bytes, rx_bytes, rx_errors, duration_sec, duration_nsec = \
            delta_port_stats[(dpid, port_no)][-1]
            stats.append({
                'dpid': dpid,
                'port_no': port_no,
                'tx_packets': tx_packets,
                'rx_packets': rx_packets,
                'tx_bytes': tx_bytes,
                'rx_bytes': rx_bytes,
                'rx_error': rx_errors,
                'durration_sec': duration_sec,
                'duration_nsec': duration_nsec
            })
        return stats

    def show_stat(self):
        while True:
            print("222222222222222222222222222222222222222222")
            # print(self.port_features)
            print(self.free_bandwidth)
            # print(self.topo_awareness.graph.edges(data=True))
            hub.sleep(5)

    def _send_echo_request(self):
        datapaths = list(self.dpid_to_switch.values())
        for datapath in datapaths:
            # ... 发送echo请求 ...
            hub.sleep(0.5)  # 每个交换机的请求间隔0.5秒

# sudo mn --topo linear,4 --controller=remote,ip=localhost,port=6633 --switch ovsk --link tc,bw=0.1,delay=0ms,loss=10
# ryu-manager --observe-link --ofp-tcp-listen-port=6633 topo_awareness.py port_statistic.py
# http://www.muzixing.com/tag/ryu-bandwidth.html