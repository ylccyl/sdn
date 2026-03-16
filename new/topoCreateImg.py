import networkx as nx
import matplotlib
matplotlib.use('Agg')  # 使用非交互式后端
import matplotlib.pyplot as plt
import os

"""
输入文件示例：
s1-s2
s2-s3
s3-s4
"""

def read_edges(file_path):
    """
    从指定文件读取边列表。
    返回边列表，格式为 [('s1', 's2'), ('s2', 's3'), ...]
    """
    edges = []
    if not os.path.exists(file_path):
        print(f"Error: File {file_path} does not exist.")
        return edges
    try:
        with open(file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and '-' in line:
                    node1, node2 = line.split('-')
                    edges.append((node1.strip(), node2.strip()))
        if not edges:
            print(f"Error: No valid edges found in {file_path}.")
        else:
            print(f"Loaded {len(edges)} edges from {file_path}")
        return edges
    except Exception as e:
        print(f"Error reading file {file_path}: {e}")
        return []

def draw_network_topology(edges, output_path='network_topology.png'):
    """
    绘制网络拓扑图并保存到指定路径。
    edges: 边列表，格式为 [('s1', 's2'), ...]
    output_path: 输出图片的路径
    """
    try:
        if not edges:
            print("Error: No edges to draw.")
            return

        # 创建无向图
        G = nx.Graph()
        G.add_edges_from(edges)

        # 设置图形大小
        plt.figure(figsize=(10, 8))

        # 使用 spring 布局，固定种子以确保可重复性
        pos = nx.spring_layout(G, seed=42)

        # 绘制节点
        nx.draw_networkx_nodes(G, pos, node_size=500, node_color='lightblue')

        # 绘制边
        nx.draw_networkx_edges(G, pos, width=1)

        # 绘制节点标签
        nx.draw_networkx_labels(G, pos, font_size=10, font_weight='bold')

        # 设置标题
        plt.title('Network Topology')

        # 保存图形
        print(f"Saving to: {output_path}")
        plt.savefig(output_path, format='png', dpi=300, bbox_inches='tight')
        print(f"File saved successfully: {output_path}")

        # 关闭图形以释放内存
        plt.close()

    except Exception as e:
        print(f"Error drawing topology: {e}")

def main():
    # 配置输入文件路径（必须提供）
    input_file = "F:/study/bysj/code/distribute controller experiment/network topology/10node.txt"  # 修改为你的文件路径
    # 配置输出路径（确保有写入权限）
    output_path = 'F:/study/bysj/code/network_topology.png'

    # 读取边列表
    edges = read_edges(input_file)

    # 绘制并保存拓扑图
    if edges:
        draw_network_topology(edges, output_path)
    else:
        print("Cannot generate topology due to invalid or empty edge list.")

if __name__ == '__main__':
    main()