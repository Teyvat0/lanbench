# -*- coding: utf-8 -*-
"""LANBench 服务端：一个线程池式的简易 TCP/UDP 测速服务。

对外行为：
  * TCP 每个连接一个线程，先握手再执行一条命令；
  * DOWNLOAD 用同一个 1MiB 缓冲区循环 sendall，避免反复分配内存；
  * UDP 收到什么原样回显；
  * stop() 可以随时优雅关停（accept 循环用 0.5s 超时轮询）。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Callable, Optional

from . import netutil
from .protocol import (
    CHUNK,
    CMD_DOWNLOAD,
    CMD_PING,
    CMD_UPLOAD,
    DEFAULT_PORT,
    MAGIC,
    PING_LEN,
    PROTO_VERSION,
    SIZE_LEN,
    pack_ack,
    pack_greeting,
    recv_exact,
    tune_socket,
)

LogFn = Callable[[str], None]

IS_WINDOWS = os.name == "nt"


def _allow_rebind(sock: socket.socket) -> None:
    """按平台决定要不要 SO_REUSEADDR。

    Windows 的 SO_REUSEADDR 语义和 POSIX 完全不同：它允许第二个 socket 绑到**已经
    LISTEN** 的同一端口上（本机实测：持有者与后来者都设了它时，第二个实例会静默
    绑定成功，端口冲突被吞掉，两个实例都显示"监听中"）。Windows 又不需要它来应对
    TIME_WAIT（实测不设也能立刻重新监听），所以 Windows 上坚决不设。
    POSIX 上 SO_REUSEADDR 只影响 TIME_WAIT 复用，不会劫持活跃监听，必须保留，
    否则重启时会 EADDRINUSE。
    """
    if not IS_WINDOWS:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass


def friendly_bind_error(port: int, exc: OSError) -> str:
    """把 bind 失败翻译成人话：WinError 10013 在 Windows 上多半是端口被别人占了。"""
    msg = f"端口 {port} 绑定失败：{exc}"
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if code in (13, 48, 98, 100, 10013, 10048):
        msg += (
            "\n\n可能原因：该端口已被别的程序占用，或被系统保留"
            "（Hyper-V / WSL 会预留成段的动态端口）。\n"
            f"查看占用：netstat -ano | findstr :{port}\n"
            "查看保留：netsh int ipv4 show excludedportrange protocol=tcp\n"
            "换个端口再试即可（例如 19527）。"
        )
    return msg


class LanBenchServer:
    """一个进程内可用的测速服务端。"""

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        host: str = "0.0.0.0",
        on_log: Optional[LogFn] = None,
    ) -> None:
        self.port = int(port)
        self.host = host
        self.on_log = on_log
        self.actual_port = self.port
        self.bytes_served = 0          # 累计服务字节数（下载+上传）
        self.connections = 0
        self._tcp: Optional[socket.socket] = None
        self._udp: Optional[socket.socket] = None
        self._conns: set[socket.socket] = set()   # 已接受的连接，stop() 时要一起断开
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._send_buf = b"\x5a" * CHUNK   # 复用的下载数据块

    # ---------------------------------------------------------------- 生命周期
    @property
    def running(self) -> bool:
        return not self._stop.is_set() and self._tcp is not None

    def start(self) -> int:
        """绑定端口并启动后台线程，返回真实监听端口。"""
        if self._tcp is not None:
            return self.actual_port

        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _allow_rebind(tcp)
        try:
            tcp.bind((self.host, self.port))
        except OSError:
            tcp.close()          # 别把半成品 socket 留着，让调用方拿到干净的异常
            raise
        tcp.listen(128)
        tcp.settimeout(0.5)
        self.actual_port = tcp.getsockname()[1]
        self._tcp = tcp

        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _allow_rebind(udp)
        try:
            udp.bind((self.host, self.actual_port))
        except OSError as exc:
            udp.close()
            udp = None
            self._log(f"UDP 端口绑定失败（{exc}），UDP 丢包测试将不可用")
        self._udp = udp

        self._stop.clear()
        self._threads = [
            self._spawn(self._accept_loop, "lanbench-accept"),
        ]
        if udp is not None:
            self._threads.append(self._spawn(self._udp_loop, "lanbench-udp"))

        self._log(f"服务已启动，监听 {self.host}:{self.actual_port}（TCP+UDP）")
        return self.actual_port

    def stop(self) -> None:
        self._stop.set()
        for s in (self._tcp, self._udp):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._tcp = None
        self._udp = None
        # 已接受的连接也要主动断开：否则它们可能正阻塞在 sendall / recv 上，
        # 线程要等到 TCP 超时才退出（GUI 里服务端是常驻的）。
        with self._lock:
            live = list(self._conns)
        for c in live:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads = []
        self._log("服务已停止")

    def _spawn(self, fn, name: str) -> threading.Thread:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        return t

    # ------------------------------------------------------------------- 线程体
    def _accept_loop(self) -> None:
        assert self._tcp is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self.connections += 1
            self._spawn(lambda c=conn, a=addr: self._handle_tcp(c, a), "lanbench-conn")

    def _udp_loop(self) -> None:
        sock = self._udp
        assert sock is not None
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                sock.sendto(data, addr)
            except OSError:
                pass

    def _handle_tcp(self, conn: socket.socket, addr) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        with self._lock:
            self._conns.add(conn)
        try:
            tune_socket(conn)
            conn.settimeout(5.0)
            hello = recv_exact(conn, len(MAGIC))
            if hello != MAGIC:
                self._log(f"{peer} 不是 LANBench 客户端，已断开")
                return
            conn.sendall(pack_greeting(netutil.hostname()))
            conn.settimeout(None)

            # 一条连接可以连续执行多条命令：延迟测试就是在同一条连接上打 N 次
            # ping，避免每条探测都重新握手。
            while not self._stop.is_set():
                try:
                    cmd = recv_exact(conn, 1)
                except (ConnectionError, OSError):
                    break  # 客户端正常收工
                if cmd == CMD_PING:
                    token = recv_exact(conn, PING_LEN - 1)
                    conn.sendall(cmd + token)
                    continue
                if cmd == CMD_DOWNLOAD:
                    size = int.from_bytes(recv_exact(conn, SIZE_LEN - 1), "big")
                    n = self._serve_download(conn, size)
                    self._log(f"{peer} 下载完成 {n / 1048576:.1f} MiB")
                    if size == 0:
                        break  # 流式模式：对端已经断开
                    continue
                if cmd == CMD_UPLOAD:
                    size = int.from_bytes(recv_exact(conn, SIZE_LEN - 1), "big")
                    n = self._serve_upload(conn, size)
                    conn.sendall(pack_ack(n))
                    self._log(f"{peer} 上传完成 {n / 1048576:.1f} MiB")
                    break
                self._log(f"{peer} 未知命令 {cmd!r}，已断开")
                break
        except (ConnectionError, socket.timeout, OSError) as exc:
            if not self._stop.is_set():
                self._log(f"{peer} 连接中断：{exc!r}")
        finally:
            with self._lock:
                self._conns.discard(conn)
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def _serve_download(self, conn: socket.socket, size: int) -> int:
        """向客户端灌数据，返回实际发出的字节数。size=0 表示发到对端断开。"""
        buf = self._send_buf
        sent = 0
        while not self._stop.is_set():
            if size:
                remain = size - sent
                if remain <= 0:
                    break
                chunk = buf if remain >= CHUNK else memoryview(buf)[:remain]
            else:
                chunk = buf
            try:
                conn.sendall(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                break
            sent += len(chunk)
        self._add_served(sent)
        return sent

    def _serve_upload(self, conn: socket.socket, size: int) -> int:
        """读客户端发来的数据，返回收到的字节数。size=0 表示读到 EOF。"""
        buf = bytearray(CHUNK)
        view = memoryview(buf)
        got = 0
        while not self._stop.is_set():
            want = CHUNK
            if size:
                remain = size - got
                if remain <= 0:
                    break
                want = min(CHUNK, remain)
            try:
                n = conn.recv_into(view[:want], want)
            except (ConnectionResetError, socket.timeout):
                break
            except OSError:
                break
            if n == 0:
                break
            got += n
        self._add_served(got)
        return got

    # -------------------------------------------------------------------- 工具
    def _add_served(self, n: int) -> None:
        with self._lock:
            self.bytes_served += n

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(f"[{time.strftime('%H:%M:%S')}] {msg}")
