#!/bin/bash
# 停止所有控制器的脚本

echo "=================================="
echo "停止SDN多域控制器系统"
echo "=================================="

echo ""
echo "步骤1: 停止Mininet网络..."
sudo mn -c
echo "  Mininet网络已清理"

echo ""
echo "步骤2: 停止所有从控制器..."
pkill -f "ryu-manager.*controller.py"
if [ $? -eq 0 ]; then
    echo "  所有从控制器已停止"
else
    echo "  未找到运行中的从控制器"
fi

echo ""
echo "步骤3: 停止根控制器..."
pkill -f "server_agent.py"
if [ $? -eq 0 ]; then
    echo "  根控制器已停止"
else
    echo "  未找到运行中的根控制器"
fi

sleep 2

echo ""
echo "步骤4: 验证进程状态..."
RUNNING_CONTROLLERS=$(pgrep -f "ryu-manager|server_agent" | wc -l)
if [ "$RUNNING_CONTROLLERS" -eq 0 ]; then
    echo "  ✓ 所有控制器已成功停止"
else
    echo "  ⚠ 仍有 $RUNNING_CONTROLLERS 个控制器进程在运行"
    echo ""
    echo "运行中的进程："
    ps aux | grep -E "ryu-manager|server_agent" | grep -v grep
    echo ""
    echo "如需强制停止，请运行："
    echo "  sudo pkill -9 -f 'ryu-manager|server_agent'"
fi

echo ""
echo "=================================="
echo "清理完成"
echo "=================================="

