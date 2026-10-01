# -*- coding: utf-8 -*-
"""LANBench 测速引擎：延迟/抖动/丢包 + 多线程 TCP 上下行吞吐。

设计要点：
  * 每次测试用独立的 TCP 连接，握手用来识别对端；
  * 吞吐测试默认「按时长跑」：客户端连上后声明 size=0（流式），跑满 duration
    秒再断开，比「固定字节数」更贴近真实带宽；
  * 并发多流可打满千兆（单条 Python 流通常到不了 1 Gbps）；
  * 进度通过回调上报，GUI 用它刷新实时速率。
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .protocol import (
    CHUNK,
    CMD_DOWNLOAD,
    CMD_PING,
    CMD_UPLOAD,
    DEFAULT_PORT,
    GREETING_LEN,
    MAGIC,
    PING_LEN,
    PROTO_VERSION,
    UP_CHUNK,
    fmt_bytes,
    pack_ping,
    pack_size_cmd,
    recv_exact,
    tune_socket,
    unpack_ack,
)


class TestError(RuntimeError):
    """测速过程中的可预期错误（连不上、对端不是 LANBench 等）。"""


@dataclass
class PingResult:
    sent: int = 0
    recv: int = 0
    loss: float = 0.0            # 0~1
    rtt_min: float = 0.0
    rtt_avg: float = 0.0
    rtt_max: float = 0.0
    jitter: float = 0.0
    server_name: str = ""


@dataclass
class ThroughputResult:
    direction: str               # "download" / "upload"
    total_bytes: int
    seconds: float
    streams: int
    errors: list[str] = field(default_factory=list)

    @property
    def mbps(self) -> float:
        return self.total_bytes * 8 / self.seconds / 1e6 if self.seconds > 0 else 0.0

    @property
    def mib_per_s(self) -> float:
        return self.total_bytes / self.seconds / 1048576 if self.seconds > 0 else 0.0


class _Counter:
    def __init__(self) -> None:
        self.value = 0
        self._lock = threading.Lock()

    def add(self, n: int) -> None:
        with self._lock:
            self.value += n

    def get(self) -> int:
        with self._lock:
            return self.value


class _AckTally:
    """汇总各条流从服务端拿到的"实收字节数"，并记录拿到确认的流数。"""

    def __init__(self) -> None:
        self.total = 0
        self.streams = 0
        self._lock = threading.Lock()

    def add(self, n: int) -> None:
        with self._lock:
            self.total += n
            self.streams += 1


class _StartGate:
    """并发流的共同起跑线。

    每条流都要先建连接、握手、发命令头，这些耗时不该算进吞吐窗口；
    第一条流准备就绪时按下秒表，所有流共用同一个截止时间。
    """

    def __init__(self) -> None:
        self.t0: Optional[float] = None
        self._lock = threading.Lock()

    def arm(self, duration: float) -> float:
        with self._lock:
            if self.t0 is None:
                self.t0 = time.monotonic()
            return self.t0 + duration

    def elapsed(self) -> float:
        with self._lock:
            t0 = self.t0
        return (time.monotonic() - t0) if t0 is not None else 0.0


class SpeedTester:
    """针对某台 LANBench 服务端跑一轮测试。一个实例只跑一轮。"""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        duration: float = 10.0,
        streams: int = 4,
        timeout: float = 5.0,
        on_log: Optional[Callable[[str], None]] = None,
        on_stage: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[float, str, float], None]] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.duration = float(duration)
        self.streams = max(1, int(streams))
        self.timeout = float(timeout)
        self.on_log = on_log
        self.on_stage = on_stage
        self.on_progress = on_progress
        self.server_name = ""
        self._stop = threading.Event()

    # ------------------------------------------------------------------ 取消
    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise TestError("测试已取消")

    # ------------------------------------------------------------------ 日志
    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _stage(self, msg: str) -> None:
        if self.on_stage:
            self.on_stage(msg)

    def _progress(self, mbps: float, text: str, fraction: float = 0.0) -> None:
        if self.on_progress:
            self.on_progress(mbps, text, fraction)

    # ------------------------------------------------------------ 连接 / 握手
    def _connect(self) -> socket.socket:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except socket.timeout as exc:
            raise TestError(f"连接 {self.host}:{self.port} 超时（对方没开服务或被防火墙拦了）") from exc
        except OSError as exc:
            raise TestError(f"连接 {self.host}:{self.port} 失败：{exc}") from exc

        tune_socket(sock)
        try:
            sock.sendall(MAGIC)
            head = recv_exact(sock, GREETING_LEN)
            if head[:4] != MAGIC:
                raise TestError("对端不是 LANBench 服务（端口被别的程序占用了？）")
            ver = head[4]
            if ver != PROTO_VERSION:
                raise TestError(f"协议版本不一致：对端 v{ver}，本机 v{PROTO_VERSION}")
            name_len = head[5]
            if name_len:
                self.server_name = recv_exact(sock, name_len).decode("utf-8", "replace")
            sock.settimeout(None)
        except TestError:
            sock.close()
            raise
        except (OSError, ConnectionError) as exc:
            sock.close()
            raise TestError(f"与 {self.host}:{self.port} 握手失败：{exc}") from exc
        return sock

    # -------------------------------------------------------------- TCP 延迟
    def tcp_ping(self, count: int = 10, interval: float = 0.02) -> PingResult:
        self._stage(f"测量延迟（{count} 次 TCP 往返）…")
        sock = self._connect()
        result = PingResult(sent=0, recv=0, server_name=self.server_name)
        rtts: list[float] = []
        try:
            for i in range(count):
                self._check_stop()
                t0 = time.perf_counter()
                sock.sendall(pack_ping(i))
                data = recv_exact(sock, PING_LEN)
                t1 = time.perf_counter()
                if data[:1] == CMD_PING:
                    result.sent += 1
                    result.recv += 1
                    rtts.append((t1 - t0) * 1000.0)
                time.sleep(interval)
        except (OSError, ConnectionError) as exc:
            raise TestError(f"延迟测试中断：{exc}") from exc
        finally:
            sock.close()

        if rtts:
            result.rtt_min = min(rtts)
            result.rtt_max = max(rtts)
            result.rtt_avg = sum(rtts) / len(rtts)
            if len(rtts) > 1:
                diffs = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
                result.jitter = sum(diffs) / len(diffs)
        result.loss = 0.0 if not result.sent else (result.sent - result.recv) / result.sent
        self._log(
            f"TCP 延迟：平均 {result.rtt_avg:.2f} ms / 最小 {result.rtt_min:.2f} / "
            f"最大 {result.rtt_max:.2f}，抖动 {result.jitter:.2f} ms"
        )
        return result

    # --------------------------------------------------------------- UDP 测试
    def udp_test(self, count: int = 100, interval: float = 0.005) -> PingResult:
        """UDP 回显测丢包和抖动（更能反映无线/拥塞链路的真实情况）。

        先按 interval 把 count 个探测包发完，再最多等 grace 秒收尾巴，
        所以整个阶段耗时约为 count*interval + grace，不会空等。
        """
        self._stage(f"测量 UDP 丢包/抖动（{count} 个探测包）…")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.002)
        result = PingResult(server_name=self.server_name)
        received: dict[int, float] = {}
        try:
            sock.connect((self.host, self.port))
        except OSError as exc:
            sock.close()
            raise TestError(f"UDP 连接 {self.host}:{self.port} 失败：{exc}") from exc

        grace = min(self.timeout, 0.5)     # 内网 RTT 极低，等太久没意义
        start = time.monotonic()
        next_send = last_send = start
        attempt = 0            # 发送尝试次数：丢包率的分母，必须包含发送失败的包
        send_errors = 0
        hard_deadline = start + count * interval + grace + 2.0
        try:
            while True:
                self._check_stop()
                now = time.monotonic()
                if attempt < count and now >= next_send:
                    payload = struct.pack(">Qd", attempt, time.perf_counter()) + b"\x00" * 16
                    try:
                        sock.send(payload)
                    except OSError:
                        # 发不出去也算"发过了"，否则丢包率会被算成 0%（假好消息）
                        send_errors += 1
                    attempt += 1
                    last_send = now
                    next_send = max(next_send + interval, now)
                if attempt >= count and now > last_send + grace:
                    break
                if now > hard_deadline:
                    break
                try:
                    data, _ = sock.recvfrom(2048)
                except (socket.timeout, OSError):
                    continue
                if len(data) >= 16:
                    seq, ts = struct.unpack(">Qd", data[:16])
                    if seq not in received:   # 去重，避免重复回显把丢包算少
                        received[seq] = (time.perf_counter() - ts) * 1000.0
        finally:
            sock.close()

        result.sent = attempt
        if send_errors:
            self._log(f"UDP 有 {send_errors} 个探测包发送失败（已计入丢包率）")

        rtts = [received[s] for s in sorted(received)]
        result.recv = len(rtts)
        if result.sent:
            result.loss = min(1.0, max(0.0, (result.sent - result.recv) / result.sent))
        else:
            result.loss = 0.0
        if rtts:
            result.rtt_min = min(rtts)
            result.rtt_max = max(rtts)
            result.rtt_avg = sum(rtts) / len(rtts)
            if len(rtts) > 1:
                diffs = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
                result.jitter = sum(diffs) / len(diffs)
        self._log(
            f"UDP：发 {result.sent} 收 {result.recv}，丢包 {result.loss * 100:.1f}%，"
            f"平均 {result.rtt_avg:.2f} ms，抖动 {result.jitter:.2f} ms"
        )
        return result

    # -------------------------------------------------------------- 吞吐测试
    def download(self, duration: Optional[float] = None) -> ThroughputResult:
        return self._throughput("download", duration if duration is not None else self.duration)

    def upload(self, duration: Optional[float] = None) -> ThroughputResult:
        return self._throughput("upload", duration if duration is not None else self.duration)

    def _throughput(self, direction: str, duration: float) -> ThroughputResult:
        label = "下载" if direction == "download" else "上传"
        self._stage(f"{label}测试：{self.streams} 条并发流，跑 {duration:.0f} 秒…")
        shared = _Counter()          # 本地累计（实时速率用）
        acked = _AckTally()          # 服务端确认收到的字节数（上传方向的权威值）
        gate = _StartGate()          # 共同起跑线：扣掉并发流的建连/握手时间
        errors: list[str] = []
        started_at = time.monotonic()
        workers = [
            threading.Thread(
                target=self._download_worker if direction == "download" else self._upload_worker,
                args=(shared, acked, gate, errors),
                name=f"lanbench-{direction}-{i}",
                daemon=True,
            )
            for i in range(self.streams)
        ]
        last_t = started_at
        last_bytes = 0
        for w in workers:
            w.start()

        while any(w.is_alive() for w in workers):
            time.sleep(0.2)
            now = time.monotonic()
            elapsed = gate.elapsed()
            bytes_now = shared.get()
            dt = now - last_t
            # 按真实间隔算瞬时速率：sleep(0.2) 只是下限，写死 0.2 会把速率放大
            inst = (bytes_now - last_bytes) * 8 / 1e6 / dt if dt > 0 else 0.0
            last_t, last_bytes = now, bytes_now
            self._progress(
                inst,
                f"{label}中 {inst:,.0f} Mbps（{elapsed:.1f}/{duration:.0f}s）",
                min(1.0, elapsed / duration) if duration > 0 else 0.0,
            )
            if self._stop.is_set():
                break
            if time.monotonic() - started_at > self.timeout + duration + 5:
                break   # 兜底：对端连不上时不要在这里空转

        for w in workers:
            w.join(timeout=2.0)
            if w.is_alive():
                errors.append(f"{label}流未按时收尾（可能卡在 socket 上）")

        seconds = max(gate.elapsed(), 1e-9)
        total = shared.get()
        if direction == "upload":
            # sendall 返回 ≠ 数据已送达对端（可能还躺在本地发送缓冲里），
            # 服务端 ack 里的"实收字节数"才是权威值。
            if acked.streams == self.streams:
                if acked.total != total:
                    self._log(f"上传按服务端确认数统计：本地计数 {fmt_bytes(total)}，"
                              f"服务端实收 {fmt_bytes(acked.total)}")
                total = acked.total
            else:
                errors.append("上传：有流没拿到服务端确认，改用本地计数（可能偏高）")
        result = ThroughputResult(direction, total, seconds, self.streams, errors)
        self._log(
            f"{label}完成：{result.mbps:,.1f} Mbps（{result.mib_per_s:.1f} MiB/s）"
            f"，{fmt_bytes(total)} / {seconds:.2f}s"
        )
        if errors:
            self._log(f"{label}有 {len(errors)} 条流异常：{errors[0]}")
        return result

    # -------------------------------------------------------------- 工作线程
    def _download_worker(self, shared: _Counter, acked: _AckTally,
                         gate: _StartGate, errors: list[str]) -> None:
        sock = None
        try:
            sock = self._connect()
            sock.settimeout(2.0)
            sock.sendall(pack_size_cmd(CMD_DOWNLOAD, 0))
            deadline = gate.arm(self.duration)
            buf = bytearray(CHUNK)
            view = memoryview(buf)
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    n = sock.recv_into(view, CHUNK)
                except socket.timeout:
                    errors.append("下载流：对端超过 2 秒没有数据，已中止")
                    break
                if n <= 0:
                    break
                shared.add(n)
        except (TestError, OSError, ConnectionError) as exc:
            errors.append(f"下载流：{exc}")
        finally:
            if sock is not None:
                sock.close()

    def _upload_worker(self, shared: _Counter, acked: _AckTally,
                       gate: _StartGate, errors: list[str]) -> None:
        sock = None
        payload = b"\xa5" * UP_CHUNK
        timeouts = 0
        try:
            sock = self._connect()
            # 给发送设个上限：对端假死（拔网线、进程卡住）时不至于一直阻塞在 sendall 上
            sock.settimeout(max(10.0, self.timeout))
            sock.sendall(pack_size_cmd(CMD_UPLOAD, 0))
            deadline = gate.arm(self.duration)
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    sock.sendall(payload)
                except socket.timeout:
                    timeouts += 1
                    if timeouts >= 2:   # 连着两次发不出去，判定为链路已经断了
                        errors.append("上传流：对端长时间无响应，已中止")
                        break
                    continue
                timeouts = 0
                shared.add(len(payload))
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            sock.settimeout(2.0)
            try:  # 服务端确认收到的字节数，统计时以它为准
                acked.add(unpack_ack(recv_exact(sock, 8)))
            except (OSError, ConnectionError) as exc:
                errors.append(f"上传流：没收到服务端确认（{exc}）")
        except (TestError, OSError, ConnectionError) as exc:
            errors.append(f"上传流：{exc}")
        finally:
            if sock is not None:
                sock.close()
