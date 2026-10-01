# -*- coding: utf-8 -*-
"""本机地址探测等小工具。"""

from __future__ import annotations

import socket
from functools import lru_cache


def _is_usable(ip: str) -> bool:
    if not ip or ip.startswith("127.") or ip.startswith("169.254."):
        return False
    if ip == "0.0.0.0":
        return False
    return True


def primary_ip() -> str:
    """默认路由出口地址；完全离线/无默认路由时返回空串。

    注意：UDP connect 不会真的发包，只是让内核选一条路由，所以内网环境也能用。
    """
    for probe in ("8.8.8.8", "1.1.1.1", "223.5.5.5"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((probe, 53))
                ip = s.getsockname()[0]
            finally:
                s.close()
            if _is_usable(ip):
                return ip
        except OSError:
            continue
    return ""


@lru_cache(maxsize=1)
def local_ipv4s() -> tuple[str, ...]:
    """本机所有可用的 IPv4 地址，默认路由地址排在最前。

    结果缓存：解析主机名在域内 DNS 慢的时候能卡住几百毫秒，而界面启动时要调用。
    返回 tuple 是为了配合缓存（列表会被调用方改坏）。
    """
    found: list[str] = []

    def add(ip: str) -> None:
        if _is_usable(ip) and ip not in found:
            found.append(ip)

    add(primary_ip())
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return tuple(found)


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"
