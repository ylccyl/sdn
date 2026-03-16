#!/usr/bin/env python
"""
修复后的Mininet网络拓扑创建脚本
主要修复:
1. 统一端口规划，避免端口冲突
2. 域间链路使用高端口号(20+)，避免与主机端口冲突
3. 清晰的端口分配策略
"""

import subprocess
import re
import time
from mininet.net import Mininet
from mininet.node import RemoteController, Host, OVSKernelSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info, error
from mininet.link import TCLink, Intf
from mininet.util import quietRun

# 控制器配置
CONTROLLER_IP = '10.5.1.163'
CONTROLLER_BASE_PORT = 6654

# 物理网卡配置（可选）
PHYSICAL_INTERFACE = 'eno1'  # 例如: 'eno1'
PHYSICAL_SWITCH = 's1'

# ===== 端口分配策略 =====
# 为了避免混淆和冲突，采用统一的端口分配策略：
# - 端口 1-10:  域内交换机互联
# - 端口 11-19: 主机连接
# - 端口 20-29: 域间交换机互联


def checkIntf(intf):
    """检查接口是否存在"""
    config = quietRun('ifconfig %s 2>/dev/null' % intf, shell=True)
    if not config:
        error('Error:', intf, 'does not exist!\n')
        return False
    
    try:
        result = subprocess.run(['ip', 'addr', 'show', intf], 
                              capture_output=True, text=True, check=True)
        ips = re.findall(r'inet\s+(\d+\.\d+\.\d+\.\d+/\d+)', result.stdout)
        if ips:
            info(f"警告: {intf} 有IP地址 {ips[0]}，将临时移除以便添加到交换机\n")
            return True
    except:
        pass
    
    return True


def save_network_config(intf):
    """保存物理网卡的原始网络配置"""
    config = {}
    try:
        result = subprocess.run(['ip', 'addr', 'show', intf], 
                              capture_output=True, text=True, check=True)
        ip_match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+/\d+)', result.stdout)
        if ip_match:
            config['ip'] = ip_match.group(1)
        
        result = subprocess.run(['ip', 'route', 'show', 'default'], 
                              capture_output=True, text=True)
        gw_match = re.search(r'default via (\d+\.\d+\.\d+\.\d+)', result.stdout)
        if gw_match:
            config['gateway'] = gw_match.group(1)
            
        info(f"保存网络配置: IP={config.get('ip', 'None')}, Gateway={config.get('gateway', 'None')}\n")
        return config
    except Exception as e:
        error(f"保存网络配置失败: {e}\n")
        return {}


def restore_network_config(intf, config):
    """恢复物理网卡的原始网络配置"""
    try:
        subprocess.run(['ovs-vsctl', 'del-port', PHYSICAL_SWITCH, intf], 
                      capture_output=True, check=False)
        
        subprocess.run(['ip', 'link', 'set', intf, 'up'], 
                      capture_output=True, check=False)
        
        if 'ip' in config:
            subprocess.run(['ip', 'addr', 'add', config['ip'], 'dev', intf], 
                          capture_output=True, check=False)
        
        if 'gateway' in config:
            subprocess.run(['ip', 'route', 'replace', 'default', 'via', config['gateway'], 'dev', intf], 
                          capture_output=True, check=False)
        
        info(f"网络配置已恢复\n")
    except Exception as e:
        error(f"恢复网络配置失败: {e}\n")


def add_hardware_interface(net, switch):
    """将物理接口添加到指定的交换机"""
    if not PHYSICAL_INTERFACE:
        return {}
    
    try:
        original_config = save_network_config(PHYSICAL_INTERFACE)
        
        if not checkIntf(PHYSICAL_INTERFACE):
            error(f"接口 {PHYSICAL_INTERFACE} 检查失败\n")
            return {}
        
        if 'ip' in original_config:
            info(f"临时移除 {PHYSICAL_INTERFACE} 的IP地址以便添加到交换机...\n")
            subprocess.run(['ip', 'addr', 'flush', 'dev', PHYSICAL_INTERFACE], 
                          capture_output=True, check=False)
        
        info(f"*** 添加硬件接口 {PHYSICAL_INTERFACE} 到交换机 {switch.name}\n")
        _intf = Intf(PHYSICAL_INTERFACE, node=switch)
        
        subprocess.run(['ip', 'link', 'set', PHYSICAL_INTERFACE, 'up'], 
                      capture_output=True, check=False)
        
        info(f"✓ 硬件接口添加完成:\n")
        info(f"  - {PHYSICAL_INTERFACE} 已添加到 {switch.name}\n")
        
        return original_config
        
    except Exception as e:
        error(f"添加硬件接口失败: {e}\n")
        return {}


