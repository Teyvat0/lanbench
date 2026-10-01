# -*- coding: utf-8 -*-
"""LANBench 命令行入口：可以做纯服务端，也可以做命令行测速端。

常用：
    python -m lanbench                     # 打开 GUI
    python -m lanbench serve               # 只跑服务端（无界面，适合放服务器上）
    python -m lanbench test 192.168.1.20   # 命令行测速，输出成绩单
    python -m lanbench firewall            # 打印放行防火墙的命令
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time

from . import APP_NAME, __version__, netutil
from .client import PingResult, SpeedTester, TestError
from .protocol import DEFAULT_PORT, fmt_bytes
from .server import LanBenchServer, friendly_bind_error


def _soft_stdout() -> None:
    """避免个别字符在 GBK 控制台里直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


# ====================================================================== serve
def cmd_serve(args: argparse.Namespace) -> int:
    # flush=True：服务端常驻又常被重定向到日志文件，不能等缓冲区满了才写盘
    def out(msg: str = "") -> None:
        print(msg, flush=True)

    server = LanBenchServer(port=args.port, host=args.host, on_log=out)
    try:
        port = server.start()
    except OSError as exc:
        out(friendly_bind_error(args.port, exc))
        return 1
    ips = netutil.local_ipv4s()
    out(f"{APP_NAME} v{__version__} 服务端已就绪（{netutil.hostname()}）")
    for ip in ips:
        out(f"  可供测试的地址：{ip}:{port}")
    if not ips:
        out("  未探测到可用的 IPv4 地址，请检查网卡。")
    out("  对端运行：python -m lanbench test <上面的地址>")
    out("  其他机器连不上时，用 python -m lanbench firewall 打印放行命令。")
    out("  Ctrl+C 退出。")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        served, conns = server.bytes_served, server.connections
        server.stop()
    out(f"\n共服务 {conns} 个连接，传输 {fmt_bytes(served)}。")
    return 0


# ======================================================================= test
def _fmt_ping(r: PingResult) -> str:
    return (f"发 {r.sent} 收 {r.recv} 丢包 {r.loss * 100:.1f}% | "
            f"最小 {r.rtt_min:.2f} 平均 {r.rtt_avg:.2f} 最大 {r.rtt_max:.2f} ms | "
            f"抖动 {r.jitter:.2f} ms")


def cmd_test(args: argparse.Namespace) -> int:
    quiet = args.json
    def log(msg: str) -> None:
        if not quiet:
            print(msg)

    def stage(msg: str) -> None:
        if not quiet:
            print(f"· {msg}")

    last_line = {"on": False}
    shown = {"pct": -25}

    def progress(mbps: float, text: str, fraction: float) -> None:
        if quiet:
            return
        if sys.stdout.isatty():
            sys.stdout.write(f"\r  {text}          ")
            sys.stdout.flush()
            last_line["on"] = True
            return
        # 管道/重定向时不能用 \r 原地刷新，按 25% 步进打点
        pct = int(fraction * 100)
        if pct >= shown["pct"] + 25:
            shown["pct"] = pct - pct % 25
            print(f"  {text}")

    tester = SpeedTester(
        args.host, port=args.port, duration=args.duration, streams=args.streams,
        on_log=log, on_stage=stage, on_progress=progress,
    )
    report: dict = {"host": args.host, "port": args.port, "ok": True, "results": {}}
    try:
        if not args.no_ping:
            r = tester.tcp_ping(args.ping_count)
            report["results"]["tcp"] = r.__dict__
            if last_line["on"]:
                print()
                last_line["on"] = False
            if not quiet:
                print(f"TCP 延迟：{_fmt_ping(r)}")
        if not args.no_udp:
            u = tester.udp_test(args.udp_count)
            report["results"]["udp"] = u.__dict__
            if last_line["on"]:
                print()
                last_line["on"] = False
            if not quiet:
                print(f"UDP 探测：{_fmt_ping(u)}")
        if not args.no_download:
            d = tester.download(args.duration)
            report["results"]["download"] = {"mbps": d.mbps, "mib_s": d.mib_per_s,
                                            "bytes": d.total_bytes, "seconds": d.seconds}
            if last_line["on"]:
                print()
                last_line["on"] = False
            if not quiet:
                print(f"下载：{d.mbps:,.1f} Mbps（{d.mib_per_s:.1f} MiB/s，{fmt_bytes(d.total_bytes)}）")
        if not args.no_upload:
            up = tester.upload(args.duration)
            report["results"]["upload"] = {"mbps": up.mbps, "mib_s": up.mib_per_s,
                                          "bytes": up.total_bytes, "seconds": up.seconds}
            if last_line["on"]:
                print()
                last_line["on"] = False
            if not quiet:
                print(f"上传：{up.mbps:,.1f} Mbps（{up.mib_per_s:.1f} MiB/s，{fmt_bytes(up.total_bytes)}）")
    except TestError as exc:
        report["ok"] = False
        report["error"] = str(exc)
        if last_line["on"]:
            print()
        print(f"测试失败：{exc}", file=sys.stderr)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif tester.server_name:
        print(f"对端主机名：{tester.server_name}")
    return 0


