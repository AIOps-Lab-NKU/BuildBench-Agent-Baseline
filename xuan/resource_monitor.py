"""resource_monitor.py — 独立的资源监控模块

周期性采样系统 / Ollama 进程 / Runner 进程的 CPU 和内存，写入 CSV。

用法：
    from resource_monitor import ResourceMonitor

    monitor = ResourceMonitor("path/to/output.csv", interval=5.0)
    monitor.start()
    # ... 运行你的任务 ...
    monitor.stop()
"""

import os
import time
import threading
from pathlib import Path
from typing import Dict, Optional

try:
    import psutil
except ImportError:
    psutil = None


class ResourceMonitor:
    """周期性采样 CPU / 内存 / Ollama 进程资源，写入 CSV。"""

    def __init__(self, csv_path: str, interval: float = 5.0):
        if psutil is None:
            raise ImportError("psutil is required for resource monitoring. Install with: pip install psutil")
        self.csv_path = csv_path
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 缓存 Process 对象，使 cpu_percent() 跨调用返回实际值（首次调用返回 0.0 作为基线）
        self._proc_cache: Dict[int, "psutil.Process"] = {}

    def _find_ollama_root(self):
        """找到 ollama serve 进程（遍历所有进程匹配一次）"""
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if name == "ollama" or "ollama serve" in cmdline:
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return None

    def _get_ollama_processes(self):
        """获取所有 Ollama 进程（通过进程树追踪，比名称匹配更可靠）

        策略：找到 ollama serve → 递归收集所有子进程。
        这比逐进程名称匹配更可靠，能稳定捕捉 llama-server 等 worker。
        """
        root = self._find_ollama_root()
        if root is None:
            return []
        try:
            descendants = root.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []
        # 刷新 memory_info 属性
        results = []
        for p in descendants:
            try:
                p.as_dict(attrs=["pid", "memory_info"])
                results.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return results

    def _get_cpu_percent(self, pid: int) -> float:
        """获取进程 CPU 使用率，利用缓存使重复调用返回实际值。

        psutil.cpu_percent() 首次调用总是返回 0.0（建立基线），
        缓存 Process 对象后第二次及后续调用返回实际 CPU 使用率。
        """
        try:
            if pid not in self._proc_cache:
                p = psutil.Process(pid)
                p.cpu_percent(interval=0)  # 首次调用，建立基线
                self._proc_cache[pid] = p
                return 0.0
            return self._proc_cache[pid].cpu_percent(interval=0) or 0.0
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            self._proc_cache.pop(pid, None)
            return 0.0

    def _sample(self) -> Dict[str, float]:
        """采集一次资源快照"""
        snap = {
            "timestamp": time.time(),
            "cpu_percent_total": psutil.cpu_percent(interval=0.5),
            "memory_total_gb": psutil.virtual_memory().total / (1024**3),
            "memory_used_gb": psutil.virtual_memory().used / (1024**3),
            "memory_percent": psutil.virtual_memory().percent,
        }

        # Ollama 进程资源（通过进程树追踪，捕获 ollama serve + 所有子进程）
        ollama_total_cpu = 0.0
        ollama_total_mem = 0.0
        for proc in self._get_ollama_processes():
            try:
                pid = proc.pid
                mem = (proc.memory_info().rss or 0) / (1024**3)
                ollama_total_cpu += self._get_cpu_percent(pid)
                ollama_total_mem += mem
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        snap["ollama_cpu_percent"] = ollama_total_cpu
        snap["ollama_memory_gb"] = ollama_total_mem

        # baseline.py / parallel_runner 进程资源
        runner_cpu = 0.0
        runner_mem = 0.0
        my_pid = os.getpid()
        for proc in psutil.process_iter(["pid", "name", "memory_info", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                name = (proc.info.get("name") or "").lower()
                if my_pid == proc.info["pid"]:
                    continue
                is_python = "python" in name or (len(cmdline) > 0 and "python" in cmdline[0])
                is_our_script = any("baseline" in c or "parallel" in c for c in cmdline)
                if is_python and is_our_script:
                    pid = proc.info["pid"]
                    runner_cpu += self._get_cpu_percent(pid)
                    runner_mem += (proc.info["memory_info"].rss or 0) / (1024**3)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        snap["runner_cpu_percent"] = runner_cpu
        snap["runner_memory_gb"] = runner_mem

        return snap

    def _loop(self):
        """采样循环（在 daemon 线程中运行）"""
        header_written = os.path.exists(self.csv_path)
        while not self._stop_event.is_set():
            snap = self._sample()
            if snap:
                if not header_written:
                    Path(self.csv_path).parent.mkdir(parents=True, exist_ok=True)
                    with open(self.csv_path, "w") as f:
                        f.write(",".join(snap.keys()) + "\n")
                    header_written = True
                with open(self.csv_path, "a") as f:
                    f.write(",".join(str(v) for v in snap.values()) + "\n")
            self._stop_event.wait(self.interval)

    def start(self):
        """启动资源监控（daemon 线程，主程序退出时自动结束）"""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止资源监控"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
