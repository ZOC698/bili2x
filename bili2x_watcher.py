import os
import sys
import time
import subprocess
import threading
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 解决 Windows 命令行默认 GBK 导致 emoji 崩溃问题
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 确保引入当前目录
CURRENT_DIR = Path(__file__).parent
CONFIG_PATH = CURRENT_DIR / "config.json"

def disable_quick_edit():
    """禁用 Windows 控制台快速编辑模式，防止鼠标点击终端窗口挂起暂停程序执行"""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h_stdin = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE = -10
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
                # ENABLE_QUICK_EDIT_MODE = 0x0040, ENABLE_EXTENDED_FLAGS = 0x0080
                new_mode = (mode.value & ~0x0040) | 0x0080
                kernel32.SetConsoleMode(h_stdin, new_mode)
        except Exception:
            pass

IGNORE_EXTS = {".crdownload", ".tmp", ".part", ".aria2", ".downloading", ".temp"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".flv"}

from bili2x_config import load_config

import queue

class VideoDownloadHandler(FileSystemEventHandler):
    def __init__(self, config):
        self.config = config
        self.processing = set()
        self.lock = threading.Lock()
        self.task_queue = queue.Queue()
        
        # 启动单消费者线程：严格保证同一时间只有 1 个任务占用 GPU 显存
        self.consumer_thread = threading.Thread(target=self._queue_consumer, daemon=True)
        self.consumer_thread.start()

    def on_created(self, event):
        if not event.is_directory:
            self._handle_possible_video(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle_possible_video(event.dest_path)

    def _handle_possible_video(self, file_path_str):
        path = Path(file_path_str)
        ext = path.suffix.lower()
        
        if ext in IGNORE_EXTS or ext not in VIDEO_EXTS:
            return

        with self.lock:
            if str(path) in self.processing:
                return
            self.processing.add(str(path))

        # 启动轻量异步线程监听此文件的下载就绪状态（不占显卡）
        threading.Thread(target=self._wait_file_ready, args=(path,), daemon=True).start()

    def _wait_file_ready(self, path):
        print(f"\n[收件箱检测到文件] {path.name}，正在确认文件下载就绪...")
        
        # 连续 2 秒检测文件大小不变且可顺利读取
        last_size = -1
        stable_count = 0
        for _ in range(60):
            time.sleep(1.0)
            if not path.exists():
                with self.lock:
                    self.processing.discard(str(path))
                return
            
            try:
                curr_size = path.stat().st_size
                if curr_size > 0 and curr_size == last_size:
                    stable_count += 1
                    if stable_count >= 2:
                        with open(path, "rb") as f:
                            pass
                        break
                else:
                    stable_count = 0
                    last_size = curr_size
            except (PermissionError, OSError):
                stable_count = 0
                continue
        else:
            print(f"[超时] 文件仍在占用中，跳过: {path.name}")
            with self.lock:
                self.processing.discard(str(path))
            return

        # 文件下载写盘完成，推入全局 FIFO 串行工作队列
        self.task_queue.put(path)
        q_size = self.task_queue.qsize()
        print(f"[加入排队] 《{path.name}》已就绪，进入工作队列！(当前等待排队数: {q_size})")

    def _queue_consumer(self):
        worker_script = CURRENT_DIR / "bili2x_worker.py"
        while True:
            path = self.task_queue.get()
            try:
                remaining = self.task_queue.qsize()
                print("\n" + "=" * 60)
                print(f"[队列调度] 开始处理: 《{path.name}》 (后续等待中: {remaining} 个)")
                print("=" * 60)
                cmd = [sys.executable, str(worker_script), str(path)]
                subprocess.run(cmd, check=True)
                print(f"[队列调度] 《{path.name}》任务结束，工作进程已退出。")
            except subprocess.CalledProcessError as e:
                print(f"[Worker 执行异常] 《{path.name}》退出码: {e.returncode}")
            except Exception as e:
                print(f"[处理异常] 《{path.name}》: {e}")
            finally:
                self.task_queue.task_done()
                with self.lock:
                    self.processing.discard(str(path))

def start_watching():
    disable_quick_edit()
    config = load_config()
    inbox_dir = Path(config.get("inbox_dir", CURRENT_DIR / "inbox"))
    out_dir = Path(config.get("output_dir", CURRENT_DIR / "output"))
    sub_dir = Path(config.get("subtitles_dir", CURRENT_DIR / "subtitles"))
    
    inbox_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    sub_dir.mkdir(parents=True, exist_ok=True)
    
    print("=" * 65)
    print("   Bili2X 自动化英日双语字幕流水线 (守护监听模式已就绪)")
    print("   - 队列管理: FIFO 串行任务队列 (单任务独立显卡执行)")
    print("   - 显存安全: 单视频逐个处理，工作进程退出后释放其资源")
    print("   - 字幕样式: 英日双语，样式按 config.json 配置")
    print("   - 硬件加速: FFmpeg NVENC 显卡硬件编码")
    print(f"   [监听收件箱] {inbox_dir}")
    print(f"   [视频成品库] {out_dir} (纯净 [EN_JA]_*.mp4，一键发布 X)")
    print(f"   [独立字幕库] {sub_dir} (中/英/日/双语 SRT 独立存放)")
    print("=" * 65)
    print(">>> 正在监听中，只要往收件箱存入视频即自动触发！按 Ctrl+C 退出 <<<\n")

    event_handler = VideoDownloadHandler(config)
    
    # 启动时检查是否有未处理的存量视频
    for item in inbox_dir.iterdir():
        if item.is_file() and item.suffix.lower() in VIDEO_EXTS:
            out_target = out_dir / f"[EN_JA]_{item.name}"
            if not out_target.exists():
                print(f"[存量发现] 检测到未转制双语视频: {item.name}，自动加入排队...")
                event_handler._handle_possible_video(str(item))

    observer = Observer()
    observer.schedule(event_handler, str(inbox_dir), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    start_watching()
