"""抖音浏览器 worker 的 Python 宿主 —— 管 Node 子进程 + JSON 行协议。

为什么是「Python 壳 + Node worker」而不是纯 Python:

  原项目 dsh-douyin-agent 本身就是这个形态 —— 浏览器控制在 Node 侧(Playwright),
  上层调度在别处。我们参考它的实现方式,保留同一套已验证的 worker 代码
  (electron/douyin-browser-worker.mjs 的副本),只把上层换成 Hermes 插件。

  而且这个形态在本机是**必须**的:Hermes 插件的 Python 侧没有装 Playwright
  (已实测 `import playwright` → ModuleNotFoundError),但 Hermes 自己的
  node_modules 里有 playwright-core,Node 侧驱动 Edge 完全没问题。

协议:stdin 一行一个 JSON 发请求,stdout 一行一个 JSON 回响应。
  请求 {"id":"r1","type":"start"}
  响应 {"id":"r1","type":"ok","value":...} / {"id":"r1","type":"error","message":"..."}
  主动事件 {"type":"state"|"observe","..."} 不带 id。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

PLUGIN_DIR = Path(__file__).resolve().parent
WORKER_PATH = PLUGIN_DIR / "worker" / "douyin-browser-worker.mjs"

# 专用 Edge 的 CDP 端口(避开 9222 —— 那是桌宠的调试口)
EDGE_CDP_PORT = 9333


class DouyinWorkerError(RuntimeError):
    """worker 侧失败(浏览器起不来、页面没开、协议违规等)。"""


def _hermes_home() -> Path:
    """HERMES_HOME 优先,否则退回 ~/.hermes —— 永远不硬编码用户目录。"""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def find_node() -> Optional[str]:
    """找一个可用的 node.exe。顺序:PATH → Hermes 自带 → 常见安装位置。"""
    node_name = "node.exe" if os.name == "nt" else "node"
    for env_key in ("PATH", "Path"):
        raw = os.environ.get(env_key, "")
        for entry in raw.split(os.pathsep):
            entry = entry.strip().strip('"')
            if not entry:
                continue
            candidate = Path(entry) / node_name
            if candidate.is_file():
                return str(candidate)
    # Hermes 自带的 node
    tools = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "tools"
    try:
        for entry in sorted(tools.iterdir()):
            if entry.name.startswith("node-"):
                candidate = entry / node_name
                if candidate.is_file():
                    return str(candidate)
    except OSError:
        pass
    for candidate in (
        Path("C:/Program Files/nodejs") / node_name,
        Path("D:/nodejs") / node_name,
        Path("D:/hermes-data/node") / node_name,
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def find_playwright_root() -> Optional[str]:
    """找 playwright-core 所在的那层目录(即含 node_modules 的父目录)。

    worker 用 createRequire 从这里解析 playwright-core —— ESM 的 `import`
    只在文件所在目录向上找 node_modules,插件目录离 Hermes 源码树很远,
    不显式指定就 MODULE_NOT_FOUND。
    """
    candidates = [
        _hermes_home() / "hermes-agent",
        PLUGIN_DIR,
        PLUGIN_DIR.parent,
    ]
    # 也看看 Hermes 源码树(可能不在 HERMES_HOME 下)
    home = Path.home()
    candidates += [home / ".hermes" / "hermes-agent", home / "hermes-agent"]
    for base in candidates:
        if (base / "node_modules" / "playwright-core" / "package.json").is_file():
            return str(base)
    # PATH 上有没有全局装的 playwright
    which = shutil.which("node")
    if which:
        node_root = Path(which).resolve().parent.parent
        if (node_root / "node_modules" / "playwright-core" / "package.json").is_file():
            return str(node_root)
    return None


def find_edge() -> Optional[str]:
    """找系统 Edge —— Playwright 用 channel:'msedge' 驱动它,不下载 Chromium。"""
    for candidate in (
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidate = Path(local) / "Microsoft/Edge/Application/msedge.exe"
        if candidate.is_file():
            return str(candidate)
    return None


class DouyinWorker:
    """Node worker 子进程的宿主:spawn、收发 JSON、超时控制。"""

    def __init__(self, profile_dir: Optional[Path] = None, port: int = EDGE_CDP_PORT):
        self.profile_dir = Path(profile_dir) if profile_dir else (_hermes_home() / "browser-profile" / "douyin")
        self.port = port
        self._proc: Optional[subprocess.Popen] = None
        self._buffer = ""
        self._lock = threading.Lock()
        # ★ 起进程也要锁:同一个扩展进程里两个 session 同时调 douyin_watch 时,
        #   两个线程会同时通过 "_proc is None" 检查 → 起两个 worker 抢同一个 CDP 端口
        #   → 一个报 ERR_ABORTED、另一个报 worker 没起(2026-11-13 实测)。
        self._start_lock = threading.Lock()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._seq = 0
        self._events: list = []

    # ---- 生命周期 ----

    def start(self, timeout: float = 30.0) -> None:
        """起 worker 子进程(不启动浏览器 —— 浏览器由 'start' 指令触发)。"""
        with self._start_lock:
            self._start_locked(timeout)

    def _start_locked(self, timeout: float) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        node = find_node()
        if not node:
            raise DouyinWorkerError("找不到 node.exe —— 插件需要 Node 来跑浏览器 worker。")
        if not WORKER_PATH.is_file():
            raise DouyinWorkerError(f"worker 文件缺失:{WORKER_PATH}")
        pw_root = find_playwright_root()
        if not pw_root:
            raise DouyinWorkerError(
                "找不到 playwright-core —— 插件需要它来驱动 Edge。"
                "装法:cd <hermes-agent> && npm install playwright-core"
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._buffer = ""
        self._proc = subprocess.Popen(
            [node, str(WORKER_PATH), str(self.profile_dir), str(self.port), pw_root],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(PLUGIN_DIR),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        threading.Thread(target=self._watch_exit, daemon=True).start()
        # 等 ready
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ready:
                return
            if self._proc.poll() is not None:
                # ★ 把 worker 的 stderr 带上:锁冲突("另一个刷抖音任务正在跑")、
                #   playwright 缺失这类原因都只写在 stderr 里,不带上等于把
                #   「为什么失败」丢掉,用户只会看到 code=3(2026-11-13 实测)。
                time.sleep(0.3)  # 等 _pump_stderr 把最后一行读进来
                detail = ""
                for ev in self._events[-3:]:
                    if ev.get("type") == "stderr" and ev.get("text"):
                        detail = ev["text"]
                raise DouyinWorkerError(
                    f"worker 启动即退出,code={self._proc.returncode}"
                    + (f":{detail}" if detail else "")
                )
            time.sleep(0.05)
        raise DouyinWorkerError(f"等 worker ready 超时({timeout}s)")

    _ready = False

    def stop(self, timeout: float = 10.0) -> None:
        """停 worker(会连带关掉 Edge —— worker 自己处理 SIGTERM)。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                # ★ 不要用 _send():它在 _proc 空掉时会自动重起一个 worker ——
                #   为了「发个 stop」而启动进程是反的。这里直接写管道。
                try:
                    if proc.stdin is not None:
                        proc.stdin.write(json.dumps({"type": "stop"}) + "\n")
                        proc.stdin.flush()
                except Exception:
                    pass
                # ★ 先等它自己收拾(关 Edge / 删锁 / 退进程),再考虑强杀。
                #   Windows 上 terminate() = TerminateProcess,Node 收不到 SIGTERM,
                #   'exit' 钩子也不会跑 —— 直接 terminate 会留下 worker.lock 和 Edge(实测)。
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        proc.kill()
        except Exception:
            pass
        finally:
            self._ready = False
            # 顺手把锁清掉:Windows 上 terminate() 是 TerminateProcess,
            # worker 的 'exit' 钩子不保证跑 → 锁会留成孤儿文件(下次能自愈,但没必要)。
            try:
                (self.profile_dir / "worker.lock").unlink(missing_ok=True)
            except OSError:
                pass
            with self._lock:
                for entry in self._pending.values():
                    entry["timer"].cancel()
                    entry["event"].set()
                self._pending.clear()

    # ---- 协议 ----

    def request(self, type_: str, timeout: float = 60.0, **extra: Any) -> Any:
        """发一条请求并等它的响应。"""
        with self._lock:
            self._seq += 1
            msg_id = f"r{self._seq}"
            event = threading.Event()
            slot = {"event": event, "value": None, "error": None, "timer": None}
            self._pending[msg_id] = slot

        timer = threading.Timer(timeout, lambda: self._fail(msg_id, f"{type_} 超时({timeout}s)"))
        timer.daemon = True
        slot["timer"] = timer
        timer.start()

        self._send({"id": msg_id, "type": type_, **extra})

        if not event.wait(timeout + 5.0):
            self._fail(msg_id, f"{type_} 等待超时")
        with self._lock:
            self._pending.pop(msg_id, None)
        if slot["error"]:
            raise DouyinWorkerError(str(slot["error"]))
        return slot["value"]

    def _send(self, payload: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            # ★ 自愈:worker 被上一条会话的 on_session_end 关掉了,或崩了 ——
            #   重起一个再发,别让「worker 没起」这种内部状态漏给用户。
            self.start()
            proc = self._proc
        if proc is None or proc.stdin is None:
            raise DouyinWorkerError("worker 没起")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise DouyinWorkerError(f"写 worker 失败:{exc}") from exc

    def _fail(self, msg_id: str, message: str) -> None:
        with self._lock:
            slot = self._pending.get(msg_id)
        if slot and not slot["event"].is_set():
            slot["error"] = message
            slot["event"].set()

    def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            self._buffer += raw
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle(msg)

    def _pump_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            text = raw.strip()
            if text:
                self._events.append({"type": "stderr", "text": text})

    def _watch_exit(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.wait()
        self._ready = False
        with self._lock:
            ids = list(self._pending.keys())
        for msg_id in ids:
            self._fail(msg_id, f"worker 退出了,code={code}")

    def _handle(self, msg: Dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "ready":
            self._ready = True
            return
        msg_id = msg.get("id")
        if msg_id:
            with self._lock:
                slot = self._pending.get(msg_id)
            if slot is None:
                return
            if msg_type == "error":
                slot["error"] = msg.get("message") or "worker 返回错误"
            else:
                slot["value"] = msg.get("value")
            slot["event"].set()
            return
        # 主动事件
        self._events.append(msg)

    def drain_events(self) -> list:
        """取走并清空主动事件。"""
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events


# ---- 进程内单例 ----

_worker: Optional[DouyinWorker] = None
_worker_lock = threading.Lock()


def get_worker() -> DouyinWorker:
    """全局唯一的 worker(浏览器和登录态都要跨调用保持)。"""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = DouyinWorker()
        if _worker._proc is None or _worker._proc.poll() is not None:
            _worker.start()
        return _worker
