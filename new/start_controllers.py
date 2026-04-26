#!/usr/bin/env python
"""
启动多个Ryu控制器的脚本
用于启动6个域的控制器，分别监听端口6654-6659
每个控制器在独立的终端窗口中运行
"""

import subprocess
import sys
import time
import signal
import os
import shutil
import tempfile
from pathlib import Path


class ControllerManager:
    """控制器管理器"""
    
    def __init__(self, base_port=6654, num_controllers=6, controller_app='controller.py', use_terminal=True):
        """
        初始化控制器管理器
        
        Args:
            base_port: 起始端口号（默认6654）
            num_controllers: 控制器数量（默认6个）
            controller_app: Ryu应用路径
            use_terminal: 是否在新终端窗口中运行（默认True）
        """
        self.base_port = base_port
        self.num_controllers = num_controllers
        self.controller_app = controller_app
        self.processes = {}  # {port: process}
        self.pids = {}  # {port: pid} 用于存储真实的PID
        # 跨平台兼容的临时目录
        import tempfile
        tmp_dir = Path(tempfile.gettempdir())
        self.pid_file = tmp_dir / 'ryu_controllers.pid'
        self.log_dir = Path(__file__).resolve().parent / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.use_terminal = use_terminal
        self.terminal_cmd = self._detect_terminal()
    
    def _detect_terminal(self):
        """检测可用的终端模拟器"""
        terminals = [
            ('gnome-terminal', ['--', 'bash', '-c']),
            ('xterm', ['-e']),
            ('konsole', ['-e']),
            ('terminator', ['-e']),
            ('xfce4-terminal', ['-e']),
            ('mate-terminal', ['-e']),
            ('lxterminal', ['-e']),
        ]
        
        for term, options in terminals:
            if shutil.which(term):
                return (term, options)
        
        return None
        
    def start_controller(self, port):
        """
        启动单个控制器
        
        Args:
            port: 控制器监听端口
            
        Returns:
            subprocess.Popen对象
        """
        # 获取脚本所在目录的绝对路径
        script_dir = os.path.dirname(os.path.abspath(__file__))
        controller_path = os.path.join(script_dir, self.controller_app)
        
        # 检查文件是否存在
        if not os.path.exists(controller_path):
            print(f'错误: 找不到控制器文件: {controller_path}')
            return None
        
        cmd = [
            'ryu-manager',
            '--observe-links',
            '--ofp-tcp-listen-port', str(port),
            controller_path
        ]
        
        print(f'启动控制器 (端口 {port})...')
        print(f'命令: {" ".join(cmd)}')
        
        try:
            if self.use_terminal and self.terminal_cmd:
                # 在新终端窗口中启动
                term_name, term_option = self.terminal_cmd
                title = f'Ryu Controller - Port {port}'
                
                # 构建终端命令
                if term_name == 'gnome-terminal':
                    # gnome-terminal 使用 -- 分隔选项和命令
                    terminal_cmd = [
                        term_name,
                        '--title', title,
                        '--', 'bash', '-c',
                        ' '.join(cmd) + '; echo ""; echo "按 Enter 键关闭此窗口..."; read'
                    ]
                elif term_name == 'xterm':
                    terminal_cmd = [
                        term_name,
                        '-T', title,
                        '-e', 'bash', '-c',
                        ' '.join(cmd) + '; echo ""; echo "按 Enter 键关闭此窗口..."; read'
                    ]
                elif term_name == 'konsole':
                    terminal_cmd = [
                        term_name,
                        '--title', title,
                        '-e', 'bash', '-c',
                        ' '.join(cmd) + '; echo ""; echo "按 Enter 键关闭此窗口..."; read'
                    ]
                elif term_name == 'terminator':
                    terminal_cmd = [
                        term_name,
                        '--title', title,
                        '-e', 'bash', '-c',
                        ' '.join(cmd) + '; echo ""; echo "按 Enter 键关闭此窗口..."; read'
                    ]
                elif term_name == 'xfce4-terminal':
                    terminal_cmd = [
                        term_name,
                        '--title', title,
                        '-e', 'bash', '-c',
                        ' '.join(cmd) + '; echo ""; echo "按 Enter 键关闭此窗口..."; read'
                    ]
                elif term_name == 'mate-terminal':
                    terminal_cmd = [
                        term_name,
                        '--title', title,
                        '-e', 'bash', '-c',
                        ' '.join(cmd) + '; echo ""; echo "按 Enter 键关闭此窗口..."; read'
                    ]
                elif term_name == 'lxterminal':
                    terminal_cmd = [
                        term_name,
                        '--title', title,
                        '-e', 'bash', '-c',
                        ' '.join(cmd) + '; echo ""; echo "按 Enter 键关闭此窗口..."; read'
                    ]
                else:
                    terminal_cmd = [
                        term_name,
                        *term_option,
                        ' '.join(cmd) + '; echo ""; echo "按 Enter 键关闭此窗口..."; read'
                    ]
                
                print(f'  在新终端窗口中启动: {term_name}')
                # 跨平台兼容的进程组创建
                if sys.platform == 'win32':
                    process = subprocess.Popen(
                        terminal_cmd,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                    )
                else:
                    process = subprocess.Popen(
                        terminal_cmd,
                        preexec_fn=os.setsid if hasattr(os, 'setsid') else None
                    )
                
                # 等待一下，检查进程是否成功启动
                time.sleep(1)
                if process.poll() is not None:
                    print(f'错误: 控制器 (端口 {port}) 启动失败')
                    return None
                
                # 注意：在终端窗口中启动时，process.pid 是终端进程的PID
                # 实际的 ryu-manager 进程是终端的子进程
                # 这里保存终端进程的PID，停止时会终止整个进程组
                self.pids[port] = process.pid
                print(f'✓ 控制器 (端口 {port}) 已在新终端窗口中启动，PID: {process.pid}')
                return process
            else:
                # 在后台启动，重定向输出到日志文件
                if not self.use_terminal:
                    print(f'  在后台启动（日志输出到文件）')
                else:
                    print(f'  警告: 未找到终端模拟器，将在后台启动')
                
                log_file_path = self.log_dir / f'ryu_controller_{port}.log'
                log_file = open(log_file_path, 'w')
                try:
                    # 跨平台兼容的进程组创建
                    if sys.platform == 'win32':
                        # Windows 使用 CREATE_NEW_PROCESS_GROUP
                        process = subprocess.Popen(
                            cmd,
                            stdout=log_file,
                            stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                        )
                    else:
                        # Unix/Linux 使用 setsid
                        preexec_fn = os.setsid if hasattr(os, 'setsid') else None
                        process = subprocess.Popen(
                            cmd,
                            stdout=log_file,
                            stderr=subprocess.STDOUT,
                            preexec_fn=preexec_fn
                        )
                    
                    # 等待一下，检查进程是否成功启动
                    time.sleep(1)
                    if process.poll() is not None:
                        # 进程已经退出，说明启动失败
                        log_file.close()
                        print(f'错误: 控制器 (端口 {port}) 启动失败')
                        return None
                    
                    # 保存真实的PID
                    self.pids[port] = process.pid
                    print(f'✓ 控制器 (端口 {port}) 已启动，PID: {process.pid}')
                    print(f'  日志文件: {log_file_path}')
                    return process
                except Exception as e:
                    log_file.close()
                    raise e
            
        except Exception as e:
            print(f'错误: 启动控制器 (端口 {port}) 时发生异常: {e}')
            return None
    
    def start_all(self):
        """启动所有控制器"""
        print('=' * 60)
        print('启动Ryu控制器')
        print('=' * 60)
        print(f'控制器数量: {self.num_controllers}')
        print(f'端口范围: {self.base_port} - {self.base_port + self.num_controllers - 1}')
        print(f'应用: {self.controller_app}')
        if self.use_terminal:
            if self.terminal_cmd:
                print(f'终端: {self.terminal_cmd[0]} (每个控制器在独立终端中运行)')
            else:
                print('终端: 未找到终端模拟器，将在后台运行')
        else:
            print('模式: 后台运行（日志输出到文件）')
        print('=' * 60)
        print()
        
        failed_ports = []
        
        for i in range(self.num_controllers):
            port = self.base_port + i
            process = self.start_controller(port)
            
            if process:
                self.processes[port] = process
            else:
                failed_ports.append(port)
            
            # 稍微延迟，避免同时启动造成资源竞争
            time.sleep(0.8)  # 增加延迟，确保终端窗口有时间打开
        
        # 保存PID到文件
        self.save_pids()
        
        print()
        print('=' * 60)
        if failed_ports:
            print(f'警告: {len(failed_ports)} 个控制器启动失败: {failed_ports}')
        else:
            print(f'✓ 所有 {self.num_controllers} 个控制器已成功启动')
            if self.use_terminal and self.terminal_cmd:
                print(f'每个控制器都在独立的终端窗口中运行')
        print('=' * 60)
        print()
        print('控制器状态:')
        self.show_status()
        print()
        if self.use_terminal and self.terminal_cmd:
            print('提示:')
            print('  - 每个控制器在独立的终端窗口中运行')
            print('  - 日志直接显示在对应的终端窗口中')
            print('  - 关闭终端窗口将停止对应的控制器')
            print('  - 停止所有控制器: python3 start_controllers.py stop')
            print('  - 查看状态: python3 start_controllers.py status')
        else:
            log_dir = self.log_dir
            print('提示:')
            print(f'  - 查看日志: tail -f {log_dir}/ryu_controller_<PORT>.log')
            print('  - 关闭后重新启动: python3.6 start_controllers.py start -n')
            print('  - 查看状态: python3 start_controllers.py status')
        print()
    
    def stop_controller(self, port):
        """停止单个控制器"""
        # 优先使用存储的PID
        pid = self.pids.get(port)
        if not pid and port in self.processes:
            pid = self.processes[port].pid
        
        if not pid:
            print(f'控制器 (端口 {port}) 未运行')
            return False
        
        try:
            if sys.platform == 'win32':
                # Windows 使用 taskkill 或直接终止进程
                try:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(2)
                    # 检查进程是否还在运行
                    try:
                        os.kill(pid, 0)
                        # 如果还在运行，强制终止
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # 进程已停止
                except ProcessLookupError:
                    print(f'控制器 (端口 {port}) 已经停止')
                except Exception as e:
                    # 尝试使用 taskkill
                    try:
                        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], 
                                     capture_output=True, check=False)
                    except:
                        pass
            else:
                # Unix/Linux: 终止整个进程组
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                    time.sleep(2)
                    # 检查进程是否还在运行
                    try:
                        os.kill(pid, 0)
                        # 如果还在运行，强制终止
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # 进程已停止
                except ProcessLookupError:
                    print(f'控制器 (端口 {port}) 已经停止')
                    return True
                except OSError as e:
                    if e.errno == 3:  # No such process
                        print(f'控制器 (端口 {port}) 已经停止')
                        return True
                    raise
            
            print(f'✓ 控制器 (端口 {port}) 已停止')
            if port in self.processes:
                del self.processes[port]
            if port in self.pids:
                del self.pids[port]
            return True
            
        except Exception as e:
            print(f'错误: 停止控制器 (端口 {port}) 时发生异常: {e}')
            return False
    
    def stop_all(self):
        """停止所有控制器"""
        print('=' * 60)
        print('停止Ryu控制器')
        print('=' * 60)
        
        # 尝试从PID文件加载进程信息
        self.load_pids()
        
        if not self.processes:
            print('没有运行中的控制器')
            return
        
        print(f'正在停止 {len(self.processes)} 个控制器...')
        print()
        
        for port in list(self.processes.keys()):
            self.stop_controller(port)
            time.sleep(0.2)
        
        # 清理PID文件
        if self.pid_file.exists():
            self.pid_file.unlink()
        
        print()
        print('=' * 60)
        print('✓ 所有控制器已停止')
        print('=' * 60)
    
    def show_status(self):
        """显示所有控制器的状态"""
        # 尝试从PID文件加载进程信息
        self.load_pids()
        
        if not self.processes:
            print('没有运行中的控制器')
            return
        
        print()
        print('端口\tPID\t状态\t\t日志文件')
        print('-' * 60)
        
        for port in sorted(self.processes.keys()):
            process = self.processes[port]
            try:
                # 检查进程是否还在运行
                process.poll()
                if process.returncode is None:
                    status = '运行中'
                else:
                    status = f'已退出 (code: {process.returncode})'
            except:
                status = '未知'
            
            # 获取PID（优先从pids字典）
            pid = self.pids.get(port)
            if not pid and port in self.processes:
                pid = self.processes[port].pid
            
            log_file = self.log_dir / f'ryu_controller_{port}.log'
            print(f'{port}\t{pid or "N/A"}\t{status}\t{log_file}')
    
    def save_pids(self):
        """保存PID到文件"""
        try:
            with open(self.pid_file, 'w') as f:
                # 优先使用 pids 字典，如果没有则使用 processes
                for port in set(list(self.pids.keys()) + list(self.processes.keys())):
                    pid = self.pids.get(port)
                    if not pid and port in self.processes:
                        pid = self.processes[port].pid
                    if pid:
                        f.write(f'{port}:{pid}\n')
        except Exception as e:
            print(f'警告: 保存PID文件失败: {e}')
    
    def load_pids(self):
        """从PID文件加载进程信息"""
        if not self.pid_file.exists():
            return
        
        try:
            with open(self.pid_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if ':' in line:
                        port_str, pid_str = line.split(':', 1)
                        port = int(port_str)
                        pid = int(pid_str)
                        
                        # 检查进程是否还在运行
                        try:
                            os.kill(pid, 0)  # 发送信号0检查进程是否存在
                            # 只保存PID，不创建假的Popen对象
                            self.pids[port] = pid
                        except ProcessLookupError:
                            # 进程不存在，跳过
                            pass
                        except PermissionError:
                            # 没有权限，但进程可能存在，仍然保存PID
                            self.pids[port] = pid
                        except OSError:
                            # 其他错误，跳过
                            pass
        except Exception as e:
            print(f'警告: 加载PID文件失败: {e}')


def main():
    """主函数"""
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
    else:
        command = 'start'
    
    # 检查是否禁用终端模式
    use_terminal = True
    if '--no-terminal' in sys.argv or '-n' in sys.argv:
        use_terminal = False
        if '--no-terminal' in sys.argv:
            sys.argv.remove('--no-terminal')
        if '-n' in sys.argv:
            sys.argv.remove('-n')
    
    manager = ControllerManager(
        base_port=6654,
        num_controllers=6,
        controller_app='controller.py',
        use_terminal=use_terminal
    )
    
    if command == 'start':
        manager.start_all()
        # 保持脚本运行，直到用户中断
        try:
            print('控制器正在运行中... (按 Ctrl+C 停止所有控制器)')
            while True:
                time.sleep(1)
                # 检查是否有进程退出
                for port in list(manager.processes.keys()):
                    process = manager.processes[port]
                    if process.poll() is not None:
                        print(f'警告: 控制器 (端口 {port}) 意外退出')
                        del manager.processes[port]
        except KeyboardInterrupt:
            print('\n\n收到中断信号，正在停止所有控制器...')
            manager.stop_all()
    
    elif command == 'stop':
        manager.stop_all()
    
    elif command == 'status':
        print('=' * 60)
        print('Ryu控制器状态')
        print('=' * 60)
        manager.show_status()
        print()
    
    elif command == 'restart':
        print('重启所有控制器...')
        manager.stop_all()
        time.sleep(2)
        manager.start_all()
    
    else:
        print(f'未知命令: {command}')
        print()
        print('用法:')
        print('  python3 start_controllers.py [start|stop|status|restart] [--no-terminal|-n]')
        print()
        print('命令说明:')
        print('  start   - 启动所有控制器（默认）')
        print('  stop    - 停止所有控制器')
        print('  status  - 查看控制器状态')
        print('  restart - 重启所有控制器')
        print()
        print('选项:')
        print('  --no-terminal, -n  - 在后台运行，不打开终端窗口（日志输出到文件）')
        print()
        print('示例:')
        print('  python3 start_controllers.py start        # 在终端窗口中启动')
        print('  python3 start_controllers.py start -n     # 在后台启动')
        sys.exit(1)


if __name__ == '__main__':
    main()

