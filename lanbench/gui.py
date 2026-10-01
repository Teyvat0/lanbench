# -*- coding: utf-8 -*-
"""LANBench 图形界面（tkinter）。

界面分三块：
  1. 服务端：本机地址 / 端口 / 一键开启，方便对端来测你；
  2. 测速端：目标地址 / 时长 / 并发流 / 测试项，一键开跑；
  3. 结果与日志：实时速率 + 最终成绩单。

所有耗时操作都在后台线程，线程只往 queue 里塞事件，主线程 after() 轮询刷新，
避免跨线程操作 tk 控件。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import APP_NAME, __version__, netutil
from .client import PingResult, SpeedTester, TestError, ThroughputResult
from .protocol import DEFAULT_PORT
from .server import LanBenchServer, friendly_bind_error

FONT = ("Microsoft YaHei UI", 10)
FONT_BIG = ("Microsoft YaHei UI", 16, "bold")
FONT_UNIT = ("Microsoft YaHei UI", 9)

WIN_W, WIN_H = 920, 700


def fmt_rate(mbps: float) -> str:
    """速率显示：位数越少越好，别把旁边的指标挤歪。"""
    if mbps >= 1000:
        return f"{mbps:,.0f}"
    if mbps >= 100:
        return f"{mbps:.1f}"
    return f"{mbps:.2f}"


class LanBenchApp:
    def __init__(self, root: tk.Tk, port: int = DEFAULT_PORT, autoserve: bool = True) -> None:
        self.root = root
        self.server: LanBenchServer | None = None
        self.tester: SpeedTester | None = None
        self._test_thread: threading.Thread | None = None
        self.events: queue.Queue = queue.Queue()
        self._stage_base, self._stage_span = 0.0, 100.0

        self.default_port = tk.IntVar(value=port)
        self.target = tk.StringVar(value="127.0.0.1")
        self.target_port = tk.IntVar(value=port)
        self.duration = tk.IntVar(value=10)
        self.streams = tk.IntVar(value=4)
        self.do_download = tk.BooleanVar(value=True)
        self.do_upload = tk.BooleanVar(value=True)
        self.do_ping = tk.BooleanVar(value=True)
        self.do_udp = tk.BooleanVar(value=True)
        self.autoserve = tk.BooleanVar(value=autoserve)
        self.local_ip = tk.StringVar(value=(netutil.local_ipv4s() or ["127.0.0.1"])[0])
        self.server_status = tk.StringVar(value="服务未开启")
        self.stage = tk.StringVar(value="待命")
        self.m_download = tk.StringVar(value="--")
        self.m_upload = tk.StringVar(value="--")
        self.m_rtt = tk.StringVar(value="--")
        self.m_jitter = tk.StringVar(value="--")
        self.m_loss = tk.StringVar(value="--")

        self._build()
        self.root.after(80, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.autoserve.get():
            self.root.after(200, self.toggle_server)

    # =============================================================== 界面搭建
    def _build(self) -> None:
        self.root.title(f"{APP_NAME} v{__version__}")
        try:
            style = ttk.Style()
            try:
                style.theme_use("vista")
            except tk.TclError:
                pass
            style.configure(".", font=FONT)
            style.configure("Metric.TLabel", font=FONT_BIG)
            style.configure("Unit.TLabel", font=FONT_UNIT, foreground="#666")
            style.configure("Hint.TLabel", foreground="#777")

            outer = ttk.Frame(self.root, padding=10)
            outer.pack(fill="both", expand=True)

            self._build_server_box(outer)
            self._build_client_box(outer)
            self._build_result_box(outer)
            self._build_log_box(outer)
        finally:
            self._fit_window()

    def _fit_window(self) -> None:
        """按控件真实需求定初始尺寸：不同 DPI / 字体下都不会把控件裁掉。"""
        self.root.update_idletasks()
        req_w, req_h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = min(max(WIN_W, req_w), max(640, sw - 40))
        h = min(max(WIN_H, req_h), max(480, sh - 80))
        self.root.geometry(f"{w}x{h}")
        # 最小尺寸 = 控件需求，用户再怎么缩小也不会出现半截按钮
        self.root.minsize(min(req_w, w), min(req_h, h))

    def _build_server_box(self, parent: ttk.Widget) -> None:
        box = ttk.LabelFrame(parent, text=" ① 本机作为服务端（对端才能测你） ", padding=8)
        box.pack(fill="x")

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="本机地址：").pack(side="left")
        self.ip_combo = ttk.Combobox(
            row, textvariable=self.local_ip, width=16, state="readonly",
            values=netutil.local_ipv4s() or ["127.0.0.1"],
        )
        self.ip_combo.pack(side="left")
        ttk.Button(row, text="复制", width=6, command=self._copy_addr).pack(side="left", padx=(6, 14))

        ttk.Label(row, text="端口：").pack(side="left")
        ttk.Spinbox(row, from_=1, to=65535, textvariable=self.default_port, width=7).pack(side="left", padx=(0, 14))
        self.btn_server = ttk.Button(row, text="开启服务", width=10, command=self.toggle_server)
        self.btn_server.pack(side="left")

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(row2, text="启动时自动开启服务", variable=self.autoserve).pack(side="left")
        ttk.Label(row2, text="状态：").pack(side="left", padx=(14, 0))
        ttk.Label(row2, textvariable=self.server_status).pack(side="left")

    def _build_client_box(self, parent: ttk.Widget) -> None:
        box = ttk.LabelFrame(parent, text=" ② 测速设置 ", padding=8)
        box.pack(fill="x", pady=(8, 0))

        row1 = ttk.Frame(box)
        row1.pack(fill="x")
        ttk.Label(row1, text="目标地址：").pack(side="left")
        ttk.Entry(row1, textvariable=self.target, width=18).pack(side="left")
        ttk.Label(row1, text="端口：").pack(side="left", padx=(10, 0))
        ttk.Spinbox(row1, from_=1, to=65535, textvariable=self.target_port, width=7).pack(side="left")
        ttk.Label(row1, text="时长(秒)：").pack(side="left", padx=(10, 0))
        ttk.Spinbox(row1, from_=2, to=120, textvariable=self.duration, width=5).pack(side="left")
        ttk.Label(row1, text="并发流：").pack(side="left", padx=(10, 0))
        ttk.Spinbox(row1, from_=1, to=32, textvariable=self.streams, width=5).pack(side="left")

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="测试项：").pack(side="left")
        ttk.Checkbutton(row2, text="延迟", variable=self.do_ping).pack(side="left")
        ttk.Checkbutton(row2, text="UDP丢包/抖动", variable=self.do_udp).pack(side="left")
        ttk.Checkbutton(row2, text="下载", variable=self.do_download).pack(side="left")
        ttk.Checkbutton(row2, text="上传", variable=self.do_upload).pack(side="left")
        self.btn_start = ttk.Button(row2, text="开始测速", width=12, command=self.start_test)
        self.btn_start.pack(side="left", padx=(18, 6))
        self.btn_stop = ttk.Button(row2, text="停止", width=8, command=self.stop_test, state="disabled")
        self.btn_stop.pack(side="left")
        ttk.Button(row2, text="自测本机", width=10, command=self._self_test).pack(side="left", padx=6)

        row3 = ttk.Frame(box)
        row3.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(row3, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(row3, textvariable=self.stage, width=40, anchor="w").pack(side="left", padx=(8, 0))

    def _build_result_box(self, parent: ttk.Widget) -> None:
        box = ttk.LabelFrame(parent, text=" ③ 结果 ", padding=8)
        box.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(box)
        row.pack(fill="x")
        # 用 grid + uniform 均分，避免窗口变窄时后面的指标被裁掉
        row.columnconfigure(0, weight=1, uniform="metric")
        for i in range(1, 5):
            row.columnconfigure(i, weight=1, uniform="metric")
        specs = [
            ("下载", self.m_download, "Mbps"),
            ("上传", self.m_upload, "Mbps"),
            ("延迟", self.m_rtt, "ms"),
            ("抖动", self.m_jitter, "ms"),
            ("丢包", self.m_loss, "%"),
        ]
        for i, (title, var, unit) in enumerate(specs):
            cell = ttk.Frame(row)
            cell.grid(row=0, column=i, sticky="nsew")
            ttk.Label(cell, text=title, style="Unit.TLabel").pack(anchor="center")
            line = ttk.Frame(cell)
            line.pack(anchor="center")
            ttk.Label(line, textvariable=var, style="Metric.TLabel").pack(side="left")
            ttk.Label(line, text=unit, style="Unit.TLabel").pack(side="left", padx=(2, 0))

    def _build_log_box(self, parent: ttk.Widget) -> None:
        box = ttk.LabelFrame(parent, text=" 日志 ", padding=6)
        box.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(box, height=8, wrap="word", font=("Consolas", 9), state="disabled")
        bar = ttk.Scrollbar(box, command=self.log.yview)
        self.log.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    # ================================================================== 服务端
    def toggle_server(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None
            self.btn_server.configure(text="开启服务")
            self.server_status.set("服务未开启")
            return
        port = int(self.default_port.get())
        srv = LanBenchServer(port=port, on_log=lambda m: self.events.put(("log", m)))
        try:
            actual = srv.start()
        except OSError as exc:
            messagebox.showerror("无法开启服务", friendly_bind_error(port, exc))
            self.events.put(("log", f"端口 {port} 绑定失败：{exc}"))
            return
        self.server = srv
        self.btn_server.configure(text="关闭服务")
        self.server_status.set(f"监听中 0.0.0.0:{actual}")
        self.events.put(("log", f"提示：其他机器连不上时，请放行防火墙端口 {actual}（需管理员）："))
        self.events.put(
            ("log", f'  netsh advfirewall firewall add rule name="LANBench {actual}" '
                    f"dir=in action=allow protocol=TCP localport={actual}")
        )
        self.events.put(
            ("log", f'  netsh advfirewall firewall add rule name="LANBench {actual} UDP" '
                    f"dir=in action=allow protocol=UDP localport={actual}")
        )

    def _copy_addr(self) -> None:
        # 服务在跑就以真实监听端口为准（用户可能改过端口框但没重启服务）
        port = self.server.actual_port if self.server is not None else int(self.default_port.get())
        addr = f"{self.local_ip.get()}:{port}"
        self.root.clipboard_clear()
        self.root.clipboard_append(addr)
        self.events.put(("log", f"已复制本机地址：{addr}"))

    # ================================================================== 测速端
    def _self_test(self) -> None:
        self.target.set("127.0.0.1")
        if self.server is None:
            self.toggle_server()
        if self.server is not None:
            # 用真实监听端口，避免"端口框改过但服务还在老端口"导致自测连不上
            self.target_port.set(self.server.actual_port)
        self.start_test()

    def start_test(self) -> None:
        if self.tester is not None:
            return
        host = self.target.get().strip() or "127.0.0.1"
        try:
            port = int(self.target_port.get())
            duration = max(1, int(self.duration.get()))
            streams = max(1, int(self.streams.get()))
        except (tk.TclError, ValueError):
            messagebox.showerror("参数错误", "端口 / 时长 / 并发流必须是整数。")
            return

        do_ping, do_udp = self.do_ping.get(), self.do_udp.get()
        do_dl, do_ul = self.do_download.get(), self.do_upload.get()
        if not any((do_ping, do_dl, do_ul)):
            messagebox.showinfo("没有测试项", "至少勾选一项测试内容。")
            return

        self.m_download.set("--")
        self.m_upload.set("--")
        self.m_rtt.set("--")
        self.m_jitter.set("--")
        self.m_loss.set("--")
        # 先把 tester 建好并挂到 self，再启动线程：否则用户在"按钮已可点、
        # 但 worker 还没赋值 self.tester"的空档点【停止】会被静默忽略。
        tester = SpeedTester(
            host, port, duration=duration, streams=streams,
            on_log=lambda m: self.events.put(("log", m)),
            on_stage=lambda m: self.events.put(("stage", m)),
            on_progress=lambda mbps, text, frac: self.events.put(("progress", mbps, text, frac)),
        )
        self.tester = tester
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress.configure(mode="determinate", value=0)
        self._stage_base, self._stage_span = 0.0, 100.0
        self.events.put(("log", f"==== 开始测试 {host}:{port}（时长 {duration}s，{streams} 流）===="))

        t = threading.Thread(
            target=self._test_worker,
            args=(tester, do_ping, do_udp, do_dl, do_ul),
            name="lanbench-test",
            daemon=True,
        )
        self._test_thread = t
        t.start()

    def _test_worker(self, tester: SpeedTester, do_ping, do_udp, do_dl, do_ul) -> None:
        try:
            if do_ping:
                self.events.put(("ping", tester.tcp_ping(10)))
                if tester.server_name:
                    self.events.put(("log", f"对端主机名：{tester.server_name}"))
            if do_udp:
                self.events.put(("udp", tester.udp_test(100)))
            if do_dl and not tester.stopped:
                self.events.put(("download", tester.download()))
            if do_ul and not tester.stopped:
                self.events.put(("upload", tester.upload()))
        except TestError as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:  # 兜底：别让 GUI 静默卡死
            self.events.put(("error", f"未预期错误：{exc!r}"))
        finally:
            self.events.put(("done", tester))

    def stop_test(self) -> None:
        if self.tester is not None:
            self.tester.stop()
            self.events.put(("log", "已请求停止，等待当前阶段收尾…"))

    # =============================================================== 事件循环
    def _pump(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle(ev)
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def _handle(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "log":
            self._append(ev[1])
        elif kind == "stage":
            self.stage.set(ev[1])
            base, span = self._stage_range(ev[1])
            self._stage_base, self._stage_span = base, span
            self.progress.configure(value=base)
            self._append("· " + ev[1])
        elif kind == "progress":
            self._append_live(ev[2], ev[3])
        elif kind == "ping":
            r: PingResult = ev[1]
            self.m_rtt.set(f"{r.rtt_avg:.2f}")
            self.m_jitter.set(f"{r.jitter:.2f}")
        elif kind == "udp":
            r: PingResult = ev[1]
            self.m_loss.set(f"{r.loss * 100:.1f}")
            if r.rtt_avg:
                self.m_jitter.set(f"{r.jitter:.2f}")
        elif kind == "download":
            r: ThroughputResult = ev[1]
            self.m_download.set(fmt_rate(r.mbps))
            self._append(f"下载：{r.mbps:,.1f} Mbps / {r.mib_per_s:.1f} MiB/s")
        elif kind == "upload":
            r: ThroughputResult = ev[1]
            self.m_upload.set(fmt_rate(r.mbps))
            self._append(f"上传：{r.mbps:,.1f} Mbps / {r.mib_per_s:.1f} MiB/s")
        elif kind == "error":
            self._append("✗ " + ev[1])
            self.stage.set("出错")
            self.progress.configure(value=0)
        elif kind == "done":
            # 只有"这一轮"的 tester 才清空：防止用户已经开了新一轮时被旧的 done 事件清掉
            tester = ev[1] if len(ev) > 1 else None
            if tester is None or self.tester is tester:
                self.tester = None
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                self.stage.set("完成")
                self.progress.configure(value=100)
                self._append("==== 测试结束 ====")

    @staticmethod
    def _stage_range(stage_text: str) -> tuple[float, float]:
        """把进度条分成几段，让用户知道大概走到哪一步。"""
        if "下载测试" in stage_text:
            return 30.0, 35.0
        if "上传测试" in stage_text:
            return 65.0, 35.0
        if "UDP" in stage_text:
            return 20.0, 10.0
        if "延迟" in stage_text:
            return 5.0, 15.0
        return 0.0, 100.0

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _append_live(self, text: str, fraction: float) -> None:
        """实时速率：只更新状态栏和进度条，不进日志，避免日志被刷爆。"""
        self.stage.set(text)
        value = self._stage_base + self._stage_span * max(0.0, min(1.0, fraction))
        self.progress.configure(value=value)

    # ================================================================== 关闭
    def _on_close(self) -> None:
        if self.tester is not None:
            self.tester.stop()
        # 让测试线程收尾（下载最多 2s、上传等 ack 最多 2s），免得解释器关闭时刷一堆噪音
        t = self._test_thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        if self.server is not None:
            self.server.stop()
            self.server = None
        self.root.destroy()


def run_gui(port: int = DEFAULT_PORT, autoserve: bool = True) -> int:
    root = tk.Tk()
    LanBenchApp(root, port=port, autoserve=autoserve)
    root.mainloop()
    return 0
