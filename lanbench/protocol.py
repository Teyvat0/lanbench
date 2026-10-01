# -*- coding: utf-8 -*-
"""LANBench 线协议：常量与报文编解码。

TCP 连上之后先握手，用来确认对端真的是 LANBench（而不是碰巧占用了同一个
端口的别的服务）：::

    C -> S : b"LBS1"
    S -> C : b"LBS1" + ver(1B) + name_len(1B) + hostname(name_len B)

握手之后客户端发一条命令，服务端按命令处理：::

    PING      b"P" + token(8B, 小端)   -> 原样回显这 9 字节
    DOWNLOAD  b"D" + size(4B, 大端)    -> size=0 表示一直发到客户端断开
    UPLOAD    b"U" + size(4B, 大端)    -> size=0 表示客户端发到 EOF 为止
                                         收完再回 8 字节 ack（大端，收字节数）

UDP 用同一个端口号：服务端收到什么就原样回显什么。
"""

from __future__ import annotations

import socket
import struct

MAGIC = b"LBS1"
PROTO_VERSION = 1
DEFAULT_PORT = 9527

CMD_PING = b"P"
CMD_DOWNLOAD = b"D"
CMD_UPLOAD = b"U"

PING_LEN = 9           # 1 字节命令 + 8 字节 token
SIZE_LEN = 5           # 1 字节命令 + 4 字节长度
GREETING_LEN = 6       # magic(4) + ver(1) + name_len(1)

CHUNK = 1 << 20        # 1 MiB，收发缓冲区
UP_CHUNK = 256 << 10   # 256 KiB，上传时便于及时检查截止时间
SOCK_BUF = 4 << 20     # 收发缓冲上限（内核可能截断到更小值）

UDP_PAYLOAD = 32       # UDP 探测包载荷（seq 8B + 时间戳 8B + 填充）


def pack_ping(token: int) -> bytes:
    """PING 请求：命令 + 8 字节小端 token。"""
    return CMD_PING + struct.pack("<Q", token)


def pack_size_cmd(cmd: bytes, size: int) -> bytes:
    """DOWNLOAD / UPLOAD 请求头：命令 + 4 字节大端长度（0 = 流式）。

    只接受 0 ~ 4GiB-1；负数直接报错，免得被 `& 0xFFFFFFFF` 悄悄变成 4GiB 请求。
    """
    if not 0 <= size <= 0xFFFFFFFF:
        raise ValueError(f"size 必须在 0 ~ 4294967295 之间，收到 {size}")
    return cmd + struct.pack(">I", size)


def pack_ack(n: int) -> bytes:
    return struct.pack(">Q", n)


def unpack_ack(data: bytes) -> int:
    return struct.unpack(">Q", data[:8])[0]


def pack_greeting(hostname: str) -> bytes:
    name = hostname.encode("utf-8", "replace")[:255]
    return MAGIC + bytes([PROTO_VERSION, len(name)]) + name


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """读满 n 字节；对端提前关闭时抛 ConnectionError。"""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        k = sock.recv_into(view[got:], n - got)
        if k == 0:
            raise ConnectionError("对端提前关闭了连接")
        got += k
    return bytes(buf)


def tune_socket(sock: socket.socket) -> None:
    """把 TCP 缓冲调大、关掉 Nagle，尽量让千兆网跑满。"""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, SOCK_BUF)
        except OSError:
            pass


def fmt_bytes(n: float) -> str:
    """人类可读的字节数。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024.0
    return f"{n:.2f} TB"