class ComplexTopo:
    """
    修复后的复杂网络拓扑类
    端口分配策略：
    - 域内链路: 端口1-10
    - 主机连接: 端口11-19  
    - 域间链路: 端口20-29
    """
    
    def __init__(self):
        self.net = None
        
    def build(self):
        """构建网络拓扑"""
        info('*** 创建Mininet网络\n')
        
        self.net = Mininet(controller=RemoteController,
                           switch=OVSKernelSwitch,
                           link=TCLink)
        
        info('*** 添加控制器\n')
        controllers = []
        for i in range(6):
            controller = self.net.addController(
                name='c%d' % (i+1),
                controller=RemoteController,
                ip=CONTROLLER_IP,
                port=CONTROLLER_BASE_PORT + i
            )
            controllers.append(controller)
            info('控制器 c%d: %s:%d\n' % (i+1, CONTROLLER_IP, CONTROLLER_BASE_PORT + i))
        
        info('*** 添加交换机\n')
        # Domain 1
        s1 = self.net.addSwitch('s1', dpid='0000000000000001', protocols='OpenFlow13')
        s2 = self.net.addSwitch('s2', dpid='0000000000000002', protocols='OpenFlow13')
        
        # Domain 2
        s3 = self.net.addSwitch('s3', dpid='0000000000000003', protocols='OpenFlow13')
        s4 = self.net.addSwitch('s4', dpid='0000000000000004', protocols='OpenFlow13')
        
        # Domain 3
        s5 = self.net.addSwitch('s5', dpid='0000000000000005', protocols='OpenFlow13')
        
        # Domain 4
        s6 = self.net.addSwitch('s6', dpid='0000000000000006', protocols='OpenFlow13')
        s7 = self.net.addSwitch('s7', dpid='0000000000000007', protocols='OpenFlow13')
        
        # Domain 5
        s8 = self.net.addSwitch('s8', dpid='0000000000000008', protocols='OpenFlow13')
        
        # Domain 6
        s9 = self.net.addSwitch('s9', dpid='0000000000000009', protocols='OpenFlow13')
        s10 = self.net.addSwitch('s10', dpid='000000000000000a', protocols='OpenFlow13')
        
        info('*** 添加域内交换机连接 (端口1-10)\n')
        # Domain 1: s1 <-> s2
        self.net.addLink(s1, s2, port1=1, port2=1)
        
        # Domain 2: s3 <-> s4
        self.net.addLink(s3, s4, port1=1, port2=1)
        
        # Domain 4: s6 <-> s7
        self.net.addLink(s6, s7, port1=1, port2=1)
        
        # Domain 6: s9 <-> s10
        self.net.addLink(s9, s10, port1=1, port2=1)
        
        info('*** 添加域间交换机连接 (端口20-29)\n')
        # 关键修复: 使用端口20+，避免与主机端口冲突
        # Domain 1 <-> Domain 2: s2 <-> s3
        self.net.addLink(s2, s3, port1=20, port2=20)
        info('  域间链路: s2:20 <-> s3:20\n')
        
        # Domain 2 <-> Domain 3: s4 <-> s5
        self.net.addLink(s4, s5, port1=20, port2=20)
        info('  域间链路: s4:20 <-> s5:20\n')
        
        # Domain 3 <-> Domain 4: s5 <-> s6
        self.net.addLink(s5, s6, port1=21, port2=20)
        info('  域间链路: s5:21 <-> s6:20\n')
        
        # Domain 4 <-> Domain 5: s7 <-> s8
        self.net.addLink(s7, s8, port1=20, port2=20)
        info('  域间链路: s7:20 <-> s8:20\n')
        
        # Domain 5 <-> Domain 6: s8 <-> s9
        self.net.addLink(s8, s9, port1=21, port2=20)
        info('  域间链路: s8:21 <-> s9:20\n')
        
        info('*** 添加主机 (端口11-19)\n')
        # Domain 1: s1连接3个主机, s2连接2个主机
        h1 = self.net.addHost('h1')
        h2 = self.net.addHost('h2')
        h3 = self.net.addHost('h3')
        self.net.addLink(s1, h1, port1=11, port2=1)
        self.net.addLink(s1, h2, port1=12, port2=1)
        self.net.addLink(s1, h3, port1=13, port2=1)
        
        h4 = self.net.addHost('h4')
        h5 = self.net.addHost('h5')
        self.net.addLink(s2, h4, port1=11, port2=1)
        self.net.addLink(s2, h5, port1=12, port2=1)
        
        # Domain 2: s3连接3个主机, s4连接2个主机
        h6 = self.net.addHost('h6')
        h7 = self.net.addHost('h7')
        h8 = self.net.addHost('h8')
        self.net.addLink(s3, h6, port1=11, port2=1)
        self.net.addLink(s3, h7, port1=12, port2=1)
        self.net.addLink(s3, h8, port1=13, port2=1)
        
        h9 = self.net.addHost('h9')
        h10 = self.net.addHost('h10')
        self.net.addLink(s4, h9, port1=11, port2=1)
        self.net.addLink(s4, h10, port1=12, port2=1)
        
        # Domain 3: s5连接3个主机
        h11 = self.net.addHost('h11')
        h12 = self.net.addHost('h12')
        h13 = self.net.addHost('h13')
        self.net.addLink(s5, h11, port1=11, port2=1)
        self.net.addLink(s5, h12, port1=12, port2=1)
        self.net.addLink(s5, h13, port1=13, port2=1)
        
        # Domain 4: s6连接2个主机, s7连接3个主机
        h14 = self.net.addHost('h14')
        h15 = self.net.addHost('h15')
        self.net.addLink(s6, h14, port1=11, port2=1)
        self.net.addLink(s6, h15, port1=12, port2=1)
        
        h16 = self.net.addHost('h16')
        h17 = self.net.addHost('h17')
        h18 = self.net.addHost('h18')
        self.net.addLink(s7, h16, port1=11, port2=1)
        self.net.addLink(s7, h17, port1=12, port2=1)
        self.net.addLink(s7, h18, port1=13, port2=1)
        
        # Domain 5: s8连接3个主机
        h19 = self.net.addHost('h19')
        h20 = self.net.addHost('h20')
        h21 = self.net.addHost('h21')
        self.net.addLink(s8, h19, port1=11, port2=1)
        self.net.addLink(s8, h20, port1=12, port2=1)
        self.net.addLink(s8, h21, port1=13, port2=1)
        
        # Domain 6: s9连接2个主机, s10连接3个主机
        h22 = self.net.addHost('h22')
        h23 = self.net.addHost('h23')
        self.net.addLink(s9, h22, port1=11, port2=1)
        self.net.addLink(s9, h23, port1=12, port2=1)
        
        h24 = self.net.addHost('h24')
        h25 = self.net.addHost('h25')
        h26 = self.net.addHost('h26')
        self.net.addLink(s10, h24, port1=11, port2=1)
        self.net.addLink(s10, h25, port1=12, port2=1)
        self.net.addLink(s10, h26, port1=13, port2=1)
        
        info('*** 配置交换机与控制器连接\n')
        # Domain 1: s1, s2 -> c1
        info('配置 Domain 1 交换机连接到控制器 c1 (%s:%d)\n' % (CONTROLLER_IP, CONTROLLER_BASE_PORT))
        s1.start([controllers[0]])
        s2.start([controllers[0]])
        s1.cmd('ovs-vsctl set-controller s1 tcp:%s:%d' % (CONTROLLER_IP, CONTROLLER_BASE_PORT))
        s2.cmd('ovs-vsctl set-controller s2 tcp:%s:%d' % (CONTROLLER_IP, CONTROLLER_BASE_PORT))
        # 关键修复：禁用OVS的LLDP特殊处理，允许LLDP包跨交换机转发
        s1.cmd('ovs-vsctl set bridge s1 other-config:forward-bpdu=true')
        s2.cmd('ovs-vsctl set bridge s2 other-config:forward-bpdu=true')
        
        # Domain 2: s3, s4 -> c2
        info('配置 Domain 2 交换机连接到控制器 c2 (%s:%d)\n' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 1))
        s3.start([controllers[1]])
        s4.start([controllers[1]])
        s3.cmd('ovs-vsctl set-controller s3 tcp:%s:%d' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 1))
        s4.cmd('ovs-vsctl set-controller s4 tcp:%s:%d' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 1))
        s3.cmd('ovs-vsctl set bridge s3 other-config:forward-bpdu=true')
        s4.cmd('ovs-vsctl set bridge s4 other-config:forward-bpdu=true')
        
        # Domain 3: s5 -> c3
        info('配置 Domain 3 交换机连接到控制器 c3 (%s:%d)\n' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 2))
        s5.start([controllers[2]])
        s5.cmd('ovs-vsctl set-controller s5 tcp:%s:%d' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 2))
        s5.cmd('ovs-vsctl set bridge s5 other-config:forward-bpdu=true')
        
        # Domain 4: s6, s7 -> c4
        info('配置 Domain 4 交换机连接到控制器 c4 (%s:%d)\n' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 3))
        s6.start([controllers[3]])
        s7.start([controllers[3]])
        s6.cmd('ovs-vsctl set-controller s6 tcp:%s:%d' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 3))
        s7.cmd('ovs-vsctl set-controller s7 tcp:%s:%d' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 3))
        s6.cmd('ovs-vsctl set bridge s6 other-config:forward-bpdu=true')
        s7.cmd('ovs-vsctl set bridge s7 other-config:forward-bpdu=true')
        
        # Domain 5: s8 -> c5
        info('配置 Domain 5 交换机连接到控制器 c5 (%s:%d)\n' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 4))
        s8.start([controllers[4]])
        s8.cmd('ovs-vsctl set-controller s8 tcp:%s:%d' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 4))
        s8.cmd('ovs-vsctl set bridge s8 other-config:forward-bpdu=true')
        
        # Domain 6: s9, s10 -> c6
        info('配置 Domain 6 交换机连接到控制器 c6 (%s:%d)\n' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 5))
        s9.start([controllers[5]])
        s10.start([controllers[5]])
        s9.cmd('ovs-vsctl set-controller s9 tcp:%s:%d' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 5))
        s10.cmd('ovs-vsctl set-controller s10 tcp:%s:%d' % (CONTROLLER_IP, CONTROLLER_BASE_PORT + 5))
        s9.cmd('ovs-vsctl set bridge s9 other-config:forward-bpdu=true')
        s10.cmd('ovs-vsctl set bridge s10 other-config:forward-bpdu=true')
        
        info('*** 网络拓扑创建完成\n')
        info('\n端口分配汇总:\n')
        info('  域内链路端口: 1-10\n')
        info('  主机连接端口: 11-19\n')
        info('  域间链路端口: 20-29\n')
        info('\n域间连接:\n')
        info('  s2:20 <-> s3:20 (Domain 1 <-> Domain 2)\n')
        info('  s4:20 <-> s5:20 (Domain 2 <-> Domain 3)\n')
        info('  s5:21 <-> s6:20 (Domain 3 <-> Domain 4)\n')
        info('  s7:20 <-> s8:20 (Domain 4 <-> Domain 5)\n')
        info('  s8:21 <-> s9:20 (Domain 5 <-> Domain 6)\n')
        
        return self.net


