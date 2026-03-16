#!/bin/bash
# 启动所有控制器的脚本

echo "=================================="
echo "启动SDN多域控制器系统"
echo "=================================="

# 检查是否已有控制器在运行
if pgrep -f "ryu-manager.*controller.py" > /dev/null; then
    echo "警告：检测到已有控制器在运行"
    echo "是否要停止旧的控制器？(y/n)"
    read -r answer
    if [ "$answer" = "y" ]; then
        echo "停止旧的控制器..."
        pkill -f "ryu-manager.*controller.py"
        pkill -f "server_agent.py"
        sleep 2
    fi
fi

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo ""
echo "步骤1: 启动根控制器 (server_agent)..."
python3 server_agent.py > logs/server_agent.log 2>&1 &
SERVER_PID=$!
echo "  根控制器已启动 (PID: $SERVER_PID)"
echo "  日志: logs/server_agent.log"
echo "  Web界面: http://localhost:5000"
sleep 3

echo ""
echo "步骤2: 启动6个从控制器..."

# 创建日志目录
mkdir -p logs

# Domain 1 控制器 (端口 6654)
echo "  启动 Domain 1 控制器 (端口 6654)..."
ryu-manager --ofp-tcp-listen-port 6654 controller.py > logs/controller_6654.log 2>&1 &
echo "    PID: $! 日志: logs/controller_6654.log"
sleep 2

# Domain 2 控制器 (端口 6655)
echo "  启动 Domain 2 控制器 (端口 6655)..."
ryu-manager --ofp-tcp-listen-port 6655 controller.py > logs/controller_6655.log 2>&1 &
echo "    PID: $! 日志: logs/controller_6655.log"
sleep 2

# Domain 3 控制器 (端口 6656)
echo "  启动 Domain 3 控制器 (端口 6656)..."
ryu-manager --ofp-tcp-listen-port 6656 controller.py > logs/controller_6656.log 2>&1 &
echo "    PID: $! 日志: logs/controller_6656.log"
sleep 2

# Domain 4 控制器 (端口 6657)
echo "  启动 Domain 4 控制器 (端口 6657)..."
ryu-manager --ofp-tcp-listen-port 6657 controller.py > logs/controller_6657.log 2>&1 &
echo "    PID: $! 日志: logs/controller_6657.log"
sleep 2

# Domain 5 控制器 (端口 6658)
echo "  启动 Domain 5 控制器 (端口 6658)..."
ryu-manager --ofp-tcp-listen-port 6658 controller.py > logs/controller_6658.log 2>&1 &
echo "    PID: $! 日志: logs/controller_6658.log"
sleep 2

# Domain 6 控制器 (端口 6659)
echo "  启动 Domain 6 控制器 (端口 6659)..."
ryu-manager --ofp-tcp-listen-port 6659 controller.py > logs/controller_6659.log 2>&1 &
echo "    PID: $! 日志: logs/controller_6659.log"
sleep 2

echo ""
echo "=================================="
echo "所有控制器启动完成！"
echo "=================================="
echo ""
echo "运行状态："
echo "  - 根控制器: http://localhost:5000"
echo "  - 从控制器端口: 6654-6659"
echo ""
echo "下一步："
echo "  1. 打开浏览器访问 http://localhost:5000 查看拓扑"
echo "  2. 在新终端运行: sudo python3 create_complex_topo.py"
echo ""
echo "查看日志："
echo "  tail -f logs/server_agent.log    # 查看根控制器日志"
echo "  tail -f logs/controller_6654.log # 查看Domain 1控制器日志"
echo ""
echo "停止所有控制器："
echo "  ./stop_controllers.sh"
echo "  或者: pkill -f 'ryu-manager.*controller.py' && pkill -f 'server_agent.py'"
echo ""

