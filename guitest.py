# -*- coding: utf-8 -*-
"""GUI 端到端冒烟测试：真实弹窗、真实自测一轮，并截图留证。

    python _guitest.py

会依次：开窗口 -> 开启本机服务 -> 自测 127.0.0.1 -> 打印结果与日志 -> 截图。
"""

from __future__ import annotations

import socket
import sys
import tkinter as tk

from lanbench.gui import LanBenchApp

OUT = []


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def shot(root: tk.Tk, name: str) -> None:
    try:
        import ctypes
        import ctypes.wintypes
        import time

        from PIL import ImageGrab

        root.lift()
        root.update_idletasks()
        root.update()
        time.sleep(0.35)
        root.update()
        # 用真实窗口矩形（物理像素）取图，避免 DPI 虚拟化导致的偏移
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
        img.save(name)
        OUT.append(f"截图已保存 {name} ({img.width}x{img.height}) rect={rect.left},{rect.top}")
    except Exception as exc:  # 截图失败不影响功能验证
        OUT.append(f"截图失败：{exc!r}")


def walk(w, out):
    out.append(w)
    for c in w.winfo_children():
        walk(c, out)


def layout_problems(root) -> list[str]:
    """找出被窗口边界裁掉的控件（宽度不够时 pack 会直接切掉后半截）。"""
    root.update_idletasks()
    allw: list = []
    walk(root, allw)
    W, H = root.winfo_width(), root.winfo_height()
    rx, ry = root.winfo_rootx(), root.winfo_rooty()
    bad = []
    for w in allw:
        if w is root or not w.winfo_ismapped():
            continue
        x, y = w.winfo_rootx() - rx, w.winfo_rooty() - ry
        if x + w.winfo_width() > W + 1 or y + w.winfo_height() > H + 1:
            bad.append(
                f"{w.winfo_class()} '{w.cget('text') if 'text' in w.keys() else ''}' "
                f"@({x},{y}) {w.winfo_width()}x{w.winfo_height()} 超出 {W}x{H}"
            )
        p = w.nametowidget(w.winfo_parent())
        if p is not root and p.winfo_ismapped():
            px, py = p.winfo_rootx() - rx, p.winfo_rooty() - ry
            if x + w.winfo_width() > px + p.winfo_width() + 1:
                bad.append(
                    f"{w.winfo_class()} '{w.cget('text') if 'text' in w.keys() else ''}' "
                    f"溢出父容器 {p.winfo_class()}（{w.winfo_width()} > {p.winfo_width()}）"
                )
    req = f"内容请求尺寸 {root.winfo_reqwidth()}x{root.winfo_reqheight()}，窗口 {W}x{H}"
    if root.winfo_reqwidth() > W:
        bad.append("水平方向装不下：" + req)
    OUT.append(req)
    return bad


def main() -> int:
    try:  # 让 Tk / GetWindowRect / ImageGrab 处于同一套像素坐标系，截图才不偏移
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    port = free_port()
    root = tk.Tk()
    app = LanBenchApp(root, port=port, autoserve=True)
    root.attributes("-topmost", True)   # 仅测试用：保证截图不被别的窗口盖住

    errors: list[str] = []

    def on_err(exc, val, tb):
        import traceback

        errors.append("".join(traceback.format_exception(exc, val, tb)))

    root.report_callback_exception = on_err

    app.target.set("127.0.0.1")
    app.target_port.set(port)
    app.duration.set(2)
    app.streams.set(2)
    for var in (app.do_ping, app.do_udp, app.do_download, app.do_upload):
        var.set(True)

    root.after(400, app._self_test)   # 走真实的"自测本机"按钮路径（会自动开服务并同步端口）
    root.after(3200, lambda: (OUT.extend("布局问题：" + p for p in layout_problems(root)),
                              shot(root, "gui_running.png")))
    root.after(8000, lambda: shot(root, "gui_result.png"))

    def finish() -> None:
        OUT.append("--- 指标 ---")
        for label, var in (
            ("下载", app.m_download), ("上传", app.m_upload), ("延迟", app.m_rtt),
            ("抖动", app.m_jitter), ("丢包", app.m_loss),
        ):
            OUT.append(f"{label}: {var.get()}")
        OUT.append("--- 日志 ---")
        OUT.append(app.log.get("1.0", "end").strip())
        OUT.append(f"--- 服务端状态: {app.server_status.get()} ---")
        if app.server is not None:
            OUT.append(f"自测目标端口={app.target_port.get()} 服务真实端口={app.server.actual_port}")
            if app.target_port.get() != app.server.actual_port:
                errors.append("自测本机没有把目标端口同步到真实监听端口")
        root.destroy()

    root.after(9000, finish)
    root.mainloop()

    print("\n".join(OUT))
    if errors:
        print("\n!!! Tk 回调异常 !!!")
        for e in errors:
            print(e)
        return 1
    print("\nGUI 冒烟测试通过（无 Tk 异常）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
