#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import logging
import json
import socket
import threading
import time
import signal
import networkx as nx
import traceback
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime
import tkinter as tk
from tkinter import ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import matplotlib.pyplot as plt

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),  # 输出到控制台
        logging.FileHandler("./server.log", mode='w', encoding='utf-8')  # 输出到文件,w模式覆盖原有日志, a模式追加
    ]
)
logger = logging.getLogger("server_agent")

# 配置参数
CONTROLLER_IP = '0.0.0.0'  # 监听所有网络接口
CONTROLLER_PORT = 5001
WEB_PORT = 5000  # REST API 端口

# 创建 Flask 应用
app = Flask(__name__)

# 启用 CORS，允许所有来源
CORS(app, resources={r"/api/*": {"origins": "*"}})

# 全局server_agent实例引用（在main()中初始化）
server_agent = None
        
@app.route('/')
def index():
    """提供Web可视化界面的HTML页面"""
    if server_agent is None:
        return '<h1>服务器未初始化</h1>', 503
    return server_agent._get_web_ui_html()

@app.route('/api/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    if server_agent is None:
        return jsonify({'error': 'Server not initialized'}), 503
    return jsonify({
        'status': 'ok',
        'controllers': len(server_agent.clients),
        'graph_nodes': len(server_agent.G.nodes()),
        'graph_edges': len(server_agent.G.edges())
    })

@app.route('/api/topo', methods=['GET'])
def get_topo():
    """获取完整的拓扑信息"""
    if server_agent is None:
        return jsonify({'error': 'Server not initialized'}), 503
    
    topo_data = {
        'switches': [],
        'links': [],
        'hosts': [],
        'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    # 收集所有交换机
    for switches in server_agent.controller_to_switches.values():
        topo_data['switches'].extend(switches)
    topo_data['switches'] = list(set(topo_data['switches']))  # 去重
    
    # 收集所有链路
    for links in server_agent.topo.values():
        topo_data['links'].extend(links)
    
    # 收集所有主机
    for hosts in server_agent.host.values():
        topo_data['hosts'].extend(hosts)
    
    return jsonify(topo_data)

@app.route('/api/controllers', methods=['GET'])
def get_controllers():
    """获取所有控制器信息"""
    if server_agent is None:
        return jsonify({'error': 'Server not initialized'}), 503
    
    # 将元组键转换为字符串以便JSON序列化
    controller_switches_str = {}
    for key, switches in server_agent.controller_to_switches.items():
        if isinstance(key, tuple):
            key_str = f"{key[0]}:{key[1]}"
        else:
            key_str = str(key)
        controller_switches_str[key_str] = switches
    
    controllers_data = {
        'active_controllers': [f"{addr[0]}:{addr[1]}" if isinstance(addr, tuple) else str(addr) 
                              for addr in server_agent.clients.keys()],
        'controller_switches': controller_switches_str
    }
    return jsonify(controllers_data)

@app.route('/api/graph', methods=['GET'])
def get_graph():
    """获取网络图信息"""
    if server_agent is None:
        return jsonify({'error': 'Server not initialized'}), 503
    
    try:
        import json
        # 获取节点列表（将非基础类型的ID转为字符串）
        nodes_list = []
        for node_id, node_data in server_agent.G.nodes(data=True):
            safe_id = node_id
            # 避免tuple等不可序列化ID
            try:
                json.dumps(node_id)
            except (TypeError, ValueError):
                safe_id = str(node_id)
            
            # 获取节点类型
            node_type = node_data.get('node_type', 'unknown')
            
            # 统计连接数量
            neighbors = list(server_agent.G.neighbors(node_id))
            connection_counts = {}
            
            if node_type == 'root_controller':
                # 根控制器：统计连接的从控制器数量
                controller_count = sum(1 for n in neighbors 
                                     if server_agent.G.nodes[n].get('node_type') == 'controller')
                connection_counts['controllers'] = controller_count
            elif node_type == 'controller':
                # 从控制器：统计连接的交换机数量
                switch_count = sum(1 for n in neighbors 
                                 if server_agent.G.nodes[n].get('node_type') == 'switch')
                connection_counts['switches'] = switch_count
            elif node_type == 'switch':
                # 交换机：统计连接的主机数量
                host_count = sum(1 for n in neighbors 
                               if server_agent.G.nodes[n].get('node_type') == 'host')
                connection_counts['hosts'] = host_count
                # 获取流表信息（如果有）
                flow_table = node_data.get('flow_table', [])
                node_data['flow_table'] = flow_table
                # 获取网关IP（如果有）
                gateway_ip = node_data.get('gateway_ip', '')
                node_data['gateway_ip'] = gateway_ip
            
            # 将统计信息添加到节点数据中
            node_data_with_stats = node_data.copy()
            node_data_with_stats['connection_counts'] = connection_counts
            
            nodes_list.append({
                'id': safe_id,
                'data': node_data_with_stats
            })
        
        # 获取边列表，转换为可序列化格式
        edges_list = []
        for src, dst, edge_data in server_agent.G.edges(data=True):
            # 处理端点ID的可序列化问题
            try:
                json.dumps(src)
            except (TypeError, ValueError):
                src = str(src)
            try:
                json.dumps(dst)
            except (TypeError, ValueError):
                dst = str(dst)

            edge_dict = {
                'source': src,
                'target': dst,
                'data': {}
            }
            # 复制边的属性，确保可序列化
            for key, value in (edge_data or {}).items():
                try:
                    json.dumps(value)
                    edge_dict['data'][key] = value
                except (TypeError, ValueError):
                    edge_dict['data'][key] = str(value)
            
            edges_list.append(edge_dict)
        
        graph_data = {
            'nodes': nodes_list,
            'edges': edges_list
        }
        
        logger.debug(f"API /api/graph 返回: {len(nodes_list)} 个节点, {len(edges_list)} 条边")
        return jsonify(graph_data)
    except Exception as e:
        logger.error(f"API /api/graph 错误: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e), 'nodes': [], 'edges': []}), 500

@app.route('/api/path', methods=['POST'])
def calculate_path():
    """计算路径"""
    if server_agent is None:
        return jsonify({'error': 'Server not initialized'}), 503
    
    data = request.get_json()
    src = data.get('src')
    dst = data.get('dst')
    
    if not src or not dst:
        return jsonify({'error': '需要提供源和目的节点'}), 400
    
    try:
        path = server_agent.handle_path_request({'src': src, 'dst': dst})
        return jsonify({'path': path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/statistics', methods=['GET'])
def get_statistics():
    """获取网络统计信息"""
    if server_agent is None:
        return jsonify({'error': 'Server not initialized'}), 503
    
    stats = {
        'controllers': len(server_agent.clients),
        'switches': sum(len(switches) for switches in server_agent.controller_to_switches.values()),
        'links': sum(len(links) for links in server_agent.topo.values()),
        'hosts': sum(len(hosts) for hosts in server_agent.host.values()),
        'graph_nodes': len(server_agent.G.nodes()),
        'graph_edges': len(server_agent.G.edges())
    }
    return jsonify(stats)


@app.route('/api/flows/add', methods=['POST'])
def add_flow():
    """下发流表（第一版：手动输入端口转发）"""
    if server_agent is None:
        return jsonify({'ok': False, 'message': 'Server not initialized'}), 503

    payload = request.get_json(silent=True) or {}
    # 期望 payload: {dpid, priority, match:{in_port}, actions:[{type:'OUTPUT', port}]}
    dpid = payload.get('dpid')
    match = payload.get('match') or {}
    actions = payload.get('actions') or []
    priority = payload.get('priority', 100)

    if dpid is None:
        return jsonify({'ok': False, 'message': 'Missing dpid'}), 400
    if 'in_port' not in match:
        return jsonify({'ok': False, 'message': 'Missing match.in_port'}), 400
    if not actions:
        return jsonify({'ok': False, 'message': 'Missing actions'}), 400

    app.logger.info(f"add_flow request: dpid={dpid}, priority={priority}, match={match}, actions={actions}")
    app.logger.info("add_flow controller_to_switches=%s", server_agent.controller_to_switches)
    app.logger.info("add_flow clients=%s", list(server_agent.clients.keys()))

    # 查找管理该交换机的控制器
    target_controller = None
    for controller_key, switches in server_agent.controller_to_switches.items():
        if dpid in switches:
            target_controller = controller_key
            break

    if target_controller is None:
        return jsonify({'ok': False, 'message': f'Controller not found for switch {dpid}'}), 404

    msg = {
        "type": "flow_add",
        "dpid": dpid,
        "priority": int(priority),
        "match": match,
        "actions": actions
    }

    app.logger.info("add_flow target_controller=%s for dpid=%s", target_controller, dpid)
    server_agent._send_to_controller(target_controller, msg)
    app.logger.info("add_flow sent flow_add to %s", target_controller)

    return jsonify({'ok': True, 'sent_to': [target_controller[0], target_controller[1]]})

@app.route('/api/switches/<int:dpid>/ports', methods=['GET'])
def get_switch_ports(dpid):
    """返回交换机端口列表（从 server_agent 当前 topo 链路里推断端口号）"""
    if server_agent is None:
        return jsonify({'ok': False, 'message': 'Server not initialized'}), 503

    ports = set()

    # server_agent.topo: {(controller_ip, port): [link_info...]}
    for _ctrl, links in (server_agent.topo or {}).items():
        for link in (links or []):
            if link.get('src') == dpid:
                p = link.get('src_port')
                if isinstance(p, int):
                    ports.add(p)

    ports = sorted(ports)
    return jsonify({'ok': True, 'dpid': dpid, 'ports': ports})
# ==================== ServerAgent类定义 ====================

class ServerAgent:
    """服务器代理，处理客户端连接和消息"""
    def __init__(self, ip, port):
        self.ip = ip
        self.port = port
        self.sock = None
        self.is_running = False
        self.clients = {}  # {client_addr: (socket, thread)}
        self.client_last_heartbeat = {}  # {client_addr: last_heartbeat_timestamp}
        self.client_lock = threading.Lock()  # 用于保护clients字典的线程锁
        
        # 心跳检测配置
        self.heartbeat_interval = 2  # 心跳检测间隔（秒）
        self.heartbeat_timeout = 6   # 3 个发送周期内未收到消息判定断联
        
        # 存储所有控制器的拓扑信息
        # 键使用(ip, port)元组以区分相同IP但不同端口的控制器
        self.topo = {}  # {(controller_ip, port): link_info}
        self.host = {}  # {(controller_ip, port): host_info}
        self.controller_to_switches = {}  # {(controller_ip, port): [switch_ids]}
        
        # 用于记录PortData查询请求的发起者
        # key: request_id, value: (请求控制器地址, 查询时间)
        self.portdata_query_requests = {}  # {request_id: (requester_addr, query_time)}
        
        # 用于路径计算的图
        self.G = nx.DiGraph()
        
        # 启动定时打印线程（使用单独的线程而不是hub）
        self.print_thread = threading.Thread(target=self.print_topo_info_loop)
        self.print_thread.daemon = True
        self.print_thread.start()

	#新修改：取消gui界面
        # 启动GUI界面
        #self.gui_thread = threading.Thread(target=self.start_gui)
        #self.gui_thread.daemon = True
        #self.gui_thread.start()
        
        # 启动心跳检测线程
        self.heartbeat_thread = threading.Thread(target=self.heartbeat_check_loop)
        self.heartbeat_thread.daemon = True
        self.heartbeat_thread.start()
        
        logger.info("初始化完成，定时打印线程已启动，心跳检测线程已启动")
        # print("初始化完成，定时打印线程已启动，心跳检测线程已启动")
 
    def _get_web_ui_html(self):
        """生成Web可视化界面的HTML页面"""
        html = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hierarchical SDN View - Root Controller</title>
    <script src="https://unpkg.com/vis-network@9.1.2/standalone/umd/vis-network.min.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen', 'Ubuntu', 'Cantarell', sans-serif;
            background: #020617;
            color: #e2e8f0;
            min-height: 100vh;
            overflow: hidden;
        }
        .app-container {
            display: flex;
            height: 100vh;
            overflow: hidden;
	    padding-top: 64px;
        }
        /* 顶部导航栏 */
        .header {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            height: 64px;
            background: rgba(15, 23, 42, 0.8);
            backdrop-filter: blur(12px);
            border-bottom: 1px solid #1e293b;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 24px;
            z-index: 100;
        }
        .header-left {
            display: flex;
            align-items: center;
            gap: 16px;
        }
        .header-icon {
            background: #d97706;
            padding: 8px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(217, 119, 6, 0.3);
        }
        .header-icon svg {
            width: 24px;
            height: 24px;
            fill: white;
        }
        .header-title {
            font-size: 20px;
            font-weight: 700;
            color: #f1f5f9;
        }
        .header-title span {
            color: #f59e0b;
        }
        .header-status {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
            color: #64748b;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
	.status-dot.ok { background: #22c55e; }   /* 正常：绿 */
	.status-dot.err { background: #ef4444; }  /* 异常：红 */
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        .header-metrics {
            display: flex;
            gap: 32px;
        }
        .metric-box {
            display: flex;
            align-items: center;
            gap: 12px;
            background: rgba(30, 41, 59, 0.5);
            padding: 8px 16px;
            border-radius: 8px;
            border: 1px solid rgba(51, 65, 85, 0.5);
        }
        .metric-icon {
            width: 16px;
            height: 16px;
        }
        .metric-content {
            display: flex;
            flex-direction: column;
        }
        .metric-label {
            font-size: 10px;
            color: #64748b;
            text-transform: uppercase;
            font-weight: 700;
            letter-spacing: 0.5px;
        }
        .metric-value {
            font-size: 14px;
            font-family: monospace;
            font-weight: 700;
            color: #e2e8f0;
        }
        /* 主内容区域 */
        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            position: relative;
        }
        /* 拓扑图区域 */
        .topology-area {
            flex: 1;
            position: relative;
            background: #020617;
            overflow: hidden;
            background-image: radial-gradient(#334155 1px, transparent 1px);
            background-size: 30px 30px;
        }
        #network {
            width: 100%;
            height: 100%;
        }
        /* 右侧信息面板 */
        .sidebar {
            width: 420px;
            background: #0f172a;
            border-left: 1px solid #1e293b;
            display: flex;
            flex-direction: column;
            box-shadow: -4px 0 24px rgba(0, 0, 0, 0.3);
            z-index: 50;
        }
        .sidebar-header {
            padding: 24px;
            border-bottom: 1px solid #1e293b;
            background: rgba(30, 41, 59, 0.3);
            display: flex;
            justify-content: space-between;
            align-items: start;
        }
        .sidebar-title-group {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .sidebar-icon {
            padding: 8px;
            border-radius: 8px;
        }
        .sidebar-icon.root { background: rgba(217, 119, 6, 0.2); color: #f59e0b; }
        .sidebar-icon.controller { background: rgba(59, 130, 246, 0.2); color: #60a5fa; }
        .sidebar-icon.switch { background: rgba(6, 182, 212, 0.2); color: #22d3ee; }
        .sidebar-icon.host { background: rgba(51, 65, 85, 0.2); color: #94a3b8; }
        .sidebar-title {
            font-size: 18px;
            font-weight: 700;
            color: white;
        }
        .sidebar-subtitle {
            font-size: 12px;
            color: #64748b;
            font-family: monospace;
            margin-top: 4px;
        }
        .sidebar-close {
            color: #64748b;
            cursor: pointer;
            padding: 4px;
            border-radius: 4px;
            transition: all 0.2s;
        }
        .sidebar-close:hover {
            color: white;
            background: #1e293b;
        }
        .sidebar-content {
            flex: 1;
            overflow-y: auto;
            padding: 24px;
        }
        .sidebar-section {
            margin-bottom: 32px;
        }
        .section-title {
            font-size: 12px;
            font-weight: 700;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 12px;
        }
        .info-card {
            background: rgba(30, 41, 59, 0.5);
            border-radius: 12px;
            padding: 16px;
            border: 1px solid rgba(51, 65, 85, 0.5);
        }
        .info-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 14px;
            margin-bottom: 12px;
        }
        .info-row:last-child {
            margin-bottom: 0;
        }
        .info-label {
            color: #94a3b8;
        }
        .info-value {
            font-family: monospace;
            color: #e2e8f0;
            font-weight: 500;
        }
        .info-value.highlight {
            background: rgba(51, 65, 85, 0.5);
            padding: 4px 8px;
            border-radius: 4px;
            font-weight: 700;
        }
        .info-value.error {
            color: #f87171;
        }
        .divider {
            height: 1px;
            background: rgba(51, 65, 85, 0.5);
            margin: 12px 0;
        }
        .empty-state {
            text-align: center;
            color: #64748b;
            margin-top: 80px;
        }
        .empty-state-icon {
            width: 64px;
            height: 64px;
            margin: 0 auto 16px;
            opacity: 0.2;
        }
        .flow-table {
            min-height: 200px;
        }
        .flow-item {
            background: rgba(30, 41, 59, 0.5);
            border: 1px solid rgba(51, 65, 85, 0.6);
            border-radius: 8px;
            padding: 14px;
            margin-bottom: 12px;
            transition: all 0.2s;
        }
        .flow-item:hover {
            border-color: rgba(59, 130, 246, 0.5);
        }
        .flow-header {
            display: flex;
            justify-content: space-between;
            align-items: start;
            margin-bottom: 10px;
        }
        .flow-priority {
            background: rgba(51, 65, 85, 0.5);
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 10px;
            font-family: monospace;
            color: #cbd5e0;
            border: 1px solid rgba(51, 65, 85, 0.3);
        }
        .flow-status {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: #22c55e;
            box-shadow: 0 0 5px rgba(34, 197, 94, 0.6);
        }
        .flow-delete {
            color: #64748b;
            cursor: pointer;
            padding: 4px;
            border-radius: 4px;
            opacity: 0;
            transition: all 0.2s;
        }
        .flow-item:hover .flow-delete {
            opacity: 1;
        }
        .flow-delete:hover {
            color: #f87171;
            background: rgba(51, 65, 85, 0.5);
        }
        .flow-details {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }
        .flow-detail-row {
            display: flex;
            gap: 8px;
            font-size: 12px;
        }
        .flow-detail-label {
            color: #64748b;
            font-weight: 500;
            width: 40px;
        }
        .flow-detail-value {
            font-family: monospace;
            flex: 1;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .flow-detail-value.match {
            color: #fbbf24;
        }
        .flow-detail-value.action {
            color: #22d3ee;
        }
        .flow-footer {
            margin-top: 12px;
            padding-top: 8px;
            border-top: 1px solid rgba(51, 65, 85, 0.3);
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 10px;
            color: #64748b;
        }
        .flow-packet-count {
            display: flex;
            align-items: center;
            gap: 4px;
            font-family: monospace;
            background: rgba(15, 23, 42, 0.3);
            padding: 4px 6px;
            border-radius: 4px;
        }
        .btn-add-flow {
            font-size: 12px;
            background: #2563eb;
            color: white;
            padding: 6px 12px;
            border-radius: 6px;
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s;
            box-shadow: 0 4px 12px rgba(37, 99, 235, 0.2);
        }
        .btn-add-flow:hover {
            background: #1d4ed8;
            transform: scale(0.98);
        }
        .btn-add-flow:active {
            transform: scale(0.95);
        }
    </style>
</head>
<body>
    <div class="app-container">
        <!-- 顶部导航栏 -->
        <header class="header">
            <div class="header-left">
                <div class="header-icon">
                    <svg viewBox="0 0 24 24" fill="currentColor">
                        <circle cx="12" cy="12" r="10"/>
                        <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
                    </svg>
                </div>
                <div>
                    <h1 class="header-title">Hierarchical <span>SDN View</span></h1>
                    <div class="header-status">
  			<span id="status-dot" class="status-dot ok"></span>
  			<span id="status" class="status connected"> 已连接</span>
		</div>
                </div>
                </div>
            <div class="header-metrics">
                <div class="metric-box">
                    <svg class="metric-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
                    </svg>
                    <div class="metric-content">
                        <span class="metric-label">Global Throughput</span>
                        <span class="metric-value" id="metric-throughput">0 Mbps</span>
            </div>
        </div>
                <div class="metric-box">
                    <svg class="metric-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
                    </svg>
                    <div class="metric-content">
                        <span class="metric-label">Avg Latency</span>
                        <span class="metric-value" id="metric-latency">0 ms</span>
            </div>
                </div>
                <div class="metric-box" style="cursor: pointer;" onclick="refreshTopology()" title="Refresh topology">
                    <div class="metric-content">
                        <span class="metric-label">Topology</span>
                        <span class="metric-value">Refresh</span>
                    </div>
                </div>
            </div>
        </header>
            
        <!-- 主内容区域 -->
        <div class="main-content">
            <!-- 拓扑图区域 -->
            <div class="topology-area">
            <div id="network"></div>
            </div>
        </div>

        <!-- 右侧信息面板 -->
        <div class="sidebar" id="sidebar">
            <div class="sidebar-header">
                <div class="sidebar-title-group">
                    <div class="sidebar-icon" id="sidebar-icon">
                        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <circle cx="12" cy="12" r="10"/>
                        </svg>
                </div>
                    <div>
                        <h2 class="sidebar-title" id="sidebar-title">Select Node</h2>
                        <p class="sidebar-subtitle" id="sidebar-subtitle">Click a node to view details</p>
                </div>
                </div>
                <div class="sidebar-close" onclick="closeSidebar()">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="18" y1="6" x2="6" y2="18"/>
                        <line x1="6" y1="6" x2="18" y2="18"/>
                    </svg>
                </div>
            </div>
            <div class="sidebar-content" id="sidebar-content">
                <div class="empty-state">
                    <svg class="empty-state-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <circle cx="12" cy="12" r="10"/>
                        <line x1="12" y1="8" x2="12" y2="12"/>
                        <line x1="12" y1="16" x2="12.01" y2="16"/>
                    </svg>
                    <p>Select a node from the topology</p>
                </div>
            </div>
        </div>
    </div>

    <script>
        let network = null;
        let nodes = null;
        let edges = null;
	let currentNodeId = null;
	let currentNodeData = null;
        let lastTopoSignature = null;

        // 创建SVG图标（基于lucide-react图标，与SDN.txt保持一致）
        function createIconSVG(iconType, color) {
            const svgMap = {
                'globe': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="' + color + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
                'server': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="' + color + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>',
                'network': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="' + color + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="M5 16v-6a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v6"/><path d="M12 12V8"/></svg>',
                'laptop': '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="' + color + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="12" rx="2" ry="2"/><line x1="2" y1="16" x2="22" y2="16"/><line x1="6" y1="20" x2="6.01" y2="20"/><line x1="10" y1="20" x2="10.01" y2="20"/><line x1="14" y1="20" x2="14.01" y2="20"/><line x1="18" y1="20" x2="18.01" y2="20"/></svg>'
            };
            return svgMap[iconType] || svgMap['laptop'];
        }
        
        // 将SVG转换为data URI
        function svgToDataURI(svgString) {
            const encoded = encodeURIComponent(svgString);
            return 'data:image/svg+xml;charset=utf-8,' + encoded;
        }
        
        // 初始化网络图
        function initNetwork() {
            try {
                console.log('开始初始化网络图...');
                
                // 检查vis库是否加载
                if (typeof vis === 'undefined') {
                    console.error('vis.js库未加载！');
                    document.getElementById('network').innerHTML = '<div style="padding: 50px; text-align: center; color: red;"><h2>错误：vis.js库加载失败</h2><p>请检查网络连接或使用离线版本</p></div>';
                    return;
                }
                
                console.log('vis.js库已加载');
                
                const container = document.getElementById('network');
		window.nodes = new vis.DataSet([]);
		window.edges = new vis.DataSet([]);
                
                console.log('DataSet创建完成');
                const data = { nodes: window.nodes, edges: window.edges };
                const options = {
                nodes: {
                    font: {
                        size: 12,
                        color: '#e2e8f0',
                        face: 'Arial',
                        //bold: true
                    },
                    borderWidth: 2,
                    shadow: {
                        enabled: true,
                        color: 'rgba(0,0,0,0.5)',
                        size: 10,
                        x: 2,
                        y: 2
                    },
                    chosen: {
                        node: function(values, id, selected, hovering) {
                            if (selected || hovering) {
                                values.borderWidth = 4;
                                values.shadow = true;
                            }
                        }
                    },
                    shapeProperties: {
                        useBorderWithImage: true
                    }
                },
                edges: {
                    width: 2,
                    color: {
                        color: '#475569',
                        highlight: '#60a5fa',
                        hover: '#60a5fa'
                    },
                    shadow: {
                        enabled: true,
                        color: 'rgba(0,0,0,0.3)',
                        size: 5
                    },
                    smooth: {
                        enabled: true,
                        type: 'curvedCW',
                        roundness: 0.2
                    },
                    arrows: {
                        to: {
                            enabled: true,
                            scaleFactor: 0.6,
                            type: 'arrow'
                        }
                    }
                },
			layout: {
			  hierarchical: {
			    enabled: false
			  }
			},
			physics: {
			  enabled: false
			},

                interaction: {
                    hover: true,
                    tooltipDelay: 100,
                    dragNodes: true,
                    dragView: true,
                    zoomView: true,
                    selectConnectedEdges: true
                },
                configure: {
                    enabled: false
                }
            };
            
                console.log('开始创建vis.Network...');
		window.network = new vis.Network(container, data, options);
                console.log('vis.Network创建完成');
                
                // 节点点击事件
		window.network.on('click', function (params) {
		  if (params.nodes.length > 0) {
		    const nodeId = params.nodes[0];

		    showNodeInfo(nodeId);

		    window.currentNodeId = nodeId;
		    window.currentNodeData = window.nodes.get(nodeId);

		    console.log('currentNodeData:', window.currentNodeData);

		    document.getElementById('sidebar').style.display = 'flex';
		  }
		});
                
                console.log('事件监听器已设置');
                
                // 加载拓扑
                console.log('准备加载拓扑数据...');
                refreshTopology();
                
                // 自动刷新（每5秒）disable to increase speed
                //setInterval(refreshTopology, 30000);
                //console.log('自动刷新已启用（每30秒）');
                
            } catch (err) {
                console.error('初始化网络图失败:', err);
                document.getElementById('network').innerHTML = '<div style="padding: 50px; text-align: center; color: red;"><h2>初始化失败</h2><p>' + err.message + '</p></div>';
            }
        }
        
        // 刷新拓扑数据
        async function refreshTopology() {
            try {
                console.log('正在获取拓扑数据...');
                const response = await fetch('/api/graph');
                
                if (!response.ok) {
                    throw new Error(`HTTP错误: ${response.status} ${response.statusText}`);
                }
                
                const data = await response.json();
                console.log('成功获取拓扑数据:', data);
                
                updateNetwork(data);
                updateStatistics();

		const dot = document.getElementById('status-dot');
		if (dot) dot.className = 'status-dot ok';
                
                document.getElementById('status').className = 'status connected';
                document.getElementById('status').textContent = '已连接';
            } catch (error) {
                console.error('获取拓扑数据失败:', error);
                console.error('错误详情:', error.message);

		const dot = document.getElementById('status-dot');
		if (dot) dot.className = 'status-dot err';

                document.getElementById('status').className = 'status error';
                document.getElementById('status').textContent = '连接错误: ' + error.message;
            }
        }
        window.refreshTopology = refreshTopology;
        // 更新网络图
	function updateNetwork(data) {
	    try {
		const nodes = window.nodes;
		const edges = window.edges;
		if (!nodes || !edges) throw new Error('window.nodes/window.edges not initialized');

		const graphNodes = data.nodes || [];
		const graphEdges = data.edges || [];

		console.log('收到拓扑数据:', data);
		console.log('节点数量:', graphNodes.length);
		console.log('边数量:', graphEdges.length);

		// === 拓扑签名：用于判断是否需要重新布局 ===
		const nodeIds = graphNodes.map(n => (n.id || n)).sort();
		const edgePairs = graphEdges.map(e => `${e.source}->${e.target}`).sort();
		const topoSignature = nodeIds.join('|') + '||' + edgePairs.join('|');

		const topoChanged = (topoSignature !== lastTopoSignature);
		lastTopoSignature = topoSignature;

		console.log('topoChanged =', topoChanged);

		// 清空现有数据
		nodes.clear();
		edges.clear();
                
                // 添加节点
                let addedNodes = 0;
                // 按节点类型分组，用于编号
                const nodeTypeCounters = {
                    'root_controller': 0,
                    'controller': 0,
                    'switch': 0,
                    'host': 0,
                    'unknown': 0
                };
                
                graphNodes.forEach((nodeObj, index) => {
                    try {
                        // 适配新的数据格式：{id: ..., data: {...}}
                        const nodeId = nodeObj.id || nodeObj;
                        const nodeData = nodeObj.data || {};
                        const nodeType = nodeData.node_type || 'unknown';
                        
                        let color, size, iconType, label, nodeNumber, iconColor;
                        
                        // 根据节点类型设置样式和编号（使用SDN.txt风格的颜色和图标）
                        if (nodeId === 'RootController' || nodeType === 'root_controller') {
                            color = { background: '#92400e', border: '#f59e0b', highlight: { background: '#b45309', border: '#fbbf24' } };
                            size = 56;  // 对应w-14 h-14 (56px)
                            iconType = 'globe';
                            iconColor = '#f59e0b';
                            nodeTypeCounters['root_controller']++;
                            nodeNumber = nodeTypeCounters['root_controller'];
                            label = 'Root ' + nodeNumber;
                        } else if (nodeType === 'controller') {
                            color = { background: '#1e3a8a', border: '#3b82f6', highlight: { background: '#1e40af', border: '#60a5fa' } };
                            size = 56;  // 对应w-14 h-14 (56px)
                            iconType = 'server';
                            iconColor = '#60a5fa';
                            nodeTypeCounters['controller']++;
                            nodeNumber = nodeTypeCounters['controller'];
                            label = 'Ctrl-' + nodeNumber;
                        } else if (nodeType === 'switch') {
                            color = { background: '#164e63', border: '#06b6d4', highlight: { background: '#155e75', border: '#22d3ee' } };
                            size = 48;  // 对应w-12 h-12 (48px)
                            iconType = 'network';
                            iconColor = '#22d3ee';
                            nodeTypeCounters['switch']++;
                            nodeNumber = nodeTypeCounters['switch'];
                            label = 'SW' + nodeNumber;
                        } else if (nodeType === 'host') {
                            color = { background: '#1e293b', border: '#475569', highlight: { background: '#334155', border: '#64748b' } };
                            size = 32;  // 对应w-8 h-8 (32px)
                            iconType = 'laptop';
                            iconColor = '#94a3b8';
                            nodeTypeCounters['host']++;
                            nodeNumber = nodeTypeCounters['host'];
                            label = 'H' + nodeNumber;
                        } else {
                            // 未知类型
                            color = { background: '#1e293b', border: '#64748b', highlight: { background: '#334155', border: '#94a3b8' } };
                            size = 32;
                            iconType = 'laptop';
                            iconColor = '#94a3b8';
                            nodeTypeCounters['unknown']++;
                            nodeNumber = nodeTypeCounters['unknown'];
                            label = 'Unknown' + nodeNumber;
                        }
                        
                        // 创建图标SVG并转换为data URI
                        const iconSVG = createIconSVG(iconType, iconColor);
                        const iconDataURI = svgToDataURI(iconSVG);
                        
                        console.log(`添加节点 ${index}: ID=${nodeId}, Type=${nodeType}, Label=${label}, Icon=${iconType}`);
                        
                        // 存储完整的节点信息，包括原始数据和统计信息
                        nodes.add({
                            id: nodeId,
                            label: label,
                            color: color,
                            size: size,
                            shape: 'image',  // 使用image形状
                            image: iconDataURI,  // 设置图标
                            brokenImage: iconDataURI,  // 备用图标
                            title: label,
                            nodeType: nodeType,
                            nodeNumber: nodeNumber,
                            nodeData: nodeData  // 存储完整的节点数据
                        });
                        
                        addedNodes++;
                    } catch (err) {
                        console.error('添加节点失败:', nodeObj, err);
                    }
                });
                
                console.log('已添加节点数:', addedNodes, '/', graphNodes.length);
            
                // 添加边
                let addedEdges = 0;
                graphEdges.forEach((edgeObj, index) => {
                    try {
                        // 适配新的数据格式：{source: ..., target: ..., data: {...}}
                        let source, target, edgeData;
                        
                        if (edgeObj.source !== undefined && edgeObj.target !== undefined) {
                            // 新格式
                            source = edgeObj.source;
                            target = edgeObj.target;
                            edgeData = edgeObj.data || {};
                        } else if (Array.isArray(edgeObj) && edgeObj.length >= 2) {
                            // 旧格式（兼容）
                            [source, target, edgeData] = edgeObj;
                        } else {
                            console.warn('无效的边格式:', edgeObj);
                            return;
                        }
                        
                        const edgeType = edgeData?.edge_type || 'unknown';
                        
                        let color, width, dashes, smooth;
                        
                        if (edgeType === 'controller_connection') {
                            color = { color: '#d97706', highlight: '#f59e0b', hover: '#fbbf24' };
                            width = 3;
                            dashes = [10, 5];
                            smooth = { type: 'curvedCW', roundness: 0.2 };
                        } else if (edgeType === 'controller_switch') {
                            color = { color: '#3b82f6', highlight: '#60a5fa', hover: '#93c5fd' };
                            width = 2.5;
                            dashes = [5, 5];
                            smooth = { type: 'cubicBezier', roundness: 0.3 };
                        } else if (edgeType === 'host_switch') {
                            color = { color: '#64748b', highlight: '#94a3b8', hover: '#cbd5e0' };
                            width = 1.5;
                            dashes = false;
                            smooth = { type: 'continuous' };
                        } else if (edgeType === 'switch_link') {
                            color = { color: '#06b6d4', highlight: '#22d3ee', hover: '#67e8f9' };
                            width = 2.5;
                            dashes = false;
                            smooth = { type: 'curvedCW', roundness: 0.4 };
                        } else {
                            color = { color: '#475569', highlight: '#64748b', hover: '#94a3b8' };
                            width = 2;
                            dashes = false;
                            smooth = { type: 'curvedCW', roundness: 0.2 };
                        }
                        
                        console.log(`添加边 ${index}: ${source} -> ${target} (${edgeType})`);
                        
                        edges.add({
                            id: `edge-${index}`,
                            from: source,
                            to: target,
                            color: color,
                            width: width,
                            dashes: dashes,
                            smooth: smooth,
                            title: `${source} -> ${target}`,
                            data: { edge_type: edgeType }  // 保存边类型
                        });
                        
                        addedEdges++;
                    } catch (err) {
                        console.error('添加边失败:', edgeObj, err);
                    }
                });
                
                console.log('已添加边数:', addedEdges, '/', graphEdges.length);
                
                // 应用自定义分层布局
		if (topoChanged) {
		  console.log('开始应用自定义分层布局...');
		  // applyCustomLayout();

		  // 只在拓扑变化时做一次 fit（否则每次刷新都动画会卡）
		if (window.network) {
  		window.network.fit({ animation: false });
		}
		} else {
		  console.log('拓扑未变化，跳过布局与fit');
		}
                console.log('拓扑布局完成');
            } catch (err) {
                console.error('updateNetwork失败:', err);
            }
        }
        
        // 自定义分层布局函数
	function applyCustomLayout() {
	  try {
	    console.log('计算自定义布局...');

	    // 关键：统一使用 window 上的 dataset/network，避免外部 nodes 为 null
	    const nodes = window.nodes;
	    const edges = window.edges;
	    const network = window.network;
	    if (!nodes || !network) throw new Error('window.nodes or window.network not initialized');

	    // 收集各层节点（使用nodeType属性而不是shape）
	    const rootNodes = [];
	    const controllerNodes = [];
	    const switchNodes = [];
	    const hostNodes = [];

	    nodes.get().forEach(node => {
	      const nodeType = node.nodeType || 'unknown';
	      if (node.id === 'RootController' || nodeType === 'root_controller') {
		rootNodes.push(node);
	      } else if (nodeType === 'controller') {
		controllerNodes.push(node);
	      } else if (nodeType === 'switch') {
		switchNodes.push(node);
	      } else if (nodeType === 'host') {
		hostNodes.push(node);
	      }
	    });
                
                console.log(`节点分布 - 根:${rootNodes.length}, 从控:${controllerNodes.length}, 交换机:${switchNodes.length}, 主机:${hostNodes.length}`);
                
                // 构建交换机-主机组（交换机与其连接的主机作为一个整体）
                const switchGroups = {};  // {switchId: [hostIds]}
                
                // 找出每个交换机连接的主机
                edges.get().forEach(edge => {
                    const edgeData = edge.data || {};
                    const fromNode = nodes.get(edge.from);
                    const toNode = nodes.get(edge.to);
                    
                    // 检查是否是主机-交换机连接
                    if (edgeData.edge_type === 'host_switch' || 
                        (fromNode && toNode && 
                         ((fromNode.nodeType === 'switch' && toNode.nodeType === 'host') ||
                          (fromNode.nodeType === 'host' && toNode.nodeType === 'switch')))) {
                        
                        const switchId = (fromNode?.nodeType === 'switch') ? edge.from : edge.to;
                        const hostId = (fromNode?.nodeType === 'host') ? edge.from : edge.to;
                        
                        if (switchId && hostId) {
                            if (!switchGroups[switchId]) {
                                switchGroups[switchId] = [];
                            }
                            if (!switchGroups[switchId].includes(hostId)) {
                                switchGroups[switchId].push(hostId);
                            }
                        }
                    }
                });
                
                console.log('交换机-主机组:', switchGroups);
                
                // 布局参数（可调整以获得最佳视觉效果）
                const canvasWidth = 2400;      // 画布宽度
                const canvasHeight = 1400;     // 画布高度
                const layerHeight = 350;       // 层与层之间的垂直间距
                const nodeSpacing = 250;       // 同一层节点之间的水平间距
                const maxNodesPerRow = 10;     // 每行最多节点数（超过则分多行）
                const rowSpacing = 200;        // 多行时的行间距
                const hostOffset = 120;        // 主机相对于交换机的垂直偏移
                
                // ========== 第0层：根控制器 ==========
                // 位置：顶部中心
                const rootY = 0;
                rootNodes.forEach((node, index) => {
                    console.log(`放置根控制器: ${node.id} at (${canvasWidth/2}, ${rootY})`);
                    nodes.update({
                        id: node.id,
                        x: canvasWidth / 2,
                        y: rootY,
                        fixed: true
                    });
                });
                
                // ========== 第1层：从控制器 ==========
                // 位置：第二层，水平等间距排列，超过maxNodesPerRow则分多行
                const controllerY = rootY + layerHeight;
                const controllerCount = controllerNodes.length;
                const controllerRowCount = Math.ceil(controllerCount / maxNodesPerRow);
                
                console.log(`放置 ${controllerCount} 个从控制器，分 ${controllerRowCount} 行`);
                
                controllerNodes.forEach((node, index) => {
                    const rowIndex = Math.floor(index / maxNodesPerRow);
                    const colIndex = index % maxNodesPerRow;
                    const nodesInRow = Math.min(maxNodesPerRow, controllerCount - rowIndex * maxNodesPerRow);
                    
                    // 计算该行的起始位置（居中）
                    const rowWidth = (nodesInRow - 1) * nodeSpacing;
                    const startX = (canvasWidth - rowWidth) / 2;
                    const x = startX + colIndex * nodeSpacing;
                    const y = controllerY + rowIndex * rowSpacing;
                    
                    console.log(`  从控 ${index}: ${node.id} at (${x}, ${y})`);
                    
                    nodes.update({
                        id: node.id,
                        x: x,
                        y: y,
                        fixed: true
                    });
                });
                
                // ========== 第2层：交换机-主机组 ==========
                // 策略：交换机与其连接的主机视为一个组，组作为整体水平排列
                // 位置：交换机在上，主机在交换机正下方（hostOffset距离）
                const switchLayerY = controllerY + layerHeight + (controllerRowCount > 1 ? rowSpacing : 0);
                
                // 创建组列表（每个组包含一个交换机和其主机）
                const groups = [];
                const assignedHosts = new Set();
                
                // 为每个交换机创建组
                switchNodes.forEach(switchNode => {
                    const group = {
                        switch: switchNode,
                        hosts: switchGroups[switchNode.id] || []
                    };
                    groups.push(group);
                    
                    // 标记已分配的主机
                    group.hosts.forEach(hostId => assignedHosts.add(hostId));
                });
                
                // 添加未分配的主机为独立组（没有连接到任何交换机的主机）
                hostNodes.forEach(hostNode => {
                    if (!assignedHosts.has(hostNode.id)) {
                        groups.push({
                            switch: null,
                            hosts: [hostNode.id]
                        });
                    }
                });
                
                console.log(`共 ${groups.length} 个交换机-主机组`);
                
                // 布局组（支持多行，每行居中等间距排列）
                const groupCount = groups.length;
                const groupRowCount = Math.ceil(groupCount / maxNodesPerRow);
                
                console.log(`开始放置 ${groupCount} 个组，分 ${groupRowCount} 行`);
                
                groups.forEach((group, index) => {
                    // 计算组在第几行、第几列
                    const rowIndex = Math.floor(index / maxNodesPerRow);
                    const colIndex = index % maxNodesPerRow;
                    const groupsInRow = Math.min(maxNodesPerRow, groupCount - rowIndex * maxNodesPerRow);
                    
                    // 计算该行的起始X坐标（使该行居中）
                    const rowWidth = (groupsInRow - 1) * nodeSpacing;
                    const startX = (canvasWidth - rowWidth) / 2;
                    const groupX = startX + colIndex * nodeSpacing;
                    const groupBaseY = switchLayerY + rowIndex * (rowSpacing + hostOffset);
                    
                    // 放置交换机（组的上部）
                    if (group.switch) {
                        console.log(`  组 ${index}: 交换机 ${group.switch.id} at (${groupX}, ${groupBaseY}), 主机数: ${group.hosts.length}`);
                        nodes.update({
                            id: group.switch.id,
                            x: groupX,
                            y: groupBaseY,
                            fixed: true
                        });
                    }
                    
                    // 放置主机（在交换机下方，作为一个整体）
                    const hostCount = group.hosts.length;
                    if (hostCount > 0) {
                        if (hostCount === 1) {
                            // 单个主机：直接在交换机正下方
                            nodes.update({
                                id: group.hosts[0],
                                x: groupX,
                                y: groupBaseY + hostOffset,
                                fixed: true
                            });
                        } else {
                            // 多个主机：以交换机为中心水平分布
                            const hostSpacing = 80;
                            const hostRowWidth = (hostCount - 1) * hostSpacing;
                            const hostStartX = groupX - hostRowWidth / 2;
                            
                            group.hosts.forEach((hostId, hostIndex) => {
                                const hostX = hostStartX + hostIndex * hostSpacing;
                                const hostY = groupBaseY + hostOffset;
                                
                                nodes.update({
                                    id: hostId,
                                    x: hostX,
                                    y: hostY,
                                    fixed: true
                                });
                            });
                        }
                    }
                });
                
                console.log('自定义布局应用完成');
                
            } catch (err) {
                console.error('应用自定义布局失败:', err);
            }
        }
        
        // 更新统计信息
        async function updateStatistics() {
            try {
                const response = await fetch('/api/statistics');
                const stats = await response.json();
                
                // 计算全局指标（简化计算）
                const totalThroughput = (stats.switches || 0) * 100; // 假设每个交换机100Mbps
                const avgLatency = 10 + Math.floor(Math.random() * 10); // 模拟延迟
                
                document.getElementById('metric-throughput').textContent = totalThroughput + ' Mbps';
                document.getElementById('metric-latency').textContent = avgLatency + ' ms';
            } catch (error) {
                console.error('获取统计信息失败:', error);
            }
        }
        
        // 测试API连接
        async function testAPI() {
            console.log('=== 开始API测试 ===');
            
            // 测试健康检查
            try {
                console.log('测试 /api/health...');
                const healthResp = await fetch('/api/health');
                const healthData = await healthResp.json();
                console.log('✓ 健康检查成功:', healthData);
                alert('API连接正常！\\n控制器数: ' + healthData.controllers + '\\n图节点数: ' + healthData.graph_nodes + '\\n图边数: ' + healthData.graph_edges);
            } catch (error) {
                console.error('✗ 健康检查失败:', error);
                alert('API连接失败！\\n请检查：\\n1. 根控制器是否运行\\n2. 浏览器控制台查看详细错误\\n3. 确认端口5000未被占用');
                return;
            }
            
            // 测试图数据
            try {
                console.log('测试 /api/graph...');
                const graphResp = await fetch('/api/graph');
                const graphData = await graphResp.json();
                console.log('✓ 图数据获取成功:', graphData);
                console.log(`  节点数: ${graphData.nodes.length}`);
                console.log(`  边数: ${graphData.edges.length}`);
            } catch (error) {
                console.error('✗ 图数据获取失败:', error);
            }
            
            // 测试统计信息
            try {
                console.log('测试 /api/statistics...');
                const statsResp = await fetch('/api/statistics');
                const statsData = await statsResp.json();
                console.log('✓ 统计信息获取成功:', statsData);
            } catch (error) {
                console.error('✗ 统计信息获取失败:', error);
            }
            
            console.log('=== API测试完成 ===');
        }
        
        // 显示节点信息
	function showNodeInfo(nodeId) {
	    const nodes = window.nodes;          // 关键：用 window.nodes
	    if (!nodes) {
		console.error('showNodeInfo: window.nodes is not initialized');
		return;
	    }

	    const node = nodes.get(nodeId);
	    if (!node) return;

	    const nodeType = node.nodeType || 'unknown';
	    const nodeData = node.nodeData || {};
	    const connectionCounts = nodeData.connection_counts || {};
            
            // 更新侧边栏标题
            const sidebarTitle = document.getElementById('sidebar-title');
            const sidebarSubtitle = document.getElementById('sidebar-subtitle');
            const sidebarIcon = document.getElementById('sidebar-icon');
            const sidebarContent = document.getElementById('sidebar-content');
            
            // 设置图标和标题
            let iconClass = '';
            let title = '';
            let subtitle = node.id;
            
            if (nodeType === 'root_controller') {
                iconClass = 'root';
                title = 'Root Controller';
            } else if (nodeType === 'controller') {
                iconClass = 'controller';
                title = 'Sub Controller';
            } else if (nodeType === 'switch') {
                iconClass = 'switch';
                title = 'OpenFlow Switch';
            } else if (nodeType === 'host') {
                iconClass = 'host';
                title = 'End Host';
            } else {
                iconClass = 'host';
                title = 'Unknown Node';
            }
            
            sidebarTitle.textContent = title;
            sidebarSubtitle.textContent = subtitle;
            sidebarIcon.className = 'sidebar-icon ' + iconClass;
            
            // 生成内容HTML
            let html = '';
            
            // 基本信息部分
            html += '<div class="sidebar-section">';
            html += '<h3 class="section-title">Basic Info</h3>';
            html += '<div class="info-card">';
            
            if (nodeType === 'root_controller') {
                html += createInfoRow('IP Address', nodeData.ip || 'N/A');
                html += createInfoRow('Node Type', 'Root Controller');
                html += createInfoRow('Connected Controllers', (connectionCounts.controllers || 0).toString());
            } else if (nodeType === 'controller') {
                html += createInfoRow('IP Address', nodeData.ip || 'N/A');
                html += createInfoRow('Port', (nodeData.port || 'N/A').toString());
                html += createInfoRow('Node Type', 'Sub Controller');
                html += createInfoRow('Connected Switches', (connectionCounts.switches || 0).toString());
            } else if (nodeType === 'switch') {
                html += createInfoRow('IP Address', nodeData.ip || node.id || 'N/A');
                if (nodeData.gateway_ip) {
                    html += createInfoRow('Gateway IP', nodeData.gateway_ip);
                }
                html += createInfoRow('DPID', node.id || 'N/A');
                html += '<div class="divider"></div>';
                // 交换机实时指标（如果有）
                if (nodeData.throughput !== undefined) {
                    html += createInfoRow('Throughput', (nodeData.throughput || 0) + ' Mbps', true);
                }
                if (nodeData.latency !== undefined) {
                    html += createInfoRow('Latency', (nodeData.latency || 0) + ' ms');
                }
                if (nodeData.loss !== undefined) {
                    html += createInfoRow('Packet Loss', (nodeData.loss || 0) + '%', false, true);
                }
                html += createInfoRow('Connected Hosts', (connectionCounts.hosts || 0).toString());
            } else if (nodeType === 'host') {
                html += createInfoRow('IP Address', node.id || 'N/A');
                if (nodeData.mac) {
                    html += createInfoRow('MAC', nodeData.mac);
                }
                html += createInfoRow('Node Type', 'End Host');
            }
            
            html += '</div>';
            html += '</div>';
            
            // 流表部分（仅交换机）
            if (nodeType === 'switch') {
                html += '<div class="sidebar-section">';
                html += '<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">';
                html += '<h3 class="section-title">Flow Tables</h3>';
                html += '<button class="btn-add-flow" onclick="showAddFlowModal()">';
                html += '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
                html += '添加规则';
                html += '</button>';
                html += '</div>';
                html += '<div class="flow-table">';
                
                const flowTable = nodeData.flow_table || [];
                if (flowTable.length > 0) {
                    flowTable.forEach((flow, idx) => {
                        html += createFlowItem(flow, node.id, idx);
                    });
                } else {
                    html += '<div class="empty-state" style="margin-top: 20px;">';
                    html += '<svg class="empty-state-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">';
                    html += '<circle cx="12" cy="12" r="10"/>';
                    html += '<line x1="12" y1="8" x2="12" y2="12"/>';
                    html += '<line x1="12" y1="16" x2="12.01" y2="16"/>';
                    html += '</svg>';
                    html += '<p style="font-size: 14px; color: #64748b;">暂无流表规则</p>';
                    html += '<p style="font-size: 12px; color: #475569; margin-top: 4px;">点击上方按钮添加第一条规则</p>';
                    html += '</div>';
                }
                
                html += '</div>';
                html += '</div>';
            }
            
            sidebarContent.innerHTML = html;
            
            // 为删除按钮添加事件监听器
            const deleteButtons = sidebarContent.querySelectorAll('.flow-delete');
            deleteButtons.forEach(btn => {
                btn.addEventListener('click', function() {
                    const switchId = this.getAttribute('data-switch-id');
                    const flowId = this.getAttribute('data-flow-id');
                    deleteFlow(switchId, flowId);
                });
            });
        }
        
        // 创建信息行
        function createInfoRow(label, value, highlight = false, error = false) {
            // 转义HTML特殊字符
            const escapeHtml = (str) => {
                if (str === null || str === undefined) return '';
                return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            };
            
            const safeLabel = escapeHtml(String(label));
            const safeValue = escapeHtml(String(value));
            let valueClass = 'info-value';
            if (highlight) valueClass += ' highlight';
            if (error) valueClass += ' error';
            return '<div class="info-row"><span class="info-label">' + safeLabel + '</span><span class="' + valueClass + '">' + safeValue + '</span></div>';
        }
        
        // 创建流表项
        function createFlowItem(flow, switchId, index) {
            // 转义特殊字符以避免XSS和语法错误
            const escapeHtml = (str) => {
                if (!str) return '';
                return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            };
            
            const safeSwitchId = escapeHtml(String(switchId));
            const safeFlowId = escapeHtml(String(flow.id || index));
            const safePriority = escapeHtml(String(flow.priority || flow.pri || 'N/A'));
            const safeMatch = escapeHtml(String(flow.match || 'N/A'));
            const safeAction = escapeHtml(String(flow.action || 'N/A'));
            const safeFlowIdNum = Math.floor(flow.id || index);
            const safePackets = flow.packets || 0;
            
            let html = '<div class="flow-item">';
            html += '<div class="flow-header">';
            html += '<div style="display: flex; align-items: center; gap: 8px;">';
            html += '<span class="flow-priority">Pri: ' + safePriority + '</span>';
            html += '<span class="flow-status"></span>';
            html += '</div>';
            html += '<div class="flow-delete" data-switch-id="' + safeSwitchId + '" data-flow-id="' + safeFlowId + '">';
            html += '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">';
            html += '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>';
            html += '</svg>';
            html += '</div>';
            html += '</div>';
            html += '<div class="flow-details">';
            html += '<div class="flow-detail-row">';
            html += '<span class="flow-detail-label">Match:</span>';
            html += '<span class="flow-detail-value match" title="' + safeMatch + '">' + safeMatch + '</span>';
            html += '</div>';
            html += '<div class="flow-detail-row">';
            html += '<span class="flow-detail-label">Action:</span>';
            html += '<span class="flow-detail-value action">' + safeAction + '</span>';
            html += '</div>';
            html += '</div>';
            html += '<div class="flow-footer">';
            html += '<span>ID: ' + safeFlowIdNum + '</span>';
            html += '<div class="flow-packet-count">';
            html += '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">';
            html += '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>';
            html += '</svg>';
            html += '<span>' + safePackets + ' pkts</span>';
            html += '</div>';
            html += '</div>';
            html += '</div>';
            return html;
        }
        
        // 关闭侧边栏
        function closeSidebar() {
            document.getElementById('sidebar').style.display = 'none';
        }
        
       // 显示添加流表模态框（简化版：自动拉端口列表 + 下拉选择）
	async function showAddFlowModal() {
	  const node = window.currentNodeData;
	  if (!node) return alert('请先点击选择一个交换机节点');
	  if (node.nodeType !== 'switch') return alert(`当前选中的是 ${node.nodeType}，请选交换机(switch)`);

	  const dpid = node.id;

	  // 1) 拉取端口列表
	  const portsResp = await fetch(`/api/switches/${encodeURIComponent(dpid)}/ports`);
	  const portsJson = await portsResp.json().catch(() => ({}));
	  if (!portsResp.ok || portsJson.ok === false) {
	    return alert('获取端口列表失败: ' + (portsJson.message || portsResp.statusText));
	  }
	  const ports = (portsJson.ports || []).map(Number).filter(Number.isFinite);

	  if (ports.length === 0) return alert(`交换机 ${dpid} 没有可用端口数据`);

	  // 2) 让用户从列表里选 in/out（先用最简单的 prompt 但不手输数字）
	  const inPort = parseInt(prompt(`选择入口端口 in_port（可选: ${ports.join(', ')}）:`), 10);
	  const outPort = parseInt(prompt(`选择出口端口 out_port（可选: ${ports.join(', ')}）:`), 10);

	  if (!ports.includes(inPort) || !ports.includes(outPort)) {
	    return alert('请选择端口列表中的端口号');
	  }

	  const flow = {
	    dpid,
	    priority: 100,
	    match: { in_port: inPort },
	    actions: [{ type: 'OUTPUT', port: outPort }]
	  };

	  const resp = await fetch('/api/flows/add', {
	    method: 'POST',
	    headers: { 'Content-Type': 'application/json' },
	    body: JSON.stringify(flow)
	  });

	  const result = await resp.json().catch(() => ({}));
	  if (!resp.ok || result.ok === false) {
	    return alert('下发失败: ' + (result.message || resp.statusText));
	  }

	  alert(`下发成功: dpid=${dpid} in_port=${inPort} -> out_port=${outPort}`);
	}
	window.showAddFlowModal = showAddFlowModal;


	// 删除流表项（占位函数）
        function deleteFlow(switchId, flowId) {
            if (confirm('确定要删除这条流表规则吗？')) {
                console.log('删除流表:', switchId, flowId);
                // TODO: 实现删除逻辑
            }
        }
        
        // 自适应缩放
        function fitNetwork() {
            if (network) {
                network.fit({
                    animation: {
                        duration: 1000,
                        easingFunction: 'easeInOutQuad'
                    }
                });
            }
        }
        
        // 切换布局
        function changeLayout() {
            const layout = document.getElementById('layout-select').value;
            console.log('切换布局:', layout);
            
            let options = {};
            
            if (layout === 'custom') {
                // 自定义分层布局
                options = {
                    layout: {
                        hierarchical: { enabled: false }
                    },
                    physics: { enabled: false }
                };
                
                network.setOptions(options);
                
                // 释放所有节点的固定状态
                nodes.get().forEach(node => {
                    nodes.update({ id: node.id, fixed: false });
                });
                
                // 重新应用自定义布局
                setTimeout(() => {
                    applyCustomLayout();
                    fitNetwork();
                }, 100);
                
            } else if (layout === 'hierarchical') {
                // vis.js内置层次布局
                options = {
                    layout: {
                        hierarchical: {
                            enabled: true,
                            direction: 'UD',
                            sortMethod: 'directed',
                            levelSeparation: 200,
                            nodeSpacing: 180
                        }
                    },
                    physics: { enabled: false }
                };
                
                // 释放固定位置
                nodes.get().forEach(node => {
                    nodes.update({ id: node.id, fixed: false });
                });
                
                network.setOptions(options);
                setTimeout(fitNetwork, 500);
                
            } else if (layout === 'physics') {
                // 物理力导向布局
                options = {
                    layout: {
                        hierarchical: { enabled: false }
                    },
                    physics: {
                        enabled: true,
                        barnesHut: {
                            gravitationalConstant: -3000,
                            centralGravity: 0.3,
                            springLength: 250,
                            springConstant: 0.04
                        },
                        stabilization: {
                            iterations: 150
                        }
                    }
                };
                
                // 释放固定位置
                nodes.get().forEach(node => {
                    nodes.update({ id: node.id, fixed: false });
                });
                
                network.setOptions(options);
                
            } else if (layout === 'circle') {
                // 环形布局
                options = {
                    layout: {
                        hierarchical: { enabled: false }
                    },
                    physics: { enabled: false }
                };
                
                network.setOptions(options);
                
                // 手动设置环形布局
                const nodeIds = nodes.getIds();
                const radius = 400;
                const angleStep = (2 * Math.PI) / nodeIds.length;
                
                nodeIds.forEach((id, index) => {
                    const angle = index * angleStep - Math.PI / 2;  // 从顶部开始
                    const x = radius * Math.cos(angle);
                    const y = radius * Math.sin(angle);
                    nodes.update({ id: id, x: x, y: y, fixed: true });
                });
                
                setTimeout(fitNetwork, 100);
            }
        }
        
        // 页面加载完成后初始化
        console.log('脚本已加载');
        window.addEventListener('load', function() {
            console.log('页面load事件触发');
            initNetwork();
        });
        
        // 备用：DOMContentLoaded事件
        document.addEventListener('DOMContentLoaded', function() {
            console.log('DOMContentLoaded事件触发');
        });
    </script>
</body>
</html>
        '''
        return html

    def start_web_server(self):
        """在单独的线程中启动 Flask 服务器"""
        def run_flask():
            try:
                # 禁用Flask的默认日志（避免过多输出）
                import logging
                log = logging.getLogger('werkzeug')
                log.setLevel(logging.WARNING)
                
                logger.info(f"Flask线程开始运行，准备绑定端口 {WEB_PORT}")
                print(f"Flask线程开始运行，准备绑定端口 {WEB_PORT}")
                
                app.run(host='0.0.0.0', port=WEB_PORT, debug=False, use_reloader=False, threaded=True)
            except Exception as e:
                logger.error(f"Flask Web服务器启动失败: {e}")
                logger.error(traceback.format_exc())
                print(f"Flask Web服务器启动失败: {e}")
                print(traceback.format_exc())
        
        web_thread = threading.Thread(target=run_flask, daemon=True)
        web_thread.start()
        
        # 等待一下让Flask有时间启动
        time.sleep(1)
        
        logger.info(f"Web 服务器线程已启动（端口 {WEB_PORT}）")
        logger.info(f"访问 http://localhost:{WEB_PORT} 查看拓扑可视化")
        print(f"Web 服务器线程已启动（端口 {WEB_PORT}）")
        print(f"访问 http://localhost:{WEB_PORT} 查看拓扑可视化")

    def start(self):
        """启动服务器"""
        try:
            # 启动 Web 服务器
            self.start_web_server()
            
            # 原有的 TCP 服务器启动代码
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((self.ip, self.port))
            self.sock.listen(5)
            self.is_running = True
            
            logger.info(f"服务器已启动，监听地址: {self.ip}:{self.port}")
            print(f"服务器已启动，监听地址: {self.ip}:{self.port}")
            
            while self.is_running:
                try:
                    client_sock, client_addr = self.sock.accept()
                    logger.info(f"接受连接: {client_addr}")
                    # print(f"接受连接: {client_addr}")
                    
                    # 为每个客户端创建新的线程
                    client_thread = threading.Thread(
                        target=self.handle_client,
                        args=(client_sock, client_addr)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                    # 设置socket超时，用于心跳检测
                    client_sock.settimeout(self.heartbeat_timeout)
                    
                    # 保存线程信息和心跳时间戳
                    with self.client_lock:
                        self.clients[client_addr] = (client_sock, client_thread)
                        self.client_last_heartbeat[client_addr] = time.time()
                    
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.is_running:
                        logger.error(f"接受连接时出错: {e}")
                        print(f"接受连接时出错: {e}")
        except Exception as e:
            logger.error(f"启动服务器时出错: {e}")
            print(f"启动服务器时出错: {e}")
        finally:
            self.stop()
    
    def handle_client(self, client_sock, client_addr):
        """处理客户端连接"""
        buffer = ""  # 用于累积未完成的消息
        try:
            while self.is_running:
                try:
                    data = client_sock.recv(4096)
                    if not data:
                        logger.info(f"客户端 {client_addr} 关闭了连接")
                        print(f"客户端 {client_addr} 关闭了连接")
                        break
                    
                    # 将接收到的数据添加到缓冲区
                    buffer += data.decode('utf-8')
                    
                    # 按换行符分割消息
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if line:  # 如果不是空行
                            try:
                                # 处理单个完整的JSON消息
                                self.process_message(client_sock, client_addr, line.encode('utf-8'))
                            except Exception as e:
                                logger.error(f"处理消息时出错: {e}, 消息内容: {line[:100]}")
                                logger.error(traceback.format_exc())
                
                except socket.timeout:
                    continue
                except Exception as e:
                    logger.error(f"接收数据时出错: {e}")
                    logger.error(traceback.format_exc())
                    print(f"接收数据时出错: {e}")
                    break
        except Exception as e:
            logger.error(f"处理客户端 {client_addr} 时出错: {e}")
            logger.error(traceback.format_exc())
            print(f"处理客户端 {client_addr} 时出错: {e}")
        finally:
            # 客户端连接关闭时的清理
            self.cleanup_disconnected_client(client_addr, reason="连接关闭")
    
    def process_message(self, client_sock, client_addr, data):
        """处理接收到的消息"""
        # 更新心跳时间戳（任何消息都视为心跳）
        with self.client_lock:
            if client_addr in self.client_last_heartbeat:
                self.client_last_heartbeat[client_addr] = time.time()
        
        try:
            # 打印接收到的数据
            # logger.info(f"从 {client_addr} 接收到消息: {data}")
            # print(f"从 {client_addr} 接收到消息: {data}")
            # 解析 JSON 数据
            message = json.loads(data.decode('utf-8'))
            self.new_method(client_addr, message)
            # print(f"从 {client_addr} 接收到消息: {message}")
            
            # 根据消息类型处理
            message_type = message.get('type')
            response = {'status': 'ok'}
            
            # 处理心跳消息
            if message_type == 'heartbeat':
                # 心跳消息，只更新时间戳（已在函数开头更新）
                logger.debug(f"收到客户端 {client_addr} 的心跳")
                return
            
            # 处理主动下线消息
            if message_type == 'disconnect':
                logger.info(f"收到客户端 {client_addr} 的主动下线消息")
                print(f"收到客户端 {client_addr} 的主动下线消息")
                self.cleanup_disconnected_client(client_addr, reason="主动下线")
                return
            
            if message_type == 'topo':
                self.handle_topo_message(client_addr, message)
            elif message_type == 'host':
                self.handle_host_message(client_addr, message)
            elif message_type == 'path_request':
                # path = self.handle_path_request(message)
                # response['path'] = path

                # response = self.handle_path_request(message)
                # client_sock.sendall(json.dumps(response).encode('utf-8'))
                # return  # 避免后面再发一次
                response = self.handle_path_request(message)
                # 广播给所有已连接的控制器
                for addr, (sock, _) in self.clients.items():
                    try:
                        data = json.dumps(response, ensure_ascii=False) + '\n'
                        sock.sendall(data.encode('utf-8'))
                    except Exception as e:
                        logger.error(f"向控制器 {addr} 发送路径信息失败: {e}")
                return  # 避免后面再发一次
            elif message_type == 'portdata_query':
                # 处理PortData查询请求，路由到对应的控制器
                self.handle_portdata_query(client_addr, message)
                return  # 避免后面再发一次
            elif message_type == 'portdata_response':
                # 处理PortData查询响应，路由回请求的控制器
                self.handle_portdata_response(client_addr, message)
                return  # 避免后面再发一次
            elif message_type == 'lldp_report':
                # 处理LLDP探测报告，由根控制器计算延迟并反馈
                self.handle_lldp_report(client_addr, message)
                return  # 已经下行，不再统一响应
            else:
                logger.warning(f"未知的消息类型: {message_type}")
                print(f"未知的消息类型: {message_type}")
                response = {'status': 'error', 'message': f'Unknown message type: {message_type}'}
            
            # 发送响应
            data = json.dumps(response, ensure_ascii=False) + '\n'
            client_sock.sendall(data.encode('utf-8'))
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析错误: {e}")
            logger.error(f"原始数据: {data}")
            print(f"JSON 解析错误: {e}")
            # 发送错误响应
            error_response = {'status': 'error', 'message': f'JSON parse error: {str(e)}'}
            error_data = json.dumps(error_response) + '\n'
            client_sock.sendall(error_data.encode('utf-8'))
        except Exception as e:
            logger.error(f"处理消息时出错: {e}")
            logger.error(traceback.format_exc())
            print(f"处理消息时出错: {e}")
            # 发送错误响应
            error_response = {'status': 'error', 'message': f'Error processing message: {str(e)}'}
            error_data = json.dumps(error_response) + '\n'
            client_sock.sendall(error_data.encode('utf-8'))

    def new_method(self, client_addr, message):
        logger.debug(f"从 {client_addr} 接收到消息: {message}")
    
    def heartbeat_check_loop(self):
        """心跳检测循环，定期检查所有客户端的连接状态"""
        while self.is_running:
            try:
                current_time = time.time()
                disconnected_clients = []
                
                with self.client_lock:
                    # 检查所有客户端的心跳状态
                    for client_addr, last_heartbeat in list(self.client_last_heartbeat.items()):
                        time_since_last_heartbeat = current_time - last_heartbeat
                        
                        if time_since_last_heartbeat > self.heartbeat_timeout:
                            # 超过超时时间，认为客户端已断联
                            logger.warning(f"客户端 {client_addr} 心跳超时 ({time_since_last_heartbeat:.2f}秒)，认为已断联")
                            print(f"客户端 {client_addr} 心跳超时 ({time_since_last_heartbeat:.2f}秒)，认为已断联")
                            disconnected_clients.append(client_addr)
                
                # 清理断联的客户端
                for client_addr in disconnected_clients:
                    self.cleanup_disconnected_client(client_addr, reason="心跳超时")
                
                # 等待下一次检测
                time.sleep(self.heartbeat_interval)
                
            except Exception as e:
                logger.error(f"心跳检测循环出错: {e}")
                logger.error(traceback.format_exc())
                time.sleep(self.heartbeat_interval)
    
    def cleanup_disconnected_client(self, client_addr, reason="未知"):
        """清理断联客户端的相关数据"""
        try:
            logger.info(f"清理客户端 {client_addr} 的数据，原因: {reason}")
            print(f"清理客户端 {client_addr} 的数据，原因: {reason}")
            
            # 关闭socket连接
            with self.client_lock:
                if client_addr in self.clients:
                    client_sock, _ = self.clients[client_addr]
                    try:
                        client_sock.close()
                    except:
                        pass
                    del self.clients[client_addr]
                
                if client_addr in self.client_last_heartbeat:
                    del self.client_last_heartbeat[client_addr]
            
            # 删除该控制器的拓扑信息
            if client_addr in self.topo:
                del self.topo[client_addr]
                logger.info(f"已删除客户端 {client_addr} 的链路信息")
            
            if client_addr in self.host:
                del self.host[client_addr]
                logger.info(f"已删除客户端 {client_addr} 的主机信息")
            
            if client_addr in self.controller_to_switches:
                del self.controller_to_switches[client_addr]
                logger.info(f"已删除客户端 {client_addr} 的交换机信息")
            
            # 清理该控制器的PortData查询请求记录
            # 删除所有由该控制器发起的查询请求记录
            request_ids_to_remove = []
            for request_id, (requester_addr, _) in self.portdata_query_requests.items():
                if requester_addr == client_addr:
                    request_ids_to_remove.append(request_id)
            for request_id in request_ids_to_remove:
                del self.portdata_query_requests[request_id]
                logger.debug(f"清理控制器 {client_addr} 的PortData查询请求记录: request_id={request_id}")
            
            # 更新网络图
            self.update_graph()
            
            logger.info(f"客户端 {client_addr} 的数据清理完成")
            print(f"客户端 {client_addr} 的数据清理完成")
            
        except Exception as e:
            logger.error(f"清理客户端 {client_addr} 数据时出错: {e}")
            logger.error(traceback.format_exc())
    
    def handle_topo_message(self, client_addr, message):
        """处理拓扑信息消息,接收时进行二次过滤"""
        # 使用完整的client_addr（包含IP和端口）作为键
        controller_key = client_addr if isinstance(client_addr, tuple) else (client_addr, 0)
        logger.info(f"处理来自 {controller_key} 的拓扑信息")
        
        # 保存交换机信息
        if 'switches' in message:
            self.controller_to_switches[controller_key] = message['switches']
            logger.info(f"更新控制器 {controller_key} 的交换机: {message['switches']}")
        
        # 保存链路信息
        if 'link' in message:
            self.topo[controller_key] = message['link']
            logger.info(f"更新控制器 {controller_key} 的链路: {len(message['link'])} 条")
            for link in message['link']:
                logger.info(f"链路详情: {link}")
        
        # ========== 关键修改:接收端二次过滤主机信息 ==========
        if 'host' in message:
            raw_hosts = message['host']
            logger.info(f"接收到 {len(raw_hosts)} 个主机信息,开始过滤...")
            
            # 获取该控制器管理的交换机列表
            controller_switches = set(self.controller_to_switches.get(controller_key, []))
            
            # 获取全局所有链路端口 (dpid, port)
            link_ports = set()
            for other_controller_key, other_links in self.topo.items():
                for link in other_links:
                    src_dpid = link.get('src')
                    src_port = link.get('src_port')
                    if src_dpid is not None and src_port is not None:
                        link_ports.add((src_dpid, src_port))
            
            filtered_hosts = []
            for host in raw_hosts:
                dpid = host.get('dpid')
                port = host.get('port')
                mac = host.get('mac')
                ip = host.get('ip')
                
                # 验证1:交换机必须属于该控制器
                if dpid not in controller_switches:
                    logger.warning(f"【主控过滤】主机所在交换机不属于该控制器: dpid={dpid}, controller={controller_key}")
                    continue
                
                # 验证2:端口不能是链路端口
                if (dpid, port) in link_ports:
                    logger.warning(f"【主控过滤】主机在链路端口上: dpid={dpid}, port={port}, MAC={mac}, IP={ip}")
                    continue
                
                # 验证3:IP地址有效性
                if not ip or ip == "0.0.0.0":
                    logger.warning(f"【主控过滤】无效IP地址: dpid={dpid}, port={port}, MAC={mac}, IP={ip}")
                    continue
                
                # 验证4:检查是否与其他控制器的交换机冲突
                is_conflict = False
                for other_controller_key, other_switches in self.controller_to_switches.items():
                    if other_controller_key == controller_key:
                        continue
                    if dpid in other_switches:
                        logger.warning(f"【主控过滤】交换机属于其他控制器: dpid={dpid}, other_controller={other_controller_key}")
                        is_conflict = True
                        break
                if is_conflict:
                    continue
                
                # 通过所有验证
                filtered_hosts.append(host)
                logger.info(f"【主控接受】主机: dpid={dpid}, port={port}, MAC={mac}, IP={ip}")
            
            logger.info(f"过滤后主机数量: {len(filtered_hosts)} / {len(raw_hosts)}")
            self.host[controller_key] = filtered_hosts
        
        # 更新图
        self.update_graph()
        logger.info("拓扑信息处理完成")
    
    def handle_host_message(self, client_addr, message):
        """处理主机信息消息"""
        # 使用完整的client_addr（包含IP和端口）作为键
        controller_key = client_addr if isinstance(client_addr, tuple) else (client_addr, 0)
        if 'hosts' in message:
            self.host[controller_key] = message['hosts']
            logger.info(f"更新控制器 {controller_key} 的主机信息: {len(message['hosts'])} 个主机")
            # print(f"更新控制器 {controller_key} 的主机信息: {len(message['hosts'])} 个主机")
            
            # 更新图
            self.update_graph()
    
    def handle_portdata_query(self, client_addr, message):
        """
        处理PortData查询请求，路由到管理该交换机的控制器
        
        Args:
            client_addr: 请求控制器的地址
            message: 查询消息，包含src_dpid和src_port_no
        """
        src_dpid = message.get('src_dpid')
        request_id = message.get('request_id')
        
        logger.debug(f"收到PortData查询请求: src_dpid={src_dpid}, request_id={request_id}, 来自 {client_addr}")
        
        # 记录查询请求的发起者，用于后续路由响应
        self.portdata_query_requests[request_id] = (client_addr, time.time())
        
        # 查找管理该交换机的控制器
        target_controller = None
        for controller_key, switches in self.controller_to_switches.items():
            if src_dpid in switches:
                target_controller = controller_key
                break
        
        if target_controller is None:
            logger.warning(f"未找到管理交换机 {src_dpid} 的控制器")
            # 发送错误响应给请求的控制器
            error_response = {
                "type": "portdata_response",
                "request_id": request_id,
                "src_dpid": src_dpid,
                "status": "error",
                "message": f"Controller not found for switch {src_dpid}"
            }
            self._send_to_controller(client_addr, error_response)
            # 清理记录
            if request_id in self.portdata_query_requests:
                del self.portdata_query_requests[request_id]
            return
        
        # 如果目标控制器就是请求的控制器，直接返回（不应该发生，但处理一下）
        if target_controller == client_addr:
            logger.warning(f"PortData查询请求的交换机属于请求控制器本身: {src_dpid}")
            # 清理记录
            if request_id in self.portdata_query_requests:
                del self.portdata_query_requests[request_id]
            return
        
        # 转发查询请求到目标控制器
        logger.debug(f"转发PortData查询请求到控制器 {target_controller}")
        self._send_to_controller(target_controller, message)
        logger.info("add_flow target_controller=%s for dpid=%s", target_controller, dpid)
    
    def handle_portdata_response(self, client_addr, message):
        """
        处理PortData查询响应，路由回请求的控制器
        
        Args:
            client_addr: 响应控制器的地址
            message: 响应消息，包含request_id
        """
        request_id = message.get('request_id')
        logger.debug(f"收到PortData查询响应: request_id={request_id}, 来自 {client_addr}")
        
        # 查找请求的控制器（从记录的查询请求中查找）
        if request_id in self.portdata_query_requests:
            requester_addr, query_time = self.portdata_query_requests[request_id]
            
            # 只将响应发送给发起查询的控制器
            logger.debug(f"转发PortData响应到请求控制器 {requester_addr}")
            self._send_to_controller(requester_addr, message)
            
            # 清理记录（响应已发送）
            del self.portdata_query_requests[request_id]
        else:
            logger.warning(f"未找到PortData查询请求记录: request_id={request_id}")
            # 如果找不到记录，可能是请求已超时或已被清理，忽略响应

    def handle_lldp_report(self, client_addr, message):
        """
        处理从控制器上报的LLDP信息，计算延迟并反馈相关控制器。
        """
        src_dpid = message.get('src_dpid')
        dst_dpid = message.get('dst_dpid')
        send_time = message.get('send_time')
        receive_time = message.get('receive_time')
        src_echo = float(message.get('src_echo', 0.0) or 0.0)
        dst_echo = float(message.get('dst_echo', 0.0) or 0.0)

        if src_dpid is None or dst_dpid is None:
            logger.warning("LLDP报告缺少交换机信息: %s", message)
            return

        if send_time is None or receive_time is None:
            error_resp = {
                "type": "lldp_delay_update",
                "status": "error",
                "message": "send_time or receive_time missing",
                "src_dpid": src_dpid,
                "dst_dpid": dst_dpid
            }
            self._send_to_controller(client_addr, error_resp)
            return

        try:
            fwd_delay = float(receive_time) - float(send_time)
            calc_delay = fwd_delay - (src_echo + dst_echo) / 2
            calc_delay = max(calc_delay, 0.0)
        except Exception as e:
            logger.error(f"计算LLDP延迟失败: {e}")
            error_resp = {
                "type": "lldp_delay_update",
                "status": "error",
                "message": f"calc error: {e}",
                "src_dpid": src_dpid,
                "dst_dpid": dst_dpid
            }
            self._send_to_controller(client_addr, error_resp)
            return

        resp = {
            "type": "lldp_delay_update",
            "status": "ok",
            "src_dpid": src_dpid,
            "dst_dpid": dst_dpid,
            "fwd_delay": fwd_delay,
            "src_echo": src_echo,
            "dst_echo": dst_echo,
            "delay": calc_delay
        }

        # 发送给上报控制器
        self._send_to_controller(client_addr, resp)

        # 同时发送给相关控制器（拥有src或dst交换机的控制器）
        targets = set()
        for controller_key, switches in self.controller_to_switches.items():
            if src_dpid in switches or dst_dpid in switches:
                targets.add(controller_key)

        for target in targets:
            if target != client_addr:
                self._send_to_controller(target, resp)

        logger.debug(f"LLDP延迟计算完成并分发: {resp}, targets={targets}")
    
    def _send_to_controller(self, controller_addr, message):
        """
        向指定控制器发送消息
        
        Args:
            controller_addr: 控制器地址（(ip, port)元组）
            message: 要发送的消息
        """
        with self.client_lock:
            if controller_addr in self.clients:
                sock, _ = self.clients[controller_addr]
                try:
                    data = json.dumps(message, ensure_ascii=False) + '\n'  # 添加换行符作为消息分隔符
                    sock.sendall(data.encode('utf-8'))
                    logger.debug(f"向控制器 {controller_addr} 发送消息: {message.get('type')}")
                except Exception as e:
                    logger.error(f"向控制器 {controller_addr} 发送消息失败: {e}")
            else:
                logger.warning(f"控制器 {controller_addr} 未连接")
    
    def update_graph(self):
        """更新网络图"""
        # 清空图
        self.G.clear()
        
        # 添加根控制器节点（用特殊标识）
        root_controller_id = "RootController"
        # 获取服务器IP地址（从配置中获取）
        root_ip = self.ip if hasattr(self, 'ip') else '0.0.0.0'
        self.G.add_node(root_controller_id, node_type='root_controller', ip=root_ip)
        
        # 收集所有控制器的标识（使用(ip, port)元组，不去重）
        controller_keys = set()
        
        # 从clients中获取（clients的键已经是(ip, port)元组）
        for client_addr in self.clients.keys():
            if isinstance(client_addr, tuple):
                controller_keys.add(client_addr)
            else:
                controller_keys.add((client_addr, 0))
        
        # 从topo中获取（现在键应该是(ip, port)元组）
        for controller_key in self.topo.keys():
            if isinstance(controller_key, tuple):
                controller_keys.add(controller_key)
            else:
                # 兼容旧数据：如果是字符串，转换为元组
                controller_keys.add((controller_key, 0))
        
        # 从controller_to_switches中获取
        for controller_key in self.controller_to_switches.keys():
            if isinstance(controller_key, tuple):
                controller_keys.add(controller_key)
            else:
                controller_keys.add((controller_key, 0))
        
        # 从host中获取
        for controller_key in self.host.keys():
            if isinstance(controller_key, tuple):
                controller_keys.add(controller_key)
            else:
                controller_keys.add((controller_key, 0))
        
        # 为每个控制器创建节点并连接到根控制器
        for controller_key in controller_keys:
            # 生成唯一的控制器ID（包含IP和端口）
            if isinstance(controller_key, tuple):
                ip, port = controller_key
                controller_id = f"Controller_{ip}_{port}"
            else:
                ip = controller_key
                port = 0
                controller_id = f"Controller_{ip}_{port}"
            
            self.G.add_node(controller_id, node_type='controller', ip=ip, port=port)
            # 从控制器连接到根控制器
            self.G.add_edge(root_controller_id, controller_id, 
                          edge_type='controller_connection', weight=1)
            logger.info(f"添加控制器节点: {controller_id} (IP: {ip}, Port: {port})")
        
        # 添加拓扑链路
        for controller_key, links in self.topo.items():
            # 生成控制器ID
            if isinstance(controller_key, tuple):
                ip, port = controller_key
                controller_id = f"Controller_{ip}_{port}"
            else:
                ip = controller_key
                port = 0
                controller_id = f"Controller_{ip}_{port}"
            
            # 确保控制器节点存在（应该已经存在了，但为了安全起见）
            if controller_id not in self.G:
                self.G.add_node(controller_id, node_type='controller', ip=ip, port=port)
                # 连接到根控制器
                if root_controller_id in self.G:
                    self.G.add_edge(root_controller_id, controller_id, 
                                  edge_type='controller_connection', weight=1)
            
            for link in links:
                # 适配controller.py发送的格式
                src = link.get('src')
                dst = link.get('dst')
                if src and dst:
                    # 先确保节点存在并设置正确的node_type（在添加边之前）
                    # 这样可以避免NetworkX自动创建没有属性的节点
                    if src not in self.G:
                        self.G.add_node(src, node_type='switch')
                    else:
                        # 如果节点已存在但没有node_type，则更新它
                        if 'node_type' not in self.G.nodes[src] or self.G.nodes[src].get('node_type') != 'switch':
                            self.G.nodes[src]['node_type'] = 'switch'
                    
                    if dst not in self.G:
                        self.G.add_node(dst, node_type='switch')
                    else:
                        # 如果节点已存在但没有node_type，则更新它
                        if 'node_type' not in self.G.nodes[dst] or self.G.nodes[dst].get('node_type') != 'switch':
                            self.G.nodes[dst]['node_type'] = 'switch'
                    
                    # 添加边，可以设置权重等属性
                    delay = link.get('delay', 1)
                    bw = link.get('bw', 1)
                    loss = link.get('loss', 0)
                    
                    # 计算权重 (可以根据延迟、带宽和丢包率计算)
                    # 确保所有值都是有限的，避免产生inf或NaN
                    import math
                    if not math.isfinite(delay) or delay < 0:
                        delay = 1
                    if not math.isfinite(bw) or bw <= 0:
                        bw = 1
                    if not math.isfinite(loss) or loss < 0:
                        loss = 0
                    
                    weight = delay * (1 + loss) / bw
                    # 确保权重是有限的
                    if not math.isfinite(weight) or weight < 0:
                        weight = 1
                    
                    self.G.add_edge(src, dst, weight=weight, controller=controller_key,
                                   delay=delay, bw=bw, loss=loss, edge_type='switch_link')
                    
                    # 添加交换机到控制器的连接（如果交换机属于该控制器）
                    if controller_id in self.G:
                        # 检查交换机是否属于该控制器
                        if controller_key in self.controller_to_switches:
                            if src in self.controller_to_switches[controller_key]:
                                if not self.G.has_edge(controller_id, src):
                                    self.G.add_edge(controller_id, src, 
                                                  edge_type='controller_switch', weight=0.5)
                            if dst in self.controller_to_switches[controller_key]:
                                if not self.G.has_edge(controller_id, dst):
                                    self.G.add_edge(controller_id, dst, 
                                                  edge_type='controller_switch', weight=0.5)
                    
                    logger.info(f"添加边: {src} -> {dst}, 权重: {weight}")
        
        # 添加交换机节点（即使没有链路）
        for controller_key, switches in self.controller_to_switches.items():
            # 生成控制器ID
            if isinstance(controller_key, tuple):
                ip, port = controller_key
                controller_id = f"Controller_{ip}_{port}"
            else:
                ip = controller_key
                port = 0
                controller_id = f"Controller_{ip}_{port}"
            
            # 确保控制器节点存在（应该已经存在了，但为了安全起见）
            if controller_id not in self.G:
                self.G.add_node(controller_id, node_type='controller', ip=ip, port=port)
                # 连接到根控制器
                if root_controller_id in self.G:
                    self.G.add_edge(root_controller_id, controller_id, 
                                  edge_type='controller_connection', weight=1)
            
            for switch_id in switches:
                if switch_id not in self.G:
                    self.G.add_node(switch_id, node_type='switch')
                else:
                    # 如果节点已存在但没有node_type或node_type不正确，则更新它
                    if 'node_type' not in self.G.nodes[switch_id] or self.G.nodes[switch_id].get('node_type') != 'switch':
                        self.G.nodes[switch_id]['node_type'] = 'switch'
                # 连接交换机到其控制器
                if not self.G.has_edge(controller_id, switch_id):
                    self.G.add_edge(controller_id, switch_id, 
                                  edge_type='controller_switch', weight=0.5)
        
        # 添加主机连接
        for controller_key, hosts in self.host.items():
            # 生成控制器ID
            if isinstance(controller_key, tuple):
                ip, port = controller_key
                controller_id = f"Controller_{ip}_{port}"
            else:
                ip = controller_key
                port = 0
                controller_id = f"Controller_{ip}_{port}"
            
            for host in hosts:
                # 适配controller.py发送的格式
                dpid = host.get('dpid')
                mac = host.get('mac')
                ip = host.get('ip')
                
                if dpid and ip:
                    # 确保交换机节点存在并设置正确的node_type
                    if dpid not in self.G:
                        self.G.add_node(dpid, node_type='switch')
                    else:
                        # 如果节点已存在但没有node_type或node_type不正确，则更新它
                        if 'node_type' not in self.G.nodes[dpid] or self.G.nodes[dpid].get('node_type') != 'switch':
                            self.G.nodes[dpid]['node_type'] = 'switch'
                    
                    # 添加主机节点并设置正确的node_type
                    if ip not in self.G:
                        self.G.add_node(ip, node_type='host', mac=mac)
                    else:
                        # 如果节点已存在但没有node_type或node_type不正确，则更新它
                        if 'node_type' not in self.G.nodes[ip] or self.G.nodes[ip].get('node_type') != 'host':
                            self.G.nodes[ip]['node_type'] = 'host'
                            if mac:
                                self.G.nodes[ip]['mac'] = mac
                    
                    # 添加主机到交换机的边
                    self.G.add_edge(ip, dpid, weight=1, controller=controller_key,
                                  edge_type='host_switch')
                    # 添加交换机到主机的边
                    self.G.add_edge(dpid, ip, weight=1, controller=controller_key,
                                  edge_type='host_switch')
                    
                    logger.info(f"添加主机连接: {mac} <-> {dpid}, IP: {ip}")
        
        logger.info(f"更新网络图完成: {len(self.G.nodes)} 个节点, {len(self.G.edges)} 条边")
        # print(f"更新网络图完成: {len(self.G.nodes)} 个节点, {len(self.G.edges)} 条边")
        #print(f"**********G图节点: {list(self.G.nodes())}")
        #print(f"**********G图边: {list(self.G.edges(data=True))}")
    
    def handle_path_request(self, message):
        """处理路径请求"""
        src = message.get('src')
        dst = message.get('dst')
        
        if not src or not dst:
            logger.error("路径请求缺少源或目的地址")
            print("路径请求缺少源或目的地址")
            # return []
            return {'status': 'error', 'message': '路径请求缺少源或目的地址'}
        
        # logger.info(f"处理路径请求: {src} -> {dst}")
        print(f"处理路径请求: {src} -> {dst}")
        
        # 检查源和目的是否在图中
        if src not in self.G or dst not in self.G:
            # logger.error(f"源或目的不在网络图中: src={src in self.G}, dst={dst in self.G}")
            print(f"源或目的不在网络图中: src={src in self.G}, dst={dst in self.G}")
            # return []
            return {'status': 'error', 'message': '源或目的不在网络图中'}
        
        try:
            # 使用Dijkstra算法计算最短路径
            path = nx.shortest_path(self.G, src, dst, weight='weight')
            print("************************")
            logger.info(f"找到路径: {path}")
            print(f"找到路径: {path}")

            # 修改响应格式，添加更多信息
            response = {
                'status': 'ok',
                'path': path,
                'src_ip': src,  # 添加源IP
                'dst_ip': dst,  # 添加目标IP
                'switch_id': message.get('switch_id', None),  # 添加交换机ID（如果有）
                'in_port': message.get('in_port', None)  # 添加输入端口（如果有）
            }
            
            # 向所有控制器广播路径信息（可选）
            # for client_addr, (client_sock, _) in self.clients.items():
                # try:
                    # client_sock.sendall(json.dumps(response).encode())
                # except Exception as e:
                    # logger.error(f"向控制器 {client_addr} 发送路径信息失败: {e}")
            # 
            return response

        except nx.NetworkXNoPath:
            # logger.error(f"没有找到从 {src} 到 {dst} 的路径")
            print(f"没有找到从 {src} 到 {dst} 的路径")
            # return []
            return {'status': 'error', 'message': f'没有找到从 {src} 到 {dst} 的路径'}
        except Exception as e:
            # logger.error(f"计算路径时出错: {e}")
            print(f"计算路径时出错: {e}")
            # return []
            return {'status': 'error', 'message': f'计算路径时出错: {e}'}
    
    def stop(self):
        """停止服务器"""
        self.is_running = False
        
        # 关闭所有客户端连接
        for client_addr, (client_sock, _) in list(self.clients.items()):
            try:
                client_sock.close()
                logger.info(f"关闭客户端连接: {client_addr}")
                print(f"关闭客户端连接: {client_addr}")
            except:
                pass
        
        # 清空客户端列表
        self.clients.clear()
        
        # 关闭服务器套接字
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        
        logger.info("服务器已停止")
        print("服务器已停止")

    def print_topo_info_loop(self):
        """定时打印拓扑信息"""
        logger.info("定时打印线程开始运行")
        # print("定时打印线程开始运行")
        
        while True:
            try:
                # 先打印一条日志确认线程在运行
                logger.info("定时打印线程正在运行...")
                # print("定时打印线程正在运行...")
                
                if self.topo or self.host or self.controller_to_switches:
                    logger.info("=" * 50)
                    logger.info("当前拓扑信息:")
                    
                    # 打印控制器信息
                    logger.info(f"已连接控制器数量: {len(self.clients)}")
                    for client_addr in self.clients:
                        logger.info(f"  - 控制器: {client_addr}")
                    
                    # 打印交换机信息
                    all_switches = set()
                    for controller_key, switches in self.controller_to_switches.items():
                        all_switches.update(switches)
                    logger.info(f"交换机总数: {len(all_switches)}")
                    for controller_key, switches in self.controller_to_switches.items():
                        controller_str = f"{controller_key[0]}:{controller_key[1]}" if isinstance(controller_key, tuple) else str(controller_key)
                        logger.info(f"  - 控制器 {controller_str} 管理的交换机: {switches}")
                    
                    # 打印链路信息
                    all_links = []
                    for controller_key, links in self.topo.items():
                        all_links.extend(links)
                    logger.info(f"链路总数: {len(all_links)}")
                    for controller_key, links in self.topo.items():
                        controller_str = f"{controller_key[0]}:{controller_key[1]}" if isinstance(controller_key, tuple) else str(controller_key)
                        logger.info(f"  - 控制器 {controller_str} 的链路:")
                        for link in links:
                            logger.info(f"    * {link}")
                    
                    # 打印主机信息
                    all_hosts = []
                    for controller_key, hosts in self.host.items():
                        all_hosts.extend(hosts)
                    logger.info(f"主机总数: {len(all_hosts)}")
                    for controller_key, hosts in self.host.items():
                        controller_str = f"{controller_key[0]}:{controller_key[1]}" if isinstance(controller_key, tuple) else str(controller_key)
                        logger.info(f"  - 控制器 {controller_str} 的主机:")
                        for host in hosts:
                            logger.info(f"    * {host}")
                    
                    # 打印图信息
                    logger.info(f"图节点数: {len(self.G.nodes)}, 边数: {len(self.G.edges)}")
                    logger.info("=" * 50)
                    
                    # 同时打印到控制台
                    print("=" * 50)
                    print("当前拓扑信息:")
                    print(f"已连接控制器数量: {len(self.clients)}")
                    print(f"交换机总数: {len(all_switches)}")
                    print(f"链路总数: {len(all_links)}")
                    print(f"主机总数: {len(all_hosts)}")
                    print(f"图节点数: {len(self.G.nodes)}, 边数: {len(self.G.edges)}")
                    print("=" * 50)
                else:
                    logger.info("当前没有拓扑信息")
                    # print("当前没有拓扑信息")
            except Exception as e:
                logger.error(f"打印拓扑信息时出错: {e}")
                print(f"打印拓扑信息时出错: {e}")
                traceback.print_exc()
            
            # 每10秒打印一次
            time.sleep(10)
    
    def start_gui(self):
        """启动GUI界面"""
        try:
            # 创建主窗口
            self.root = tk.Tk()
            self.root.title("Network Topology Visualization - Root Controller")
            self.root.geometry("1200x800")
            
            # 创建GUI应用
            self.gui_app = TopoGUI(self.root, self)
            
            # 启动GUI主循环
            self.root.mainloop()
        except Exception as e:
            logger.error(f"启动GUI界面失败: {e}")
            traceback.print_exc()

class TopoGUI:
    """网络拓扑可视化GUI"""
    def __init__(self, root, server_agent):
        self.root = root
        self.server_agent = server_agent
        
        # 创建主框架
        main_frame = ttk.Frame(root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # 配置网格权重
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)
        
        # 创建标题和信息框架
        info_frame = ttk.Frame(main_frame)
        info_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 10))
        
        title_label = ttk.Label(info_frame, text="Network Topology Visualization", 
                               font=("Arial", 16, "bold"))
        title_label.grid(row=0, column=0, sticky=tk.W)
        
        # 创建统计信息框架
        stats_frame = ttk.LabelFrame(info_frame, text="Network Statistics", padding="5")
        stats_frame.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=(20, 0))
        
        self.stats_labels = {}
        stats_info = [
            ("Controllers", "controllers"),
            ("Switches", "switches"),
            ("Links", "links"),
            ("Hosts", "hosts")
        ]
        
        for i, (label_text, key) in enumerate(stats_info):
            label = ttk.Label(stats_frame, text=f"{label_text}:")
            label.grid(row=i, column=0, sticky=tk.W, padx=(0, 5))
            value_label = ttk.Label(stats_frame, text="0", foreground="blue")
            value_label.grid(row=i, column=1, sticky=tk.W)
            self.stats_labels[key] = value_label
        
        # 创建刷新按钮
        refresh_btn = ttk.Button(info_frame, text="Refresh", command=self.refresh_topo)
        refresh_btn.grid(row=0, column=2, padx=(20, 0))
        
        # 创建拓扑图框架
        topo_frame = ttk.LabelFrame(main_frame, text="Network Topology Graph", padding="5")
        topo_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        topo_frame.columnconfigure(0, weight=1)
        topo_frame.rowconfigure(0, weight=1)
        
        # 创建matplotlib图形
        self.fig = Figure(figsize=(14, 10), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_title("网络拓扑结构", fontsize=14, fontweight='bold')
        self.ax.axis('off')
        
        # 将matplotlib图形嵌入到tkinter
        self.canvas = FigureCanvasTkAgg(self.fig, topo_frame)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # 交互功能相关变量
        self.hover_annotation = None
        self.selected_node = None
        self.node_info_window = None
        self.node_positions = {}  # 存储节点位置，用于交互
        self.node_data_cache = {}  # 缓存节点数据
        
        # 绑定鼠标事件
        self.canvas.mpl_connect('motion_notify_event', self.on_hover)
        self.canvas.mpl_connect('button_press_event', self.on_click)
        
        # 初始绘制
        self.refresh_topo()
        
        # 启动定时更新（每3秒更新一次）
        self.update_topo_loop()
    
    def get_statistics(self):
        """获取网络统计信息"""
        try:
            # 收集所有控制器的IP地址（去重）
            controller_ips = set()
            
            # 从clients中提取IP（clients的键是(ip, port)元组）
            for client_addr in self.server_agent.clients.keys():
                if isinstance(client_addr, tuple):
                    controller_ip = client_addr[0]  # 提取IP地址
                else:
                    controller_ip = client_addr
                controller_ips.add(controller_ip)
            
            # 从其他数据源提取控制器标识（使用(ip, port)元组）
            controller_keys = set()
            
            # 从topo中获取
            for key in self.server_agent.topo.keys():
                if isinstance(key, tuple):
                    controller_keys.add(key)
                else:
                    controller_keys.add((key, 0))
            
            # 从controller_to_switches中获取
            for key in self.server_agent.controller_to_switches.keys():
                if isinstance(key, tuple):
                    controller_keys.add(key)
                else:
                    controller_keys.add((key, 0))
            
            # 从host中获取
            for key in self.server_agent.host.keys():
                if isinstance(key, tuple):
                    controller_keys.add(key)
                else:
                    controller_keys.add((key, 0))
            
            # 合并所有控制器标识
            all_controller_keys = controller_ips.union(controller_keys)
            controllers = len(all_controller_keys)
            switches = sum(len(switches) for switches in self.server_agent.controller_to_switches.values())
            links = sum(len(links) for links in self.server_agent.topo.values())
            hosts = sum(len(hosts) for hosts in self.server_agent.host.values())
            return {
                'controllers': controllers,
                'switches': switches,
                'links': links,
                'hosts': hosts
            }
        except:
            return {'controllers': 0, 'switches': 0, 'links': 0, 'hosts': 0}
    
    def update_statistics(self):
        """更新统计信息显示"""
        stats = self.get_statistics()
        for key, value in stats.items():
            if key in self.stats_labels:
                self.stats_labels[key].config(text=str(value))
    
    def improved_layout(self, G):
        """
        改进的布局算法，结合层次布局和力导向布局
        使用NetworkX的高级布局算法来避免连接线重合
        """
        if len(G.nodes()) == 0:
            return {}
        n = len(G.nodes())

        # 基础分层布局：保证“根-控-交换机-主机”纵向有序
        layered_pos = self.hierarchical_layout(G)

        # 大图直接用分层（确保整齐，避免过度计算）
        if n > 120:
            return layered_pos

        # 中等规模：以分层为初始，轻量 spring 微调横向分布
        if n > 60:
            try:
                pos = nx.spring_layout(
                    G,
                    pos=layered_pos,
                    k=1.2 / max(n ** 0.5, 1),
                    iterations=25,
                    weight='weight',
                    seed=42,
                )
                # 清理可能的NaN值
                pos = self._clean_layout_positions(pos)
            except Exception as e:
                logger.warning(f"spring布局失败，使用分层布局: {e}")
                pos = layered_pos
            # 对齐纵向层次，保持整齐
            return self._align_layers_with_layout(G, pos, layered_pos)

        # 小图：Kamada-Kawai 打底，失败则 spring
        try:
            pos = nx.kamada_kawai_layout(G, weight='weight')
            # 清理可能的NaN值
            pos = self._clean_layout_positions(pos)
        except Exception as e:
            logger.warning(f"Kamada-Kawai布局失败: {e}")
            try:
                pos = nx.spring_layout(G, k=2, iterations=50, weight='weight', seed=42)
                # 清理可能的NaN值
                pos = self._clean_layout_positions(pos)
            except Exception as e2:
                logger.warning(f"spring布局失败，使用分层布局: {e2}")
                pos = layered_pos
        
        # 最后使用分层的 y 轴对齐，保留横向优化结果
        return self._align_layers_with_layout(G, pos, layered_pos)
    
    def _clean_layout_positions(self, pos):
        """
        清理布局位置中的NaN和inf值，替换为有效的默认坐标
        
        Args:
            pos: 节点位置字典 {node: (x, y)}
        
        Returns:
            清理后的位置字典
        """
        import math
        
        cleaned_pos = {}
        default_x = 5.0  # 默认x坐标
        default_y = 4.0  # 默认y坐标
        
        for node, (x, y) in pos.items():
            # 检查并修复x坐标
            if not math.isfinite(x):
                logger.warning(f"节点 {node} 的x坐标无效 ({x})，使用默认值")
                x = default_x
            
            # 检查并修复y坐标
            if not math.isfinite(y):
                logger.warning(f"节点 {node} 的y坐标无效 ({y})，使用默认值")
                y = default_y
            
            cleaned_pos[node] = (x, y)
        
        return cleaned_pos

    def _align_layers_with_layout(self, G, pos, layered_pos):
        """
        将力导向/KK 产生的横向结果与分层 y 轴对齐，避免上下层错乱。
        """
        import math
        
        aligned = {}
        for node in G.nodes():
            base_y = layered_pos.get(node, (0, 0))[1]
            x = pos.get(node, layered_pos.get(node, (0, 0)))[0]
            
            # 验证坐标有效性
            if not math.isfinite(x):
                x = layered_pos.get(node, (5.0, 0))[0]
            if not math.isfinite(base_y):
                base_y = 4.0
            
            aligned[node] = (x, base_y)
        return aligned
    
    def hierarchical_layout(self, G):
        """
        自定义层次布局算法（作为备选方案）
        层次结构：根控制器 -> 从控制器 -> 交换机 -> 主机
        """
        pos = {}
        
        # 分离节点类型
        root_controller = None
        controllers = []
        switches = []
        hosts = []
        
        # 控制器到交换机的映射
        controller_to_switches = {}
        # 交换机到主机的映射
        switch_to_hosts = {}
        
        for node in G.nodes():
            node_data = G.nodes[node]
            node_type = node_data.get('node_type', 'unknown')
            
            if node_type == 'root_controller':
                root_controller = node
            elif node_type == 'controller':
                controllers.append(node)
                controller_to_switches[node] = []
            elif node_type == 'switch':
                switches.append(node)
            elif node_type == 'host':
                hosts.append(node)
            else:
                # 兼容旧数据
                if isinstance(node, str) and '.' in node and node.count('.') == 3:
                    hosts.append(node)
                elif isinstance(node, (int, str)) and str(node).isdigit():
                    switches.append(node)
                elif node.startswith('Controller_'):
                    controllers.append(node)
                    controller_to_switches[node] = []
                else:
                    hosts.append(node)
        
        # 构建控制器到交换机的映射
        for edge in G.edges(data=True):
            u, v, data = edge
            edge_type = data.get('edge_type', '')
            
            if edge_type == 'controller_switch':
                # u是控制器，v是交换机
                if u in controllers and v in switches:
                    if u not in controller_to_switches:
                        controller_to_switches[u] = []
                    if v not in controller_to_switches[u]:
                        controller_to_switches[u].append(v)
        
        # 构建交换机到主机的映射
        for edge in G.edges(data=True):
            u, v, data = edge
            edge_type = data.get('edge_type', '')
            
            if edge_type == 'host_switch':
                # 可能是 u->v 或 v->u
                if u in switches and v in hosts:
                    if u not in switch_to_hosts:
                        switch_to_hosts[u] = []
                    if v not in switch_to_hosts[u]:
                        switch_to_hosts[u].append(v)
                elif v in switches and u in hosts:
                    if v not in switch_to_hosts:
                        switch_to_hosts[v] = []
                    if u not in switch_to_hosts[v]:
                        switch_to_hosts[v].append(u)
        
        # 布局参数
        width = 10  # 画布宽度
        height = 8  # 画布高度
        
        # Layer 0: 根控制器（顶部中心）
        if root_controller:
            pos[root_controller] = (width / 2, height - 0.5)
        
        # Layer 1: 从控制器（第二层，水平排列）
        if controllers:
            controller_count = len(controllers)
            if controller_count == 1:
                controller_x_positions = [width / 2]
            else:
                # 在中心区域均匀分布
                margin = 2.0
                available_width = width - 2 * margin
                if controller_count > 1:
                    spacing = available_width / (controller_count - 1)
                else:
                    spacing = 0
                controller_x_positions = [margin + i * spacing for i in range(controller_count)]
            
            for i, controller in enumerate(controllers):
                pos[controller] = (controller_x_positions[i], height - 2.0)
        
        # Layer 2: 交换机（第三层，按控制器分组）
        switch_y = height - 3.5
        
        # 收集所有已分配的交换机位置，用于避免重叠
        assigned_switch_positions = []
        
        # 为每个控制器分配交换机
        for controller in controllers:
            controller_switches = controller_to_switches.get(controller, [])
            if not controller_switches:
                continue
            
            # 获取控制器的x坐标
            controller_x = pos.get(controller, (width / 2, 0))[0]
            
            # 计算交换机数量
            switch_count = len(controller_switches)
            
            # 在控制器下方均匀排列交换机
            if switch_count == 1:
                switch_x_positions = [controller_x]
            else:
                # 交换机分布范围（控制器左右各延伸一定距离）
                switch_span = min(3.5, max(1.5, switch_count * 0.7))
                if switch_count > 1:
                    spacing = (switch_span * 2) / (switch_count - 1)
                else:
                    spacing = 0
                switch_x_positions = [controller_x - switch_span + i * spacing 
                                     for i in range(switch_count)]
            
            # 检查并调整位置以避免与其他控制器下的交换机重叠
            for i, switch_node in enumerate(controller_switches):
                desired_x = switch_x_positions[i]
                # 如果位置太接近已分配的交换机，稍微调整
                min_distance = 0.8
                adjusted_x = desired_x
                for existing_x, existing_y in assigned_switch_positions:
                    if abs(existing_x - desired_x) < min_distance and abs(existing_y - switch_y) < 0.5:
                        # 偏移到右侧
                        adjusted_x = existing_x + min_distance
                        break
                
                pos[switch_node] = (adjusted_x, switch_y)
                assigned_switch_positions.append((adjusted_x, switch_y))
        
        # 处理没有分配到控制器的交换机（可能没有控制器连接）
        unassigned_switches = [s for s in switches if s not in pos]
        if unassigned_switches:
            switch_count = len(unassigned_switches)
            margin = 2.0
            available_width = width - 2 * margin
            if switch_count > 1:
                spacing = available_width / (switch_count - 1)
            else:
                spacing = 0
            for i, switch_node in enumerate(unassigned_switches):
                switch_x = margin + i * spacing
                pos[switch_node] = (switch_x, switch_y)
                assigned_switch_positions.append((switch_x, switch_y))
        
        # Layer 3: 主机（底层，按交换机分组）
        host_y = height - 5.0
        
        # 为主机分配位置
        for switch_node in switches:
            if switch_node not in pos:
                continue
            
            switch_hosts = switch_to_hosts.get(switch_node, [])
            if not switch_hosts:
                continue
            
            # 获取交换机的x坐标
            switch_x = pos[switch_node][0]
            
            # 计算主机数量
            host_count = len(switch_hosts)
            
            # 在交换机下方均匀排列主机
            if host_count == 1:
                host_x_positions = [switch_x]
            else:
                # 主机分布范围（交换机左右各延伸一定距离）
                host_span = min(2.0, max(0.8, host_count * 0.5))
                if host_count > 1:
                    spacing = (host_span * 2) / (host_count - 1)
                else:
                    spacing = 0
                host_x_positions = [switch_x - host_span + i * spacing 
                                   for i in range(host_count)]
            
            for i, host_node in enumerate(switch_hosts):
                pos[host_node] = (host_x_positions[i], host_y)
        
        # 处理没有分配到交换机的主机
        unassigned_hosts = [h for h in hosts if h not in pos]
        if unassigned_hosts:
            host_count = len(unassigned_hosts)
            margin = 1.0
            available_width = width - 2 * margin
            if host_count > 1:
                spacing = available_width / (host_count - 1)
            else:
                spacing = 0
            for i, host_node in enumerate(unassigned_hosts):
                pos[host_node] = (margin + i * spacing, host_y)
        
        # 归一化坐标到 [0, 1] 范围（matplotlib 会自动处理）
        # 但我们可以保持绝对坐标，让布局更清晰
        
        return pos

    def _plot_edge_with_offset(self, u, v, pos, color, lw, alpha, offset=0.0, z=1):
        """
        绘制带侧向偏移的边，减少平行/反向边的完全重叠。
        offset 为正时向左/上偏移，为负时向右/下偏移。
        """
        import math
        
        if u not in pos or v not in pos:
            return
        
        x1, y1 = pos[u]
        x2, y2 = pos[v]
        
        # 检查坐标有效性
        if not (math.isfinite(x1) and math.isfinite(y1) and math.isfinite(x2) and math.isfinite(y2)):
            logger.warning(f"边 ({u}, {v}) 的坐标无效，跳过绘制")
            return
        
        dx, dy = x2 - x1, y2 - y1
        length = (dx ** 2 + dy ** 2) ** 0.5
        
        if not math.isfinite(length) or length == 0:
            return
        
        # 计算法向偏移
        nx, ny = -dy / length, dx / length
        x1_o, y1_o = x1 + nx * offset, y1 + ny * offset
        x2_o, y2_o = x2 + nx * offset, y2 + ny * offset
        
        # 再次检查偏移后的坐标
        if not (math.isfinite(x1_o) and math.isfinite(y1_o) and math.isfinite(x2_o) and math.isfinite(y2_o)):
            logger.warning(f"边 ({u}, {v}) 偏移后的坐标无效，跳过绘制")
            return
        
        self.ax.plot([x1_o, x2_o], [y1_o, y2_o], color=color, alpha=alpha, linewidth=lw, zorder=z)
    
    def refresh_topo(self):
        """刷新拓扑图"""
        try:
            # 清空当前图形
            self.ax.clear()
            self.ax.set_title("Network Topology", fontsize=14, fontweight='bold')
            self.ax.axis('off')
            
            # 获取网络图
            G = self.server_agent.G
            
            if len(G.nodes()) == 0:
                self.ax.text(0.5, 0.5, "No Topology Data\nWaiting for controllers...", 
                             ha='center', va='center', fontsize=16, 
                             transform=self.ax.transAxes)
                self.canvas.draw()
                self.update_statistics()
                return
            
            # 使用改进的布局算法
            pos = self.improved_layout(G)
            
            # 清理布局中的NaN和inf值
            pos = self._clean_layout_positions(pos)
            
            # 保存节点位置用于交互
            self.node_positions = pos.copy()
            
            # 设置坐标轴范围以适应层次布局
            if pos:
                x_coords = [p[0] for p in pos.values()]
                y_coords = [p[1] for p in pos.values()]
                
                # 过滤掉NaN和inf值
                import math
                x_coords = [x for x in x_coords if math.isfinite(x)]
                y_coords = [y for y in y_coords if math.isfinite(y)]
                
                if x_coords and y_coords:
                    x_min, x_max = min(x_coords), max(x_coords)
                    y_min, y_max = min(y_coords), max(y_coords)
                    
                    # 验证计算结果是否有效
                    if math.isfinite(x_min) and math.isfinite(x_max) and math.isfinite(y_min) and math.isfinite(y_max):
                        # 添加边距
                        x_margin = (x_max - x_min) * 0.1 if x_max > x_min else 1
                        y_margin = (y_max - y_min) * 0.1 if y_max > y_min else 1
                        self.ax.set_xlim(x_min - x_margin, x_max + x_margin)
                        self.ax.set_ylim(y_min - y_margin, y_max + y_margin)
                    else:
                        # 使用默认范围
                        self.ax.set_xlim(-1, 11)
                        self.ax.set_ylim(-1, 9)
                else:
                    # 没有有效坐标，使用默认范围
                    self.ax.set_xlim(-1, 11)
                    self.ax.set_ylim(-1, 9)
            
            # 分离节点类型
            switches = []
            hosts = []
            controllers = []
            root_controller = None
            
            for node in G.nodes():
                node_data = G.nodes[node]
                node_type = node_data.get('node_type', 'unknown')
                
                if node_type == 'root_controller':
                    root_controller = node
                elif node_type == 'controller':
                    controllers.append(node)
                elif node_type == 'switch':
                    switches.append(node)
                elif node_type == 'host':
                    hosts.append(node)
                else:
                    # 兼容旧数据：根据节点名称判断
                    if isinstance(node, str) and '.' in node and node.count('.') == 3:
                        # 看起来像IP地址，可能是主机
                        hosts.append(node)
                    elif isinstance(node, (int, str)) and str(node).isdigit():
                        # 可能是交换机ID
                        switches.append(node)
                    elif node.startswith('Controller_'):
                        controllers.append(node)
                    else:
                        hosts.append(node)
            
            # 分离不同类型的边
            switch_links = []
            host_switch_links = []
            controller_switch_links = []
            controller_links = []
            
            for edge in G.edges(data=True):
                edge_type = edge[2].get('edge_type', 'unknown')
                if edge_type == 'switch_link':
                    switch_links.append((edge[0], edge[1]))
                elif edge_type == 'host_switch':
                    host_switch_links.append((edge[0], edge[1]))
                elif edge_type == 'controller_switch':
                    controller_switch_links.append((edge[0], edge[1]))
                elif edge_type == 'controller_connection':
                    controller_links.append((edge[0], edge[1]))
                else:
                    # 默认归类为交换机链路
                    switch_links.append((edge[0], edge[1]))
            
            # 预处理反向/重复边，给出轻微偏移以减小重叠
            bidir = set()
            for (u, v) in switch_links:
                if (v, u) in switch_links:
                    bidir.add(tuple(sorted((u, v))))
            for (u, v) in controller_links:
                if (v, u) in controller_links:
                    bidir.add(tuple(sorted((u, v))))

            def edge_offset(u, v):
                key = tuple(sorted((u, v)))
                if key in bidir:
                    return 0.08 if (u < v) else -0.08
                return 0.0

            # 绘制边 - 按类型用不同颜色，带偏移避免完全重叠
            if switch_links:
                for (u, v) in switch_links:
                    self._plot_edge_with_offset(u, v, pos, color='gray', lw=1.5, alpha=0.6,
                                                offset=edge_offset(u, v), z=1)
            
            if controller_links:
                for (u, v) in controller_links:
                    self._plot_edge_with_offset(u, v, pos, color='red', lw=2.2, alpha=0.8,
                                                offset=edge_offset(u, v), z=2)
            
            if controller_switch_links:
                for (u, v) in controller_switch_links:
                    self._plot_edge_with_offset(u, v, pos, color='orange', lw=1.6, alpha=0.7,
                                                offset=0.0, z=2)
            
            if host_switch_links:
                for (u, v) in host_switch_links:
                    self._plot_edge_with_offset(u, v, pos, color='green', lw=1.0, alpha=0.5,
                                                offset=0.0, z=1)
            
            # 绘制根控制器节点
            if root_controller and root_controller in pos:
                import math
                x, y = pos[root_controller]
                if math.isfinite(x) and math.isfinite(y):
                    self.ax.scatter([x], [y], 
                              c='red', s=1500, marker='*', edgecolors='darkred', 
                              linewidths=2, alpha=0.9, zorder=5)
                    self.ax.text(x, y, 
                           'Root', ha='center', va='center', fontsize=10, 
                           fontweight='bold', color='white', zorder=6)
            
            # 绘制从控制器节点
            if controllers:
                import math
                controller_pos = {c: pos[c] for c in controllers if c in pos}
                if controller_pos:
                    # 提取坐标，过滤无效值
                    valid_controllers = []
                    x_coords = []
                    y_coords = []
                    for c in controller_pos.keys():
                        x, y = pos[c]
                        if math.isfinite(x) and math.isfinite(y):
                            valid_controllers.append(c)
                            x_coords.append(x)
                            y_coords.append(y)
                    
                    if x_coords and y_coords:
                        self.ax.scatter(x_coords, y_coords, 
                                        c='purple', s=1000, marker='D', 
                                        edgecolors='darkviolet', linewidths=2, 
                                        alpha=0.9, zorder=4)
                        controller_pos = {c: pos[c] for c in valid_controllers}
                    
                    # 添加标签
                    controller_labels = {}
                    for controller in controller_pos.keys():
                        # 提取IP地址和端口
                        if controller.startswith('Controller_'):
                            # 格式: Controller_IP_PORT
                            parts = controller.split('_', 2)
                            if len(parts) >= 3:
                                ip = parts[1]
                                port = parts[2]
                                controller_labels[controller] = f"Ctrl\n{ip}:{port}"
                            elif len(parts) == 2:
                                ip = parts[1]
                                controller_labels[controller] = f"Ctrl\n{ip}"
                            else:
                                controller_labels[controller] = controller
                        else:
                            controller_labels[controller] = controller
                    
                    for controller, (x, y) in controller_pos.items():
                        self.ax.text(x, y, controller_labels.get(controller, controller), 
                                   ha='center', va='center', fontsize=7, 
                                   fontweight='bold', color='white', zorder=5)
            
            # 绘制交换机节点
            if switches:
                import math
                switch_pos = {s: pos[s] for s in switches if s in pos}
                if switch_pos:
                    # 提取坐标，过滤无效值
                    valid_switches = []
                    x_coords = []
                    y_coords = []
                    for s in switch_pos.keys():
                        x, y = pos[s]
                        if math.isfinite(x) and math.isfinite(y):
                            valid_switches.append(s)
                            x_coords.append(x)
                            y_coords.append(y)
                    
                    if x_coords and y_coords:
                        self.ax.scatter(x_coords, y_coords,
                                      c='lightblue', s=800, marker='s', 
                                      edgecolors='darkblue', linewidths=1.5, 
                                      alpha=0.9, zorder=3)
                        switch_pos = {s: pos[s] for s in valid_switches}
                    
                    # 添加标签
                    for switch, (x, y) in switch_pos.items():
                        self.ax.text(x, y, f"SW{switch}", 
                                   ha='center', va='center', fontsize=8, 
                                   fontweight='bold', zorder=4)
            
            # 绘制主机节点
            if hosts:
                import math
                host_pos = {h: pos[h] for h in hosts if h in pos}
                if host_pos:
                    # 提取坐标，过滤无效值
                    valid_hosts = []
                    x_coords = []
                    y_coords = []
                    for h in host_pos.keys():
                        x, y = pos[h]
                        if math.isfinite(x) and math.isfinite(y):
                            valid_hosts.append(h)
                            x_coords.append(x)
                            y_coords.append(y)
                    
                    if x_coords and y_coords:
                        self.ax.scatter(x_coords, y_coords,
                                      c='lightgreen', s=500, marker='o', 
                                      edgecolors='darkgreen', linewidths=1, 
                                      alpha=0.9, zorder=3)
                        host_pos = {h: pos[h] for h in valid_hosts}
                    
                    # 添加标签（只显示IP地址）
                    host_labels = {}
                    for host in host_pos.keys():
                        if isinstance(host, str) and '.' in host:
                            # 简化IP显示（只显示最后一部分）
                            parts = host.split('.')
                            if len(parts) == 4:
                                host_labels[host] = f".{parts[-1]}"
                            else:
                                host_labels[host] = host
                        else:
                            host_labels[host] = f"H{host}"
                    
                    for host, (x, y) in host_pos.items():
                        self.ax.text(x, y, host_labels.get(host, host), 
                                   ha='center', va='center', fontsize=6, 
                                   zorder=4)
            
            # 添加图例
            legend_elements = [
                plt.Line2D([0], [0], marker='*', color='w', 
                          markerfacecolor='red', markersize=15, 
                          markeredgecolor='darkred', markeredgewidth=2,
                          label='Root Controller'),
                plt.Line2D([0], [0], marker='D', color='w', 
                          markerfacecolor='purple', markersize=12, 
                          markeredgecolor='darkviolet', markeredgewidth=2,
                          label='Sub Controller'),
                plt.Line2D([0], [0], marker='s', color='w', 
                          markerfacecolor='lightblue', markersize=10, 
                          markeredgecolor='darkblue',
                          label='Switch'),
                plt.Line2D([0], [0], marker='o', color='w', 
                          markerfacecolor='lightgreen', markersize=8, 
                          markeredgecolor='darkgreen',
                          label='Host'),
                plt.Line2D([0], [0], color='red', linestyle='--', linewidth=2.5,
                          label='Controller Link'),
                plt.Line2D([0], [0], color='orange', linestyle=':', linewidth=2,
                          label='Controller-Switch'),
                plt.Line2D([0], [0], color='gray', linewidth=1.5,
                          label='Switch Link')
            ]
            self.ax.legend(handles=legend_elements, loc='upper left', fontsize=8, 
                          framealpha=0.9)
            
            # 缓存节点数据用于交互
            self.node_data_cache = {}
            for node in G.nodes():
                node_data = G.nodes[node]
                node_type = node_data.get('node_type', 'unknown')
                self.node_data_cache[node] = {
                    'type': node_type,
                    'data': node_data,
                    'neighbors': list(G.neighbors(node))
                }
            
            # 更新统计信息
            self.update_statistics()
            
            # 刷新画布
            self.canvas.draw()
            
        except Exception as e:
            logger.error(f"刷新拓扑图时出错: {e}")
            traceback.print_exc()
            self.ax.text(0.5, 0.5, f"Error drawing topology:\n{str(e)}", 
                        ha='center', va='center', fontsize=12, 
                        transform=self.ax.transAxes, color='red')
            self.canvas.draw()
    
    def get_node_info(self, node):
        """获取节点的详细信息"""
        if node not in self.node_data_cache:
            return None
        
        node_info = self.node_data_cache[node]
        node_type = node_info['type']
        node_data = node_info['data']
        neighbors = node_info['neighbors']
        
        info_text = f"Node: {node}\n"
        info_text += f"Type: {node_type}\n"
        info_text += f"Neighbors: {len(neighbors)}\n"
        
        # 根据节点类型添加特定信息
        if node_type == 'switch':
            info_text += f"Switch ID: {node}\n"
            # 获取连接的控制器信息
            G = self.server_agent.G
            for edge in G.edges(node, data=True):
                if edge[2].get('edge_type') == 'controller_switch':
                    controller = edge[1] if edge[0] == node else edge[0]
                    info_text += f"Controller: {controller}\n"
                    break
            # 获取连接的主机
            host_count = sum(1 for n in neighbors if self.node_data_cache.get(n, {}).get('type') == 'host')
            info_text += f"Connected Hosts: {host_count}\n"
        elif node_type == 'host':
            info_text += f"IP Address: {node}\n"
            if 'mac' in node_data:
                info_text += f"MAC Address: {node_data['mac']}\n"
            # 获取连接的交换机
            G = self.server_agent.G
            for edge in G.edges(node, data=True):
                if edge[2].get('edge_type') == 'host_switch':
                    switch = edge[1] if edge[0] == node else edge[0]
                    info_text += f"Connected Switch: {switch}\n"
                    break
        elif node_type == 'controller':
            info_text += f"Controller: {node}\n"
            # 获取管理的交换机数
            switch_count = sum(1 for n in neighbors if self.node_data_cache.get(n, {}).get('type') == 'switch')
            info_text += f"Managed Switches: {switch_count}\n"
        
        return info_text
    
    def on_hover(self, event):
        """鼠标悬停事件处理"""
        if event.inaxes != self.ax:
            if self.hover_annotation:
                self.hover_annotation.remove()
                self.hover_annotation = None
                self.canvas.draw_idle()
            return
        
        # 清除之前的悬停标注
        if self.hover_annotation:
            self.hover_annotation.remove()
            self.hover_annotation = None
        
        # 查找鼠标附近的节点
        min_distance = float('inf')
        closest_node = None
        
        for node, (x, y) in self.node_positions.items():
            # 计算鼠标位置到节点的距离
            distance = ((event.xdata - x) ** 2 + (event.ydata - y) ** 2) ** 0.5
            # 根据节点类型设置不同的检测半径
            node_type = self.node_data_cache.get(node, {}).get('type', 'unknown')
            if node_type == 'switch':
                radius = 0.15
            elif node_type == 'host':
                radius = 0.1
            elif node_type in ['controller', 'root_controller']:
                radius = 0.2
            else:
                radius = 0.1
            
            if distance < radius and distance < min_distance:
                min_distance = distance
                closest_node = node
        
        # 如果找到节点，显示信息
        if closest_node:
            info_text = self.get_node_info(closest_node)
            if info_text:
                # 创建悬停提示框
                self.hover_annotation = self.ax.annotate(
                    info_text,
                    xy=self.node_positions[closest_node],
                    xytext=(10, 10),
                    textcoords='offset points',
                    bbox=dict(boxstyle='round,pad=0.5', facecolor='yellow', alpha=0.8),
                    fontsize=8,
                    family='monospace'
                )
                self.canvas.draw_idle()
    
    def on_click(self, event):
        """鼠标点击事件处理"""
        if event.inaxes != self.ax or event.button != 1:  # 只处理左键点击
            return
        
        # 查找点击的节点
        min_distance = float('inf')
        clicked_node = None
        
        for node, (x, y) in self.node_positions.items():
            distance = ((event.xdata - x) ** 2 + (event.ydata - y) ** 2) ** 0.5
            node_type = self.node_data_cache.get(node, {}).get('type', 'unknown')
            if node_type == 'switch':
                radius = 0.15
            elif node_type == 'host':
                radius = 0.1
            elif node_type in ['controller', 'root_controller']:
                radius = 0.2
            else:
                radius = 0.1
            
            if distance < radius and distance < min_distance:
                min_distance = distance
                clicked_node = node
        
        # 如果点击了节点，显示详细信息窗口
        if clicked_node:
            self.show_node_details(clicked_node)
    
    def show_node_details(self, node):
        """显示节点的详细信息窗口"""
        # 关闭之前的窗口
        if self.node_info_window:
            self.node_info_window.destroy()
        
        # 创建新窗口
        self.node_info_window = tk.Toplevel(self.root)
        self.node_info_window.title(f"Node Details: {node}")
        self.node_info_window.geometry("400x300")
        
        # 创建文本区域
        text_frame = ttk.Frame(self.node_info_window, padding="10")
        text_frame.pack(fill=tk.BOTH, expand=True)
        
        # 获取详细信息
        info_text = self.get_node_info(node)
        if not info_text:
            info_text = f"Node: {node}\nNo detailed information"
        
        # 添加更详细的信息
        G = self.server_agent.G
        node_data = G.nodes[node]
        node_type = node_data.get('node_type', 'unknown')
        
        # 获取连接的边信息
        edges_info = []
        for edge in G.edges(node, data=True):
            neighbor = edge[1] if edge[0] == node else edge[0]
            edge_data = edge[2]
            edge_type = edge_data.get('edge_type', 'unknown')
            weight = edge_data.get('weight', 1)
            edges_info.append(f"  -> {neighbor} (Type: {edge_type}, Weight: {weight})")
        
        # 创建详细信息文本
        detailed_text = info_text + "\n\nConnection Info:\n"
        if edges_info:
            detailed_text += "\n".join(edges_info[:10])  # 最多显示10个连接
            if len(edges_info) > 10:
                detailed_text += f"\n... {len(edges_info) - 10} more connections"
        else:
            detailed_text += "  No connections"
        
        # 显示文本
        text_widget = tk.Text(text_frame, wrap=tk.WORD, font=("Courier", 10))
        text_widget.pack(fill=tk.BOTH, expand=True)
        text_widget.insert(tk.END, detailed_text)
        text_widget.config(state=tk.DISABLED)  # 只读
        
        # 添加关闭按钮
        button_frame = ttk.Frame(self.node_info_window)
        button_frame.pack(fill=tk.X, padx=10, pady=5)
        close_btn = ttk.Button(button_frame, text="Close", 
                               command=self.node_info_window.destroy)
        close_btn.pack(side=tk.RIGHT)
    
    def update_topo_loop(self):
        """定时更新拓扑图的循环"""
        try:
            self.refresh_topo()
        except Exception as e:
            logger.error(f"更新拓扑图时出错: {e}")
        
        # 3秒后再次更新
        self.root.after(3000, self.update_topo_loop)

def main():
    """主函数"""
    global server_agent
    
    # 创建ServerAgent实例并赋值给全局变量
    server_agent = ServerAgent(CONTROLLER_IP, CONTROLLER_PORT)
    
    # 注册信号处理器
    def signal_handler(sig, frame):
        print("\n接收到中断信号，正在关闭服务器...")
        server_agent.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 启动服务器
    server_agent.start()

if __name__ == "__main__":
    main()

