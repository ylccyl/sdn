#!/usr/bin/env python3
"""
简单的API测试脚本（不依赖curl）
用于测试根控制器的Web API是否正常工作
"""

import urllib.request
import json
import sys

def test_api(url, name):
    """测试单个API端点"""
    try:
        print(f"测试 {name}...")
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            print(f"  ✓ 成功")
            print(f"  响应: {json.dumps(data, indent=2, ensure_ascii=False)}")
            return True, data
    except urllib.error.URLError as e:
        print(f"  ✗ 连接失败: {e.reason}")
        return False, None
    except Exception as e:
        print(f"  ✗ 错误: {e}")
        return False, None

def main():
    print("=" * 50)
    print("Web API 测试工具")
    print("=" * 50)
    print()
    
    base_url = "http://localhost:5000"
    
    # 测试健康检查
    success, data = test_api(f"{base_url}/api/health", "健康检查")
    if not success:
        print("\n❌ 健康检查失败，请检查：")
        print("  1. 根控制器是否运行: ps aux | grep server_agent")
        print("  2. Web端口是否监听: sudo lsof -i :5000")
        print("  3. 防火墙设置")
        sys.exit(1)
    
    print()
    
    # 测试图数据
    success, data = test_api(f"{base_url}/api/graph", "图数据API")
    if success and data:
        nodes = data.get('nodes', [])
        edges = data.get('edges', [])
        print(f"  节点数量: {len(nodes)}")
        print(f"  边数量: {len(edges)}")
        
        if len(nodes) == 0:
            print("  ⚠️  提示: 图中没有节点，可能还没有控制器连接")
        
        # 显示节点类型分布
        if nodes:
            print("\n  节点类型分布:")
            node_types = {}
            for node in nodes:
                node_data = node.get('data', {})
                node_type = node_data.get('node_type', 'unknown')
                node_types[node_type] = node_types.get(node_type, 0) + 1
            for ntype, count in node_types.items():
                print(f"    {ntype}: {count}")
    
    print()
    
    # 测试统计信息
    success, data = test_api(f"{base_url}/api/statistics", "统计信息API")
    if success and data:
        print(f"  控制器: {data.get('controllers', 0)}")
        print(f"  交换机: {data.get('switches', 0)}")
        print(f"  主机: {data.get('hosts', 0)}")
        print(f"  链路: {data.get('links', 0)}")
    
    print()
    print("=" * 50)
    print("✓ 所有API测试完成！")
    print("=" * 50)
    print()
    print("下一步：")
    print("  1. 浏览器访问: http://localhost:5000")
    print("  2. 启动从控制器: ./start_controllers.sh")
    print("  3. 创建拓扑: sudo python3 create_complex_topo.py")

if __name__ == '__main__':
    main()

