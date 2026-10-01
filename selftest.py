# -*- coding: utf-8 -*-
"""LANBench 自检脚本：在本机回环上起服务端，跑完整一轮测试并校验结果。

    python selftest.py            # 默认 2 秒/方向、2 条流
    python selftest.py -d 5 -s 4  # 自定义时长和并发流

全部用例通过退出码为 0，否则为 1。用于交付前验证协议、吞吐与异常处理。
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading

from lanbench.client import SpeedTester, TestError
from lanbench.protocol import fmt_bytes
from lanbench.server import LanBenchServer

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" —— {detail}" if detail else ""))
    return ok


def fake_server(sock: socket.socket) -> None:
    """假装是 LANBench，但握手回垃圾数据，用来验证客户端能识别出来。"""
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        def handle(c: socket.socket) -> None:
            try:
                c.recv(64)
                c.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            except OSError:
                pass
            finally:
                c.close()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--duration", type=float, default=2.0)
    ap.add_argument("-s", "--streams", type=int, default=2)
    ap.add_argument("--min-mbps", type=float, default=200.0,
                    help="回环吞吐下限，低于这个值认为实现有问题")
    args = ap.parse_args()

    print("=" * 72)
    print("LANBench 自检：本机回环")
    print("=" * 72)

    server = LanBenchServer(port=0, host="127.0.0.1", on_log=lambda m: None)
    port = server.start()
    print(f"服务端已监听 127.0.0.1:{port}（TCP+UDP）\n")

    try:
        # --- 1. 握手 + TCP 延迟 ---------------------------------------------
        tester = SpeedTester("127.0.0.1", port, duration=args.duration, streams=args.streams)
        ping = tester.tcp_ping(10)
        check("TCP 握手成功并识别对端主机名", bool(tester.server_name),
              f"对端={tester.server_name or '未知'}")
        check("TCP 延迟测试无丢包", ping.recv == 10 and ping.sent == 10,
              f"发 {ping.sent} 收 {ping.recv}，平均 {ping.rtt_avg:.2f} ms")
        check("回环 RTT 合理（< 5 ms）", 0 < ping.rtt_avg < 5.0,
              f"平均 {ping.rtt_avg:.3f} ms，抖动 {ping.jitter:.3f} ms")

        # --- 2. UDP 丢包/抖动 -----------------------------------------------
        udp = tester.udp_test(100)
        check("UDP 回显无丢包", udp.loss == 0.0 and udp.sent == 100,
              f"发 {udp.sent} 收 {udp.recv}，平均 {udp.rtt_avg:.2f} ms")

        # --- 3. 下载吞吐 -----------------------------------------------------
        dl = tester.download()
        check("下载吞吐达标", dl.mbps >= args.min_mbps,
              f"{dl.mbps:,.1f} Mbps（{dl.mib_per_s:.1f} MiB/s，{fmt_bytes(dl.total_bytes)}）")
        check("下载时长符合预期", dl.seconds >= args.duration * 0.8,
              f"{dl.seconds:.2f}s / 目标 {args.duration:.1f}s")
        check("下载无流异常", not dl.errors, dl.errors[0] if dl.errors else "无")

        # --- 4. 上传吞吐 -----------------------------------------------------
        ul = tester.upload()
        check("上传吞吐达标", ul.mbps >= args.min_mbps,
              f"{ul.mbps:,.1f} Mbps（{ul.mib_per_s:.1f} MiB/s，{fmt_bytes(ul.total_bytes)}）")
        check("上传无流异常", not ul.errors, ul.errors[0] if ul.errors else "无")

        # --- 5. 服务端统计 ---------------------------------------------------
        # 上传总量现在以服务端 ack（实收字节数）为准，所以服务端累计流量应当
        # 不小于客户端两个方向之和（下载方向还有在途数据，只会更多）。
        check("服务端统计流量与客户端一致",
              server.bytes_served >= dl.total_bytes + ul.total_bytes,
              f"服务端 {fmt_bytes(server.bytes_served)}，客户端 {fmt_bytes(dl.total_bytes + ul.total_bytes)}"
              f"，连接数 {server.connections}")

        # --- 6. 提前取消能正常收尾 --------------------------------------------
        t2 = SpeedTester("127.0.0.1", port, duration=30, streams=1)
        threading.Timer(0.6, t2.stop).start()
        try:
            r = t2.download()
            check("中途停止能及时返回", r.seconds < 10,
                  f"实际跑了 {r.seconds:.2f}s（目标 30s）")
        except TestError as exc:
            check("中途停止能及时返回", "取消" in str(exc), str(exc))

        # --- 7. 端口上不是 LANBench 时给出明确错误 ----------------------------
        fake = socket.socket()
        fake.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        fake.bind(("127.0.0.1", 0))
        fake.listen(8)
        fake_port = fake.getsockname()[1]
        threading.Thread(target=fake_server, args=(fake,), daemon=True).start()
        t3 = SpeedTester("127.0.0.1", fake_port, timeout=2.0)
        try:
            t3.tcp_ping(3)
            check("拒绝非 LANBench 服务", False, "本应报错却成功了")
        except TestError as exc:
            check("拒绝非 LANBench 服务", "不是 LANBench" in str(exc), str(exc))
        finally:
            fake.close()

        # --- 8. 连不上的地址给出明确错误 --------------------------------------
        free = socket.socket()          # 拿一个刚释放的端口，比固定用 9 号端口可靠
        free.bind(("127.0.0.1", 0))
        dead_port = free.getsockname()[1]
        free.close()
        t4 = SpeedTester("127.0.0.1", dead_port, timeout=1.5)
        try:
            t4.tcp_ping(1)
            check("连不上时有清晰错误", False, "本应报错却成功了")
        except TestError as exc:
            check("连不上时有清晰错误", "失败" in str(exc) or "超时" in str(exc), str(exc))

        # --- 9. 端口被别人占用时必须拒绝启动，不能静默"劫持"（Windows 特有坑）---
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(5)
        busy_port = holder.getsockname()[1]
        try:
            srv2 = LanBenchServer(port=busy_port, host="127.0.0.1")
            try:
                srv2.start()
                srv2.stop()
                check("端口被占用时拒绝启动", False,
                      f"竟然在已被占用的 {busy_port} 上绑定成功（两个实例会互相抢连接）")
            except OSError as exc:
                check("端口被占用时拒绝启动", True, f"正确报错：{exc}")
        finally:
            holder.close()

        # --- 10. 停机后能立刻用同一端口重新启动，在主服务 stop() 之后测（见下）---
    finally:
        server.stop()

    # 真实场景：用户来回开关服务，端口上还留着上一轮测试的 TIME_WAIT 连接
    srv3 = LanBenchServer(port=port, host="127.0.0.1")
    try:
        again = srv3.start()
        check("停机后可立刻重绑同一端口", again == port, f"重新监听 {again}（原 {port}）")
    except OSError as exc:
        check("停机后可立刻重绑同一端口", False, f"重绑失败：{exc}")
    finally:
        srv3.stop()

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 72)
    print(f"共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  FAILED: {name} —— {detail}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
