#!/bin/bash
# Web界面诊断脚本

echo "======================================"
echo "    Web界面诊断工具"
echo "======================================"
echo ""

# 1. 检查根控制器进程
echo "1. 检查根控制器进程..."
ROOT_PID=$(pgrep -f "server_agent.py")
if [ -z "$ROOT_PID" ]; then
    echo "  ❌ 根控制器未运行！"
    echo "  解决方案: python3 server_agent.py &"
    exit 1
else
    echo "  ✓ 根控制器运行中 (PID: $ROOT_PID)"
fi

# 2. 检查Web端口
echo ""
echo "2. 检查Web端口5000..."
if sudo lsof -i :5000 > /dev/null 2>&1; then
    echo "  ✓ 端口5000已监听"
else
    echo "  ❌ 端口5000未监听！"
    echo "  等待3秒后重试..."
    sleep 3
    if sudo lsof -i :5000 > /dev/null 2>&1; then
        echo "  ✓ 端口5000现在已监听"
    else
        echo "  ❌ 端口5000仍未监听，请检查根控制器日志"
        exit 1
    fi
fi

# 3. 测试健康检查API
echo ""
echo "3. 测试健康检查API..."
# 使用Python测试（不依赖curl）
HEALTH_RESPONSE=$(python3 -c "
import urllib.request
import json
try:
    with urllib.request.urlopen('http://localhost:5000/api/health', timeout=5) as response:
        data = json.loads(response.read().decode('utf-8'))
        print(json.dumps(data))
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
" 2>&1)
HEALTH_STATUS=$?
if [ $HEALTH_STATUS -eq 0 ] && [[ ! "$HEALTH_RESPONSE" =~ "ERROR:" ]]; then
    echo "  ✓ API响应正常"
    echo "  响应: $HEALTH_RESPONSE"
else
    echo "  ❌ API无响应！"
    echo "  错误: $HEALTH_RESPONSE"
    exit 1
fi

# 4. 测试图数据API
echo ""
echo "4. 测试图数据API..."
# 使用Python测试
GRAPH_TEST=$(python3 -c "
import urllib.request
import json
try:
    with urllib.request.urlopen('http://localhost:5000/api/graph', timeout=5) as response:
        data = json.loads(response.read().decode('utf-8'))
        node_count = len(data.get('nodes', []))
        edge_count = len(data.get('edges', []))
        print(f'OK:{node_count}:{edge_count}')
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
" 2>&1)
GRAPH_STATUS=$?

if [ $GRAPH_STATUS -eq 0 ] && [[ "$GRAPH_TEST" =~ ^OK: ]]; then
    echo "  ✓ 图数据API响应正常"
    NODE_COUNT=$(echo "$GRAPH_TEST" | cut -d: -f2)
    EDGE_COUNT=$(echo "$GRAPH_TEST" | cut -d: -f3)
    echo "  节点数: $NODE_COUNT"
    echo "  边数: $EDGE_COUNT"
    
    if [ "$NODE_COUNT" -eq 0 ] 2>/dev/null; then
        echo "  ⚠️  警告: 图中没有节点！可能还没有控制器连接。"
    fi
    GRAPH_STATUS=0
else
    echo "  ❌ 图数据API无响应！"
    echo "  错误: $GRAPH_TEST"
    GRAPH_STATUS=1
fi

# 5. 检查从控制器连接
echo ""
echo "5. 检查从控制器..."
CONTROLLER_COUNT=$(pgrep -f "ryu-manager.*controller.py" | wc -l)
echo "  从控制器进程数: $CONTROLLER_COUNT"

if [ "$CONTROLLER_COUNT" -eq 0 ]; then
    echo "  ⚠️  警告: 没有从控制器运行"
    echo "  解决方案: ./start_controllers.sh"
else
    echo "  ✓ 从控制器运行中"
    echo "  进程列表:"
    pgrep -f "ryu-manager.*controller.py" -a | while read line; do
        echo "    - $line"
    done
fi

# 6. 检查TCP连接
echo ""
echo "6. 检查TCP端口5001（从控制器连接端口）..."
if sudo lsof -i :5001 > /dev/null 2>&1; then
    echo "  ✓ 端口5001已监听"
    CONN_COUNT=$(sudo lsof -i :5001 | grep ESTABLISHED | wc -l)
    echo "  已建立连接数: $CONN_COUNT"
else
    echo "  ❌ 端口5001未监听！"
fi

# 7. 总结
echo ""
echo "======================================"
echo "    诊断总结"
echo "======================================"

# 安全的数值比较（确保变量存在且为数字）
if [ "${HEALTH_STATUS:-1}" -eq 0 ] && [ "${GRAPH_STATUS:-1}" -eq 0 ]; then
    echo "✓ API正常"
else
    echo "❌ API异常"
fi

if [ "${CONTROLLER_COUNT:-0}" -gt 0 ]; then
    echo "✓ 从控制器运行中 ($CONTROLLER_COUNT 个)"
else
    echo "⚠️  没有从控制器运行"
fi

if [ -n "$NODE_COUNT" ] && [ "${NODE_COUNT:-0}" -gt 0 ] 2>/dev/null; then
    echo "✓ 拓扑图有数据 ($NODE_COUNT 个节点)"
else
    echo "⚠️  拓扑图为空（等待控制器连接）"
fi

echo ""
echo "下一步操作:"
if [ "$CONTROLLER_COUNT" -eq 0 ]; then
    echo "  1. 启动从控制器: ./start_controllers.sh"
fi
echo "  2. 打开浏览器: http://localhost:5000"
echo "  3. 按F12打开开发者工具查看详细信息"
echo "  4. 点击页面上的 '🔍 测试API' 按钮"
echo ""