# =================================================================== firewall
def cmd_firewall(args: argparse.Namespace) -> int:
    port = args.port
    cmds = [
        f'netsh advfirewall firewall add rule name="LANBench TCP {port}" dir=in '
        f"action=allow protocol=TCP localport={port}",
        f'netsh advfirewall firewall add rule name="LANBench UDP {port}" dir=in '
        f"action=allow protocol=UDP localport={port}",
    ]
    print("请在【管理员】PowerShell/CMD 里执行下面两条命令，放行入站端口：")
    for c in cmds:
        print("  " + c)
    if args.apply:
        print("\n--apply 指定，正在尝试执行（需要管理员权限）…")
        for c in cmds:
            proc = subprocess.run(c, shell=True, capture_output=True, text=True)
            print(f"  退出码 {proc.returncode} {proc.stdout.strip()}{proc.stderr.strip()}")
    return 0


# ====================================================================== 参数
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lanbench",
        description=f"{APP_NAME} v{__version__} —— 轻量局域网测速（服务端与客户端一体）",
    )
    p.add_argument("--version", action="version", version=f"{APP_NAME} v{__version__}")
    sub = p.add_subparsers(dest="cmd")

    g = sub.add_parser("gui", help="打开图形界面（默认）")
    g.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"本机服务端口，默认 {DEFAULT_PORT}")
    g.add_argument("--no-serve", action="store_true", help="启动时不自动开启本机服务")

    s = sub.add_parser("serve", help="只跑服务端（无界面）")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--host", default="0.0.0.0")

    t = sub.add_parser("test", help="命令行测速")
    t.add_argument("host", help="目标地址，例如 192.168.1.20")
    t.add_argument("--port", type=int, default=DEFAULT_PORT)
    t.add_argument("--duration", type=float, default=10.0, help="每个方向跑多久（秒），默认 10")
    t.add_argument("--streams", type=int, default=4, help="并发连接数，默认 4")
    t.add_argument("--ping-count", type=int, default=10)
    t.add_argument("--udp-count", type=int, default=100)
    t.add_argument("--no-ping", action="store_true", help="跳过 TCP 延迟测试")
    t.add_argument("--no-udp", action="store_true", help="跳过 UDP 丢包/抖动测试")
    t.add_argument("--no-download", action="store_true")
    t.add_argument("--no-upload", action="store_true")
    t.add_argument("--json", action="store_true", help="只输出 JSON 结果（便于脚本调用）")

    f = sub.add_parser("firewall", help="打印/执行放行防火墙端口的命令")
    f.add_argument("--port", type=int, default=DEFAULT_PORT)
    f.add_argument("--apply", action="store_true", help="直接尝试执行（需管理员）")

    return p


def main(argv: list[str] | None = None) -> int:
    _soft_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd in (None, "gui"):
        from .gui import run_gui  # 延迟导入：无界面环境也能用 serve/test

        port = getattr(args, "port", DEFAULT_PORT)
        autoserve = not getattr(args, "no_serve", False)
        return run_gui(port=port, autoserve=autoserve)
    if args.cmd == "serve":
        return cmd_serve(args)
    if args.cmd == "test":
        return cmd_test(args)
    if args.cmd == "firewall":
        return cmd_firewall(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