def main():
    """主函数"""
    setLogLevel('info')
    
    topo = ComplexTopo()
    net = topo.build()
    
    # 添加硬件接口
    original_config = {}
    if PHYSICAL_INTERFACE:
        info('\n*** 添加硬件接口到交换机\n')
        try:
            switch = net.get(PHYSICAL_SWITCH)
            original_config = add_hardware_interface(net, switch)
        except Exception as e:
            error(f"警告: 添加硬件接口失败: {e}\n")
    
    # 启动网络
    info('*** 启动网络\n')
    net.start()

    # 配置主机默认路由
    info('*** 配置主机默认路由\n')
    try:
        for host in net.hosts:
            intf = host.defaultIntf()
            intf_name = intf.name
            host.cmd(f'ip route replace default dev {intf_name}')
            info('  %s: 默认路由 -> %s\n' % (host.name, intf_name))
    except Exception as e:
        error(f"配置主机默认路由失败: {e}\n")
    
    # 确认控制器连接
    info('*** 确认交换机控制器连接\n')
    switch_controller_map = {
        's1': CONTROLLER_BASE_PORT, 's2': CONTROLLER_BASE_PORT,
        's3': CONTROLLER_BASE_PORT + 1, 's4': CONTROLLER_BASE_PORT + 1,
        's5': CONTROLLER_BASE_PORT + 2,
        's6': CONTROLLER_BASE_PORT + 3, 's7': CONTROLLER_BASE_PORT + 3,
        's8': CONTROLLER_BASE_PORT + 4,
        's9': CONTROLLER_BASE_PORT + 5, 's10': CONTROLLER_BASE_PORT + 5
    }
    
    for switch_name, expected_port in switch_controller_map.items():
        switch = net.get(switch_name)
        switch.cmd('ovs-vsctl set-controller %s tcp:%s:%d' % (switch_name, CONTROLLER_IP, expected_port))
        controller_info = switch.cmd('ovs-vsctl get-controller %s' % switch_name).strip()
        info('  %s -> %s\n' % (switch_name, controller_info))
    
    # 启动CLI
    info('*** 启动Mininet CLI\n')
    info('测试命令:\n')
    info('  pingall          # 测试连通性\n')
    info('  h1 ping h6       # 跨域ping测试\n')
    info('  dump             # 显示详细信息\n')
    
    CLI(net)
    
    # 清理
    info('*** 停止网络\n')
    
    if PHYSICAL_INTERFACE and original_config:
        info('*** 恢复物理网卡配置\n')
        try:
            restore_network_config(PHYSICAL_INTERFACE, original_config)
        except Exception as e:
            error(f"警告: 恢复网络配置时出错: {e}\n")
    
    net.stop()


if __name__ == '__main__':
    main()